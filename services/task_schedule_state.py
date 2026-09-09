"""Persist APScheduler next-run times across process restarts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import AppConfig
from services.task_run_history import latest_finished_run

_TASK_KEYS = {
    "full_sync": "SCHEDULED_TASK_NEXT_RUN_FULL_SYNC",
    "lite_sync": "SCHEDULED_TASK_NEXT_RUN_LITE_SYNC",
    "collections_sync": "SCHEDULED_TASK_NEXT_RUN_COLLECTIONS_SYNC",
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


def is_task_overdue(task_key: str, interval_hours: int, *, now: datetime | None = None) -> bool:
    """True when the schedule is enabled and due.

    A persisted next-run in the past means overdue (do not let a recent last-run
    + interval hide a missed schedule clock). With no persisted next-run, fall
    back to last finished + interval.
    """
    if int(interval_hours or 0) <= 0:
        return False
    when = now or _utc_now()
    safe_hours = max(1, int(interval_hours))
    persisted = get_persisted_next_run(task_key)
    if persisted is not None:
        return persisted <= when

    last = latest_finished_run(task_key)
    if last and last.ended_at:
        ended = last.ended_at
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=timezone.utc)
        candidate = ended.astimezone(timezone.utc) + timedelta(hours=safe_hours)
        return candidate <= when
    # Enabled schedule with no history and no persisted next-run: treat as due.
    return True


def resolve_next_run_time(task_key: str, interval_hours: int) -> datetime:
    """Next fire time: persisted future time, else last completion + interval, else catch-up soon."""
    now = _utc_now()
    safe_hours = max(1, int(interval_hours or 1))
    key = str(task_key).strip().lower()

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
        candidate = ended.astimezone(timezone.utc) + timedelta(hours=safe_hours)
        if candidate > now:
            persist_next_run(key, candidate)
            return candidate

    catch_up = now + timedelta(minutes=_CATCH_UP_MINUTES)
    persist_next_run(key, catch_up)
    return catch_up


def bump_next_run_after_run(task_key: str, interval_hours: int, *, completed_at: datetime | None = None) -> datetime:
    """After a successful manual or scheduled run, schedule the next interval from completion."""
    ended = completed_at or _utc_now()
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=timezone.utc)
    safe_hours = max(1, int(interval_hours or 1))
    nxt = ended.astimezone(timezone.utc) + timedelta(hours=safe_hours)
    persist_next_run(task_key, nxt)
    return nxt
