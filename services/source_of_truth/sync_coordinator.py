"""Serialize full vs lite/calendar maintenance and expose pipeline lock."""

from __future__ import annotations

import threading

_pipeline_lock = threading.Lock()
_coordinator_lock = threading.Lock()
_full_sync_active = False


def is_full_sync_active() -> bool:
    with _coordinator_lock:
        return _full_sync_active


def try_begin_full_sync() -> bool:
    """Mark full sync active. Returns False when another full sync is already running."""
    global _full_sync_active
    with _coordinator_lock:
        if _full_sync_active:
            return False
        _full_sync_active = True
        return True


def begin_full_sync() -> None:
    """Force-mark full sync active (prefer ``try_begin_full_sync`` for new entry points)."""
    global _full_sync_active
    with _coordinator_lock:
        _full_sync_active = True


def end_full_sync() -> None:
    global _full_sync_active
    with _coordinator_lock:
        _full_sync_active = False
    try:
        from services.source_of_truth.live_catalog_touch import clear_live_catalog_touches

        clear_live_catalog_touches()
    except Exception:
        pass
    try:
        from services.series_episode_stats_hooks import wake_stats_refresh_worker

        wake_stats_refresh_worker()
    except Exception:
        pass
    try:
        from services.source_of_truth.scheduled_sync import kick_pending_sync_reruns

        kick_pending_sync_reruns()
    except Exception:
        pass


def should_skip_lite_or_calendar() -> bool:
    return is_full_sync_active()


def acquire_pipeline_blocking() -> None:
    _pipeline_lock.acquire(blocking=True)


def try_acquire_pipeline() -> bool:
    return _pipeline_lock.acquire(blocking=False)


def release_pipeline() -> None:
    try:
        _pipeline_lock.release()
    except RuntimeError:
        pass
