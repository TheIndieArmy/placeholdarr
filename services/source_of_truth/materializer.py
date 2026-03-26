from __future__ import annotations

import os
from typing import Any

from sqlalchemy import func, or_

from core.config import settings
from core.logger import logger
from services.media_servers.refresh import refresh_all_paths
from services.placeholders import (
    ensure_episode_nfo,
    ensure_movie_nfo,
    ensure_placeholder_file,
    episode_placeholder_path,
    movie_placeholder_path,
    ensure_series_nfo,
)
from services.postgres.db import get_session
from services.postgres.models import Episode, Movie, Placeholder, Season, Series
from services.source_of_truth.determiner import DETERMINATION_NEEDS, DETERMINATION_OBSOLETE
from services.source_of_truth.media_observation import observe_placeholders_with_polling
from services.source_of_truth.observation_trail import enqueue_observation_trail, unresolved_placeholder_ids
from services.source_of_truth.placeholder_cleanup import (
    cleanup_episode_placeholder_files,
    cleanup_movie_placeholder_files,
)


REQUEST_STATUS = "REQUEST"
REQUEST_REASON = "placeholder_request"


def _sync_content_placeholder_status(session, *, movie_id: int | None, episode_id: int | None) -> None:
    """Compatibility projection from Placeholder.display_status -> content placeholder_status.

    Placeholder rows remain canonical for runtime display state. Content-row
    placeholder_status is maintained as a derived mirror for compatibility while
    legacy paths are phased out.
    """
    q = session.query(Placeholder).filter(Placeholder.has_placeholder == True)  # noqa: E712
    if movie_id is not None:
        q = q.filter(Placeholder.movie_id == movie_id)
        row = q.order_by(Placeholder.id.desc()).first()
        movie = session.query(Movie).filter(Movie.id == int(movie_id)).first()
        if movie:
            movie.placeholder_status = getattr(row, 'display_status', None) if row else None
            movie.updated_at = func.now()
            session.add(movie)
        return

    if episode_id is not None:
        q = q.filter(Placeholder.episode_id == episode_id)
        row = q.order_by(Placeholder.id.desc()).first()
        episode = session.query(Episode).filter(Episode.id == int(episode_id)).first()
        if episode:
            episode.placeholder_status = getattr(row, 'display_status', None) if row else None
            episode.updated_at = func.now()
            session.add(episode)
        return


def _mark_placeholder_row_active(session, *, movie_id: int | None, episode_id: int | None, path: str) -> None:
    q = session.query(Placeholder)
    if movie_id is not None:
        q = q.filter(Placeholder.movie_id == movie_id)
    elif episode_id is not None:
        q = q.filter(Placeholder.episode_id == episode_id)
    else:
        return

    row = q.order_by(Placeholder.id.desc()).first()
    # If no linked row exists yet, reuse any existing path row from fs-scan to
    # avoid duplicate placeholder rows for the same file.
    if not row and path:
        row = (
            session.query(Placeholder)
            .filter(Placeholder.path == path)
            .order_by(Placeholder.id.desc())
            .first()
        )
    if not row:
        row = Placeholder(movie_id=movie_id, episode_id=episode_id, path=path, created_by="source_of_truth.materializer")
        session.add(row)

    if movie_id is not None:
        row.movie_id = movie_id
    if episode_id is not None:
        row.episode_id = episode_id

    # Treat (re)materialization as a reconnect event: force fresh media-id observation.
    row.plex_placeholder_id = None
    row.jellyfin_placeholder_id = None
    row.emby_placeholder_id = None
    row.plex_id_observed_at = None
    row.jellyfin_id_observed_at = None
    row.emby_id_observed_at = None
    row.media_lookup_error = None
    row.media_lookup_last_attempt_at = None

    row.path = path
    row.has_placeholder = True
    row.lifecycle_status = "ACTIVE"
    row.display_status = REQUEST_STATUS
    row.display_reason = REQUEST_REASON
    row.display_progress = 0
    row.last_observed_at = func.now()
    row.updated_at = func.now()
    session.add(row)


