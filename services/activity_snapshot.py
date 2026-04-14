"""In-memory snapshot of queue-monitor progress for the dashboard activity feed."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

_snapshot_lock = threading.Lock()
_snapshot: dict[str, Any] | None = None


def set_queue_download_snapshot(items: list[dict[str, Any]]) -> None:
    """Replace the live download-queue snapshot (one batched activity row in the UI)."""
    global _snapshot
    with _snapshot_lock:
        _snapshot = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "items": list(items),
        }


def clear_queue_download_snapshot() -> None:
    global _snapshot
    with _snapshot_lock:
        _snapshot = None


def get_queue_download_activity_row() -> dict[str, Any] | None:
    """Build a single synthetic activity row for /api/activity, or None when idle."""
    with _snapshot_lock:
        if not _snapshot:
            return None
        items = _snapshot.get("items") or []
        if not isinstance(items, list) or not items:
            return None
        updated = str(_snapshot.get("updated_at") or "")
        n = len(items)
        return {
            "id": "queue-monitor-active",
            "type": "job",
            "job_type": "queue_monitor_batch",
            "display_name": "Active downloads",
            "status": "WORKING",
            "details": f"{n} title(s) — watching Radarr/Sonarr until the real file is imported",
            "time": updated,
            "progress": {"running": True, "queue_items": items},
        }
