"""Lightweight phase boundary metrics for sync/RAM observability."""

from __future__ import annotations

import resource
import time
from typing import Any

from core.logger import logger


def process_rss_mb() -> float | None:
    """Return this process max RSS in MiB (Linux: ru_maxrss is KiB)."""
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return float(usage.ru_maxrss) / 1024.0
    except Exception:
        return None


def arr_cache_snapshot() -> dict[str, int]:
    try:
        from services.source_of_truth.arr_api import cache_stats

        return cache_stats()
    except Exception:
        return {"entries": 0, "max_entries": 0}


def _format_metrics(
    *,
    elapsed_s: float | None = None,
    rows: int | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    parts: list[str] = []
    rss = process_rss_mb()
    if rss is not None:
        parts.append(f"rss_mb={rss:.1f}")
    cache = arr_cache_snapshot()
    parts.append(f"arr_cache_entries={int(cache.get('entries') or 0)}")
    if elapsed_s is not None:
        parts.append(f"elapsed_s={elapsed_s:.1f}")
    if rows is not None and elapsed_s is not None and elapsed_s > 0:
        parts.append(f"rows_per_s={rows / elapsed_s:.2f}")
    if extra:
        for key, value in extra.items():
            parts.append(f"{key}={value}")
    return " · ".join(parts)


def log_phase_boundary(
    phase: str,
    *,
    event: str,
    elapsed_s: float | None = None,
    rows: int | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit a single structured line at phase start/end for operator grep."""
    metrics = _format_metrics(elapsed_s=elapsed_s, rows=rows, extra=extra)
    logger.info(
        f"Phase metric · {phase} · {event} · {metrics}",
        extra={"emoji_type": "info"},
    )


class PhaseTimer:
    """Context manager that logs start/end with RSS and cache size."""

    def __init__(self, phase: str, *, rows: int | None = None, extra: dict[str, Any] | None = None):
        self.phase = phase
        self.rows = rows
        self.extra = extra
        self._started_mono: float | None = None

    def __enter__(self) -> PhaseTimer:
        self._started_mono = time.monotonic()
        log_phase_boundary(self.phase, event="start", extra=self.extra)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        elapsed = time.monotonic() - (self._started_mono or time.monotonic())
        event = "failed" if exc_type else "end"
        log_phase_boundary(
            self.phase,
            event=event,
            elapsed_s=elapsed,
            rows=self.rows,
            extra=self.extra,
        )
