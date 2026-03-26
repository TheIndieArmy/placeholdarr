from __future__ import annotations

import re

from core.config import settings


VALID_PROJECTION_MODES = {"summary", "title", "both", "off"}


def get_projection_mode() -> str:
    raw = str(getattr(settings, "PLACEHOLDER_STATUS_PROJECTION_MODE", "summary") or "summary").strip().lower()
    if raw in VALID_PROJECTION_MODES:
        return raw
    return "summary"


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
    text = str(summary or "")
    return re.sub(r"^\[[^\]]+\]\s*", "", text).strip()


def project_title(title: str | None, status: str | None) -> str:
    clean_title = strip_status_from_title(title)
    clean_status = str(status or "").strip()
    if not clean_status or get_projection_mode() not in {"title", "both"}:
        return clean_title
    return f"{clean_title} - [{clean_status}]".strip()


def project_summary(summary: str | None, status: str | None) -> str:
    clean_summary = strip_status_from_summary(summary)
    clean_status = str(status or "").strip()
    if not clean_status or get_projection_mode() not in {"summary", "both"}:
        return clean_summary
    return f"[{clean_status}] {clean_summary}".strip()