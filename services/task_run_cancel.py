"""Process-local cancel flags for in-progress ``scheduled_task_run`` rows.

``abandon_task_run`` sets the flag so long pipelines can stop at the next safe
checkpoint (per item / small chunk), without requiring an app restart. Job-batched
follow-ups (art/NFO) are also failed so queued work does not start.
"""

from __future__ import annotations

import contextvars
import threading
from contextlib import contextmanager
from typing import Iterator

_lock = threading.Lock()
_cancelled: set[int] = set()
_active_task_run_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "placeholdarr_active_task_run_id",
    default=None,
)

# How often long loops call ``ensure_active_task_run_not_cancelled`` when using
# the counter helper (1 = every item).
DEFAULT_CANCEL_CHECK_EVERY = 1


class TaskRunCancelled(Exception):
    """Raised when a task run should stop after Stop / abandon was requested."""


def request_task_run_cancel(task_run_id: int) -> None:
    with _lock:
        _cancelled.add(int(task_run_id))


def clear_task_run_cancel(task_run_id: int) -> None:
    with _lock:
        _cancelled.discard(int(task_run_id))


def is_task_run_cancel_requested(task_run_id: int | None) -> bool:
    if task_run_id is None:
        return False
    with _lock:
        return int(task_run_id) in _cancelled


def ensure_task_run_not_cancelled(task_run_id: int | None) -> None:
    """Raise ``TaskRunCancelled`` when Stop was requested for this run."""
    if is_task_run_cancel_requested(task_run_id):
        raise TaskRunCancelled(f"task_run_id={int(task_run_id)} cancelled")


def active_task_run_id() -> int | None:
    return _active_task_run_id.get()


def ensure_active_task_run_not_cancelled() -> None:
    """Raise when the ContextVar-scoped task run was stopped."""
    ensure_task_run_not_cancelled(_active_task_run_id.get())


def maybe_check_active_cancel(counter: int, *, every: int = DEFAULT_CANCEL_CHECK_EVERY) -> None:
    """Check cancel on every ``every``-th item (``counter`` is 1-based)."""
    step = max(1, int(every or 1))
    if int(counter) % step == 0:
        ensure_active_task_run_not_cancelled()


@contextmanager
def task_run_cancel_scope(task_run_id: int | None) -> Iterator[None]:
    """Bind ``task_run_id`` for nested loops (determination, materialization, art scan)."""
    tid = int(task_run_id) if task_run_id is not None else None
    token = _active_task_run_id.set(tid)
    try:
        yield
    finally:
        _active_task_run_id.reset(token)
