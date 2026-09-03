from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_

from core.config import settings
from core.logger import logger, start_verbose_stall_heartbeat
from services.media_servers.refresh import refresh_all_path_batches_with_section_fallback, refresh_selected_sections
from services.placeholder_poster_art import (
    ensure_episode_still_art,
    ensure_movie_art,
    ensure_series_art,
)
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
from services.postgres.models import Episode, Job, Movie, Placeholder, PlaceholderActivityHistory, Season, Series
from services.source_of_truth.determiner import (
    DETERMINATION_EXISTS,
    DETERMINATION_NEEDS,
    DETERMINATION_NOT_NEEDED,
    DETERMINATION_OBSOLETE,
    run_determination_for_entities_in_session,
)
from services.source_of_truth.arr_share_guard import filter_movie_disk_cleanup_paths
from services.source_of_truth.placeholder_cleanup import (
    cleanup_deleted_series_placeholder_files,
    cleanup_episode_placeholder_files,
    cleanup_movie_placeholder_files,
)
from services.source_of_truth.media_refresh_handler import (
    KIND_BULK_INITIAL,
    KIND_DELAYED_FINAL,
    KIND_OVERLAP_PATH_BATCH,
    KIND_PHASE_FINAL_EPISODE,
    KIND_PHASE_FINAL_MOVIE,
    KIND_PHASE_INITIAL_EPISODE,
    KIND_PHASE_INITIAL_MOVIE,
    enqueue_media_refresh_job,
    use_job_driven_refresh,
)
from services.source_of_truth.refresh_throttle import try_acquire_refresh_lease
from services.source_of_truth.status_orchestrator import StatusOrchestrator
from services.messages.context import build_projection_context_from_session
from services.status_projection import projected_status_display


def _enqueue_media_refresh_via_new_session(
    *,
    kind: str,
    payload: dict[str, Any] | None,
    delay_seconds: float,
) -> bool:
    """Enqueue a media_refresh Job using a fresh short-lived session.

    Returns True on successful commit, False otherwise. We use a dedicated
    session so we never piggy-back on the materializer's current transaction
    state — the Job needs to be committed promptly so the worker NOTIFY
    listener can see it (Postgres only delivers NOTIFY after commit).
    """
    s = get_session()
    try:
        enqueue_media_refresh_job(
            s,
            kind=kind,
            payload=payload or {},
            delay_seconds=delay_seconds,
        )
        s.commit()
        return True
    except Exception as exc:
        try:
            s.rollback()
        except Exception:
            pass
        logger.error(
            f"Failed to enqueue media_refresh kind={kind} delay={delay_seconds}s: {exc}",
            extra={"emoji_type": "error"},
        )
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _schedule_media_refresh(
    timer_fn,
    *,
    delay_seconds: float,
    kind: str,
    payload: dict[str, Any] | None,
    log_message: str | None = None,
) -> None:
    """Either enqueue a media_refresh Job (Phase 3) or fall back to threading.Timer.

    Behind feature flag `USE_JOB_DRIVEN_REFRESH`. When the flag is on AND the
    Job enqueue succeeds, the Timer is NOT started. On any failure, falls back
    to the legacy Timer so refreshes never silently disappear.
    """
    if use_job_driven_refresh():
        if _enqueue_media_refresh_via_new_session(
            kind=kind,
            payload=payload,
            delay_seconds=delay_seconds,
        ):
            if log_message:
                logger.info(log_message, extra={"emoji_type": "info"})
            return
        # If enqueue failed, fall back to the Timer below so we don't lose the
        # refresh entirely.
        logger.warning(
            f"media_refresh job enqueue failed for kind={kind}; falling back to threading.Timer",
            extra={"emoji_type": "warning"},
        )
    threading.Timer(delay_seconds, timer_fn).start()
    if log_message:
        logger.info(log_message, extra={"emoji_type": "info"})


REQUEST_STATUS = "REQUEST"
REQUEST_REASON = "placeholder_request"


def _placeholder_runtime_minutes(
    session,
    *,
    movie_id: int | None = None,
    episode_id: int | None = None,
) -> int | None:
    if movie_id is not None:
        movie = session.query(Movie).filter(Movie.id == int(movie_id)).first()
        if movie:
            try:
                runtime = int(getattr(movie, "radarr_runtime", 0) or 0)
                return runtime if runtime > 0 else None
            except Exception:
                return None
    if episode_id is not None:
        episode = session.query(Episode).filter(Episode.id == int(episode_id)).first()
        if not episode:
            return None
        try:
            runtime = int(getattr(episode, "sonarr_runtime", 0) or 0)
            if runtime > 0:
                return runtime
        except Exception:
            pass
        season = session.query(Season).filter(Season.id == episode.season_id).first()
        series = session.query(Series).filter(Series.id == season.series_id).first() if season else None
        if series:
            try:
                runtime = int(getattr(series, "sonarr_runtime", 0) or 0)
                return runtime if runtime > 0 else None
            except Exception:
                return None
    return None


def _materialization_overlap_enabled() -> bool:
    return bool(getattr(settings, "MATERIALIZATION_OVERLAP_ENABLED", False))


