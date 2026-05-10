from __future__ import annotations

import re

from core.config import settings


VALID_PROJECTION_MODES = {"summary", "title", "both"}


def get_projection_mode() -> str:
    raw = str(getattr(settings, "PLACEHOLDER_STATUS_PROJECTION_MODE", "summary") or "summary").strip().lower()
    if raw == "off":
        return "summary"
    if raw in VALID_PROJECTION_MODES:
        return raw
    return "summary"


def _updates_scope() -> str:
    raw = str(getattr(settings, "PLACEHOLDER_STATUS_UPDATES", "ALL") or "ALL").strip().upper()
    if raw in {"OFF", "REQUEST", "ALL"}:
        return raw
    return "ALL"


def should_project_status(status: str | None) -> bool:
    scope = _updates_scope()
    if scope == "OFF":
        return False
    if scope == "REQUEST":
        return str(status or "").strip().upper() == "REQUEST"
    return True


def strip_status_from_title(title: str | None) -> str:
    text = str(title or "").strip()
    if not text:
        return ""

    previous = None
    while text != previous:
        previous = text
        text = re.sub(r"^\[[^\]]+\]\s*", "", text).strip()
        text = re.sub(r"\s*-\s*\[[^\]]+\]\s*$", "", text).strip()
        text = re.sub(r"\s*\[[^\]]+\]\s*$", "", text).strip()

    return re.sub(r"\s+", " ", text).strip()


def strip_status_from_summary(summary: str | None) -> str:
    """Remove optional REQUEST runtime leader and leading projected status bracket."""
    text = str(summary or "")
    # Legacy: "~45m · " before "[REQUEST]" (older NFO / summaries)
    text = re.sub(r"^~(?:\d+h(?:\s+\d+m)?|\d+m)\s*·\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\[[^\]]+\]\s*", "", text).strip()
    return text


def _rounded_minutes(value: int | float | str | None) -> int:
    if value is None or value == "":
        return 0
    try:
        return max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return 0


def format_duration_label(minutes: int) -> str:
    """Human-readable duration from whole minutes (e.g. 45m, 1h, 1h 5m)."""
    if minutes <= 0:
        return ""
    if minutes < 60:
        return f"{minutes}m"
    h, m = divmod(minutes, 60)
    if m == 0:
        return f"{h}h"
    return f"{h}h {m}m"


# User-facing labels for status enum values that should not be shown in their
# raw SCREAMING_SNAKE form. Anything not listed here renders as the raw value.
_FRIENDLY_STATUS_LABELS: dict[str, str] = {
    "SEARCH_QUEUED": "Search queued",
}


def _friendly_status_label(status: str | None) -> str:
    """Map an enum value to its user-facing label. Falls back to the raw value."""
    raw = str(status or "").strip()
    if not raw:
        return ""
    upper = raw.upper()
    return _FRIENDLY_STATUS_LABELS.get(upper, raw)


def _summary_status_bracket(clean_status: str, runtime_minutes: int | None) -> str:
    """Build [REQUEST] or [1h 5m · REQUEST] when runtime is known (REQUEST only)."""
    status_upper = clean_status.upper()
    if status_upper == "REQUEST" and runtime_minutes is not None:
        rm = _rounded_minutes(runtime_minutes)
        if rm > 0:
            dur = format_duration_label(rm)
            if dur:
                return f"[{dur} · {clean_status}]"
    label = _friendly_status_label(clean_status) or clean_status
    return f"[{label}]"


def projected_status_display(
    status: str | None,
    *,
    reason: str | None = None,
    runtime_minutes: int | None = None,
) -> str | None:
    """Return persisted user-facing status label for display surfaces."""
    raw_status = str(status or "").strip()
    if not raw_status:
        return None
    raw_reason = str(reason or "").strip()
    upper = raw_status.upper()
    effective = raw_status
    if upper in {
        "COMING_SOON",
        "COMING_SOON_30",
        "COMING_SOON_14",
        "COMING_SOON_7",
        "COMING_SOON_1",
        "COMING_SOON_TODAY",
    } and raw_reason:
        effective = raw_reason
    elif upper == "DOWNLOADING" and raw_reason:
        effective = raw_reason
    elif upper == "SEARCHING" and raw_reason.lower() == "queued":
        effective = raw_reason

    eff_upper = str(effective).strip().upper()
    if eff_upper == "REQUEST":
        return _summary_status_bracket("REQUEST", runtime_minutes)
    friendly = _friendly_status_label(effective)
    return friendly.strip() or None


def project_title(title: str | None, status: str | None) -> str:
    clean_title = strip_status_from_title(title)
    clean_status = str(status or "").strip()
    if not clean_status or not should_project_status(clean_status) or get_projection_mode() not in {"title", "both"}:
        return clean_title
    label = _friendly_status_label(clean_status) or clean_status
    return f"{clean_title} - [{label}]".strip()


def project_summary(
    summary: str | None,
    status: str | None,
    *,
    runtime_minutes: int | None = None,
) -> str:
    clean_summary = strip_status_from_summary(summary)
    clean_status = str(status or "").strip()
    if not clean_status or not should_project_status(clean_status) or get_projection_mode() not in {"summary", "both"}:
        return clean_summary
    bracket = _summary_status_bracket(clean_status, runtime_minutes)
    return f"{bracket} {clean_summary}".strip()