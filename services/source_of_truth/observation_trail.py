from __future__ import annotations

import hashlib
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func

from core.config import settings
from core.logger import logger
from services.postgres.models import Job, ObservationTrailAttempt, Placeholder
from services.source_of_truth.media_observation import observe_placeholders_with_polling
from services.source_of_truth.observation_selection import rank_placeholder_ids_for_observation


TRAIL_JOB_TYPE = "placeholder_observation_trail"

TRAIL_FIRST_DELAY_SECONDS = 300


def _safe_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _trail_max_attempts() -> int:
    return max(1, _as_int(getattr(settings, "OBSERVATION_TRAIL_MAX_ATTEMPTS", 6), 6))


def _stubborn_demotion_after_attempts() -> int:
    return max(1, _as_int(getattr(settings, "OBSERVATION_TRAIL_STUBBORN_DEMOTION_AFTER_ATTEMPTS", 4), 4))


def _stubborn_delay_seconds() -> int:
    return max(60, _as_int(getattr(settings, "OBSERVATION_TRAIL_STUBBORN_DELAY_SECONDS", 900), 900))


def _single_flight_retry_after_seconds(candidate_count: int, attempt_number: int) -> int:
    base = 30
    if candidate_count >= 500:
        base = 120
    elif candidate_count >= 250:
        base = 90
    elif candidate_count >= 100:
        base = 60
    return min(_stubborn_delay_seconds(), base + max(0, attempt_number - 1) * 15)


def _service_enabled(service: str) -> bool:
    if service == "plex":
        return bool(getattr(settings, "ENABLE_PLEX", False))
    if service == "jellyfin":
        return bool(getattr(settings, "ENABLE_JELLYFIN", False))
    if service == "emby":
        return bool(getattr(settings, "ENABLE_EMBY", False))
    return False


def _is_resolved_for_enabled_services(placeholder: Placeholder) -> bool:
    # Deferred observation is intentionally Plex-only for now.
    if not _service_enabled("plex"):
        return True
    return bool(getattr(placeholder, "plex_placeholder_id", None))


def unresolved_placeholder_ids(placeholders: list[Placeholder]) -> list[int]:
    unresolved: list[int] = []
    for row in placeholders:
        if not getattr(row, "id", None):
            continue
        if not getattr(row, "has_placeholder", False):
            continue
        if _is_resolved_for_enabled_services(row):
            continue
        unresolved.append(int(row.id))
    return unresolved


def _build_trail_group_id(source: str, placeholder_ids: list[int]) -> str:
    sorted_ids = sorted(set(int(x) for x in placeholder_ids))
    digest = hashlib.sha1(
        (f"{source}:" + ",".join(str(x) for x in sorted_ids)).encode("utf-8")
    ).hexdigest()[:16]
    return f"obs_trail:{source}:{digest}"


def _insert_trail_job_with_session(
    session,
    *,
    job_type: str = TRAIL_JOB_TYPE,
    payload: dict[str, Any],
    group_id: str,
    run_after: datetime,
) -> int:
    existing = (
        session.query(Job)
        .filter(
            Job.group_id == group_id,
            Job.job_type == job_type,
            Job.status.in_(["PENDING", "CLAIMED", "WORKING"]),
        )
        .first()
    )
    if existing:
        return int(existing.id)

    job = Job(
        job_type=job_type,
        payload=payload,
        status="PENDING",
        run_after=run_after,
        group_id=group_id,
        created_at=func.now(),
    )
    session.add(job)
    session.flush()
    try:
        session.refresh(job)
    except Exception:
        pass
    return int(job.id)
