from services.postgres.db import get_session
from services.postgres.models import Job
from services.list_capture import process_enrich_base_subflow, process_discover_episodes_subflow
from core.logger import logger
from datetime import datetime
from core.time import now_utc


def claim_pending_jobs(phase: str, limit: int = 100):
    """Claim up to `limit` pending jobs for the given phase (group id prefix is ignored).

    Returns a list of job rows (dicts) for processing.
    """
    session = get_session()
    try:
        # Use UTC-aware now so comparisons match run_after (stored in UTC)
        now = now_utc()
        # Fetch pending jobs whose run_after <= now
        rows = session.query(Job).filter(Job.status == 'PENDING', Job.run_after <= now, Job.job_type == f'subjob:{phase}').limit(limit).all()
        jobs = []
        for r in rows:
            # Capture payload and id into a plain dict to avoid DetachedInstance issues
            jobs.append({'id': r.id, 'payload': r.payload})
            # Mark as claimed to avoid double-processing in this simple worker
            r.status = 'CLAIMED'
            session.add(r)
        session.commit()
        return jobs
    finally:
        session.close()


def process_enrich_base_jobs_once(limit: int = 100):
    # Process both 'enrich_base' and 'discover_episodes' phases via separate claims
    jobs = claim_pending_jobs('enrich_base', limit=limit)
    if not jobs:
        logger.info('No enrich_base jobs claimed')
        return 0

    processed = 0
    for j in jobs:
        payload = j.get('payload') or {}
        subflow_id = payload.get('subflow_id') or payload.get('item_id')
        run_id = payload.get('run_id')
        job_id = j.get('id')
        job_status = 'DONE'
        job_error = None
        try:
            if not subflow_id:
                logger.info(f'Job {job_id} missing subflow_id')
                job_status = 'FAILED'
                job_error = 'missing subflow_id'
            else:
                # dispatch based on job type present in payload phase
                phase = (payload.get('phase') or 'enrich_base')
                if phase == 'discover_episodes':
                    res = process_discover_episodes_subflow(subflow_id, run_id)
                else:
                    # default: handle legacy enrich_base as the enrich handler
                    res = process_enrich_base_subflow(subflow_id, run_id)
                processed += 1
        except Exception as exc:
            logger.info(f'Error processing job {job_id}: {exc}')
            job_status = 'FAILED'
            job_error = str(exc)
        finally:
            session = get_session()
            try:
                job_row = session.query(Job).filter(Job.id == job_id).first()
                if job_row:
                    job_row.status = job_status
                    job_row.error_message = job_error
                    session.add(job_row)
                    session.commit()
            finally:
                session.close()

    logger.info(f'Processed {processed} enrich_base jobs')
    return processed


if __name__ == '__main__':
    # Simple entrypoint for local testing
    process_enrich_base_jobs_once(limit=200)
