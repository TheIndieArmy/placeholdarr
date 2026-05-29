"""Serialize full vs lite/calendar maintenance and expose pipeline lock."""

from __future__ import annotations

import threading

_pipeline_lock = threading.Lock()
_coordinator_lock = threading.Lock()
_full_sync_active = False


def is_full_sync_active() -> bool:
    with _coordinator_lock:
        return _full_sync_active


def begin_full_sync() -> None:
    global _full_sync_active
    with _coordinator_lock:
        _full_sync_active = True


def end_full_sync() -> None:
    global _full_sync_active
    with _coordinator_lock:
        _full_sync_active = False


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
