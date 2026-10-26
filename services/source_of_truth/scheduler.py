import logging
from datetime import datetime, timezone
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from services.app_timezone import get_app_zone, parse_hhmm
from services.source_of_truth.scheduled_sync import run_lite_sync, run_scheduled_full_sync
from services.task_schedule import (
    any_calendar_mode,
    describe_schedule,
    get_schedule_for_task,
    normalize_schedule,
    schedule_enabled,
    schedule_interval_hours,
    smallest_interval_hours_among,
)
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
JOB_ID_COLLECTIONS = "collections:sync"


def _ensure_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone=get_app_zone())
    return _scheduler


def _job_id_for(task_key: str) -> str | None:
    if task_key == "full_sync":
        return JOB_ID_FULL
    if task_key == "lite_sync":
        return JOB_ID_LITE
    if task_key == "collections_sync":
        return JOB_ID_COLLECTIONS
    return None


def _collections_parent_schedule() -> dict[str, Any]:
    """Effective schedule used to tick the collections APScheduler job."""
    global_sched = get_schedule_for_task("collections_sync")
    if not schedule_enabled(global_sched):
        return global_sched

    recipe_schedules: list[dict[str, Any] | None] = [global_sched]
    try:
        from services.collections.engine import enabled_recipe_schedules

        recipe_schedules.extend(enabled_recipe_schedules())
    except Exception:
        pass

    if any_calendar_mode(recipe_schedules):
        # Hourly tick; per-recipe due gating decides who actually runs.
        return {
            "enabled": True,
            "mode": "interval",
            "interval_hours": 1,
            "time": None,
            "weekdays": None,
            "weekday": None,
            "monthday": None,
            "cron": None,
        }

    smallest = smallest_interval_hours_among(recipe_schedules)
    if smallest and smallest > 0:
        hours = schedule_interval_hours(global_sched) or smallest
        return {
            "enabled": True,
            "mode": "interval",
            "interval_hours": min(hours, smallest) if hours else smallest,
            "time": None,
            "weekdays": None,
            "weekday": None,
            "monthday": None,
            "cron": None,
        }
    return global_sched


def _trigger_for_schedule(sched: dict[str, Any]):
    mode = str(sched.get("mode") or "interval")
    zone = get_app_zone()
    if mode == "interval":
        hours = max(1, schedule_interval_hours(sched))
        return IntervalTrigger(hours=hours, timezone=zone)
    if mode == "cron":
        expr = str(sched.get("cron") or "").strip()
        return CronTrigger.from_crontab(expr, timezone=zone)

    hour, minute = parse_hhmm(sched.get("time") or "03:00")
    if mode == "daily":
        return CronTrigger(hour=hour, minute=minute, timezone=zone)
    if mode == "weekly":
        # APScheduler cron: mon=0 … sun=6 matches our weekday ints.
        days = list(sched.get("weekdays") or [])
        if not days and sched.get("weekday") is not None:
            days = [int(sched["weekday"])]
        if not days:
            days = [0]
        dow = ",".join(str(int(d)) for d in sorted({int(d) for d in days if 0 <= int(d) <= 6}))
        return CronTrigger(day_of_week=dow, hour=hour, minute=minute, timezone=zone)
    if mode == "monthly":
        # Cron day=N skips short months; next_run resolution clamps via clamp_month_day.
        md = max(1, min(31, int(sched.get("monthday") or 1)))
        return CronTrigger(day=md, hour=hour, minute=minute, timezone=zone)
    return IntervalTrigger(hours=24, timezone=zone)


def _start_scheduled_job(
    task_key: str,
    sched: dict[str, Any],
    target,
    label: str,
    *,
    job_id: str,
) -> None:
    if not schedule_enabled(sched):
        logger.info("%s scheduler disabled", label)
        sched_obj = _ensure_scheduler()
        try:
            if sched_obj.get_job(job_id):
                sched_obj.remove_job(job_id)
        except Exception:
            pass
        return

    scheduler = _ensure_scheduler()
    next_run_time = resolve_next_run_time(task_key, sched)
    trigger = _trigger_for_schedule(sched)

    try:
        scheduler.add_job(
            target,
            trigger=trigger,
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            next_run_time=next_run_time,
        )
        logger.info(
            "%s scheduler started: %s, next_run=%s",
            label,
            describe_schedule(sched),
            next_run_time.isoformat(),
        )
    except Exception:
        logger.exception("Failed to start scheduler for %s", label)


def _apply_next_run_to_job(
    task_key: str,
    nxt: datetime,
    sched: dict[str, Any],
    *,
    log_label: str = "after completion",
) -> None:
    if _scheduler is None:
        return
    job_id = _job_id_for(task_key)
    if not job_id:
        return
    try:
        job = _scheduler.get_job(job_id)
        if job:
            if nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=timezone.utc)
            _scheduler.modify_job(
                job_id,
                trigger=_trigger_for_schedule(sched),
                next_run_time=nxt,
            )
            logger.info(
                "Rescheduled %s %s: next_run=%s",
                task_key,
                log_label,
                nxt.isoformat(),
                extra={"emoji_type": "info"},
            )
    except Exception:
        logger.exception("Failed to reschedule job %s", job_id)


def reschedule_task_after_completion(task_key: str, *, completed_at: datetime | None = None) -> None:
    """Persist and apply the next run time after manual or scheduled completion."""
    if task_key == "collections_sync":
        sched = _collections_parent_schedule()
    else:
        sched = get_schedule_for_task(task_key)
    if not schedule_enabled(sched):
        return
    nxt = bump_next_run_after_run(task_key, sched, completed_at=completed_at)
    _apply_next_run_to_job(task_key, nxt, sched)


