"""REQUEST line rendering helpers (kept out of status_projection to avoid import cycles)."""

from __future__ import annotations

from typing import Any

from services.messages import render
from services.messages.context import MEDIA_KIND_KEY, augment_context_with_runtime
from services.messages.registry import request_line_key_for_media


def request_inner_line_synopsis(
    runtime_minutes: int | None,
    media_context: dict[str, Any] | None = None,
    *,
    media_kind: str | None = None,
) -> str:
    """Render the movie or TV REQUEST synopsis inner line for the given media context."""
    ctx = augment_context_with_runtime(dict(media_context or {}), runtime_minutes)
    kind = str(media_kind or "").strip() or str(ctx.get(MEDIA_KIND_KEY) or "").strip()
    key = request_line_key_for_media(kind)
    return render(key, ctx)
