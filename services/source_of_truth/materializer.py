from __future__ import annotations

import os
from datetime import datetime, timezone
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
    resolve_calendar_variant_dummy_path,
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
from services.source_of_truth.status_reconciler import enqueue_status_projection
from services.source_of_truth.status_orchestrator import StatusOrchestrator


REQUEST_STATUS = "REQUEST"
REQUEST_REASON = "placeholder_request"


def _compute_initial_dummy_variant_for_episode(episode: Episode) -> str:
    """Return 'coming_soon' or 'request' for dummy file selection at episode creation time."""
    lookahead_days = int(getattr(settings, "CALENDAR_LOOKAHEAD_DAYS", 30) or 30)
    if not settings.coming_soon_placeholders_enabled:
        return "request"
    if lookahead_days <= 0:
        return "request"
    air_date = getattr(episode, "air_date", None)
    if not air_date:
        return "request"
    days_until = (air_date - datetime.now(timezone.utc).date()).days
    if days_until < 0 or days_until > lookahead_days:
        return "request"
    return "coming_soon"


def _compute_initial_dummy_variant_for_movie(movie: Movie) -> str:
    """Return 'coming_soon' or 'request' for dummy file selection at movie creation time."""
    lookahead_days = int(getattr(settings, "CALENDAR_LOOKAHEAD_DAYS", 30) or 30)
    if not settings.coming_soon_placeholders_enabled:
        return "request"
    if lookahead_days <= 0:
        return "request"
    preferred = str(getattr(settings, "PREFERRED_MOVIE_DATE_TYPE", "inCinemas") or "inCinemas").strip()
    release_map = {
        "inCinemas": "theater_release_date",
        "digitalRelease": "digital_release_date",
        "physicalRelease": "physical_release_date",
    }
    release_date = getattr(movie, release_map.get(preferred, "theater_release_date"), None)
    if not release_date:
        return "request"
    days_until = (release_date - datetime.now(timezone.utc).date()).days
    if days_until < 0 or days_until > lookahead_days:
        return "request"
    return "coming_soon"


def _dummy_file_path_for_variant(variant: str) -> str:
    """Return the configured dummy file path for the given variant."""
    return resolve_calendar_variant_dummy_path(variant)


def _activity_reason_from_observation_source(source: str) -> str:
    token = str(source or "").strip().lower()
    mapping = {
        "full_sync_materialization": "Library sync",
        "startup_lite_materialization": "Library sync",
        "event_series_add": "Series added",
        "event_movie_add": "Movie added",
        "event_movie_file_deleted": "Real file deleted",
        "event_episode_file_deleted": "Real file deleted",
        "event_movie_deleted": "Media deleted",
        "event_series_deleted": "Media deleted",
        "event_movie_imported_grace_finalize": "Import completed",
        "event_episode_imported_grace_finalize": "Import completed",
        "event_materialization": "Event materialization",
    }
    return mapping.get(token, "Materialization run")


def _apply_initial_status_for_placeholder(session, *, placeholder_id: int, event_type: str = "creation") -> None:
    """
    After materializer creates or deletes a placeholder, compute and apply initial status.
    
    This decouples status changes from file operations: the orchestrator handles all
    status computation, not just file state changes.
    """
    try:
        orchestrator = StatusOrchestrator(session=session)
        intent = orchestrator.compute_status_for_lifecycle_event(placeholder_id, event_type=event_type)
        
        if intent:
            orchestrator.apply_and_project_statuses([intent])
            logger.debug(f"Applied initial status for Placeholder[{placeholder_id}]: {intent.new_status}")
    except Exception as e:
        logger.error(f"Failed to apply initial status for Placeholder[{placeholder_id}]: {e}", exc_info=True)


def _sync_content_placeholder_status(session, *, movie_id: int | None, episode_id: int | None) -> None:
    """Deprecated no-op.

    Placeholder.display_status is now the single source of truth.
    
    STATUS REFACTOR NOTE:
    Materializer is responsible for:
    1. Creating/deleting placeholder files
    2. Creating/updating/deleting Placeholder rows
    3. Setting INITIAL status to REQUEST on new placeholders via StatusOrchestrator
    
    Materializer is NOT responsible for:
    - Long-term status changes (countdown, queue progress, cleanup)
    - Status projection to media servers
    - Status-triggered NFO rewrites
    
    All subsequent status changes are driven by independent status_orchestrator flows:
    - CalendarPhase: countdown logic
    - QueueMonitor: search/download progress
    - Event handlers: import/delete/playback cleanup
    
    See services/source_of_truth/status_orchestrator.py for the unified status architecture.
    """
    return None


