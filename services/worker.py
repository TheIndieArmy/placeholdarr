import time
from services.jobs import claim_jobs, requeue_job, job_done
from services.enrich import process_enrich_base_subflow, enrich_episode
from services.postgres.db import get_session
from services.postgres.models import Job as JobModel
from services.postgres.models import SubFlow
from core.logger import logger
from services.jobs import insert_job, insert_job_with_session


def _handle_claimed_job(job):
    job_id = job['id']
    job_type = job['job_type']
    payload = job['payload'] or {}
    try:
        if job_type == 'subjob:enrich_base':
            subflow_id = payload.get('subflow_id')
            if not subflow_id:
                logger.info(f"Job {job_id} missing subflow_id; marking FAILED")
                job_done(job_id, success=False, error_message='missing_subflow_id')
                return False
            # Guard: ensure the SubFlow is at the expected step_index and phase
            session = get_session()
            try:
                sf = session.query(SubFlow).filter(SubFlow.id == int(subflow_id)).first()
            finally:
                try:
                    session.close()
                except Exception:
                    pass

            if not sf:
                logger.info(f"Job {job_id} references missing SubFlow {subflow_id}; marking DONE to avoid retries")
                job_done(job_id, success=True)
                return True

            # Parse expected phase from SubFlow.steps using step_index
            try:
                steps = (sf.steps or '').split(',')
                expected_step_index = int(payload.get('step_index', 0))
                expected_phase = payload.get('phase')
                actual_phase = steps[sf.step_index] if 0 <= sf.step_index < len(steps) else None
            except Exception:
                actual_phase = None
                expected_phase = payload.get('phase')

            # If the job is for a later/earlier phase than the SubFlow currently is, requeue briefly
            if actual_phase != expected_phase or sf.step_index != expected_step_index:
                logger.info(f"Job {job_id} (phase={expected_phase} idx={expected_step_index}) is not ready: subflow {sf.id} is at phase={actual_phase} idx={sf.step_index}; requeueing")
                requeue_job(job_id, delay_seconds=3)
                return False

            # Execute the current phase handler
            ok = process_enrich_base_subflow(subflow_id)
            if ok:
                # Advance the SubFlow.step_index and enqueue next-phase job if any
                session = get_session()
                try:
                        sfrow = session.query(SubFlow).filter(SubFlow.id == int(subflow_id)).with_for_update().first()
                        if sfrow:
                            try:
                                steps = (sfrow.steps or '').split(',')
                                prev_idx = int(sfrow.step_index or 0)
                                # compute next index; only advance if we are not already at the final step
                                if prev_idx + 1 < len(steps):
                                    new_idx = prev_idx + 1
                                    sfrow.step_index = new_idx
                                    session.add(sfrow)
                                    session.commit()

                                    # enqueue the phase at new_idx
                                    next_phase = steps[new_idx]
                                    payload_next = {'run_id': payload.get('run_id'), 'phase': next_phase, 'subflow_id': sfrow.id, 'step_index': new_idx}
                                    group_id = f"subflow:{sfrow.id}:{next_phase}"
                                    # Insert next job using the same session for atomicity
                                    insert_job_with_session(session, f'subjob:{next_phase}', payload_next, group_id=group_id)
                                else:
                                    # already at last step; nothing to enqueue
                                    session.commit()
                            except Exception:
                                session.rollback()
                finally:
                    try:
                        session.close()
                    except Exception:
                        pass

                job_done(job_id, success=True)
                return True
            else:
                # transient failure; requeue with backoff
                # try to read max_attempts from the Job row
                try:
                    session = get_session()
                    jrow = session.query(JobModel).filter(JobModel.id == job_id).first()
                    max_attempts = jrow.max_attempts if jrow else 5
                finally:
                    try:
                        session.close()
                    except Exception:
                        pass
                requeue_job(job_id, delay_seconds=10)
                return False
        elif job_type == 'reenrich:episode' or payload.get('episode_id'):
            episode_id = payload.get('episode_id')
            if not episode_id:
                logger.info(f"Job {job_id} missing episode_id; marking FAILED")
                job_done(job_id, success=False, error_message='missing_episode_id')
                return False
            ok = enrich_episode(int(episode_id))
            if ok:
                job_done(job_id, success=True)
                return True
            else:
                # transient failure; requeue
                try:
                    session = get_session()
                    jrow = session.query(JobModel).filter(JobModel.id == job_id).first()
                    max_attempts = jrow.max_attempts if jrow else 5
                finally:
                    try:
                        session.close()
                    except Exception:
                        pass
                requeue_job(job_id, delay_seconds=15)
                return False
        else:
            logger.info(f"Unhandled job_type: {job_type}")
            job_done(job_id, success=False, error_message=f'unhandled_job_type:{job_type}')
            return False
    except Exception as exc:
        logger.exception(f"Exception while processing job {job_id}: {exc}")
        try:
            requeue_job(job_id, delay_seconds=30)
        except Exception:
            pass
        job_done(job_id, success=False, error_message=str(exc))
        return False


def run_once(limit: int = 10):
    claimed = claim_jobs(limit=limit)
    if not claimed:
        return 0
    processed = 0
    for job in claimed:
        ok = _handle_claimed_job(job)
        if ok:
            processed += 1
    return processed


def run_loop(poll_interval: float = 1.0):
    logger.info('Worker loop starting')
    try:
        while True:
            processed = run_once(limit=10)
            if processed == 0:
                time.sleep(poll_interval)
    except KeyboardInterrupt:
        logger.info('Worker loop stopped by KeyboardInterrupt')
