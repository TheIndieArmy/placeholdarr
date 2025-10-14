from typing import List, Dict, Any, Optional
from services.postgres.db import get_session
from services.postgres.models import Job
from sqlalchemy import text, func
from datetime import datetime, timedelta, timezone
from core.logger import logger


def insert_job(job_type: str, payload: dict, group_id: Optional[str] = None, run_after: Optional[datetime] = None) -> int:
    """Insert a job with application-level dedupe by group_id.

    If a pending or claimed job with the same group_id exists, return its id
    instead of inserting a duplicate.
    """
    session = get_session()
    try:
        if group_id:
            # Check for an existing active job with this group_id
            existing = session.query(Job).filter(Job.group_id == group_id, Job.status.in_(['PENDING', 'CLAIMED'])).first()
            if existing:
                return existing.id

        # If no run_after provided, use DB clock (func.now()) so scheduling is based on DB time
        if run_after is None:
            run_after_val = func.now()
        else:
            # Normalize naive datetimes to UTC-aware
            if isinstance(run_after, datetime) and run_after.tzinfo is None:
                run_after_val = run_after.replace(tzinfo=timezone.utc)
            else:
                run_after_val = run_after

        j = Job(job_type=job_type, payload=payload, status='PENDING', run_after=run_after_val, group_id=group_id)
        session.add(j)
        session.commit()
        return j.id
    finally:
        session.close()


def insert_job_with_session(session, job_type: str, payload: dict, group_id: Optional[str] = None, run_after: Optional[datetime] = None) -> int:
    """Insert a job using an existing SQLAlchemy session (does not close it).

    This permits callers to perform the insert within an existing transaction so
    they can update related rows atomically.
    """
    if group_id:
        existing = session.query(Job).filter(Job.group_id == group_id, Job.status.in_(['PENDING', 'CLAIMED'])).first()
        if existing:
            return existing.id

    if run_after is None:
        run_after_val = func.now()
    else:
        if isinstance(run_after, datetime) and run_after.tzinfo is None:
            run_after_val = run_after.replace(tzinfo=timezone.utc)
        else:
            run_after_val = run_after

    j = Job(job_type=job_type, payload=payload, status='PENDING', run_after=run_after_val, group_id=group_id)
    session.add(j)
    # Do not commit here; caller controls transaction/commit
    session.flush()
    return j.id


def claim_jobs(limit: int = 10) -> List[Dict[str, Any]]:
    """Atomically claim up to `limit` jobs and return their rows for processing.

    Uses a single UPDATE ... FROM (CTE) pattern to atomically flip status to 'CLAIMED'
    and return the claimed rows.
    """
    session = get_session()
    try:
        sql = text(f"""
        WITH cte AS (
            SELECT id FROM job
            WHERE status = 'PENDING' AND (run_after IS NULL OR run_after <= now())
            ORDER BY run_after NULLS FIRST, id
            LIMIT :limit
            FOR UPDATE SKIP LOCKED
        )
        UPDATE job
        SET status = 'CLAIMED', updated_at = now()
        FROM cte
        WHERE job.id = cte.id
        RETURNING job.id, job.job_type, job.payload, job.group_id, job.attempts;
        """
        )
        res = session.execute(sql, {'limit': limit}).fetchall()
        # Convert rows to dicts
        claimed = []
        for row in res:
            claimed.append({'id': row[0], 'job_type': row[1], 'payload': row[2], 'group_id': row[3], 'attempts': row[4]})
        session.commit()
        return claimed
    finally:
        session.close()


def requeue_job(job_id: int, delay_seconds: int = 10):
    session = get_session()
    try:
        job = session.query(Job).filter(Job.id == job_id).with_for_update().first()
        if not job:
            return False
        job.attempts = (job.attempts or 0) + 1
        # Use the per-job max_attempts if present, otherwise default to 5
        try:
            allowed = int(job.max_attempts) if getattr(job, 'max_attempts', None) is not None else 5
        except Exception:
            allowed = 5
        if job.attempts >= allowed:
            job.status = 'FAILED'
            job.last_error = f"Exceeded max attempts ({allowed})"
            session.add(job)
            session.commit()
            return

        # Use a timezone-aware UTC datetime so DB receives an aware value
        job.run_after = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        job.status = 'PENDING'
        session.add(job)
        session.commit()
        return True
    finally:
        session.close()


def job_done(job_id: int, success: bool = True, error_message: str = None) -> bool:
    """Mark a job as DONE or FAILED and persist optional error message."""
    session = get_session()
    try:
        j = session.query(Job).filter(Job.id == job_id).first()
        if not j:
            return False
        j.status = 'DONE' if success else 'FAILED'
        if error_message:
            j.error_message = error_message
        # Persist an aware UTC timestamp for updated_at to avoid timezone-ambiguity
        j.updated_at = datetime.now(timezone.utc)
        session.add(j)
        session.commit()
        return True
    finally:
        session.close()
