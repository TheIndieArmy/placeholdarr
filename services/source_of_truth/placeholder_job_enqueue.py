"""Shared batched Job enqueue for placeholder-scoped worker tasks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.config import settings
from core.logger import logger

from services.postgres.models import Job


def job_batch_size() -> int:
    return max(1, int(getattr(settings, "STATUS_JOB_BATCH_SIZE", 250) or 250))


def job_debounce_seconds() -> float:
    return max(0.0, float(getattr(settings, "STATUS_JOB_DEBOUNCE_SECONDS", 0.5) or 0.5))


def normalize_placeholder_ids(placeholder_ids: list[int] | tuple[int, ...] | None) -> list[int]:
    seen: set[int] = set()
    normalized: list[int] = []
    for value in placeholder_ids or []:
        if value is None:
            continue
        pid = int(value)
        if pid in seen:
            continue
        seen.add(pid)
        normalized.append(pid)
    return normalized


def job_placeholder_ids(job: Job) -> list[int]:
    payload = job.payload if isinstance(job.payload, dict) else {}
    return normalize_placeholder_ids(payload.get("placeholder_ids") or [])


def set_job_placeholder_ids(job: Job, placeholder_ids: list[int]) -> None:
    payload = dict(job.payload or {}) if isinstance(job.payload, dict) else {}
    payload["placeholder_ids"] = placeholder_ids
    job.payload = payload


def _placeholder_player_merge_flag(placeholder_ids: list[int], by_id: dict[int, bool] | None) -> bool:
    if not by_id:
        return True
    return any(bool(by_id.get(int(pid), True)) for pid in placeholder_ids)


def _job_player_metadata_refresh(job: Job) -> bool:
    payload = job.payload if isinstance(job.payload, dict) else {}
    return bool(payload.get("player_metadata_refresh", True))


def enqueue_batched_placeholder_job(
    job_type: str,
    placeholder_ids: list[int],
    session,
    *,
    player_metadata_refresh_by_id: dict[int, bool] | None = None,
    merge_into_pending: bool = True,
    payload_extras: dict[str, Any] | None = None,
    include_player_metadata_refresh: bool = False,
) -> dict:
    """Enqueue one or more batched jobs for ``placeholder_ids``."""
    ids_remaining = normalize_placeholder_ids(placeholder_ids)
    if not ids_remaining:
        return {"ok": False, "reason": "no_placeholder_ids"}

    batch_size = job_batch_size()
    debounce_seconds = job_debounce_seconds()
    now = datetime.now(timezone.utc)
    run_after = now if debounce_seconds <= 0 else now + timedelta(seconds=debounce_seconds)

    touched_job_ids: list[int] = []
    created_jobs = 0
    updated_jobs = 0

    pending_jobs: list[Job] = []
    if merge_into_pending:
        pending_jobs = (
            session.query(Job)
            .filter(Job.job_type == job_type, Job.status == "PENDING")
            .order_by(Job.run_after.asc().nullsfirst(), Job.id.asc())
            .with_for_update(skip_locked=True)
            .all()
        )

    for job in pending_jobs:
        if not ids_remaining:
            break
        existing_ids = job_placeholder_ids(job)
        if len(existing_ids) >= batch_size:
            continue

        additions: list[int] = []
        existing_set = set(existing_ids)
        capacity = batch_size - len(existing_ids)
        for pid in ids_remaining:
            if pid in existing_set:
                continue
            additions.append(pid)
            existing_set.add(pid)
            if len(additions) >= capacity:
                break

        if not additions:
            continue

        set_job_placeholder_ids(job, existing_ids + additions)
        payload = dict(job.payload or {}) if isinstance(job.payload, dict) else {}
        if include_player_metadata_refresh:
            merged_flag = _job_player_metadata_refresh(job) or _placeholder_player_merge_flag(
                additions, player_metadata_refresh_by_id
            )
            payload["player_metadata_refresh"] = merged_flag
        if payload_extras:
            payload.update(payload_extras)
        job.payload = payload
        job.updated_at = now
        session.add(job)
        touched_job_ids.append(int(job.id))
        updated_jobs += 1
        added_set = set(additions)
        ids_remaining = [pid for pid in ids_remaining if pid not in added_set]

    from services.source_of_truth.job_priority import default_priority_for

    job_priority = default_priority_for(job_type)
    while ids_remaining:
        chunk = ids_remaining[:batch_size]
        ids_remaining = ids_remaining[batch_size:]
        payload: dict[str, Any] = {"placeholder_ids": chunk, **(payload_extras or {})}
        if include_player_metadata_refresh:
            payload["player_metadata_refresh"] = _placeholder_player_merge_flag(
                chunk, player_metadata_refresh_by_id
            )
        job = Job(
            job_type=job_type,
            payload=payload,
            status="PENDING",
            run_after=run_after,
            priority=job_priority,
        )
        session.add(job)
        session.flush()
        touched_job_ids.append(int(job.id))
        created_jobs += 1

    session.commit()

    logger.debug(
        f"Queued {job_type} placeholders={len(normalize_placeholder_ids(placeholder_ids))} "
        f"jobs_touched={len(touched_job_ids)} jobs_created={created_jobs} jobs_updated={updated_jobs} "
        f"batch_size={batch_size} debounce_seconds={debounce_seconds:.3f}",
        extra={"emoji_type": "processing"},
    )

    return {
        "ok": True,
        "job_id": touched_job_ids[0] if touched_job_ids else None,
        "job_ids": touched_job_ids,
        "jobs_created": created_jobs,
        "jobs_updated": updated_jobs,
        "placeholder_count": len(normalize_placeholder_ids(placeholder_ids)),
    }
