from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.config import settings
from services.messages import render as render_message
from services.placeholders import episode_placeholder_path, movie_placeholder_path
from services.placeholder_activity_log import (
    append_placeholder_activity_status,
    materialization_stats_dict,
    outcome_reason_and_status_from_materialization,
)
from services.postgres.models import Episode, Job, Movie, Placeholder, Season, Series
from services.source_of_truth.arr_api import fetch_radarr_movie, fetch_sonarr_episode_item
from services.source_of_truth.arr_share_guard import expand_determination_entity_ids
from services.source_of_truth.determiner import run_determination_for_entities_in_session
from services.source_of_truth.materializer import run_materialization_for_entities
from services.source_of_truth.materializer import run_materialization_for_entities_in_session
from services.source_of_truth.status_intent import DisplayStatus, StatusIntent, StatusSource
from services.source_of_truth.status_orchestrator import StatusOrchestrator
from services.source_of_truth.status_reconciler import NFO_REFRESH_JOB_TYPE
from services.status_projection import projected_status_display

IMPORT_GRACE_JOB_TYPE = "import_grace"
IMPORT_GRACE_REASON = "import_grace_countdown"
IMPORT_GRACE_CANCEL_ERROR = "Cancelled: file deleted during import grace"
IMPORT_GRACE_INTERRUPT_REASON = "import_grace_interrupted"
# Number of countdown ticks before the final "less than a minute" tick.
# Historically: 5, 4, 3, 2, 1 minutes remaining, plus a "less than a minute" final.
COUNTDOWN_MINUTES_DECREASING = (5, 4, 3, 2, 1)


def arr_import_still_has_file(session, *, content_type: str, entity_id: int) -> bool:
    """True when Arr still reports hasFile, or the Arr check cannot be completed.

    Placeholdarr often cannot see Arr download/import paths (unmounted volumes), so
    grace validity uses Radarr/Sonarr hasFile rather than a local filesystem check.
    API failure or missing ids keep grace running (cannot verify).
    """
    ct = str(content_type or "").strip().lower()
    try:
        eid = int(entity_id)
    except (TypeError, ValueError):
        return True
    if ct not in {"movie", "episode"} or eid <= 0:
        return True

    try:
        if ct == "movie":
            movie = session.query(Movie).filter(Movie.id == eid).first()
            if not movie:
                return True
            radarr_id = getattr(movie, "radarrid", None)
            if not radarr_id:
                return True
            base_url, api_key = settings.resolve_arr_endpoint(
                "radarr",
                instance_key=str(getattr(movie, "instance_key", "") or "") or None,
                instance_id=str(getattr(movie, "instance_id", "") or "") or None,
            )
            if not base_url or not api_key:
                return True
            item = fetch_radarr_movie(int(radarr_id), base_url, api_key, bypass_cache=True)
            if not isinstance(item, dict):
                return True
            return bool(item.get("hasFile") or (item.get("movieFile") or {}).get("path"))

        episode = session.query(Episode).filter(Episode.id == eid).first()
        if not episode:
            return True
        sonarr_id = getattr(episode, "sonarrid", None)
        if not sonarr_id:
            return True
        season = session.query(Season).filter(Season.id == int(episode.season_id)).first()
        if not season:
            return True
        series = session.query(Series).filter(Series.id == int(season.series_id)).first()
        if not series:
            return True
        base_url, api_key = settings.resolve_arr_endpoint(
            "sonarr",
            instance_key=str(getattr(series, "instance_key", "") or "") or None,
            instance_id=str(getattr(series, "instance_id", "") or "") or None,
        )
        if not base_url or not api_key:
            return True
        item = fetch_sonarr_episode_item(int(sonarr_id), url=base_url, api_key=api_key, bypass_cache=True)
        if not isinstance(item, dict):
            return True
        return bool(item.get("hasFile") or (item.get("episodeFile") or {}).get("path"))
    except Exception:
        return True


