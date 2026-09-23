"""Rematerialize Placeholdarr folders after destination-map / LIBRARY_ROOT changes."""

from __future__ import annotations

import os
from typing import Any

from core.logger import logger
from services.library_destinations import (
    ensure_dest_folders_exist,
    resolve_movie_dest,
    resolve_series_dest,
)
from services.postgres.db import get_session
from services.postgres.models import Episode, Movie, Season, Series


def _safe_unlink(path: str | None) -> bool:
    text = str(path or "").strip()
    if not text or not os.path.isfile(text):
        return False
    try:
        os.unlink(text)
        return True
    except OSError:
        return False


def recompute_catalog_placeholder_folders(session) -> dict[str, Any]:
    """Rewrite movie/series/season/episode placeholder_folder from the current destination map.

    Clears placeholder filepaths when the folder changes so materialization recreates files.
    Best-effort deletes the previous placeholder media file.
    """
    stats = {
        "movies_updated": 0,
        "movies_cleared_filepath": 0,
        "series_updated": 0,
        "seasons_updated": 0,
        "episodes_updated": 0,
        "episodes_cleared_filepath": 0,
        "touched_movie_row_ids": [],
        "touched_episode_row_ids": [],
    }

    ensure_dest_folders_exist()

    movie_ids: list[int] = []
    for movie in session.query(Movie).filter(Movie.is_deleted == False).all():  # noqa: E712
        arr_path = getattr(movie, "radarrpath", None)
        instance_key = str(getattr(movie, "instance_key", None) or "").strip().lower() or None
        dest = resolve_movie_dest(
            instance_key=instance_key,
            arr_path=arr_path if isinstance(arr_path, str) else None,
        )
        if not dest.dest_folder:
            continue
        old_folder = str(getattr(movie, "placeholder_folder", None) or "").strip()
        leaf = os.path.basename(old_folder) if old_folder else None
        if not leaf and getattr(movie, "title", None):
            title = str(movie.title)
            year = getattr(movie, "year", None)
            tmdb = getattr(movie, "tmdbid", None) or getattr(movie, "id", None)
            leaf = f"{title} ({year}) {{tmdb-{tmdb}}}" if year else f"{title} {{tmdb-{tmdb}}}"
        new_folder = os.path.join(dest.dest_folder, leaf) if leaf else dest.dest_folder
        if os.path.normpath(old_folder or ".") == os.path.normpath(new_folder):
            continue
        old_filepath = getattr(movie, "placeholder_filepath", None)
        movie.placeholder_folder = new_folder
        stats["movies_updated"] += 1
        if old_filepath:
            _safe_unlink(str(old_filepath))
            movie.placeholder_filepath = None
            stats["movies_cleared_filepath"] += 1
        rid = getattr(movie, "id", None)
        if rid is not None:
            movie_ids.append(int(rid))
        session.add(movie)

    episode_ids: list[int] = []
    for series in session.query(Series).filter(Series.is_deleted == False).all():  # noqa: E712
        arr_path = getattr(series, "sonarrpath", None)
        instance_key = str(getattr(series, "instance_key", None) or "").strip().lower() or None
        dest = resolve_series_dest(
            instance_key=instance_key,
            arr_path=arr_path if isinstance(arr_path, str) else None,
        )
        if not dest.dest_folder:
            continue
        old_series_folder = str(getattr(series, "placeholder_folder", None) or "").strip()
        leaf = os.path.basename(old_series_folder) if old_series_folder else None
        if not leaf and getattr(series, "title", None):
            title = str(series.title)
            year = getattr(series, "year", None)
            tvdb = getattr(series, "tvdbid", None) or getattr(series, "id", None)
            leaf = f"{title} ({year}) {{tvdb-{tvdb}}}" if year else f"{title} {{tvdb-{tvdb}}}"
        new_series_folder = os.path.join(dest.dest_folder, leaf) if leaf else dest.dest_folder
        series_changed = os.path.normpath(old_series_folder or ".") != os.path.normpath(new_series_folder)
        if series_changed:
            series.placeholder_folder = new_series_folder
            stats["series_updated"] += 1
            session.add(series)

        seasons = (
            session.query(Season)
            .filter(Season.series_id == series.id, Season.is_deleted == False)  # noqa: E712
            .all()
        )
        for season in seasons:
            sn = int(getattr(season, "season_number", 0) or 0)
            new_season_folder = os.path.join(new_series_folder, f"Season {sn:02d}")
            old_season = str(getattr(season, "placeholder_folder", None) or "").strip()
            if os.path.normpath(old_season or ".") != os.path.normpath(new_season_folder):
                season.placeholder_folder = new_season_folder
                stats["seasons_updated"] += 1
                session.add(season)

            episodes = (
                session.query(Episode)
                .filter(Episode.season_id == season.id, Episode.is_deleted == False)  # noqa: E712
                .all()
            )
            for episode in episodes:
                old_ep_folder = str(getattr(episode, "placeholder_folder", None) or "").strip()
                folder_changed = os.path.normpath(old_ep_folder or ".") != os.path.normpath(new_season_folder)
                if folder_changed or series_changed:
                    episode.placeholder_folder = new_season_folder
                    if folder_changed:
                        stats["episodes_updated"] += 1
                    old_filepath = getattr(episode, "placeholder_filepath", None)
                    if old_filepath:
                        _safe_unlink(str(old_filepath))
                        episode.placeholder_filepath = None
                        stats["episodes_cleared_filepath"] += 1
                    eid = getattr(episode, "id", None)
                    if eid is not None:
                        episode_ids.append(int(eid))
                    session.add(episode)

    stats["touched_movie_row_ids"] = sorted(set(movie_ids))
    stats["touched_episode_row_ids"] = sorted(set(episode_ids))
    return stats


