"""Fresher live webhook state fencing against in-flight full/lite sync writers.

When an import (or similar live event) updates catalog rows, stamp
``live_catalog_touched_at``. Sync writers that loaded a stale snapshot must
skip overwriting those rows until they re-observe Arr for that entity.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from core.logger import logger

# In-memory set of (kind, id) touched since process start for the active sync run.
# Cleared when full sync ends. Survives even before DB column migration lands.
_touched: set[tuple[str, int]] = set()
_touched_lock = __import__("threading").Lock()


def mark_live_catalog_touch(
    session,
    *,
    movie_ids: Iterable[int] | None = None,
    episode_ids: Iterable[int] | None = None,
    series_ids: Iterable[int] | None = None,
) -> None:
    """Record that live event work updated these catalog rows."""
    now = datetime.now(timezone.utc)
    movies = [int(x) for x in (movie_ids or []) if x is not None]
    episodes = [int(x) for x in (episode_ids or []) if x is not None]
    series = [int(x) for x in (series_ids or []) if x is not None]
    with _touched_lock:
        for mid in movies:
            _touched.add(("movie", mid))
        for eid in episodes:
            _touched.add(("episode", eid))
        for sid in series:
            _touched.add(("series", sid))

    try:
        from services.postgres.models import Episode, Movie, Series

        if movies and hasattr(Movie, "live_catalog_touched_at"):
            session.query(Movie).filter(Movie.id.in_(movies)).update(
                {Movie.live_catalog_touched_at: now},
                synchronize_session=False,
            )
        if episodes and hasattr(Episode, "live_catalog_touched_at"):
            session.query(Episode).filter(Episode.id.in_(episodes)).update(
                {Episode.live_catalog_touched_at: now},
                synchronize_session=False,
            )
        if series and hasattr(Series, "live_catalog_touched_at"):
            session.query(Series).filter(Series.id.in_(series)).update(
                {Series.live_catalog_touched_at: now},
                synchronize_session=False,
            )
    except Exception as exc:
        logger.debug(
            f"live_catalog_touched_at column stamp skipped: {exc}",
            extra={"emoji_type": "debug"},
        )


def was_touched_during_sync(kind: str, entity_id: int) -> bool:
    with _touched_lock:
        return (str(kind), int(entity_id)) in _touched


def clear_live_catalog_touches() -> None:
    with _touched_lock:
        _touched.clear()


def filter_ids_not_live_touched(kind: str, ids: list[int]) -> list[int]:
    """Drop ids that a live event updated while sync is (or was) running."""
    out: list[int] = []
    for raw in ids:
        try:
            eid = int(raw)
        except Exception:
            continue
        if was_touched_during_sync(kind, eid):
            continue
        out.append(eid)
    return out


def sync_writer_should_skip_entity(kind: str, entity: Any) -> bool:
    """True when a sync Arr apply should not overwrite this row from a stale snapshot."""
    if entity is None:
        return False
    eid = getattr(entity, "id", None)
    if eid is None:
        return False
    if was_touched_during_sync(kind, int(eid)):
        return True
    touched = getattr(entity, "live_catalog_touched_at", None)
    if touched is None:
        return False
    # Column present: treat any non-null stamp as "live won until next explicit clear".
    # Full sync clears the in-memory set on end; column remains until next live event.
    return was_touched_during_sync(kind, int(eid))


__all__ = [
    "mark_live_catalog_touch",
    "was_touched_during_sync",
    "clear_live_catalog_touches",
    "filter_ids_not_live_touched",
    "sync_writer_should_skip_entity",
]
