"""Run-stage coordinator: unlock phases for an OrchestratorRun sequentially.

This coordinator will unlock each phase (set run_after = now() for that
phase's jobs) and then wait for all jobs of that phase to reach a terminal
state before advancing. It runs in a background thread when invoked by the
caller (list-capture or other run creator).
"""
from time import sleep
from typing import Optional
from core.logger import logger
from services.orchestrator import OrchestratorRun, PHASES
from services.postgres.db import get_session
from sqlalchemy import text


def _nonterminal_count_for_phase(run_id: str, phase: str) -> int:
    session = get_session()
    try:
        sql = text("""
        SELECT count(*) FROM job
        WHERE job_type = :jt AND (payload->>'run_id') = :rid
          AND status IN ('PENDING','CLAIMED','WORKING')
        """)
        row = session.execute(sql, {'jt': f'subjob:{phase}', 'rid': run_id}).fetchone()
        return int(row[0]) if row else 0
    finally:
        try:
            session.close()
        except Exception:
            pass


def coordinate_run(run_id: str, poll_interval: float = 5.0, phase_timeout: Optional[float] = None):
    """Coordinate the given run by unlocking phases sequentially.

    - run_id: the OrchestratorRun.run_id string
    - poll_interval: seconds between checks
    - phase_timeout: optional max seconds to wait for a phase to drain (None = unlimited)
    """
    try:
        run = OrchestratorRun(run_id=run_id)
    except Exception:
        logger.exception(f"Failed to construct OrchestratorRun for {run_id}")
        return

    logger.info(f"Run coordinator starting for {run_id}")
    for phase in PHASES:
        try:
            logger.info(f"Run coordinator: unlocking phase '{phase}' for run {run_id}")
            run.unlock_phase(phase)
        except Exception:
            logger.exception(f"Failed to unlock phase {phase} for run {run_id}")

        # Wait until there are no non-terminal jobs for this phase
        waited = 0.0
        while True:
            try:
                count = _nonterminal_count_for_phase(run_id, phase)
            except Exception:
                logger.exception(f"Error counting jobs for phase {phase} run {run_id}")
                count = 0
            if count == 0:
                logger.info(f"Run coordinator: phase '{phase}' drained for run {run_id}")
                break
            if phase_timeout is not None and waited >= phase_timeout:
                logger.warning(f"Run coordinator: phase '{phase}' timeout after {phase_timeout}s for run {run_id}; advancing anyway")
                break
            sleep(poll_interval)
            waited += poll_interval

    logger.info(f"Run coordinator finished for {run_id}")
