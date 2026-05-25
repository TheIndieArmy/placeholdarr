import logging
from datetime import datetime, timezone
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler

from core.config import settings
from services.source_of_truth.scheduled_sync import run_lite_sync, run_scheduled_full_sync
from services.task_schedule_state import (
    bump_next_run_after_run,
    get_persisted_next_run,
    persist_next_run,
    resolve_next_run_time,
)


logger = logging.getLogger("services.source_of_truth.scheduler")
_scheduler: BackgroundScheduler | None = None

JOB_ID_FULL = "source_of_truth:all_arrs"
JOB_ID_LITE = "source_of_truth:lite_sync"
JOB_ID_CALENDAR = "source_of_truth:calendar_date_refresh"


def _interval_hours_for(task_key: str) -> int:
    if task_key == "full_sync":
        return max(0, int(getattr(settings, "FULL_SYNC_INTERVAL_HOURS", 0) or 0))
    if task_key == "lite_sync":
        return max(0, int(getattr(settings, "LITE_SYNC_INTERVAL_HOURS", 0) or 0))
    return 0


def _start_interval(
    task_key: str,
    interval_hours: int,
    target,
    label: str,
    *,
    job_id: str,
    disable_hint: str,
) -> None:
    if interval_hours <= 0:
        logger.info(f"{label} scheduler disabled ({disable_hint} <= 0)")
        return

    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler()

    safe_interval_hours = max(1, int(interval_hours))
    if safe_interval_hours != int(interval_hours):
        logger.warning(
            f"{label} interval clamped from {interval_hours}h to {safe_interval_hours}h (minimum is 1h)"
        )

    next_run_time = resolve_next_run_time(task_key, safe_interval_hours)

    try:
        _scheduler.add_job(
            target,
            "interval",
            hours=safe_interval_hours,
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            next_run_time=next_run_time,
        )
        logger.info(
            f"{label} scheduler started: every {safe_interval_hours}h, next_run={next_run_time.isoformat()}",
        )
    except Exception:
        logger.exception(f"Failed to start scheduler for {label}")


def reschedule_task_after_completion(task_key: str, *, completed_at: datetime | None = None) -> None:
    """Persist and apply the next run time after manual or scheduled completion."""
    hours = _interval_hours_for(task_key)
    if hours <= 0:
        return
    nxt = bump_next_run_after_run(task_key, hours, completed_at=completed_at)
    if _scheduler is None:
        return
    job_id = JOB_ID_FULL if task_key == "full_sync" else JOB_ID_LITE if task_key == "lite_sync" else None
    if not job_id:
        return
    try:
        job = _scheduler.get_job(job_id)
        if job:
            job.reschedule(trigger="interval", hours=max(1, hours), next_run_time=nxt)
            logger.info(
                "Rescheduled %s after completion: next_run=%s",
                task_key,
                nxt.isoformat(),
                extra={"emoji_type": "info"},
            )
    except Exception:
        logger.exception("Failed to reschedule job %s", job_id)


def _run_all_syncs_scheduled():
    run_scheduled_full_sync(trigger="scheduled")


def _run_lite_sync_scheduled():
    run_lite_sync(trigger="scheduled")


def schedule_all_syncs():
    """Schedule interval-based full and lite sync jobs using persisted next-run times."""
    global _scheduler

    full_hours = int(getattr(settings, "FULL_SYNC_INTERVAL_HOURS", 0) or 0)
    _start_interval(
        "full_sync",
        full_hours,
        _run_all_syncs_scheduled,
        "All ARRs (full sync)",
        job_id=JOB_ID_FULL,
        disable_hint="FULL_SYNC_INTERVAL_HOURS",
    )

    lite_hours = int(getattr(settings, "LITE_SYNC_INTERVAL_HOURS", 0) or 0)
    _start_interval(
        "lite_sync",
        lite_hours,
        _run_lite_sync_scheduled,
        "Lite sync",
        job_id=JOB_ID_LITE,
        disable_hint="LITE_SYNC_INTERVAL_HOURS",
    )

    calendar_hours = int(getattr(settings, "CALENDAR_SYNC_INTERVAL_HOURS", 0) or 0)
    if lite_hours <= 0 and calendar_hours > 0:
        from services.source_of_truth.scheduled_sync import run_calendar_only_maintenance

        def _legacy_calendar_scheduled():
            run_calendar_only_maintenance(trigger="scheduled")

        if _scheduler is None:
            _scheduler = BackgroundScheduler()
        safe_calendar = max(1, int(calendar_hours))
        cal_next = resolve_next_run_time("lite_sync", safe_calendar)
        try:
            _scheduler.add_job(
                _legacy_calendar_scheduled,
                "interval",
                hours=safe_calendar,
                id=JOB_ID_CALENDAR,
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                next_run_time=cal_next,
            )
        except Exception:
            logger.exception("Failed to start legacy calendar scheduler")
    elif calendar_hours > 0 and lite_hours > 0:
        logger.info(
            "CALENDAR_SYNC_INTERVAL_HOURS ignored while LITE_SYNC_INTERVAL_HOURS is enabled "
            "(lite sync includes calendar date refresh and calendar phase)",
            extra={"emoji_type": "info"},
        )

    if _scheduler and not _scheduler.running:
        _scheduler.start()


def get_scheduled_task_metadata() -> dict[str, Any]:
    """Expose interval and next-run for Tasks UI (persisted + live job)."""
    out: dict[str, Any] = {
        "full_sync": {"enabled": False, "interval_hours": 0, "next_run": None},
        "lite_sync": {"enabled": False, "interval_hours": 0, "next_run": None},
    }
    for task_key in ("full_sync", "lite_sync"):
        hours = _interval_hours_for(task_key)
        out[task_key]["interval_hours"] = hours
        out[task_key]["enabled"] = hours > 0

        persisted = get_persisted_next_run(task_key)
        if persisted:
            nrt = persisted
            if nrt.tzinfo is None:
                nrt = nrt.replace(tzinfo=timezone.utc)
            out[task_key]["next_run"] = nrt.astimezone(timezone.utc).isoformat()

        if _scheduler is not None:
            job_id = JOB_ID_FULL if task_key == "full_sync" else JOB_ID_LITE
            try:
                job = _scheduler.get_job(job_id)
                if job and job.next_run_time:
                    nrt = job.next_run_time
                    if getattr(nrt, "tzinfo", None) is None:
                        nrt = nrt.replace(tzinfo=timezone.utc)
                    iso = nrt.astimezone(timezone.utc).isoformat()
                    out[task_key]["next_run"] = iso
                    persist_next_run(task_key, nrt)
            except Exception:
                pass
    return out
