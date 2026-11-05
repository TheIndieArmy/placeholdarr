"""Dedicated 10s Plex watchlist RSS loop (author-targeted sync + reconcile).

Replaces the old every-5-minutes APScheduler watchlist job. Cheap CDN polls look
for feed changes; only changed authors are fetched (Home token/switch or GraphQL).
Full roster pulls still run on the reconcile-hours interval (and on manual/startup).
Without Plex Pass / RSS, falls back to a slower full API poll (~5 minutes).
"""

from __future__ import annotations

import threading
import time
from typing import Any

from core.config import settings
from core.logger import logger

_lock = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_tick_lock = threading.Lock()
_last_fallback_mono = 0.0
_last_roster_refresh_mono = 0.0

# Light roster discover at most once per this many seconds from the loop.
_ROSTER_REFRESH_SECONDS = 300.0


def start_watchlist_rss_loop() -> None:
    """Start the daemon RSS poll thread (idempotent)."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(
            target=_run_loop,
            name="watchlist-rss-loop",
            daemon=True,
        )
        _thread.start()
        from services.watchlist.sync import (
            WATCHLIST_API_FALLBACK_SECONDS,
            WATCHLIST_RSS_POLL_SECONDS,
        )

        logger.info(
            f"Watchlist RSS loop started poll={WATCHLIST_RSS_POLL_SECONDS}s "
            f"api_fallback={WATCHLIST_API_FALLBACK_SECONDS}s",
            extra={"emoji_type": "gear"},
        )


def stop_watchlist_rss_loop() -> None:
    """Signal the RSS loop to stop (best-effort join)."""
    global _thread
    _stop.set()
    with _lock:
        t = _thread
        _thread = None
    if t is not None and t.is_alive():
        t.join(timeout=3)
    logger.info("Watchlist RSS loop stopped", extra={"emoji_type": "info"})


def watchlist_rss_loop_running() -> bool:
    t = _thread
    return t is not None and t.is_alive()


def _run_loop() -> None:
    from services.watchlist.sync import WATCHLIST_RSS_POLL_SECONDS

    while not _stop.is_set():
        try:
            _tick()
        except Exception as exc:
            logger.warning(f"Watchlist RSS loop tick failed: {exc}", extra={"emoji_type": "warning"})
        _stop.wait(timeout=max(2.0, float(WATCHLIST_RSS_POLL_SECONDS)))


def _tick() -> None:
    """One poll cycle. Skipped when disabled, no token, or a sync is already running."""
    global _last_fallback_mono, _last_roster_refresh_mono

    if not bool(getattr(settings, "ENABLE_PLEX_WATCHLIST_SYNC", False)):
        return

    from services.media_servers.plex_watchlist import plex_watchlist_token
    from services.task_run_history import get_working_run
    from services.watchlist.sync import (
        WATCHLIST_API_FALLBACK_SECONDS,
        poll_rss_for_changed_authors,
        run_watchlist_sync,
        run_watchlist_targeted_sync,
        watchlist_reconcile_due,
    )

    if not plex_watchlist_token():
        return
    if get_working_run("watchlist_sync"):
        return
    # Avoid overlapping ticks if a prior fetch overruns the poll interval.
    if not _tick_lock.acquire(blocking=False):
        return
    try:
        now = time.monotonic()
        # Occasional roster refresh so author mapping stays current without
        # discovering friends/Home on every 10s tick.
        if now - _last_roster_refresh_mono >= _ROSTER_REFRESH_SECONDS:
            try:
                from services.watchlist.roster import refresh_roster

                refresh_roster(preserve_sync_toggles=True)
            except Exception as roster_exc:
                logger.debug(
                    f"Watchlist RSS loop roster refresh skipped: {roster_exc}",
                    extra={"emoji_type": "debug"},
                )
            _last_roster_refresh_mono = now

        # Full reconcile takes priority over targeted author pulls.
        if watchlist_reconcile_due():
            logger.info(
                "Watchlist full reconcile due; pulling all enabled users",
                extra={"emoji_type": "info"},
            )
            run_watchlist_sync(trigger="reconcile")
            _last_fallback_mono = now
            return

        use_rss = bool(getattr(settings, "WATCHLIST_USE_RSS_HINT", True))
        rss_meta: dict[str, Any] = {"rss_available": False, "author_ids": set()}
        if use_rss:
            rss_meta = poll_rss_for_changed_authors()

        if rss_meta.get("rss_available"):
            authors = rss_meta.get("author_ids") or set()
            if authors:
                run_watchlist_targeted_sync(authors, trigger="rss")
            return

        # No RSS (no Pass / feeds unavailable): slower full API fallback.
        if now - _last_fallback_mono >= float(WATCHLIST_API_FALLBACK_SECONDS):
            logger.info(
                "Watchlist RSS unavailable; running API fallback pull",
                extra={"emoji_type": "info"},
            )
            run_watchlist_sync(trigger="fallback")
            _last_fallback_mono = now
    finally:
        _tick_lock.release()


def watchlist_loop_status() -> dict[str, Any]:
    """Metadata for Tasks UI (enabled, label, next reconcile)."""
    enabled = bool(getattr(settings, "ENABLE_PLEX_WATCHLIST_SYNC", False))
    use_rss = bool(getattr(settings, "WATCHLIST_USE_RSS_HINT", True))
    from services.watchlist.sync import (
        WATCHLIST_API_FALLBACK_SECONDS,
        WATCHLIST_RSS_POLL_SECONDS,
        next_watchlist_reconcile_at,
        watchlist_reconcile_hours,
    )

    if not enabled:
        label = "Disabled"
    elif use_rss:
        label = f"RSS every {int(WATCHLIST_RSS_POLL_SECONDS)}s; full every {watchlist_reconcile_hours():g}h"
    else:
        label = (
            f"API every {int(WATCHLIST_API_FALLBACK_SECONDS // 60)} min; "
            f"full every {watchlist_reconcile_hours():g}h"
        )
    nxt = next_watchlist_reconcile_at()
    return {
        "enabled": enabled,
        "running": watchlist_rss_loop_running(),
        "schedule_label": label,
        "next_reconcile": nxt.isoformat() if nxt is not None else None,
        "poll_seconds": int(WATCHLIST_RSS_POLL_SECONDS),
        "fallback_seconds": int(WATCHLIST_API_FALLBACK_SECONDS),
    }
