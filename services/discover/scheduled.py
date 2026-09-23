"""Scheduled/manual runner for the discover_sync maintenance task."""
from __future__ import annotations

from datetime import datetime, timezone

from core.logger import logger
from services.discover.mode import is_tmdb_discover_mode
from services.discover.pipeline import run_discover_pipeline
from services.task_run_history import (
    begin_task_run,
    finish_task_run,
    get_working_run,
    record_skipped_task_run,
    update_task_run_summary,
)
from services.task_run_phases import TaskRunPhaseTracker, build_progress_from_phases


def run_discover_sync(*, trigger: str = "manual") -> dict:
    """Run the TMDB Discover seed → overlay → determine → materialize pipeline."""
    if not is_tmdb_discover_mode():
        record_skipped_task_run(
            task_key="discover_sync",
            trigger=trigger,
            skip_reason="catalog mode is not tmdb_discover",
        )
        return {"status": "skipped", "reason": "not_discover_mode"}

    if get_working_run("discover_sync"):
        record_skipped_task_run(
            task_key="discover_sync",
            trigger=trigger,
            skip_reason="discover sync already running",
        )
        return {"status": "skipped", "reason": "already_running"}

    run_id = begin_task_run(task_key="discover_sync", trigger=trigger)
    started_at = datetime.now(timezone.utc)
    phases = TaskRunPhaseTracker(run_id, started_at=started_at, mode="discover")
    try:
        logger.info(
            f"Discover catalog sync started (trigger={trigger})",
            extra={"emoji_type": "gear"},
        )
        result = run_discover_pipeline(ensure_default_source=False, phase_tracker=phases)
        update_task_run_summary(
            run_id,
            {
                "mode": "discover",
                "discover": result,
                "phases": phases.phases(),
                "progress": build_progress_from_phases(
                    task_run_id=run_id,
                    mode="discover",
                    started_at=started_at,
                    phases=phases.phases(),
                    overall_status="DONE",
                    completed_at=datetime.now(timezone.utc),
                ),
            },
        )
        finish_task_run(run_id, status="done", summary={"mode": "discover", "discover": result})
        from services.source_of_truth.scheduler import reschedule_task_after_completion

        reschedule_task_after_completion("discover_sync")
        return result
    except Exception as exc:
        logger.error(f"Discover sync run failed: {exc}", extra={"emoji_type": "error"})
        finish_task_run(run_id, status="failed", error_message=str(exc), summary={"mode": "discover"})
        raise
