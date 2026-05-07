"""Backfill machinery for template-config changes.

When a user saves customized status messages (or wrapper / separator / case settings),
existing placeholders may already be on disk with the old projected text in their NFOs
and Plex/player metadata. This module wires the three apply-scope choices the Settings
UI offers into concrete actions:

- ``now``: enqueue a single ``nfo_refresh`` follow-up job covering every active
  placeholder. Reuses the existing job pipeline so the worker rewrites NFOs and pushes
  player metadata refreshes for each title.
- ``next_full_sync``: set the ``PLACEHOLDER_TEMPLATE_BACKFILL_PENDING`` AppConfig flag.
  The next full sync (scheduled or manual) consumes it via ``run_pending_backfill_if_set``.
- ``future``: clear the pending flag (no retroactive work).

The pending flag is also honored at startup as a safety net so a deploy followed by a
sync does not silently skip the queued backfill.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import AppConfig, Placeholder
from services.source_of_truth.status_reconciler import enqueue_nfo_refresh


PENDING_FLAG_KEY = "PLACEHOLDER_TEMPLATE_BACKFILL_PENDING"


def _collect_active_placeholder_ids(session) -> list[int]:
    """Return ids for every ``has_placeholder = true`` row, ordered ascending.

    Backfill scope is "all active placeholders" in v1; per-key affected-stage filtering
    is a future optimization.
    """
    rows = (
        session.query(Placeholder.id)
        .filter(Placeholder.has_placeholder == True)  # noqa: E712
        .order_by(Placeholder.id.asc())
        .all()
    )
    return [int(r[0]) for r in rows if r and r[0] is not None]


def _set_pending_flag(session, pending: bool) -> None:
    row = session.query(AppConfig).filter(AppConfig.key == PENDING_FLAG_KEY).first()
    if row is None:
        if pending:
            session.add(
                AppConfig(
                    key=PENDING_FLAG_KEY,
                    value=True,
                    value_type="bool",
                    restart_required=False,
                    description=(
                        "Internal flag: a Status Messages save asked for backfill on the next full sync."
                    ),
                )
            )
    else:
        row.value = bool(pending)
        row.value_type = "bool"
        session.add(row)


def _read_pending_flag(session) -> bool:
    row = session.query(AppConfig).filter(AppConfig.key == PENDING_FLAG_KEY).first()
    return bool(row and row.value)


def is_template_backfill_pending() -> bool:
    """True when a template-backfill is queued to run on the next full sync."""
    session = get_session()
    try:
        return _read_pending_flag(session)
    except Exception as exc:
        logger.debug(f"Template backfill pending check failed: {exc}", extra={"emoji_type": "debug"})
        return False
    finally:
        try:
            session.close()
        except Exception:
            pass


def mark_template_backfill_pending() -> dict[str, Any]:
    """Persist the ``next_full_sync`` decision so subsequent syncs see the flag."""
    session = get_session()
    try:
        _set_pending_flag(session, True)
        session.commit()
        logger.info(
            "Template backfill scheduled for the next full sync (Status Messages save).",
            extra={"emoji_type": "calendar"},
        )
        return {"ok": True, "pending": True}
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        logger.warning(f"Failed to mark template backfill pending: {exc}", extra={"emoji_type": "warning"})
        return {"ok": False, "reason": str(exc)}
    finally:
        try:
            session.close()
        except Exception:
            pass


def clear_pending_template_backfill() -> dict[str, Any]:
    """Reset the pending flag (used by ``future`` and after ``now`` runs)."""
    session = get_session()
    try:
        _set_pending_flag(session, False)
        session.commit()
        return {"ok": True, "pending": False}
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        logger.warning(f"Failed to clear template backfill flag: {exc}", extra={"emoji_type": "warning"})
        return {"ok": False, "reason": str(exc)}
    finally:
        try:
            session.close()
        except Exception:
            pass


def enqueue_template_backfill(*, source: str = "user_save") -> dict[str, Any]:
    """Queue an NFO refresh covering every active placeholder.

    Returns a small status dict describing how many placeholders were enqueued. The actual
    NFO rewrite + projection refresh is performed asynchronously by the worker.
    """
    session = get_session()
    try:
        ids = _collect_active_placeholder_ids(session)
        if not ids:
            logger.info(
                f"Template backfill ({source}): no active placeholders to refresh.",
                extra={"emoji_type": "info"},
            )
            return {"ok": True, "placeholder_count": 0, "enqueued": False, "source": source}

        out = enqueue_nfo_refresh(
            ids,
            session=session,
            merge_into_pending=False,
            payload_extras={
                "template_backfill_source": source,
                "template_backfill_started_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        if not isinstance(out, dict) or not out.get("ok"):
            return out if isinstance(out, dict) else {"ok": False, "reason": "enqueue_failed"}

        out["placeholder_count"] = len(ids)
        out["enqueued"] = True
        out["source"] = source
        logger.info(
            f"Template backfill ({source}) queued for {len(ids)} placeholders.",
            extra={"emoji_type": "processing"},
        )
        return out
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        logger.warning(f"Template backfill ({source}) failed to enqueue: {exc}", extra={"emoji_type": "warning"})
        return {"ok": False, "reason": str(exc), "source": source}
    finally:
        try:
            session.close()
        except Exception:
            pass


def run_pending_backfill_if_set(*, source: str = "full_sync") -> dict[str, Any]:
    """If the pending flag is set, enqueue the backfill and clear the flag.

    Called from ``run_full_sync`` (and at startup as a safety net) so user saves that
    chose ``next_full_sync`` get materialized exactly once per scheduled / manual sync.
    """
    if not is_template_backfill_pending():
        return {"ok": True, "ran": False, "reason": "no_pending_flag"}

    out = enqueue_template_backfill(source=source)
    if out.get("ok"):
        clear_pending_template_backfill()
        out["ran"] = True
    else:
        out["ran"] = False
    return out


def placeholder_count_for_apply_now() -> int:
    """How many placeholders an immediate-now backfill would currently cover.

    Cheap helper exposed so the Settings UI modal can show a precise number when asking
    the user to confirm the apply-scope.
    """
    session = get_session()
    try:
        n = (
            session.query(func.count(Placeholder.id))
            .filter(Placeholder.has_placeholder == True)  # noqa: E712
            .scalar()
        )
        return int(n or 0)
    except Exception:
        return 0
    finally:
        try:
            session.close()
        except Exception:
            pass