def _overlap_checkpoint_threshold(media_type: str) -> int:
    if str(media_type).lower() == "movie":
        value = int(getattr(settings, "MATERIALIZATION_OVERLAP_MOVIE_CHECKPOINT_COUNT", 200) or 200)
    else:
        value = int(getattr(settings, "MATERIALIZATION_OVERLAP_EPISODE_CHECKPOINT_COUNT", 400) or 400)
    return max(1, value)


def _overlap_staleness_seconds() -> int:
    return max(10, int(getattr(settings, "MATERIALIZATION_OVERLAP_MAX_STALENESS_SECONDS", 120) or 120))


def _overlap_min_candidates() -> int:
    return max(1, int(getattr(settings, "MATERIALIZATION_OVERLAP_MIN_CANDIDATES", 100) or 100))


def _overlap_max_pending_slices() -> int:
    return max(1, int(getattr(settings, "MATERIALIZATION_OVERLAP_MAX_PENDING_SLICES_PER_SOURCE", 1) or 1))


def _overlap_refresh_min_interval_seconds() -> int:
    return max(0, int(getattr(settings, "MATERIALIZATION_OVERLAP_REFRESH_MIN_INTERVAL_SECONDS", 90) or 90))


def _overlap_refresh_lease_seconds() -> int:
    return max(0, int(getattr(settings, "MATERIALIZATION_OVERLAP_REFRESH_LEASE_SECONDS", 180) or 180))


def _should_trigger_overlap_checkpoint(
    *,
    processed_since_checkpoint: int,
    count_threshold: int,
    last_checkpoint_mono: float,
    now_mono: float,
    staleness_seconds: int,
) -> tuple[bool, str | None]:
    if processed_since_checkpoint >= max(1, int(count_threshold or 1)):
        return True, "count_threshold"
    if (now_mono - float(last_checkpoint_mono)) >= max(1, int(staleness_seconds or 1)):
        return True, "time_staleness"
    return False, None


def _target_refresh_section_ids(*, has_movies: bool, has_episodes: bool) -> list[int]:
    section_ids: list[int] = []
    if has_movies:
        movie_section = getattr(settings, "PLEX_MOVIE_SECTION_ID", None)
        if movie_section is not None:
            section_ids.append(int(movie_section))
    if has_episodes:
        tv_section = getattr(settings, "PLEX_TV_SECTION_ID", None)
        if tv_section is not None:
            section_ids.append(int(tv_section))
    return sorted(set(section_ids))


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
    # If no linked row exists yet, reuse a path-only row from fs-scan (movie_id/episode_id NULL)
    # to avoid duplicate Placeholder rows for the same file.
    #
    # Do **not** reuse a row already owned by a different Movie/Episode: Radarr primary vs secondary
    # (same TMDB, same on-disk path) would otherwise steal the single FK row on each materialization,
    # leaving the sibling Movie with has_placeholder=True in DB but no Placeholder.movie_id row —
    # reconcile then drops linkage and the title flips needs_placeholder ↔ materialized forever.
    if not row and path:
        candidate = (
            session.query(Placeholder)
            .filter(Placeholder.path == path)
            .order_by(Placeholder.id.desc())
            .first()
        )
        if candidate:
            c_mid = getattr(candidate, "movie_id", None)
            c_eid = getattr(candidate, "episode_id", None)
            if movie_id is not None:
                if c_eid is None and (c_mid is None or int(c_mid) == int(movie_id)):
                    row = candidate
            elif episode_id is not None:
                if c_mid is None and (c_eid is None or int(c_eid) == int(episode_id)):
                    row = candidate
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
    _rm = _placeholder_runtime_minutes(session, movie_id=movie_id, episode_id=episode_id)
    _media_ctx = build_projection_context_from_session(
        session,
        movie_id=movie_id,
        episode_id=episode_id,
        runtime_minutes=_rm,
    )
    row.display_status_projected = projected_status_display(
        REQUEST_STATUS,
        reason=REQUEST_REASON,
        runtime_minutes=_rm,
        media_context=_media_ctx,
    )
    row.display_progress = 0
    # Reset observation-tracking keys in extra so stale state from a previous
    # observation round cannot affect the new pass (e.g. plex_metadata_ready_seen).
    extra = dict(getattr(row, 'extra', {}) or {})
    extra.pop('plex_metadata_ready_seen', None)
    if activity_reason:
        extra['create_reason'] = str(activity_reason)
        extra['last_action_reason'] = str(activity_reason)
        extra['last_action'] = 'created'
        # Collection-named Activity batches need a provenance stamp here (e.g. batch_id /
        # recipe_id) when materialization is driven by Collections Add-to-*arr — deferred.
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
        # Clear path so FS scan cannot match this tombstoned row to a sibling's on-disk file.
        # Column is NOT NULL — use empty string rather than SQL NULL.
        row.path = ""
        row.display_status = None
        row.display_status_projected = None
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


