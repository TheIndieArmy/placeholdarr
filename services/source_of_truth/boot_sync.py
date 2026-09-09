"""Unified boot sync decision: STARTUP_SYNC_MODE + overdue schedules → one mode."""

from __future__ import annotations

from typing import Any

from core.config import settings
from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import ArrState
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
    """Choose at most one boot sync: ``full``, ``lite``, or ``off``.

    Demands come from overdue scheduled tasks and ``STARTUP_SYNC_MODE``.
    Any full demand wins over lite. ``require_first_full`` forces full when an
    instance has never completed first full (post-onboarding).
    """
    setting = str(getattr(settings, "STARTUP_SYNC_MODE", "auto") or "auto").strip().lower()
    if setting not in {"off", "full", "lite", "auto"}:
        logger.warning(
            f"Unknown STARTUP_SYNC_MODE={setting!r}; defaulting to auto",
            extra={"emoji_type": "warning"},
        )
        setting = "auto"

    full_hours = max(0, int(getattr(settings, "FULL_SYNC_INTERVAL_HOURS", 0) or 0))
    lite_hours = max(0, int(getattr(settings, "LITE_SYNC_INTERVAL_HOURS", 0) or 0))
    overdue_full = is_task_overdue("full_sync", full_hours)
    overdue_lite = is_task_overdue("lite_sync", lite_hours)
    first_full_needed = needs_first_full_sync(instances)

    want_full = bool(overdue_full)
    want_lite = bool(overdue_lite)
    reasons: list[str] = []

    if overdue_full:
        reasons.append("overdue_full")
    if overdue_lite:
        reasons.append("overdue_lite")

    if require_first_full and first_full_needed:
        want_full = True
        reasons.append("require_first_full")

    if setting == "full":
        want_full = True
        reasons.append("setting_full")
    elif setting == "lite":
        want_lite = True
        reasons.append("setting_lite")
    elif setting == "auto":
        if not instances:
            reasons.append("setting_auto_no_instances")
        elif first_full_needed:
            want_full = True
            reasons.append("setting_auto_first_full")
        else:
            want_lite = True
            reasons.append("setting_auto_lite")
    else:
        reasons.append("setting_off")

    if want_full:
        mode = "full"
    elif want_lite:
        mode = "lite"
    else:
        mode = "off"

    detail = {
        "setting": setting,
        "mode": mode,
        "overdue_full": overdue_full,
        "overdue_lite": overdue_lite,
        "first_full_needed": first_full_needed,
        "require_first_full": bool(require_first_full),
        "reasons": reasons,
    }
    logger.info(
        "Boot sync decision mode=%s setting=%s overdue_full=%s overdue_lite=%s "
        "first_full_needed=%s reasons=%s",
        mode,
        setting,
        overdue_full,
        overdue_lite,
        first_full_needed,
        ",".join(reasons) or "none",
        extra={"emoji_type": "info"},
    )
    return mode, detail
