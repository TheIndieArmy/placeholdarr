"""One-at-a-time gate for heavy job classes (calendar, sync, full materializer).

Phase 5 of the holistic NOTIFY audit: with multiple workers draining the
same queue, two heavy batch jobs of the same class can overlap and cause
classic lock contention. This module implements a row-based gate (using
``app_config``) that lets a handler atomically claim "I'm the singleton
for class X, please skip if you're another instance".

Why row-based instead of ``pg_advisory_lock``? Because session-scoped
advisory locks pin a DB connection for the lock's lifetime, which is
exactly the problem Phase 1 fixed for startup_sync. Row-based gates use
short-lived sessions for acquire/release/heartbeat and are released even
if the holder dies (the gate row goes stale and the next attempt steals
it).

Pattern:

    from services.source_of_truth.class_singleton import (
        try_acquire_class_singleton,
        release_class_singleton,
        heartbeat_class_singleton,
    )

    owner = f"job:{job_id}"
    if not try_acquire_class_singleton("calendar_phase", owner=owner):
        return {"ok": True, "skipped": "class_busy"}

    stop = threading.Event()
    threading.Thread(target=lambda: heartbeat_class_singleton("calendar_phase", owner=owner, stop_event=stop), daemon=True).start()
    try:
        run_calendar_phase()
    finally:
        stop.set()
        release_class_singleton("calendar_phase", owner=owner)
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import AppConfig


_GATE_KEY_PREFIX = "class_singleton:"
_DEFAULT_STALE_SECONDS = 600
_DEFAULT_HEARTBEAT_SECONDS = 60


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _gate_key(class_name: str) -> str:
    return f"{_GATE_KEY_PREFIX}{class_name}"


def try_acquire_class_singleton(
    class_name: str,
    *,
    owner: str,
    stale_seconds: int = _DEFAULT_STALE_SECONDS,
) -> bool:
    """Acquire the gate for ``class_name`` in a tiny short-lived transaction.

    Returns True if this caller is now the singleton; False if another
    owner holds a fresh claim. Uses SELECT FOR UPDATE on the gate row to
    serialize concurrent acquires.
    """
    session = get_session()
    try:
        cutoff = _now_utc() - timedelta(seconds=int(max(60, stale_seconds)))
        row = (
            session.query(AppConfig)
            .filter(AppConfig.key == _gate_key(class_name))
            .with_for_update(skip_locked=False)
            .first()
        )
        if row is None:
            row = AppConfig(
                key=_gate_key(class_name),
                value={"active": True, "owner": owner, "started_at": _now_utc().isoformat()},
                value_type="json",
                description=f"Class-singleton gate for {class_name} (row-based; not session-pinning).",
            )
            session.add(row)
            session.commit()
            return True

        existing = row.value if isinstance(row.value, dict) else {}
        active = bool(existing.get("active"))
        is_stale = bool(row.updated_at is None or row.updated_at < cutoff)
        if active and not is_stale:
            session.rollback()
            return False
        row.value = {"active": True, "owner": owner, "started_at": _now_utc().isoformat()}
        row.updated_at = _now_utc()
        session.add(row)
        session.commit()
        return True
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        logger.warning(
            f"class_singleton({class_name}) acquire failed (treating as denied): {exc}",
            extra={"emoji_type": "warning"},
        )
        return False
    finally:
        try:
            session.close()
        except Exception:
            pass


def release_class_singleton(class_name: str, *, owner: str) -> None:
    """Release the gate for ``class_name`` if we still own it. Idempotent."""
    session = get_session()
    try:
        row = (
            session.query(AppConfig)
            .filter(AppConfig.key == _gate_key(class_name))
            .with_for_update(skip_locked=False)
            .first()
        )
        if row is None:
            session.rollback()
            return
        existing = row.value if isinstance(row.value, dict) else {}
        if existing.get("owner") and existing.get("owner") != owner:
            session.rollback()
            return
        row.value = {"active": False, "owner": owner, "released_at": _now_utc().isoformat()}
        row.updated_at = _now_utc()
        session.add(row)
        session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
    finally:
        try:
            session.close()
        except Exception:
            pass


def _heartbeat_once(class_name: str, owner: str) -> None:
    session = get_session()
    try:
        row = (
            session.query(AppConfig)
            .filter(AppConfig.key == _gate_key(class_name))
            .first()
        )
        if row is None:
            session.rollback()
            return
        existing = row.value if isinstance(row.value, dict) else {}
        if existing.get("owner") != owner or not existing.get("active"):
            session.rollback()
            return
        row.updated_at = _now_utc()
        session.add(row)
        session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
    finally:
        try:
            session.close()
        except Exception:
            pass


def heartbeat_class_singleton(
    class_name: str,
    *,
    owner: str,
    stop_event: threading.Event,
    interval_seconds: int = _DEFAULT_HEARTBEAT_SECONDS,
) -> None:
    """Run as a daemon thread target: bumps gate row's updated_at periodically."""
    while not stop_event.wait(int(max(5, interval_seconds))):
        _heartbeat_once(class_name, owner)


def start_heartbeat_thread(
    class_name: str,
    *,
    owner: str,
    interval_seconds: int = _DEFAULT_HEARTBEAT_SECONDS,
) -> tuple[threading.Thread, threading.Event]:
    """Convenience wrapper: returns ``(thread, stop_event)``."""
    stop = threading.Event()
    t = threading.Thread(
        target=heartbeat_class_singleton,
        kwargs={
            "class_name": class_name,
            "owner": owner,
            "stop_event": stop,
            "interval_seconds": interval_seconds,
        },
        name=f"class-singleton-hb-{class_name}",
        daemon=True,
    )
    t.start()
    return t, stop


__all__ = [
    "try_acquire_class_singleton",
    "release_class_singleton",
    "heartbeat_class_singleton",
    "start_heartbeat_thread",
]
