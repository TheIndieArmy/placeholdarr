"""Tasks API: scheduled maintenance metadata, history, and manual triggers."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from core.config import settings
from core.logger import logger
from services.postgres.models import ScheduledTaskRun
from services.source_of_truth.scheduler import get_scheduled_task_metadata
from services.source_of_truth.scheduled_sync import (
    run_calendar_only_maintenance,
    run_lite_sync,
    run_scheduled_full_sync,
)
from services.source_of_truth.placeholder_refresh import (
    reconcile_stuck_placeholder_refresh_tasks,
    run_placeholder_refresh_task,
)
from services.task_run_history import (
    abandon_orphaned_working_task_runs,
    abandon_task_run,
    get_working_run,
    latest_finished_run,
    list_recent_runs,
)
from services.task_run_phases import reconcile_stuck_art_backfill_tasks

router = APIRouter()

TASK_LABELS = {
    "full_sync": "Full ARR sync",
    "lite_sync": "Lite sync",
    "calendar_only": "Calendar only",
    "placeholder_refresh": "Placeholder refresh",
}


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _duration_seconds(started: datetime | None, ended: datetime | None) -> float | None:
    if not started or not ended:
        return None
    s = started if started.tzinfo else started.replace(tzinfo=timezone.utc)
    e = ended if ended.tzinfo else ended.replace(tzinfo=timezone.utc)
    return max(0.0, (e - s).total_seconds())


def _serialize_run(row: ScheduledTaskRun) -> dict[str, Any]:
    dur = _duration_seconds(row.started_at, row.ended_at)
    summary = row.summary if isinstance(row.summary, dict) else {}
    progress = summary.get("progress") if isinstance(summary.get("progress"), dict) else None
    details = None
    if isinstance(progress, dict):
        details = progress.get("details") or progress.get("display_name")
    wall_clock_ended = summary.get("wall_clock_ended_at")
    wall_dur = None
    if wall_clock_ended and row.started_at:
        try:
            end_dt = datetime.fromisoformat(str(wall_clock_ended).replace("Z", "+00:00"))
            wall_dur = _duration_seconds(row.started_at, end_dt)
        except Exception:
            wall_dur = None
    art_backfill = summary.get("art_backfill") if isinstance(summary.get("art_backfill"), dict) else None
    art_pending = False
    if art_backfill and isinstance(progress, dict):
        inner = progress.get("progress") if isinstance(progress.get("progress"), dict) else {}
        sections = inner.get("sections") if isinstance(inner.get("sections"), list) else progress.get("sections")
        if isinstance(sections, list):
            for sec in sections:
                if str(sec.get("key") or sec.get("name") or "").lower() in {"art_refresh", "art refresh"}:
                    if str(sec.get("status") or "").lower() == "working":
                        art_pending = True
                    break
    return {
        "id": row.id,
        "task_key": row.task_key,
        "task_label": TASK_LABELS.get(row.task_key, row.task_key),
        "trigger": row.trigger,
        "status": str(row.status or "").upper(),
        "started_at": _iso(row.started_at),
        "ended_at": _iso(row.ended_at),
        "duration_seconds": dur,
        "sync_duration_seconds": dur,
        "wall_clock_duration_seconds": wall_dur,
        "art_backfill_pending": art_pending,
        "error_message": row.error_message,
        "skip_reason": row.skip_reason,
        "details": details,
        "progress": progress,
    }


class TaskRunRequest(BaseModel):
    task_key: str = Field(..., description="full_sync | lite_sync | calendar_only | placeholder_refresh")
    metadata: bool | None = Field(None, description="When task_key=placeholder_refresh, run metadata refresh phase")
    art: bool | None = Field(None, description="When task_key=placeholder_refresh, run art refresh phase")


class TaskAbandonRequest(BaseModel):
    run_id: int | None = Field(
        None,
        description="Specific scheduled_task_run id to abandon; omit to abandon all working runs",
    )
    reason: str | None = Field(None, description="Stored on the run row (default: abandoned_manually)")


@router.get("/api/tasks/scheduled")
async def tasks_scheduled():
    meta = get_scheduled_task_metadata()
    rows = []
    for task_key in ("full_sync", "lite_sync"):
        sched = meta.get(task_key) or {}
        interval = int(sched.get("interval_hours") or 0)
        working = get_working_run(task_key)
        last = latest_finished_run(task_key)
        if working:
            last_dur = _duration_seconds(working.started_at, datetime.now(timezone.utc))
        else:
            last_dur = _duration_seconds(last.started_at, last.ended_at) if last else None
        rows.append(
            {
                "task_key": task_key,
                "label": TASK_LABELS[task_key],
                "enabled": bool(sched.get("enabled")),
                "interval_hours": interval,
                "interval_label": _interval_label(interval),
                "next_run": sched.get("next_run"),
                "running": working is not None,
                "last_run": _iso(last.ended_at or last.started_at) if last else None,
                "last_duration_seconds": last_dur,
                "last_status": str(last.status or "").upper() if last else None,
            }
        )
    return {"tasks": rows}


def _interval_label(hours: int) -> str:
    if hours <= 0:
        return "Disabled"
    if hours % 168 == 0 and hours >= 168:
        weeks = hours // 168
        return f"Every {weeks} week" if weeks == 1 else f"Every {weeks} weeks"
    if hours == 1:
        return "Every 1 hour"
    return f"Every {hours} hours"


@router.get("/api/tasks/history")
async def tasks_history(limit: int = Query(50, ge=1, le=200)):
    reconcile_stuck_art_backfill_tasks()
    reconcile_stuck_placeholder_refresh_tasks()
    runs = list_recent_runs(limit=limit)
    return [_serialize_run(r) for r in runs]


@router.get("/api/tasks/status")
async def tasks_status():
    reconcile_stuck_art_backfill_tasks()
    reconcile_stuck_placeholder_refresh_tasks()
    working = get_working_run()
    if not working:
        return {"working": False, "run": None}
    return {"working": True, "run": _serialize_run(working)}


@router.post("/api/tasks/abandon")
async def tasks_abandon(body: TaskAbandonRequest | None = None):
    """Mark stuck WORKING task runs failed (e.g. after restart mid-sync) so a new run can start."""
    body = body or TaskAbandonRequest()
    reason = str(body.reason or "abandoned_manually").strip() or "abandoned_manually"
    if body.run_id is not None:
        ok = abandon_task_run(int(body.run_id), reason=reason)
        if not ok:
            raise HTTPException(
                status_code=404,
                detail=f"Task run {body.run_id} is not in working state (or does not exist)",
            )
        return {"ok": True, "abandoned": [int(body.run_id)], "reason": reason}
    abandoned = abandon_orphaned_working_task_runs(reason=reason)
    return {"ok": True, "abandoned": abandoned, "reason": reason}


@router.post("/api/tasks/run")
async def tasks_run(body: TaskRunRequest):
    key = str(body.task_key or "").strip().lower()
    if key not in {"full_sync", "lite_sync", "calendar_only", "placeholder_refresh"}:
        raise HTTPException(status_code=400, detail=f"Unknown task_key: {key}")

    if key == "full_sync":
        existing = get_working_run("full_sync")
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Task already running: full_sync (run id {existing.id})",
            )
    elif key == "lite_sync":
        if get_working_run("lite_sync") or get_working_run("full_sync"):
            raise HTTPException(status_code=409, detail="Task already running: lite or full sync in progress")
    elif key == "placeholder_refresh":
        if get_working_run("placeholder_refresh") or get_working_run("full_sync"):
            raise HTTPException(
                status_code=409,
                detail="Task already running: placeholder refresh or full sync in progress",
            )
    else:
        if get_working_run() or get_working_run("full_sync"):
            raise HTTPException(status_code=409, detail="Another maintenance task is already running")

    def _runner():
        try:
            if key == "full_sync":
                run_scheduled_full_sync(trigger="manual")
            elif key == "lite_sync":
                run_lite_sync(trigger="manual")
            elif key == "placeholder_refresh":
                run_placeholder_refresh_task(
                    trigger="manual",
                    source="manual_task",
                    metadata=bool(True if body.metadata is None else body.metadata),
                    art=bool(True if body.art is None else body.art),
                )
            else:
                run_calendar_only_maintenance(trigger="manual")
        except Exception as exc:
            logger.error("Manual task run failed task_key=%s: %s", key, exc, extra={"emoji_type": "error"})

    threading.Thread(target=_runner, name=f"manual-task-{key}", daemon=True).start()
    return {"ok": True, "task_key": key, "message": "Task started"}
