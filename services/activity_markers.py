"""Persist lightweight EventLog rows so the dashboard can anchor sync times without log parsing."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import EventLog

# Informational only — not processed by the webhook worker (no Job row).
EVENT_STARTUP_SOURCE_OF_TRUTH = "internal_dashboard_startup_source_of_truth"
EVENT_CALENDAR_DATE_REFRESH = "internal_dashboard_calendar_date_refresh"


def _slim_determination_for_marker(stats: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(stats, dict):
        return {}
    keys = (
        "needs_placeholder",
        "placeholder_exists",
        "not_needed",
        "obsolete_placeholder",
        "path_drift_movies",
        "path_drift_episodes",
        "movies_total",
        "episodes_total",
        "movies_changed",
        "episodes_changed",
    )
    out: dict[str, Any] = {}
    for k in keys:
        if k not in stats:
            continue
        try:
            out[k] = int(stats[k])
        except Exception:
            continue
    return out


def _slim_materialization_for_marker(stats: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(stats, dict):
        return {}
    keys = (
        "created",
        "noop",
        "deleted",
        "errors",
        "files_created",
        "nfo_written",
        "movie_refresh_triggered",
        "tv_refresh_triggered",
    )
    out: dict[str, Any] = {}
    for k in keys:
        if k not in stats:
            continue
        v = stats[k]
        if isinstance(v, bool):
            out[k] = v
            continue
        try:
            out[k] = int(v)
        except Exception:
            continue
    return out


def record_startup_source_of_truth_activity(
    *,
    mode: str,
    started_at: datetime,
    completed_at: datetime | None = None,
    determination: dict[str, Any] | None = None,
    materialization: dict[str, Any] | None = None,
) -> None:
    """Record when startup source-of-truth began (authoritative for Activity \"When\")."""
    done = completed_at or datetime.now(timezone.utc)
    session = get_session()
    try:
        payload: dict[str, Any] = {
            "started_at": started_at.astimezone(timezone.utc).isoformat(),
            "completed_at": done.astimezone(timezone.utc).isoformat(),
            "mode": str(mode or "").strip().lower(),
        }
        slim_det = _slim_determination_for_marker(determination)
        if slim_det:
            payload["determination"] = slim_det
        slim_mat = _slim_materialization_for_marker(materialization)
        if slim_mat:
            payload["materialization"] = slim_mat
        session.add(
            EventLog(
                event_type=EVENT_STARTUP_SOURCE_OF_TRUTH,
                source="placeholdarr",
                payload=payload,
                status="DONE",
                attempts=0,
                max_attempts=0,
                updated_at=done,
            )
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.warning(
            f"Could not persist startup activity marker: {exc}",
            extra={"emoji_type": "warning"},
        )
    finally:
        session.close()


def record_calendar_date_refresh_activity(
    *,
    started_at: datetime,
    stats: dict[str, Any],
    completed_at: datetime | None = None,
) -> None:
    """Record calendar date refresh window + stats (authoritative started_at for Activity)."""
    done = completed_at or datetime.now(timezone.utc)
    session = get_session()
    try:
        payload = dict(stats) if isinstance(stats, dict) else {}
        payload["started_at"] = started_at.astimezone(timezone.utc).isoformat()
        payload["completed_at"] = done.astimezone(timezone.utc).isoformat()
        session.add(
            EventLog(
                event_type=EVENT_CALENDAR_DATE_REFRESH,
                source="placeholdarr",
                payload=payload,
                status="DONE",
                attempts=0,
                max_attempts=0,
                updated_at=done,
            )
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.warning(
            f"Could not persist calendar date refresh activity marker: {exc}",
            extra={"emoji_type": "warning"},
        )
    finally:
        session.close()
