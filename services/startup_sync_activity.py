"""Persist startup sync phase snapshots into system_activity_history."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import SystemActivityHistory


def _utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if getattr(dt, "tzinfo", None) is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _phase_status(current_phase: str, section: str, *, has_stats: bool, failed: bool = False) -> str:
    if failed:
        return "failed"
    if has_stats:
        return "done"
    if current_phase == section:
        return "working"
    return "pending"


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _build_startup_sync_row(
    *,
    mode: str,
    started_at: datetime,
    current_phase: str,
    startup_sync_stats: dict[str, Any] | None,
    determination_stats: dict[str, Any] | None,
    materialization_stats: dict[str, Any] | None,
    completed_at: datetime | None,
    failed: bool,
    error_message: str | None,
) -> dict[str, Any]:
    startup_sync_stats = startup_sync_stats or {}
    determination_stats = determination_stats or {}
    materialization_stats = materialization_stats or {}

    display_mode = str(mode or "auto").strip().lower() or "auto"
    is_lite = display_mode == "lite"
    display_name = "Lite Sync Progress" if is_lite else "Full Sync Progress"
    job_type = "lite_sync_progress" if is_lite else "full_sync_progress"

    running = completed_at is None and not failed
    overall_status = "FAILED" if failed else ("WORKING" if running else "DONE")

    discovery_status = _phase_status(
        current_phase,
        "discovery",
        has_stats=bool(startup_sync_stats),
        failed=failed,
    )
    determination_status = _phase_status(
        current_phase,
        "determination",
        has_stats=bool(determination_stats),
        failed=failed,
    )
    materialization_failed = failed or _to_int(materialization_stats.get("errors"), 0) > 0
    materialization_status = _phase_status(
        current_phase,
        "materialization",
        has_stats=bool(materialization_stats),
        failed=materialization_failed,
    )

    movies_discovered = _to_int(startup_sync_stats.get("movies_seen"), _to_int(determination_stats.get("movies_total"), 0))
    series_discovered = _to_int(startup_sync_stats.get("series_seen"), 0)
    episodes_discovered = _to_int(startup_sync_stats.get("episodes_seen"), _to_int(determination_stats.get("episodes_total"), 0))
    created_n = _to_int(materialization_stats.get("created"), 0)
    noop_n = _to_int(materialization_stats.get("noop"), 0)
    if bool(materialization_stats) and created_n == 0 and noop_n > 0:
        details = (
            f"Materialization: {noop_n} item(s) already had a placeholder file on disk • "
            f"{created_n} new file(s) written • Mode {display_mode}"
        )
    else:
        details = (
            f"Materialization created {created_n} placeholders • "
            f"Mode {display_mode}"
        )
    if error_message:
        details = f"{details} • Error {error_message}"

    sections: list[dict[str, Any]] = [
        {
            "name": "Discovery",
            "status": discovery_status,
            "metrics": [
                {"label": "Movies discovered", "value": movies_discovered if bool(startup_sync_stats) else "--"},
                {"label": "Series discovered", "value": series_discovered if bool(startup_sync_stats) else "--"},
                {"label": "Episodes discovered", "value": episodes_discovered if bool(startup_sync_stats) else "--"},
            ],
        },
        {
            "name": "Determination",
            "status": determination_status,
            "metrics": [
                {
                    "label": "Needs placeholder",
                    "value": _to_int(determination_stats.get("needs_placeholder"), 0) if bool(determination_stats) else "--",
                },
                {
                    "label": "Already had placeholder",
                    "value": _to_int(determination_stats.get("placeholder_exists"), 0) if bool(determination_stats) else "--",
                },
                {
                    "label": "Not needed",
                    "value": _to_int(determination_stats.get("not_needed"), 0) if bool(determination_stats) else "--",
                },
            ],
        },
        {
            "name": "Materialization",
            "status": materialization_status,
            "metrics": [
                {"label": "Created", "value": _to_int(materialization_stats.get("created"), 0) if bool(materialization_stats) else "--"},
                {
                    "label": "Already on disk",
                    "value": _to_int(materialization_stats.get("noop"), 0) if bool(materialization_stats) else "--",
                },
                {"label": "Files created", "value": _to_int(materialization_stats.get("files_created"), 0) if bool(materialization_stats) else "--"},
                {"label": "NFO written", "value": _to_int(materialization_stats.get("nfo_written"), 0) if bool(materialization_stats) else "--"},
            ],
        },
    ]

    run_id = f"startup-sync-{display_mode}-{int(_utc(started_at).timestamp())}"
    return {
        "id": run_id,
        "type": "job",
        "job_type": job_type,
        "display_name": display_name,
        "status": overall_status,
        "details": details,
        "error": error_message,
        "time": _utc(started_at).isoformat(),
        "progress": {
            "running": running,
            "sections": sections,
        },
    }


def record_startup_sync_progress(
    *,
    mode: str,
    started_at: datetime,
    current_phase: str,
    startup_sync_stats: dict[str, Any] | None,
    determination_stats: dict[str, Any] | None,
    materialization_stats: dict[str, Any] | None,
    completed_at: datetime | None = None,
    failed: bool = False,
    error_message: str | None = None,
) -> None:
    """Append one startup sync progress snapshot row to system_activity_history."""
    session = get_session()
    try:
        row = _build_startup_sync_row(
            mode=mode,
            started_at=started_at,
            current_phase=current_phase,
            startup_sync_stats=startup_sync_stats,
            determination_stats=determination_stats,
            materialization_stats=materialization_stats,
            completed_at=completed_at,
            failed=failed,
            error_message=error_message,
        )
        ref_id = int(_utc(started_at).timestamp())
        session.add(
            SystemActivityHistory(
                occurred_at=_utc(completed_at) if completed_at else datetime.now(timezone.utc),
                origin="startup_sync_progress",
                ref_id=ref_id,
                snapshot={"rows": [row]},
            )
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.warning("startup sync progress snapshot skipped: %s", exc, extra={"emoji_type": "warning"})
    finally:
        session.close()