def _mark_placeholder_rows_deleted_for_episodes(
    session,
    *,
    episode_ids: list[int],
    activity_reason: str | None = None,
    bulk_series_id: int | None = None,
    bulk_series_title: str | None = None,
) -> list[str]:
    ids = [int(eid) for eid in (episode_ids or []) if eid is not None]
    if not ids:
        return []
    rows = session.query(Placeholder).filter(Placeholder.episode_id.in_(ids)).all()
    paths = [r.path for r in rows if getattr(r, "path", None)]
    for row in rows:
        row.has_placeholder = False
        row.lifecycle_status = "DELETED"
        # Clear path so FS scan cannot match this tombstoned row to a sibling's on-disk file.
        # Column is NOT NULL — use empty string rather than SQL NULL.
        row.path = ""
        row.display_status = None
        row.display_status_projected = None
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
        extra = dict(getattr(row, "extra", {}) or {})
        if activity_reason:
            extra["delete_reason"] = str(activity_reason)
            extra["last_action_reason"] = str(activity_reason)
            extra["last_action"] = "deleted"
        if bulk_series_id is not None:
            extra["bulk_series_delete"] = True
            extra["bulk_delete_series_id"] = int(bulk_series_id)
        if bulk_series_title:
            extra["bulk_delete_series_title"] = str(bulk_series_title)
        row.extra = extra
        row.updated_at = func.now()
        session.add(row)
    return paths


