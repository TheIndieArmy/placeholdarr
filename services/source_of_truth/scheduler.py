import logging
from datetime import datetime, timezone
from typing import Any, Collection

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from services.app_timezone import get_app_zone, parse_hhmm
from services.source_of_truth.scheduled_sync import (
    run_calendar_only_maintenance,
    run_lite_sync,
    run_scheduled_full_sync,
)
from services.task_schedule import (
    any_calendar_mode,
    describe_schedule,
    get_schedule_for_task,
    next_occurrence_after,
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


class _ClampedMonthlyTrigger(BaseTrigger):
    """Monthly fires via ``next_occurrence_after`` so day 29–31 clamp on short months.

    Plain ``CronTrigger(day=N)`` skips months that lack day N; our schedule math does not.
    """

    def __init__(self, sched: dict[str, Any]):
        super().__init__()
        self._sched = dict(sched)

    def get_next_fire_time(self, previous_fire_time, now):
        if now is None:
            return None
        after = previous_fire_time if previous_fire_time is not None else now
        try:
            nxt = next_occurrence_after(self._sched, after)
        except ValueError:
            return None
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        return nxt


logger = logging.getLogger("services.source_of_truth.scheduler")
_scheduler: BackgroundScheduler | None = None

JOB_ID_FULL = "source_of_truth:all_arrs"
JOB_ID_LITE = "source_of_truth:lite_sync"
JOB_ID_CALENDAR = "source_of_truth:calendar_sync"
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
    if task_key in {"calendar_sync", "calendar_only"}:
        return JOB_ID_CALENDAR
    if task_key == "collections_sync":
        return JOB_ID_COLLECTIONS
    return None


def _collections_parent_schedule() -> dict[str, Any]:
    """Effective schedule used to tick the collections APScheduler job.

    Global Collections sync schedule is the default for recipes that follow it.
    Recipes with their own enabled schedule still keep this job alive when the
    global schedule is disabled (follow-global recipes stay manual-only then).
    """
    global_sched = get_schedule_for_task("collections_sync")

    explicit_recipe_schedules: list[dict[str, Any]] = []
    try:
        from services.collections.engine import enabled_recipe_schedules

        for raw in enabled_recipe_schedules():
            if not isinstance(raw, dict):
                continue
            try:
                sched = normalize_schedule(raw)
            except ValueError:
                continue
            if schedule_enabled(sched):
                explicit_recipe_schedules.append(sched)
    except Exception:
        pass

    candidates: list[dict[str, Any]] = []
    if schedule_enabled(global_sched):
        candidates.append(global_sched)
        candidates.extend(explicit_recipe_schedules)
    else:
        candidates = list(explicit_recipe_schedules)

    if not candidates:
        return global_sched

    if any_calendar_mode(candidates):
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

    smallest = smallest_interval_hours_among(candidates)
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
    return candidates[0]


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
        return _ClampedMonthlyTrigger(sched)
    return IntervalTrigger(hours=24, timezone=zone)


def _start_scheduled_job(
    task_key: str,
    sched: dict[str, Any],
    target,
    label: str,
    *,
    job_id: str,
    ignore_persisted: bool = False,
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
    next_run_time = resolve_next_run_time(task_key, sched, ignore_persisted=ignore_persisted)
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
    key = "calendar_sync" if task_key == "calendar_only" else str(task_key)
    if key == "collections_sync":
        sched = _collections_parent_schedule()
    else:
        sched = get_schedule_for_task(key)
    if not schedule_enabled(sched):
        return
    nxt = bump_next_run_after_run(key, sched, completed_at=completed_at)
    _apply_next_run_to_job(key, nxt, sched)


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


def _run_calendar_sync_scheduled():
    run_calendar_only_maintenance(trigger="scheduled")


def refresh_collections_schedule() -> None:
    """Re-apply the collections job after recipe schedule overrides change.

    Always drops the persisted next-run so a far-future global tick cannot stick
    after the parent cadence switches to hourly (calendar recipe overrides).
    """
    _start_scheduled_job(
        "collections_sync",
        _collections_parent_schedule(),
        _run_collections_sync_scheduled,
        "Collections sync",
        job_id=JOB_ID_COLLECTIONS,
        ignore_persisted=True,
    )


def refresh_all_schedules(*, reset_next_run_for: Collection[str] | None = None) -> None:
    """Rebuild full/lite/collections jobs from current settings (no process restart).

    ``reset_next_run_for``: task keys whose persisted next-run should be cleared so the
    new cadence takes effect immediately (settings save). Startup rebuilds omit this
    and keep persisted clocks.
    """
    schedule_all_syncs(reset_next_run_for=reset_next_run_for)


def schedule_all_syncs(*, reset_next_run_for: Collection[str] | None = None):
    """Schedule full, lite, and collections jobs from JSON schedules."""
    global _scheduler
    reset = {str(k).strip().lower() for k in (reset_next_run_for or ())}

    _start_scheduled_job(
        "full_sync",
        get_schedule_for_task("full_sync"),
        _run_all_syncs_scheduled,
        "All ARRs (full sync)",
        job_id=JOB_ID_FULL,
        ignore_persisted="full_sync" in reset,
    )

    lite_sched = get_schedule_for_task("lite_sync")
    _start_scheduled_job(
        "lite_sync",
        lite_sched,
        _run_lite_sync_scheduled,
        "Lite sync",
        job_id=JOB_ID_LITE,
        ignore_persisted="lite_sync" in reset,
    )

    _start_scheduled_job(
        "collections_sync",
        _collections_parent_schedule(),
        _run_collections_sync_scheduled,
        "Collections sync",
        job_id=JOB_ID_COLLECTIONS,
        ignore_persisted="collections_sync" in reset,
    )

    _start_scheduled_job(
        "calendar_sync",
        get_schedule_for_task("calendar_sync"),
        _run_calendar_sync_scheduled,
        "Calendar sync",
        job_id=JOB_ID_CALENDAR,
        ignore_persisted="calendar_sync" in reset,
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
        "calendar_sync": {
            "enabled": False,
            "interval_hours": 0,
            "next_run": None,
            "schedule": None,
            "schedule_label": "Disabled",
            "timezone": tz_name,
        },
    }
    for task_key in ("full_sync", "lite_sync", "collections_sync", "calendar_sync"):
        # Collections job may run on a faster parent tick than the global setting
        # (hourly when any enabled recipe uses a calendar cadence).
        raw_sched = (
            _collections_parent_schedule()
            if task_key == "collections_sync"
            else get_schedule_for_task(task_key)
        )
        try:
            sched = normalize_schedule(raw_sched)
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