def _mark_placeholder_rows_deleted(session, *, movie_id: int | None, episode_id: int | None) -> list[str]:
    q = session.query(Placeholder)
    if movie_id is not None:
        q = q.filter(Placeholder.movie_id == movie_id)
    elif episode_id is not None:
        q = q.filter(Placeholder.episode_id == episode_id)
    else:
        return []

    rows = q.all()
    paths = [r.path for r in rows if getattr(r, "path", None)]
    for row in rows:
        row.has_placeholder = False
        row.lifecycle_status = "DELETED"
        row.display_status = None
        row.display_reason = None
        row.display_progress = None
        row.last_observed_at = func.now()
        row.plex_placeholder_id = None
        row.jellyfin_placeholder_id = None
        row.emby_placeholder_id = None
        row.plex_id_observed_at = None
        row.jellyfin_id_observed_at = None
        row.emby_id_observed_at = None
        row.media_lookup_error = None
        row.media_lookup_last_attempt_at = None
        row.updated_at = func.now()
        session.add(row)
    return paths


def apply_movie_materialization(movie_id: int, session=None) -> dict[str, Any]:
    owns_session = session is None
    session = session or get_session()
    try:
        movie = session.query(Movie).filter(Movie.id == int(movie_id)).first()
        if not movie:
            return {"ok": False, "reason": "movie_not_found", "movie_id": movie_id}

        determination = getattr(movie, "determination", None)
        if determination == DETERMINATION_NEEDS:
            target_path = getattr(movie, "placeholder_filepath", None) or movie_placeholder_path(movie)
            created = ensure_placeholder_file(target_path)
            nfo_written = False
            if settings.PLACEHOLDER_CREATE_NFO:
                nfo_written = ensure_movie_nfo(target_path, movie)
            movie.has_placeholder = True
            movie.placeholder_filepath = target_path
            movie.updated_at = func.now()
            _mark_placeholder_row_active(session, movie_id=movie.id, episode_id=None, path=target_path)
            _sync_content_placeholder_status(session, movie_id=movie.id, episode_id=None)
            session.add(movie)
            if owns_session:
                session.commit()
            return {
                "ok": True,
                "action": "created_or_exists",
                "created": created,
                "deleted": False,
                "nfo_written": nfo_written,
                "path": target_path,
                "movie_id": movie.id,
            }

        if determination == DETERMINATION_OBSOLETE:
            candidate_paths = _mark_placeholder_rows_deleted(session, movie_id=movie.id, episode_id=None)
            if getattr(movie, "placeholder_filepath", None):
                candidate_paths.append(movie.placeholder_filepath)
            first_path = next((p for p in candidate_paths if p), None)
            cleanup_result = cleanup_movie_placeholder_files(candidate_paths=candidate_paths)

            movie.has_placeholder = False
            movie.placeholder_filepath = None
            movie.updated_at = func.now()
            _sync_content_placeholder_status(session, movie_id=movie.id, episode_id=None)
            session.add(movie)
            if owns_session:
                session.commit()
            return {
                "ok": True,
                "action": "deleted_or_absent",
                "created": False,
                "deleted": cleanup_result.get("deleted", False),
                "nfo_deleted": cleanup_result.get("nfo_deleted", False),
                "directories_deleted": cleanup_result.get("directories_deleted", 0),
                "series_nfo_deleted": cleanup_result.get("series_nfo_deleted", False),
                "refresh_paths": cleanup_result.get("refresh_paths", []),
                "path": first_path,
                "movie_id": movie.id,
            }

        return {"ok": True, "action": "noop", "movie_id": movie.id}
    except Exception as e:
        if owns_session:
            session.rollback()
        return {"ok": False, "reason": str(e), "movie_id": movie_id}
    finally:
        if owns_session:
            session.close()


