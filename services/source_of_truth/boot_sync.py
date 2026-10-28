"""Boot sync decision: overdue schedules + first-full safety (no startup-mode preference)."""

from __future__ import annotations

from typing import Any

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import ArrState
from services.task_schedule import get_schedule_for_task
from services.task_schedule_state import is_task_overdue


def needs_first_full_sync(instances: list[dict]) -> bool:
    """True when any configured ARR instance has never completed a first full sync."""
    if not instances:
        return False
    session = get_session()
    try:
        for instance in instances:
            key = str(instance.get("instance_key") or "").strip()
            if not key:
                continue
            row = session.query(ArrState).filter(ArrState.instance_key == key).first()
            if not row or row.first_full_sync_completed_at is None:
                return True
    finally:
        session.close()
    return False


def resolve_boot_sync_mode(
    instances: list[dict],
    *,
    require_first_full: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Choose boot maintenance from overdue schedules (and first-full).

    Returns ``full``, ``lite``, ``calendar``, or ``off``.

    - Full wins over everything (and includes calendar work inside the full pipeline).
    - Else lite if overdue (calendar may still be overdue separately; caller can chain).
    - Else calendar-only if the calendar schedule is overdue.
    - ``require_first_full`` forces full when an instance has never completed first full.
    """
    overdue_full = is_task_overdue("full_sync", get_schedule_for_task("full_sync"))
    overdue_lite = is_task_overdue("lite_sync", get_schedule_for_task("lite_sync"))
    overdue_calendar = is_task_overdue("calendar_sync", get_schedule_for_task("calendar_sync"))
    first_full_needed = needs_first_full_sync(instances)

    reasons: list[str] = []
    if overdue_full:
        reasons.append("overdue_full")
    if overdue_lite:
        reasons.append("overdue_lite")
    if overdue_calendar:
        reasons.append("overdue_calendar")

    want_full = bool(overdue_full)
    if require_first_full and first_full_needed:
        want_full = True
        reasons.append("require_first_full")

    if want_full:
        mode = "full"
    elif overdue_lite:
        mode = "lite"
    elif overdue_calendar:
        mode = "calendar"
    else:
        mode = "off"

    detail = {
        "mode": mode,
        "overdue_full": overdue_full,
        "overdue_lite": overdue_lite,
        "overdue_calendar": overdue_calendar,
        "also_run_calendar": bool(overdue_calendar and mode == "lite"),
        "first_full_needed": first_full_needed,
        "require_first_full": bool(require_first_full),
        "reasons": reasons,
    }
    logger.info(
        "Boot sync decision mode=%s overdue_full=%s overdue_lite=%s overdue_calendar=%s "
        "first_full_needed=%s reasons=%s",
        mode,
        overdue_full,
        overdue_lite,
        overdue_calendar,
        first_full_needed,
        ",".join(reasons) or "none",
        extra={"emoji_type": "info"},
    )
    return mode, detail
