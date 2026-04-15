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


def record_startup_source_of_truth_activity(
    *,
    mode: str,
    started_at: datetime,
    completed_at: datetime | None = None,
) -> None:
    """Record when startup source-of-truth began (authoritative for Activity \"When\")."""
    done = completed_at or datetime.now(timezone.utc)
    session = get_session()
    try:
        session.add(
            EventLog(
                event_type=EVENT_STARTUP_SOURCE_OF_TRUTH,
                source="placeholdarr",
                payload={
                    "started_at": started_at.astimezone(timezone.utc).isoformat(),
                    "completed_at": done.astimezone(timezone.utc).isoformat(),
                    "mode": str(mode or "").strip().lower(),
                },
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
