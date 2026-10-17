"""CRUD helpers for ``scheduled_task_run`` (Tasks UI history + in-progress status)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.orm.attributes import flag_modified

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import Job, ScheduledTaskRun


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
        run_id = int(row.id)
        try:
            from services.task_run_cancel import clear_task_run_cancel

            clear_task_run_cancel(run_id)
        except Exception:
            pass
        return run_id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def mutate_task_run_summary(
    run_id: int,
    mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> dict[str, Any] | None:
    """Apply ``mutator`` to ``summary`` under a row lock so concurrent batch writes cannot drop counters.

    ``mutator`` receives a shallow copy of the current summary and must return the next summary
    (or ``None`` to leave the row unchanged).
    """
    session = get_session()
    try:
        row = (
            session.query(ScheduledTaskRun)
            .filter(ScheduledTaskRun.id == int(run_id))
            .with_for_update()
            .first()
        )
        if not row:
            return None
        current = dict(row.summary) if isinstance(row.summary, dict) else {}
        updated = mutator(current)
        if updated is None:
            return current
        row.summary = updated
        flag_modified(row, "summary")
        session.add(row)
        session.commit()
        return updated
    except Exception as exc:
        session.rollback()
        logger.warning("task run summary mutate failed run_id=%s: %s", run_id, exc)
        return None
    finally:
        session.close()


def update_task_run_summary(run_id: int, summary: dict[str, Any]) -> None:
    """Shallow-merge ``summary`` into the locked row (preferred: ``mutate_task_run_summary`` for counters)."""

    def _merge(current: dict[str, Any]) -> dict[str, Any]:
        return {**current, **summary}

    mutate_task_run_summary(int(run_id), _merge)


def reopen_task_run(run_id: int) -> None:
    """Mark a task run in-progress again (e.g. background art backfill after sync phases)."""
    session = get_session()
    try:
        row = session.query(ScheduledTaskRun).filter(ScheduledTaskRun.id == int(run_id)).first()
        if not row:
            return
        row.status = "working"
        row.ended_at = None
        session.add(row)
        session.commit()
    except Exception:
        session.rollback()
        raise
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
        want = str(status).strip().lower()
        existing = row.summary if isinstance(row.summary, dict) else {}
        current = str(row.status or "").lower()
        # Stop / abandon already closed the run; do not let a finishing pipeline
        # promote it back to done/working.
        interrupted = bool(existing.get("interrupted")) or current == "stopped"
        if current in {"stopped", "failed"} and interrupted and want in {"done", "working"}:
            if summary is not None:
                row.summary = {**existing, **summary, "interrupted": True}
                session.add(row)
                session.commit()
            return
        row.status = want
        row.ended_at = _utc_now()
        if error_message:
            row.error_message = str(error_message)[:4000]
        if skip_reason:
            row.skip_reason = str(skip_reason)[:256]
        if summary is not None:
            row.summary = {**existing, **summary}
        session.add(row)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        try:
            from services.task_run_cancel import clear_task_run_cancel

            clear_task_run_cancel(int(run_id))
        except Exception:
            pass
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


def _cancel_follow_up_jobs_for_task_run(session, task_run_id: int) -> int:
    """Fail queued art/NFO jobs tied to a task run that will not complete."""
    from sqlalchemy import String, cast, or_

    from services.worker import mark_claim_revoked

    tid = str(int(task_run_id))
    col = Job.payload["full_sync_task_run_id"]
    legacy = Job.payload["art_backfill_task_run_id"]
    refresh = Job.payload["placeholder_refresh_task_run_id"]
    match = or_(
        col.as_string() == tid,
        cast(col, String) == tid,
        legacy.as_string() == tid,
        cast(legacy, String) == tid,
        refresh.as_string() == tid,
        cast(refresh, String) == tid,
    )
    rows = (
        session.query(Job)
        .filter(
            Job.job_type.in_(("placeholder_art_refresh", "nfo_refresh")),
            Job.status.in_(("PENDING", "CLAIMED", "WORKING")),
            match,
        )
        .all()
    )
    n = 0
    for job in rows:
        was_claimed = str(job.status or "").upper() == "CLAIMED"
        job.status = "FAILED"
        job.error_message = "Parent task run abandoned"
        job.claim_token = None
        session.add(job)
        if was_claimed and job.id is not None:
            try:
                mark_claim_revoked(int(job.id))
            except Exception:
                pass
        n += 1
    return n


def mark_summary_interrupted(summary: dict[str, Any], *, ended_at: datetime, reason: str) -> dict[str, Any]:
    """Freeze live progress and mark phases interrupted for a user/restart Stop."""
    return _mark_phases_interrupted(summary, ended_at=ended_at, reason=reason)


def _mark_phases_interrupted(summary: dict[str, Any], *, ended_at: datetime, reason: str) -> dict[str, Any]:
    out = dict(summary)
    phases = out.get("phases")
    if isinstance(phases, list):
        iso = ended_at.isoformat()
        updated: list[dict[str, Any]] = []
        for phase in phases:
            p = dict(phase) if isinstance(phase, dict) else {}
            if str(p.get("status") or "").lower() == "working":
                p["status"] = "failed"
                if not p.get("ended_at"):
                    p["ended_at"] = iso
            updated.append(p)
        out["phases"] = updated
    progress = out.get("progress")
    if isinstance(progress, dict):
        prog = dict(progress)
        prog["overall_status"] = "STOPPED"
        inner = prog.get("progress")
        if isinstance(inner, dict):
            inner = dict(inner)
            inner["overall_status"] = "STOPPED"
            sections = inner.get("sections")
            if isinstance(sections, list):
                inner["sections"] = [
                    {
                        **(s if isinstance(s, dict) else {}),
                        "status": "failed"
                        if str((s or {}).get("status") or "").lower() == "working"
                        else (s or {}).get("status"),
                    }
                    for s in sections
                ]
            prog["progress"] = inner
        out["progress"] = prog
    # Freeze live bars: drop batch rows, keep phase done/total at the stop moment.
    live = out.get("live")
    if isinstance(live, dict) and live:
        try:
            from services.task_live_progress import freeze_task_live_on_interrupt

            frozen = freeze_task_live_on_interrupt(live)
            if isinstance(frozen, dict):
                out["live"] = frozen
        except Exception:
            # Still mark interrupted even if live freeze fails.
            pass
    for backfill_key in ("art_backfill", "nfo_backfill"):
        meta = out.get(backfill_key)
        if isinstance(meta, dict) and str(meta.get("status") or "").lower() == "working":
            closed = dict(meta)
            closed["status"] = "failed"
            out[backfill_key] = closed
    out["interrupted"] = True
    out["interruption_reason"] = reason
    out["wall_clock_ended_at"] = ended_at.isoformat()
    return out


def abandon_task_run(run_id: int, *, reason: str = "interrupted") -> bool:
    """Mark a stuck in-progress task run stopped so new manual/scheduled runs can start.

    Also requests cooperative cancel so long sync pipelines can stop between
    phases, and fails queued art/NFO follow-up jobs for this run.
    """
    from services.task_run_cancel import request_task_run_cancel

    request_task_run_cancel(int(run_id))
    session = get_session()
    try:
        row = session.query(ScheduledTaskRun).filter(ScheduledTaskRun.id == int(run_id)).first()
        if not row or str(row.status or "").lower() != "working":
            return False
        now = _utc_now()
        summary = row.summary if isinstance(row.summary, dict) else {}
        summary = _mark_phases_interrupted(summary, ended_at=now, reason=reason)
        jobs_cancelled = _cancel_follow_up_jobs_for_task_run(session, int(run_id))
        row.status = "stopped"
        row.ended_at = now
        row.error_message = str(reason)[:4000]
        row.summary = summary
        session.add(row)
        session.commit()
        logger.warning(
            "Abandoned task run id=%s task_key=%s reason=%s follow_up_jobs_failed=%s",
            run_id,
            row.task_key,
            reason,
            jobs_cancelled,
            extra={"emoji_type": "warning"},
        )
        return True
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def abandon_orphaned_working_task_runs(*, reason: str = "interrupted_by_restart") -> list[int]:
    """On process start: close any task rows left ``working`` after a crash/restart."""
    session = get_session()
    try:
        rows = session.query(ScheduledTaskRun).filter(ScheduledTaskRun.status == "working").all()
        ids = [int(r.id) for r in rows if r and r.id is not None]
    finally:
        session.close()
    abandoned: list[int] = []
    for run_id in ids:
        if abandon_task_run(run_id, reason=reason):
            abandoned.append(run_id)
    if abandoned:
        logger.warning(
            "Abandoned %s orphaned working task run(s) after restart: %s",
            len(abandoned),
            abandoned,
            extra={"emoji_type": "warning"},
        )
    return abandoned


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
                ScheduledTaskRun.status.in_(("done", "failed", "stopped", "skipped")),
            )
            .order_by(ScheduledTaskRun.ended_at.desc().nullslast(), ScheduledTaskRun.started_at.desc())
            .first()
        )
    finally:
        session.close()
