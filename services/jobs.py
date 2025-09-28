import json
import time
import logging
import threading
from datetime import datetime, timedelta
from services.postgres.db import get_session
from services.postgres.models import Job, Series, SubFlow, Episode
from core.config import settings
from core.logger import logger
from sqlalchemy import text, or_
from services.flow_manager import flow_manager
from services.plex_client import refresh_plex_dummy
from services.jellyfin_client import refresh_jellyfin_dummy
from services.integrations import enrich_from_arr
from core.config import settings

# Simple job worker that claims Job rows and splits batch payloads into Series SubFlows
# This is intentionally minimal; it uses an atomic UPDATE ... RETURNING to claim jobs

def claim_next_job(session):
    """Atomically claim the next available PENDING job and return the Job row, or None.

    Uses an UPDATE ... FROM (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1) subquery with RETURNING
    so only one worker can claim a given job even under concurrency.
    """
    try:
        # Atomically select-and-update one pending job
        stmt = text("""
            WITH candidate AS (
                SELECT id FROM job
                WHERE status = 'PENDING' AND (run_after IS NULL OR run_after <= now())
                ORDER BY id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE job
            SET status = 'CLAIMED', attempts = COALESCE(attempts,0) + 1, updated_at = now()
            FROM candidate
            WHERE job.id = candidate.id
            RETURNING job.id
        """)
        res = session.execute(stmt).fetchone()
        if not res:
            return None
        job_id = res[0]
        job = session.query(Job).get(job_id)
        if not job:
            return None
        # Keep as CLAIMED for clarity; worker will process and mark DONE/FAILED
        session.add(job)
        session.commit()
        return job
    except Exception as e:
        logger.error(f"claim_next_job error: {e}", extra={'emoji_type': 'error'})
        session.rollback()
        return None