def enqueue_observation_trail(
    session,
    *,
    placeholder_ids: list[int],
    source: str,
    run_id: str | None = None,
    delay_seconds: int | None = None,
) -> dict[str, Any]:
    """Enqueue one delayed follow-up observation pass for unresolved placeholders."""
    if not _service_enabled("plex"):
        return {"enqueued": False, "reason": "plex_disabled"}

    ids = rank_placeholder_ids_for_observation(
        session,
        [int(x) for x in placeholder_ids if x is not None],
    )
    if not ids:
        return {"enqueued": False, "reason": "no_placeholder_ids"}

    effective_delay = delay_seconds if delay_seconds is not None else TRAIL_FIRST_DELAY_SECONDS
    group_id = _build_trail_group_id(source, ids)
    run_after = _safe_now() + timedelta(seconds=effective_delay)

    payload = {
        "source": source,
        "run_id": run_id,
        "placeholder_ids": ids,
        "total_candidates": len(ids),
        "attempt": 0,
        "max_attempts": _trail_max_attempts(),
    }

    job_id = _insert_trail_job_with_session(
        session,
        payload=payload,
        group_id=group_id,
        run_after=run_after,
    )

    return {
        "enqueued": True,
        "job_id": int(job_id),
        "group_id": group_id,
        "first_delay_seconds": effective_delay,
    }


