from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.config import settings
from services.postgres.models import Episode, Job, Movie, Placeholder
from services.source_of_truth.determiner import run_determination_for_entities_in_session
from services.source_of_truth.materializer import run_materialization_for_entities
from services.source_of_truth.materializer import run_materialization_for_entities_in_session
from services.source_of_truth.status_reconciler import STATUS_PROJECTION_JOB_TYPE

IMPORT_GRACE_JOB_TYPE = "import_grace"
IMPORT_GRACE_REASON = "import_grace_countdown"
COUNTDOWN_STATUSES = [
    "NOW IN LIBRARY - RETIRING PLACEHOLDER IN 5 MIN",
    "NOW IN LIBRARY - RETIRING PLACEHOLDER IN 4 MIN",
    "NOW IN LIBRARY - RETIRING PLACEHOLDER IN 3 MIN",
    "NOW IN LIBRARY - RETIRING PLACEHOLDER IN 2 MIN",
    "NOW IN LIBRARY - RETIRING PLACEHOLDER IN 1 MIN",
    "NOW IN LIBRARY - RETIRING PLACEHOLDER IN LESS THAN A MINUTE",
]


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

    scheduled: list[dict[str, Any]] = []
    for step_index, status_text in enumerate(COUNTDOWN_STATUSES):
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
            "step_index": len(COUNTDOWN_STATUSES),
            "run_after": now + timedelta(seconds=len(COUNTDOWN_STATUSES) * step),
            "status_text": None,
            "finalize": True,
        }
    )
    return scheduled


def _enqueue_status_projection_job(session, placeholder_ids: list[int]) -> None:
    if not placeholder_ids:
        return
    session.add(
        Job(
            job_type=STATUS_PROJECTION_JOB_TYPE,
            payload={"placeholder_ids": placeholder_ids},
            status="PENDING",
            run_after=datetime.now(timezone.utc),
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
    _enqueue_status_projection_job(session, active_ids)

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

    if is_finalize:
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
            determination = run_determination_for_entities_in_session(
                session,
                movie_ids=[entity_id],
            )
            logger.debug(f"Finalize: Determination complete for movie_id={entity_id}: {determination}", extra={'emoji_type': 'debug'})
            logger.debug(f"Finalize: Running in-session materialization for movie_id={entity_id}", extra={'emoji_type': 'debug'})
            materialization = run_materialization_for_entities_in_session(
                session,
                movie_ids=[entity_id],
                observation_source="event_movie_imported_grace_finalize",
            )
            logger.debug(f"Finalize: Materialization complete for movie_id={entity_id}: {materialization}", extra={'emoji_type': 'debug'})
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
            determination = run_determination_for_entities_in_session(
                session,
                episode_ids=[entity_id],
            )
            logger.debug(f"Finalize: Determination complete for episode_id={entity_id}: {determination}", extra={'emoji_type': 'debug'})
            logger.debug(f"Finalize: Running in-session materialization for episode_id={entity_id}", extra={'emoji_type': 'debug'})
            materialization = run_materialization_for_entities_in_session(
                session,
                episode_ids=[entity_id],
                observation_source="event_episode_imported_grace_finalize",
            )
            logger.debug(f"Finalize: Materialization complete for episode_id={entity_id}: {materialization}", extra={'emoji_type': 'debug'})
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
            _enqueue_status_projection_job(session, remaining_ids)

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

    logger.debug(
        f"Import grace tick: step_index={step_index}, setting status_text='{status_text}' for {len(placeholder_ids)} placeholder(s)",
        extra={'emoji_type': 'debug'}
    )
    active_ids = _set_countdown_status(session, placeholder_ids, status_text)
    _enqueue_status_projection_job(session, active_ids)

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
