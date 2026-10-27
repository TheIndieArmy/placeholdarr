"""Persist APScheduler next-run times across process restarts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import AppConfig
from services.task_run_history import latest_finished_run
from services.task_schedule import (
    bump_next_run_for_schedule,
    next_occurrence_after,
    normalize_schedule,
    schedule_enabled,
)

_TASK_KEYS = {
    "full_sync": "SCHEDULED_TASK_NEXT_RUN_FULL_SYNC",
    "lite_sync": "SCHEDULED_TASK_NEXT_RUN_LITE_SYNC",
    "collections_sync": "SCHEDULED_TASK_NEXT_RUN_COLLECTIONS_SYNC",
    "calendar_sync": "SCHEDULED_TASK_NEXT_RUN_CALENDAR_SYNC",
    # Legacy alias used by older calendar-only task runs.
    "calendar_only": "SCHEDULED_TASK_NEXT_RUN_CALENDAR_SYNC",
}

CATCH_UP_MINUTES = 2
_CATCH_UP_MINUTES = CATCH_UP_MINUTES  # backwards-compatible alias


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def get_persisted_next_run(task_key: str) -> datetime | None:
    key = _TASK_KEYS.get(str(task_key).strip().lower())
    if not key:
        return None
    session = get_session()
    try:
        row = session.query(AppConfig).filter(AppConfig.key == key).first()
        if not row or not row.value:
            return None
        raw = row.value
        if isinstance(raw, dict):
            return _parse_iso(raw.get("next_run"))
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    return _parse_iso(payload.get("next_run"))
            except Exception:
                return _parse_iso(raw)
        return None
    finally:
        session.close()


def persist_next_run(task_key: str, next_run: datetime) -> None:
    key = _TASK_KEYS.get(str(task_key).strip().lower())
    if not key:
        return
    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=timezone.utc)
    payload = {"next_run": next_run.astimezone(timezone.utc).isoformat()}
    session = get_session()
    try:
        row = session.query(AppConfig).filter(AppConfig.key == key).first()
        if row:
            row.value = payload
        else:
            session.add(AppConfig(key=key, value=payload))
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.warning("persist_next_run failed task_key=%s: %s", task_key, exc)
    finally:
        session.close()


def clear_persisted_next_run(task_key: str) -> None:
    """Drop stored next-run so the next resolve recomputes from the current schedule."""
    key = _TASK_KEYS.get(str(task_key).strip().lower())
    if not key:
        return
    session = get_session()
    try:
        row = session.query(AppConfig).filter(AppConfig.key == key).first()
        if row:
            session.delete(row)
            session.commit()
    except Exception as exc:
        session.rollback()
        logger.warning("clear_persisted_next_run failed task_key=%s: %s", task_key, exc)
    finally:
        session.close()


def is_task_overdue(task_key: str, schedule: dict[str, Any] | int, *, now: datetime | None = None) -> bool:
    """True when the schedule is enabled and due.

    ``schedule`` may be a schedule dict or a legacy interval_hours int.
    """
    if isinstance(schedule, int):
        if schedule <= 0:
            return False
        from services.task_schedule import interval_schedule

        sched = interval_schedule(max(1, int(schedule)))
    else:
        try:
            sched = normalize_schedule(schedule)
        except ValueError:
            return False
        if not schedule_enabled(sched):
            return False

    when = now or _utc_now()
    persisted = get_persisted_next_run(task_key)
    if persisted is not None:
        return persisted <= when

    last = latest_finished_run(task_key)
    if last and last.ended_at:
        ended = last.ended_at
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=timezone.utc)
        candidate = bump_next_run_for_schedule(sched, completed_at=ended)
        return candidate <= when
    # Enabled schedule with no history and no persisted next-run: treat as due.
    return True


def _coerce_schedule(schedule: dict[str, Any] | int) -> dict[str, Any]:
    if isinstance(schedule, int):
        from services.task_schedule import interval_schedule

        if int(schedule) <= 0:
            return normalize_schedule({"enabled": False})
        return interval_schedule(max(1, int(schedule) or 1))
    return normalize_schedule(schedule)


def resolve_next_run_time(
    task_key: str,
    schedule: dict[str, Any] | int,
    *,
    ignore_persisted: bool = False,
) -> datetime:
    """Next fire time: persisted future time, else next occurrence, else catch-up soon.

    When ``ignore_persisted`` is True (settings schedule change), drop the old
    clock and compute the next occurrence of the *current* schedule after now.
    """
    now = _utc_now()
    sched = _coerce_schedule(schedule)
    key = str(task_key).strip().lower()

    if ignore_persisted:
        clear_persisted_next_run(key)
        if not schedule_enabled(sched):
            return now + timedelta(minutes=_CATCH_UP_MINUTES)
        nxt = next_occurrence_after(sched, now)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        nxt = nxt.astimezone(timezone.utc)
        persist_next_run(key, nxt)
        return nxt

    persisted = get_persisted_next_run(key)
    if persisted and persisted > now:
        return persisted
    # Persisted but past: catch up soon (do not rewrite from last-run + interval).
    if persisted is not None and persisted <= now:
        catch_up = now + timedelta(minutes=_CATCH_UP_MINUTES)
        persist_next_run(key, catch_up)
        return catch_up

    last = latest_finished_run(key)
    if last and last.ended_at:
        ended = last.ended_at
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=timezone.utc)
        candidate = bump_next_run_for_schedule(sched, completed_at=ended)
        if candidate > now:
            persist_next_run(key, candidate)
            return candidate

    catch_up = now + timedelta(minutes=_CATCH_UP_MINUTES)
    persist_next_run(key, catch_up)
    return catch_up


def bump_next_run_after_run(
    task_key: str,
    schedule: dict[str, Any] | int,
    *,
    completed_at: datetime | None = None,
) -> datetime:
    """After a successful manual or scheduled run, schedule the next occurrence."""
    if isinstance(schedule, int):
        sched = {
            "enabled": int(schedule) > 0,
            "mode": "interval",
            "interval_hours": max(1, int(schedule) or 1),
            "time": None,
            "weekday": None,
            "monthday": None,
        }
    else:
        sched = normalize_schedule(schedule)
    nxt = bump_next_run_for_schedule(sched, completed_at=completed_at)
    persist_next_run(task_key, nxt)
    return nxt
