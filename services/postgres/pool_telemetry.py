"""SQLAlchemy pool instrumentation for diagnosing connection exhaustion.

When every pool slot is checked out, new work fails with QueuePool timeout.
This module records *who* held each connection (thread name, duration) and
tags Postgres ``application_name`` on checkout so ``pg_stat_activity`` shows
the same information server-side.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from sqlalchemy import event

from core.logger import logger

_lock = threading.Lock()
# connection_record id -> (monotonic_start, thread_name)
_active: dict[int, tuple[float, str]] = {}
_last_near_full_log_mono: float = 0.0


def _sanitize_app_name(name: str) -> str:
    out = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (name or ""))[:48]
    return out or "unknown"


def _set_pg_application_name(dbapi_connection: Any, thread_name: str) -> None:
    """Best-effort SET application_name (Postgres max ~64 chars)."""
    label = f"ph_{_sanitize_app_name(thread_name)}"
    if len(label) > 63:
        label = label[:63]
    try:
        cur = dbapi_connection.cursor()
        cur.execute("SET application_name = %s", (label,))
        cur.close()
    except Exception:
        try:
            cur.close()
        except Exception:
            pass


def register_pool_telemetry(
    engine: Any,
    *,
    pool_size: int,
    max_overflow: int,
    slow_checkin_seconds: float = 15.0,
    near_full_cooldown_seconds: float = 30.0,
    free_slots_warn_threshold: int = 3,
) -> None:
    """Attach checkout/checkin listeners once per engine.

    ``free_slots_warn_threshold``: when checked_out >= capacity - N, emit a
    snapshot of all active holders (rate-limited by ``near_full_cooldown_seconds``).
    """
    capacity = max(1, int(pool_size) + int(max_overflow))

    @event.listens_for(engine.pool, "checkout")
    def _on_checkout(dbapi_connection: Any, connection_record: Any, connection_proxy: Any) -> None:  # noqa: ARG001
        global _last_near_full_log_mono
        tname = threading.current_thread().name
        rid = id(connection_record)
        now = time.monotonic()
        with _lock:
            _active[rid] = (now, tname)

        _set_pg_application_name(dbapi_connection, tname)

        pool = getattr(connection_record, "pool", None)
        if pool is None:
            return
        try:
            checked_out = int(pool.checkedout())
        except Exception:
            return

        free = capacity - checked_out
        if free > free_slots_warn_threshold:
            return

        snap_mono = time.monotonic()
        with _lock:
            if snap_mono - _last_near_full_log_mono < near_full_cooldown_seconds:
                return
            _last_near_full_log_mono = snap_mono
            rows = sorted(
                ((snap_mono - t0, th) for _r, (t0, th) in _active.items()),
                reverse=True,
            )

        parts = [f"{th} held_s={held:.1f}" for held, th in rows[:25]]
        extra = f" (+{len(rows) - 25} more)" if len(rows) > 25 else ""
        try:
            status = str(pool.status())
        except Exception:
            status = ""
        logger.warning(
            "DB pool nearly exhausted: "
            f"checked_out={checked_out}/{capacity} free_slots≈{free}. "
            f"Active holders (longest first): {'; '.join(parts)}{extra}. "
            f"Pool status: {status}",
            extra={"emoji_type": "warning"},
        )

    @event.listens_for(engine.pool, "checkin")
    def _on_checkin(dbapi_connection: Any, connection_record: Any) -> None:  # noqa: ARG001
        rid = id(connection_record)
        end = time.monotonic()
        with _lock:
            tup = _active.pop(rid, None)
        if not tup:
            return
        start, tname = tup
        held = end - start
        if held >= slow_checkin_seconds:
            logger.warning(
                f"DB pool connection held {held:.1f}s (thread={tname!r}) — "
                "if many threads do this concurrently, pool exhaustion follows. "
                "Cross-check pg_stat_activity.application_name for ph_* labels.",
                extra={"emoji_type": "warning"},
            )


def pool_telemetry_snapshot() -> dict[str, Any]:
    """Lightweight snapshot for optional API / health use."""
    now = time.monotonic()
    with _lock:
        rows = [
            {"thread": th, "held_seconds": round(now - t0, 2)}
            for _rid, (t0, th) in sorted(_active.items(), key=lambda x: x[1][0])
        ]
        count = len(rows)
    return {"active_pool_checkouts": count, "holders": rows}
