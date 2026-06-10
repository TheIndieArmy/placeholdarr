"""Server-sent dashboard events: liveness pings and cheap state deltas."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

PING_INTERVAL_S = 20.0
POLL_INTERVAL_S = 0.75


def _dashboard_state_events() -> list[dict[str, Any]]:
    from services.startup_gate import startup_sync_complete

    events: list[dict[str, Any]] = [
        {"type": "startup_sync_complete", "value": bool(startup_sync_complete.is_set())},
    ]

    try:
        from services.library_catalog_version import get_library_versions
        from services.postgres.db import get_session

        session = get_session()
        try:
            versions = get_library_versions(session)
            events.append(
                {
                    "type": "library_version",
                    "movies_version": int(versions.get("movies_version") or 0),
                    "series_version": int(versions.get("series_version") or 0),
                }
            )
        finally:
            session.close()
    except Exception:
        pass

    return events


def _state_fingerprint(events: list[dict[str, Any]]) -> str:
    return json.dumps(events, sort_keys=True, separators=(",", ":"))


async def iter_dashboard_events() -> AsyncIterator[dict[str, Any]]:
    """Yield dashboard SSE payloads; emits snapshots on change and periodic pings."""
    last_fingerprint = ""
    last_ping_at = 0.0

    while True:
        loop_started = time.monotonic()
        events = await asyncio.to_thread(_dashboard_state_events)
        fingerprint = _state_fingerprint(events)
        if fingerprint != last_fingerprint:
            for event in events:
                yield event
            last_fingerprint = fingerprint

        if loop_started - last_ping_at >= PING_INTERVAL_S:
            yield {"type": "ping", "ts": int(time.time() * 1000)}
            last_ping_at = loop_started

        await asyncio.sleep(POLL_INTERVAL_S)
