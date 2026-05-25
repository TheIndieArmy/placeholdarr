"""Job priority defaults so interactive work isn't starved by batch phases.

Phase 5 of the holistic NOTIFY audit: with all workers draining the same
queue, a flood of one job type can starve another. Instead of splitting
workers into lanes (more invasive), we add a ``priority`` column on Job
and order claims by ``priority DESC, run_after ASC, id ASC``. Higher
numbers come first.

Defaults (see ``default_priority_for``):
- 100 — webhook events, playback start (operator-visible latency)
- 50  — NFO refresh, queue monitor (semi-interactive)
- 10  — entity materialization, import grace (background, but cheap)
- 0   — full sync, calendar phase, media refresh batch (true background)
"""

from __future__ import annotations


# Symbolic constants kept in one place so callers (enqueue helpers, tests)
# agree with the worker.
PRIORITY_INTERACTIVE = 100
PRIORITY_SEMI_INTERACTIVE = 50
PRIORITY_BACKGROUND_LIGHT = 10
PRIORITY_BACKGROUND = 0


_PRIORITY_BY_TYPE: dict[str, int] = {
    "webhook_event": PRIORITY_INTERACTIVE,
    "playback_fallback": PRIORITY_INTERACTIVE,
    "nfo_refresh": PRIORITY_SEMI_INTERACTIVE,
    "placeholder_art_refresh": PRIORITY_SEMI_INTERACTIVE,
    "queue_monitor": PRIORITY_SEMI_INTERACTIVE,
    "entity_materialization": PRIORITY_BACKGROUND_LIGHT,
    "import_grace": PRIORITY_BACKGROUND_LIGHT,
    "media_refresh": PRIORITY_BACKGROUND,
    "startup_sync_runner": PRIORITY_BACKGROUND,
    "full_sync": PRIORITY_BACKGROUND,
    "calendar_phase": PRIORITY_BACKGROUND,
    "calendar_date_refresh": PRIORITY_BACKGROUND,
}


def default_priority_for(job_type: str) -> int:
    """Return the recommended priority for a Job of ``job_type``.

    Unknown job types fall back to ``PRIORITY_BACKGROUND_LIGHT`` so they
    are claimed before pure batch but after explicit interactive work.
    """
    return _PRIORITY_BY_TYPE.get(str(job_type or "").strip(), PRIORITY_BACKGROUND_LIGHT)


__all__ = [
    "PRIORITY_INTERACTIVE",
    "PRIORITY_SEMI_INTERACTIVE",
    "PRIORITY_BACKGROUND_LIGHT",
    "PRIORITY_BACKGROUND",
    "default_priority_for",
]
