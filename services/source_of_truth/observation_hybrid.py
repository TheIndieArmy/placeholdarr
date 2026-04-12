from __future__ import annotations

import hashlib
import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func

from core.config import settings
from core.logger import logger
from services.postgres.models import Job, Placeholder
from services.source_of_truth.media_observation import (
    observe_placeholders_with_polling,
)
from services.source_of_truth.observation_selection import (
    rank_placeholder_ids_for_observation,
    select_placeholder_ids_for_hybrid_refill,
)
from services.source_of_truth.observation_trail import enqueue_observation_trail, unresolved_placeholder_ids


HYBRID_SLICE_JOB_TYPE = "placeholder_observation_hybrid_slice"

_DEFER_TO_TRAIL_STOP_REASONS = {
    "no_progress_max_polls",
    "unresolved_after_observer",
}


def _safe_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _enabled() -> bool:
    return bool(getattr(settings, "HYBRID_OBSERVATION_SLICES_ENABLED", False))


def _initial_delay_seconds() -> int:
    return max(0, _as_int(getattr(settings, "HYBRID_OBSERVATION_INITIAL_DELAY_SECONDS", 15), 15))


def _cadence_seconds() -> int:
    return max(5, _as_int(getattr(settings, "HYBRID_OBSERVATION_CADENCE_SECONDS", 120), 120))


def _max_attempts() -> int:
    return max(1, _as_int(getattr(settings, "HYBRID_OBSERVATION_MAX_ATTEMPTS", 4), 4))


def _min_unresolved() -> int:
    return max(1, _as_int(getattr(settings, "HYBRID_OBSERVATION_MIN_UNRESOLVED", 1), 1))


def _target_slice_size() -> int:
    return max(1, _as_int(getattr(settings, "HYBRID_OBSERVATION_TARGET_SLICE_SIZE", 400), 400))


def _low_watermark() -> int:
    target = _target_slice_size()
    configured = max(1, _as_int(getattr(settings, "HYBRID_OBSERVATION_LOW_WATERMARK", 120), 120))
    return min(target, configured)


def _sort_ids_oldest_first(session, placeholder_ids: list[int]) -> list[int]:
    return rank_placeholder_ids_for_observation(session, [int(x) for x in placeholder_ids if x is not None])


def _mid_pass_refill_enabled() -> bool:
    return bool(getattr(settings, "HYBRID_OBSERVATION_MID_PASS_REFILL_ENABLED", True))


def _single_flight_retry_delay_seconds(candidate_count: int, busy_count: int) -> int:
    base_delay = max(5, _as_int(getattr(settings, "HYBRID_OBSERVATION_SINGLE_FLIGHT_RETRY_BASE_SECONDS", 30), 30))
    max_delay = max(base_delay, _as_int(getattr(settings, "HYBRID_OBSERVATION_SINGLE_FLIGHT_RETRY_MAX_SECONDS", 180), 180))
    cohort_multiplier = 1
    if candidate_count >= 500:
        cohort_multiplier = 4
    elif candidate_count >= 250:
        cohort_multiplier = 3
    elif candidate_count >= 100:
        cohort_multiplier = 2
    retry_multiplier = min(4, max(1, int(busy_count or 1)))
    return min(max_delay, base_delay * max(cohort_multiplier, retry_multiplier))


def _ordered_rows_by_ids(rows: list[Placeholder], ordered_ids: list[int]) -> list[Placeholder]:
    row_by_id = {int(getattr(row, "id")): row for row in rows if getattr(row, "id", None) is not None}
    ordered = [row_by_id[row_id] for row_id in ordered_ids if row_id in row_by_id]
    return ordered or rows


def _refill_low_water_slice_ids(session, active_ids: list[int]) -> tuple[list[int], list[int]]:
    current = [int(x) for x in active_ids if x is not None]
    if not current:
        return [], []

    target = _target_slice_size()
    low_water = _low_watermark()
    if len(current) >= low_water or len(current) >= target:
        return current, []

    needed = max(0, target - len(current))
    extras = select_placeholder_ids_for_hybrid_refill(
        session,
        exclude_ids=set(current),
        limit=needed,
    )

    merged = current[:]
    seen = set(merged)
    for placeholder_id in extras:
        if placeholder_id in seen:
            continue
        seen.add(placeholder_id)
        merged.append(placeholder_id)

    added_ids = [placeholder_id for placeholder_id in merged if placeholder_id not in set(current)]
    return merged, added_ids


def _build_group_id(source: str) -> str:
    token = str(source or "unknown").strip().lower()
    digest = hashlib.sha1(token.encode("utf-8")).hexdigest()[:16]
    return f"obs_hybrid:{token}:{digest}"


