import json
import time
import logging
import threading
from datetime import datetime, timedelta
from services.postgres.db import get_session
from services.postgres.models import Job, Series, SubFlow, Episode, Movie
from core.config import settings
from core.logger import logger
from datetime import timedelta
from sqlalchemy import text, or_
from services.flow_manager import flow_manager
from services.plex_client import refresh_plex_dummy
from services.jellyfin_client import refresh_jellyfin_dummy
from services.integrations import enrich_from_arr
from core.config import settings
from services.utils import is_library_accessible
import sys


# Helper: enqueue a deduplicated determine_placeholder job for a movie or episode
def _enqueue_determine_placeholder_db(session, movie_id=None, episode_id=None):
    """Enqueue a determine_placeholder job in the DB if one doesn't already exist for the same group_id.

    This uses the provided SQLAlchemy session and does NOT commit; caller should commit.
    """
    try:
        from services.postgres.models import Job as JobModel
        if movie_id is not None:
            try:
                mid = int(movie_id)
            except Exception:
                mid = movie_id
            group_id = f"determine:movie:{mid}"
            exists = (
                session.query(JobModel)
                .filter(JobModel.job_type == 'determine_placeholder')
                .filter(JobModel.group_id == group_id)
                .filter(JobModel.status.in_(['PENDING','CLAIMED','WORKING']))
                .first()
            )
            if not exists:
                newjob = JobModel(job_type='determine_placeholder', payload={'movie_id': mid}, status='PENDING', group_id=group_id)
                session.add(newjob)
        elif episode_id is not None:
            try:
                eid = int(episode_id)
            except Exception:
                eid = episode_id
            group_id = f"determine:episode:{eid}"
            exists = (
                session.query(JobModel)
                .filter(JobModel.job_type == 'determine_placeholder')
                .filter(JobModel.group_id == group_id)
                .filter(JobModel.status.in_(['PENDING','CLAIMED','WORKING']))
                .first()
            )
            if not exists:
                newjob = JobModel(job_type='determine_placeholder', payload={'episode_id': eid}, status='PENDING', group_id=group_id)
                session.add(newjob)
    except Exception:
        # non-fatal: do not let enqueueing prevent enrichment success
        try:
            session.rollback()
        except Exception:
            pass
    return

# In-memory aggregation for enrichment summary logging (worker-local)
# Keys: total, movies_done, series_done, other_done, failures, last_flush, last_total
# Added sets to track which DB ids were processed so the flush can enqueue
# a determine job per-entity (episode/movie) rather than depending on the
# single payload that happened to trigger the flush.
_ENRICH_SUMMARY = {
    'total': 0,
    'movies_done': 0,
    'series_done': 0,
    'other_done': 0,
    'failures': 0,
    'last_flush': None,
    'last_total': 0,
    # track DB ids processed in this worker during the summary window
    'episode_ids': set(),
    'movie_ids': set(),
}
# Track the last total emitted as a definitive "complete" so we only log that once per milestone
_ENRICH_LAST_COMPLETE_TOTAL = 0

# In-memory aggregation for determination persistence summary logging (worker-local)
# Keys: total, persisted, failures, last_flush, last_total
_DETERMINE_SUMMARY = {
    'total': 0,
    'persisted': 0,
    'failures': 0,
    'last_flush': None,
    'last_total': 0,
}

# Simple job worker that claims Job rows and splits batch payloads into Series SubFlows
# This is intentionally minimal; it uses an atomic UPDATE ... RETURNING to claim jobs

