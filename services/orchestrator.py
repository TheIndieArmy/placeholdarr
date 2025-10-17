from datetime import datetime, timedelta, timezone
import uuid
from typing import List

from services.postgres.db import get_session
from services.postgres.models import Job, Movie, SubFlow
from sqlalchemy import text


# Minimal orchestrator that does not import services_old. It uses the project's
# Job model directly to create and unlock phase subjobs.

FAR_FUTURE = datetime(2999, 1, 1, tzinfo=timezone.utc)

PHASES = [
    'enrich_base',
    'enrich_files',
    'fs_scan',
    'merge_scan',
    'determine',
    'materialize',
]


def _enqueue_job_local(job_type: str, payload: dict, run_after: datetime = None, group_id: str = None):
    # Use application-level insert helper which dedupes by group_id
    from services.jobs import insert_job
    return insert_job(job_type, payload, group_id=group_id, run_after=run_after)


class OrchestratorRun:
    def __init__(self, run_id: str = None, types: List[str] = None, note: str = None, created_at: datetime = None):
        self.run_id = run_id or f"fullsync:{uuid.uuid4()}"
        self.types = types or ['movie']
        self.note = note
        # Accept an optional created_at value. Prefer DB-provided timestamps when
        # persisting runs; do NOT generate an application-side timestamp by default
        # so persisted records are consistently authored by the DB clock.
        self.created_at = created_at

    def create_phase_subjobs(self, phase: str, item_ids: List[int], payload_extra: dict = None):
        if phase not in PHASES:
            raise ValueError('unknown phase')
        payload_extra = payload_extra or {}
        created_ids = []
        # Resolve existing SubFlow ids for these item_ids (only movie-level here)
        session = get_session()
        try:
            rows = session.query(SubFlow.movie_id, SubFlow.id).filter(SubFlow.movie_id.in_(item_ids)).all()
            sf_by_movie = {r[0]: r[1] for r in rows}
        except Exception:
            sf_by_movie = {}
        finally:
            try:
                session.close()
            except Exception:
                pass
        for iid in item_ids:
            payload = {'run_id': self.run_id, 'phase': phase, 'item_id': iid}
            payload.update(payload_extra)
            # Prefer a subflow-based group id when we can resolve the item's SubFlow id
            group_id = f"item:{phase}:{iid}"
            # If iid corresponds to a movie_id that has a SubFlow, prefer subflow group id
            sfid = sf_by_movie.get(iid)
            if sfid:
                group_id = f"subflow:{sfid}:{phase}"
                payload['subflow_id'] = sfid
            # Let insert_job() default to the database clock (func.now()) so jobs
            # become claimable immediately and there is no application/DB clock skew.
            jid = _enqueue_job_local(job_type=f'subjob:{phase}', payload=payload, run_after=None, group_id=group_id)
            created_ids.append(jid)
        return created_ids

    def unlock_phase(self, phase: str):
        # Set run_after = now() for all jobs in this run+phase so workers can pick them up
        session = get_session()
        try:
            # Update any pending subjob for this run by inspecting the JSON payload's run_id
            sql = text("""
            UPDATE job
            SET run_after = now(), updated_at = now()
            WHERE job_type = :jt AND status = 'PENDING' AND (payload->>'run_id') = :rid
            """)
            session.execute(sql, {'jt': f'subjob:{phase}', 'rid': self.run_id})
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
