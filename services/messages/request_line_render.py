"""REQUEST ``line.request`` rendering helpers (kept out of status_projection to avoid import cycles)."""

from __future__ import annotations

from typing import Any

from services.messages import render
from services.messages.context import augment_context_with_runtime


def request_inner_line_synopsis(
    runtime_minutes: int | None,
    media_context: dict[str, Any] | None = None,
) -> str:
    ctx = augment_context_with_runtime(dict(media_context or {}), runtime_minutes)
    return render("line.request", ctx)
