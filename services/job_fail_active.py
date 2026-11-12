"""Fail active/hung worker jobs (operator recovery + handler watchdog).

FAILED is terminal for the queue: startup reclaim only moves CLAIMED→PENDING,
never FAILED→PENDING. Use these helpers so hung holders stop being recycled.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import Job

FAIL_ACTIVE_REASON = "failed_by_user_fail_active_jobs"
WATCHDOG_TIMEOUT_REASON = "handler_watchdog_timeout"
_ACTIVE_STATUSES = ("PENDING", "CLAIMED", "WORKING")


def fail_active_jobs(*, reason: str = FAIL_ACTIVE_REASON) -> dict[str, Any]:
    """Mark all PENDING/CLAIMED/WORKING jobs FAILED and revoke claims.

    Does not kill in-process handler threads by itself (FS/NFS hangs may still
    hold a connection). The API schedules a process restart after this so those
    threads drop. The queue itself becomes finite.
    """
    from services.worker import mark_claim_revoked

    reason_text = str(reason or FAIL_ACTIVE_REASON).strip() or FAIL_ACTIVE_REASON
    session = get_session()
    try:
        rows = (
            session.query(Job.id, Job.claim_token, Job.status, Job.job_type)
            .filter(Job.status.in_(_ACTIVE_STATUSES))
            .all()
        )
        if not rows:
            session.rollback()
            return {"ok": True, "failed_count": 0, "job_ids": [], "reason": reason_text}

        job_ids = [int(r[0]) for r in rows if r[0] is not None]
        tokens: list[tuple[int, str]] = []
        for r in rows:
            jid = int(r[0]) if r[0] is not None else 0
            tok = str(r[1] or "").strip()
            if jid and tok:
                tokens.append((jid, tok))

        (
            session.query(Job)
            .filter(Job.id.in_(job_ids), Job.status.in_(_ACTIVE_STATUSES))
            .update(
                {
                    Job.status: "FAILED",
                    Job.error_message: reason_text,
                    Job.claim_token: None,
                    Job.updated_at: func.now(),
                },
                synchronize_session=False,
            )
        )
        session.commit()
        for jid, tok in tokens:
            try:
                mark_claim_revoked(jid, tok)
            except Exception:
                pass
        logger.warning(
            f"Failed {len(job_ids)} PENDING/CLAIMED/WORKING job(s) reason={reason_text} "
            f"job_ids={job_ids[:40]}{'…' if len(job_ids) > 40 else ''}",
            extra={"emoji_type": "warning"},
        )
        return {"ok": True, "failed_count": len(job_ids), "job_ids": job_ids, "reason": reason_text}
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            session.close()
        except Exception:
            pass


def fail_claimed_job_for_watchdog(
    job_id: int,
    claim_token: str | None,
    *,
    reason: str = WATCHDOG_TIMEOUT_REASON,
) -> bool:
    """Fail one CLAIMED/WORKING row if it still matches ``claim_token``.

    Returns True when the row was updated to FAILED. For art/NFO refresh batches
    linked to a Maintenance placeholder_refresh task_run, also bumps
    ``batches_failed`` and attempts to close the parent so it does not sit WORKING
    forever after the queue drains.
    """
    from services.worker import mark_claim_revoked

    jid = int(job_id)
    tok = str(claim_token or "").strip()
    reason_text = str(reason or WATCHDOG_TIMEOUT_REASON).strip() or WATCHDOG_TIMEOUT_REASON
    if jid <= 0 or not tok:
        return False

    session = get_session()
    job_type = ""
    payload: dict[str, Any] = {}
    try:
        row = (
            session.query(Job)
            .filter(
                Job.id == jid,
                Job.status.in_(("CLAIMED", "WORKING")),
                Job.claim_token == tok,
            )
            .first()
        )
        if row is None:
            session.rollback()
            return False
        job_type = str(getattr(row, "job_type", "") or "")
        raw_payload = getattr(row, "payload", None)
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        row.status = "FAILED"
        row.error_message = reason_text
        row.claim_token = None
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        session.commit()
        try:
            mark_claim_revoked(jid, tok)
        except Exception:
            pass
        logger.warning(
            f"Watchdog failed hung job_id={jid} reason={reason_text} "
            f"(handler thread not killed; queue row is terminal FAILED)",
            extra={"emoji_type": "warning"},
        )
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        logger.warning(
            f"Watchdog fail job_id={jid} skipped: {exc}",
            extra={"emoji_type": "warning"},
        )
        return False
    finally:
        try:
            session.close()
        except Exception:
            pass

    if job_type in ("placeholder_art_refresh", "nfo_refresh"):
        try:
            from services.source_of_truth.placeholder_refresh import (
                note_placeholder_refresh_batch_terminal_failure,
                try_complete_placeholder_refresh_task_run,
            )

            raw_tid = payload.get("placeholder_refresh_task_run_id")
            if raw_tid is not None:
                batch_index = None
                try:
                    batch_index = int(payload.get("batch_index") or 0) or None
                except Exception:
                    batch_index = None
                note_placeholder_refresh_batch_terminal_failure(
                    task_run_id=int(raw_tid),
                    job_type=job_type,
                    batch_index=batch_index,
                    reason=reason_text,
                )
                try_complete_placeholder_refresh_task_run(
                    int(raw_tid),
                    failed=True,
                    error_message=reason_text,
                    exclude_job_id=jid,
                )
        except Exception as exc:
            logger.debug(
                f"Watchdog placeholder_refresh completion after job_id={jid} skipped: {exc}",
                extra={"emoji_type": "debug"},
            )
        try:
            raw_full = payload.get("full_sync_task_run_id") or payload.get("art_backfill_task_run_id")
            if raw_full is not None:
                from services.task_run_phases import try_complete_full_sync_task_run

                try_complete_full_sync_task_run(
                    int(raw_full),
                    failed=True,
                    error_message=reason_text,
                )
        except Exception as exc:
            logger.debug(
                f"Watchdog full_sync completion after job_id={jid} skipped: {exc}",
                extra={"emoji_type": "debug"},
            )
    return True
