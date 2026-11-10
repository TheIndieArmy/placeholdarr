"""Entity-scoped advisory locks for rematerialize paths.

Global pipeline remains the gate for library-wide rewrites (full/lite determination+mat,
density Apply). Single-title rematerialize (reconcile, import grace, policy-fast, tag
policy) can take a per-entity lock so concurrent titles do not serialize on the pipeline.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import text

from core.logger import logger


def _entity_lock_key(entity_type: str, entity_id: int) -> int:
    kind = str(entity_type or "").strip().lower() or "unknown"
    raw = f"placeholdarr:entity:{kind}:{int(entity_id)}".encode("utf-8")
    # Stable signed 64-bit key for pg_try_advisory_lock(bigint).
    digest = hashlib.blake2b(raw, digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def try_acquire_entity_lock(session, *, entity_type: str, entity_id: int) -> bool:
    """Non-blocking session advisory lock for one movie/series/episode id."""
    key = _entity_lock_key(entity_type, entity_id)
    try:
        row = session.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
        return bool(row)
    except Exception as exc:
        logger.debug(
            f"entity lock acquire failed type={entity_type} id={entity_id}: {exc}",
            extra={"emoji_type": "debug"},
        )
        return False


def release_entity_lock(session, *, entity_type: str, entity_id: int) -> None:
    key = _entity_lock_key(entity_type, entity_id)
    try:
        session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
    except Exception:
        pass


@contextmanager
def entity_lock(
    session,
    *,
    entity_type: str,
    entity_id: int,
    blocking: bool = False,
) -> Iterator[bool]:
    """Yield True when the lock is held. Always unlocks on exit if acquired."""
    key = _entity_lock_key(entity_type, entity_id)
    held = False
    try:
        if blocking:
            session.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
            held = True
        else:
            held = bool(
                session.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
            )
        yield held
    finally:
        if held:
            try:
                session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            except Exception:
                pass


__all__ = [
    "try_acquire_entity_lock",
    "release_entity_lock",
    "entity_lock",
]
