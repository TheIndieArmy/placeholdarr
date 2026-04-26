"""Append-only ``system_activity_history`` rows for `/api/activity` (EventLog + Job)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import event
from sqlalchemy.orm import object_session
from sqlalchemy.orm.attributes import get_history

from core.logger import logger
from services.postgres.models import EventLog, Job, SystemActivityHistory

_hooks_registered = False


def _utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if getattr(dt, "tzinfo", None) is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _append(session, *, origin: str, ref_id: int, occurred_at: datetime, rows: list[dict]) -> None:
    if not rows:
        return
    try:
        session.add(
            SystemActivityHistory(
                occurred_at=_utc(occurred_at),
                origin=origin,
                ref_id=int(ref_id),
                snapshot={"rows": list(rows)},
            )
        )
    except Exception as exc:
        logger.warning("system_activity_history insert skipped: %s", exc, extra={"emoji_type": "warning"})


def _on_event_log_after_insert(_mapper, _connection, target: EventLog) -> None:
    session = object_session(target)
    if session is None or getattr(target, "id", None) is None:
        return
    from routes import dashboard as dash

    rows = dash.build_activity_snapshots_for_event_log(session, target)
    if not rows:
        return
    _append(session, origin="event_log", ref_id=int(target.id), occurred_at=target.created_at, rows=rows)


def _on_job_after_insert(_mapper, _connection, target: Job) -> None:
    session = object_session(target)
    if session is None or getattr(target, "id", None) is None:
        return
    from routes import dashboard as dash

    rows = dash.build_activity_snapshots_for_job(session, target)
    if not rows:
        return
    _append(session, origin="job", ref_id=int(target.id), occurred_at=target.updated_at or target.created_at, rows=rows)


def _on_job_after_update(_mapper, _connection, target: Job) -> None:
    session = object_session(target)
    if session is None or getattr(target, "id", None) is None:
        return
    hist = get_history(target, "status")
    if not hist.has_changes():
        return
    from routes import dashboard as dash

    rows = dash.build_activity_snapshots_for_job(session, target)
    if not rows:
        return
    _append(session, origin="job", ref_id=int(target.id), occurred_at=target.updated_at or target.created_at, rows=rows)


def register_system_activity_history_hooks() -> None:
    global _hooks_registered
    if _hooks_registered:
        return
    _hooks_registered = True
    event.listen(EventLog, "after_insert", _on_event_log_after_insert)
    event.listen(Job, "after_insert", _on_job_after_insert)
    event.listen(Job, "after_update", _on_job_after_update)
    logger.info("Registered system_activity_history ORM hooks", extra={"emoji_type": "success"})
