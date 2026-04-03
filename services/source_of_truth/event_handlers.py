from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_
from sqlalchemy import func

from core.config import settings
from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import Episode, Season, Series, Movie, Placeholder
from services.placeholders import episode_placeholder_path, movie_placeholder_path
from services.source_of_truth.observation_trail import enqueue_observation_trail
from services.source_of_truth.status_reconciler import enqueue_status_projection
from services.source_of_truth.arr_api import (
    fetch_radarr_movie,
    fetch_sonarr_episodes,
    fetch_sonarr_series_item,
)
from services.source_of_truth.determiner import run_determination_for_entities
from services.source_of_truth.materializer import run_materialization_for_entities
from services.source_of_truth.import_grace import (
    schedule_episode_import_grace,
    schedule_movie_import_grace,
)
from services.source_of_truth.sync_runner import (
    _episode_fields,
    _movie_fields,
    _resolve_episode_file_payload,
    _series_fields,
    _upsert_episode,
    _upsert_movie,
    _upsert_season,
    _upsert_series,
)


def _infer_is_4k_from_instance(instance: str | None) -> bool:
    """Return True if instance identifier indicates 4K variant."""
    if not instance:
        return False
    normalized = str(instance).strip().lower()
    mapping = getattr(settings, 'instance_is_4k', {}) or {}
    if normalized in mapping:
        return bool(mapping[normalized])
    return normalized.endswith('_4k') or normalized.endswith('4k')


def _resolve_instance_key(content_type: str, instance: str | None, is_4k: bool) -> str:
    normalized = str(instance or '').strip().lower()
    if normalized:
        return normalized
    if content_type == 'movie':
        return settings.RADARR_4K_INSTANCE_KEY if is_4k else settings.RADARR_STD_INSTANCE_KEY
    return settings.SONARR_4K_INSTANCE_KEY if is_4k else settings.SONARR_STD_INSTANCE_KEY


def _resolve_endpoint(content_type: str, is_4k: bool) -> tuple[str, str]:
    if content_type == "movie":
        if is_4k:
            if settings.RADARR_4K_URL and settings.RADARR_4K_API_KEY:
                return settings.RADARR_4K_URL, settings.RADARR_4K_API_KEY
            raise ValueError("unconfigured_4k_instance:movie")
        return settings.RADARR_URL, settings.RADARR_API_KEY

    if is_4k:
        if settings.SONARR_4K_URL and settings.SONARR_4K_API_KEY:
            return settings.SONARR_4K_URL, settings.SONARR_4K_API_KEY
        raise ValueError("unconfigured_4k_instance:series")
    return settings.SONARR_URL, settings.SONARR_API_KEY


def _extract_series_id(payload: dict[str, Any]) -> int | None:
    series = payload.get("series") if isinstance(payload.get("series"), dict) else {}
    val = (
        series.get("id")
        or payload.get("seriesId")
        or payload.get("series_id")
        or payload.get("id")
    )
    try:
        return int(val) if val is not None else None
    except Exception:
        return None


def _extract_movie_id(payload: dict[str, Any]) -> int | None:
    movie = payload.get("movie") if isinstance(payload.get("movie"), dict) else {}
    val = (
        movie.get("id")
        or payload.get("movieId")
        or payload.get("movie_id")
        or payload.get("id")
    )
    try:
        return int(val) if val is not None else None
    except Exception:
        return None


def _extract_episode_pairs(payload: dict[str, Any]) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    episodes = payload.get("episodes") if isinstance(payload.get("episodes"), list) else []
    for ep in episodes:
        if not isinstance(ep, dict):
            continue
        try:
            season_num = int(ep.get("seasonNumber") or 0)
            episode_num = int(ep.get("episodeNumber") or 0)
        except Exception:
            continue
        if season_num >= 0 and episode_num > 0:
            pairs.append((season_num, episode_num))
    return pairs


