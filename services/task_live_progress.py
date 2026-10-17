"""Generic live progress on ``scheduled_task_run.summary.live``.

One model for every long-running task (art refresh first; calendar / full /
collections sync later). Surfaces choose what to render; they do not fetch
separate payloads.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.logger import logger

LIVE_KEY = "live"
DONE_PRUNE_AFTER_S = 10.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    d = dt or _utc_now()
    if getattr(d, "tzinfo", None) is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat()


def _parse_iso(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


def _load_summary(task_run_id: int) -> dict[str, Any]:
    from services.postgres.db import get_session
    from services.postgres.models import ScheduledTaskRun

    session = get_session()
    try:
        row = session.query(ScheduledTaskRun).filter(ScheduledTaskRun.id == int(task_run_id)).first()
        if not row or not isinstance(row.summary, dict):
            return {}
        return dict(row.summary)
    finally:
        session.close()


def _task_run_trigger(task_run_id: int) -> str:
    from services.postgres.db import get_session
    from services.postgres.models import ScheduledTaskRun

    session = get_session()
    try:
        row = session.query(ScheduledTaskRun).filter(ScheduledTaskRun.id == int(task_run_id)).first()
        return str(getattr(row, "trigger", None) or "").strip() if row else ""
    finally:
        session.close()


def _normalize_unit(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    try:
        index = int(raw.get("index"))
    except Exception:
        return None
    try:
        done = int(raw.get("done") or 0)
    except Exception:
        done = 0
    try:
        total = int(raw.get("total") or 0)
    except Exception:
        total = 0
    status = str(raw.get("status") or "working").strip().lower()
    if status not in ("working", "done"):
        status = "working"
    out: dict[str, Any] = {
        "index": index,
        "done": max(0, done),
        "total": max(0, total),
        "status": status,
    }
    label = raw.get("label")
    if label is not None and str(label).strip():
        out["label"] = str(label).strip()
    finished_at = raw.get("finished_at")
    if finished_at:
        out["finished_at"] = str(finished_at)
    elif status == "done":
        out["finished_at"] = _iso()
    return out


def _prune_active_units(units: list[dict[str, Any]], *, now: datetime) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for unit in units:
        status = str(unit.get("status") or "").lower()
        if status == "working":
            kept.append(unit)
            continue
        finished = _parse_iso(unit.get("finished_at"))
        if finished is None:
            kept.append(unit)
            continue
        age = (now - finished).total_seconds()
        if age <= DONE_PRUNE_AFTER_S:
            kept.append(unit)
    kept.sort(key=lambda u: int(u.get("index") or 0))
    return kept


def _merge_active_units(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]] | None,
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    by_index: dict[int, dict[str, Any]] = {}
    for unit in existing:
        normalized = _normalize_unit(unit)
        if normalized:
            by_index[int(normalized["index"])] = normalized
    for unit in incoming or []:
        normalized = _normalize_unit(unit)
        if not normalized:
            continue
        idx = int(normalized["index"])
        prev = by_index.get(idx)
        if prev and str(prev.get("status")) == "done" and str(normalized.get("status")) == "working":
            # Do not reopen a finished unit from a stale heartbeat.
            continue
        if prev and str(normalized.get("status")) == "done" and not normalized.get("finished_at"):
            normalized["finished_at"] = prev.get("finished_at") or _iso(now)
        by_index[idx] = normalized
    return _prune_active_units(list(by_index.values()), now=now)


def _normalize_phase(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """One durable pipeline phase bar (never auto-pruned like ephemeral batch units)."""
    if not isinstance(raw, dict):
        return None
    key = str(raw.get("key") or "").strip().lower().replace(" ", "_")
    if not key:
        return None
    label = str(raw.get("label") or key.replace("_", " ").title()).strip() or key
    try:
        done = int(raw.get("done") or 0)
    except Exception:
        done = 0
    try:
        total = int(raw.get("total") or 0)
    except Exception:
        total = 0
    status = str(raw.get("status") or "working").strip().lower()
    if status not in ("working", "done", "pending", "skipped", "failed"):
        status = "working"
    if status == "skipped":
        # Intentionally not run: no meaningful counts.
        done = 0
        total = 0
    elif status == "pending":
        # Queued / not started yet: keep total when known (e.g. batches waiting on workers).
        done = 0
        total = max(0, total)
    elif status == "done" and total <= 0:
        done = 0
    elif total > 0:
        done = max(0, min(done, total))
    else:
        done = max(0, done)
    return {
        "key": key,
        "label": label,
        "done": done,
        "total": max(0, total),
        "status": status,
    }


def _merge_phases(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]] | None,
    *,
    replace: bool = False,
) -> list[dict[str, Any]]:
    """Merge phase bars by key. Finished phases stay visible for the whole run."""
    by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    if not replace:
        for raw in existing:
            phase = _normalize_phase(raw)
            if not phase:
                continue
            key = str(phase["key"])
            by_key[key] = phase
            order.append(key)
    for raw in incoming or []:
        phase = _normalize_phase(raw)
        if not phase:
            continue
        key = str(phase["key"])
        prev = by_key.get(key)
        prev_status = str(prev.get("status") or "") if prev else ""
        new_status = str(phase.get("status") or "")
        if prev and prev_status in ("done", "skipped", "failed") and new_status == "working":
            continue
        if prev and prev_status == "skipped" and new_status == "done":
            # Skipped means the phase never ran; do not rewrite as a successful finish.
            phase = dict(prev)
        elif prev and prev_status == "failed" and new_status in ("done", "skipped", "pending"):
            # Interrupted freeze wins over late finalize/skip publishes.
            phase = dict(prev)
        elif prev and new_status not in ("skipped", "pending"):
            # Allow status-only updates without wiping prior done/total (phase bars persist).
            incoming_has_total = isinstance(raw, dict) and raw.get("total") is not None
            incoming_has_done = isinstance(raw, dict) and raw.get("done") is not None
            if not incoming_has_total and int(prev.get("total") or 0) > 0:
                phase["total"] = int(prev.get("total") or 0)
            if not incoming_has_done and int(prev.get("total") or 0) > 0:
                if new_status == "done":
                    phase["done"] = int(phase.get("total") or prev.get("done") or 0)
                else:
                    phase["done"] = int(prev.get("done") or 0)
        by_key[key] = phase
        if key not in order:
            order.append(key)
    return [by_key[k] for k in order if k in by_key]


def freeze_task_live_on_interrupt(live: dict[str, Any] | None) -> dict[str, Any] | None:
    """Clear ephemeral batch bars and freeze phase counts after Stop / abandon.

    Working phases become ``failed`` with done/total left as-is so the UI keeps
    the last known progress instead of flipping batches to 100% or overall to 0%.
    """
    if not isinstance(live, dict) or not live:
        return live
    out = dict(live)
    out["frozen"] = True
    out["active_units"] = []
    try:
        out["seq"] = int(out.get("seq") or 0) + 1
    except Exception:
        out["seq"] = 1
    out["updated_at"] = _iso()

    phases: list[dict[str, Any]] = []
    for raw in out.get("phases") or []:
        if not isinstance(raw, dict):
            continue
        phase = dict(raw)
        if str(phase.get("status") or "").lower() == "working":
            phase["status"] = "failed"
        normalized = _normalize_phase(phase)
        if normalized:
            phases.append(normalized)
    out["phases"] = phases

    sections: list[dict[str, Any]] = []
    for raw in out.get("detail_sections") or []:
        if not isinstance(raw, dict):
            continue
        section = dict(raw)
        if str(section.get("status") or "").lower() == "working":
            section["status"] = "failed"
        sections.append(section)
    out["detail_sections"] = sections
    return out


def publish_task_live_progress(
    task_run_id: int,
    *,
    kind: str,
    label: str,
    units_done: int,
    units_total: int,
    unit_label: str = "batches",
    stage: str | None = None,
    stage_label: str | None = None,
    phases: list[dict[str, Any]] | None = None,
    active_units: list[dict[str, Any]] | None = None,
    detail_sections: list[dict[str, Any]] | None = None,
    replace_active_units: bool = False,
    replace_phases: bool = False,
) -> dict[str, Any] | None:
    """Merge-safe write of ``summary.live``. Bumps ``seq`` on every publish.

    ``phases`` are durable per-pipeline-step bars (art: Determining + Updating;
    full sync later: one bar per sync phase). Unlike ``active_units`` (ephemeral
    batches), completed phases are never pruned from the live payload.
    """
    from services.task_run_history import update_task_run_summary

    try:
        run_id = int(task_run_id)
    except Exception:
        return None
    if run_id <= 0:
        return None

    now = _utc_now()
    summary = _load_summary(run_id)
    prev = summary.get(LIVE_KEY) if isinstance(summary.get(LIVE_KEY), dict) else {}
    # After Stop / abandon: keep the frozen snapshot; ignore late batch/finalize publishes.
    if summary.get("interrupted") or (isinstance(prev, dict) and prev.get("frozen")):
        return prev if isinstance(prev, dict) else None
    try:
        seq = int(prev.get("seq") or 0) + 1
    except Exception:
        seq = 1

    prev_units = list(prev.get("active_units") or []) if isinstance(prev.get("active_units"), list) else []
    if replace_active_units:
        merged_units = _prune_active_units(
            [u for u in (_normalize_unit(x) for x in (active_units or [])) if u],
            now=now,
        )
    else:
        merged_units = _merge_active_units(prev_units, active_units, now=now)

    prev_phases = list(prev.get("phases") or []) if isinstance(prev.get("phases"), list) else []
    if phases is None and not replace_phases:
        merged_phases = [_normalize_phase(p) for p in prev_phases]
        merged_phases = [p for p in merged_phases if p]
    else:
        merged_phases = _merge_phases(prev_phases, phases, replace=replace_phases)

    total = max(0, int(units_total or 0))
    done = max(0, int(units_done or 0))
    if total > 0:
        done = min(done, total)
        overall_pct = int(round((100.0 * done) / total))
    else:
        # Nothing queued (e.g. determination found no art mismatches): treat as complete.
        overall_pct = 100
        done = 0

    sections = detail_sections
    if sections is None and isinstance(prev.get("detail_sections"), list):
        sections = list(prev.get("detail_sections") or [])

    stage_key = str(stage or prev.get("stage") or "working").strip().lower() or "working"
    stage_text = str(stage_label or prev.get("stage_label") or "").strip()
    if not stage_text:
        if stage_key == "determining":
            stage_text = "Determining"
        elif stage_key == "queued":
            stage_text = "Queued"
        elif stage_key in ("refreshing", "updating"):
            stage_text = "Updating"
        else:
            stage_text = stage_key.replace("_", " ").title()

    live: dict[str, Any] = {
        "seq": seq,
        "updated_at": _iso(now),
        "kind": str(kind or "").strip().lower() or "task",
        "label": str(label or "Task").strip() or "Task",
        "stage": stage_key,
        "stage_label": stage_text,
        "overall_pct": max(0, min(100, overall_pct)),
        "units_done": done,
        "units_total": total,
        "unit_label": str(unit_label or "batches").strip() or "batches",
        "phases": merged_phases,
        "active_units": merged_units,
        "detail_sections": list(sections or []),
    }
    try:
        update_task_run_summary(run_id, {LIVE_KEY: live})
    except Exception as exc:
        logger.debug("publish_task_live_progress failed run_id=%s: %s", run_id, exc, extra={"emoji_type": "debug"})
        return None
    return live


def clear_task_live_progress(task_run_id: int) -> None:
    """Remove live progress (e.g. when the run finishes)."""
    from services.task_run_history import update_task_run_summary

    try:
        update_task_run_summary(int(task_run_id), {LIVE_KEY: None})
    except Exception as exc:
        logger.debug("clear_task_live_progress failed run_id=%s: %s", task_run_id, exc, extra={"emoji_type": "debug"})


def working_live_progress_seq_fingerprint() -> str:
    """Token of working-run live seq values for SSE change detection."""
    from services.postgres.db import get_session
    from services.postgres.models import ScheduledTaskRun

    session = get_session()
    try:
        rows = (
            session.query(ScheduledTaskRun.id, ScheduledTaskRun.summary)
            .filter(ScheduledTaskRun.status == "working")
            .order_by(ScheduledTaskRun.id.asc())
            .all()
        )
    finally:
        session.close()
    parts: list[str] = []
    for row in rows:
        summary = row.summary if isinstance(row.summary, dict) else {}
        live = summary.get(LIVE_KEY) if isinstance(summary.get(LIVE_KEY), dict) else {}
        try:
            seq = int(live.get("seq") or 0)
        except Exception:
            seq = 0
        parts.append(f"{int(row.id)}:{seq}")
    return ",".join(parts) if parts else "none"


def _art_items_progress(
    art: dict[str, Any],
    *,
    active_batch: dict[str, Any] | None,
    prev_live: dict[str, Any] | None,
) -> tuple[int, int]:
    """Content progress: finished-batch items + in-flight working batch items."""
    items_total = int(art.get("items_total") or 0)
    items_locked = int(art.get("items_done") or 0)
    if items_total <= 0:
        # Legacy / mid-upgrade runs: fall back to batch counts for headline %.
        batches_total = int(art.get("batch_count") or 0)
        batches_done = int(art.get("batches_done") or 0)
        return batches_done, batches_total

    by_index: dict[int, dict[str, Any]] = {}
    prev_units = prev_live.get("active_units") if isinstance(prev_live, dict) else None
    if isinstance(prev_units, list):
        for raw in prev_units:
            unit = _normalize_unit(raw if isinstance(raw, dict) else None)
            if unit:
                by_index[int(unit["index"])] = unit
    if active_batch is not None:
        unit = _normalize_unit(active_batch)
        if unit:
            by_index[int(unit["index"])] = unit

    in_flight = 0
    for unit in by_index.values():
        if str(unit.get("status") or "").lower() != "working":
            continue
        try:
            in_flight += max(0, int(unit.get("done") or 0))
        except Exception:
            continue

    items_done = min(items_total, max(0, items_locked + in_flight))
    return items_done, items_total


def _art_phase_rows(
    *,
    determining_status: str,
    determining_done: int = 0,
    determining_total: int = 0,
    updating_status: str = "pending",
    updating_done: int = 0,
    updating_total: int = 0,
) -> list[dict[str, Any]]:
    """Always publish both art phase bars so the run shape is stable from the start."""
    return [
        {
            "key": "determining",
            "label": "Determining",
            "done": int(determining_done or 0),
            "total": int(determining_total or 0),
            "status": determining_status,
        },
        {
            "key": "updating",
            "label": "Updating",
            "done": int(updating_done or 0),
            "total": int(updating_total or 0),
            "status": updating_status,
        },
    ]


def _prev_phase(prev_live: dict[str, Any] | None, key: str) -> dict[str, Any] | None:
    prev = prev_live if isinstance(prev_live, dict) else {}
    for raw in prev.get("phases") or []:
        if isinstance(raw, dict) and str(raw.get("key") or "").lower() == key:
            return raw
    return None


def _preserve_determination_section(
    prev_live: dict[str, Any] | None,
    sections: list[dict[str, Any]],
    *,
    status: str = "done",
) -> list[dict[str, Any]]:
    """Keep the Determining phase card when live switches to Updating."""
    prev = prev_live if isinstance(prev_live, dict) else {}
    prev_sections = prev.get("detail_sections") if isinstance(prev.get("detail_sections"), list) else []
    det: dict[str, Any] | None = None
    for raw in prev_sections:
        if isinstance(raw, dict) and str(raw.get("name") or "") == "Determination":
            det = dict(raw)
            break
    if status == "skipped":
        trigger = "-"
        if det:
            for metric in det.get("metrics") or []:
                if isinstance(metric, dict) and str(metric.get("label") or "") == "Trigger":
                    trigger = metric.get("value") or "-"
                    break
        det = {
            "name": "Determination",
            "status": "skipped",
            "metrics": [
                {"label": "Trigger", "value": trigger},
                {"label": "Scanned", "value": "— / —"},
                {"label": "Needs refresh", "value": "—"},
                {"label": "Reason", "value": "Skipped (settings apply)"},
            ],
        }
    elif not det:
        return sections
    else:
        det["status"] = status
    out = [det]
    for section in sections:
        if str(section.get("name") or "") == "Determination":
            continue
        out.append(section)
    return out


def seed_art_refresh_live_phases(
    task_run_id: int,
    *,
    skip_determination: bool = False,
) -> dict[str, Any] | None:
    """Show both art phase bars immediately (pending, or Determining skipped)."""
    trigger = _task_run_trigger(int(task_run_id)) or "-"
    trigger_label = trigger.replace("_", " ").title() if trigger != "-" else "-"
    if skip_determination:
        phases = _art_phase_rows(determining_status="skipped", updating_status="pending")
        detail_sections: list[dict[str, Any]] = [
            {
                "name": "Determination",
                "status": "skipped",
                "metrics": [
                    {"label": "Trigger", "value": trigger_label},
                    {"label": "Scanned", "value": "— / —"},
                    {"label": "Needs refresh", "value": "—"},
                    {"label": "Reason", "value": "Skipped (settings apply)"},
                ],
            },
        ]
        # Batches may exist but workers can still be busy (e.g. startup sync).
        stage = "queued"
        stage_label = "Queued"
    else:
        phases = _art_phase_rows(determining_status="pending", updating_status="pending")
        detail_sections = [
            {
                "name": "Determination",
                "status": "pending",
                "metrics": [
                    {"label": "Trigger", "value": trigger_label},
                    {"label": "Scanned", "value": "— / —"},
                    {"label": "Needs refresh", "value": "—"},
                ],
            },
        ]
        stage = "determining"
        stage_label = "Determining"
    return publish_task_live_progress(
        int(task_run_id),
        kind="art_refresh",
        label="Art refresh",
        stage=stage,
        stage_label=stage_label,
        units_done=0,
        units_total=0,
        unit_label="placeholders",
        phases=phases,
        active_units=[],
        detail_sections=detail_sections,
        replace_active_units=True,
        replace_phases=True,
    )


def publish_art_determination_live(
    task_run_id: int,
    *,
    scanned: int,
    total: int,
    needed: int = 0,
) -> dict[str, Any] | None:
    """Publish pre-enqueue art determination progress (same live model, stage=determining)."""
    trigger = _task_run_trigger(int(task_run_id)) or "-"
    total_n = max(0, int(total or 0))
    scanned_n = max(0, min(int(scanned or 0), total_n if total_n else int(scanned or 0)))
    needed_n = max(0, int(needed or 0))
    det_done = total_n > 0 and scanned_n >= total_n
    detail_sections: list[dict[str, Any]] = [
        {
            "name": "Determination",
            "status": "done" if det_done else "working",
            "metrics": [
                {"label": "Trigger", "value": trigger.replace("_", " ").title() if trigger != "-" else "-"},
                {"label": "Scanned", "value": f"{scanned_n} / {total_n}"},
                {"label": "Needs refresh", "value": needed_n},
            ],
        },
    ]
    return publish_task_live_progress(
        int(task_run_id),
        kind="art_refresh",
        label="Art refresh",
        stage="determining",
        stage_label="Determining",
        units_done=scanned_n,
        units_total=total_n,
        unit_label="placeholders",
        phases=_art_phase_rows(
            determining_status="done" if det_done else "working",
            determining_done=scanned_n,
            determining_total=total_n,
            updating_status="pending",
        ),
        active_units=[],
        detail_sections=detail_sections,
        replace_active_units=True,
    )


def publish_art_refresh_live(
    task_run_id: int,
    *,
    active_batch: dict[str, Any] | None = None,
    replace_active_units: bool = False,
) -> dict[str, Any] | None:
    """Art-refresh adapter: derive live fields from ``summary.art_backfill`` + phases."""
    summary = _load_summary(int(task_run_id))
    art = summary.get("art_backfill") if isinstance(summary.get("art_backfill"), dict) else {}
    counts = art.get("counts") if isinstance(art.get("counts"), dict) else {}
    batches_done = int(art.get("batches_done") or 0)
    batches_total = int(art.get("batch_count") or 0)
    trigger = _task_run_trigger(int(task_run_id)) or str(summary.get("trigger") or "").strip() or "-"
    source = str(art.get("source") or summary.get("art_backfill_source") or "-")
    prev_live = summary.get(LIVE_KEY) if isinstance(summary.get(LIVE_KEY), dict) else {}
    items_done, items_total = _art_items_progress(art, active_batch=active_batch, prev_live=prev_live)
    art_working = str(art.get("status") or "").lower() == "working"
    prev_det = _prev_phase(prev_live, "determining")
    prev_det_status = str((prev_det or {}).get("status") or "").lower()
    determination_skipped = bool(art.get("determination_skipped")) or prev_det_status == "skipped"
    if determination_skipped:
        determining_status = "skipped"
    elif prev_det_status == "done" or (prev_det_status == "working" and int((prev_det or {}).get("total") or 0) > 0):
        determining_status = "done"
    else:
        # Updating started without a finished scan (e.g. settings apply).
        determining_status = "skipped"

    empty_queue = int(items_total or 0) <= 0 and int(batches_total or 0) <= 0 and active_batch is None
    prev_units = prev_live.get("active_units") if isinstance(prev_live.get("active_units"), list) else []
    had_working_unit = any(
        isinstance(u, dict) and str(u.get("status") or "").lower() == "working" for u in prev_units
    )
    # art_backfill.status becomes "working" as soon as batches are enqueued; workers may still
    # be blocked (startup sync). Stay pending/queued until a batch heartbeat or progress exists.
    update_started = bool(
        active_batch is not None
        or int(items_done or 0) > 0
        or int(batches_done or 0) > 0
        or had_working_unit
    )
    if empty_queue and determining_status == "done":
        updating_status = "skipped"
    elif empty_queue and determining_status == "skipped":
        updating_status = "skipped"
    elif art_working and not empty_queue and update_started:
        updating_status = "working"
    elif art_working and not empty_queue and not update_started:
        updating_status = "pending"
    else:
        updating_status = "done"

    from services.task_run_phases import ART_COUNT_KEYS

    created_total = sum(int(counts.get(k) or 0) for k in ART_COUNT_KEYS)
    trigger_label = trigger.replace("_", " ").title() if trigger != "-" else "-"
    source_label = source.replace("_", " ") if source != "-" else "-"
    if updating_status == "skipped":
        run_section: dict[str, Any] = {
            "name": "Run",
            "status": "skipped",
            "metrics": [
                {"label": "Trigger", "value": trigger_label},
                {"label": "Source", "value": source_label},
                {"label": "Placeholders", "value": "— / —"},
                {"label": "Batches", "value": "— / —"},
                {"label": "Reason", "value": "Nothing needed a refresh"},
            ],
        }
        outcomes_section: dict[str, Any] = {
            "name": "Outcomes",
            "status": "skipped",
            "metrics": [
                {"label": "Movie posters", "value": "—"},
                {"label": "Series posters", "value": "—"},
                {"label": "Season posters", "value": "—"},
                {"label": "Episode stills", "value": "—"},
                {"label": "Created total", "value": "—"},
            ],
        }
    elif updating_status == "pending":
        run_section = {
            "name": "Run",
            "status": "pending",
            "metrics": [
                {"label": "Trigger", "value": trigger_label},
                {"label": "Source", "value": source_label},
                {"label": "Placeholders", "value": f"0 / {items_total}"},
                {"label": "Batches", "value": f"0 / {batches_total}"},
                {"label": "Reason", "value": "Queued; waiting for a worker"},
            ],
        }
        outcomes_section = {
            "name": "Outcomes",
            "status": "pending",
            "metrics": [
                {"label": "Movie posters", "value": 0},
                {"label": "Series posters", "value": 0},
                {"label": "Season posters", "value": 0},
                {"label": "Episode stills", "value": 0},
                {"label": "Created total", "value": 0},
            ],
        }
    else:
        run_section = {
            "name": "Run",
            "status": updating_status,
            "metrics": [
                {"label": "Trigger", "value": trigger_label},
                {"label": "Source", "value": source_label},
                {"label": "Placeholders", "value": f"{items_done} / {items_total}"},
                {"label": "Batches", "value": f"{batches_done} / {batches_total}"},
            ],
        }
        outcomes_section = {
            "name": "Outcomes",
            "status": updating_status,
            "metrics": [
                {"label": "Movie posters", "value": int(counts.get("movie") or 0)},
                {"label": "Series posters", "value": int(counts.get("series") or 0)},
                {"label": "Season posters", "value": int(counts.get("season") or 0)},
                {"label": "Episode stills", "value": int(counts.get("episode") or 0)},
                {"label": "Created total", "value": created_total},
            ],
        }

    detail_sections: list[dict[str, Any]] = _preserve_determination_section(
        prev_live,
        [run_section, outcomes_section],
        status=determining_status if determining_status in ("done", "skipped") else "done",
    )

    # Ephemeral batch bars: show while Updating is working; clear once the phase finishes
    # (prune-after-N-seconds only runs on subsequent publishes, which stop when the run ends).
    if updating_status in ("done", "skipped", "pending") or not art_working:
        units: list[dict[str, Any]] | None = []
        replace_active_units = True
    elif active_batch is not None:
        units = [active_batch]
    else:
        units = None

    det_done = int((prev_det or {}).get("done") or 0)
    det_total = int((prev_det or {}).get("total") or 0)
    phase_updates = _art_phase_rows(
        determining_status=determining_status,
        determining_done=det_done,
        determining_total=det_total,
        updating_status=updating_status,
        updating_done=0 if updating_status in ("skipped", "pending") else items_done,
        updating_total=(
            0
            if updating_status == "skipped"
            else items_total
        ),
    )

    # Headline follows the phase that still matters for compact surfaces.
    if updating_status == "skipped" and determining_status == "done":
        headline_done, headline_total = det_done, det_total
        stage, stage_label = "determining", "Determining"
    elif updating_status == "skipped":
        headline_done, headline_total = 0, 0
        stage, stage_label = "updating", "Updating"
    elif updating_status == "pending":
        headline_done, headline_total = 0, items_total
        stage, stage_label = "queued", "Queued"
    else:
        headline_done, headline_total = items_done, items_total
        stage, stage_label = "updating", "Updating"

    return publish_task_live_progress(
        int(task_run_id),
        kind="art_refresh",
        label="Art refresh",
        stage=stage,
        stage_label=stage_label,
        units_done=headline_done,
        units_total=headline_total,
        unit_label="placeholders",
        phases=phase_updates,
        active_units=units,
        detail_sections=detail_sections,
        replace_active_units=replace_active_units,
    )