def restore_request_after_import_grace_interrupt(
    session,
    *,
    content_type: str,
    entity_id: int,
    placeholder_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Project REQUEST (with player metadata push) after grace is interrupted.

    Used when Arr no longer reports a file mid-grace. EVENT_DELETE_FILE source
    ensures post-NFO player title/summary projection runs (unlike INITIAL_CREATION).
    """
    ct = str(content_type or "").strip().lower()
    try:
        eid = int(entity_id)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "bad_entity_id", "applied": 0}

    ids = [int(pid) for pid in (placeholder_ids or []) if pid is not None]
    if not ids:
        q = session.query(Placeholder.id).filter(Placeholder.has_placeholder == True)  # noqa: E712
        if ct == "movie":
            q = q.filter(Placeholder.movie_id == eid)
        elif ct == "episode":
            q = q.filter(Placeholder.episode_id == eid)
        else:
            return {"ok": False, "reason": "bad_content_type", "applied": 0}
        ids = [int(pid) for (pid,) in q.all()]

    if not ids:
        return {"ok": True, "applied": 0, "placeholder_ids": []}

    intents = [
        StatusIntent(
            placeholder_id=pid,
            new_status=DisplayStatus.REQUEST.value,
            reason="Import grace interrupted: Arr no longer reports a file",
            source=StatusSource.EVENT_DELETE_FILE,
            trigger_nfo_refresh=True,
            metadata={
                "content_type": ct,
                "entity_id": eid,
                "reason": IMPORT_GRACE_INTERRUPT_REASON,
            },
        )
        for pid in ids
    ]
    orchestrator = StatusOrchestrator(session=session)
    applied = orchestrator.apply_and_project_statuses(intents)
    return {"ok": True, "applied": int(applied), "placeholder_ids": ids}


def cancel_import_grace_for_entity(
    session,
    *,
    content_type: str,
    entity_id: int,
) -> int:
    """Fail queued/in-flight import_grace jobs for one movie or episode row.

    File-delete handlers call this so countdown ticks and finalize cannot overwrite
    a mid-grace interrupt (placeholder kept, status restored to REQUEST).
    Returns how many jobs were cancelled.
    """
    ct = str(content_type or "").strip().lower()
    try:
        eid = int(entity_id)
    except (TypeError, ValueError):
        return 0
    if ct not in {"movie", "episode"} or eid <= 0:
        return 0

    rows = (
        session.query(Job)
        .filter(
            Job.job_type == IMPORT_GRACE_JOB_TYPE,
            Job.status.in_(("PENDING", "CLAIMED", "WORKING")),
        )
        .all()
    )
    cancelled = 0
    for job in rows:
        payload = job.payload if isinstance(job.payload, dict) else {}
        if str(payload.get("content_type") or "").strip().lower() != ct:
            continue
        try:
            if int(payload.get("entity_id")) != eid:
                continue
        except (TypeError, ValueError):
            continue
        job.status = "FAILED"
        job.error_message = IMPORT_GRACE_CANCEL_ERROR
        session.add(job)
        cancelled += 1
    return cancelled


def interrupt_import_grace_keep_placeholder(
    session,
    *,
    content_type: str,
    entity_id: int,
) -> dict[str, Any]:
    """Cancel active grace for an entity and restore REQUEST without rematerializing.

    Used when Arr reports file-delete while import grace is still running: the
    placeholder file/row was never removed, so clearing ``has_placeholder`` and
    rematerializing is unnecessary (and races the countdown UI).
    """
    cancelled = cancel_import_grace_for_entity(
        session,
        content_type=content_type,
        entity_id=entity_id,
    )
    if cancelled <= 0:
        return {"interrupted": False, "cancelled_jobs": 0, "restore": None}
    restore = restore_request_after_import_grace_interrupt(
        session,
        content_type=content_type,
        entity_id=entity_id,
    )
    return {
        "interrupted": True,
        "cancelled_jobs": int(cancelled),
        "restore": restore,
    }


def _countdown_status_text(minutes_remaining: int) -> str:
    """Render the customizable countdown status text for a given minutes-remaining tick."""
    return render_message("import_grace.countdown", {"MinutesRemaining": str(int(minutes_remaining))})


def _countdown_lt_1_min_status_text() -> str:
    return render_message("import_grace.countdown_lt_1_min", {})


def _all_countdown_status_texts(step_seconds: int = 60) -> list[str]:
    """Render all countdown status strings in display order.

    Accelerated cadence still uses the configured 5/4/3/2/1-minute templates so
    each tick is a distinct projected status. The step interval only changes
    how quickly those labels advance, not the label text.
    """
    del step_seconds  # Cadence is applied in build_import_grace_schedule, not labels.
    return [_countdown_status_text(m) for m in COUNTDOWN_MINUTES_DECREASING] + [_countdown_lt_1_min_status_text()]


# Backwards-compatible shim for any consumer still importing the static list.
# Generated lazily so it picks up template overrides at scheduling time.
def __getattr__(name: str):  # pragma: no cover - simple compatibility shim
    if name == "COUNTDOWN_STATUSES":
        return _all_countdown_status_texts()
    raise AttributeError(name)


def _import_grace_step_seconds() -> int:
    is_accelerated = bool(getattr(settings, "ENABLE_IMPORT_GRACE_ACCELERATED", False))
    if is_accelerated:
        step = max(1, int(getattr(settings, "IMPORT_GRACE_ACCELERATED_STEP_SECONDS", 10)))
        from core.logger import logger
        logger.info(f"Import grace using accelerated cadence: {step}s per step", extra={'emoji_type': 'info'})
        return step
    return max(1, int(getattr(settings, "IMPORT_GRACE_STEP_SECONDS", 60)))


def build_import_grace_schedule(base_time: datetime | None = None, step_seconds: int | None = None) -> list[dict[str, Any]]:
    now = base_time or datetime.now(timezone.utc)
    step = max(1, int(step_seconds or _import_grace_step_seconds()))

    countdown_texts = _all_countdown_status_texts(step)
    scheduled: list[dict[str, Any]] = []
    for step_index, status_text in enumerate(countdown_texts):
        scheduled.append(
            {
                "step_index": step_index,
                "run_after": now + timedelta(seconds=step_index * step),
                "status_text": status_text,
                "finalize": False,
            }
        )

    scheduled.append(
        {
            "step_index": len(countdown_texts),
            "run_after": now + timedelta(seconds=len(countdown_texts) * step),
            "status_text": None,
            "finalize": True,
        }
    )
    return scheduled




def _enqueue_nfo_refresh_job(session, placeholder_ids: list[int]) -> None:
    if not placeholder_ids:
        return
    from services.source_of_truth.job_priority import default_priority_for

    session.add(
        Job(
            job_type=NFO_REFRESH_JOB_TYPE,
            payload={"placeholder_ids": placeholder_ids},
            status="PENDING",
            run_after=datetime.now(timezone.utc),
            priority=default_priority_for(NFO_REFRESH_JOB_TYPE),
        )
    )


def _set_countdown_status(session, placeholder_ids: list[int], status_text: str) -> list[int]:
    if not placeholder_ids:
        return []

    rows = (
        session.query(Placeholder)
        .filter(
            Placeholder.id.in_(placeholder_ids),
            Placeholder.has_placeholder == True,  # noqa: E712
        )
        .all()
    )

    active_ids: list[int] = []
    for row in rows:
        row.display_status = status_text
        row.display_reason = IMPORT_GRACE_REASON
        row.display_status_projected = projected_status_display(status_text, reason=IMPORT_GRACE_REASON)
        # Import grace is past Arr queue tracking; stop RefreshMonitoredDownloads polling.
        row.queue_monitor_active = False
        row.queue_monitor_active_set_at = None
        session.add(row)
        active_ids.append(int(row.id))
    return active_ids


def _schedule_import_grace(
    session,
    *,
    content_type: str,
    entity_id: int,
    placeholder_ids: list[int],
    file_path: str | None,
) -> dict[str, Any]:
    schedule = build_import_grace_schedule()

    initial_status = str(schedule[0]["status_text"])
    active_ids = _set_countdown_status(session, placeholder_ids, initial_status)
    _enqueue_nfo_refresh_job(session, active_ids)

    from services.source_of_truth.job_priority import default_priority_for

    grace_priority = default_priority_for(IMPORT_GRACE_JOB_TYPE)
    for item in schedule[1:]:
        session.add(
            Job(
                job_type=IMPORT_GRACE_JOB_TYPE,
                payload={
                    "content_type": content_type,
                    "entity_id": int(entity_id),
                    "placeholder_ids": placeholder_ids,
                    "file_path": file_path,
                    "step_index": int(item["step_index"]),
                    "status_text": item["status_text"],
                    "finalize": bool(item["finalize"]),
                },
                status="PENDING",
                run_after=item["run_after"],
                priority=grace_priority,
            )
        )

    return {
        "ok": True,
        "content_type": content_type,
        "entity_id": int(entity_id),
        "placeholder_count": len(placeholder_ids),
        "active_placeholder_count": len(active_ids),
        "step_seconds": _import_grace_step_seconds(),
        "scheduled_jobs": len(schedule) - 1,
        "initial_status": initial_status,
    }


def schedule_movie_import_grace(session, movie_row_id: int, file_path: str | None = None) -> dict[str, Any]:
    raw_ids = (
        session.query(Placeholder.id)
        .filter(
            Placeholder.movie_id == int(movie_row_id),
            Placeholder.has_placeholder == True,  # noqa: E712
        )
        .all()
    )
    placeholder_ids = [int(pid) for (pid,) in raw_ids]
    return _schedule_import_grace(
        session,
        content_type="movie",
        entity_id=int(movie_row_id),
        placeholder_ids=placeholder_ids,
        file_path=file_path,
    )


def schedule_episode_import_grace(session, episode_row_id: int, file_path: str | None = None) -> dict[str, Any]:
    raw_ids = (
        session.query(Placeholder.id)
        .filter(
            Placeholder.episode_id == int(episode_row_id),
            Placeholder.has_placeholder == True,  # noqa: E712
        )
        .all()
    )
    placeholder_ids = [int(pid) for (pid,) in raw_ids]
    return _schedule_import_grace(
        session,
        content_type="episode",
        entity_id=int(episode_row_id),
        placeholder_ids=placeholder_ids,
        file_path=file_path,
    )


def _import_grace_job_was_cancelled(session, job: Job) -> bool:
    """True when file-delete (or Arr abort) already failed this grace job row.

    Cancel commits in another session while a worker may still hold a CLAIMED
    descriptor. Re-read error_message so we do not overwrite REQUEST with a
    stale countdown tick after interrupt.
    """
    raw_id = getattr(job, "id", None)
    try:
        jid = int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        jid = None
    try:
        session.refresh(job)
    except Exception:
        pass
    err = str(getattr(job, "error_message", "") or "")
    if err == IMPORT_GRACE_CANCEL_ERROR:
        return True
    if jid is None:
        return False
    try:
        from sqlalchemy import text

        row = session.execute(
            text("SELECT error_message FROM job WHERE id = :id"),
            {"id": jid},
        ).first()
        if row and str(row[0] or "") == IMPORT_GRACE_CANCEL_ERROR:
            return True
    except Exception:
        pass
    return False


def process_import_grace_job(session, job: Job) -> dict[str, Any]:
    from core.logger import logger
    
    payload = job.payload or {}
    content_type = str(payload.get("content_type") or "").strip().lower()

    entity_id_val = payload.get("entity_id")
    try:
        entity_id = int(entity_id_val)
    except Exception:
        logger.debug(f"Import grace job missing entity_id: {entity_id_val}", extra={'emoji_type': 'debug'})
        return {"done": True, "ok": False, "reason": "missing_entity_id"}

    placeholder_ids = [int(pid) for pid in (payload.get("placeholder_ids") or []) if pid is not None]
    file_path = payload.get("file_path")
    is_finalize = bool(payload.get("finalize"))
    step_index = int(payload.get("step_index", -1))

    logger.debug(
        f"Import grace job processing: step_index={step_index}, finalize={is_finalize}, content_type={content_type}, entity_id={entity_id}, placeholder_ids={placeholder_ids}",
        extra={'emoji_type': 'debug'}
    )

    if _import_grace_job_was_cancelled(session, job):
        logger.info(
            f"Import grace skipped (job cancelled): step_index={step_index} finalize={is_finalize} "
            f"content_type={content_type} entity_id={entity_id}",
            extra={"emoji_type": "info"},
        )
        return {
            "done": True,
            "ok": True,
            "phase": "cancelled",
            "content_type": content_type,
            "entity_id": entity_id,
            "reason": "cancelled",
        }

    if not arr_import_still_has_file(session, content_type=content_type, entity_id=entity_id):
        phase = "finalize_aborted_arr_no_file" if is_finalize else "tick_aborted_arr_no_file"
        logger.info(
            f"Import grace aborted ({phase}): Arr no longer reports hasFile "
            f"content_type={content_type} entity_id={entity_id} file_path={file_path!r}",
            extra={"emoji_type": "info"},
        )
        cancelled = cancel_import_grace_for_entity(
            session,
            content_type=content_type,
            entity_id=entity_id,
        )
        restore = restore_request_after_import_grace_interrupt(
            session,
            content_type=content_type,
            entity_id=entity_id,
            placeholder_ids=placeholder_ids,
        )
        return {
            "done": True,
            "ok": True,
            "phase": phase,
            "content_type": content_type,
            "entity_id": entity_id,
            "reason": "arr_no_file",
            "file_path": file_path,
            "cancelled_jobs": cancelled,
            "restore": restore,
        }

    materialization: dict[str, Any] = {}

    if is_finalize:
        if _import_grace_job_was_cancelled(session, job):
            logger.info(
                f"Import grace finalize skipped (job cancelled): content_type={content_type} entity_id={entity_id}",
                extra={"emoji_type": "info"},
            )
            return {
                "done": True,
                "ok": True,
                "phase": "cancelled",
                "content_type": content_type,
                "entity_id": entity_id,
                "reason": "cancelled",
            }
        logger.info(
            f"Import grace finalization starting: content_type={content_type}, entity_id={entity_id}, placeholder_ids={placeholder_ids}",
            extra={'emoji_type': 'info'}
        )
        if content_type == "movie":
            logger.debug(f"Finalize: Looking up movie with id={entity_id}", extra={'emoji_type': 'debug'})
            movie_row = session.query(Movie).filter(Movie.id == entity_id).first()
            if not movie_row:
                logger.warning(f"Finalize: Movie not found with id={entity_id}", extra={'emoji_type': 'warning'})
                return {"done": True, "ok": False, "reason": f"movie_not_found:{entity_id}"}
            logger.debug(f"Finalize: Found movie, setting has_file=True, file_path={file_path}", extra={'emoji_type': 'debug'})
            movie_row.has_file = True
            if file_path:
                movie_row.radarr_filepath = str(file_path)
            session.add(movie_row)
            session.flush()
            logger.debug(f"Finalize: Movie flushed, running determination for movie_id={entity_id}", extra={'emoji_type': 'debug'})
            mat_movie_ids, _mat_episode_ids = expand_determination_entity_ids(
                session,
                movie_ids=[entity_id],
            )
            determination = run_determination_for_entities_in_session(
                session,
                movie_ids=mat_movie_ids,
            )
            logger.debug(f"Finalize: Determination complete for movie_id={entity_id}: {determination}", extra={'emoji_type': 'debug'})
            logger.debug(f"Finalize: Running in-session materialization for movie_id={entity_id}", extra={'emoji_type': 'debug'})
            materialization = run_materialization_for_entities_in_session(
                session,
                movie_ids=mat_movie_ids,
                observation_source="event_movie_imported_grace_finalize",
            )
            logger.debug(f"Finalize: Materialization complete for movie_id={entity_id}: {materialization}", extra={'emoji_type': 'debug'})
            movie_ref = session.query(Movie).filter(Movie.id == entity_id).first()
            mat = materialization_stats_dict(materialization)
            result_reason, status_label = outcome_reason_and_status_from_materialization(
                "Import grace finished (movie)", mat
            )
            result_path = (
                str(getattr(movie_ref, "placeholder_filepath", "") or "").strip()
                if movie_ref
                else ""
            ) or (movie_placeholder_path(movie_ref) if movie_ref else "")
            append_placeholder_activity_status(
                session,
                item_type="movie",
                movie_id=int(entity_id),
                episode_id=None,
                series_id=None,
                season_id=None,
                season_number=None,
                instance_key=getattr(movie_ref, "instance_key", None) if movie_ref else None,
                instance_id=getattr(movie_ref, "instance_id", None) if movie_ref else None,
                event_type="placeholder_event_import_grace_movie_finalize",
                path=str(result_path or ""),
                item_title=str(getattr(movie_ref, "title", "") or "Unknown Movie") if movie_ref else "Unknown Movie",
                series_title=None,
                reason=result_reason,
                status_label=status_label,
                source="import_grace_finalize",
                extra_snapshot={
                    "determination": determination if isinstance(determination, dict) else {},
                    "materialization": mat,
                    "movie_id": int(entity_id),
                    "placeholder_ids": placeholder_ids,
                },
            )
        elif content_type == "episode":
            logger.debug(f"Finalize: Looking up episode with id={entity_id}", extra={'emoji_type': 'debug'})
            episode_row = session.query(Episode).filter(Episode.id == entity_id).first()
            if not episode_row:
                logger.warning(f"Finalize: Episode not found with id={entity_id}", extra={'emoji_type': 'warning'})
                return {"done": True, "ok": False, "reason": f"episode_not_found:{entity_id}"}
            logger.debug(f"Finalize: Found episode, setting has_file=True, file_path={file_path}", extra={'emoji_type': 'debug'})
            episode_row.has_file = True
            if file_path:
                episode_row.sonarr_filepath = str(file_path)
            session.add(episode_row)
            session.flush()
            logger.debug(f"Finalize: Episode flushed, running determination for episode_id={entity_id}", extra={'emoji_type': 'debug'})
            _mat_movie_ids, mat_episode_ids = expand_determination_entity_ids(
                session,
                episode_ids=[entity_id],
            )
            determination = run_determination_for_entities_in_session(
                session,
                episode_ids=mat_episode_ids,
            )
            logger.debug(f"Finalize: Determination complete for episode_id={entity_id}: {determination}", extra={'emoji_type': 'debug'})
            logger.debug(f"Finalize: Running in-session materialization for episode_id={entity_id}", extra={'emoji_type': 'debug'})
            materialization = run_materialization_for_entities_in_session(
                session,
                episode_ids=mat_episode_ids,
                observation_source="event_episode_imported_grace_finalize",
            )
            logger.debug(f"Finalize: Materialization complete for episode_id={entity_id}: {materialization}", extra={'emoji_type': 'debug'})
            ctx = (
                session.query(Episode, Season, Series)
                .join(Season, Episode.season_id == Season.id)
                .join(Series, Season.series_id == Series.id)
                .filter(Episode.id == entity_id)
                .first()
            )
            mat = materialization_stats_dict(materialization)
            result_reason, status_label = outcome_reason_and_status_from_materialization(
                "Import grace finished (episode)", mat
            )
            if ctx:
                ep, season, series = ctx
                sn = int(season.season_number or 0)
                en = int(ep.episode_number or 0)
                ep_label = f"S{sn:02d}E{en:02d} - {ep.title}"
                result_path = str(getattr(ep, "placeholder_filepath", "") or "").strip() or episode_placeholder_path(
                    ep, season, series
                )
                append_placeholder_activity_status(
                    session,
                    item_type="episode",
                    movie_id=None,
                    episode_id=int(entity_id),
                    series_id=int(series.id),
                    season_id=int(season.id),
                    season_number=sn,
                    instance_key=getattr(series, "instance_key", None),
                    instance_id=getattr(series, "instance_id", None),
                    event_type="placeholder_event_import_grace_episode_finalize",
                    path=str(result_path or ""),
                    item_title=ep_label,
                    series_title=str(getattr(series, "title", "") or "") or None,
                    reason=result_reason,
                    status_label=status_label,
                    source="import_grace_finalize",
                    extra_snapshot={
                        "determination": determination if isinstance(determination, dict) else {},
                        "materialization": mat,
                        "episode_id": int(entity_id),
                        "placeholder_ids": placeholder_ids,
                    },
                )
                # Next-unaired may advance after import; refresh series/season New Ep. banners.
                try:
                    from services.source_of_truth.placeholder_art_reconciler import (
                        enqueue_art_refresh_for_calendar_date_changes,
                    )

                    enqueue_art_refresh_for_calendar_date_changes(
                        session,
                        series_ids=[int(series.id)],
                    )
                except Exception as art_exc:
                    logger.debug(
                        f"Import grace New Ep. art enqueue skipped episode_id={entity_id}: {art_exc}",
                        extra={"emoji_type": "debug"},
                        exc_info=True,
                    )
            else:
                append_placeholder_activity_status(
                    session,
                    item_type="episode",
                    movie_id=None,
                    episode_id=int(entity_id),
                    series_id=None,
                    season_id=None,
                    season_number=None,
                    instance_key=None,
                    instance_id=None,
                    event_type="placeholder_event_import_grace_episode_finalize",
                    path="",
                    item_title="Unknown episode",
                    series_title=None,
                    reason=result_reason,
                    status_label=status_label,
                    source="import_grace_finalize",
                    extra_snapshot={
                        "determination": determination if isinstance(determination, dict) else {},
                        "materialization": mat,
                        "episode_id": int(entity_id),
                        "placeholder_ids": placeholder_ids,
                    },
                )
        else:
            logger.warning(f"Finalize: Unsupported content_type={content_type}", extra={'emoji_type': 'warning'})
            return {"done": True, "ok": False, "reason": f"unsupported_content_type:{content_type}"}

        remaining_ids: list[int] = []
        if placeholder_ids:
            logger.debug(f"Finalize: Checking remaining placeholders: {placeholder_ids}", extra={'emoji_type': 'debug'})
            rows = (
                session.query(Placeholder)
                .filter(
                    Placeholder.id.in_(placeholder_ids),
                    Placeholder.has_placeholder == True,  # noqa: E712
                )
                .all()
            )
            remaining_ids = [int(row.id) for row in rows]
            logger.info(f"Finalize: Found {len(remaining_ids)} remaining placeholders to clean up", extra={'emoji_type': 'info'})
            # Status projection removed
            pass

        logger.info(
            f"Import grace finalization complete: {len(remaining_ids)} remaining placeholders queued for projection",
            extra={'emoji_type': 'success'}
        )
        return {
            "done": True,
            "ok": True,
            "phase": "finalize",
            "content_type": content_type,
            "entity_id": entity_id,
            "remaining_placeholders": len(remaining_ids),
            "materialization": materialization,
        }

    status_text = str(payload.get("status_text") or "").strip()
    if not status_text:
        logger.debug(f"Import grace tick: missing status_text at step_index={step_index}", extra={'emoji_type': 'debug'})
        return {"done": True, "ok": False, "reason": "missing_status_text"}

    # Re-check cancel immediately before writing countdown (delete may have raced us).
    if _import_grace_job_was_cancelled(session, job):
        logger.info(
            f"Import grace tick skipped (job cancelled): step_index={step_index} "
            f"content_type={content_type} entity_id={entity_id}",
            extra={"emoji_type": "info"},
        )
        return {
            "done": True,
            "ok": True,
            "phase": "cancelled",
            "content_type": content_type,
            "entity_id": entity_id,
            "reason": "cancelled",
        }

    logger.debug(
        f"Import grace tick: step_index={step_index}, setting status_text='{status_text}' for {len(placeholder_ids)} placeholder(s)",
        extra={'emoji_type': 'debug'}
    )
    active_ids = _set_countdown_status(session, placeholder_ids, status_text)
    _enqueue_nfo_refresh_job(session, active_ids)

    logger.debug(
        f"Import grace tick complete: step_index={step_index}, updated {len(active_ids)} placeholder(s)",
        extra={'emoji_type': 'debug'}
    )
    return {
        "done": True,
        "ok": True,
        "phase": "tick",
        "content_type": content_type,
        "entity_id": entity_id,
        "updated_placeholders": len(active_ids),
    }
