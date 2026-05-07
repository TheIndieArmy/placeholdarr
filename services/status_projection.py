from __future__ import annotations

import re
from typing import Any

from core.config import settings

from services.source_of_truth.status_intent import DisplayStatus


VALID_PROJECTION_MODES = {"summary", "title", "both"}

# Canonical DB/display statuses whose bracket text should resolve via ``status.label.*``
# (plus queue-monitor synthetic ``RETRYING``). Anything else is treated as free-form text.
_CANONICAL_DISPLAY_STATUSES: frozenset[str] = frozenset(s.value for s in DisplayStatus) | {"RETRYING"}

# Legacy override keys that, when present, indicate the user customized things in the pre-
# situation-first model. Read-compat: until they save in the new UI, projection follows
# the legacy bracket pipeline so their existing wording stays visible.
_LEGACY_REQUEST_OVERRIDE_KEYS: tuple[str, ...] = (
    "bracket.format",
    "bracket.with_runtime",
    "status.label.REQUEST",
    "runtime.format.hm",
    "runtime.format.h",
    "runtime.format.m",
)
_LEGACY_REASON_OVERRIDE_KEYS: tuple[str, ...] = ("bracket.format",)


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

    # Defer to the customizable template engine so users can localize duration formatting.
    from services.messages import render

    if minutes < 60:
        return render("runtime.format.m", {"Minutes": str(minutes)})
    h, m = divmod(minutes, 60)
    if m == 0:
        return render("runtime.format.h", {"Hours": str(h)})
    return render("runtime.format.hm", {"Hours": str(h), "Minutes": str(m)})


def _bracket_context_for_status(clean_status: str, runtime_minutes: int | None) -> dict[str, Any]:
    """Context for legacy ``bracket.format`` / ``bracket.with_runtime`` rendering.

    Canonical enum values use ``__status_enum__`` so ``{Status}`` resolves through the user's
    ``status.label.<ENUM>`` templates. Free-form strings (import-grace lines, queue reason text)
    pass ``Status`` literally.
    """
    text = str(clean_status or "").strip()
    if not text:
        return {}

    upper = text.upper()
    if upper not in _CANONICAL_DISPLAY_STATUSES:
        return {"Status": text}

    ctx: dict[str, Any] = {"__status_enum__": upper}

    if upper == "REQUEST" and runtime_minutes is not None:
        rm = _rounded_minutes(runtime_minutes)
        if rm > 0:
            duration = format_duration_label(rm)
            if duration:
                ctx["Runtime"] = duration

    return ctx


def _has_legacy_overrides(overrides: dict[str, Any], keys: tuple[str, ...]) -> bool:
    """True when at least one of ``keys`` has a non-empty saved override."""
    if not isinstance(overrides, dict):
        return False
    for k in keys:
        v = overrides.get(k)
        if isinstance(v, str) and v.strip():
            return True
    return False


def _request_inner_line(runtime_minutes: int | None) -> str:
    """Render the situation-first ``line.request`` inner text for REQUEST placeholders."""
    from services.messages import render

    ctx: dict[str, Any] = {}
    if runtime_minutes is not None:
        rm = _rounded_minutes(runtime_minutes)
        if rm > 0:
            duration = format_duration_label(rm)
            if duration:
                ctx["Runtime"] = duration
    return render("line.request", ctx)


def _summary_status_bracket(clean_status: str, runtime_minutes: int | None) -> str:
    """Build the wrapped status text shown in Plex/NFO summaries.

    Pipeline (situation-first): render an inner line per situation, then apply the
    global wrapper preset (``[ ]`` by default) post-render.

    Read-compat: when the user customized the legacy bracket templates and has not yet
    saved a value through the new UI, fall back to the legacy bracket renderer that
    bakes its own brackets in via the saved override. ``status.label.*`` overrides
    keep working for canonical non-REQUEST stages because they resolve via the
    engine when we render the inner word.
    """
    from services.messages import apply_wrapper, render
    from services.messages.store import get_overrides
    from services.messages.template_engine import UnknownTemplateKeyError

    text = str(clean_status or "").strip()
    if not text:
        return ""

    upper = text.upper()
    overrides = get_overrides()

    if upper == "REQUEST":
        if not overrides.get("line.request") and _has_legacy_overrides(overrides, _LEGACY_REQUEST_OVERRIDE_KEYS):
            ctx = _bracket_context_for_status(text, runtime_minutes)
            if ctx.get("Runtime"):
                return render("bracket.with_runtime", ctx)
            return render("bracket.format", ctx)
        inner = _request_inner_line(runtime_minutes)
        return apply_wrapper(inner)

    if upper in _CANONICAL_DISPLAY_STATUSES:
        # Legacy bracket override always wins (read-compat for users who customized shape).
        if _has_legacy_overrides(overrides, _LEGACY_REASON_OVERRIDE_KEYS):
            ctx = _bracket_context_for_status(text, runtime_minutes)
            return render("bracket.format", ctx)
        # New path: resolve the canonical word via ``status.label.*`` so hidden customizations
        # (kept for read-compat) still affect on-screen text, then wrap globally.
        try:
            inner = render(f"status.label.{upper}", {})
        except UnknownTemplateKeyError:
            inner = text
        return apply_wrapper(inner)

    if _has_legacy_overrides(overrides, _LEGACY_REASON_OVERRIDE_KEYS):
        ctx = _bracket_context_for_status(text, runtime_minutes)
        if not ctx:
            return ""
        return render("bracket.format", ctx)

    return apply_wrapper(text)


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
    return str(effective).strip() or None


def project_title(title: str | None, status: str | None) -> str:
    clean_title = strip_status_from_title(title)
    clean_status = str(status or "").strip()
    if not clean_status or not should_project_status(clean_status) or get_projection_mode() not in {"title", "both"}:
        return clean_title

    from services.messages import render

    bracket = _summary_status_bracket(clean_status, None)
    return render(
        "title.suffix.format",
        {"Title": clean_title, "Bracket": bracket},
    ).strip()


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