def enqueue_destination_rematerialize(*, source: str = "settings_save", apply_now: bool = True) -> dict[str, Any]:
    """Recompute folders and enqueue materialization for moved placeholders."""
    session = get_session()
    out: dict[str, Any] = {"ok": True, "source": source, "apply_now": bool(apply_now)}
    try:
        stats = recompute_catalog_placeholder_folders(session)
        session.commit()
        out["recompute"] = {
            k: v
            for k, v in stats.items()
            if k not in {"touched_movie_row_ids", "touched_episode_row_ids"}
        }
        movie_ids = list(stats.get("touched_movie_row_ids") or [])
        episode_ids = list(stats.get("touched_episode_row_ids") or [])
        out["touched_movie_row_ids"] = movie_ids
        out["touched_episode_row_ids"] = episode_ids

        if not movie_ids and not episode_ids:
            logger.info(
                f"Destination rematerialize ({source}): no folder changes",
                extra={"emoji_type": "info"},
            )
            return out

        if not apply_now:
            logger.info(
                f"Destination rematerialize deferred to next full sync ({source}): "
                f"movies={stats.get('movies_updated')} series={stats.get('series_updated')} "
                f"episodes={stats.get('episodes_updated')}",
                extra={"emoji_type": "info"},
            )
            out["pending_next_full_sync"] = True
            return out

        from services.source_of_truth.entity_materialization_job import (
            enqueue_entity_materialization_job,
        )

        enqueue_entity_materialization_job(
            session,
            movie_ids=movie_ids,
            episode_ids=episode_ids,
            observation_source=f"destination_rematerialize:{source}",
        )
        session.commit()
        out["jobs_enqueued"] = True
        logger.info(
            f"Destination rematerialize ({source}): movies={stats.get('movies_updated')} "
            f"series={stats.get('series_updated')} episodes={stats.get('episodes_updated')} "
            f"apply_now={apply_now}",
            extra={"emoji_type": "info"},
        )
        return out
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        logger.error(f"Destination rematerialize failed: {exc}", extra={"emoji_type": "error"})
        return {"ok": False, "error": str(exc), "source": source}
    finally:
        session.close()