def enqueue_hybrid_observation_slice(
    session,
    *,
    placeholder_ids: list[int],
    source: str,
    trigger_reason: str,
    delay_seconds: int | None = None,
) -> dict[str, Any]:
    if not _enabled():
        return {"enqueued": False, "reason": "hybrid_slices_disabled"}

    ids = rank_placeholder_ids_for_observation(
        session,
        [int(x) for x in placeholder_ids if x is not None],
    )
    if not ids:
        return {"enqueued": False, "reason": "no_placeholder_ids"}

    group_id = _build_group_id(source)
    effective_delay = _initial_delay_seconds() if delay_seconds is None else max(0, int(delay_seconds))
    run_after = _safe_now() + timedelta(seconds=effective_delay)

    existing = (
        session.query(Job)
        .filter(
            Job.group_id == group_id,
            Job.job_type == HYBRID_SLICE_JOB_TYPE,
            Job.status.in_(["PENDING", "CLAIMED", "WORKING"]),
        )
        .first()
    )
    if existing:
        existing_payload = dict(existing.payload or {})
        current_ids = [int(x) for x in (existing_payload.get("placeholder_ids") or []) if x is not None]
        incoming_ids = [int(x) for x in ids if x is not None]
        merged_ids = rank_placeholder_ids_for_observation(session, current_ids + incoming_ids)
        reasons = list(existing_payload.get("trigger_reasons") or [])
        if trigger_reason not in reasons:
            reasons.append(str(trigger_reason))

        existing_payload["placeholder_ids"] = merged_ids
        existing_payload["total_candidates"] = len(merged_ids)
        existing_payload["trigger_reasons"] = reasons
        existing_payload["coalesced_count"] = int(existing_payload.get("coalesced_count") or 0) + 1
        existing_payload["last_coalesced_at"] = _safe_now().isoformat()

        if existing.status == "PENDING" and existing.run_after and existing.run_after > run_after:
            existing.run_after = run_after

        existing.payload = existing_payload
        existing.updated_at = func.now()
        session.add(existing)
        return {
            "enqueued": True,
            "job_id": int(existing.id),
            "group_id": group_id,
            "coalesced": True,
            "candidate_count": len(merged_ids),
        }

    payload = {
        "source": str(source),
        "trigger_reasons": [str(trigger_reason)],
        "placeholder_ids": ids,
        "total_candidates": len(ids),
        "attempt": 0,
        "coalesced_count": 0,
        "queued_at": _safe_now().isoformat(),
        "host": os.getenv("HOSTNAME") or "unknown",
    }
    job = Job(
        job_type=HYBRID_SLICE_JOB_TYPE,
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
    return {
        "enqueued": True,
        "job_id": int(job.id),
        "group_id": group_id,
        "coalesced": False,
        "candidate_count": len(ids),
    }


def process_hybrid_observation_slice_job(session, job: Job) -> dict[str, Any]:
    payload = dict(job.payload or {})
    source = str(payload.get("source") or "unknown")
    attempt = max(1, _as_int(payload.get("attempt"), 0) + 1)
    placeholder_ids = [int(x) for x in (payload.get("placeholder_ids") or []) if x is not None]

    if not placeholder_ids:
        payload["attempt"] = attempt
        payload["last_reason"] = "no_candidates"
        job.payload = payload
        job.updated_at = func.now()
        session.add(job)
        return {"done": True, "reason": "no_candidates", "attempt": attempt}

    rows = (
        session.query(Placeholder)
        .filter(Placeholder.id.in_(placeholder_ids))
        .all()
    )
    ordered_rows = _ordered_rows_by_ids(rows, placeholder_ids)
    candidates = [row for row in ordered_rows if bool(getattr(row, "has_placeholder", False))]
    if not candidates:
        payload["attempt"] = attempt
        payload["last_reason"] = "no_active_placeholders"
        job.payload = payload
        job.updated_at = func.now()
        session.add(job)
        return {"done": True, "reason": "no_active_placeholders", "attempt": attempt}

    def _mid_pass_refill_callback(*, active_placeholder_ids: list[int], **_kwargs) -> list[int]:
        if not _mid_pass_refill_enabled():
            return []
        _, added_ids = _refill_low_water_slice_ids(session, active_placeholder_ids)
        return added_ids

    started = time.monotonic()
    observe_stats = observe_placeholders_with_polling(
        session,
        candidates,
        allow_title_fallback=True,
        mid_pass_refill_callback=_mid_pass_refill_callback,
        auto_enqueue_trail_on_defer=False,
    )

    refreshed_rows = (
        session.query(Placeholder)
        .filter(Placeholder.id.in_(placeholder_ids))
        .all()
    )
    unresolved_ids = unresolved_placeholder_ids(refreshed_rows)
    unresolved_count = len(unresolved_ids)
    stop_reason = str(observe_stats.get("stop_reason") or "unknown")

    refill_ids, refill_added_ids = _refill_low_water_slice_ids(session, unresolved_ids)
    refill_added = len(refill_added_ids)
    if refill_added > 0:
        payload["refill_added"] = int(payload.get("refill_added", 0) or 0) + int(refill_added)
        payload["refill_last_added"] = int(refill_added)
        payload["refill_last_total"] = len(refill_ids)
        logger.info(
            "Hybrid low-water refill expanded observation candidate set "
            f"job_id={job.id} added={refill_added} total={len(refill_ids)}",
            extra={"emoji_type": "info"},
        )

    payload["attempt"] = attempt
    payload["last_reason"] = stop_reason
    payload["last_duration_ms"] = int(math.ceil((time.monotonic() - started) * 1000.0))
    payload["last_observe_stats"] = {
        "observed_plex": int(observe_stats.get("observed_plex", 0) or 0),
        "observed_jellyfin": int(observe_stats.get("observed_jellyfin", 0) or 0),
        "observed_emby": int(observe_stats.get("observed_emby", 0) or 0),
        "observe_failed": int(observe_stats.get("observe_failed", 0) or 0),
        "mid_pass_refill_events": int(observe_stats.get("mid_pass_refill_events", 0) or 0),
        "mid_pass_refill_added": int(observe_stats.get("mid_pass_refill_added", 0) or 0),
    }
    payload["last_unresolved_ids"] = unresolved_ids

    if stop_reason == "single_flight_busy":
        busy_count = int(payload.get("single_flight_busy_count", 0) or 0) + 1
        retry_delay = _single_flight_retry_delay_seconds(len(placeholder_ids), busy_count)
        payload["single_flight_busy_count"] = busy_count
        payload["single_flight_retry_after_seconds"] = retry_delay
        job.payload = payload
        job.status = "PENDING"
        job.run_after = _safe_now() + timedelta(seconds=retry_delay)
        job.updated_at = func.now()
        session.add(job)
        logger.info(
            "Hybrid observation slice deferred by single-flight lock "
            f"job_id={job.id} unresolved={unresolved_count} retry_after={retry_delay}s busy_count={busy_count}",
            extra={"emoji_type": "info"},
        )
        return {"done": False, "reason": stop_reason, "attempt": attempt, "unresolved": unresolved_count}

    payload["single_flight_busy_count"] = 0

    # Refill must be honored before any stop/defer branch so slices do not drain
    # below low-water and terminate while additional unresolved candidates exist.
    if refill_added > 0 and unresolved_count > 0 and attempt < _max_attempts():
        payload["placeholder_ids"] = refill_ids
        payload["total_candidates"] = len(refill_ids)
        payload["next_run_after"] = (_safe_now() + timedelta(seconds=_cadence_seconds())).isoformat()
        job.payload = payload
        job.status = "PENDING"
        job.run_after = _safe_now() + timedelta(seconds=_cadence_seconds())
        job.updated_at = func.now()
        session.add(job)
        logger.info(
            "Hybrid observation slice continuing after low-water refill "
            f"job_id={job.id} attempt={attempt} refill_added={refill_added} next_total={len(refill_ids)}",
            extra={"emoji_type": "info"},
        )
        return {"done": False, "reason": "refilled_continue", "attempt": attempt, "unresolved": unresolved_count}

    if stop_reason == "idle_no_progress_deferred":
        job.payload = payload
        job.updated_at = func.now()
        session.add(job)
        return {"done": True, "reason": stop_reason, "attempt": attempt, "unresolved": unresolved_count}

    if stop_reason in _DEFER_TO_TRAIL_STOP_REASONS:
        if unresolved_count > 0:
            trail_ids = refill_ids if refill_added > 0 else unresolved_ids
            trail_result = enqueue_observation_trail(
                session,
                placeholder_ids=trail_ids,
                source=f"hybrid_slice:{source}",
            )
            payload["trail_enqueued"] = bool(trail_result.get("enqueued"))
            payload["trail_job_id"] = trail_result.get("job_id")
        job.payload = payload
        job.updated_at = func.now()
        session.add(job)
        return {"done": True, "reason": stop_reason, "attempt": attempt, "unresolved": unresolved_count}

    if unresolved_count >= _min_unresolved() and attempt < _max_attempts():
        next_ids = refill_ids if refill_ids else unresolved_ids
        payload["placeholder_ids"] = next_ids
        payload["total_candidates"] = len(next_ids)
        payload["next_run_after"] = (_safe_now() + timedelta(seconds=_cadence_seconds())).isoformat()
        job.payload = payload
        job.status = "PENDING"
        job.run_after = _safe_now() + timedelta(seconds=_cadence_seconds())
        job.updated_at = func.now()
        session.add(job)
        logger.info(
            "Hybrid observation slice scheduled successor pass "
            f"job_id={job.id} attempt={attempt} unresolved={unresolved_count}",
            extra={"emoji_type": "info"},
        )
        return {"done": False, "reason": "scheduled_successor", "attempt": attempt, "unresolved": unresolved_count}

    if unresolved_count > 0:
        trail_result = enqueue_observation_trail(
            session,
            placeholder_ids=unresolved_ids,
            source=f"hybrid_slice:{source}",
        )
        payload["trail_enqueued"] = bool(trail_result.get("enqueued"))
        payload["trail_job_id"] = trail_result.get("job_id")

    job.payload = payload
    job.updated_at = func.now()
    session.add(job)
    logger.info(
        "Hybrid observation slice completed "
        f"job_id={job.id} attempt={attempt} unresolved={unresolved_count} reason={stop_reason}",
        extra={"emoji_type": "info" if unresolved_count else "success"},
    )
    return {"done": True, "reason": stop_reason, "attempt": attempt, "unresolved": unresolved_count}