def _mark_placeholder_row_active(
    session,
    *,
    movie_id: int | None,
    episode_id: int | None,
    path: str,
    series_id: int | None = None,
    season_id: int | None = None,
    calendar_dummy_variant: str | None = None,
    activity_reason: str | None = None,
) -> None:
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
        row.episode_id = None
        row.series_id = None
        row.season_id = None
    if episode_id is not None:
        row.movie_id = None
        row.episode_id = episode_id
        row.series_id = series_id
        row.season_id = season_id

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
    # Reset observation-tracking keys in extra so stale state from a previous
    # observation round cannot affect the new pass (e.g. plex_metadata_ready_seen).
    extra = dict(getattr(row, 'extra', {}) or {})
    extra.pop('plex_metadata_ready_seen', None)
    if activity_reason:
        extra['create_reason'] = str(activity_reason)
        extra['last_action_reason'] = str(activity_reason)
        extra['last_action'] = 'created'
    if calendar_dummy_variant:
        extra['calendar_dummy_variant'] = calendar_dummy_variant
    row.extra = extra
    row.last_observed_at = func.now()
    row.updated_at = func.now()
    session.add(row)


def _mark_placeholder_rows_deleted(
    session,
    *,
    movie_id: int | None,
    episode_id: int | None,
    activity_reason: str | None = None,
) -> list[str]:
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
        extra = dict(getattr(row, 'extra', {}) or {})
        if activity_reason:
            extra['delete_reason'] = str(activity_reason)
            extra['last_action_reason'] = str(activity_reason)
            extra['last_action'] = 'deleted'
        row.extra = extra
        row.updated_at = func.now()
        session.add(row)
    return paths


def apply_movie_materialization(movie_id: int, session=None, activity_reason: str | None = None) -> dict[str, Any]:
    owns_session = session is None
    session = session or get_session()
    try:
        movie = session.query(Movie).filter(Movie.id == int(movie_id)).first()
        if not movie:
            return {"ok": False, "reason": "movie_not_found", "movie_id": movie_id}

        determination = getattr(movie, "determination", None)
        if determination == DETERMINATION_NEEDS:
            target_path = getattr(movie, "placeholder_filepath", None) or movie_placeholder_path(movie)
            _initial_variant = _compute_initial_dummy_variant_for_movie(movie)
            created = ensure_placeholder_file(target_path, dummy_file_path=_dummy_file_path_for_variant(_initial_variant))
            nfo_written = False
            if settings.PLACEHOLDER_CREATE_NFO:
                nfo_written = ensure_movie_nfo(target_path, movie)
            movie.has_placeholder = True
            movie.placeholder_filepath = target_path
            movie.updated_at = func.now()
            _mark_placeholder_row_active(
                session,
                movie_id=movie.id,
                episode_id=None,
                path=target_path,
                calendar_dummy_variant=_initial_variant,
                activity_reason=activity_reason,
            )
            _sync_content_placeholder_status(session, movie_id=movie.id, episode_id=None)
            
            # Apply initial status for the created placeholder
            placeholder_row = session.query(Placeholder).filter(
                Placeholder.movie_id == movie.id
            ).order_by(Placeholder.id.desc()).first()
            if placeholder_row:
                _apply_initial_status_for_placeholder(session, placeholder_id=placeholder_row.id, event_type="creation")
            
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
            candidate_paths = _mark_placeholder_rows_deleted(
                session,
                movie_id=movie.id,
                episode_id=None,
                activity_reason=activity_reason,
            )
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