def _record_series_bulk_delete_history(
    session,
    *,
    series: Series,
    removed_placeholders: int,
    files_deleted: int,
    activity_reason: str | None,
) -> None:
    """Persist one aggregate placeholder-activity row for series tombstone cleanup."""
    if int(removed_placeholders or 0) <= 0:
        return
    title = str(getattr(series, "title", "") or "Unknown Series").strip() or "Unknown Series"
    reason = (
        f"Series removed: tombstone cleanup, {int(removed_placeholders)} placeholders deleted"
        f" ({int(files_deleted or 0)} files deleted)"
    )
    if activity_reason:
        reason = f"{reason}. {str(activity_reason).strip()}"
    session.add(
        PlaceholderActivityHistory(
            occurred_at=datetime.now(timezone.utc),
            action="Deleted",
            item_type="series",
            placeholder_id=None,
            movie_id=None,
            episode_id=None,
            series_id=int(getattr(series, "id", 0) or 0) or None,
            season_id=None,
            season_number=None,
            instance_key=getattr(series, "instance_key", None),
            instance_id=getattr(series, "instance_id", None),
            event_type="placeholder_deleted_series_bulk",
            path=str(getattr(series, "placeholder_folder", "") or ""),
            item_title=title,
            series_title=title,
            reason=reason,
            status_label="Deleted",
            source="series_tombstone_bulk",
            event_log_id=None,
            extra_snapshot={
                "bulk_series_delete": True,
                "bulk_delete_series_id": int(getattr(series, "id", 0) or 0) or None,
                "bulk_delete_series_title": str(getattr(series, "title", "") or ""),
                "removed_placeholders": int(removed_placeholders or 0),
                "files_deleted": int(files_deleted or 0),
            },
        )
    )


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
            nfo_written = ensure_movie_nfo(target_path, movie)
            ensure_movie_art(movie, target_path)
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
            placeholder_id = int(placeholder_row.id) if placeholder_row and getattr(placeholder_row, "id", None) else None

            # Persist canonical determination so the next materialization pass does not
            # re-select this row while determination==needs_placeholder (already fixed in DB).
            if bool(getattr(movie, "has_file", False)) or bool(getattr(movie, "is_deleted", False)):
                movie.determination = DETERMINATION_NOT_NEEDED
            else:
                movie.determination = DETERMINATION_EXISTS
            movie.determination_updated_at = func.now()

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
                "placeholder_id": placeholder_id,
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
            disk_paths = filter_movie_disk_cleanup_paths(session, movie, candidate_paths)
            cleanup_result = cleanup_movie_placeholder_files(candidate_paths=disk_paths)

            movie.has_placeholder = False
            movie.placeholder_filepath = None
            movie.determination = DETERMINATION_NOT_NEEDED
            movie.determination_updated_at = func.now()
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
            nfo_written = ensure_episode_nfo(target_path, episode, season, series)
            # ensure series-level tvshow.nfo is present as well
            series_folder = getattr(series, "placeholder_folder", None)
            try:
                series_nfo_written = ensure_series_nfo(series, folder=series_folder)
            except Exception:
                series_nfo_written = False
            try:
                ensure_series_art(series, series_folder=series_folder)
                ensure_episode_still_art(episode, season, series, target_path)
            except Exception:
                pass
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
            placeholder_id = int(placeholder_row.id) if placeholder_row and getattr(placeholder_row, "id", None) else None

            if bool(getattr(episode, "has_file", False)) or bool(getattr(episode, "is_deleted", False)):
                episode.determination = DETERMINATION_NOT_NEEDED
            else:
                episode.determination = DETERMINATION_EXISTS
            episode.determination_updated_at = func.now()

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
                "placeholder_id": placeholder_id,
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
            episode.determination = DETERMINATION_NOT_NEEDED
            episode.determination_updated_at = func.now()
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
        "hybrid_slice_enqueued": 0,
        "hybrid_slice_job_id": None,
        "hybrid_slice_coalesced": 0,
        "overlap_enabled": 0,
        "overlap_checkpoint_count": 0,
        "overlap_slices_enqueued": 0,
        "overlap_slices_coalesced": 0,
        "overlap_refresh_suppressed": 0,
        "overlap_skipped_min_candidates": 0,
        "overlap_skipped_pending_guard": 0,
        "overlap_first_checkpoint_at_seconds": None,
        "overlap_last_trigger": None,
        "observation_trail_enqueued": 0,
        "observation_trail_job_id": None,
        "observation_trail_group_id": None,
        "movie_refresh_triggered": False,
        "tv_refresh_triggered": False,
    }
    changed_paths: set[str] = set()
    delete_refresh_paths: set[str] = set()
    stats["movies_considered"] = len(movie_ids)
    stats["episodes_considered"] = len(episode_ids)
    activity_reason = _activity_reason_from_observation_source(observation_source)
    overlap_enabled = _materialization_overlap_enabled()
    overlap_started = time.monotonic()
    overlap_state: dict[str, Any] = {
        "movie_processed": 0,
        "episode_processed": 0,
        "pending_candidate_ids": set(),
        "pending_created_paths": set(),
        "pending_delete_paths": set(),
        "last_checkpoint_mono": overlap_started,
        "checkpoint_sequence": 0,
    }
    is_full_sync = (observation_source == "full_sync_materialization")
    event_scoped = (not is_full_sync) and str(observation_source or "").startswith("event_")
    movie_phase_start_mono: float | None = None
    last_movie_periodic_refresh_mono: float | None = None
    episode_phase_start_mono: float | None = None
    last_episode_periodic_refresh_mono: float | None = None
    bulk_initial_non_plex_refresh_done = False
    scoped_core_started = time.monotonic()

    def _scoped_phase(label: str) -> None:
        if is_full_sync:
            return
        logger.info(
            f"Scoped materialization phase [{observation_source}]: {label} "
            f"elapsed_s={time.monotonic() - scoped_core_started:.1f}",
            extra={"emoji_type": "info"},
        )

    def _trigger_bulk_initial_media_server_refresh() -> None:
        """Trigger a combined library refresh for all media servers after bulk operations."""
        if is_full_sync:
            # Full Sync uses its own phase-based rhythm.
            return
        nonlocal bulk_initial_non_plex_refresh_done
        if bulk_initial_non_plex_refresh_done:
            return
        
        def _delayed_media_server_refresh():
            refresh_stats = refresh_selected_sections(
                has_movies=bool(movie_ids),
                has_episodes=bool(episode_ids),
                include_plex=True,
                include_jellyfin=True,
                include_emby=True,
            )
            logger.info(
                f"Completed delayed media server library refresh: refreshed={refresh_stats.get('refreshed', 0)} "
                f"failed={refresh_stats.get('failed', 0)}",
                extra={"emoji_type": "success"},
            )

        _schedule_media_refresh(
            _delayed_media_server_refresh,
            delay_seconds=20.0,
            kind=KIND_BULK_INITIAL,
            payload={
                "has_movies": bool(movie_ids),
                "has_episodes": bool(episode_ids),
                "include_plex": True,
                "include_jellyfin": True,
                "include_emby": True,
            },
            log_message="Bulk sync scheduled initial media server library refresh in 20 seconds.",
        )
        bulk_initial_non_plex_refresh_done = True

    def _emit_overlap_checkpoint(trigger_reason: str, *, force: bool = False) -> None:
        if not overlap_enabled or is_full_sync or event_scoped:
            # Skip path-based overlap checkpoints during full sync or event-scoped runs.
            return

        now_mono = time.monotonic()
        overlap_state["checkpoint_sequence"] += 1
        stats["overlap_checkpoint_count"] += 1
        stats["overlap_last_trigger"] = str(trigger_reason)
        if stats.get("overlap_first_checkpoint_at_seconds") is None:
            stats["overlap_first_checkpoint_at_seconds"] = int(max(0.0, now_mono - overlap_started))

        candidate_ids = [int(x) for x in overlap_state["pending_candidate_ids"] if x is not None]
        if not force and len(candidate_ids) < _overlap_min_candidates():
            stats["overlap_skipped_min_candidates"] += 1
            overlap_state["last_checkpoint_mono"] = now_mono
            overlap_state["movie_processed"] = 0
            overlap_state["episode_processed"] = 0
            return

        active_slices = 0 # No more slices
        if False: # Skip slice guard
            stats["overlap_skipped_pending_guard"] += 1
            overlap_state["last_checkpoint_mono"] = now_mono
            overlap_state["movie_processed"] = 0
            overlap_state["episode_processed"] = 0
            return

        created_paths = set(overlap_state["pending_created_paths"])
        delete_paths = set(overlap_state["pending_delete_paths"])
        if created_paths or delete_paths:
            target_sections = _target_refresh_section_ids(
                    has_movies=bool(movie_ids),
                    has_episodes=bool(episode_ids),
                )
            lease = try_acquire_refresh_lease(
                section_ids=target_sections,
                source="materialization_overlap_checkpoint",
                min_interval_seconds=_overlap_refresh_min_interval_seconds(),
                lease_seconds=_overlap_refresh_lease_seconds(),
            )
            if bool(lease.get("allowed", False)):
                def _delayed_plex_path_refresh(_created_paths, _delete_paths):
                    refresh_stats = refresh_all_path_batches_with_section_fallback(
                        [
                            (_created_paths, "Created"),
                            (_delete_paths, "Deleted"),
                        ],
                        has_movies=bool(movie_ids),
                        has_episodes=bool(episode_ids),
                        enable_section_fallback=False,
                        fallback_wait_seconds=0,
                        include_plex=True,
                    )
                    logger.info(
                        f"Completed delayed Plex path refresh: refreshed={refresh_stats.get('refreshed', 0)} "
                        f"failed={refresh_stats.get('failed', 0)}",
                        extra={"emoji_type": "success"},
                    )

                if use_job_driven_refresh() and _enqueue_media_refresh_via_new_session(
                    kind=KIND_OVERLAP_PATH_BATCH,
                    payload={
                        "has_movies": bool(movie_ids),
                        "has_episodes": bool(episode_ids),
                        "include_plex": True,
                        "created_paths": sorted(set(created_paths)),
                        "delete_paths": sorted(set(delete_paths)),
                    },
                    delay_seconds=20.0,
                ):
                    pass
                else:
                    threading.Timer(20.0, _delayed_plex_path_refresh, args=(set(created_paths), set(delete_paths))).start()
                overlap_state["pending_created_paths"].clear()
                overlap_state["pending_delete_paths"].clear()
                logger.info("Scheduled delayed Plex path refresh in 20 seconds.", extra={"emoji_type": "info"})
            else:
                stats["overlap_refresh_suppressed"] += 1
                logger.info(
                    "Materialization overlap refresh suppressed by durable throttle "
                    f"reason={lease.get('reason')} blocked_sections={lease.get('blocked_section_ids')}",
                    extra={"emoji_type": "info"},
                )

        if candidate_ids:
            # Instead of observation slices, we now just trigger a standard refresh for the new items.
            _trigger_bulk_initial_media_server_refresh()
            overlap_state["pending_candidate_ids"].clear()

        overlap_state["last_checkpoint_mono"] = now_mono
        overlap_state["movie_processed"] = 0
        overlap_state["episode_processed"] = 0

    def _split_movie_ids_by_determination(session, ids: list[int]) -> tuple[list[int], list[int]]:
        if not ids:
            return [], []
        rows = session.query(Movie.id, Movie.determination).filter(Movie.id.in_(ids)).all()
        obsolete_ids: list[int] = []
        needs_ids: list[int] = []
        for mid, det in rows:
            if det == DETERMINATION_OBSOLETE:
                obsolete_ids.append(int(mid))
            elif det == DETERMINATION_NEEDS:
                needs_ids.append(int(mid))
        return obsolete_ids, needs_ids

    def _process_single_movie_materialization(movie_id: int) -> None:
        nonlocal stats, changed_paths, delete_refresh_paths, movie_phase_start_mono, last_movie_periodic_refresh_mono
        result = apply_movie_materialization(movie_id, session=session, activity_reason=activity_reason)
        if not result.get("ok"):
            stats["errors"] += 1
            logger.error(
                f"Movie materialization failed movie_id={movie_id}: {result.get('reason')}",
                extra={"emoji_type": "error"},
            )
            return

        action = result.get("action")
        if action == "created_or_exists":
            if result.get("created"):
                stats["created"] += 1
            else:
                stats["noop"] += 1
            if result.get("created"):
                logger.info(
                    f"Placeholder created for movie_id={movie_id}",
                    extra={"emoji_type": "create"},
                )
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
                    overlap_state["pending_created_paths"].add(path)
            placeholder_id = result.get("placeholder_id")
            if placeholder_id:
                overlap_state["pending_candidate_ids"].add(int(placeholder_id))
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
                overlap_state["pending_delete_paths"].add(refresh_path)
        else:
            stats["noop"] += 1

        if is_full_sync and result.get("ok") and (result.get("created") or result.get("deleted")):
            now = time.monotonic()
            if movie_phase_start_mono is None:
                movie_phase_start_mono = now
                last_movie_periodic_refresh_mono = now

                def _initial_movie_refresh():
                    refresh_selected_sections(has_movies=True, has_episodes=False, bypass_suppression=True)

                _schedule_media_refresh(
                    _initial_movie_refresh,
                    delay_seconds=5.0,
                    kind=KIND_PHASE_INITIAL_MOVIE,
                    payload={
                        "has_movies": True,
                        "has_episodes": False,
                        "bypass_suppression": True,
                    },
                    log_message="Full sync movie phase started; scheduled initial library refresh in 5 seconds.",
                )
                stats["movie_refresh_triggered"] = True

            if now - last_movie_periodic_refresh_mono >= 300:
                refresh_selected_sections(has_movies=True, has_episodes=False, bypass_suppression=True)
                last_movie_periodic_refresh_mono = now
                logger.info("Full sync movie phase recurring refresh triggered (5-minute interval).", extra={"emoji_type": "info"})

        if overlap_enabled:
            overlap_state["movie_processed"] += 1
            now_mono = time.monotonic()
            should_trigger, trigger_kind = _should_trigger_overlap_checkpoint(
                processed_since_checkpoint=int(overlap_state["movie_processed"]),
                count_threshold=_overlap_checkpoint_threshold("movie"),
                last_checkpoint_mono=float(overlap_state["last_checkpoint_mono"]),
                now_mono=now_mono,
                staleness_seconds=_overlap_staleness_seconds(),
            )
            if should_trigger:
                _emit_overlap_checkpoint(f"movie_{trigger_kind}")

    movie_obsolete_ids, _movie_needs_seed = _split_movie_ids_by_determination(session, movie_ids)
    for movie_id in movie_obsolete_ids:
        _process_single_movie_materialization(movie_id)
    if movie_obsolete_ids:
        run_determination_for_entities_in_session(session, movie_ids=list(movie_obsolete_ids), episode_ids=[])
    _scoped_phase(
        "obsolete movie pass done (includes secondary scoped determination when obsolete set non-empty)"
    )
    if movie_ids:
        pending_movie_needs = [
            int(r[0])
            for r in session.query(Movie.id)
            .filter(Movie.id.in_(movie_ids), Movie.determination == DETERMINATION_NEEDS)
            .all()
        ]
    else:
        pending_movie_needs = []
    for movie_id in pending_movie_needs:
        _process_single_movie_materialization(movie_id)
    _scoped_phase("needs-placeholder movie pass done")

    if is_full_sync and movie_phase_start_mono is not None:
        def _final_movie_refresh():
            refresh_selected_sections(has_movies=True, has_episodes=False, bypass_suppression=True)
        _schedule_media_refresh(
            _final_movie_refresh,
            delay_seconds=5.0,
            kind=KIND_PHASE_FINAL_MOVIE,
            payload={
                "has_movies": True,
                "has_episodes": False,
                "bypass_suppression": True,
            },
            log_message="Full sync movie phase complete; scheduled final library refresh in 5 seconds.",
        )

    def _split_episode_ids_by_determination(session, ids: list[int]) -> tuple[list[int], list[int]]:
        if not ids:
            return [], []
        rows = session.query(Episode.id, Episode.determination).filter(Episode.id.in_(ids)).all()
        obsolete_ids: list[int] = []
        needs_ids: list[int] = []
        for eid, det in rows:
            if det == DETERMINATION_OBSOLETE:
                obsolete_ids.append(int(eid))
            elif det == DETERMINATION_NEEDS:
                needs_ids.append(int(eid))
        return obsolete_ids, needs_ids

    def _process_single_episode_materialization(episode_id: int) -> None:
        nonlocal stats, changed_paths, delete_refresh_paths, episode_phase_start_mono, last_episode_periodic_refresh_mono
        result = apply_episode_materialization(episode_id, session=session, activity_reason=activity_reason)
        if not result.get("ok"):
            stats["errors"] += 1
            logger.error(
                f"Episode materialization failed episode_id={episode_id}: {result.get('reason')}",
                extra={"emoji_type": "error"},
            )
            return

        action = result.get("action")
        if action == "created_or_exists":
            if result.get("created"):
                stats["created"] += 1
            else:
                stats["noop"] += 1
            if result.get("created"):
                logger.info(
                    f"Placeholder created for episode_id={episode_id}",
                    extra={"emoji_type": "create"},
                )
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
                    overlap_state["pending_created_paths"].add(path)
            placeholder_id = result.get("placeholder_id")
            if placeholder_id:
                overlap_state["pending_candidate_ids"].add(int(placeholder_id))
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
                overlap_state["pending_delete_paths"].add(refresh_path)
        else:
            stats["noop"] += 1

        if is_full_sync and result.get("ok") and (result.get("created") or result.get("deleted")):
            now = time.monotonic()
            if episode_phase_start_mono is None:
                episode_phase_start_mono = now
                last_episode_periodic_refresh_mono = now

                def _initial_episode_refresh():
                    refresh_selected_sections(has_movies=False, has_episodes=True, bypass_suppression=True)

                _schedule_media_refresh(
                    _initial_episode_refresh,
                    delay_seconds=5.0,
                    kind=KIND_PHASE_INITIAL_EPISODE,
                    payload={
                        "has_movies": False,
                        "has_episodes": True,
                        "bypass_suppression": True,
                    },
                    log_message="Full sync episode phase started; scheduled initial library refresh in 5 seconds.",
                )
                stats["tv_refresh_triggered"] = True

            if now - last_episode_periodic_refresh_mono >= 300:
                refresh_selected_sections(has_movies=False, has_episodes=True, bypass_suppression=True)
                last_episode_periodic_refresh_mono = now
                logger.info("Full sync episode phase recurring refresh triggered (5-minute interval).", extra={"emoji_type": "info"})

        if overlap_enabled:
            overlap_state["episode_processed"] += 1
            now_mono = time.monotonic()
            should_trigger, trigger_kind = _should_trigger_overlap_checkpoint(
                processed_since_checkpoint=int(overlap_state["episode_processed"]),
                count_threshold=_overlap_checkpoint_threshold("episode"),
                last_checkpoint_mono=float(overlap_state["last_checkpoint_mono"]),
                now_mono=now_mono,
                staleness_seconds=_overlap_staleness_seconds(),
            )
            if should_trigger:
                _emit_overlap_checkpoint(f"episode_{trigger_kind}")

    def _process_deleted_series_bulk(series_id: int, series_episode_ids: list[int]) -> None:
        nonlocal stats, delete_refresh_paths
        ids = [int(x) for x in (series_episode_ids or []) if x is not None]
        if not ids:
            return
        series = session.query(Series).filter(Series.id == int(series_id)).first()
        if not series:
            for episode_id in ids:
                _process_single_episode_materialization(episode_id)
            return

        candidate_paths = _mark_placeholder_rows_deleted_for_episodes(
            session,
            episode_ids=ids,
            activity_reason=activity_reason,
            bulk_series_id=int(series_id),
            bulk_series_title=str(getattr(series, "title", "") or ""),
        )
        db_paths = [
            r[0]
            for r in session.query(Episode.placeholder_filepath)
            .filter(Episode.id.in_(ids), Episode.placeholder_filepath.isnot(None))
            .all()
        ]
        candidate_paths.extend([p for p in db_paths if p])
        cleanup_result = cleanup_deleted_series_placeholder_files(
            session,
            series=series,
            candidate_paths=candidate_paths,
        )
        session.query(Episode).filter(Episode.id.in_(ids)).update(
            {
                Episode.has_placeholder: False,
                Episode.placeholder_filepath: None,
                Episode.updated_at: func.now(),
            },
            synchronize_session=False,
        )
        from services.series_episode_stats_hooks import refresh_series_stats_after_bulk

        refresh_series_stats_after_bulk(session, series_ids={int(series_id)})
        stats["deleted"] += len(ids)
        files_deleted_count = int(cleanup_result.get("files_deleted", 0) or 0)
        stats["files_deleted"] += files_deleted_count
        stats["nfo_deleted"] += int(cleanup_result.get("nfo_deleted_count", 0) or 0)
        stats["directories_deleted"] += int(cleanup_result.get("directories_deleted", 0) or 0)
        if cleanup_result.get("series_nfo_deleted"):
            stats["series_nfo_deleted"] += 1
        _record_series_bulk_delete_history(
            session,
            series=series,
            removed_placeholders=len(ids),
            files_deleted=files_deleted_count,
            activity_reason=activity_reason,
        )
        for refresh_path in cleanup_result.get("refresh_paths", []) or []:
            delete_refresh_paths.add(refresh_path)
            overlap_state["pending_delete_paths"].add(refresh_path)
        logger.debug(
            f"Series-level placeholder cleanup for deleted series_id={int(series_id)} "
            f"title={getattr(series, 'title', None)!r}: episodes={len(ids)} "
            f"files_deleted={int(cleanup_result.get('files_deleted', 0) or 0)} "
            f"dirs_deleted={int(cleanup_result.get('directories_deleted', 0) or 0)}",
            extra={"emoji_type": "delete"},
        )

    episode_obsolete_ids, _episode_needs_seed = _split_episode_ids_by_determination(session, episode_ids)
    bulk_deleted_series_map: dict[int, list[int]] = {}
    if episode_obsolete_ids:
        deleted_series_rows = (
            session.query(Episode.id, Season.series_id)
            .join(Season, Episode.season_id == Season.id)
            .join(Series, Season.series_id == Series.id)
            .filter(Episode.id.in_(episode_obsolete_ids), Series.is_deleted == True)  # noqa: E712
            .all()
        )
        for eid, sid in deleted_series_rows:
            if sid is None:
                continue
            bulk_deleted_series_map.setdefault(int(sid), []).append(int(eid))
    bulk_episode_ids = {eid for eids in bulk_deleted_series_map.values() for eid in eids}
    for sid, eids in bulk_deleted_series_map.items():
        _process_deleted_series_bulk(sid, eids)
    for episode_id in episode_obsolete_ids:
        if episode_id in bulk_episode_ids:
            continue
        _process_single_episode_materialization(episode_id)
    if episode_obsolete_ids:
        run_determination_for_entities_in_session(session, movie_ids=[], episode_ids=list(episode_obsolete_ids))
    if episode_ids:
        pending_episode_needs = [
            int(r[0])
            for r in session.query(Episode.id)
            .filter(Episode.id.in_(episode_ids), Episode.determination == DETERMINATION_NEEDS)
            .all()
        ]
    else:
        pending_episode_needs = []
    for episode_id in pending_episode_needs:
        _process_single_episode_materialization(episode_id)
    _scoped_phase("episode obsolete + needs passes done")

    if is_full_sync and episode_phase_start_mono is not None:
        def _final_episode_refresh():
            refresh_selected_sections(has_movies=False, has_episodes=True, bypass_suppression=True)
        _schedule_media_refresh(
            _final_episode_refresh,
            delay_seconds=5.0,
            kind=KIND_PHASE_FINAL_EPISODE,
            payload={
                "has_movies": False,
                "has_episodes": True,
                "bypass_suppression": True,
            },
            log_message="Full sync episode phase complete; scheduled final library refresh in 5 seconds.",
        )

    # Final overlap flush coalesces path refreshes for large materialization passes. For
    # event_scoped runs (``event_*`` sources, e.g. ARR webhooks) it is usually 1 title and
    # the checkpoint still called ``try_acquire_refresh_lease`` / job enqueue and could
    # block 10s+ on DB locks while this session held rows. The delayed final block below
    # already schedules the same path refresh with created_paths/delete_refresh_paths.
    if overlap_enabled and (movie_ids or episode_ids) and not event_scoped:
        _scoped_phase("before final overlap checkpoint")
        _emit_overlap_checkpoint("final_loop_flush", force=True)
        _scoped_phase("after final overlap checkpoint")
    elif overlap_enabled and (movie_ids or episode_ids) and event_scoped:
        logger.info(
            f"Skipped final overlap checkpoint (event_scoped source={observation_source}) — "
            f"delayed final path refresh still scheduled with collected paths",
            extra={"emoji_type": "info"},
        )

    logger.info(
        "Placeholder creation summary: "
        f"placeholders_created={stats['created']} "
        f"already_present={stats['noop']} "
        f"deleted={stats['deleted']} "
        f"errors={stats['errors']} "
        f"nfo_written={stats['nfo_written']}",
        extra={"emoji_type": "success"},
    )

    # Event-scoped runs used to hold one huge transaction until after delayed-final
    # scheduling/enqueue, making session.commit() wait on row locks held by other workers.
    # Commit core placeholder/movie/episode writes here so locks release before job enqueue.
    if not is_full_sync:
        session.commit()
        logger.info(
            "Scoped materialization: committed core DB state before delayed path refresh scheduling "
            "(releases locks earlier for event-scoped runs)",
            extra={"emoji_type": "info"},
        )

    _scoped_phase("before scheduling delayed final path refresh (async Timer or job)")

    if not is_full_sync:
        def _trigger_delayed_final_refresh():
            refresh_stats = refresh_all_path_batches_with_section_fallback(
                [
                    (changed_paths, "Created"),
                    (delete_refresh_paths, "Deleted"),
                ],
                has_movies=bool(movie_ids),
                has_episodes=bool(episode_ids),
                enable_section_fallback=False,
                fallback_wait_seconds=0,
                include_plex=True,
            )
            logger.info(
                f"Completed delayed final media server refresh: refreshed={refresh_stats.get('refreshed', 0)} "
                f"failed={refresh_stats.get('failed', 0)}",
                extra={"emoji_type": "success"},
            )

        _schedule_media_refresh(
            _trigger_delayed_final_refresh,
            delay_seconds=20.0,
            kind=KIND_DELAYED_FINAL,
            payload={
                "has_movies": bool(movie_ids),
                "has_episodes": bool(episode_ids),
                "include_plex": True,
                "created_paths": sorted(set(changed_paths)),
                "delete_paths": sorted(set(delete_refresh_paths)),
            },
            log_message="Media server refreshes were scheduled to run asynchronously in 20 seconds.",
        )

    _scoped_phase("materialization core returning (next step is session.commit in caller)")
    return stats


