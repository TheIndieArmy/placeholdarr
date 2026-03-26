import time
from datetime import datetime, timezone

from sqlalchemy import and_

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import EventLog, Job
from services.source_of_truth.event_add import process_movie_add_event, process_series_add_event
from services.source_of_truth.observation_trail import (
    TRAIL_JOB_TYPE,
    process_observation_trail_job,
)


def _claim_next_job(session):
    now = datetime.now(timezone.utc)
    job = (
        session.query(Job)
        .filter(
            and_(
                Job.status == 'PENDING',
                (Job.run_after.is_(None) | (Job.run_after <= now)),
            )
        )
        .order_by(Job.id.asc())
        .with_for_update(skip_locked=True)
        .first()
    )
    if not job:
        return None
    job.status = 'CLAIMED'
    job.attempts = int(job.attempts or 0) + 1
    job.updated_at = now
    session.add(job)
    session.commit()
    return job


def _process_webhook_event(session, job: Job):
    payload = job.payload or {}
    event_log_id = payload.get('event_log_id')
    if not event_log_id:
        raise ValueError('missing_event_log_id')

    event = session.query(EventLog).filter(EventLog.id == int(event_log_id)).first()
    if not event:
        # Event row missing: mark job done to avoid poison-looping.
        return

    event_type = str(getattr(event, 'event_type', '') or '').strip().lower()
    payload = event.payload if isinstance(event.payload, dict) else {}

    # Extract instance identifier from source string (e.g. "webhook:radarr_std" -> "radarr_std")
    source_str = getattr(event, 'source', '') or ''
    instance = None
    if source_str.startswith('webhook:'):
        instance = source_str.split(':', 1)[1].strip() or None

    if event_type == 'seriesadd':
        result = process_series_add_event(payload, instance=instance)
        logger.info(
            f"Processed seriesadd event_log_id={event.id} result={result.get('upsert_stats', {})}",
            extra={'emoji_type': 'success'},
        )
    elif event_type in ('movieadd', 'movieadded'):
        result = process_movie_add_event(payload, instance=instance)
        logger.info(
            f"Processed {event_type} event_log_id={event.id} movie_id={result.get('movie_id')}",
            extra={'emoji_type': 'success'},
        )

    event.status = 'DONE'
    event.processed_at = datetime.now(timezone.utc)
    event.updated_at = datetime.now(timezone.utc)
    session.add(event)


def _mark_job_done(session, job: Job):
    job.status = 'DONE'
    job.updated_at = datetime.now(timezone.utc)
    session.add(job)


def _mark_job_failed(session, job: Job, error: Exception):
    attempts = int(job.attempts or 0)
    max_attempts = int(job.max_attempts or 5)
    job.error_message = str(error)
    job.updated_at = datetime.now(timezone.utc)
    if attempts >= max_attempts:
        job.status = 'FAILED'
    else:
        job.status = 'PENDING'
    session.add(job)

    payload = job.payload or {}
    event_log_id = payload.get('event_log_id')
    if event_log_id:
        event = session.query(EventLog).filter(EventLog.id == int(event_log_id)).first()
        if event:
            event.attempts = int(event.attempts or 0) + 1
            event.error_message = str(error)
            event.updated_at = datetime.now(timezone.utc)
            if event.attempts >= int(event.max_attempts or 10):
                event.status = 'FAILED'
            session.add(event)


def _process_claimed_job(session, job: Job):
    if job.job_type == 'webhook_event':
        _process_webhook_event(session, job)
        _mark_job_done(session, job)
        return

    if job.job_type == TRAIL_JOB_TYPE:
        result = process_observation_trail_job(session, job)
        if result.get('done', False):
            _mark_job_done(session, job)
        return

    # Keep unknown job types non-blocking while rebuild is in progress.
    logger.debug(f'Skipping unhandled job_type={job.job_type}', extra={'emoji_type': 'debug'})
    _mark_job_done(session, job)


def run_loop(poll_interval_seconds: float = 2.0):
    """Minimal durable worker loop for event jobs."""
    from services.startup_gate import startup_sync_complete
    if not startup_sync_complete.is_set():
        logger.info('Worker holding: waiting for startup sync to complete...', extra={'emoji_type': 'info'})
        startup_sync_complete.wait(timeout=1800)
    logger.info('Worker loop started', extra={'emoji_type': 'gear'})
    while True:
        session = get_session()
        try:
            job = _claim_next_job(session)
            if not job:
                session.close()
                time.sleep(max(0.25, poll_interval_seconds))
                continue

            try:
                _process_claimed_job(session, job)
                session.commit()
            except Exception as e:
                session.rollback()
                _mark_job_failed(session, job, e)
                session.commit()
                logger.error(f'Worker job {job.id} failed: {e}', extra={'emoji_type': 'error'})
        except Exception as e:
            try:
                session.rollback()
            except Exception:
                pass
            logger.error(f'Worker loop iteration failed: {e}', extra={'emoji_type': 'error'})
            time.sleep(max(0.25, poll_interval_seconds))
        finally:
            try:
                session.close()
            except Exception:
                pass