def claim_next_job(session):
    """Atomically claim the next available PENDING job and return the Job row, or None.

    Uses an UPDATE ... FROM (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1) subquery with RETURNING
    so only one worker can claim a given job even under concurrency.
    """
    try:
        # Atomically select-and-update one pending job. To ensure we do not
        # process multiple jobs for the same content concurrently, exclude any
        # candidate whose group_id already has a job in CLAIMED or WORKING state.
        # Jobs with NULL group_id are unaffected (they can be processed normally).
        stmt = text("""
            WITH candidate AS (
                SELECT j.id FROM job j
                WHERE j.status = 'PENDING'
                  AND (j.run_after IS NULL OR j.run_after <= now())
                  AND NOT EXISTS (
                      SELECT 1 FROM job a
                      WHERE a.group_id IS NOT NULL
                        AND a.group_id = j.group_id
                        AND a.status IN ('CLAIMED','WORKING')
                  )
                ORDER BY j.id
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
            # schedule using local server time so DB now() will consider it eligible
            combined_job = Job(job_type='combined_refresh', payload=combined_payload, status='PENDING', run_after=datetime.now() + timedelta(seconds=5))
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


def process_attach_dummypaths_job(session, job: Job):
    """Process an attach_dummypaths job: find the Series by TVDB id and run
    attach_dummypaths_from_fs(session, series_row, is_4k).
    This centralizes filesystem scanning under the job worker instead of
    spawning background threads from the enricher.
    """
    try:
        payload = job.payload or {}
        tvdb = payload.get('tvdb')
        is_4k = bool(payload.get('is_4k', False))

        if not tvdb:
            job.status = 'FAILED'
            job.error_message = 'Missing tvdb in payload'
            session.add(job)
            session.commit()
            return False

        # Resolve series by tvdb
        try:
            s = session.query(Series).filter_by(tvdbid=int(tvdb)).first()
        except Exception:
            s = session.query(Series).filter_by(tvdbid=str(tvdb)).first()

        if not s:
            job.status = 'FAILED'
            job.error_message = f'Series tvdb {tvdb} not found'
            session.add(job)
            session.commit()
            return False

        # Run attach routine from integrations (kept in job worker to centralize IO)
        try:
            # Pre-flight: check placeholder storage accessibility and bail out if it's down.
            lib_root = settings.TV_LIBRARY_FOLDER_4K if is_4k else settings.TV_LIBRARY_FOLDER
            if lib_root and not is_library_accessible(lib_root):
                logger.error(f"TV placeholder storage unreachable: {lib_root}. Stopping worker to avoid destructive operations.", extra={'emoji_type': 'error'})
                try:
                    session.close()
                finally:
                    sys.exit(1)

            from services.integrations import attach_dummypaths_from_fs
            attach_dummypaths_from_fs(session=session, series_row=s, is_4k=is_4k)
        except Exception as e:
            try:
                from services.utils import format_movie_label
            except Exception:
                pass
            logger.error(f"attach_dummypaths job failed for tvdb={tvdb}: {e}", extra={'emoji_type': 'error'})
            job.status = 'FAILED'
            job.error_message = str(e)
            session.add(job)
            session.commit()
            return False

        # Success
        # We intentionally DO NOT enqueue a determine_placeholder job here. Determination
        # must run after enrichment completes to avoid races; the enrichment worker
        # flush logic will enqueue per-entity determine jobs for any entities it
        # processed. Leaving this out prevents duplicate/early determine runs.
        logger.debug(f"attach_dummypaths completed for series id={getattr(s,'id',None)}; determination will be scheduled by enrichment flush", extra={'emoji_type': 'debug'})

        job.status = 'DONE'
        session.add(job)
        session.commit()
        try:
            from services.utils import format_episode_label
            label = f"series tvdb={tvdb}"
        except Exception:
            label = f"series tvdb={tvdb}"
        logger.verbose(f"attach_dummypaths job completed for {label}", extra={'emoji_type': 'success'})
        return True
    except Exception as e:
        logger.error(f"Failed processing attach_dummypaths job {getattr(job,'id',None)}: {e}", extra={'emoji_type': 'error'})
        try:
            job.status = 'FAILED'
            job.error_message = str(e)
            session.add(job)
            session.commit()
        except Exception:
            session.rollback()
        return False


def process_attach_moviedummypath_job(session, job: Job):
    """Process an attach_moviedummypath job: find the Movie by id and run
    attach_moviedummypath_from_fs(session, movie_row, is_4k).
    """
    try:
        payload = job.payload or {}
        movie_id = payload.get('movie_id')
        is_4k = bool(payload.get('is_4k', False))

        if not movie_id:
            job.status = 'FAILED'
            job.error_message = 'Missing movie_id in payload'
            session.add(job)
            session.commit()
            return False

        mv = session.query(Movie).get(int(movie_id))
        if not mv:
            job.status = 'FAILED'
            job.error_message = f'Movie id {movie_id} not found'
            session.add(job)
            session.commit()
            return False

        try:
            # Pre-flight: check placeholder storage accessibility and bail out if it's down.
            lib_root = settings.MOVIE_LIBRARY_FOLDER_4K if is_4k else settings.MOVIE_LIBRARY_FOLDER
            if lib_root and not is_library_accessible(lib_root):
                logger.error(f"Movie placeholder storage unreachable: {lib_root}. Stopping worker to avoid destructive operations.", extra={'emoji_type': 'error'})
                try:
                    session.close()
                finally:
                    sys.exit(1)

            from services.integrations import attach_moviedummypath_from_fs
            attach_moviedummypath_from_fs(session=session, movie_row=mv, is_4k=is_4k)
        except Exception as e:
            logger.error(f"attach_moviedummypath job failed for movie_id={movie_id}: {e}", extra={'emoji_type': 'error'})
            job.status = 'FAILED'
            job.error_message = str(e)
            session.add(job)
            session.commit()
            return False

        # After attaching any on-disk placeholder, we DO NOT enqueue determine jobs here.
        # Determination should run after enrichment; the enrichment flush will enqueue
        # determine_placeholder jobs for movies the worker processed. This prevents
        # early determination and reduces duplicate work.
        logger.debug(f"attach_moviedummypath completed for movie id={getattr(mv,'id',None)}; determination will be scheduled by enrichment flush", extra={'emoji_type': 'debug'})

        job.status = 'DONE'
        session.add(job)
        session.commit()
        logger.verbose(f"attach_moviedummypath job completed for movie id={movie_id}", extra={'emoji_type': 'success'})
        return True
    except Exception as e:
        logger.error(f"Failed processing attach_moviedummypath job {getattr(job,'id',None)}: {e}", extra={'emoji_type': 'error'})
        try:
            job.status = 'FAILED'
            job.error_message = str(e)
            session.add(job)
            session.commit()
        except Exception:
            session.rollback()
        return False


# NOTE: full-sync behavior is centralized in services/syncer.run_full_sync.
# We intentionally do not implement a direct filesystem-scanning full-sync here to
# avoid duplicate implementations. If a job needs to trigger a full-sync, it
# should call the syncer helper or enqueue per-series/per-movie attach jobs.


def work_once():
    session = get_session()
    try:
        job = claim_next_job(session)
        if not job:
            return False
        logger.verbose(f"Claimed job {job.id} of type {job.job_type}", extra={'emoji_type': 'job'})
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
                    # set retry using local time so DB now() aligns
                    job.run_after = datetime.now() + timedelta(seconds=retry_delay)
                    # Mark as PENDING again so claim_next_job can pick it up later
                    job.status = 'PENDING'
                    session.add(job)
                    session.commit()
                    logger.info(f"Could not acquire lock for enrichment job {job.id}; requeued as PENDING for +{retry_delay}s", extra={'emoji_type': 'wait'})
                else:
                    try:
                        # We have the lock, run enrichment end-to-end
                        enrich_from_arr(payload=payload, is_4k=is_4k)

                        # Deterministically enqueue determine jobs for any movie/episode
                        # we can extract from the payload using the same DB session. Doing
                        # this here (DB-driven) avoids relying only on the worker-local
                        # in-memory flush which can miss items processed by other workers
                        # or outside the flush window.
                        try:
                            # movie path: try to derive the DB primary key for the movie
                            # Prefer resolving external IDs (Radarr tmdb/radarr ids) to the
                            # Movie PK so determine jobs reference the correct DB row.
                            m_id = None
                            try:
                                # If payload contains an explicit movie_id (assumed to be PK), use it
                                if payload.get('movie_id'):
                                    try:
                                        m_id = int(payload.get('movie_id'))
                                    except Exception:
                                        m_id = None
                                # If payload contains a movie object with a radarr/arr id, try to resolve
                                elif payload.get('movie') and payload.get('movie').get('id'):
                                    try:
                                        radarr_id = int(payload.get('movie').get('id'))
                                    except Exception:
                                        radarr_id = payload.get('movie').get('id')
                                    if radarr_id is not None:
                                        try:
                                            from services.postgres.models import Movie as MovieModel
                                            mv = session.query(MovieModel).filter(MovieModel.radarrid == radarr_id).first()
                                            if mv:
                                                m_id = mv.id
                                        except Exception:
                                            m_id = None
                                # If payload contains a tmdb id, try to resolve
                                elif payload.get('movie') and (payload.get('movie').get('tmdbId') or payload.get('movie').get('tmdb')):
                                    try:
                                        tmdb = int(payload.get('movie').get('tmdbId') or payload.get('movie').get('tmdb'))
                                    except Exception:
                                        tmdb = None
                                    if tmdb is not None:
                                        try:
                                            from services.postgres.models import Movie as MovieModel
                                            mv = session.query(MovieModel).filter(MovieModel.tmdbid == tmdb).first()
                                            if mv:
                                                m_id = mv.id
                                        except Exception:
                                            m_id = None
                            except Exception:
                                m_id = None

                            if m_id:
                                _enqueue_determine_placeholder_db(session, movie_id=m_id)

                            # episode path: if this enrichment included explicit episode ids
                            try:
                                if payload.get('episode_id'):
                                    _enqueue_determine_placeholder_db(session, episode_id=payload.get('episode_id'))
                                elif payload.get('series') and payload.get('series').get('episodes'):
                                    for e in payload.get('series').get('episodes'):
                                        try:
                                            if e and e.get('id'):
                                                _enqueue_determine_placeholder_db(session, episode_id=int(e.get('id')))
                                        except Exception:
                                            continue
                            except Exception:
                                pass
                        except Exception:
                            pass

                        job.status = 'DONE'
                        session.add(job)
                        session.commit()
                        # Track enrichment completions in an in-memory counter and
                        # emit a periodic summary instead of noisy per-item INFO.
                        try:
                            _ENRICH_SUMMARY['total'] += 1
                            # classify by payload type (movie vs tv/series)
                            if payload.get('movie') or payload.get('movie_id'):
                                _ENRICH_SUMMARY['movies_done'] += 1
                                # attempt to record the Movie DB primary key for later determine enqueue
                                try:
                                    m_id = None
                                    # If caller provided an explicit DB movie_id use it
                                    if payload.get('movie_id'):
                                        try:
                                            m_id = int(payload.get('movie_id'))
                                        except Exception:
                                            m_id = None
                                    # Else if payload.movie.id is present, it may be an external Radarr id; try to resolve
                                    elif payload.get('movie') and payload.get('movie').get('id'):
                                        try:
                                            radarr_id = int(payload.get('movie').get('id'))
                                        except Exception:
                                            radarr_id = payload.get('movie').get('id')
                                        if radarr_id is not None:
                                            try:
                                                from services.postgres.models import Movie as MovieModel
                                                mv = session.query(MovieModel).filter(MovieModel.radarrid == radarr_id).first()
                                                if mv:
                                                    m_id = mv.id
                                            except Exception:
                                                m_id = None
                                    # Else try resolving by tmdb id if present
                                    elif payload.get('movie') and (payload.get('movie').get('tmdbId') or payload.get('movie').get('tmdb')):
                                        try:
                                            tmdb = int(payload.get('movie').get('tmdbId') or payload.get('movie').get('tmdb'))
                                        except Exception:
                                            tmdb = None
                                        if tmdb is not None:
                                            try:
                                                from services.postgres.models import Movie as MovieModel
                                                mv = session.query(MovieModel).filter(MovieModel.tmdbid == tmdb).first()
                                                if mv:
                                                    m_id = mv.id
                                            except Exception:
                                                m_id = None
                                except Exception:
                                    m_id = None
                                if m_id:
                                    try:
                                        _ENRICH_SUMMARY['movie_ids'].add(int(m_id))
                                    except Exception:
                                        pass
                            elif payload.get('series') or payload.get('series_tvdb') or payload.get('series_id'):
                                _ENRICH_SUMMARY['series_done'] += 1
                                # For series-level payloads, try to capture any explicit episode identifiers
                                try:
                                    # episode-level payload shapes (sonarr/radarr style)
                                    if payload.get('episode_id'):
                                        _ENRICH_SUMMARY['episode_ids'].add(int(payload.get('episode_id')))
                                    elif payload.get('series') and payload.get('series').get('episodes'):
                                        # ARR payloads sometimes include per-episode entries with sonarr ids
                                        for e in payload.get('series').get('episodes'):
                                            try:
                                                if e and e.get('id'):
                                                    _ENRICH_SUMMARY['episode_ids'].add(int(e.get('id')))
                                            except Exception:
                                                continue
                                except Exception:
                                    pass
                            else:
                                _ENRICH_SUMMARY['other_done'] += 1
                        except Exception:
                            pass
                        logger.verbose(f"Enrichment job {job.id} completed", extra={'emoji_type': 'success'})
                        # Flush summary if enough time has passed or we've accumulated many entries
                        try:
                            # use local time for now to match DB timezone
                            now = datetime.now()
                            last = _ENRICH_SUMMARY.get('last_flush') or (now - timedelta(seconds=9999))
                            count_since = _ENRICH_SUMMARY.get('total', 0) - _ENRICH_SUMMARY.get('last_total', 0)
                            if (now - last).total_seconds() >= getattr(settings, 'ENRICH_SUMMARY_INTERVAL', 10) or count_since >= getattr(settings, 'ENRICH_SUMMARY_COUNT', 50):
                                logger.info(
                                    f"Enrichment summary: total={_ENRICH_SUMMARY.get('total',0)} "
                                    f"movies_done={_ENRICH_SUMMARY.get('movies_done',0)} "
                                    f"series_done={_ENRICH_SUMMARY.get('series_done',0)} "
                                    f"other_done={_ENRICH_SUMMARY.get('other_done',0)} "
                                    f"failures={_ENRICH_SUMMARY.get('failures',0)}",
                                    extra={'emoji_type': 'summary'}
                                )
                                _ENRICH_SUMMARY['last_flush'] = now
                                _ENRICH_SUMMARY['last_total'] = _ENRICH_SUMMARY.get('total',0)

                                # If the enrichment queue is drained emit a definitive completion line once
                                try:
                                    pending = session.query(Job).filter(Job.job_type == 'enrichment', Job.status.in_(['PENDING','CLAIMED','WORKING'])).count()
                                    if pending == 0 and _ENRICH_SUMMARY.get('total', 0) != _ENRICH_LAST_COMPLETE_TOTAL:
                                        logger.info(
                                            f"Enrichment phase complete: total_enriched={_ENRICH_SUMMARY.get('total',0)} "
                                            f"movies_done={_ENRICH_SUMMARY.get('movies_done',0)} "
                                            f"series_done={_ENRICH_SUMMARY.get('series_done',0)} "
                                            f"other_done={_ENRICH_SUMMARY.get('other_done',0)} "
                                            f"failures={_ENRICH_SUMMARY.get('failures',0)}",
                                            extra={'emoji_type': 'summary'}
                                        )
                                        try:
                                            _ENRICH_LAST_COMPLETE_TOTAL = _ENRICH_SUMMARY.get('total', 0)
                                        except Exception:
                                            pass
                                except Exception:
                                    pass

                                # From the worker session, enqueue deduped determine_placeholder jobs
                                # Iterate the collected episode_ids and movie_ids so we enqueue
                                # one determine job per actual DB entity processed during the window.
                                try:
                                    from services.postgres.models import Job as JobModel
                                    # Enqueue episode-level determine jobs
                                    try:
                                        for ep_id in list(_ENRICH_SUMMARY.get('episode_ids', set())):
                                            try:
                                                group_id = f"determine:episode:{ep_id}"
                                                exists = session.query(JobModel).filter(JobModel.job_type == 'determine_placeholder').filter(JobModel.group_id == group_id).filter(JobModel.status.in_(['PENDING','CLAIMED','WORKING'])).first()
                                                if not exists:
                                                    newjob = JobModel(job_type='determine_placeholder', payload={'episode_id': ep_id}, status='PENDING', group_id=group_id)
                                                    session.add(newjob)
                                                    try:
                                                        session.commit()
                                                    except Exception:
                                                        try:
                                                            session.rollback()
                                                        except Exception:
                                                            pass
                                            except Exception:
                                                continue
                                    except Exception:
                                        pass

                                    # Enqueue movie-level determine jobs
                                    try:
                                        for m_id in list(_ENRICH_SUMMARY.get('movie_ids', set())):
                                            try:
                                                group_id = f"determine:movie:{m_id}"
                                                exists = session.query(JobModel).filter(JobModel.job_type == 'determine_placeholder').filter(JobModel.group_id == group_id).filter(JobModel.status.in_(['PENDING','CLAIMED','WORKING'])).first()
                                                if not exists:
                                                    newjob = JobModel(job_type='determine_placeholder', payload={'movie_id': m_id}, status='PENDING', group_id=group_id)
                                                    session.add(newjob)
                                                    try:
                                                        session.commit()
                                                    except Exception:
                                                        try:
                                                            session.rollback()
                                                        except Exception:
                                                            pass
                                            except Exception:
                                                continue
                                    except Exception:
                                        pass

                                    # Commit once for the batch of enqueues
                                    try:
                                        session.commit()
                                        # Log counts
                                        try:
                                            ecount = len(_ENRICH_SUMMARY.get('episode_ids', set()))
                                            mcount = len(_ENRICH_SUMMARY.get('movie_ids', set()))
                                            if ecount or mcount:
                                                logger.info(f"Enqueued worker determine_placeholder jobs: episodes={ecount} movies={mcount}", extra={'emoji_type': 'queue'})
                                        except Exception:
                                            pass
                                    except Exception:
                                        try:
                                            session.rollback()
                                        except Exception:
                                            pass
                                    finally:
                                        # clear the sets after attempting enqueue
                                        try:
                                            _ENRICH_SUMMARY['episode_ids'].clear()
                                            _ENRICH_SUMMARY['movie_ids'].clear()
                                        except Exception:
                                            pass
                                except Exception:
                                    # non-fatal: don't let enqueueing impact enrichment success
                                    pass

                                # Flush determine summary periodically (every N items or T seconds)
                                try:
                                    nowdt = datetime.now()
                                    lastd = _DETERMINE_SUMMARY.get('last_flush') or (nowdt - timedelta(seconds=9999))
                                    since = _DETERMINE_SUMMARY.get('total', 0) - _DETERMINE_SUMMARY.get('last_total', 0)
                                    if (nowdt - lastd).total_seconds() >= getattr(settings, 'DETERMINE_SUMMARY_INTERVAL', 10) or since >= getattr(settings, 'DETERMINE_SUMMARY_COUNT', 50):
                                        logger.info(
                                            f"Determine persistence summary: total={_DETERMINE_SUMMARY.get('total',0)} persisted={_DETERMINE_SUMMARY.get('persisted',0)} failures={_DETERMINE_SUMMARY.get('failures',0)}",
                                            extra={'emoji_type': 'summary'}
                                        )
                                        _DETERMINE_SUMMARY['last_flush'] = nowdt
                                        _DETERMINE_SUMMARY['last_total'] = _DETERMINE_SUMMARY.get('total',0)
                                except Exception:
                                    pass
                        except Exception:
                            pass
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
                # Record a failure in the in-memory enrichment summary so periodic
                # reports reflect actual failures observed while processing jobs.
                try:
                    _ENRICH_SUMMARY['failures'] = _ENRICH_SUMMARY.get('failures', 0) + 1
                except Exception:
                    pass
                job.status = 'FAILED'
                job.error_message = str(e)
                session.add(job)
                session.commit()
        elif job.job_type == 'resolve_episodefile':
            # Background worker: resolve an episodeFile by id and persist episode metadata
            try:
                payload = job.payload or {}
                episode_id = payload.get('episode_id')
                ef_id = payload.get('episodeFileId')
                is_4k = bool(payload.get('is_4k', False))
                if not episode_id or not ef_id:
                    job.status = 'FAILED'
                    job.error_message = 'Missing episode_id or episodeFileId in payload'
                    session.add(job)
                    session.commit()
                else:
                    try:
                        # Fetch episode row
                        ep_row = session.query(Episode).get(int(episode_id))
                        if not ep_row:
                            job.status = 'FAILED'
                            job.error_message = f'Episode id {episode_id} not found'
                            session.add(job)
                            session.commit()
                        else:
                            # fetch ef by id and apply canonical metadata
                            from services.arr_enrichment import fetch_episodefile_by_id, extract_episodefile_metadata
                            config = settings.SONARR_4K_URL if is_4k else settings.SONARR_URL
                            api_key = settings.SONARR_4K_API_KEY if is_4k else settings.SONARR_API_KEY
                            headers = {'X-Api-Key': api_key}
                            base_url = config
                            ef = fetch_episodefile_by_id(base_url, headers, int(ef_id))
                            # We need the original episode payload shape for extract helper; build a minimal ep dict
                            ep_payload = {'id': getattr(ep_row, 'sonarrid', None) or None, 'monitored': getattr(ep_row, 'sonarr_monitored', False)}
                            meta = extract_episodefile_metadata(ep_payload, ef)
                            updated = False
                            try:
                                if meta.get('sonarr_episode_id') and getattr(ep_row, 'sonarrid', None) != meta.get('sonarr_episode_id'):
                                    ep_row.sonarrid = meta.get('sonarr_episode_id')
                                    updated = True
                            except Exception:
                                pass
                            try:
                                if getattr(ep_row, 'sonarr_status', None) != meta.get('sonarr_status'):
                                    ep_row.sonarr_status = meta.get('sonarr_status')
                                    updated = True
                            except Exception:
                                pass
                            try:
                                if getattr(ep_row, 'sonarr_monitored', None) != meta.get('sonarr_monitored'):
                                    ep_row.sonarr_monitored = meta.get('sonarr_monitored')
                                    updated = True
                            except Exception:
                                pass
                            try:
                                has_file = meta.get('has_file', False)
                                if getattr(ep_row, 'has_file', None) != has_file:
                                    ep_row.has_file = has_file
                                    updated = True
                                if has_file:
                                    if meta.get('episodefile_path') and getattr(ep_row, 'episodefile_path', None) != meta.get('episodefile_path'):
                                        ep_row.episodefile_path = meta.get('episodefile_path')
                                        updated = True
                                    if meta.get('episodefile_size') is not None and getattr(ep_row, 'episodefile_size', None) != meta.get('episodefile_size'):
                                        ep_row.episodefile_size = meta.get('episodefile_size')
                                        updated = True
                                    if meta.get('sonarr_quality') and getattr(ep_row, 'sonarr_quality', None) != meta.get('sonarr_quality'):
                                        ep_row.sonarr_quality = meta.get('sonarr_quality')
                                        updated = True
                                    if meta.get('sonarrpath') and getattr(ep_row, 'sonarrpath', None) != meta.get('sonarrpath'):
                                        ep_row.sonarrpath = meta.get('sonarrpath')
                                        updated = True
                                else:
                                    if getattr(ep_row, 'episodefile_path', None) is not None:
                                        ep_row.episodefile_path = None
                                        updated = True
                                    if getattr(ep_row, 'episodefile_size', None) is not None:
                                        ep_row.episodefile_size = None
                                        updated = True
                                    if getattr(ep_row, 'sonarr_quality', None) is not None:
                                        ep_row.sonarr_quality = None
                                        updated = True
                                    if getattr(ep_row, 'sonarrpath', None) is not None:
                                        ep_row.sonarrpath = None
                                        updated = True
                            except Exception:
                                pass
                            if updated:
                                session.add(ep_row)
                                session.commit()
                            job.status = 'DONE'
                            session.add(job)
                            session.commit()
                            # Track episode id so flush will enqueue a determine job for it
                            try:
                                try:
                                    epid = int(episode_id)
                                except Exception:
                                    epid = None
                                if epid:
                                    try:
                                        _ENRICH_SUMMARY['episode_ids'].add(epid)
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                    except Exception as e:
                        logger.error(f"resolve_episodefile job {job.id} failed: {e}", extra={'emoji_type':'error'})
                        job.status = 'FAILED'
                        job.error_message = str(e)
                        session.add(job)
                        session.commit()
            except Exception as e:
                logger.error(f"resolve_episodefile job {job.id} failed outer: {e}", extra={'emoji_type':'error'})
                job.status = 'FAILED'
                job.error_message = str(e)
                session.add(job)
                session.commit()
        elif job.job_type == 'resolve_moviefile':
            # Background worker: resolve a movieFile by id and persist movie metadata
            try:
                payload = job.payload or {}
                movie_id = payload.get('movie_id')
                mf_id = payload.get('movieFileId')
                is_4k = bool(payload.get('is_4k', False))
                if not movie_id or not mf_id:
                    job.status = 'FAILED'
                    job.error_message = 'Missing movie_id or movieFileId in payload'
                    session.add(job)
                    session.commit()
                else:
                    try:
                        mv_row = session.query(Movie).get(int(movie_id))
                        if not mv_row:
                            job.status = 'FAILED'
                            job.error_message = f'Movie id {movie_id} not found'
                            session.add(job)
                            session.commit()
                        else:
                            from services.arr_enrichment import fetch_movie_by_id, extract_moviefile_metadata
                            config = settings.RADARR_4K_URL if is_4k else settings.RADARR_URL
                            api_key = settings.RADARR_4K_API_KEY if is_4k else settings.RADARR_API_KEY
                            headers = {'X-Api-Key': api_key}
                            base_url = config
                            mf = fetch_movie_by_id(base_url, headers, int(mf_id))
                            # Build a minimal movie payload for extractor
                            movie_payload = {'id': getattr(mv_row, 'radarrid', None), 'monitored': getattr(mv_row, 'radarr_monitored', False)}
                            meta = extract_moviefile_metadata(movie_payload, mf)
                            updated = False
                            try:
                                has_file = meta.get('has_file', False)
                                if getattr(mv_row, 'has_file', None) != has_file:
                                    mv_row.has_file = has_file
                                    updated = True
                                if has_file:
                                    if meta.get('moviefile_path') and getattr(mv_row, 'moviefile_path', None) != meta.get('moviefile_path'):
                                        mv_row.moviefile_path = meta.get('moviefile_path')
                                        updated = True
                                    if meta.get('moviefile_size') is not None and getattr(mv_row, 'moviefile_size', None) != meta.get('moviefile_size'):
                                        mv_row.moviefile_size = meta.get('moviefile_size')
                                        updated = True
                                    if meta.get('radarr_quality') and getattr(mv_row, 'radarr_quality', None) != meta.get('radarr_quality'):
                                        mv_row.radarr_quality = meta.get('radarr_quality')
                                        updated = True
                                    if meta.get('radarrpath') and getattr(mv_row, 'radarrpath', None) != meta.get('radarrpath'):
                                        mv_row.radarrpath = meta.get('radarrpath')
                                        updated = True
                                else:
                                    if getattr(mv_row, 'moviefile_path', None) is not None:
                                        mv_row.moviefile_path = None
                                        updated = True
                                    if getattr(mv_row, 'moviefile_size', None) is not None:
                                        mv_row.moviefile_size = None
                                        updated = True
                                    if getattr(mv_row, 'radarr_quality', None) is not None:
                                        mv_row.radarr_quality = None
                                        updated = True
                                    if getattr(mv_row, 'radarrpath', None) is not None:
                                        mv_row.radarrpath = None
                                        updated = True
                            except Exception:
                                pass
                            if updated:
                                session.add(mv_row)
                                session.commit()
                            job.status = 'DONE'
                            session.add(job)
                            session.commit()
                    except Exception as e:
                        logger.error(f"resolve_moviefile job {job.id} failed: {e}", extra={'emoji_type':'error'})
                        job.status = 'FAILED'
                        job.error_message = str(e)
                        session.add(job)
                        session.commit()
            except Exception as e:
                logger.error(f"resolve_moviefile job {job.id} failed outer: {e}", extra={'emoji_type':'error'})
                job.status = 'FAILED'
                job.error_message = str(e)
                session.add(job)
                session.commit()
        elif job.job_type == 'attach_dummypaths':
            try:
                process_attach_dummypaths_job(session, job)
            except Exception as e:
                logger.error(f"attach_dummypaths job {getattr(job,'id',None)} handler raised: {e}", extra={'emoji_type':'error'})
                job.status = 'FAILED'
                job.error_message = str(e)
                session.add(job)
                session.commit()
        elif job.job_type == 'attach_moviedummypath':
            try:
                process_attach_moviedummypath_job(session, job)
            except Exception as e:
                logger.error(f"attach_moviedummypath job {getattr(job,'id',None)} handler raised: {e}", extra={'emoji_type':'error'})
        elif job.job_type == 'determine_placeholder':
            try:
                # Lightweight determination job: persist decision for a single entity
                payload = job.payload or {}
                from services.deciders import maybe_persist_decision_for_movie, maybe_persist_decision_for_episode
                # Movie path
                if payload.get('movie_id'):
                    mid = payload.get('movie_id')
                    try:
                        mv = session.query(Movie).get(int(mid))
                    except Exception:
                        mv = session.query(Movie).get(mid)
                    if mv:
                        # If there's an active enrichment job for this movie (by tmdb),
                        # defer determination to avoid racing with enrichment.
                        try:
                            from services.postgres.models import Job as JobModel
                            enrich_group = f"enrich:movie:{getattr(mv,'tmdbid',None)}"
                            active = session.query(JobModel).filter(JobModel.job_type == 'enrichment', JobModel.group_id == enrich_group, JobModel.status.in_(['PENDING','CLAIMED','WORKING'])).first()
                            if active:
                                # Requeue determine job a bit later and commit
                                delay = int(getattr(settings, 'ENRICHMENT_LOCK_REQUEUE_SECONDS', 2) or 2)
                                job.run_after = datetime.now() + timedelta(seconds=delay)
                                job.status = 'PENDING'
                                session.add(job)
                                session.commit()
                                try:
                                    from services.utils import format_movie_label
                                    label = format_movie_label(mv)
                                except Exception:
                                    label = f"movie.id={getattr(mv,'id',None)}"
                                logger.verbose(f"Deferring determine_placeholder for {label} because enrichment job {active.id} is active; requeued +{delay}s", extra={'emoji_type': 'wait'})
                                return True
                        except Exception:
                            pass
                        # Compute canonical determination and ensure it is persisted.
                        try:
                            from services.deciders import compute_canonical_determination_for_movie
                            prev_det = getattr(mv, 'determination', None)
                            computed = compute_canonical_determination_for_movie(session=session, movie=mv, is_4k=bool(getattr(mv, 'is_4k', False)))
                            if computed == 'unknown':
                                logger.debug(f"determine_placeholder: computed 'unknown' for movie {mid}; skipping persist", extra={'emoji_type':'debug'})
                            else:
                                # Persist and commit immediately so verification reads the DB state
                                maybe_persist_decision_for_movie(session=session, movie=mv, extra_meta={}, is_4k=bool(getattr(mv, 'is_4k', False)), commit=True)
                                # refresh and verify persistence
                                try:
                                    session.refresh(mv)
                                except Exception:
                                    pass
                                new_det = getattr(mv, 'determination', None)
                                if new_det != computed:
                                    # persistence didn't take; requeue to try again later
                                    delay = int(getattr(settings, 'DETERMINE_RETRY_SECONDS', 5) or 5)
                                    job.run_after = datetime.now() + timedelta(seconds=delay)
                                    job.status = 'PENDING'
                                    session.add(job)
                                    session.commit()
                                    logger.warning(f"determine_placeholder: persistence mismatch for movie {mid} (computed={computed} prev={prev_det} new={new_det}); requeued +{delay}s", extra={'emoji_type':'warning'})
                                    return True
                                else:
                                    # successful persist
                                    try:
                                        _DETERMINE_SUMMARY['total'] += 1
                                        _DETERMINE_SUMMARY['persisted'] += 1
                                    except Exception:
                                        pass
                        except Exception:
                            logger.debug(f"determine_placeholder: failed to compute/persist decision for movie {mid}", extra={'emoji_type':'debug'})

                # Episode path (direct)
                elif payload.get('episode_id'):
                    try:
                        ep_id = payload.get('episode_id')
                        from services.postgres.models import Episode as EpisodeModel, Season as SeasonModel, Series as SeriesModel
                        try:
                            ep = session.query(EpisodeModel).get(int(ep_id))
                        except Exception:
                            ep = session.query(EpisodeModel).get(ep_id)
                        if ep:
                            # Defer if enrichment for the series/episode is active
                            try:
                                from services.postgres.models import Job as JobModel
                                # enrichment group naming can vary; attempt to check by series tvdb if available
                                enrich_group = None
                                try:
                                    # prefer sonarr/arr ids where available
                                    enrich_group = f"enrich:tv:{getattr(session.query(SeriesModel).get(getattr(ep, 'series_id', None)),'tvdbid',None)}"
                                except Exception:
                                    enrich_group = None
                                active = None
                                if enrich_group:
                                    active = session.query(JobModel).filter(JobModel.job_type == 'enrichment', JobModel.group_id == enrich_group, JobModel.status.in_(['PENDING','CLAIMED','WORKING'])).first()
                                if active:
                                    delay = int(getattr(settings, 'ENRICHMENT_LOCK_REQUEUE_SECONDS', 2) or 2)
                                    job.run_after = datetime.now() + timedelta(seconds=delay)
                                    job.status = 'PENDING'
                                    session.add(job)
                                    session.commit()
                                    try:
                                        from services.utils import format_episode_label
                                        label = format_episode_label(series=session.query(SeriesModel).get(getattr(ep, 'series_id', None)), season=session.query(SeasonModel).get(getattr(ep, 'season_id', None)), episode=ep)
                                    except Exception:
                                        label = f"episode.id={getattr(ep,'id',None)}"
                                    logger.verbose(f"Deferring determine_placeholder for {label} because enrichment job {active.id} is active; requeued +{delay}s", extra={'emoji_type': 'wait'})
                                    return True
                            except Exception:
                                pass
                            # Compute canonical determination and ensure persistence for the episode.
                            try:
                                from services.postgres.models import Series as SeriesModel2, Season as SeasonModel2
                                from services.deciders import compute_canonical_determination_for_episode
                                series_row = None
                                season_row = None
                                try:
                                    series_row = session.query(SeriesModel2).get(getattr(ep, 'series_id', None))
                                except Exception:
                                    series_row = None
                                try:
                                    season_row = session.query(SeasonModel2).get(getattr(ep, 'season_id', None))
                                except Exception:
                                    season_row = None
                                prev_det = getattr(ep, 'determination', None)
                                computed = compute_canonical_determination_for_episode(session=session, series=series_row, season=season_row, episode=ep, is_4k=bool(getattr(ep, 'is_4k', False)))
                                if computed == 'unknown':
                                    logger.debug(f"determine_placeholder: computed 'unknown' for episode {getattr(ep,'id',None)}; skipping persist", extra={'emoji_type':'debug'})
                                else:
                                    # Persist and commit immediately so verification reads the DB state
                                    maybe_persist_decision_for_episode(session=session, series=series_row, season=season_row, episode=ep, extra_meta={}, is_4k=bool(getattr(ep, 'is_4k', False)), commit=True)
                                    try:
                                        session.refresh(ep)
                                    except Exception:
                                        pass
                                    new_det = getattr(ep, 'determination', None)
                                    if new_det != computed:
                                        delay = int(getattr(settings, 'DETERMINE_RETRY_SECONDS', 5) or 5)
                                        job.run_after = datetime.now() + timedelta(seconds=delay)
                                        job.status = 'PENDING'
                                        session.add(job)
                                        session.commit()
                                        logger.warning(f"determine_placeholder: persistence mismatch for episode {getattr(ep,'id',None)} (computed={computed} prev={prev_det} new={new_det}); requeued +{delay}s", extra={'emoji_type':'warning'})
                                        return True
                                    else:
                                        try:
                                            _DETERMINE_SUMMARY['total'] += 1
                                            _DETERMINE_SUMMARY['persisted'] += 1
                                        except Exception:
                                            pass
                            except Exception:
                                logger.debug(f"determine_placeholder: failed to compute/persist decision for episode {ep_id}", extra={'emoji_type':'debug'})
                    except Exception:
                        logger.debug("determine_placeholder: episode path parsing failed", extra={'emoji_type':'debug'})

                # TV path
                elif payload.get('series_id') and payload.get('season') and payload.get('episode'):
                    try:
                        sid = payload.get('series_id')
                        season_idx = payload.get('season')
                        ep_idx = payload.get('episode')
                        from services.postgres.models import Season as SeasonModel, Episode as EpisodeModel
                        season = session.query(SeasonModel).filter(SeasonModel.series_id == sid, SeasonModel.season_number == int(season_idx)).first()
                        ep = None
                        if season:
                            ep = session.query(EpisodeModel).filter(EpisodeModel.season_id == season.id, EpisodeModel.episode_number == int(ep_idx)).first()
                        if ep and season:
                            try:
                                # Defer if enrichment for the series is active
                                try:
                                    from services.postgres.models import Job as JobModel
                                    enrich_group = f"enrich:tv:{getattr(session.query(__import__('services.postgres.models', fromlist=['Series']).Series).get(sid),'tvdbid',None)}"
                                    active = session.query(JobModel).filter(JobModel.job_type == 'enrichment', JobModel.group_id == enrich_group, JobModel.status.in_(['PENDING','CLAIMED','WORKING'])).first()
                                    if active:
                                        delay = int(getattr(settings, 'ENRICHMENT_LOCK_REQUEUE_SECONDS', 2) or 2)
                                        job.run_after = datetime.now() + timedelta(seconds=delay)
                                        job.status = 'PENDING'
                                        session.add(job)
                                        session.commit()
                                        try:
                                            from services.utils import format_episode_label
                                            label = format_episode_label(series=session.query(__import__('services.postgres.models', fromlist=['Series']).Series).get(sid), season=season, episode=ep)
                                        except Exception:
                                            label = f"episode.id={getattr(ep,'id',None)}"
                                        logger.verbose(f"Deferring determine_placeholder for {label} because enrichment job {active.id} is active; requeued +{delay}s", extra={'emoji_type': 'wait'})
                                        return True
                                except Exception:
                                    pass
                                # Compute and persist canonical determination for the TV episode.
                                try:
                                    series_row = session.query(__import__('services.postgres.models', fromlist=['Series']).Series).get(sid)
                                except Exception:
                                    series_row = None
                                try:
                                    maybe_persist_decision_for_episode(session=session, series=series_row, season=season, episode=ep, extra_meta={}, is_4k=bool(getattr(ep, 'is_4k', False)))
                                except Exception:
                                    logger.debug(f"determine_placeholder: failed to compute/persist decision for episode {getattr(ep,'id',None)}", extra={'emoji_type':'debug'})
                            except Exception:
                                logger.debug(f"determine_placeholder: failed to compute/persist decision for episode {ep.id}", extra={'emoji_type':'debug'})
                    except Exception:
                        logger.debug("determine_placeholder: tv path parsing failed", extra={'emoji_type':'debug'})

                # If payload only has series_id (bulk), attempt to run determination for all episodes without files in that series
                elif payload.get('series_id') and not payload.get('season') and not payload.get('episode'):
                    try:
                        sid = payload.get('series_id')
                        from services.postgres.models import Season as SeasonModel, Episode as EpisodeModel, Series as SeriesModel
                        series = session.query(SeriesModel).get(sid)
                        if series:
                            # iterate episodes that look like placeholders
                            seasons = session.query(SeasonModel).filter_by(series_id=series.id).all()
                            for srow in seasons:
                                eps = session.query(EpisodeModel).filter_by(season_id=srow.id).all()
                                for ep in eps:
                                    try:
                                        # Only persist canonical determinations for episodes that are relevant
                                        try:
                                            if getattr(ep, 'placeholder_exists', False) or getattr(ep, 'dummypath', None) or not getattr(ep, 'is_deleted', False):
                                                maybe_persist_decision_for_episode(session=session, series=series, season=srow, episode=ep, extra_meta={}, is_4k=bool(getattr(ep, 'is_4k', False)))
                                        except Exception:
                                            continue
                                    except Exception:
                                        continue
                    except Exception:
                        logger.debug("determine_placeholder: bulk series determination failed", extra={'emoji_type':'debug'})

                job.status = 'DONE'
                session.add(job)
                session.commit()
            except Exception as e:
                logger.error(f"determine_placeholder job {job.id} failed: {e}", extra={'emoji_type':'error'})
                job.status = 'FAILED'
                job.error_message = str(e)
                session.add(job)
                session.commit()
                job.status = 'FAILED'
                job.error_message = str(e)
                session.add(job)
                session.commit()
        elif job.job_type == 'global_workflow':
            try:
                from services.global_workflow import process_global_workflow_job
                process_global_workflow_job(session, job)
            except Exception as e:
                logger.error(f"global_workflow job {getattr(job,'id',None)} handler raised: {e}", extra={'emoji_type': 'error'})
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