def reschedule_after_full_sync_success(*, completed_at: datetime | None = None) -> None:
    """Full sync subsumes lite: advance both schedules from completion."""
    reschedule_task_after_completion("full_sync", completed_at=completed_at)
    if schedule_enabled(get_schedule_for_task("lite_sync")):
        reschedule_task_after_completion("lite_sync", completed_at=completed_at)


def _run_all_syncs_scheduled():
    run_scheduled_full_sync(trigger="scheduled")


def _run_lite_sync_scheduled():
    run_lite_sync(trigger="scheduled")


def _run_collections_sync_scheduled():
    from services.collections.scheduled import run_collections_sync

    run_collections_sync(trigger="scheduled")


def refresh_collections_schedule() -> None:
    """Re-apply the collections job after recipe schedule overrides change."""
    _start_scheduled_job(
        "collections_sync",
        _collections_parent_schedule(),
        _run_collections_sync_scheduled,
        "Collections sync",
        job_id=JOB_ID_COLLECTIONS,
    )


def refresh_all_schedules() -> None:
    """Rebuild full/lite/collections jobs from current settings (no process restart)."""
    schedule_all_syncs()


def schedule_all_syncs():
    """Schedule full, lite, and collections jobs from JSON schedules."""
    global _scheduler

    _start_scheduled_job(
        "full_sync",
        get_schedule_for_task("full_sync"),
        _run_all_syncs_scheduled,
        "All ARRs (full sync)",
        job_id=JOB_ID_FULL,
    )

    lite_sched = get_schedule_for_task("lite_sync")
    _start_scheduled_job(
        "lite_sync",
        lite_sched,
        _run_lite_sync_scheduled,
        "Lite sync",
        job_id=JOB_ID_LITE,
    )

    _start_scheduled_job(
        "collections_sync",
        _collections_parent_schedule(),
        _run_collections_sync_scheduled,
        "Collections sync",
        job_id=JOB_ID_COLLECTIONS,
    )

    from core.config import settings

    calendar_hours = int(getattr(settings, "CALENDAR_SYNC_INTERVAL_HOURS", 0) or 0)
    lite_on = schedule_enabled(lite_sched)
    if not lite_on and calendar_hours > 0:
        from services.source_of_truth.scheduled_sync import run_calendar_only_maintenance

        def _legacy_calendar_scheduled():
            run_calendar_only_maintenance(trigger="scheduled")

        scheduler = _ensure_scheduler()
        legacy = {
            "enabled": True,
            "mode": "interval",
            "interval_hours": max(1, calendar_hours),
            "time": None,
            "weekday": None,
            "monthday": None,
        }
        cal_next = resolve_next_run_time("lite_sync", legacy)
        try:
            scheduler.add_job(
                _legacy_calendar_scheduled,
                trigger=IntervalTrigger(hours=max(1, calendar_hours), timezone=get_app_zone()),
                id=JOB_ID_CALENDAR,
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                next_run_time=cal_next,
            )
        except Exception:
            logger.exception("Failed to start legacy calendar scheduler")
    else:
        if _scheduler is not None:
            try:
                if _scheduler.get_job(JOB_ID_CALENDAR):
                    _scheduler.remove_job(JOB_ID_CALENDAR)
            except Exception:
                pass
        if calendar_hours > 0 and lite_on:
            logger.info(
                "CALENDAR_SYNC_INTERVAL_HOURS ignored while lite sync is enabled "
                "(lite sync includes calendar date refresh and calendar phase)",
                extra={"emoji_type": "info"},
            )

    if _scheduler and not _scheduler.running:
        _scheduler.start()


def get_scheduled_task_metadata() -> dict[str, Any]:
    """Expose schedule + next-run for Tasks UI (persisted + live job)."""
    from services.app_timezone import app_timezone_name

    tz_name = app_timezone_name()
    out: dict[str, Any] = {
        "timezone": tz_name,
        "full_sync": {
            "enabled": False,
            "interval_hours": 0,
            "next_run": None,
            "schedule": None,
            "schedule_label": "Disabled",
            "timezone": tz_name,
        },
        "lite_sync": {
            "enabled": False,
            "interval_hours": 0,
            "next_run": None,
            "schedule": None,
            "schedule_label": "Disabled",
            "timezone": tz_name,
        },
        "collections_sync": {
            "enabled": False,
            "interval_hours": 0,
            "next_run": None,
            "schedule": None,
            "schedule_label": "Disabled",
            "timezone": tz_name,
        },
    }
    for task_key in ("full_sync", "lite_sync", "collections_sync"):
        sched = get_schedule_for_task(task_key)
        try:
            sched = normalize_schedule(sched)
        except ValueError:
            sched = {
                "enabled": False,
                "mode": "interval",
                "interval_hours": None,
                "time": None,
                "weekdays": None,
                "weekday": None,
                "monthday": None,
                "cron": None,
            }
        hours = schedule_interval_hours(sched) if str(sched.get("mode")) == "interval" else 0
        out[task_key]["interval_hours"] = hours
        out[task_key]["enabled"] = schedule_enabled(sched)
        out[task_key]["schedule"] = sched
        out[task_key]["schedule_label"] = describe_schedule(sched)
        out[task_key]["timezone"] = tz_name

        persisted = get_persisted_next_run(task_key)
        if persisted:
            nrt = persisted
            if nrt.tzinfo is None:
                nrt = nrt.replace(tzinfo=timezone.utc)
            out[task_key]["next_run"] = nrt.astimezone(timezone.utc).isoformat()

        if _scheduler is not None:
            job_id = _job_id_for(task_key)
            try:
                job = _scheduler.get_job(job_id) if job_id else None
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
