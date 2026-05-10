"""Backfill machinery for template-config changes.

When a user saves customized status messages (or wrapper / separator / case settings),
existing placeholders may already be on disk with the old projected text in their NFOs
and Plex/player metadata. This module wires the three apply-scope choices the Settings
UI offers into concrete actions:

- ``now``: enqueue batched ``nfo_refresh`` jobs for every active placeholder (same as
  REQUEST NFO backfill). The worker rewrites NFOs only; the last batch triggers one
  library section refresh with Plex ``force=1`` so players pick up changes without
  per-title direct projection.
- ``next_full_sync``: set the ``PLACEHOLDER_TEMPLATE_BACKFILL_PENDING`` AppConfig flag.
  The next full sync (scheduled or manual) consumes it via ``run_pending_backfill_if_set``.
- ``future``: clear the pending flag (no retroactive work).

The pending flag is also honored at startup as a safety net so a deploy followed by a
sync does not silently skip the queued backfill.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, text

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import AppConfig, Job, Placeholder
from services.source_of_truth.status_reconciler import NFO_REFRESH_JOB_TYPE, enqueue_nfo_refresh


PENDING_FLAG_KEY = "PLACEHOLDER_TEMPLATE_BACKFILL_PENDING"
# Latest template_backfill_run_id allowed to trigger the one-shot library refresh (Plex force).
# New immediate enqueue supersedes pending jobs; in-flight older batches still write NFOs but skip
# refresh_all_sections unless AppConfig still matches their payload run id.
ACTIVE_TEMPLATE_RUN_KEY = "PLACEHOLDER_TEMPLATE_BACKFILL_ACTIVE_RUN_ID"


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


def supersede_pending_template_nfo_jobs(session) -> int:
    """Mark pending immediate template backfill ``nfo_refresh`` jobs as done without processing.

    Prevents stacked full-library sweeps when the user saves ``Apply now`` repeatedly.
    """
    now = datetime.now(timezone.utc)
    rows = (
        session.query(Job)
        .filter(
            Job.job_type == NFO_REFRESH_JOB_TYPE,
            Job.status == "PENDING",
            text("(job.payload::jsonb) ? 'template_backfill_source'"),
        )
        .with_for_update(skip_locked=True)
        .all()
    )
    for job in rows:
        job.status = "DONE"
        job.error_message = "superseded_by_newer_template_backfill"
        job.updated_at = now
        session.add(job)
    return len(rows)


def _set_active_template_run_id(session, run_id: str) -> None:
    row = session.query(AppConfig).filter(AppConfig.key == ACTIVE_TEMPLATE_RUN_KEY).first()
    if row is None:
        session.add(
            AppConfig(
                key=ACTIVE_TEMPLATE_RUN_KEY,
                value=str(run_id),
                value_type="string",
                restart_required=False,
                description=(
                    "Internal: template apply-now run id allowed to trigger the completion library refresh."
                ),
            )
        )
    else:
        row.value = str(run_id)
        row.value_type = "string"
        session.add(row)


def is_active_template_backfill_run(session, run_id: str) -> bool:
    """True if ``run_id`` is still the authoritative immediate template backfill run."""
    rid = str(run_id or "").strip()
    if not rid:
        return False
    row = session.query(AppConfig).filter(AppConfig.key == ACTIVE_TEMPLATE_RUN_KEY).first()
    active = str(row.value or "").strip() if row else ""
    return bool(active) and active == rid


def clear_active_template_backfill_run_if_matches(session, run_id: str) -> None:
    """Clear active run marker after the completion refresh for that run (compare-and-clear)."""
    rid = str(run_id or "").strip()
    if not rid:
        return
    row = session.query(AppConfig).filter(AppConfig.key == ACTIVE_TEMPLATE_RUN_KEY).first()
    if row is None:
        return
    if str(row.value or "").strip() != rid:
        return
    row.value = ""
    row.value_type = "string"
    session.add(row)


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

    Returns a small status dict describing how many placeholders were enqueued. The worker
    rewrites NFOs in batches; the final batch schedules one library refresh (see
    ``status_reconciler.process_nfo_refresh_job``), matching REQUEST NFO backfill behavior.
    """
    session = get_session()
    try:
        superseded = supersede_pending_template_nfo_jobs(session)
        if superseded:
            logger.info(
                f"Template backfill ({source}): superseded {superseded} pending nfo_refresh job(s) from a prior apply-now.",
                extra={"emoji_type": "processing"},
            )

        ids = _collect_active_placeholder_ids(session)
        if not ids:
            logger.info(
                f"Template backfill ({source}): no active placeholders to refresh.",
                extra={"emoji_type": "info"},
            )
            session.commit()
            return {
                "ok": True,
                "placeholder_count": 0,
                "enqueued": False,
                "source": source,
                "superseded_pending_jobs": superseded,
            }

        run_id = uuid.uuid4().hex
        started = datetime.now(timezone.utc).isoformat()
        _set_active_template_run_id(session, run_id)

        out = enqueue_nfo_refresh(
            ids,
            session=session,
            merge_into_pending=False,
            player_metadata_refresh={int(pid): False for pid in ids},
            payload_extras={
                "template_backfill_source": source,
                "template_backfill_started_at": started,
                "template_backfill_run_id": run_id,
                "template_backfill_refresh_on_completion": True,
            },
        )
        if not isinstance(out, dict) or not out.get("ok"):
            return out if isinstance(out, dict) else {"ok": False, "reason": "enqueue_failed"}

        out["placeholder_count"] = len(ids)
        out["enqueued"] = True
        out["source"] = source
        out["superseded_pending_jobs"] = superseded
        logger.info(
            f"Template backfill ({source}) queued for {len(ids)} placeholders (run_id={run_id}).",
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