def process_series_add_event(payload: dict[str, Any], instance: str | None = None) -> dict[str, Any]:
    """Process one seriesadd event using full-sync compatible ingest/materialize logic."""
    stats: dict[str, Any] = {
        "series_upserted": 0,
        "seasons_upserted": 0,
        "episodes_upserted": 0,
        "episodes_touched": 0,
    }

    series_id = _extract_series_id(payload)
    if not series_id:
        raise ValueError("seriesadd_missing_series_id")

    inferred_is_4k = _infer_is_4k_from_instance(instance)
    resolved_instance_key = _resolve_instance_key('series', instance, inferred_is_4k)
    base_url, api_key = _resolve_endpoint("series", inferred_is_4k)
    if not base_url or not api_key:
        raise ValueError("seriesadd_missing_sonarr_config")

    series_entry = fetch_sonarr_series_item(series_id, base_url, api_key)
    if not isinstance(series_entry, dict):
        logger.warning(
            f"Series add ARR lookup failed for series_id={series_id}; falling back to webhook payload.",
            extra={'emoji_type': 'warning'},
        )
        series_entry = payload.get("series") if isinstance(payload.get("series"), dict) else None
    if not isinstance(series_entry, dict):
        raise ValueError("seriesadd_missing_series_payload")

    session = get_session()
    try:
        s_fields = _series_fields(series_entry, inferred_is_4k, resolved_instance_key)
        if not s_fields.get("tvdbid"):
            raise ValueError("seriesadd_missing_tvdbid")

        series_row, _ = _upsert_series(session, s_fields)
        stats["series_upserted"] = 1

        episodes = fetch_sonarr_episodes(series_id, base_url, api_key)
        if not episodes and isinstance(payload.get("episodes"), list):
            logger.warning(
                f"Series add episodes lookup returned no results for series_id={series_id}; using payload episodes when available.",
                extra={'emoji_type': 'warning'},
            )
            episodes = payload.get("episodes") or []

        include_specials = bool(getattr(settings, "INCLUDE_SPECIALS", False))
        season_rows_by_number: dict[int, Season] = {}
        season_rollups: dict[int, dict[str, int | bool | str | None]] = {}
        season_overview_by_number: dict[int, str] = {}
        touched_episode_rows: list[Episode] = []
        missing_placeholder_episode_rows: list[Episode] = []

        for ep in episodes:
            season_number = int(ep.get("seasonNumber") or 0)
            if season_number == 0 and not include_specials:
                continue

            season_row, season_created = _upsert_season(session, series_row, season_number)
            season_rows_by_number[season_number] = season_row
            if season_created:
                stats["seasons_upserted"] += 1

            episode_file = _resolve_episode_file_payload(ep, base_url, api_key)
            ep_fields = _episode_fields(series_row, season_row, ep, episode_file)

            overview = str(ep.get("overview") or "").strip()
            if overview and season_number not in season_overview_by_number:
                season_overview_by_number[season_number] = overview

            rollup = season_rollups.setdefault(
                season_number,
                {"files": 0, "episodes": 0, "monitored": False, "status": None},
            )
            rollup["episodes"] = int(rollup["episodes"] or 0) + 1
            if ep_fields.get("has_file"):
                rollup["files"] = int(rollup["files"] or 0) + 1
            rollup["monitored"] = bool(rollup["monitored"]) or bool(ep_fields.get("sonarr_monitored"))
            if not rollup.get("status") and ep_fields.get("sonarr_status"):
                rollup["status"] = ep_fields.get("sonarr_status")

            ep_row, ep_created = _upsert_episode(session, ep_fields)

            # Event-scoped FS truth check: if users manually removed placeholder files
            # out-of-band, clear stale DB placeholder flags for this episode before determination.
            expected_path = episode_placeholder_path(ep_row, season_row, series_row)
            exists_on_disk = os.path.isfile(expected_path)
            ep_row.has_placeholder = bool(exists_on_disk)
            ep_row.placeholder_filepath = expected_path if exists_on_disk else None
            if not exists_on_disk:
                missing_placeholder_episode_rows.append(ep_row)

            touched_episode_rows.append(ep_row)
            if ep_created:
                stats["episodes_upserted"] += 1

        for season_number, season_row in season_rows_by_number.items():
            rollup = season_rollups.get(season_number) or {}
            files_count = int(rollup.get("files") or 0)
            season_row.has_files = bool(files_count)
            season_row.seasonfile_count = files_count
            season_row.sonarr_monitored = bool(rollup.get("monitored"))
            if rollup.get("status"):
                season_row.sonarr_status = str(rollup["status"])
            if season_overview_by_number.get(season_number):
                season_row.sonarr_season_overview = season_overview_by_number[season_number]
            season_row.is_deleted = False
            session.add(season_row)

        missing_episode_ids = [
            int(ep.id) for ep in missing_placeholder_episode_rows if getattr(ep, "id", None)
        ]
        if missing_episode_ids:
            stale_rows = (
                session.query(Placeholder)
                .filter(Placeholder.episode_id.in_(missing_episode_ids), Placeholder.has_placeholder == True)  # noqa: E712
                .all()
            )
            for row in stale_rows:
                row.has_placeholder = False
                if hasattr(row, "lifecycle_status"):
                    row.lifecycle_status = "MISSING"
                row.plex_placeholder_id = None
                row.jellyfin_placeholder_id = None
                row.emby_placeholder_id = None
                row.plex_id_observed_at = None
                row.jellyfin_id_observed_at = None
                row.emby_id_observed_at = None
                row.media_lookup_error = None
                row.media_lookup_last_attempt_at = None
                row.last_observed_at = func.now()
                row.updated_at = func.now()
                session.add(row)

        session.flush()
        episode_ids = [int(ep.id) for ep in touched_episode_rows if getattr(ep, "id", None)]
        stats["episodes_touched"] = len(episode_ids)
        session.commit()

        determination_stats = run_determination_for_entities(episode_ids=episode_ids)
        materialization_stats = run_materialization_for_entities(
            episode_ids=episode_ids,
            observation_source="event_series_add",
        )

        followup = {"observation_trail_enqueued": 0, "status_projection_enqueued": 0, "placeholder_ids": []}

        # Gap fix parity with movie-add: when placeholders already exist, scoped
        # materialization is a no-op, so enqueue observation + status projection.
        if int(determination_stats.get("placeholder_exists", 0) or 0) > 0:
            follow_session = get_session()
            try:
                active_rows = (
                    follow_session.query(Placeholder)
                    .filter(Placeholder.episode_id.in_(episode_ids), Placeholder.has_placeholder == True)  # noqa: E712
                    .all()
                )
                placeholder_ids = [int(row.id) for row in active_rows if getattr(row, "id", None)]
                followup["placeholder_ids"] = placeholder_ids
                if placeholder_ids:
                    obs_result = enqueue_observation_trail(
                        follow_session,
                        placeholder_ids=placeholder_ids,
                        source="event_series_add_existing_placeholder",
                        delay_seconds=0,
                    )
                    followup["observation_trail_enqueued"] = 1 if obs_result.get("enqueued") else 0

                    proj_result = enqueue_status_projection(placeholder_ids, session=follow_session)
                    followup["status_projection_enqueued"] = 1 if proj_result.get("ok") else 0
            finally:
                follow_session.close()

        return {
            "ok": True,
            "event": "seriesadd",
            "is_4k": inferred_is_4k,
            "series_id": int(series_row.id),
            "episode_ids": episode_ids,
            "upsert_stats": stats,
            "determination": determination_stats,
            "materialization": materialization_stats,
            "followup": followup,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def process_movie_add_event(payload: dict[str, Any], instance: str | None = None) -> dict[str, Any]:
    """Process one movieadd event using full-sync compatible ingest/materialize logic."""
    movie_id = _extract_movie_id(payload)
    if not movie_id:
        raise ValueError("movieadd_missing_movie_id")

    inferred_is_4k = _infer_is_4k_from_instance(instance)
    resolved_instance_key = _resolve_instance_key('movie', instance, inferred_is_4k)
    base_url, api_key = _resolve_endpoint("movie", inferred_is_4k)
    if not base_url or not api_key:
        raise ValueError("movieadd_missing_radarr_config")

    movie_entry = fetch_radarr_movie(movie_id, base_url, api_key)
    if not isinstance(movie_entry, dict):
        logger.warning(
            f"Movie add ARR lookup failed for movie_id={movie_id}; falling back to webhook payload.",
            extra={'emoji_type': 'warning'},
        )
        movie_entry = payload.get("movie") if isinstance(payload.get("movie"), dict) else None
    if not isinstance(movie_entry, dict):
        raise ValueError("movieadd_missing_movie_payload")

    session = get_session()
    try:
        fields = _movie_fields(movie_entry, inferred_is_4k, resolved_instance_key)
        if not fields.get("tmdbid"):
            raise ValueError("movieadd_missing_tmdbid")

        movie_row, _ = _upsert_movie(session, fields)
        movie_row.last_found_in_radarr = datetime.now(timezone.utc)

        # Event-scoped FS truth check: if users manually removed placeholder files
        # out-of-band, clear stale DB placeholder flags for this movie before determination.
        expected_path = movie_placeholder_path(movie_row)
        exists_on_disk = os.path.isfile(expected_path)
        movie_row.has_placeholder = bool(exists_on_disk)
        movie_row.placeholder_filepath = expected_path if exists_on_disk else None
        if not exists_on_disk:
            stale_rows = (
                session.query(Placeholder)
                .filter(Placeholder.movie_id == movie_row.id, Placeholder.has_placeholder == True)  # noqa: E712
                .all()
            )
            for row in stale_rows:
                row.has_placeholder = False
                if hasattr(row, "lifecycle_status"):
                    row.lifecycle_status = "MISSING"
                row.plex_placeholder_id = None
                row.jellyfin_placeholder_id = None
                row.emby_placeholder_id = None
                row.plex_id_observed_at = None
                row.jellyfin_id_observed_at = None
                row.emby_id_observed_at = None
                row.media_lookup_error = None
                row.media_lookup_last_attempt_at = None
                row.last_observed_at = func.now()
                row.updated_at = func.now()
                session.add(row)

        session.add(movie_row)
        session.flush()
        movie_row_id = int(movie_row.id)
        session.commit()

        determination_stats = run_determination_for_entities(movie_ids=[movie_row_id])
        materialization_stats = run_materialization_for_entities(
            movie_ids=[movie_row_id],
            observation_source="event_movie_add",
        )

        followup = {"observation_trail_enqueued": 0, "status_projection_enqueued": 0, "placeholder_ids": []}

        # Gap fix: when placeholder already exists, scoped materialization is a no-op,
        # so we still enqueue observation + status projection for this movie's active placeholders.
        if int(determination_stats.get("placeholder_exists", 0) or 0) > 0:
            follow_session = get_session()
            try:
                active_rows = (
                    follow_session.query(Placeholder)
                    .filter(Placeholder.movie_id == movie_row_id, Placeholder.has_placeholder == True)  # noqa: E712
                    .all()
                )
                placeholder_ids = [int(row.id) for row in active_rows if getattr(row, "id", None)]
                followup["placeholder_ids"] = placeholder_ids
                if placeholder_ids:
                    obs_result = enqueue_observation_trail(
                        follow_session,
                        placeholder_ids=placeholder_ids,
                        source="event_movie_add_existing_placeholder",
                        delay_seconds=0,
                    )
                    followup["observation_trail_enqueued"] = 1 if obs_result.get("enqueued") else 0

                    proj_result = enqueue_status_projection(placeholder_ids, session=follow_session)
                    followup["status_projection_enqueued"] = 1 if proj_result.get("ok") else 0
            finally:
                follow_session.close()

        return {
            "ok": True,
            "event": "movieadd",
            "is_4k": inferred_is_4k,
            "movie_id": movie_row_id,
            "determination": determination_stats,
            "materialization": materialization_stats,
            "followup": followup,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def process_movie_imported_event(payload: dict[str, Any], instance: str | None = None) -> dict[str, Any]:
    """Process movie import by scheduling a delayed grace cleanup workflow."""
    movie_id = _extract_movie_id(payload)
    if not movie_id:
        raise ValueError("movieimport_missing_movie_id")

    inferred_is_4k = _infer_is_4k_from_instance(instance)
    resolved_instance_key = _resolve_instance_key('movie', instance, inferred_is_4k)
    movie_file = payload.get("movieFile", {}) if isinstance(payload.get("movieFile"), dict) else {}
    file_path = movie_file.get("path") if movie_file else None

    session = get_session()
    try:
        # Strict match: radarrid + is_4k
        movie_row = session.query(Movie).filter(
            Movie.radarrid == movie_id,
            Movie.instance_key == resolved_instance_key,
        ).first()

        # Lenient fallback: radarrid only
        if not movie_row:
            movie_row = session.query(Movie).filter(
                Movie.radarrid == movie_id
            ).first()

        if not movie_row:
            raise ValueError(f"movieimport_not_found:radarrid={movie_id}")

        # Do not mark has_file immediately. A delayed grace job finalizes state and cleanup.
        session.add(movie_row)
        session.flush()
        movie_row_id = int(movie_row.id)

        grace_stats = schedule_movie_import_grace(
            session,
            movie_row_id=movie_row_id,
            file_path=file_path,
        )
        session.commit()

        return {
            "ok": True,
            "event": "movie_imported",
            "is_4k": inferred_is_4k,
            "movie_id": movie_row_id,
            "grace": grace_stats,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def process_episode_imported_event(payload: dict[str, Any], instance: str | None = None) -> dict[str, Any]:
    """Process episode import by scheduling a delayed grace cleanup workflow."""
    episode_ids = _extract_episode_ids(payload)
    if not episode_ids:
        raise ValueError("episodeimport_missing_episode_id")

    series_id = _extract_series_id(payload)
    inferred_is_4k = _infer_is_4k_from_instance(instance)
    resolved_instance_key = _resolve_instance_key('series', instance, inferred_is_4k)
    episode_file = payload.get("episodeFile") if isinstance(payload.get("episodeFile"), dict) else {}
    file_path = episode_file.get("path") if episode_file else None

    session = get_session()
    try:
        # Strict match: episode sonarrid(s) + source instance key
        episode_rows = (
            session.query(Episode)
            .join(Season, Episode.season_id == Season.id)
            .join(Series, Season.series_id == Series.id)
            .filter(Episode.sonarrid.in_(episode_ids), Series.instance_key == resolved_instance_key)
            .all()
        )

        # Lenient fallback: series_id + season/episode pair(s) from payload.
        if not episode_rows and series_id:
            for season_num, episode_num in _extract_episode_pairs(payload):
                matched = (
                    session.query(Episode)
                    .join(Season, Episode.season_id == Season.id)
                    .join(Series, Season.series_id == Series.id)
                    .filter(
                        Series.sonarrid == series_id,
                        Series.instance_key == resolved_instance_key,
                        Season.season_number == season_num,
                        Episode.episode_number == episode_num,
                    )
                    .all()
                )
                if matched:
                    episode_rows.extend(matched)

        # Final lenient fallback: sonarr episode ids without instance-key scoping.
        if not episode_rows:
            episode_rows = session.query(Episode).filter(Episode.sonarrid.in_(episode_ids)).all()

        deduped_episode_rows: list[Episode] = []
        seen_row_ids: set[int] = set()
        for episode_row in episode_rows:
            row_id = int(getattr(episode_row, 'id', 0) or 0)
            if not row_id or row_id in seen_row_ids:
                continue
            seen_row_ids.add(row_id)
            deduped_episode_rows.append(episode_row)

        if not deduped_episode_rows:
            raise ValueError(f"episodeimport_not_found:ids={episode_ids}")

        episode_row_ids: list[int] = []
        grace_stats: list[dict[str, Any]] = []
        for episode_row in deduped_episode_rows:
            # Do not mark has_file immediately. A delayed grace job finalizes state and cleanup.
            session.add(episode_row)
            session.flush()
            episode_row_id = int(episode_row.id)
            episode_row_ids.append(episode_row_id)
            grace_stats.append(
                schedule_episode_import_grace(
                    session,
                    episode_row_id=episode_row_id,
                    file_path=file_path,
                )
            )
        session.commit()

        return {
            "ok": True,
            "event": "episode_imported",
            "is_4k": inferred_is_4k,
            "episode_id": episode_row_ids[0],
            "episode_ids": episode_row_ids,
            "grace": grace_stats,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _extract_episode_id(payload: dict[str, Any]) -> int | None:
    """Extract episode sonarr ID from payload."""
    episode_ids = _extract_episode_ids(payload)
    return episode_ids[0] if episode_ids else None


def _extract_episode_ids(payload: dict[str, Any]) -> list[int]:
    """Extract one or more episode sonarr IDs from payload."""
    episode = payload.get("episode") if isinstance(payload.get("episode"), dict) else {}
    episode_ids: list[int] = []

    candidates = [
        episode.get("id"),
        payload.get("episodeId"),
        payload.get("episode_id"),
    ]

    episodes = payload.get("episodes") if isinstance(payload.get("episodes"), list) else []
    for item in episodes:
        if isinstance(item, dict):
            candidates.append(item.get("id"))
            candidates.append(item.get("episodeId"))
            candidates.append(item.get("episode_id"))

    seen: set[int] = set()
    for value in candidates:
        try:
            normalized = int(value) if value is not None else None
        except Exception:
            normalized = None
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        episode_ids.append(normalized)
    return episode_ids


def _extract_first_episode_pair(payload: dict[str, Any]) -> tuple[int, int] | None:
    """Extract first episode's (season_number, episode_number) pair from payload."""
    episodes = payload.get("episodes") if isinstance(payload.get("episodes"), list) else []
    if not episodes:
        return None
    
    first_ep = episodes[0] if isinstance(episodes[0], dict) else {}
    try:
        season_num = int(first_ep.get("seasonNumber") or 0)
        episode_num = int(first_ep.get("episodeNumber") or 0)
        if season_num >= 0 and episode_num > 0:
            return (season_num, episode_num)
    except Exception:
        pass
    return None


def process_movie_file_deleted_event(payload: dict[str, Any], instance: str | None = None) -> dict[str, Any]:
    """Process movie file-deleted event by resetting has_file and rerunning determination/materialization."""
    movie_id = _extract_movie_id(payload)
    if not movie_id:
        raise ValueError("moviefiledelete_missing_movie_id")

    inferred_is_4k = _infer_is_4k_from_instance(instance)
    resolved_instance_key = _resolve_instance_key('movie', instance, inferred_is_4k)

    session = get_session()
    try:
        # Strict match: radarrid + is_4k (instance-aware)
        movie_row = session.query(Movie).filter(
            Movie.radarrid == movie_id,
            Movie.instance_key == resolved_instance_key,
        ).first()

        if not movie_row:
            raise ValueError(f"moviefiledelete_movie_not_found:radarrid={movie_id}:is_4k={inferred_is_4k}")

        # Reset file state
        movie_row.has_file = False
        movie_row.radarr_filepath = None
        session.add(movie_row)
        session.flush()
        movie_row_id = int(movie_row.id)
        session.commit()

        determination_stats = run_determination_for_entities(movie_ids=[movie_row_id])
        materialization_stats = run_materialization_for_entities(
            movie_ids=[movie_row_id],
            observation_source="event_movie_file_deleted",
        )

        return {
            "ok": True,
            "event": "movie_file_deleted",
            "is_4k": inferred_is_4k,
            "movie_id": movie_row_id,
            "determination": determination_stats,
            "materialization": materialization_stats,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def process_movie_deleted_event(payload: dict[str, Any], instance: str | None = None) -> dict[str, Any]:
    """Process movie delete event by marking deleted and running targeted determination/materialization."""
    movie_id = _extract_movie_id(payload)
    inferred_is_4k = _infer_is_4k_from_instance(instance)
    resolved_instance_key = _resolve_instance_key('movie', instance, inferred_is_4k)

    session = get_session()
    try:
        movie_row = None
        if movie_id:
            movie_row = session.query(Movie).filter(
                Movie.radarrid == movie_id,
                Movie.instance_key == resolved_instance_key,
            ).first()
            if not movie_row:
                movie_row = session.query(Movie).filter(Movie.radarrid == movie_id).first()

        if not movie_row:
            movie_payload = payload.get("movie") if isinstance(payload.get("movie"), dict) else {}
            tmdb_id = movie_payload.get("tmdbId")
            try:
                tmdb_id_int = int(tmdb_id) if tmdb_id is not None else None
            except Exception:
                tmdb_id_int = None
            if tmdb_id_int:
                movie_row = session.query(Movie).filter(
                    Movie.tmdbid == tmdb_id_int,
                    Movie.instance_key == resolved_instance_key,
                ).first()

        if not movie_row:
            raise ValueError(f"moviedelete_movie_not_found:radarrid={movie_id}:is_4k={inferred_is_4k}")

        movie_row.is_deleted = True
        movie_row.has_file = False
        movie_row.radarr_filepath = None
        session.add(movie_row)
        session.flush()
        movie_row_id = int(movie_row.id)
        session.commit()

        determination_stats = run_determination_for_entities(movie_ids=[movie_row_id])
        materialization_stats = run_materialization_for_entities(
            movie_ids=[movie_row_id],
            observation_source="event_movie_deleted",
        )

        return {
            "ok": True,
            "event": "movie_deleted",
            "is_4k": inferred_is_4k,
            "movie_id": movie_row_id,
            "determination": determination_stats,
            "materialization": materialization_stats,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def process_episode_file_deleted_event(payload: dict[str, Any], instance: str | None = None) -> dict[str, Any]:
    """Process episode file-deleted event by resetting has_file and rerunning determination/materialization."""
    series_id = _extract_series_id(payload)
    if not series_id:
        raise ValueError("episodefiledelete_missing_series_id")

    inferred_is_4k = _infer_is_4k_from_instance(instance)
    resolved_instance_key = _resolve_instance_key('series', instance, inferred_is_4k)

    session = get_session()
    try:
        # Strict match: sonarrid + is_4k (instance-aware)
        series_row = session.query(Series).filter(
            Series.sonarrid == series_id,
            Series.instance_key == resolved_instance_key,
        ).first()

        if not series_row:
            raise ValueError(f"episodefiledelete_series_not_found:sonarrid={series_id}:is_4k={inferred_is_4k}")

        episode_rows: list[Episode] = []
        
        # Strict match: episode sonarrid + is_4k
        episode_id = _extract_episode_id(payload)
        if episode_id:
            ep_row = (
                session.query(Episode)
                .join(Season, Episode.season_id == Season.id)
                .join(Series, Season.series_id == Series.id)
                .filter(Episode.sonarrid == episode_id, Series.instance_key == resolved_instance_key)
                .first()
            )
            if ep_row:
                episode_rows = [ep_row]

        # Strict fallback: resolve by series + season/episode + is_4k
        if not episode_rows:
            pair = _extract_first_episode_pair(payload)
            if pair:
                season_num, episode_num = pair
                episode_rows = session.query(Episode).join(
                    Season, Episode.season_id == Season.id
                ).filter(
                    Season.series_id == series_row.id,
                    Season.season_number == season_num,
                    Episode.episode_number == episode_num,
                ).all()

        if not episode_rows:
            raise ValueError(f"episodefiledelete_episode_not_found:series={series_id}")

        # Reset file state for all matched episodes
        for ep_row in episode_rows:
            ep_row.has_file = False
            ep_row.sonarr_filepath = None
            session.add(ep_row)

        session.flush()
        episode_ids = [int(ep.id) for ep in episode_rows if getattr(ep, "id", None)]
        session.commit()

        determination_stats = run_determination_for_entities(episode_ids=episode_ids)
        materialization_stats = run_materialization_for_entities(
            episode_ids=episode_ids,
            observation_source="event_episode_file_deleted",
        )

        return {
            "ok": True,
            "event": "episode_file_deleted",
            "is_4k": inferred_is_4k,
            "series_id": int(series_row.id),
            "episode_ids": episode_ids,
            "determination": determination_stats,
            "materialization": materialization_stats,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def process_series_deleted_event(payload: dict[str, Any], instance: str | None = None) -> dict[str, Any]:
    """Process series delete event by marking deleted and running targeted determination/materialization."""
    sonarr_series_id = _extract_series_id(payload)
    if not sonarr_series_id:
        raise ValueError("seriesdelete_missing_series_id")

    inferred_is_4k = _infer_is_4k_from_instance(instance)
    resolved_instance_key = _resolve_instance_key('series', instance, inferred_is_4k)

    session = get_session()
    try:
        series_row = session.query(Series).filter(
            Series.sonarrid == sonarr_series_id,
            Series.instance_key == resolved_instance_key,
        ).first()

        if not series_row:
            series_row = session.query(Series).filter(Series.sonarrid == sonarr_series_id).first()

        if not series_row:
            series_payload = payload.get("series") if isinstance(payload.get("series"), dict) else {}
            tvdb_id = series_payload.get("tvdbId") or series_payload.get("tvdbid")
            try:
                tvdb_id_int = int(tvdb_id) if tvdb_id is not None else None
            except Exception:
                tvdb_id_int = None
            if tvdb_id_int:
                series_row = session.query(Series).filter(
                    Series.tvdbid == tvdb_id_int,
                    Series.instance_key == resolved_instance_key,
                ).first()

        if not series_row:
            raise ValueError(f"seriesdelete_series_not_found:sonarrid={sonarr_series_id}:is_4k={inferred_is_4k}")

        series_row.is_deleted = True
        series_row.has_files = False
        series_row.seriesfile_count = 0
        session.add(series_row)
        session.flush()

        season_ids = [
            int(row[0])
            for row in session.query(Season.id).filter(Season.series_id == series_row.id).all()
        ]
        episode_ids: list[int] = []
        if season_ids:
            episode_ids = [
                int(row[0])
                for row in session.query(Episode.id).filter(Episode.season_id.in_(season_ids)).all()
            ]

            session.query(Season).filter(Season.id.in_(season_ids)).update(
                {
                    Season.is_deleted: True,
                    Season.has_files: False,
                    Season.seasonfile_count: 0,
                },
                synchronize_session=False,
            )

            session.query(Episode).filter(Episode.id.in_(episode_ids)).update(
                {
                    Episode.is_deleted: True,
                    Episode.has_file: False,
                    Episode.sonarr_filepath: None,
                },
                synchronize_session=False,
            )

        session.commit()

        determination_stats = run_determination_for_entities(episode_ids=episode_ids)
        materialization_stats = run_materialization_for_entities(
            episode_ids=episode_ids,
            observation_source="event_series_deleted",
        )

        return {
            "ok": True,
            "event": "series_deleted",
            "is_4k": inferred_is_4k,
            "series_id": int(series_row.id),
            "episode_ids": episode_ids,
            "determination": determination_stats,
            "materialization": materialization_stats,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
