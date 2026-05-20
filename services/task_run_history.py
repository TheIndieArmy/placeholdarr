"""CRUD helpers for ``scheduled_task_run`` (Tasks UI history + in-progress status)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import ScheduledTaskRun


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def begin_task_run(*, task_key: str, trigger: str) -> int:
    session = get_session()
    try:
        row = ScheduledTaskRun(
            task_key=str(task_key).strip().lower(),
            trigger=str(trigger).strip().lower(),
            status="working",
            started_at=_utc_now(),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return int(row.id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def update_task_run_summary(run_id: int, summary: dict[str, Any]) -> None:
    session = get_session()
    try:
        row = session.query(ScheduledTaskRun).filter(ScheduledTaskRun.id == int(run_id)).first()
        if not row:
            return
        existing = row.summary if isinstance(row.summary, dict) else {}
        merged = {**existing, **summary}
        row.summary = merged
        session.add(row)
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.warning("task run summary update failed run_id=%s: %s", run_id, exc)
    finally:
        session.close()


def finish_task_run(
    run_id: int,
    *,
    status: str,
    summary: dict[str, Any] | None = None,
    error_message: str | None = None,
    skip_reason: str | None = None,
) -> None:
    session = get_session()
    try:
        row = session.query(ScheduledTaskRun).filter(ScheduledTaskRun.id == int(run_id)).first()
        if not row:
            return
        row.status = str(status).strip().lower()
        row.ended_at = _utc_now()
        if error_message:
            row.error_message = str(error_message)[:4000]
        if skip_reason:
            row.skip_reason = str(skip_reason)[:256]
        if summary is not None:
            existing = row.summary if isinstance(row.summary, dict) else {}
            row.summary = {**existing, **summary}
        session.add(row)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def record_skipped_task_run(*, task_key: str, trigger: str, skip_reason: str) -> int:
    session = get_session()
    try:
        now = _utc_now()
        row = ScheduledTaskRun(
            task_key=str(task_key).strip().lower(),
            trigger=str(trigger).strip().lower(),
            status="skipped",
            started_at=now,
            ended_at=now,
            skip_reason=str(skip_reason)[:256],
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return int(row.id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_working_run(task_key: str | None = None) -> ScheduledTaskRun | None:
    session = get_session()
    try:
        q = session.query(ScheduledTaskRun).filter(ScheduledTaskRun.status == "working")
        if task_key:
            q = q.filter(ScheduledTaskRun.task_key == str(task_key).strip().lower())
        return q.order_by(ScheduledTaskRun.started_at.desc()).first()
    finally:
        session.close()


def any_task_working() -> bool:
    return get_working_run() is not None


def list_recent_runs(*, limit: int = 50) -> list[ScheduledTaskRun]:
    session = get_session()
    try:
        return (
            session.query(ScheduledTaskRun)
            .order_by(ScheduledTaskRun.started_at.desc())
            .limit(max(1, min(int(limit), 200)))
            .all()
        )
    finally:
        session.close()


def latest_finished_run(task_key: str) -> ScheduledTaskRun | None:
    session = get_session()
    try:
        return (
            session.query(ScheduledTaskRun)
            .filter(
                ScheduledTaskRun.task_key == str(task_key).strip().lower(),
                ScheduledTaskRun.status.in_(("done", "failed", "skipped")),
            )
            .order_by(ScheduledTaskRun.ended_at.desc().nullslast(), ScheduledTaskRun.started_at.desc())
            .first()
        )
    finally:
        session.close()
