"""In-memory snapshot of queue-monitor progress for the dashboard activity feed."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

_snapshot_lock = threading.Lock()
_snapshot: dict[str, Any] | None = None


def set_queue_download_snapshot(items: list[dict[str, Any]]) -> None:
    """Replace the live queue-monitor snapshot (one batched activity row in the UI).

    An empty ``items`` list is kept so the row does not disappear while Radarr/Sonarr
    still have no /queue rows during an active search.
    """
    global _snapshot
    now = datetime.now(timezone.utc).isoformat()
    new_items = list(items or [])
    with _snapshot_lock:
        prev = _snapshot
        prev_started: str | None = None
        if isinstance(prev, dict):
            prev_started = str(prev.get("started_at") or "").strip() or None
        started_at = prev_started or now
        _snapshot = {
            "updated_at": now,
            "started_at": started_at,
            "items": new_items,
        }


def clear_queue_download_snapshot() -> None:
    global _snapshot
    with _snapshot_lock:
        _snapshot = None


def get_queue_download_snapshot() -> dict[str, Any] | None:
    """Return the raw live snapshot dict, or None when the monitor is idle."""
    with _snapshot_lock:
        if not _snapshot:
            return None
        return dict(_snapshot)


def get_queue_download_activity_row() -> dict[str, Any] | None:
    """Build a single synthetic activity row for /api/activity, or None when idle."""
    with _snapshot_lock:
        if not _snapshot:
            return None
        raw_items = _snapshot.get("items")
        items = raw_items if isinstance(raw_items, list) else []
        started = str(_snapshot.get("started_at") or _snapshot.get("updated_at") or "")
        n = len(items)
        if n == 0:
            details = (
                "Monitoring Radarr/Sonarr — queue is empty; indexer search may still be running "
                "or nothing matched yet"
            )
        else:
            details = (
                f"{n} title(s) — monitoring search, queue, and import until the real file is in the library"
            )
        return {
            "id": "queue-monitor-active",
            "type": "job",
            "job_type": "queue_monitor_batch",
            "display_name": "Active searches",
            "status": "WORKING",
            "details": details,
            "time": started,
            "progress": {"running": True, "queue_items": items},
        }