def apply_episode_materialization(episode_id: int, session=None) -> dict[str, Any]:
    owns_session = session is None
    session = session or get_session()
    try:
        episode = session.query(Episode).filter(Episode.id == int(episode_id)).first()
        if not episode:
            return {"ok": False, "reason": "episode_not_found", "episode_id": episode_id}

        season = session.query(Season).filter(Season.id == int(episode.season_id)).first()
        if not season:
            return {"ok": False, "reason": "season_not_found", "episode_id": episode_id}

        series = session.query(Series).filter(Series.id == int(season.series_id)).first()
        if not series:
            return {"ok": False, "reason": "series_not_found", "episode_id": episode_id}

        determination = getattr(episode, "determination", None)
        if determination == DETERMINATION_NEEDS:
            target_path = getattr(episode, "placeholder_filepath", None) or episode_placeholder_path(episode, season, series)
            created = ensure_placeholder_file(target_path)
            nfo_written = False
            if settings.PLACEHOLDER_CREATE_NFO:
                nfo_written = ensure_episode_nfo(target_path, episode, season, series)
                # ensure series-level tvshow.nfo is present as well
                try:
                    series_nfo_written = ensure_series_nfo(series, folder=getattr(series, "placeholder_folder", None))
                except Exception:
                    series_nfo_written = False
            else:
                series_nfo_written = False
            episode.has_placeholder = True
            episode.placeholder_filepath = target_path
            episode.updated_at = func.now()
            _mark_placeholder_row_active(session, movie_id=None, episode_id=episode.id, path=target_path)
            _sync_content_placeholder_status(session, movie_id=None, episode_id=episode.id)
            session.add(episode)
            if owns_session:
                session.commit()
            return {
                "ok": True,
                "action": "created_or_exists",
                "created": created,
                "deleted": False,
                "nfo_written": nfo_written,
                "series_nfo_written": series_nfo_written,
                "path": target_path,
                "episode_id": episode.id,
            }

        if determination == DETERMINATION_OBSOLETE:
            candidate_paths = _mark_placeholder_rows_deleted(session, movie_id=None, episode_id=episode.id)
            if getattr(episode, "placeholder_filepath", None):
                candidate_paths.append(episode.placeholder_filepath)
            first_path = next((p for p in candidate_paths if p), None)
            cleanup_result = cleanup_episode_placeholder_files(
                session,
                season=season,
                series=series,
                candidate_paths=candidate_paths,
            )

            episode.has_placeholder = False
            episode.placeholder_filepath = None
            episode.updated_at = func.now()
            _sync_content_placeholder_status(session, movie_id=None, episode_id=episode.id)
            session.add(episode)
            if owns_session:
                session.commit()
            return {
                "ok": True,
                "action": "deleted_or_absent",
                "created": False,
                "deleted": cleanup_result.get("deleted", False),
                "nfo_deleted": cleanup_result.get("nfo_deleted", False),
                "directories_deleted": cleanup_result.get("directories_deleted", 0),
                "series_nfo_deleted": cleanup_result.get("series_nfo_deleted", False),
                "refresh_paths": cleanup_result.get("refresh_paths", []),
                "path": first_path,
                "episode_id": episode.id,
            }

        return {"ok": True, "action": "noop", "episode_id": episode.id}
    except Exception as e:
        if owns_session:
            session.rollback()
        return {"ok": False, "reason": str(e), "episode_id": episode_id}
    finally:
        if owns_session:
            session.close()


