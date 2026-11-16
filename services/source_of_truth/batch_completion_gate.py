"""Serialize multi-batch art/NFO completion (one library refresh per run_id)."""

from __future__ import annotations

import zlib

from sqlalchemy import text

from core.logger import logger
from services.postgres.models import Job, ScheduledTaskRun
from services.source_of_truth.idempotency import (
    backfill_run_completion_idempotency_key,
    release_processed_key,
    try_claim_processed_key,
)

_BACKFILL_COMPLETION_LOCK_BASE = 8_729_0300


def _advisory_lock_key(*, scope: str, run_id: str) -> int:
    h = zlib.crc32(f"{scope}\0{run_id}".encode("utf-8")) & 0x7FFFFFFF
    return _BACKFILL_COMPLETION_LOCK_BASE + (h % 900_000)


def _try_advisory_lock(session, *, scope: str, run_id: str) -> bool:
    try:
        key = _advisory_lock_key(scope=scope, run_id=run_id)
        return bool(
            session.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
        )
    except Exception as exc:
        logger.debug(
            f"batch_completion_gate: advisory lock failed scope={scope} run_id={run_id!r}: {exc}",
            extra={"emoji_type": "debug"},
        )
        return False


def _advisory_unlock(session, *, scope: str, run_id: str) -> None:
    try:
        key = _advisory_lock_key(scope=scope, run_id=run_id)
        session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
    except Exception as exc:
        logger.debug(
            f"batch_completion_gate: advisory unlock failed scope={scope} run_id={run_id!r}: {exc}",
            extra={"emoji_type": "debug"},
        )


def _backfill_counters_ready(
    session,
    *,
    task_run_id: int | None,
    summary_meta_key: str | None,
    run_id: str,
) -> bool:
    if task_run_id is None or not summary_meta_key:
        return True
    try:
        row = (
            session.query(ScheduledTaskRun)
            .filter(ScheduledTaskRun.id == int(task_run_id))
            .first()
        )
        if not row or not isinstance(row.summary, dict):
            return True
        meta = row.summary.get(summary_meta_key)
        if not isinstance(meta, dict):
            return True
        if str(meta.get("run_id") or "").strip() != str(run_id).strip():
            return True
        batch_count = int(meta.get("batch_count") or 0)
        batches_done = int(meta.get("batches_done") or 0)
        batches_failed = int(meta.get("batches_failed") or 0)
        if batch_count <= 0:
            return True
        return (batches_done + batches_failed) >= batch_count
    except Exception:
        return True


def _unstarted_sibling_batches(
    session,
    job: Job,
    *,
    run_id: str,
    job_type: str,
    payload_run_id_key: str,
) -> bool:
    """True when another batch for this run_id is still queued (PENDING only)."""
    job_id = int(job.id)
    pending = (
        session.query(Job.id)
        .filter(
            Job.job_type == job_type,
            Job.status == "PENDING",
            Job.id != job_id,
            Job.payload[payload_run_id_key].as_string() == run_id,
        )
        .first()
    )
    return bool(pending)


def should_finalize_backfill_run(
    session,
    job: Job,
    *,
    run_id: str,
    job_type: str,
    payload_run_id_key: str,
    task_run_id: int | None = None,
    summary_meta_key: str | None = None,
) -> bool:
    """True when batch counters are satisfied and no unstarted sibling batches remain."""
    if not run_id:
        return False
    scope = str(job_type)
    if not _try_advisory_lock(session, scope=scope, run_id=run_id):
        return False
    try:
        if not _backfill_counters_ready(
            session,
            task_run_id=task_run_id,
            summary_meta_key=summary_meta_key,
            run_id=run_id,
        ):
            return False
        if _unstarted_sibling_batches(
            session,
            job,
            run_id=run_id,
            job_type=job_type,
            payload_run_id_key=payload_run_id_key,
        ):
            return False
        return True
    finally:
        _advisory_unlock(session, scope=scope, run_id=run_id)


def try_claim_backfill_library_completion(
    session,
    job: Job,
    *,
    run_id: str,
    job_type: str,
    payload_run_id_key: str,
    task_run_id: int | None = None,
    summary_meta_key: str | None = None,
) -> bool:
    """Gate + idempotency claim so exactly one handler fires the library completion scan."""
    if not run_id:
        return False
    scope = str(job_type)
    if not _try_advisory_lock(session, scope=scope, run_id=run_id):
        return False
    completion_key: str | None = None
    try:
        if not _backfill_counters_ready(
            session,
            task_run_id=task_run_id,
            summary_meta_key=summary_meta_key,
            run_id=run_id,
        ):
            return False
        if _unstarted_sibling_batches(
            session,
            job,
            run_id=run_id,
            job_type=job_type,
            payload_run_id_key=payload_run_id_key,
        ):
            return False
        completion_key = backfill_run_completion_idempotency_key(
            job_type=job_type, run_id=run_id
        )
        if not try_claim_processed_key(session, completion_key, job_type=job_type):
            return False
        try:
            session.commit()
        except Exception:
            session.rollback()
            if completion_key:
                release_processed_key(session, completion_key)
            return False
        return True
    finally:
        _advisory_unlock(session, scope=scope, run_id=run_id)


__all__ = [
    "should_finalize_backfill_run",
    "try_claim_backfill_library_completion",
]
