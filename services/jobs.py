import json
import time
import logging
from datetime import datetime, timedelta
from services.postgres.db import get_session
from services.postgres.models import Job, Series, SubFlow
from core.config import settings
from core.logger import logger
from sqlalchemy import text
from services.flow_manager import flow_manager
from services.plex_client import refresh_plex_dummy
from services.jellyfin_client import refresh_jellyfin_dummy

# Simple job worker that claims Job rows and splits batch payloads into Series SubFlows
# This is intentionally minimal; it uses SELECT FOR UPDATE SKIP LOCKED to claim jobs

def claim_next_job(session):
    """Claim the next available job using SELECT FOR UPDATE SKIP LOCKED.
    Returns the Job row or None if none available."""
    job = None
    try:
        job = session.execute(text("""
            SELECT id FROM job
            WHERE status = 'PENDING' AND (run_after IS NULL OR run_after <= now())
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        """)).fetchone()
        if not job:
            return None
        job_id = job[0]
        j = session.query(Job).get(job_id)
        if not j:
            return None
        j.status = 'CLAIMED'
        j.attempts = (j.attempts or 0) + 1
        session.add(j)
        session.commit()
        return j
    except Exception as e:
        logger.error(f"claim_next_job error: {e}", extra={'emoji_type': 'error'})
        session.rollback()
        return None


def process_import_list_job(session, job: Job):
    """Process a job of type 'import_list'. Payload is expected to be a dict with 'series_tvdb' list.
    Creates one Series-level SubFlow per tvdb and optionally triggers combined refresh.
    """
    try:
        payload = job.payload or {}
        series_list = payload.get('series_tvdb', [])
        created_subflows = []
        created_series = []

        for tvdb in series_list:
            # Find series by tvdb
            try:
                s = session.query(Series).filter_by(tvdbid=int(tvdb)).first()
            except Exception:
                s = session.query(Series).filter_by(tvdbid=str(tvdb)).first()

            if not s:
                logger.warning(f"Series with TVDB {tvdb} not found in DB; skipping", extra={'emoji_type': 'warning'})
                continue

            created_series.append(s.id)

            # Create a Series-level SubFlow if not exists
            # Use simple idempotent check
            steps_entry = flow_manager.get_initial('handle_seriesadd')
            steps = (steps_entry.__name__ if callable(steps_entry) else ','.join(f.__name__ for f in steps_entry))
            filter_kwargs = {'series_id': s.id, 'branch': 'all', 'steps': steps, 'action': 'handle_seriesadd'}
            existing = session.query(SubFlow).filter_by(**filter_kwargs).filter(SubFlow.status.in_(['PENDING','QUEUED','FAILED'])).first()
            if existing:
                logger.debug(f"SubFlow already exists for series {s.id}: {existing.id}", extra={'emoji_type': 'debug'})
                created_subflows.append(existing.id)
                continue

            sf = SubFlow(
                series_id=s.id,
                action='handle_seriesadd',
                branch='all',
                steps=steps,
                step_index=0,
                status='PENDING'
            )
            session.add(sf)
            session.commit()
            created_subflows.append(sf.id)
            logger.info(f"Created Series-level SubFlow {sf.id} for series {s.title}", extra={'emoji_type': 'success'})

        # After creating all Series SubFlows, optionally trigger combined refresh
        if created_subflows and getattr(settings, 'BATCH_SERIES_SUBFLOWS', True):
            logger.info(f"Created {len(created_subflows)} series subflows; scheduling combined refresh job", extra={'emoji_type': 'refresh'})
            # Schedule a follow-up combined_refresh job that waits for per-episode creation to finish
            from datetime import datetime, timedelta
            combined_payload = {'series_ids': created_series, 'created_subflows': created_subflows}
            combined_job = Job(job_type='combined_refresh', payload=combined_payload, status='PENDING', run_after=datetime.utcnow() + timedelta(seconds=5))
            session.add(combined_job)
            session.commit()
            logger.info(f"Scheduled combined_refresh job {combined_job.id} for {len(created_series)} series", extra={'emoji_type': 'refresh'})

        # Mark job DONE
        job.status = 'DONE'
        session.add(job)
        session.commit()
        return True

    except Exception as e:
        logger.error(f"Failed processing import_list job {job.id}: {e}", extra={'emoji_type': 'error'})
        job.status = 'FAILED'
        job.error_message = str(e)
        session.add(job)
        session.commit()
        return False