def process_import_list_job(session, job: Job):
    """Process a job of type 'import_list'. Payload is expected to be a dict with 'series_tvdb' list.
    Creates one Series-level SubFlow per tvdb and schedules a combined_refresh job that includes
    expected_counts per-series computed from DB (episodes needing placeholders).
    """
    try:
        payload = job.payload or {}
        series_list = payload.get('series_tvdb', [])
        created_subflows = []
        created_series = []
        expected_counts = {}

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

            # Compute expected episode count for this series:
            # Count episodes that likely need placeholders (no file & not deleted)
            try:
                from services.postgres.models import Season
                cnt = (
                    session.query(Episode)
                    .join(Season, Episode.season_id == Season.id)
                    .filter(Season.series_id == s.id)
                    .filter(Episode.is_deleted == False)
                    .filter(or_(Episode.episodefile_path == None, Episode.has_file == False))
                ).count()
            except Exception:
                cnt = 0

            expected_counts[str(s.id)] = cnt

            # Create a Series-level SubFlow if not exists
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
            combined_payload = {'series_ids': created_series, 'created_subflows': created_subflows}
            combined_job = Job(job_type='combined_refresh', payload=combined_payload, status='PENDING', run_after=datetime.utcnow() + timedelta(seconds=5))
            # persist expected_counts map on job for deterministic waiting
            combined_job.expected_counts = expected_counts
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
    """Wait until per-episode creation subflows for the provided series IDs reach the expected_counts,
    then invoke bulk refresh functions for Plex and Jellyfin. Uses expected_counts stored on job when available.
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

        # Pull expected_counts from job column or payload
        expected_counts = job.expected_counts or payload.get('expected_counts') or {}

        timeout = getattr(settings, 'JOB_COMBINED_REFRESH_TIMEOUT', 60)
        poll = getattr(settings, 'JOB_COMBINED_REFRESH_POLL_INTERVAL', 1)
        waited = 0

        # For each series, wait until number of episode SubFlows created >= expected_count and no PENDING creation subflows
        pending_series = set(series_ids)
        while waited < timeout and pending_series:
            for sid in list(pending_series):
                expected = int(expected_counts.get(str(sid), 0)) if expected_counts else None

                # Count episode SubFlows for this series (those with episode_id set)
                created_eps = (
                    session.query(SubFlow)
                    .filter(SubFlow.series_id == sid)
                    .filter(SubFlow.episode_id != None)
                    .filter(SubFlow.action == 'handle_seriesadd')
                    .count()
                )

                # Count any still-PENDING creation subflows for this series
                pending_creations = (
                    session.query(SubFlow)
                    .filter(SubFlow.series_id == sid)
                    .filter(SubFlow.action == 'handle_seriesadd')
                    .filter(SubFlow.status == 'PENDING')
                    .count()
                )

                ready = False
                if expected is None:
                    # No expected provided: consider ready when no PENDING creation subflows remain
                    ready = (pending_creations == 0)
                else:
                    # Consider ready when created_eps >= expected and no PENDING creation subflows
                    ready = (created_eps >= expected and pending_creations == 0)

                if ready:
                    pending_series.remove(sid)
                    logger.info(f"Series {sid} ready for combined refresh (created_eps={created_eps}, expected={expected})", extra={'emoji_type': 'info'})
                else:
                    logger.verbose(f"Series {sid} not ready yet (created_eps={created_eps}, expected={expected}, pending={pending_creations})", extra={'emoji_type': 'wait'})

            if pending_series:
                session.expunge_all()
                time.sleep(poll)
                waited += poll

        if waited >= timeout and pending_series:
            logger.warning(f"combined_refresh job {job.id} timed out waiting for series: {pending_series} (waited {waited}s)", extra={'emoji_type': 'warning'})

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
        elif job.job_type == 'enrichment':
            # Process an enrichment job: attempt to acquire a per-entity advisory lock
            # so enrichment for the same series/movie serializes. If lock cannot be
            # obtained immediately, requeue the job a short time later to avoid
            # busy-waiting and to give other workers a chance.
            try:
                payload = job.payload or {}
                is_4k = bool(payload.get('is_4k', False))

                # Derive a numeric lock key from payload: prefer explicit arr id, then tvdb/tmdb, then job id
                lock_key = None
                try:
                    arr_id = payload.get('series', {}) and (payload.get('series', {}).get('id') or payload.get('series', {}).get('sonarrId'))
                except Exception:
                    arr_id = None
                try:
                    if arr_id:
                        lock_key = int(arr_id)
                    elif payload.get('series', {}) and payload.get('series', {}).get('tvdbId'):
                        lock_key = int(payload.get('series', {}).get('tvdbId'))
                    elif payload.get('movie', {}) and payload.get('movie', {}).get('tmdbId'):
                        lock_key = int(payload.get('movie', {}).get('tmdbId'))
                    else:
                        lock_key = int(job.id)
                except Exception:
                    lock_key = int(job.id)

                # Try to acquire advisory lock non-blocking
                got_lock = False
                try:
                    # pg_try_advisory_lock returns true if lock acquired
                    stmt = text("SELECT pg_try_advisory_lock(:k)")
                    res = session.execute(stmt, {'k': lock_key}).scalar()
                    got_lock = bool(res)
                except Exception as e:
                    logger.debug(f"Advisory lock attempt failed: {e}", extra={'emoji_type': 'debug'})
                    got_lock = False

                if not got_lock:
                    # Requeue a short time later to avoid contention
                    retry_delay = int(getattr(settings, 'ENRICHMENT_LOCK_REQUEUE_SECONDS', 2) or 2)
                    job.run_after = datetime.utcnow() + timedelta(seconds=retry_delay)
                    session.add(job)
                    session.commit()
                    logger.info(f"Could not acquire lock for enrichment job {job.id}; requeued for +{retry_delay}s", extra={'emoji_type': 'wait'})
                else:
                    try:
                        # We have the lock, run enrichment end-to-end
                        enrich_from_arr(payload=payload, is_4k=is_4k)
                        job.status = 'DONE'
                        session.add(job)
                        session.commit()
                        logger.info(f"Enrichment job {job.id} completed", extra={'emoji_type': 'success'})
                    finally:
                        # Release the advisory lock explicitly
                        try:
                            stmt = text("SELECT pg_advisory_unlock(:k)")
                            session.execute(stmt, {'k': lock_key})
                            session.commit()
                        except Exception:
                            # If unlock fails, rely on connection close to release locks
                            session.rollback()
            except Exception as e:
                logger.error(f"Enrichment job {job.id} failed: {e}", extra={'emoji_type': 'error'})
                job.status = 'FAILED'
                job.error_message = str(e)
                session.add(job)
                session.commit()
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


# Background worker start guard and helper so other modules (handlers/enricher)
# can ensure the job worker is running without duplicating logic.
_worker_thread_started = False
_worker_thread_lock = threading.Lock()


def start_worker_once():
    """Start the background job worker thread once (idempotent).

    Safe to call from multiple places; only the first call will actually start
    the thread.
    """
    global _worker_thread_started
    with _worker_thread_lock:
        if not _worker_thread_started:
            try:
                t = threading.Thread(target=run_worker_loop, args=(), daemon=True)
                t.start()
                _worker_thread_started = True
                logger.debug("Started background job worker thread (central)", extra={'emoji_type': 'debug'})
            except Exception as e:
                logger.error(f"Failed to start job worker thread: {e}", extra={'emoji_type': 'error'})