def apply_episode_materialization(episode_id: int, session=None, activity_reason: str | None = None) -> dict[str, Any]:
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
            _initial_variant = _compute_initial_dummy_variant_for_episode(episode)
            created = ensure_placeholder_file(target_path, dummy_file_path=_dummy_file_path_for_variant(_initial_variant))
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
            _mark_placeholder_row_active(
                session,
                movie_id=None,
                episode_id=episode.id,
                path=target_path,
                series_id=series.id,
                season_id=season.id,
                calendar_dummy_variant=_initial_variant,
                activity_reason=activity_reason,
            )
            _sync_content_placeholder_status(session, movie_id=None, episode_id=episode.id)
            
            # Apply initial status for the created placeholder
            placeholder_row = session.query(Placeholder).filter(
                Placeholder.episode_id == episode.id
            ).order_by(Placeholder.id.desc()).first()
            if placeholder_row:
                _apply_initial_status_for_placeholder(session, placeholder_id=placeholder_row.id, event_type="creation")
            
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
            candidate_paths = _mark_placeholder_rows_deleted(
                session,
                movie_id=None,
                episode_id=episode.id,
                activity_reason=activity_reason,
            )
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
    changed_paths: set[str] = set()
    delete_refresh_paths: set[str] = set()
    stats["movies_considered"] = len(movie_ids)
    stats["episodes_considered"] = len(episode_ids)
    activity_reason = _activity_reason_from_observation_source(observation_source)

    for movie_id in movie_ids:
        result = apply_movie_materialization(movie_id, session=session, activity_reason=activity_reason)
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
            logger.debug(
                f"Placeholder materialized for movie_id={movie_id}: "
                f"state={'created' if result.get('created') else 'already_present'} "
                f"path={result.get('path')}",
                extra={"emoji_type": "create"},
            )
            if result.get("created"):
                stats["files_created"] += 1
                path = result.get("path")
                if path:
                    changed_paths.add(path)
            if result.get("nfo_written"):
                stats["nfo_written"] += 1
        elif action == "deleted_or_absent":
            stats["deleted"] += 1
            logger.debug(
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
                delete_refresh_paths.add(refresh_path)
        else:
            stats["noop"] += 1

    for episode_id in episode_ids:
        result = apply_episode_materialization(episode_id, session=session, activity_reason=activity_reason)
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
            logger.debug(
                f"Placeholder materialized for episode_id={episode_id}: "
                f"state={'created' if result.get('created') else 'already_present'} "
                f"path={result.get('path')}",
                extra={"emoji_type": "create"},
            )
            if result.get("created"):
                stats["files_created"] += 1
                path = result.get("path")
                if path:
                    changed_paths.add(path)
            # count episode and series-level NFO writes
            if result.get("nfo_written") or result.get("series_nfo_written"):
                stats["nfo_written"] += 1
        elif action == "deleted_or_absent":
            stats["deleted"] += 1
            logger.debug(
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
                delete_refresh_paths.add(refresh_path)
        else:
            stats["noop"] += 1

    logger.info(
        "Materialization batch summary: "
        f"created={stats['created']} deleted={stats['deleted']} noop={stats['noop']} errors={stats['errors']} "
        f"files_created={stats['files_created']} files_deleted={stats['files_deleted']} nfo_written={stats['nfo_written']}",
        extra={"emoji_type": "success"},
    )

    refresh_stats = refresh_all_paths(changed_paths)
    delete_refresh_stats = refresh_all_paths(delete_refresh_paths, update_type="Deleted")
    stats["media_refresh_requested"] = refresh_stats.get("refreshed", 0) + delete_refresh_stats.get("refreshed", 0)
    stats["media_refresh_failed"] = refresh_stats.get("failed", 0) + delete_refresh_stats.get("failed", 0)

    logger.info(
        f"Media server refreshes completed after placeholder materialization: "
        f"refreshed={stats['media_refresh_requested']} "
        f"failed={stats['media_refresh_failed']}",
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

    # Always perform immediate polling for all enabled media servers so IDs can
    # be captured even when Plex is disabled.
    observe_stats = observe_placeholders_with_polling(
        session,
        observed_candidates,
        allow_title_fallback=False,
    )
    stats["media_id_observed_plex"] = observe_stats.get("observed_plex", 0)
    stats["media_id_observed_jellyfin"] = observe_stats.get("observed_jellyfin", 0)
    stats["media_id_observed_emby"] = observe_stats.get("observed_emby", 0)
    stats["media_id_observe_failed"] = observe_stats.get("observe_failed", 0)

    if getattr(settings, "ENABLE_PLEX", False):
        ready_for_plex_projection = [
            int(row.id)
            for row in observed_candidates
            if getattr(row, "has_placeholder", False)
            and getattr(row, "plex_placeholder_id", None)
        ]
        if ready_for_plex_projection:
            projection_result = enqueue_status_projection(ready_for_plex_projection, session=session)
            logger.info(
                "Post-observation Plex status projection queued "
                f"placeholders={len(ready_for_plex_projection)} ok={projection_result.get('ok', False)}",
                extra={"emoji_type": "info"},
            )

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

    return stats


def run_materialization_pass() -> dict[str, Any]:
    """Apply file/DB side effects for needs/obsolete determinations.

    Creates all placeholder files first, then sends a single grouped refresh to
    each media server (one request per unique folder), then runs one observation
    sweep.  Placeholders Plex didn't scan within the observation window are
    deferred to the observation trail job queue picked up by background workers.

    This single-pass approach is far faster than per-batch cycling for large
    first-run syncs (tens of thousands of items) while still giving Plex a
    path-scoped refresh signal for every new folder.
    """
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

        total = len(movie_ids) + len(episode_ids)
        logger.info(
            f"Materialization pass: {len(movie_ids)} movies + {len(episode_ids)} episodes = {total} total",
            extra={"emoji_type": "info"},
        )

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
        stats = run_materialization_for_entities_in_session(
            session,
            movie_ids=movie_ids,
            episode_ids=episode_ids,
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


def run_materialization_for_entities_in_session(
    session,
    movie_ids: list[int] | None = None,
    episode_ids: list[int] | None = None,
    observation_source: str = "event_materialization",
) -> dict[str, Any]:
    movie_ids = [int(mid) for mid in (movie_ids or []) if mid is not None]
    episode_ids = [int(eid) for eid in (episode_ids or []) if eid is not None]

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

    return _run_materialization_for_ids(
        session,
        movie_ids=actionable_movie_ids,
        episode_ids=actionable_episode_ids,
        observation_source=observation_source,
    )
