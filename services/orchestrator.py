from datetime import datetime, timedelta
import uuid
from typing import List

from services.postgres.db import get_session
from services.postgres.models import Job, Movie


# Minimal orchestrator that does not import services_old. It uses the project's
# Job model directly to create and unlock phase subjobs.

FAR_FUTURE = datetime(2999, 1, 1)

PHASES = [
    'enrich_base',
    'enrich_files',
    'fs_scan',
    'merge_scan',
    'determine',
    'materialize',
]


def _enqueue_job_local(job_type: str, payload: dict, run_after: datetime = None, group_id: str = None):
    session = get_session()
    try:
        j = Job(job_type=job_type, payload=payload, status='PENDING', run_after=run_after, group_id=group_id)
        session.add(j)
        session.commit()
        # return created job id for caller convenience
        return j.id
    finally:
        session.close()


class OrchestratorRun:
    def __init__(self, run_id: str = None, types: List[str] = None, note: str = None):
        self.run_id = run_id or f"fullsync:{uuid.uuid4()}"
        self.types = types or ['movie']
        self.note = note
        self.created_at = datetime.now()

    def create_phase_subjobs(self, phase: str, item_ids: List[int], payload_extra: dict = None):
        if phase not in PHASES:
            raise ValueError('unknown phase')
        payload_extra = payload_extra or {}
        created_ids = []
        grp = f"{self.run_id}:{phase}"
        for iid in item_ids:
            payload = {'run_id': self.run_id, 'phase': phase, 'item_id': iid}
            payload.update(payload_extra)
            jid = _enqueue_job_local(job_type=f'subjob:{phase}', payload=payload, run_after=FAR_FUTURE, group_id=grp)
            created_ids.append(jid)
        return created_ids

    def unlock_phase(self, phase: str):
        # Set run_after = now() for all jobs in this run+phase so workers can pick them up
        grp = f"{self.run_id}:{phase}"
        session = get_session()
        try:
            now = datetime.now()
            session.query(Job).filter(Job.group_id == grp, Job.status == 'PENDING').update({Job.run_after: now})
            session.commit()
        finally:
            session.close()


def run_fullsync_movie_only_sample(sample_size: int = 10):
    """Create a sample orchestrator run for the first `sample_size` movies and unlock phases sequentially.
    This is a simple smoke helper used during development.
    """
    run = OrchestratorRun(types=['movie'], note='sample run')

    session = get_session()
    try:
        rows = session.query(Movie.id).limit(sample_size).all()
        movie_ids = [r[0] for r in rows]
    finally:
        session.close()

    for phase in PHASES:
        run.create_phase_subjobs(phase, movie_ids)

    # Unlock phases sequentially
    for phase in PHASES:
        run.unlock_phase(phase)

    return run