def process_combined_refresh_job(session, job: Job):
    """Wait until per-episode creation subflows for the provided series IDs are no longer PENDING,
    then invoke bulk refresh functions for Plex and Jellyfin.
    """
    try:
        payload = job.payload or {}
        series_ids = payload.get('series_ids', [])
        if not series_ids:
            logger.warning(f"combined_refresh job {job.id} has no series_ids; marking DONE", extra={'emoji_type': 'warning'})
            job.status = 'DONE'
            session.add(job)
            session.commit()
            return True

        # Wait until there are no SubFlows for these series stuck in PENDING (initial creation)
        timeout = getattr(settings, 'JOB_COMBINED_REFRESH_TIMEOUT', 60)
        waited = 0
        poll = 1
        while waited < timeout:
            pending = (
                session.query(SubFlow)
                .join(Series, SubFlow.series_id == Series.id)
                .filter(Series.id.in_(series_ids), SubFlow.action == 'handle_seriesadd', SubFlow.status == 'PENDING')
                .count()
            )
            if pending == 0:
                break
            logger.verbose(f"combined_refresh job {job.id}: waiting for {pending} pending subflows to finish (waited {waited}s)", extra={'emoji_type': 'wait'})
            session.expunge_all()
            time.sleep(poll)
            waited += poll

        if waited >= timeout:
            logger.warning(f"combined_refresh job {job.id} timed out waiting for creation subflows (waited {waited}s)", extra={'emoji_type': 'warning'})

        # Call bulk refresh functions to refresh all queued SubFlows for the 'handle_seriesadd' action
        logger.info(f"combined_refresh job {job.id}: invoking bulk Plex refresh", extra={'emoji_type': 'refresh'})
        try:
            refresh_plex_dummy(session, None, None, 'handle_seriesadd')
        except Exception as e:
            logger.error(f"combined_refresh job {job.id}: plex refresh failed: {e}", extra={'emoji_type': 'error'})

        logger.info(f"combined_refresh job {job.id}: invoking bulk Jellyfin refresh", extra={'emoji_type': 'refresh'})
        try:
            refresh_jellyfin_dummy(session, None, None, 'handle_seriesadd')
        except Exception as e:
            logger.error(f"combined_refresh job {job.id}: jellyfin refresh failed: {e}", extra={'emoji_type': 'error'})

        # Mark job DONE
        job.status = 'DONE'
        session.add(job)
        session.commit()
        return True
    except Exception as e:
        logger.error(f"Failed processing combined_refresh job {job.id}: {e}", extra={'emoji_type': 'error'})
        job.status = 'FAILED'
        job.error_message = str(e)
        session.add(job)
        session.commit()
        return False


def work_once():
    session = get_session()
    try:
        job = claim_next_job(session)
        if not job:
            return False
        logger.info(f"Claimed job {job.id} of type {job.job_type}", extra={'emoji_type': 'job'})
        if job.job_type == 'import_list':
            process_import_list_job(session, job)
        elif job.job_type == 'combined_refresh':
            process_combined_refresh_job(session, job)
        else:
            logger.warning(f"Unknown job type: {job.job_type}. Marking as FAILED", extra={'emoji_type': 'warning'})
            job.status = 'FAILED'
            job.error_message = f"Unknown job type: {job.job_type}"
            session.add(job)
            session.commit()
        return True
    finally:
        session.close()


def run_worker_loop(poll_interval=2):
    logger.info("Starting job worker loop", extra={'emoji_type': 'start'})
    while True:
        try:
            did_work = work_once()
            if not did_work:
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            logger.info("Job worker stopping", extra={'emoji_type': 'stop'})
            break
        except Exception as e:
            logger.error(f"Job worker loop error: {e}", extra={'emoji_type': 'error'})
            time.sleep(poll_interval)