def process_observation_trail_job(session, job: Job) -> dict[str, Any]:
    """Execute one delayed follow-up observation pass and complete the trail job."""
    payload = dict(job.payload or {})
    source = str(payload.get("source") or "unknown")

    placeholder_ids = [int(x) for x in (payload.get("placeholder_ids") or []) if x is not None]
    total_candidates = max(0, _as_int(payload.get("total_candidates"), len(placeholder_ids)))
    attempt_number = max(1, _as_int(payload.get("attempt"), 0) + 1)
    max_attempts = max(1, _as_int(payload.get("max_attempts"), _trail_max_attempts()))

    rows = (
        session.query(Placeholder)
        .filter(Placeholder.id.in_(placeholder_ids))
        .all()
        if placeholder_ids
        else []
    )

    start = time.monotonic()
    candidates = [p for p in rows if bool(getattr(p, "has_placeholder", False)) and not _is_resolved_for_enabled_services(p)]
    before_count = len(candidates)


    observe_stats = {
        "observed_plex": 0,
        "observed_jellyfin": 0,
        "observed_emby": 0,
        "observe_failed": before_count,
    }
    error_message = None

    try:
        if candidates:
            observe_stats = observe_placeholders_with_polling(
                session,
                candidates,
                allow_title_fallback=True,
                auto_enqueue_trail_on_defer=False,
            )
    except Exception as e:
        error_message = str(e)
        logger.warning(
            f"Observation trail attempt failed job_id={job.id} attempt={attempt_number}: {e}",
            extra={"emoji_type": "warning"},
        )

    # Always recompute unresolved from DB row state after observation attempt.
    refreshed_rows = (
        session.query(Placeholder)
        .filter(Placeholder.id.in_(placeholder_ids))
        .all()
        if placeholder_ids
        else []
    )
    unresolved_ids = unresolved_placeholder_ids(refreshed_rows)
    after_count = len(unresolved_ids)
    resolved_reason = "resolved_all" if after_count <= 0 else str(observe_stats.get("stop_reason") or "unresolved_after_observer")

    logger.debug(
        "Observation trail decision snapshot "
        f"job_id={job.id} attempt={attempt_number}/{max_attempts} source={source} "
        f"before={before_count} after={after_count} reason={resolved_reason}",
        extra={"emoji_type": "debug"},
    )

    # If another observation run is active, reschedule this trail job shortly
    # instead of marking it done so deferred follow-up guarantees are preserved.
    if resolved_reason == "single_flight_busy":
        retry_after_seconds = _single_flight_retry_after_seconds(after_count or before_count, attempt_number)
        payload["attempt"] = max(0, attempt_number - 1)
        payload["last_resolution_reason"] = resolved_reason
        payload["last_unresolved_ids"] = unresolved_ids
        payload["single_flight_retry_after_seconds"] = retry_after_seconds
        job.payload = payload
        job.status = "PENDING"
        job.run_after = _safe_now() + timedelta(seconds=retry_after_seconds)
        job.error_message = None
        job.updated_at = func.now()
        session.add(job)
        logger.info(
            "Observation trail deferred by single-flight lock: "
            f"job_id={job.id} retry_after={retry_after_seconds}s unresolved={after_count}",
            extra={"emoji_type": "info"},
        )
        return {
            "done": False,
            "attempt": attempt_number,
            "unresolved": after_count,
            "reason": resolved_reason,
        }

    if after_count > 0 and resolved_reason in {"idle_no_progress_deferred", "no_progress_max_polls"}:
        if attempt_number < max_attempts:
            demoted = attempt_number >= _stubborn_demotion_after_attempts()
            retry_after_seconds = _stubborn_delay_seconds() if demoted else (120 if resolved_reason == "idle_no_progress_deferred" else 90)
            payload["attempt"] = attempt_number
            payload["last_resolution_reason"] = resolved_reason
            payload["last_unresolved_ids"] = unresolved_ids
            payload["next_retry_after_seconds"] = retry_after_seconds
            payload["stubborn_tail_demoted"] = demoted
            job.payload = payload
            job.status = "PENDING"
            job.run_after = _safe_now() + timedelta(seconds=retry_after_seconds)
            job.error_message = None
            job.updated_at = func.now()
            session.add(job)
            logger.info(
                "Observation trail continuation scheduled "
                f"job_id={job.id} reason={resolved_reason} attempt={attempt_number}/{max_attempts} "
                f"retry_after={retry_after_seconds}s unresolved={after_count} demoted={demoted}",
                extra={"emoji_type": "info"},
            )
            return {
                "done": False,
                "attempt": attempt_number,
                "unresolved": after_count,
                "reason": f"retry:{resolved_reason}",
            }

        logger.info(
            "Observation trail reached max retries for unresolved placeholders "
            f"job_id={job.id} attempt={attempt_number}/{max_attempts} unresolved={after_count} reason={resolved_reason}",
            extra={"emoji_type": "warning"},
        )

    elapsed_ms = int(math.ceil((time.monotonic() - start) * 1000.0))

    session.add(
        ObservationTrailAttempt(
            trail_job_id=int(job.id),
            source=source,
            attempt_number=attempt_number,
            placeholders_before=before_count,
            placeholders_after=after_count,
            observed_plex=int(observe_stats.get("observed_plex", 0) or 0),
            observed_jellyfin=int(observe_stats.get("observed_jellyfin", 0) or 0),
            observed_emby=int(observe_stats.get("observed_emby", 0) or 0),
            observe_failed=int(observe_stats.get("observe_failed", 0) or 0),
            max_attempts=max_attempts,
            elapsed_ms=elapsed_ms,
            resolution_reason=resolved_reason,
            unresolved_placeholder_ids=unresolved_ids,
            error_message=error_message,
        )
    )

    payload["attempt"] = attempt_number
    payload["last_unresolved_ids"] = unresolved_ids
    payload["last_observe_stats"] = {
        "observed_plex": int(observe_stats.get("observed_plex", 0) or 0),
        "observed_jellyfin": int(observe_stats.get("observed_jellyfin", 0) or 0),
        "observed_emby": int(observe_stats.get("observed_emby", 0) or 0),
        "observe_failed": int(observe_stats.get("observe_failed", 0) or 0),
    }
    payload["last_resolution_reason"] = resolved_reason

    job.payload = payload
    job.error_message = error_message
    job.updated_at = func.now()
    session.add(job)

    logger.info(
        "Observation follow-up complete: "
        f"{total_candidates - after_count}/{total_candidates} ready, "
        f"{after_count} still waiting "
        f"(attempt {attempt_number}, reason={resolved_reason}).",
        extra={"emoji_type": "info" if after_count else "success"},
    )
    return {
        "done": True,
        "attempt": attempt_number,
        "unresolved": after_count,
        "reason": resolved_reason,
    }