def run_materialization_pass() -> dict[str, Any]:
    """Apply file/DB side effects for needs/obsolete determinations.

    Creates all placeholder files first, then runs one observation sweep.
    Bulk sync skips immediate Plex path refresh fanout and repeated overlap
    refreshes; Emby and Jellyfin get one library refresh when hybrid
    continuation is first queued and one more after materialization while
    hybrid observation owns Plex follow-up refresh decisions during the
    unresolved tail.

    This single-pass approach is far faster than per-batch cycling for large
    first-run syncs (tens of thousands of items) while avoiding continuous scan
    churn from placeholder fanout.
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
    log_subject: str | None = None,
) -> dict[str, Any]:
    """Run materialization only for targeted entities.

    This is used by webhook/event workflows so only affected content is
    materialized, refreshed, and observed.
    """
    movie_ids = [int(mid) for mid in (movie_ids or []) if mid is not None]
    episode_ids = [int(eid) for eid in (episode_ids or []) if eid is not None]
    subject = str(log_subject or "").strip()
    subject_part = f" · {subject}" if subject else ""

    session = get_session()
    stop_scoped_hb = threading.Event()
    scoped_started = time.monotonic()
    _scoped_hb_interval = max(
        3.0, float(getattr(settings, "STALL_HEARTBEAT_INTERVAL_SEC", 10.0) or 10.0)
    )

    def _scoped_materialization_stall_heartbeat():
        while not stop_scoped_hb.wait(_scoped_hb_interval):
            logger.verbose(
                f"Scoped materialization still running: observation_source={observation_source} "
                f"movies={len(movie_ids)} episodes={len(episode_ids)} "
                f"elapsed_s={time.monotonic() - scoped_started:.1f} "
                f"(long stall after 'persisting' → often PostgreSQL commit waiting on locks)",
                extra={"emoji_type": "processing"},
            )

    _scoped_t = None
    if movie_ids or episode_ids:
        logger.info(
            f"Scoped materialization starting{subject_part}: observation_source={observation_source} "
            f"movies={len(movie_ids)} episodes={len(episode_ids)}",
            extra={"emoji_type": "info"},
        )
        _scoped_t = threading.Thread(
            target=_scoped_materialization_stall_heartbeat,
            name=f"scoped-mat-hb-{observation_source}",
            daemon=True,
        )
        _scoped_t.start()
    try:
        stats = run_materialization_for_entities_in_session(
            session,
            movie_ids=movie_ids,
            episode_ids=episode_ids,
            observation_source=observation_source,
        )
        if movie_ids or episode_ids:
            logger.info(
                f"Scoped materialization persisting{subject_part}: observation_source={observation_source} "
                f"(if this line is followed by a long silence, commit is usually waiting on a row lock)",
                extra={"emoji_type": "info"},
            )
        v_hb = start_verbose_stall_heartbeat(
            f"scoped_materialization.session.commit source={observation_source}",
        )
        try:
            session.commit()
        finally:
            v_hb.set()
        logger.info(
            f"Scoped materialization complete{subject_part}: {stats}",
            extra={"emoji_type": "success"},
        )
        return stats
    except Exception as e:
        session.rollback()
        logger.error(
            f"Scoped materialization failed{subject_part}: {e}",
            extra={"emoji_type": "error"},
        )
        raise
    finally:
        stop_scoped_hb.set()
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