def _run_materialization_for_ids(
    session,
    *,
    movie_ids: list[int],
    episode_ids: list[int],
    observation_source: str,
) -> dict[str, Any]:
    """Shared materialization core for full-sync and event-scoped runs."""
    stats: dict[str, Any] = {
        "movies_considered": 0,
        "episodes_considered": 0,
        "created": 0,
        "deleted": 0,
        "noop": 0,
        "errors": 0,
        "files_created": 0,
        "files_deleted": 0,
        "directories_deleted": 0,
        "nfo_written": 0,
        "nfo_deleted": 0,
        "series_nfo_deleted": 0,
        "media_refresh_requested": 0,
        "media_refresh_failed": 0,
        "media_id_observed_plex": 0,
        "media_id_observed_jellyfin": 0,
        "media_id_observed_emby": 0,
        "media_id_observe_failed": 0,
        "observation_trail_enqueued": 0,
        "observation_trail_job_id": None,
        "observation_trail_group_id": None,
    }
    changed_folders: set[str] = set()
    stats["movies_considered"] = len(movie_ids)
    stats["episodes_considered"] = len(episode_ids)

    for movie_id in movie_ids:
        result = apply_movie_materialization(movie_id, session=session)
        if not result.get("ok"):
            stats["errors"] += 1
            logger.error(
                f"Movie materialization failed movie_id={movie_id}: {result.get('reason')}",
                extra={"emoji_type": "error"},
            )
            continue

        action = result.get("action")
        if action == "created_or_exists":
            stats["created"] += 1
            logger.info(
                f"Placeholder materialized for movie_id={movie_id}: "
                f"state={'created' if result.get('created') else 'already_present'} "
                f"path={result.get('path')}",
                extra={"emoji_type": "create"},
            )
            if result.get("created"):
                stats["files_created"] += 1
                path = result.get("path")
                if path:
                    changed_folders.add(os.path.dirname(path))
            if result.get("nfo_written"):
                stats["nfo_written"] += 1
        elif action == "deleted_or_absent":
            stats["deleted"] += 1
            logger.info(
                f"Placeholder cleanup for movie_id={movie_id}: "
                f"state={'deleted' if result.get('deleted') else 'already_absent'} "
                f"path={result.get('path')} "
                f"dirs_deleted={int(result.get('directories_deleted', 0) or 0)}",
                extra={"emoji_type": "delete"},
            )
            if result.get("deleted"):
                stats["files_deleted"] += 1
            if result.get("nfo_deleted"):
                stats["nfo_deleted"] += 1
            stats["directories_deleted"] += int(result.get("directories_deleted", 0) or 0)
            if result.get("series_nfo_deleted"):
                stats["series_nfo_deleted"] += 1
            for refresh_path in result.get("refresh_paths", []) or []:
                changed_folders.add(refresh_path)
        else:
            stats["noop"] += 1

    for episode_id in episode_ids:
        result = apply_episode_materialization(episode_id, session=session)
        if not result.get("ok"):
            stats["errors"] += 1
            logger.error(
                f"Episode materialization failed episode_id={episode_id}: {result.get('reason')}",
                extra={"emoji_type": "error"},
            )
            continue

        action = result.get("action")
        if action == "created_or_exists":
            stats["created"] += 1
            logger.info(
                f"Placeholder materialized for episode_id={episode_id}: "
                f"state={'created' if result.get('created') else 'already_present'} "
                f"path={result.get('path')}",
                extra={"emoji_type": "create"},
            )
            if result.get("created"):
                stats["files_created"] += 1
                path = result.get("path")
                if path:
                    changed_folders.add(os.path.dirname(path))
            # count episode and series-level NFO writes
            if result.get("nfo_written") or result.get("series_nfo_written"):
                stats["nfo_written"] += 1
        elif action == "deleted_or_absent":
            stats["deleted"] += 1
            logger.info(
                f"Placeholder cleanup for episode_id={episode_id}: "
                f"state={'deleted' if result.get('deleted') else 'already_absent'} "
                f"path={result.get('path')} "
                f"dirs_deleted={int(result.get('directories_deleted', 0) or 0)} "
                f"series_nfo_deleted={bool(result.get('series_nfo_deleted', False))}",
                extra={"emoji_type": "delete"},
            )
            if result.get("deleted"):
                stats["files_deleted"] += 1
            if result.get("nfo_deleted"):
                stats["nfo_deleted"] += 1
            stats["directories_deleted"] += int(result.get("directories_deleted", 0) or 0)
            if result.get("series_nfo_deleted"):
                stats["series_nfo_deleted"] += 1
            for refresh_path in result.get("refresh_paths", []) or []:
                changed_folders.add(refresh_path)
        else:
            stats["noop"] += 1

    refresh_stats = refresh_all_paths(changed_folders)
    stats["media_refresh_requested"] = refresh_stats.get("refreshed", 0)
    stats["media_refresh_failed"] = refresh_stats.get("failed", 0)

    logger.info(
        f"Media server refreshes completed after placeholder materialization: "
        f"refreshed={refresh_stats.get('refreshed', 0)} "
        f"failed={refresh_stats.get('failed', 0)}",
        extra={"emoji_type": "success"},
    )

    # Only observe candidates touched by this materialization scope to avoid
    # re-polling unrelated placeholders on event-driven runs.
    observed_candidates_q = session.query(Placeholder).filter(Placeholder.has_placeholder == True)  # noqa: E712
    filters = []
    if movie_ids:
        filters.append(Placeholder.movie_id.in_(movie_ids))
    if episode_ids:
        filters.append(Placeholder.episode_id.in_(episode_ids))
    if filters:
        observed_candidates_q = observed_candidates_q.filter(or_(*filters))
    else:
        observed_candidates_q = observed_candidates_q.filter(Placeholder.id == -1)
    observed_candidates = observed_candidates_q.all()

    if getattr(settings, "ENABLE_PLEX", False):
        # Check if Plex is busy before starting observation polling
        plex_is_busy = False
        # Always perform immediate polling
        observe_stats = observe_placeholders_with_polling(session, observed_candidates)
        stats["media_id_observed_plex"] = observe_stats.get("observed_plex", 0)
        stats["media_id_observed_jellyfin"] = observe_stats.get("observed_jellyfin", 0)
        stats["media_id_observed_emby"] = observe_stats.get("observed_emby", 0)
        stats["media_id_observe_failed"] = observe_stats.get("observe_failed", 0)

        unresolved_ids = unresolved_placeholder_ids(observed_candidates)
        # Always enqueue observation trail for unresolved placeholders
        if unresolved_ids:
            enqueue_result = enqueue_observation_trail(
                session,
                placeholder_ids=unresolved_ids,
                source=observation_source,
            )
            if enqueue_result.get("enqueued"):
                stats["observation_trail_enqueued"] = 1
                stats["observation_trail_job_id"] = enqueue_result.get("job_id")
                stats["observation_trail_group_id"] = enqueue_result.get("group_id")
                logger.info(
                    "Deferred observation trail enqueued "
                    f"job_id={enqueue_result.get('job_id')} "
                    f"unresolved={len(unresolved_ids)}",
                    extra={"emoji_type": "info"},
                )
    else:
        logger.info(
            "Skipping observation polling and trail enqueue because Plex is disabled.",
            extra={"emoji_type": "info"},
        )

    return stats


