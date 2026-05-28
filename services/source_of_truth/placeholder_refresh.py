"""Unified placeholder refresh intent + apply-scope orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import AppConfig

PENDING_REFRESH_KEY = "PLACEHOLDER_REFRESH_PENDING"
PENDING_REFRESH_VERSION = 1

PLACEHOLDER_REFRESH_TASK_RUN_ID_KEY = "placeholder_refresh_task_run_id"
PLACEHOLDER_REFRESH_RUN_ID_KEY = "placeholder_refresh_run_id"
PLACEHOLDER_REFRESH_SUBSTEP_KEY = "placeholder_refresh_substep"

from services.task_run_history import begin_task_run, finish_task_run, update_task_run_summary


def _empty_intent(*, source: str | None = None) -> dict[str, Any]:
    return {
        "version": PENDING_REFRESH_VERSION,
        "metadata": False,
        "art": False,
        "templates": False,
        "source": str(source or "").strip(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _normalize_intent(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _empty_intent()
    out = _empty_intent(source=str(raw.get("source") or "").strip())
    out["version"] = int(raw.get("version") or PENDING_REFRESH_VERSION)
    out["metadata"] = bool(raw.get("metadata"))
    out["art"] = bool(raw.get("art"))
    out["templates"] = bool(raw.get("templates"))
    if isinstance(raw.get("updated_at"), str) and str(raw.get("updated_at")).strip():
        out["updated_at"] = str(raw["updated_at"]).strip()
    return out


def _legacy_intent_flags(session) -> dict[str, bool]:
    out = {"metadata": False, "art": False, "templates": False}
    try:
        from services.source_of_truth.template_backfill import PENDING_FLAG_KEY as LEGACY_TEMPLATE_PENDING_KEY

        row = session.query(AppConfig).filter(AppConfig.key == LEGACY_TEMPLATE_PENDING_KEY).first()
        if row and bool(row.value):
            out["metadata"] = True
            out["templates"] = True
    except Exception:
        pass
    try:
        from services.source_of_truth.art_backfill import PENDING_FLAG_KEY as LEGACY_ART_PENDING_KEY

        row = session.query(AppConfig).filter(AppConfig.key == LEGACY_ART_PENDING_KEY).first()
        if row and bool(row.value):
            out["art"] = True
    except Exception:
        pass
    return out


def _get_pending_row(session):
    return session.query(AppConfig).filter(AppConfig.key == PENDING_REFRESH_KEY).first()


def _set_pending_intent(session, intent: dict[str, Any]) -> None:
    payload = _normalize_intent(intent)
    row = _get_pending_row(session)
    if row is None:
        row = AppConfig(
            key=PENDING_REFRESH_KEY,
            value=payload,
            value_type="json",
            restart_required=False,
            description="Internal: pending placeholder refresh intent (metadata/art/templates).",
        )
        session.add(row)
        return
    row.value = payload
    row.value_type = "json"
    session.add(row)


def get_pending_intent() -> dict[str, Any]:
    session = get_session()
    try:
        row = _get_pending_row(session)
        intent = _normalize_intent(row.value if row else None)
        legacy = _legacy_intent_flags(session)
        intent["metadata"] = bool(intent["metadata"] or legacy["metadata"])
        intent["art"] = bool(intent["art"] or legacy["art"])
        intent["templates"] = bool(intent["templates"] or legacy["templates"])
        return intent
    finally:
        session.close()


def has_pending_intent() -> bool:
    intent = get_pending_intent()
    return bool(intent.get("metadata") or intent.get("art") or intent.get("templates"))


def merge_pending_intent(
    *,
    metadata: bool = False,
    art: bool = False,
    templates: bool = False,
    source: str | None = None,
) -> dict[str, Any]:
    session = get_session()
    try:
        row = _get_pending_row(session)
        merged = _normalize_intent(row.value if row else None)
        legacy = _legacy_intent_flags(session)
        merged["metadata"] = bool(merged["metadata"] or legacy["metadata"] or metadata)
        merged["art"] = bool(merged["art"] or legacy["art"] or art)
        merged["templates"] = bool(merged["templates"] or legacy["templates"] or templates)
        merged["source"] = str(source or merged.get("source") or "").strip()
        merged["updated_at"] = datetime.now(timezone.utc).isoformat()
        _set_pending_intent(session, merged)
        session.commit()
        return merged
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def clear_pending_intent() -> dict[str, Any]:
    intent = _empty_intent()
    session = get_session()
    try:
        _set_pending_intent(session, intent)
        session.commit()
        return intent
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _refresh_enqueue_committed(result: Any) -> bool:
    """True when an enqueue helper completed successfully (work queued or nothing to do)."""
    if not isinstance(result, dict):
        return False
    if not bool(result.get("ok")):
        return False
    if bool(result.get("enqueued")):
        return True
    try:
        if int(result.get("jobs_created") or 0) > 0:
            return True
        if int(result.get("jobs_updated") or 0) > 0:
            return True
        job_ids = result.get("job_ids")
        if isinstance(job_ids, list) and len(job_ids) > 0:
            return True
        if result.get("job_id") is not None:
            return True
        return int(result.get("placeholder_count") or 0) == 0
    except (TypeError, ValueError):
        return False


def _finalize_placeholder_refresh_enqueue(
    *,
    task_run_id: int,
    summary: dict[str, Any],
    out: dict[str, Any],
    metadata: bool,
    art: bool,
    metadata_committed: bool,
    art_committed: bool,
) -> dict[str, Any]:
    """Close the task run only when every requested domain failed to enqueue; otherwise stay working."""
    metadata_failed = bool(metadata and not metadata_committed)
    art_failed = bool(art and not art_committed)
    if not metadata_failed and not art_failed:
        return out

    merged = dict(summary)
    if metadata_failed:
        merged = _mark_phase_failed(merged, "metadata_refresh", reason="enqueue_failed")
    if art_failed:
        merged = _mark_phase_failed(merged, "art_refresh", reason="enqueue_failed")

    any_committed = (bool(metadata) and metadata_committed) or (bool(art) and art_committed)
    if not any_committed:
        out = {**out, "ok": False, "reason": "enqueue_failed"}
        merged = _persist_placeholder_refresh_progress(task_run_id, {**merged, **out}, overall_status="FAILED")
        finish_task_run(task_run_id, status="failed", summary=merged, error_message="enqueue_failed")
        return out

    # Partial enqueue: keep task working for domains that did queue work.
    out = {**out, "ok": True, "partial_enqueue_failure": True}
    if metadata_failed:
        out["metadata_enqueue_failed"] = True
    if art_failed:
        out["art_enqueue_failed"] = True
    merged = _persist_placeholder_refresh_progress(task_run_id, {**merged, **out}, overall_status="WORKING")
    update_task_run_summary(task_run_id, merged)
    return out


def clear_pending_intent_domains(
    *,
    metadata: bool = False,
    art: bool = False,
    templates: bool = False,
) -> dict[str, Any]:
    """Clear only the requested pending refresh domains; leave other flags intact."""
    if not metadata and not art and not templates:
        return get_pending_intent()

    session = get_session()
    try:
        row = _get_pending_row(session)
        merged = _normalize_intent(row.value if row else None)
        if metadata or templates:
            merged["metadata"] = False
            merged["templates"] = False
        if art:
            merged["art"] = False
        merged["updated_at"] = datetime.now(timezone.utc).isoformat()
        _set_pending_intent(session, merged)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    if metadata or templates:
        try:
            from services.source_of_truth.template_backfill import clear_pending_template_backfill

            clear_pending_template_backfill()
        except Exception as exc:
            logger.warning(
                f"Failed clearing legacy template backfill pending flag: {exc}",
                extra={"emoji_type": "warning"},
            )
    if art:
        try:
            from services.source_of_truth.art_backfill import clear_pending_art_backfill

            clear_pending_art_backfill()
        except Exception as exc:
            logger.warning(
                f"Failed clearing legacy art backfill pending flag: {exc}",
                extra={"emoji_type": "warning"},
            )
    return merged


def execute_placeholder_refresh_apply_scope(
    *,
    apply_scope: str,
    metadata: bool = False,
    art: bool = False,
    templates: bool = False,
    source: str = "settings_save",
    task_run_trigger: str | None = None,
) -> dict[str, Any]:
    scope = str(apply_scope or "future").strip().lower()
    if scope not in {"now", "next_full_sync", "future"}:
        scope = "future"

    requested_metadata = bool(metadata or templates)
    requested_art = bool(art)
    if not requested_metadata and not requested_art:
        return {
            "ok": True,
            "scope": scope,
            "enqueued": False,
            "pending": has_pending_intent(),
        }

    if scope == "future":
        cleared = clear_pending_intent_domains(
            metadata=requested_metadata,
            art=requested_art,
            templates=bool(templates),
        )
        return {
            "ok": True,
            "scope": "future",
            "enqueued": False,
            "pending": bool(cleared.get("metadata") or cleared.get("art") or cleared.get("templates")),
        }

    if scope == "next_full_sync":
        out = merge_pending_intent(
            metadata=requested_metadata,
            art=requested_art,
            templates=bool(templates),
            source=source,
        )
        out.update({"ok": True, "scope": "next_full_sync", "enqueued": False, "pending": True})
        return out

    # scope == "now"
    metadata_committed = False
    art_committed = False
    task_run_id: int | None = None
    task_summary: dict[str, Any] | None = None
    track_task_run = bool(str(task_run_trigger or "").strip())
    if track_task_run:
        trigger = str(task_run_trigger).strip().lower()
        task_run_id = begin_task_run(task_key="placeholder_refresh", trigger=trigger)
        task_summary = _initial_refresh_summary(
            metadata=requested_metadata,
            art=requested_art,
            source=source,
            trigger=trigger,
        )
        task_summary = _persist_placeholder_refresh_progress(task_run_id, task_summary, overall_status="WORKING")

    out: dict[str, Any] = {
        "ok": True,
        "scope": "now",
        "enqueued": False,
        "metadata_requested": requested_metadata,
        "art_requested": requested_art,
    }
    if task_run_id is not None:
        out["task_run_id"] = task_run_id

    enqueue_kwargs: dict[str, Any] = {}
    if task_run_id is not None:
        enqueue_kwargs["placeholder_refresh_task_run_id"] = int(task_run_id)

    if requested_metadata:
        from services.source_of_truth.template_backfill import enqueue_template_backfill

        nfo_out = enqueue_template_backfill(source=f"{source}:apply_now", **enqueue_kwargs)
        out["nfo_backfill"] = nfo_out
        metadata_committed = _refresh_enqueue_committed(nfo_out)
        out["enqueued"] = bool(out["enqueued"] or metadata_committed and bool(nfo_out.get("enqueued")))
    if requested_art:
        from services.source_of_truth.placeholder_art_reconciler import enqueue_placeholder_art_backfill_all

        art_out = enqueue_placeholder_art_backfill_all(source=f"{source}:apply_now", **enqueue_kwargs)
        out["art_backfill"] = art_out
        art_committed = _refresh_enqueue_committed(art_out)
        out["enqueued"] = bool(out["enqueued"] or art_committed and bool(art_out.get("enqueued")))

    if task_run_id is not None and task_summary is not None:
        if (requested_metadata and not metadata_committed) or (requested_art and not art_committed):
            return _finalize_placeholder_refresh_enqueue(
                task_run_id=task_run_id,
                summary=task_summary,
                out=out,
                metadata=requested_metadata,
                art=requested_art,
                metadata_committed=metadata_committed,
                art_committed=art_committed,
            )
        if not out["enqueued"]:
            phases = list(task_summary.get("phases") or [])
            for phase in phases:
                if str(phase.get("status") or "").lower() == "working":
                    phase["status"] = "skipped"
                    phase["metrics"] = [{"label": "Reason", "value": "no_active_placeholders"}]
            done_summary = _persist_placeholder_refresh_progress(
                task_run_id,
                {**task_summary, "phases": phases, **out},
                overall_status="DONE",
            )
            finish_task_run(task_run_id, status="done", summary=done_summary)
            return out
        merged = _persist_placeholder_refresh_progress(
            task_run_id, {**task_summary, **out}, overall_status="WORKING"
        )
        update_task_run_summary(task_run_id, merged)

    clear_metadata = bool(requested_metadata and metadata_committed)
    clear_art = bool(requested_art and art_committed)
    if clear_metadata or clear_art:
        try:
            if clear_metadata and clear_art:
                cleared = clear_pending_intent()
            else:
                cleared = clear_pending_intent_domains(
                    metadata=clear_metadata,
                    art=clear_art,
                    templates=bool(templates) and clear_metadata,
                )
            out["pending"] = bool(cleared.get("metadata") or cleared.get("art") or cleared.get("templates"))
        except Exception as exc:
            logger.warning(f"Failed clearing pending placeholder refresh intent: {exc}", extra={"emoji_type": "warning"})
    else:
        out["pending"] = has_pending_intent()

    if (requested_metadata and not metadata_committed) or (requested_art and not art_committed):
        out["ok"] = False
        out["reason"] = "enqueue_failed"
    return out


def _collect_active_placeholder_ids(session) -> list[int]:
    from services.postgres.models import Placeholder

    rows = (
        session.query(Placeholder.id)
        .filter(Placeholder.has_placeholder == True)  # noqa: E712
        .order_by(Placeholder.id.asc())
        .all()
    )
    return [int(r[0]) for r in rows if r and r[0] is not None]


def placeholder_refresh_task_label(summary: dict[str, Any] | None) -> str:
    """Human label for Tasks queue rows."""
    if not isinstance(summary, dict):
        return "Metadata & art refresh"
    metadata = bool(summary.get("metadata_requested"))
    art = bool(summary.get("art_requested"))
    if metadata and art:
        return "Metadata & art refresh"
    if metadata:
        return "Metadata refresh"
    if art:
        return "Art refresh"
    return "Placeholder refresh"


def _persist_placeholder_refresh_progress(
    task_run_id: int,
    summary: dict[str, Any],
    *,
    overall_status: str | None = None,
) -> dict[str, Any]:
    from services.task_run_phases import _save_phases, phases_from_summary

    phases = phases_from_summary(summary) or list(summary.get("phases") or [])
    any_working = any(str(p.get("status") or "").lower() == "working" for p in phases)
    status = overall_status or ("WORKING" if any_working else "DONE")
    _save_phases(int(task_run_id), phases, extra=summary)
    merged = dict(summary)
    merged["phases"] = phases
    return merged


def _initial_refresh_summary(*, metadata: bool, art: bool, source: str, trigger: str) -> dict[str, Any]:
    phases: list[dict[str, Any]] = []
    phases.append(
        {
            "key": "metadata_refresh",
            "name": "Metadata refresh",
            "status": "working" if metadata else "skipped",
            "started_at": datetime.now(timezone.utc).isoformat() if metadata else None,
            "metrics": ([] if metadata else [{"label": "Reason", "value": "not_requested"}]),
        }
    )
    phases.append(
        {
            "key": "art_refresh",
            "name": "Art refresh",
            "status": "working" if art else "skipped",
            "started_at": datetime.now(timezone.utc).isoformat() if art else None,
            "metrics": ([] if art else [{"label": "Reason", "value": "not_requested"}]),
        }
    )
    return {
        "mode": "placeholder_refresh",
        "trigger": trigger,
        "source": source,
        "metadata_requested": metadata,
        "art_requested": art,
        "phases": phases,
    }


def run_placeholder_refresh_task(
    *,
    source: str = "manual",
    trigger: str = "manual",
    metadata: bool = True,
    art: bool = True,
) -> dict[str, Any]:
    """Start a coordinated placeholder refresh task run and enqueue needed batches."""
    metadata = bool(metadata)
    art = bool(art)
    task_run_id = begin_task_run(task_key="placeholder_refresh", trigger=trigger)
    summary = _initial_refresh_summary(metadata=metadata, art=art, source=source, trigger=trigger)
    summary = _persist_placeholder_refresh_progress(task_run_id, summary, overall_status="WORKING")

    if not metadata and not art:
        finish_task_run(task_run_id, status="done", summary=summary)
        return {"ok": True, "task_run_id": task_run_id, "enqueued": False, "reason": "nothing_requested"}

    session = get_session()
    try:
        ids = _collect_active_placeholder_ids(session)
        if not ids:
            phases = summary.get("phases") if isinstance(summary.get("phases"), list) else []
            for p in phases:
                if str(p.get("status") or "").lower() == "working":
                    p["status"] = "skipped"
                    p["metrics"] = [{"label": "Reason", "value": "no_active_placeholders"}]
            summary = {**summary, "phases": phases}
            summary = _persist_placeholder_refresh_progress(task_run_id, summary, overall_status="DONE")
            finish_task_run(task_run_id, status="done", summary=summary)
            return {"ok": True, "task_run_id": task_run_id, "enqueued": False, "reason": "no_active_placeholders"}

        run_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        out: dict[str, Any] = {"ok": True, "task_run_id": task_run_id, "enqueued": False, "run_id": run_id}
        metadata_committed = False
        art_committed = False
        if metadata:
            from services.source_of_truth.status_reconciler import enqueue_nfo_refresh

            nfo_extras = {
                PLACEHOLDER_REFRESH_TASK_RUN_ID_KEY: int(task_run_id),
                PLACEHOLDER_REFRESH_RUN_ID_KEY: run_id,
                PLACEHOLDER_REFRESH_SUBSTEP_KEY: "metadata",
                "request_backfill_run_id": f"placeholder_refresh:{run_id}",
                "request_backfill_refresh_on_completion": not art,
            }
            nfo_out = enqueue_nfo_refresh(
                ids,
                session=session,
                merge_into_pending=False,
                player_metadata_refresh={int(pid): False for pid in ids},
                payload_extras=nfo_extras,
            )
            out["nfo_backfill"] = nfo_out
            metadata_committed = _refresh_enqueue_committed(nfo_out)
            out["enqueued"] = bool(out["enqueued"] or (metadata_committed and bool(nfo_out.get("enqueued"))))
            if metadata_committed:
                from services.task_run_phases import begin_nfo_backfill_phase

                batch_count = int(nfo_out.get("jobs_created") or len(nfo_out.get("job_ids") or []) or 0)
                begin_nfo_backfill_phase(
                    int(task_run_id),
                    nfo_run_id=f"placeholder_refresh:{run_id}",
                    placeholder_count=len(ids),
                    batch_count=batch_count,
                    source=source,
                )
        if art:
            from services.source_of_truth.placeholder_art_reconciler import (
                ART_BACKFILL_REFRESH_ON_COMPLETION_KEY,
                ART_BACKFILL_RUN_ID_KEY,
                enqueue_placeholder_art_refresh,
            )

            art_extras = {
                PLACEHOLDER_REFRESH_TASK_RUN_ID_KEY: int(task_run_id),
                PLACEHOLDER_REFRESH_RUN_ID_KEY: run_id,
                PLACEHOLDER_REFRESH_SUBSTEP_KEY: "art",
                ART_BACKFILL_RUN_ID_KEY: f"placeholder_refresh:{run_id}",
                ART_BACKFILL_REFRESH_ON_COMPLETION_KEY: True,
            }
            art_out = enqueue_placeholder_art_refresh(
                ids,
                session=session,
                merge_into_pending=False,
                payload_extras=art_extras,
            )
            out["art_backfill"] = art_out
            art_committed = _refresh_enqueue_committed(art_out)
            out["enqueued"] = bool(out["enqueued"] or (art_committed and bool(art_out.get("enqueued"))))
            if art_committed:
                from services.task_run_phases import begin_art_backfill_phase

                batch_count = int(art_out.get("jobs_created") or art_out.get("batch_count") or 0)
                begin_art_backfill_phase(
                    int(task_run_id),
                    art_run_id=f"placeholder_refresh:{run_id}",
                    placeholder_count=len(ids),
                    batch_count=batch_count,
                    source=source,
                )

        if (metadata and not metadata_committed) or (art and not art_committed):
            return _finalize_placeholder_refresh_enqueue(
                task_run_id=task_run_id,
                summary=summary,
                out=out,
                metadata=metadata,
                art=art,
                metadata_committed=metadata_committed,
                art_committed=art_committed,
            )
        merged = _persist_placeholder_refresh_progress(task_run_id, {**summary, **out}, overall_status="WORKING")
        update_task_run_summary(task_run_id, merged)
        return out
    except Exception as exc:
        session.rollback()
        finish_task_run(task_run_id, status="failed", summary=summary, error_message=str(exc))
        return {"ok": False, "task_run_id": task_run_id, "reason": str(exc)}
    finally:
        session.close()


def enqueue_scoped_placeholder_refresh(
    *,
    placeholder_ids: list[int],
    source: str,
    metadata: bool = True,
    art: bool = True,
    player_metadata_refresh: bool = True,
) -> dict[str, Any]:
    """Queue metadata/art refresh for a scoped placeholder set."""
    ids = sorted({int(pid) for pid in (placeholder_ids or []) if pid is not None})
    if not ids:
        return {"ok": False, "reason": "no_placeholder_ids"}
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    out: dict[str, Any] = {"ok": True, "enqueued": False, "run_id": run_id}
    metadata_committed = False
    art_committed = False
    session = get_session()
    try:
        if metadata:
            from services.source_of_truth.status_reconciler import enqueue_nfo_refresh

            nfo_out = enqueue_nfo_refresh(
                ids,
                session=session,
                merge_into_pending=False,
                player_metadata_refresh={int(pid): bool(player_metadata_refresh) for pid in ids},
                payload_extras={
                    PLACEHOLDER_REFRESH_RUN_ID_KEY: f"scoped:{run_id}",
                    PLACEHOLDER_REFRESH_SUBSTEP_KEY: "metadata",
                    "request_backfill_run_id": f"scoped:{run_id}",
                    "request_backfill_refresh_on_completion": not art,
                },
            )
            out["nfo_backfill"] = nfo_out
            metadata_committed = _refresh_enqueue_committed(nfo_out)
            out["enqueued"] = bool(out["enqueued"] or (metadata_committed and bool(nfo_out.get("enqueued"))))
        if art:
            from services.source_of_truth.placeholder_art_reconciler import (
                ART_BACKFILL_REFRESH_ON_COMPLETION_KEY,
                ART_BACKFILL_RUN_ID_KEY,
                enqueue_placeholder_art_refresh,
            )

            art_out = enqueue_placeholder_art_refresh(
                ids,
                session=session,
                merge_into_pending=False,
                payload_extras={
                    PLACEHOLDER_REFRESH_RUN_ID_KEY: f"scoped:{run_id}",
                    PLACEHOLDER_REFRESH_SUBSTEP_KEY: "art",
                    ART_BACKFILL_RUN_ID_KEY: f"scoped:{run_id}",
                    ART_BACKFILL_REFRESH_ON_COMPLETION_KEY: True,
                },
            )
            out["art_backfill"] = art_out
            art_committed = _refresh_enqueue_committed(art_out)
            out["enqueued"] = bool(out["enqueued"] or (art_committed and bool(art_out.get("enqueued"))))
        if (metadata and not metadata_committed) or (art and not art_committed):
            out["ok"] = False
            out["reason"] = "enqueue_failed"
        return out
    finally:
        session.close()


def _mark_phase_done(summary: dict[str, Any], phase_key: str) -> dict[str, Any]:
    phases = list(summary.get("phases") or [])
    now = datetime.now(timezone.utc).isoformat()
    for p in phases:
        if str(p.get("key") or "") != phase_key:
            continue
        if str(p.get("status") or "").lower() != "working":
            continue
        p["status"] = "done"
        p["ended_at"] = now
    summary["phases"] = phases
    return summary


def _mark_phase_failed(summary: dict[str, Any], phase_key: str, *, reason: str | None = None) -> dict[str, Any]:
    phases = list(summary.get("phases") or [])
    now = datetime.now(timezone.utc).isoformat()
    for p in phases:
        if str(p.get("key") or "") != phase_key:
            continue
        if str(p.get("status") or "").lower() == "skipped":
            continue
        p["status"] = "failed"
        p["ended_at"] = now
        if reason:
            metrics = list(p.get("metrics") or [])
            metrics.append({"label": "Reason", "value": str(reason)})
            p["metrics"] = metrics
    summary["phases"] = phases
    return summary


def try_complete_placeholder_refresh_task_run(
    task_run_id: int,
    *,
    failed: bool = False,
    error_message: str | None = None,
    exclude_job_id: int | None = None,
) -> bool:
    from sqlalchemy import String, cast
    from services.postgres.models import Job, ScheduledTaskRun

    session = get_session()
    try:
        tid = str(int(task_run_id))
        q = session.query(Job.status).filter(
            Job.job_type.in_(("placeholder_art_refresh", "nfo_refresh")),
            cast(Job.payload[PLACEHOLDER_REFRESH_TASK_RUN_ID_KEY], String) == tid,
        )
        if exclude_job_id is not None:
            q = q.filter(Job.id != int(exclude_job_id))
        statuses = [str(r[0] or "").upper() for r in q.all() if r and r[0] is not None]
        if any(s in ("PENDING", "CLAIMED") for s in statuses):
            return False

        row = session.query(ScheduledTaskRun).filter(ScheduledTaskRun.id == int(task_run_id)).first()
        if not row or str(row.status or "").lower() != "working":
            return False
        summary = row.summary if isinstance(row.summary, dict) else {}
        has_failed_job = any(s == "FAILED" for s in statuses)
        if failed or has_failed_job:
            fail_reason = str(error_message or "linked_refresh_job_failed")
            summary = _mark_phase_failed(summary, "metadata_refresh", reason=fail_reason)
            summary = _mark_phase_failed(summary, "art_refresh", reason=fail_reason)
            summary = _persist_placeholder_refresh_progress(task_run_id, summary, overall_status="FAILED")
            finish_task_run(task_run_id, status="failed", summary=summary, error_message=fail_reason)
            return True
        summary = _mark_phase_done(summary, "metadata_refresh")
        summary = _mark_phase_done(summary, "art_refresh")
        summary = _persist_placeholder_refresh_progress(task_run_id, summary, overall_status="DONE")
        finish_task_run(task_run_id, status="done", summary=summary)
        return True
    finally:
        session.close()


def reconcile_stuck_placeholder_refresh_tasks() -> int:
    from services.postgres.models import ScheduledTaskRun

    session = get_session()
    fixed = 0
    try:
        rows = (
            session.query(ScheduledTaskRun)
            .filter(ScheduledTaskRun.task_key == "placeholder_refresh", ScheduledTaskRun.status == "working")
            .all()
        )
        for row in rows:
            if try_complete_placeholder_refresh_task_run(int(row.id)):
                fixed += 1
    finally:
        session.close()
    return fixed


def run_placeholder_refresh_if_pending(*, source: str, task_run_id: int) -> dict[str, Any]:
    """Consume unified pending intent from a full sync and enqueue needed follow-up batches."""
    intent = get_pending_intent()
    metadata = bool(intent.get("metadata") or intent.get("templates"))
    art = bool(intent.get("art"))
    if not metadata and not art:
        return {"ok": True, "enqueued": False, "reason": "not_requested"}

    metadata_committed = False
    art_committed = False
    out: dict[str, Any] = {"ok": True, "enqueued": False, "metadata": metadata, "art": art}
    if metadata:
        from services.source_of_truth.template_backfill import enqueue_template_backfill

        nfo_out = enqueue_template_backfill(source=source, task_run_id=task_run_id)
        out["nfo_backfill"] = nfo_out
        metadata_committed = _refresh_enqueue_committed(nfo_out)
        out["enqueued"] = bool(out["enqueued"] or metadata_committed and bool(nfo_out.get("enqueued")))
    if art:
        from services.source_of_truth.placeholder_art_reconciler import enqueue_placeholder_art_backfill_all

        art_out = enqueue_placeholder_art_backfill_all(source=source, task_run_id=task_run_id)
        out["art_backfill"] = art_out
        art_committed = _refresh_enqueue_committed(art_out)
        out["enqueued"] = bool(out["enqueued"] or art_committed and bool(art_out.get("enqueued")))

    clear_metadata = bool(metadata and metadata_committed)
    clear_art = bool(art and art_committed)
    if clear_metadata or clear_art:
        try:
            if clear_metadata and clear_art:
                cleared = clear_pending_intent()
            else:
                cleared = clear_pending_intent_domains(
                    metadata=clear_metadata,
                    art=clear_art,
                    templates=clear_metadata,
                )
            out["pending"] = bool(cleared.get("metadata") or cleared.get("art") or cleared.get("templates"))
        except Exception as exc:
            logger.warning(f"Failed clearing pending intent after full sync consume: {exc}", extra={"emoji_type": "warning"})
    else:
        out["pending"] = has_pending_intent()

    if (metadata and not metadata_committed) or (art and not art_committed):
        out["ok"] = False
        out["reason"] = "enqueue_failed"
    return out
