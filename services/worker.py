import time
from datetime import datetime, timezone

from sqlalchemy import and_

from core.logger import logger
from services.event_normalization import legacy_dispatch_event_type
from services.postgres.db import get_session
from services.postgres.models import EventLog, Job
from services.source_of_truth.event_handlers import (
    process_episode_file_deleted_event,
    process_episode_imported_event,
    process_movie_add_event,
    process_movie_deleted_event,
    process_movie_file_deleted_event,
    process_movie_imported_event,
    process_series_add_event,
    process_series_deleted_event,
)
from services.source_of_truth.event_playback import (
    PLAYBACK_FALLBACK_JOB_TYPE,
    process_playback_fallback_job,
    process_playback_start_event,
)
from services.source_of_truth.import_grace import (
    IMPORT_GRACE_JOB_TYPE,
    process_import_grace_job,
)
from services.source_of_truth.status_reconciler import (
    NFO_REFRESH_JOB_TYPE,
    process_nfo_refresh_job,
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
        .order_by(Job.run_after.asc().nullsfirst(), Job.id.asc())
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
    event_meta = payload.get('_event_meta') if isinstance(payload, dict) else None
    raw_event_type = None
    if isinstance(event_meta, dict):
        raw_event_type = event_meta.get('raw_event_type')

    dispatch_type = legacy_dispatch_event_type(event_type, raw_event_type)

    # Extract instance identifier from source string (e.g. "webhook:radarr_std" -> "radarr_std")
    source_str = getattr(event, 'source', '') or ''
    instance = None
    if source_str.startswith('webhook:'):
        instance = source_str.split(':', 1)[1].strip() or None

    handled = False
    if dispatch_type == 'seriesadd':
        result = process_series_add_event(payload, instance=instance)
        logger.info(
            f"Processed seriesadd event_log_id={event.id} result={result.get('upsert_stats', {})}",
            extra={'emoji_type': 'success'},
        )
        handled = True
    elif dispatch_type in ('movieadd', 'movieadded'):
        result = process_movie_add_event(payload, instance=instance)
        logger.info(
            f"Processed {dispatch_type} event_log_id={event.id} movie_id={result.get('movie_id')}",
            extra={'emoji_type': 'success'},
        )
        handled = True
    elif event_type == 'movie_imported' or dispatch_type == 'moviefileimported':
        result = process_movie_imported_event(payload, instance=instance)
        logger.info(
            f"Processed movie_imported event_log_id={event.id} movie_id={result.get('movie_id')}",
            extra={'emoji_type': 'success'},
        )
        handled = True
    elif event_type == 'episode_imported' or dispatch_type in ('episodefileimported', 'download'):
        result = process_episode_imported_event(payload, instance=instance)
        logger.info(
            f"Processed episode_imported event_log_id={event.id} episodes={len(result.get('episode_ids') or [])}",
            extra={'emoji_type': 'success'},
        )
        handled = True
    elif event_type == 'movie_file_deleted' or dispatch_type == 'moviefiledelete':
        result = process_movie_file_deleted_event(payload, instance=instance)
        logger.info(
            f"Processed movie_file_deleted event_log_id={event.id} movie_id={result.get('movie_id')}",
            extra={'emoji_type': 'success'},
        )
        handled = True
    elif event_type == 'episode_file_deleted' or dispatch_type == 'episodefiledelete':
        result = process_episode_file_deleted_event(payload, instance=instance)
        logger.info(
            f"Processed episode_file_deleted event_log_id={event.id} episodes={len(result.get('episode_ids') or [])}",
            extra={'emoji_type': 'success'},
        )
        handled = True
    elif event_type == 'movie_deleted' or dispatch_type == 'moviedelete':
        result = process_movie_deleted_event(payload, instance=instance)
        logger.info(
            f"Processed movie_deleted event_log_id={event.id} movie_id={result.get('movie_id')}",
            extra={'emoji_type': 'success'},
        )
        handled = True
    elif event_type == 'series_deleted' or dispatch_type == 'seriesdelete':
        result = process_series_deleted_event(payload, instance=instance)
        logger.info(
            f"Processed series_deleted event_log_id={event.id} episodes={len(result.get('episode_ids') or [])}",
            extra={'emoji_type': 'success'},
        )
        handled = True
    elif event_type == 'playback_start' or dispatch_type in ('playback.start', 'playbackstart'):
        result = process_playback_start_event(payload, instance=instance)
        logger.info(
            f"Processed playback_start event_log_id={event.id} result={result}",
            extra={'emoji_type': 'success'},
        )
        handled = True

    if not handled:
        event.error_message = f'unhandled_event_type:{event_type}'
        logger.warning(
            f"Webhook event not yet handled event_log_id={event.id} canonical={event_type} raw={raw_event_type or 'unknown'}",
            extra={'emoji_type': 'warning'},
        )

    event.status = 'DONE'
    event.processed_at = datetime.now(timezone.utc)
    event.updated_at = datetime.now(timezone.utc)
    session.add(event)


def _mark_job_done(session, job: Job):
    job.status = 'DONE'
    job.updated_at = datetime.now(timezone.utc)
    session.add(job)


def _is_non_retriable_webhook_error(error: Exception) -> bool:
    message = str(error or '').strip().lower()
    if not message:
        return False

    # Permanent payload-shape / validation failures will not improve by retrying
    # the same stored webhook payload.
    if message == 'missing_event_log_id':
        return True
    if message == 'unresolved_playback_media_type':
        return True
    if '_missing_' in message:
        return True
    return False


def _mark_job_failed(session, job: Job, error: Exception):
    attempts = int(job.attempts or 0)
    max_attempts = int(job.max_attempts or 5)
    non_retriable = bool(job.job_type == 'webhook_event' and _is_non_retriable_webhook_error(error))
    job.error_message = str(error)
    job.updated_at = datetime.now(timezone.utc)
    if non_retriable or attempts >= max_attempts:
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
            if non_retriable or event.attempts >= int(event.max_attempts or 10):
                event.status = 'FAILED'
            session.add(event)


def _process_claimed_job(session, job: Job):
    if job.job_type == 'webhook_event':
        _process_webhook_event(session, job)
        _mark_job_done(session, job)
        return


    if job.job_type == NFO_REFRESH_JOB_TYPE:
        result = process_nfo_refresh_job(session, job)
        if not result.get('ok', False):
            raise ValueError(str(result.get('reason') or 'nfo_refresh_failed'))
        _mark_job_done(session, job)
        return

    if job.job_type == IMPORT_GRACE_JOB_TYPE:
        logger.debug(f"Processing import_grace job: job_id={getattr(job, 'id', '?')}, payload={getattr(job, 'payload', '?')}", extra={'emoji_type': 'debug'})
        result = process_import_grace_job(session, job)
        logger.debug(f"import_grace job result: job_id={getattr(job, 'id', '?')}, result={result}", extra={'emoji_type': 'debug'})
        if not result.get('ok', False):
            reason = str(result.get('reason') or 'import_grace_failed')
            logger.warning(f"import_grace job failed: job_id={getattr(job, 'id', '?')}, reason={reason}", extra={'emoji_type': 'warning'})
            raise ValueError(reason)
        logger.info(f"import_grace job completed: job_id={getattr(job, 'id', '?')}, phase={result.get('phase')}", extra={'emoji_type': 'success'})
        _mark_job_done(session, job)
        return

    if job.job_type == PLAYBACK_FALLBACK_JOB_TYPE:
        result = process_playback_fallback_job(session, job)
        if not result.get('ok', False):
            raise ValueError(str(result.get('reason') or 'playback_fallback_failed'))
        logger.info(
            f"Processed playback fallback job_id={getattr(job, 'id', '?')} result={result}",
            extra={'emoji_type': 'success'},
        )
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