def run_materialization_pass() -> dict[str, Any]:
    """Apply file/DB side effects for needs/obsolete determinations."""
    session = get_session()
    try:
        movie_ids = [
            r[0]
            for r in session.query(Movie.id)
            .filter(Movie.determination.in_([DETERMINATION_NEEDS, DETERMINATION_OBSOLETE]))
            .all()
        ]
        episode_ids = [
            r[0]
            for r in session.query(Episode.id)
            .filter(Episode.determination.in_([DETERMINATION_NEEDS, DETERMINATION_OBSOLETE]))
            .all()
        ]

        stats = _run_materialization_for_ids(
            session,
            movie_ids=movie_ids,
            episode_ids=episode_ids,
            observation_source="full_sync_materialization",
        )
        session.commit()
        logger.info(f"Materialization phase complete: {stats}", extra={"emoji_type": "success"})
        return stats
    except Exception as e:
        session.rollback()
        logger.error(f"Materialization phase failed: {e}", extra={"emoji_type": "error"})
        raise
    finally:
        session.close()


def run_materialization_for_entities(
    movie_ids: list[int] | None = None,
    episode_ids: list[int] | None = None,
    observation_source: str = "event_materialization",
) -> dict[str, Any]:
    """Run materialization only for targeted entities.

    This is used by webhook/event workflows so only affected content is
    materialized, refreshed, and observed.
    """
    movie_ids = [int(mid) for mid in (movie_ids or []) if mid is not None]
    episode_ids = [int(eid) for eid in (episode_ids or []) if eid is not None]

    session = get_session()
    try:
        actionable_movie_ids = [
            r[0]
            for r in session.query(Movie.id)
            .filter(Movie.id.in_(movie_ids), Movie.determination.in_([DETERMINATION_NEEDS, DETERMINATION_OBSOLETE]))
            .all()
        ] if movie_ids else []

        actionable_episode_ids = [
            r[0]
            for r in session.query(Episode.id)
            .filter(Episode.id.in_(episode_ids), Episode.determination.in_([DETERMINATION_NEEDS, DETERMINATION_OBSOLETE]))
            .all()
        ] if episode_ids else []

        stats = _run_materialization_for_ids(
            session,
            movie_ids=actionable_movie_ids,
            episode_ids=actionable_episode_ids,
            observation_source=observation_source,
        )
        session.commit()
        logger.info(f"Scoped materialization complete: {stats}", extra={"emoji_type": "success"})
        return stats
    except Exception as e:
        session.rollback()
        logger.error(f"Scoped materialization failed: {e}", extra={"emoji_type": "error"})
        raise
    finally:
        session.close()
