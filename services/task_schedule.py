"""Task schedule model: Interval | Daily | Weekly | Monthly | Cron in app timezone.

Stored as JSON on AppConfig (``FULL_SYNC_SCHEDULE``, etc.):

  {
    "enabled": true,
    "mode": "interval" | "daily" | "weekly" | "monthly" | "cron",
    "interval_hours": 12,   # interval only
    "time": "03:00",        # daily / weekly / monthly
    "weekdays": [6],        # weekly (0=Mon … 6=Sun); ``weekday`` single also accepted
    "monthday": 1,          # monthly 1–31 (clamped on short months)
    "cron": "0 3 * * 0"     # cron only (5-field)
  }

Legacy ``*_INTERVAL_HOURS`` and WIP every/unit shapes normalize on read.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from apscheduler.triggers.cron import CronTrigger

from services.app_timezone import (
    clamp_month_day,
    format_hhmm,
    get_app_zone,
    parse_hhmm,
    to_app_zone,
)

SCHEDULE_MODES = frozenset({"interval", "daily", "weekly", "monthly", "cron"})

WEEKDAY_LABELS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

TASK_SCHEDULE_KEYS = {
    "full_sync": "FULL_SYNC_SCHEDULE",
    "lite_sync": "LITE_SYNC_SCHEDULE",
    "collections_sync": "COLLECTIONS_SYNC_SCHEDULE",
    "calendar_sync": "CALENDAR_SYNC_SCHEDULE",
}

LEGACY_INTERVAL_KEYS = {
    "full_sync": "FULL_SYNC_INTERVAL_HOURS",
    "lite_sync": "LITE_SYNC_INTERVAL_HOURS",
    "collections_sync": "COLLECTIONS_SYNC_INTERVAL_HOURS",
    "calendar_sync": "CALENDAR_SYNC_INTERVAL_HOURS",
}

DEFAULT_INTERVAL_HOURS = {
    "full_sync": 168,
    "lite_sync": 12,
    "collections_sync": 24,
    # Twice daily (same cadence as lite); legacy calendar hours 0 still migrate via get_schedule.
    "calendar_sync": 12,
}

DEFAULT_TIME = "03:00"


def _disabled_schedule() -> dict[str, Any]:
    return {
        "enabled": False,
        "mode": "interval",
        "interval_hours": None,
        "time": None,
        "weekdays": None,
        "monthday": None,
        "cron": None,
    }


def _enabled_base(mode: str) -> dict[str, Any]:
    return {
        "enabled": True,
        "mode": mode,
        "interval_hours": None,
        "time": None,
        "weekdays": None,
        "monthday": None,
        "cron": None,
    }


def interval_schedule(hours: int) -> dict[str, Any]:
    """Every N hours (or Off when hours <= 0)."""
    n = int(hours or 0)
    if n <= 0:
        return _disabled_schedule()
    out = _enabled_base("interval")
    out["interval_hours"] = max(1, n)
    return out


def schedule_from_legacy_hours(hours: int | None) -> dict[str, Any]:
    """Migrate legacy hour intervals into mode schedules.

    0 → off; 12 → interval 12h; 24 → daily 03:00; 168 → weekly Sunday 03:00;
    other multiples of 168 → weekly Sunday; multiples of 24 → daily; else interval.
    """
    try:
        n = int(hours or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return _disabled_schedule()
    if n == 12:
        return interval_schedule(12)
    if n % 168 == 0:
        out = _enabled_base("weekly")
        out["time"] = DEFAULT_TIME
        out["weekdays"] = [6]
        return out
    if n % 24 == 0:
        out = _enabled_base("daily")
        out["time"] = DEFAULT_TIME
        return out
    return interval_schedule(n)


def default_schedule_for_task(task_key: str) -> dict[str, Any]:
    key = str(task_key or "").strip().lower()
    hours = int(DEFAULT_INTERVAL_HOURS.get(key, 24))
    return schedule_from_legacy_hours(hours)


def _parse_weekdays(raw: dict[str, Any]) -> list[int]:
    raw_days = raw.get("weekdays")
    if raw_days is None and raw.get("weekday") is not None:
        raw_days = [raw.get("weekday")]
    if not isinstance(raw_days, (list, tuple)) or not raw_days:
        raise ValueError("weekly schedule requires at least one weekday (0–6, Monday–Sunday)")
    days: list[int] = []
    for item in raw_days:
        try:
            d = int(item)
        except (TypeError, ValueError) as exc:
            raise ValueError("weekday must be 0–6 (Monday–Sunday)") from exc
        if not (0 <= d <= 6):
            raise ValueError("weekday must be 0–6 (Monday–Sunday)")
        if d not in days:
            days.append(d)
    days.sort()
    return days


def _migrate_every_unit_dict(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert WIP every/unit recurrence into mode form."""
    enabled = bool(raw.get("enabled", True))
    if not enabled:
        return _disabled_schedule()

    unit = str(raw.get("unit") or "hours").strip().lower()
    try:
        every = int(raw.get("every") if raw.get("every") is not None else raw.get("interval_hours") or 1)
    except (TypeError, ValueError):
        every = 1
    every = max(1, every)
    time_raw = raw.get("time") or DEFAULT_TIME
    try:
        hour, minute = parse_hhmm(time_raw)
        time_s = format_hhmm(hour, minute)
    except ValueError:
        time_s = DEFAULT_TIME

    if unit == "hours":
        return interval_schedule(every)

    if unit == "days":
        if every == 1:
            out = _enabled_base("daily")
            out["time"] = time_s
            return out
        # every N days → cron (day-of-month star with hour/minute; approx via interval hours)
        return interval_schedule(every * 24)

    if unit == "weeks":
        days = []
        try:
            days = _parse_weekdays(raw)
        except ValueError:
            days = [6]
        if every == 1:
            out = _enabled_base("weekly")
            out["time"] = time_s
            out["weekdays"] = days
            return out
        # every N weeks: keep first weekday as weekly (best-effort)
        out = _enabled_base("weekly")
        out["time"] = time_s
        out["weekdays"] = days
        return out

    if unit == "months":
        try:
            monthday = int(raw.get("monthday") if raw.get("monthday") is not None else 1)
        except (TypeError, ValueError):
            monthday = 1
        monthday = max(1, min(31, monthday))
        out = _enabled_base("monthly")
        out["time"] = time_s
        out["monthday"] = monthday
        return out

    return interval_schedule(24)


