"""Scheduled/manual runner for the collections_sync maintenance task."""
from __future__ import annotations

from core.logger import logger
from services.collections.engine import run_all_enabled_recipes
from services.task_run_history import begin_task_run, finish_task_run, get_working_run, record_skipped_task_run


def run_collections_sync(*, trigger: str = "scheduled") -> dict:
    """Run all enabled collection recipes under a tracked task run."""
    if get_working_run("collections_sync"):
        record_skipped_task_run(
            task_key="collections_sync",
            trigger=trigger,
            skip_reason="collections sync already running",
        )
        return {"status": "skipped"}

    run_id = begin_task_run(task_key="collections_sync", trigger=trigger)
    try:
        from core.config import settings

        default_hours = max(1, int(getattr(settings, "COLLECTIONS_SYNC_INTERVAL_HOURS", 24) or 24))
        # Manual trigger forces all active recipes; scheduled ticks only run due ones
        # (the job ticks at the smallest per-recipe interval, so most ticks skip most recipes).
        results = run_all_enabled_recipes(force=(trigger == "manual"), default_interval_hours=default_hours)
        status = "done" if results.get("failed", 0) == 0 else "failed"
        error_message = None
        if status == "failed":
            error_message = f"{results['failed']} of {results['total']} recipe(s) failed"
        finish_task_run(run_id, status=status, summary={"collections": results}, error_message=error_message)
        from services.source_of_truth.scheduler import reschedule_task_after_completion

        reschedule_task_after_completion("collections_sync")
        return results
    except Exception as exc:
        logger.error(f"Collections sync run failed: {exc}", extra={"emoji_type": "error"})
        finish_task_run(run_id, status="failed", error_message=str(exc))
        raise
