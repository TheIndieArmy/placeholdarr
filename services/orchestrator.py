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
    # global enrich+merge phase is next; episode creation is performed
    # inline during the series `enrich_base` handler so we don't need a
    # dedicated create_episode_subflows phase here.
    # global FS-scan is centralized and should not be a per-item phase;
    # workers will trigger the idempotent run-level scanner when appropriate.
    'enrich_and_merge',
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
    def create_phase_subjobs(self, phase: str, item_ids: List[int], payload_extra: dict = None, run_after: datetime = None):
        """Create deduped phase jobs for the provided item ids.

        By default this inserts jobs with run_after = FAR_FUTURE so they are not
        claimable until the run coordinator unlocks the phase. Caller may pass
        an explicit run_after to override.
        """
        if phase not in PHASES:
            raise ValueError('unknown phase')
        payload_extra = payload_extra or {}
        created_ids = []
        # Resolve existing SubFlow ids for these item_ids. Item ids may refer
        # to movies, series or episodes; attempt to map any matching SubFlow by
        # movie_id, series_id or episode_id so we can attach subflow_id to the
        # created job payloads (workers expect subflow-bound jobs when possible).
        session = get_session()
        try:
            rows = session.query(SubFlow.movie_id, SubFlow.series_id, SubFlow.episode_id, SubFlow.id).filter(
                (SubFlow.movie_id.in_(item_ids)) | (SubFlow.series_id.in_(item_ids)) | (SubFlow.episode_id.in_(item_ids))
            ).all()
            sf_by_item = {}
            for mv, sv, ev, sfid in rows:
                try:
                    if mv is not None and mv in item_ids:
                        sf_by_item[mv] = sfid
                except Exception:
                    pass
                try:
                    if sv is not None and sv in item_ids:
                        sf_by_item[sv] = sfid
                except Exception:
                    pass
                try:
                    if ev is not None and ev in item_ids:
                        sf_by_item[ev] = sfid
                except Exception:
                    pass
        except Exception:
            sf_by_item = {}
        finally:
            try:
                session.close()
            except Exception:
                pass

        # If no explicit run_after specified, default to FAR_FUTURE to keep jobs
        # pending until unlocked by the coordinator.
        if run_after is None:
            run_after = FAR_FUTURE

        # Compute the step_index from the orchestrator PHASES list
        try:
            step_index = PHASES.index(phase)
        except Exception:
            step_index = 0

        for iid in item_ids:
            payload = {'run_id': self.run_id, 'phase': phase, 'item_id': iid, 'step_index': step_index}
            payload.update(payload_extra)
            # Prefer a subflow-based group id when we can resolve the item's SubFlow id
            group_id = f"item:{phase}:{iid}"
            # If iid corresponds to any SubFlow-mapped id, prefer subflow group id
            sfid = sf_by_item.get(iid)
            # If we have an existing SubFlow for this item, skip creating a
            # phase job when the SubFlow has already advanced beyond the
            # requested phase. This avoids enqueuing obsolete jobs during
            # repeated fullsync runs against the same DB.
            if sfid:
                try:
                    s = get_session()
                    try:
                        sfrow = s.query(SubFlow).filter(SubFlow.id == int(sfid)).first()
                    finally:
                        try:
                            s.close()
                        except Exception:
                            pass
                    if sfrow and getattr(sfrow, 'step_index', 0) > step_index:
                        # SubFlow already past this phase; skip creating job
                        continue
                except Exception:
                    # best-effort: fall back to creating the job if any DB error
                    pass
            if sfid:
                group_id = f"subflow:{sfid}:{phase}"
                payload['subflow_id'] = sfid
            # Insert using the orchestrator's enqueue helper which dedupes by group_id
            jid = _enqueue_job_local(job_type=f'subjob:{phase}', payload=payload, run_after=run_after, group_id=group_id)
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