def validate_cron_expression(expr: str) -> str:
    """Return cleaned 5-field cron or raise ValueError."""
    text = str(expr or "").strip()
    if not text:
        raise ValueError("cron expression is required")
    parts = text.split()
    if len(parts) != 5:
        raise ValueError("cron must be a 5-field expression (min hour dom month dow)")
    try:
        CronTrigger.from_crontab(text, timezone=get_app_zone())
    except Exception as exc:
        raise ValueError(f"invalid cron expression: {exc}") from exc
    return text


def normalize_schedule(raw: Any, *, default_task: str | None = None) -> dict[str, Any]:
    """Validate and normalize a schedule dict. Raises ValueError on bad input."""
    if raw is None or raw == "":
        if default_task:
            return default_schedule_for_task(default_task)
        return _disabled_schedule()

    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except Exception as exc:
            raise ValueError("schedule must be an object") from exc

    if not isinstance(raw, dict):
        raise ValueError("schedule must be an object")

    # WIP every/unit → mode
    if "unit" in raw and str(raw.get("mode") or "") not in SCHEDULE_MODES:
        raw = _migrate_every_unit_dict(raw)

    enabled = bool(raw.get("enabled", True))
    if not enabled:
        return _disabled_schedule()

    mode = str(raw.get("mode") or "interval").strip().lower()
    if mode not in SCHEDULE_MODES:
        raise ValueError(f"mode must be one of: {', '.join(sorted(SCHEDULE_MODES))}")

    if mode == "interval":
        try:
            hours = int(
                raw.get("interval_hours")
                if raw.get("interval_hours") is not None
                else raw.get("every")
                or 0
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("interval_hours must be an integer >= 1") from exc
        if hours < 1:
            raise ValueError("interval_hours must be >= 1 when enabled")
        return interval_schedule(hours)

    if mode == "cron":
        cron_s = validate_cron_expression(str(raw.get("cron") or ""))
        out = _enabled_base("cron")
        out["cron"] = cron_s
        return out

    try:
        hour, minute = parse_hhmm(raw.get("time") or DEFAULT_TIME)
        time_s = format_hhmm(hour, minute)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    if mode == "daily":
        out = _enabled_base("daily")
        out["time"] = time_s
        return out

    if mode == "weekly":
        days = _parse_weekdays(raw)
        out = _enabled_base("weekly")
        out["time"] = time_s
        out["weekdays"] = days
        return out

    # monthly
    try:
        monthday = int(raw.get("monthday") if raw.get("monthday") is not None else 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("monthday must be 1–31") from exc
    if not (1 <= monthday <= 31):
        raise ValueError("monthday must be 1–31")
    out = _enabled_base("monthly")
    out["time"] = time_s
    out["monthday"] = monthday
    return out


def schedule_enabled(sched: dict[str, Any] | None) -> bool:
    if not isinstance(sched, dict):
        return False
    return bool(sched.get("enabled"))


def schedule_interval_hours(sched: dict[str, Any] | None) -> int:
    """Hours for interval mode; 0 if disabled or other modes."""
    if not schedule_enabled(sched):
        return 0
    mode = str((sched or {}).get("mode") or "")
    if mode == "interval":
        try:
            return max(0, int((sched or {}).get("interval_hours") or 0))
        except (TypeError, ValueError):
            return 0
    return 0


def _ui_clock_format() -> str:
    try:
        from core.config import settings

        raw = str(getattr(settings, "UI_CLOCK_FORMAT", "12h") or "12h").strip().lower()
    except Exception:
        raw = "12h"
    if raw in {"24", "24h", "24-hour", "24hour"}:
        return "24h"
    return "12h"


def format_time_for_display(time_s: str | None) -> str:
    """Format HH:MM for UI labels using UI_CLOCK_FORMAT (12h or 24h)."""
    try:
        hour, minute = parse_hhmm(time_s or DEFAULT_TIME)
    except ValueError:
        hour, minute = 3, 0
    if _ui_clock_format() == "24h":
        return format_hhmm(hour, minute)
    suffix = "AM" if hour < 12 else "PM"
    h12 = hour % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}:{minute:02d} {suffix}"


def describe_schedule(sched: dict[str, Any] | None) -> str:
    try:
        sched = normalize_schedule(sched) if isinstance(sched, dict) else None
    except ValueError:
        return "Disabled"
    if not schedule_enabled(sched):
        return "Disabled"

    mode = str((sched or {}).get("mode") or "interval")
    time_s = format_time_for_display(str((sched or {}).get("time") or DEFAULT_TIME))

    if mode == "interval":
        hours = schedule_interval_hours(sched)
        if hours == 1:
            return "Every hour"
        return f"Every {hours} hours"

    if mode == "daily":
        return f"Daily at {time_s}"

    if mode == "weekly":
        days = list((sched or {}).get("weekdays") or [])
        if not days and (sched or {}).get("weekday") is not None:
            days = [int((sched or {})["weekday"])]
        labels = [WEEKDAY_LABELS[d] for d in days if 0 <= int(d) <= 6]
        day_part = ", ".join(labels) if labels else "days"
        return f"Weekly on {day_part} at {time_s}"

    if mode == "monthly":
        md = int((sched or {}).get("monthday") or 1)
        return f"Monthly on day {md} at {time_s}"

    if mode == "cron":
        return f"Cron: {(sched or {}).get('cron') or 'unset'}"

    return "Disabled"


def _combine_local(d: datetime, hour: int, minute: int) -> datetime:
    zone = get_app_zone()
    local = d.astimezone(zone) if d.tzinfo else d.replace(tzinfo=zone)
    return local.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _next_interval_after(after_utc: datetime, hours: int) -> datetime:
    hours = max(1, int(hours))
    after = after_utc if after_utc.tzinfo else after_utc.replace(tzinfo=timezone.utc)
    return after.astimezone(timezone.utc) + timedelta(hours=hours)


def _next_daily_after(after_local: datetime, hour: int, minute: int) -> datetime:
    cand = _combine_local(after_local, hour, minute)
    if cand <= after_local:
        cand = cand + timedelta(days=1)
        cand = cand.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return cand


def _next_weekly_after(after_local: datetime, hour: int, minute: int, weekdays: list[int]) -> datetime:
    days = sorted({int(d) for d in weekdays if 0 <= int(d) <= 6}) or [0]
    zone = get_app_zone()
    start_date = after_local.date()
    for offset in range(0, 15):
        day = start_date + timedelta(days=offset)
        if day.weekday() not in days:
            continue
        cand = datetime(day.year, day.month, day.day, hour, minute, 0, 0, tzinfo=zone)
        if cand > after_local:
            return cand
    cand = _combine_local(after_local, hour, minute) + timedelta(days=1)
    return cand


def _next_monthly_after(after_local: datetime, hour: int, minute: int, monthday: int) -> datetime:
    y, m = after_local.year, after_local.month

    def _cand(year: int, month: int) -> datetime:
        day = clamp_month_day(year, month, monthday)
        return datetime(year, month, day, hour, minute, 0, 0, tzinfo=get_app_zone())

    cand = _cand(y, m)
    if cand <= after_local:
        m += 1
        if m > 12:
            m = 1
            y += 1
        cand = _cand(y, m)
    while cand <= after_local:
        m += 1
        if m > 12:
            m = 1
            y += 1
        cand = _cand(y, m)
    return cand


def _next_cron_after(after_utc: datetime, cron_expr: str) -> datetime:
    trigger = CronTrigger.from_crontab(cron_expr, timezone=get_app_zone())
    after = after_utc if after_utc.tzinfo else after_utc.replace(tzinfo=timezone.utc)
    nxt = trigger.get_next_fire_time(previous_fire_time=None, now=after)
    if nxt is None:
        raise ValueError("cron produced no next fire time")
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=get_app_zone())
    return nxt.astimezone(timezone.utc)


