from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_

from core.config import settings
from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import Episode, Season, Series
from services.source_of_truth.arr_api import (
    fetch_radarr_movie,
    fetch_sonarr_episodes,
    fetch_sonarr_series_item,
)
from services.source_of_truth.determiner import run_determination_for_entities
from services.source_of_truth.materializer import run_materialization_for_entities
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
    return instance.lower().endswith('_4k') or instance.lower().endswith('4k')


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
        s_fields = _series_fields(series_entry, inferred_is_4k)
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

        session.flush()
        episode_ids = [int(ep.id) for ep in touched_episode_rows if getattr(ep, "id", None)]
        stats["episodes_touched"] = len(episode_ids)
        session.commit()

        determination_stats = run_determination_for_entities(episode_ids=episode_ids)
        materialization_stats = run_materialization_for_entities(
            episode_ids=episode_ids,
            observation_source="event_series_add",
        )

        return {
            "ok": True,
            "event": "seriesadd",
            "is_4k": inferred_is_4k,
            "series_id": int(series_row.id),
            "episode_ids": episode_ids,
            "upsert_stats": stats,
            "determination": determination_stats,
            "materialization": materialization_stats,
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
        fields = _movie_fields(movie_entry, inferred_is_4k)
        if not fields.get("tmdbid"):
            raise ValueError("movieadd_missing_tmdbid")

        movie_row, _ = _upsert_movie(session, fields)
        movie_row.last_found_in_radarr = datetime.now(timezone.utc)
        session.add(movie_row)
        session.flush()
        movie_row_id = int(movie_row.id)
        session.commit()

        determination_stats = run_determination_for_entities(movie_ids=[movie_row_id])
        materialization_stats = run_materialization_for_entities(
            movie_ids=[movie_row_id],
            observation_source="event_movie_add",
        )

        return {
            "ok": True,
            "event": "movieadd",
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


def process_movie_imported_event(payload: dict[str, Any], instance: str | None = None) -> dict[str, Any]:
    """Process one movie import event by reusing add-event ingest semantics.

    Import events should converge quickly to ARR has_file truth and run determination/
    materialization to clear placeholders when obsolete.
    """
    result = process_movie_add_event(payload, instance=instance)
    result["event"] = "movie_imported"
    return result


def process_episode_imported_event(payload: dict[str, Any], instance: str | None = None) -> dict[str, Any]:
    """Process one episode import event and upsert only touched episodes when provided."""
    stats: dict[str, Any] = {
        "series_upserted": 0,
        "seasons_upserted": 0,
        "episodes_upserted": 0,
        "episodes_touched": 0,
    }

    series_id = _extract_series_id(payload)
    if not series_id:
        raise ValueError("episodeimport_missing_series_id")

    inferred_is_4k = _infer_is_4k_from_instance(instance)
    base_url, api_key = _resolve_endpoint("series", inferred_is_4k)
    if not base_url or not api_key:
        raise ValueError("episodeimport_missing_sonarr_config")

    series_entry = fetch_sonarr_series_item(series_id, base_url, api_key)
    if not isinstance(series_entry, dict):
        logger.warning(
            f"Episode import ARR lookup failed for series_id={series_id}; falling back to webhook payload.",
            extra={'emoji_type': 'warning'},
        )
        series_entry = payload.get("series") if isinstance(payload.get("series"), dict) else None
    if not isinstance(series_entry, dict):
        raise ValueError("episodeimport_missing_series_payload")

    webhook_episode_pairs = _extract_episode_pairs(payload)
    payload_episodes = payload.get("episodes") if isinstance(payload.get("episodes"), list) else []

    session = get_session()
    try:
        s_fields = _series_fields(series_entry, inferred_is_4k)
        if not s_fields.get("tvdbid"):
            raise ValueError("episodeimport_missing_tvdbid")

        series_row, _ = _upsert_series(session, s_fields)
        stats["series_upserted"] = 1

        episodes_source: list[dict[str, Any]] = []
        if webhook_episode_pairs:
            sonarr_episodes = fetch_sonarr_episodes(series_id, base_url, api_key)
            pair_map = {(int(season), int(episode)) for season, episode in webhook_episode_pairs}
            episodes_source = [
                ep
                for ep in sonarr_episodes
                if isinstance(ep, dict)
                and (int(ep.get("seasonNumber") or 0), int(ep.get("episodeNumber") or 0)) in pair_map
            ]
            if not episodes_source:
                # Fallback to raw webhook episodes when Sonarr lookup did not include them yet.
                episodes_source = [ep for ep in payload_episodes if isinstance(ep, dict)]
        else:
            episodes_source = [ep for ep in payload_episodes if isinstance(ep, dict)]

        if not episodes_source:
            raise ValueError("episodeimport_missing_episode_payload")

        include_specials = bool(getattr(settings, "INCLUDE_SPECIALS", False))
        season_rows_by_number: dict[int, Season] = {}
        touched_episode_rows: list[Episode] = []
        payload_episode_file = payload.get("episodeFile") if isinstance(payload.get("episodeFile"), dict) else {}

        for ep in episodes_source:
            season_number = int(ep.get("seasonNumber") or 0)
            if season_number == 0 and not include_specials:
                continue

            season_row, season_created = _upsert_season(session, series_row, season_number)
            season_rows_by_number[season_number] = season_row
            if season_created:
                stats["seasons_upserted"] += 1

            ep_entry = dict(ep)
            if payload_episode_file and not isinstance(ep_entry.get("episodeFile"), dict):
                ep_entry["episodeFile"] = payload_episode_file

            episode_file = _resolve_episode_file_payload(ep_entry, base_url, api_key)
            if not episode_file and payload_episode_file:
                episode_file = payload_episode_file

            ep_fields = _episode_fields(series_row, season_row, ep_entry, episode_file)
            ep_fields["has_file"] = True
            if not ep_fields.get("sonarr_filepath") and isinstance(payload_episode_file, dict):
                fallback_path = str(payload_episode_file.get("path") or "").strip()
                if fallback_path:
                    ep_fields["sonarr_filepath"] = fallback_path

            ep_row, ep_created = _upsert_episode(session, ep_fields)
            touched_episode_rows.append(ep_row)
            if ep_created:
                stats["episodes_upserted"] += 1

        # Refresh lightweight season aggregate indicators for touched seasons.
        for season_number, season_row in season_rows_by_number.items():
            files_count = (
                session.query(Episode)
                .filter(and_(Episode.season_id == season_row.id, Episode.has_file.is_(True), Episode.is_deleted.is_(False)))
                .count()
            )
            season_row.has_files = bool(files_count)
            season_row.seasonfile_count = int(files_count)
            season_row.is_deleted = False
            session.add(season_row)

        session.flush()
        episode_ids = [int(ep.id) for ep in touched_episode_rows if getattr(ep, "id", None)]
        stats["episodes_touched"] = len(episode_ids)
        session.commit()

        determination_stats = run_determination_for_entities(episode_ids=episode_ids)
        materialization_stats = run_materialization_for_entities(
            episode_ids=episode_ids,
            observation_source="event_episode_imported",
        )

        return {
            "ok": True,
            "event": "episode_imported",
            "is_4k": inferred_is_4k,
            "series_id": int(series_row.id),
            "episode_ids": episode_ids,
            "upsert_stats": stats,
            "determination": determination_stats,
            "materialization": materialization_stats,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
