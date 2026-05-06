"""Side-effect idempotency helpers for the NOTIFY-era worker.

Phase 4 of the holistic NOTIFY audit. The stale-CLAIMED reaper requeues rows
whose original handler thread may still be running, which means the same
external action (NFO write, Plex refresh, ARR search trigger) can fire
twice when the original handler eventually finishes.

The pattern this module enforces is "claim-before-side-effect":

    from services.source_of_truth.idempotency import (
        nfo_refresh_idempotency_key,
        try_claim_processed_key,
    )

    key = nfo_refresh_idempotency_key(job_id=job.id)
    if not try_claim_processed_key(session, key, job_type='nfo_refresh'):
        return {"ok": True, "skipped": "already_processed"}

    # ... actual side effect runs here ...

The claim is a single ``INSERT ... ON CONFLICT DO NOTHING`` issued in the
caller's session; the caller must commit to durably record the claim. Two
parallel runners racing on the same key see exactly one ``True`` return.

Idempotency is best-effort: if a handler crashes between the side effect
and the claim's commit, a re-run will repeat the side effect. Handlers
that need stronger guarantees should claim BEFORE the side effect (the
common case) so a re-run sees the key and skips, accepting the trade-off
that a partial side effect may have occurred. The plan documents this.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text

from core.logger import logger


def try_claim_processed_key(session, key: str, *, job_type: Optional[str] = None) -> bool:
    """Atomically claim ``key`` in ``processed_job_key``.

    Returns True if this caller is the first to claim the key (proceed
    with the side effect). Returns False if the key already exists (skip).

    The claim is INSERT ... ON CONFLICT DO NOTHING so two parallel
    handlers racing on the same key cleanly resolve: exactly one gets
    True. Caller must commit() the session for the claim to persist.
    """
    if not key:
        return True
    try:
        result = session.execute(
            text(
                """
                INSERT INTO processed_job_key (key, job_type, created_at)
                VALUES (:key, :job_type, now())
                ON CONFLICT (key) DO NOTHING
                """
            ),
            {"key": str(key), "job_type": str(job_type) if job_type else None},
        )
        return int(getattr(result, "rowcount", 0) or 0) > 0
    except Exception as exc:
        logger.warning(
            f"idempotency: claim failed for key={key!r}: {exc} — proceeding (best-effort)",
            extra={"emoji_type": "warning"},
        )
        # On DB error we proceed rather than block forever; the caller
        # commits anyway and the next run gets a fresh attempt.
        return True


def release_processed_key(session, key: str) -> None:
    """Delete a previously-claimed key. Idempotent.

    Used by handlers that fail BEFORE running the side effect so the next
    run is not skipped. Most handlers should NOT call this — they want
    the skip behaviour on retry.
    """
    if not key:
        return
    try:
        session.execute(
            text("DELETE FROM processed_job_key WHERE key = :key"),
            {"key": str(key)},
        )
    except Exception as exc:
        logger.debug(
            f"idempotency: release failed for key={key!r}: {exc}",
            extra={"emoji_type": "debug"},
        )


def prune_old_processed_keys(session, *, older_than_seconds: int = 30 * 24 * 3600) -> int:
    """Sweep keys older than the cutoff. Returns rowcount.

    Defaults to 30 days. The set is small in practice (one row per
    side-effecting job) and unique-indexed, so periodic pruning keeps the
    table from growing unboundedly without affecting correctness.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=int(max(60, older_than_seconds)))
    try:
        res = session.execute(
            text("DELETE FROM processed_job_key WHERE created_at < :cutoff"),
            {"cutoff": cutoff},
        )
        return int(getattr(res, "rowcount", 0) or 0)
    except Exception as exc:
        logger.debug(
            f"idempotency: prune failed: {exc}",
            extra={"emoji_type": "debug"},
        )
        return 0


# ----------------------------------------------------------------
# Stable key constructors. Centralised so handlers and tests agree.
# ----------------------------------------------------------------


def nfo_refresh_idempotency_key(*, job_id: int) -> str:
    return f"nfo_refresh:job={int(job_id)}"


def media_refresh_idempotency_key(*, job_id: int) -> str:
    return f"media_refresh:job={int(job_id)}"


def arr_search_idempotency_key(*, job_type: str, job_id: int) -> str:
    return f"arr_search:{job_type}:job={int(job_id)}"


__all__ = [
    "try_claim_processed_key",
    "release_processed_key",
    "prune_old_processed_keys",
    "nfo_refresh_idempotency_key",
    "media_refresh_idempotency_key",
    "arr_search_idempotency_key",
]