def next_occurrence_after(sched: dict[str, Any], after: datetime) -> datetime:
    """Next fire time (UTC) strictly after ``after``."""
    sched = normalize_schedule(sched)
    if not schedule_enabled(sched):
        raise ValueError("schedule disabled")

    after_aware = after if after.tzinfo else after.replace(tzinfo=timezone.utc)
    mode = str(sched.get("mode") or "interval")

    if mode == "interval":
        return _next_interval_after(after_aware, schedule_interval_hours(sched))

    if mode == "cron":
        return _next_cron_after(after_aware, str(sched.get("cron") or ""))

    after_local = to_app_zone(after_aware)
    hour, minute = parse_hhmm(sched.get("time") or DEFAULT_TIME)

    if mode == "daily":
        nxt = _next_daily_after(after_local, hour, minute)
    elif mode == "weekly":
        days = list(sched.get("weekdays") or [])
        if not days and sched.get("weekday") is not None:
            days = [int(sched["weekday"])]
        nxt = _next_weekly_after(after_local, hour, minute, days or [0])
    elif mode == "monthly":
        nxt = _next_monthly_after(after_local, hour, minute, int(sched.get("monthday") or 1))
    else:
        raise ValueError(f"unknown mode {mode}")

    return nxt.astimezone(timezone.utc)


def bump_next_run_for_schedule(
    sched: dict[str, Any],
    *,
    completed_at: datetime | None = None,
) -> datetime:
    """Next run after a successful completion (UTC)."""
    ended = completed_at or datetime.now(timezone.utc)
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=timezone.utc)
    nxt = next_occurrence_after(sched, ended)
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=timezone.utc)
    return nxt.astimezone(timezone.utc)


def get_schedule_for_task(task_key: str) -> dict[str, Any]:
    """Resolve schedule from runtime settings (with legacy interval fallback)."""
    from core.config import settings

    key = str(task_key or "").strip().lower()
    setting_key = TASK_SCHEDULE_KEYS.get(key)
    raw = getattr(settings, setting_key, None) if setting_key else None
    if isinstance(raw, dict) and raw:
        try:
            return normalize_schedule(raw, default_task=key)
        except ValueError:
            pass
    if isinstance(raw, str) and raw.strip():
        try:
            return normalize_schedule(raw, default_task=key)
        except ValueError:
            pass
    legacy_key = LEGACY_INTERVAL_KEYS.get(key)
    if legacy_key:
        try:
            hours = int(getattr(settings, legacy_key, 0) or 0)
        except (TypeError, ValueError):
            hours = int(DEFAULT_INTERVAL_HOURS.get(key, 0))
        # Calendar interval 0 used to mean "run with lite"; use the calendar default cadence.
        if key == "calendar_sync" and hours <= 0:
            hours = int(DEFAULT_INTERVAL_HOURS.get(key, 12))
        return schedule_from_legacy_hours(hours)
    return default_schedule_for_task(key)


def migrate_interval_hours_to_schedules(session) -> list[str]:
    """Write ``*_SCHEDULE`` AppConfig rows from legacy hour ints when missing."""
    from services.postgres.models import AppConfig

    written: list[str] = []
    for task_key, schedule_key in TASK_SCHEDULE_KEYS.items():
        existing = session.query(AppConfig).filter(AppConfig.key == schedule_key).first()
        if existing is not None and isinstance(existing.value, dict) and existing.value:
            val = existing.value
            needs = False
            if isinstance(val, dict):
                mode = str(val.get("mode") or "")
                # Only rewrite WIP every/unit or singular weekday without weekdays.
                if "unit" in val and mode not in SCHEDULE_MODES:
                    needs = True
                elif val.get("weekday") is not None and not val.get("weekdays"):
                    needs = True
            if needs:
                try:
                    existing.value = normalize_schedule(val, default_task=task_key)
                    existing.value_type = "task_schedule"
                    session.add(existing)
                    written.append(schedule_key)
                except ValueError:
                    pass
            continue
        legacy_key = LEGACY_INTERVAL_KEYS[task_key]
        legacy_row = session.query(AppConfig).filter(AppConfig.key == legacy_key).first()
        if legacy_row is not None and legacy_row.value not in (None, ""):
            try:
                hours = int(legacy_row.value)
            except (TypeError, ValueError):
                hours = int(DEFAULT_INTERVAL_HOURS[task_key])
        else:
            hours = int(DEFAULT_INTERVAL_HOURS[task_key])
        # Legacy calendar 0 = piggyback on lite; seed an enabled daily schedule instead of Disabled.
        if task_key == "calendar_sync" and hours <= 0:
            hours = int(DEFAULT_INTERVAL_HOURS[task_key])
        payload = schedule_from_legacy_hours(hours)
        if existing is None:
            session.add(
                AppConfig(
                    key=schedule_key,
                    value=payload,
                    value_type="task_schedule",
                    restart_required=False,
                    description=f"Schedule for {task_key}",
                )
            )
        else:
            existing.value = payload
            existing.value_type = "task_schedule"
            session.add(existing)
        written.append(schedule_key)
    if written:
        session.commit()
    return written


def recipe_schedule_from_legacy_hours(hours: int | None) -> dict[str, Any] | None:
    """Per-recipe: null hours → follow global; else interval N hours."""
    if hours is None:
        return None
    try:
        n = int(hours)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return interval_schedule(n)


def recipe_is_due(
    *,
    schedule: dict[str, Any] | None,
    global_schedule: dict[str, Any],
    last_run_at: datetime | None,
    now: datetime | None = None,
) -> bool:
    """Whether a recipe should run on this collections tick."""
    effective = schedule if isinstance(schedule, dict) and schedule else global_schedule
    try:
        effective = normalize_schedule(effective)
    except ValueError:
        return False
    if not schedule_enabled(effective):
        return False

    when = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    if last_run_at is None:
        return True

    last = last_run_at if last_run_at.tzinfo else last_run_at.replace(tzinfo=timezone.utc)
    nxt = next_occurrence_after(effective, last)
    return when >= nxt - timedelta(minutes=5)


def smallest_interval_hours_among(schedules: list[dict[str, Any] | None]) -> Optional[int]:
    """Min hours among interval-mode schedules (for parent tick)."""
    best: Optional[int] = None
    for sched in schedules:
        if not isinstance(sched, dict):
            continue
        try:
            sched = normalize_schedule(sched)
        except ValueError:
            continue
        if not schedule_enabled(sched):
            continue
        if str(sched.get("mode") or "") != "interval":
            continue
        h = schedule_interval_hours(sched)
        if h <= 0:
            continue
        best = h if best is None else min(best, h)
    return best


def any_calendar_mode(schedules: list[dict[str, Any] | None]) -> bool:
    """True when any enabled schedule uses daily/weekly/monthly/cron (needs frequent tick)."""
    for sched in schedules:
        if not isinstance(sched, dict):
            continue
        try:
            sched = normalize_schedule(sched)
        except ValueError:
            continue
        if not schedule_enabled(sched):
            continue
        mode = str(sched.get("mode") or "")
        if mode in {"daily", "weekly", "monthly", "cron"}:
            return True
    return False
