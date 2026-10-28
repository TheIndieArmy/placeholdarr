"""Generic live progress on ``scheduled_task_run.summary.live``.

One model for every long-running task (art refresh, full/lite/calendar sync;
collections stay off live). Surfaces choose what to render; they do not fetch
separate payloads.
"""

from __future__ import annotations

import re
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


def _normalize_metrics(raw: Any) -> list[dict[str, Any]]:
    """Count cards under an expandable phase bar."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        out.append({"label": label, "value": item.get("value")})
    return out


def _counts_from_phase_metrics(
    phase_key: str,
    metrics: list[dict[str, Any]] | None,
) -> tuple[int | None, int | None]:
    """Best-effort done/total for a phase bar from finished count cards."""
    if not metrics:
        return None, None
    by_label: dict[str, Any] = {}
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        label = str(metric.get("label") or "").strip().lower()
        if label:
            by_label[label] = metric.get("value")

    def _as_int(value: Any) -> int | None:
        try:
            return int(value)
        except Exception:
            return None

    key = str(phase_key or "").strip().lower()
    if key in ("fs_scan", "filesystem_scan"):
        files = _as_int(by_label.get("files on disk (walked)"))
        if files is not None:
            return files, files
        indexed = _as_int(by_label.get("new paths indexed"))
        if indexed is not None:
            return indexed, indexed
    if key in ("determination", "status_determination"):
        movies = _as_int(by_label.get("movies evaluated")) or 0
        episodes = _as_int(by_label.get("episodes evaluated")) or 0
        total = movies + episodes
        if total > 0:
            return total, total
        return 0, 0
    if key in ("materialization", "placeholder_materialization"):
        created = _as_int(by_label.get("placeholders created")) or 0
        removed = _as_int(by_label.get("placeholders removed")) or 0
        noop = _as_int(by_label.get("already up to date")) or 0
        errors = _as_int(by_label.get("errors")) or 0
        total = created + removed + noop + errors
        return total, total
    if key in ("calendar", "calendar_status"):
        scanned = _as_int(by_label.get("placeholders scanned"))
        if scanned is not None:
            return scanned, scanned
        updates = _as_int(by_label.get("calendar updates"))
        if updates is not None:
            return updates, updates
    if key in ("calendar_dates",):
        movies = _as_int(by_label.get("movies checked")) or 0
        episodes = _as_int(by_label.get("episodes checked")) or 0
        total = movies + episodes
        if total > 0 or "movies checked" in by_label or "episodes checked" in by_label:
            return total, total
        updated = (_as_int(by_label.get("movie dates updated")) or 0) + (
            _as_int(by_label.get("episode dates updated")) or 0
        )
        return updated, updated
    if key in ("catalog_gc", "catalog_cleanup"):
        movies = _as_int(by_label.get("movies purged")) or 0
        series = _as_int(by_label.get("series purged")) or 0
        seasons = _as_int(by_label.get("seasons purged")) or 0
        episodes = _as_int(by_label.get("episodes purged")) or 0
        total = movies + series + seasons + episodes
        return total, total
    if key in ("arr_sync", "arr_catalog_sync"):
        total_items = 0
        found = False
        for label, value in by_label.items():
            if label == "total arr sync time":
                continue
            text = str(value or "")
            for match in re.finditer(r"(\d+)\s+movies", text, re.I):
                total_items += int(match.group(1))
                found = True
            for match in re.finditer(r"(\d+)\s+series", text, re.I):
                total_items += int(match.group(1))
                found = True
        if found:
            return total_items, total_items
    return None, None


def _normalize_phase_unit(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Nested bar under a phase (e.g. one ARR instance inside ARR catalog sync)."""
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
        done = 0
        total = 0
    elif status == "pending":
        done = 0
        total = max(0, total)
    elif status == "done" and total <= 0 and done > 0:
        total = done
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


def _normalize_phase_units(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        unit = _normalize_phase_unit(item if isinstance(item, dict) else None)
        if unit:
            out.append(unit)
    return out


def _merge_phase_units(
    existing: list[dict[str, Any]] | None,
    incoming: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Merge nested phase units by key; preserve finished instance bars."""
    by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for raw in existing or []:
        unit = _normalize_phase_unit(raw if isinstance(raw, dict) else None)
        if not unit:
            continue
        key = str(unit["key"])
        by_key[key] = unit
        order.append(key)
    for raw in incoming or []:
        if not isinstance(raw, dict):
            continue
        unit = _normalize_phase_unit(raw)
        if not unit:
            continue
        key = str(unit["key"])
        prev = by_key.get(key)
        if prev and str(prev.get("status") or "") in ("done", "skipped", "failed") and str(unit.get("status")) == "working":
            continue
        if prev and "total" not in raw and int(prev.get("total") or 0) > 0 and int(unit.get("total") or 0) <= 0:
            unit["total"] = int(prev.get("total") or 0)
            if str(unit.get("status")) == "done":
                unit["done"] = unit["total"]
        by_key[key] = unit
        if key not in order:
            order.append(key)
    return [by_key[k] for k in order if k in by_key]


def _rollup_phase_units(units: list[dict[str, Any]] | None) -> tuple[int, int]:
    """Sum nested unit done/total for the parent phase bar."""
    done = 0
    total = 0
    for unit in units or []:
        try:
            done += max(0, int(unit.get("done") or 0))
        except Exception:
            pass
        try:
            total += max(0, int(unit.get("total") or 0))
        except Exception:
            pass
    return done, total


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
    elif status == "done" and total <= 0 and done > 0:
        # Finished with a known count but no denominator (e.g. calendar 12 updates).
        total = done
    elif status == "done" and total <= 0:
        done = 0
    elif total > 0:
        done = max(0, min(done, total))
    else:
        done = max(0, done)
    out: dict[str, Any] = {
        "key": key,
        "label": label,
        "done": done,
        "total": max(0, total),
        "status": status,
    }
    # Preserve metrics / units only when the payload includes them (merge copies from prev otherwise).
    if "metrics" in raw:
        out["metrics"] = _normalize_metrics(raw.get("metrics"))
    if "units" in raw:
        out["units"] = _normalize_phase_units(raw.get("units"))
    return out


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
        # Keep prior count cards / nested bars unless this update replaces them.
        if prev and "metrics" not in phase and prev.get("metrics"):
            phase["metrics"] = list(prev.get("metrics") or [])
        if prev and "units" not in phase and prev.get("units"):
            phase["units"] = list(prev.get("units") or [])
        elif prev and "units" in phase and prev.get("units"):
            phase["units"] = _merge_phase_units(list(prev.get("units") or []), list(phase.get("units") or []))
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


def _build_live_payload(
    summary: dict[str, Any],
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
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Build next ``summary.live`` from a locked summary snapshot. Returns None if frozen."""
    prev = summary.get(LIVE_KEY) if isinstance(summary.get(LIVE_KEY), dict) else {}
    if summary.get("interrupted") or (isinstance(prev, dict) and prev.get("frozen")):
        return None
    stamp = now or _utc_now()
    try:
        seq = int(prev.get("seq") or 0) + 1
    except Exception:
        seq = 1

    prev_units = list(prev.get("active_units") or []) if isinstance(prev.get("active_units"), list) else []
    if replace_active_units:
        merged_units = _prune_active_units(
            [u for u in (_normalize_unit(x) for x in (active_units or [])) if u],
            now=stamp,
        )
    else:
        merged_units = _merge_active_units(prev_units, active_units, now=stamp)
    # Completion publishes must land at full total even if the last heartbeat was N-1.
    for unit in merged_units:
        if str(unit.get("status") or "").lower() != "done":
            continue
        try:
            total_u = int(unit.get("total") or 0)
            done_u = int(unit.get("done") or 0)
        except Exception:
            continue
        if total_u > 0 and done_u < total_u:
            unit["done"] = total_u

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
        # Match phase honesty: do not report 100% while a batch bar is still working.
        if any(str(u.get("status") or "").lower() == "working" for u in merged_units) and done >= total:
            done = total - 1
        overall_pct = int(round((100.0 * done) / total))
        if done < total:
            overall_pct = min(overall_pct, 99)
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

    return {
        "seq": seq,
        "updated_at": _iso(stamp),
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
    """Merge-safe write of ``summary.live`` under a row lock. Bumps ``seq`` on every publish.

    ``phases`` are durable per-pipeline-step bars (art: Determining + Updating;
    full sync later: one bar per sync phase). Unlike ``active_units`` (ephemeral
    batches), completed phases are never pruned from the live payload.
    """
    from services.task_run_history import mutate_task_run_summary

    try:
        run_id = int(task_run_id)
    except Exception:
        return None
    if run_id <= 0:
        return None

    now = _utc_now()
    built: dict[str, Any] | None = None

    def _mutator(summary: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal built
        live = _build_live_payload(
            summary,
            kind=kind,
            label=label,
            units_done=units_done,
            units_total=units_total,
            unit_label=unit_label,
            stage=stage,
            stage_label=stage_label,
            phases=phases,
            active_units=active_units,
            detail_sections=detail_sections,
            replace_active_units=replace_active_units,
            replace_phases=replace_phases,
            now=now,
        )
        if live is None:
            prev = summary.get(LIVE_KEY) if isinstance(summary.get(LIVE_KEY), dict) else None
            built = prev if isinstance(prev, dict) else None
            return None
        summary[LIVE_KEY] = live
        built = live
        return summary

    try:
        mutate_task_run_summary(run_id, _mutator)
    except Exception as exc:
        logger.debug("publish_task_live_progress failed run_id=%s: %s", run_id, exc, extra={"emoji_type": "debug"})
        return None
    return built


def clear_task_live_progress(task_run_id: int) -> None:
    """Remove live progress (e.g. when the run finishes)."""
    from services.task_run_history import update_task_run_summary

    try:
        update_task_run_summary(int(task_run_id), {LIVE_KEY: None})
    except Exception as exc:
        logger.debug("clear_task_live_progress failed run_id=%s: %s", task_run_id, exc, extra={"emoji_type": "debug"})


# --- Sync / calendar live (coarse phase bars) ---------------------------------

FULL_SYNC_CORE_PHASES: list[tuple[str, str]] = [
    ("arr_sync", "ARR catalog sync"),
    ("fs_scan", "Filesystem scan"),
    ("determination", "Status determination"),
    ("materialization", "Placeholder materialization"),
    ("catalog_gc", "Catalog cleanup"),
    ("calendar", "Calendar status"),
]

# Boot full path (no catalog_gc batch; calendar dates fold into the pipeline before determination).
STARTUP_FULL_SYNC_CORE_PHASES: list[tuple[str, str]] = [
    ("arr_sync", "ARR catalog sync"),
    ("fs_scan", "Filesystem scan"),
    ("determination", "Status determination"),
    ("materialization", "Placeholder materialization"),
    ("calendar", "Calendar status"),
]

LITE_SYNC_CORE_PHASES: list[tuple[str, str]] = [
    ("discovery", "Catalog discovery"),
    ("determination", "Status determination"),
    ("materialization", "Placeholder materialization"),
]

CALENDAR_ONLY_PHASES: list[tuple[str, str]] = [
    ("calendar_dates", "Calendar dates"),
    ("calendar", "Calendar status"),
]

def _task_run_key_for_live(task_run_id: int) -> str:
    from services.postgres.db import get_session
    from services.postgres.models import ScheduledTaskRun

    session = get_session()
    try:
        row = session.query(ScheduledTaskRun).filter(ScheduledTaskRun.id == int(task_run_id)).first()
        return str(row.task_key or "").strip().lower() if row else ""
    finally:
        session.close()


def _sync_kind_label(kind: str) -> tuple[str, str]:
    key = str(kind or "").strip().lower()
    if key == "full_sync":
        return "full_sync", "Full sync"
    if key == "lite_sync":
        return "lite_sync", "Lite sync"
    if key in {"calendar_only", "calendar_sync"}:
        return "calendar_sync", "Calendar sync"
    return key or "task", (key or "task").replace("_", " ").title()


def _pending_phase_rows(phase_defs: list[tuple[str, str]]) -> list[dict[str, Any]]:
    from services.task_run_phases import empty_live_metrics_for_phase

    rows: list[dict[str, Any]] = []
    for key, label in phase_defs:
        rows.append(
            {
                "key": key,
                "label": label,
                "status": "pending",
                "done": 0,
                "total": 0,
                "metrics": empty_live_metrics_for_phase(key),
            }
        )
    return rows


def _coarse_phase_counts(phases: list[dict[str, Any]]) -> tuple[int, int]:
    total = len(phases)
    done = 0
    for phase in phases:
        status = str(phase.get("status") or "").lower()
        if status in ("done", "skipped", "failed"):
            done += 1
    return done, total


def _stage_from_phases(phases: list[dict[str, Any]]) -> tuple[str, str]:
    working = next((p for p in phases if str(p.get("status") or "").lower() == "working"), None)
    if working:
        key = str(working.get("key") or "working")
        label = str(working.get("label") or key.replace("_", " ").title())
        return key, label
    pending = next((p for p in phases if str(p.get("status") or "").lower() == "pending"), None)
    if pending:
        key = str(pending.get("key") or "queued")
        label = str(pending.get("label") or key.replace("_", " ").title())
        return key, label
    if phases:
        last = phases[-1]
        key = str(last.get("key") or "done")
        label = str(last.get("label") or "Done")
        return key, label
    return "working", "Working"


def seed_sync_live_phases(
    task_run_id: int,
    *,
    kind: str,
    label: str | None = None,
    phase_defs: list[tuple[str, str]],
) -> dict[str, Any] | None:
    """Seed all expected sync/calendar phase bars as pending at run start."""
    kind_key, default_label = _sync_kind_label(kind)
    rows = _pending_phase_rows(phase_defs)
    if not rows:
        return None
    first = rows[0]
    stage = str(first.get("key") or "working")
    stage_label = str(first.get("label") or stage.replace("_", " ").title())
    units_done, units_total = _coarse_phase_counts(rows)
    return publish_task_live_progress(
        int(task_run_id),
        kind=kind_key,
        label=str(label or default_label).strip() or default_label,
        stage=stage,
        stage_label=stage_label,
        units_done=units_done,
        units_total=units_total,
        unit_label="phases",
        phases=rows,
        active_units=[],
        detail_sections=[],
        replace_active_units=True,
        replace_phases=True,
    )


def publish_sync_phase_live(
    task_run_id: int,
    *,
    kind: str,
    phase_key: str,
    phase_label: str | None = None,
    status: str,
    label: str | None = None,
    detail_metrics: list[dict[str, Any]] | None = None,
    done: int | None = None,
    total: int | None = None,
    unit_label: str | None = None,
    phase_units: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Advance one sync/calendar phase bar (pending/working/done/skipped/failed).

    Prefer real ``done``/``total`` (ARR items, files walked, DB rows). When omitted,
    falls back to coarse 0/1 only for phases with no countable work.

    ``phase_units`` nests durable bars under this phase (e.g. one per ARR instance).
    """
    kind_key, default_label = _sync_kind_label(kind)
    key = str(phase_key or "").strip().lower().replace(" ", "_")
    if not key:
        return None
    status_key = str(status or "working").strip().lower()
    if status_key not in ("working", "done", "pending", "skipped", "failed"):
        status_key = "working"
    display = str(phase_label or key.replace("_", " ").title()).strip() or key

    prev = _load_summary(int(task_run_id)).get(LIVE_KEY)
    prev_phases: list[dict[str, Any]] = []
    if isinstance(prev, dict) and isinstance(prev.get("phases"), list):
        prev_phases = [p for p in (_normalize_phase(x) for x in prev["phases"]) if p]

    by_key = {str(p["key"]): dict(p) for p in prev_phases}
    order = [str(p["key"]) for p in prev_phases]
    if key not in by_key:
        order.append(key)
    prev_phase = by_key.get(key) or {}
    try:
        prev_done = int(prev_phase.get("done") or 0)
    except Exception:
        prev_done = 0
    try:
        prev_total = int(prev_phase.get("total") or 0)
    except Exception:
        prev_total = 0

    merged_units: list[dict[str, Any]] | None = None
    if phase_units is not None:
        merged_units = _merge_phase_units(
            list(prev_phase.get("units") or []) if isinstance(prev_phase.get("units"), list) else [],
            list(phase_units),
        )
    elif isinstance(prev_phase.get("units"), list) and prev_phase.get("units"):
        merged_units = _normalize_phase_units(prev_phase.get("units"))

    derived_done: int | None = None
    derived_total: int | None = None
    if detail_metrics is not None and done is None and total is None:
        derived_done, derived_total = _counts_from_phase_metrics(key, detail_metrics)
    if merged_units and done is None and total is None and derived_done is None:
        derived_done, derived_total = _rollup_phase_units(merged_units)

    if status_key == "skipped":
        phase_done, phase_total = 0, 0
    elif done is not None or total is not None:
        try:
            phase_done = int(done) if done is not None else prev_done
        except Exception:
            phase_done = prev_done
        try:
            phase_total = int(total) if total is not None else prev_total
        except Exception:
            phase_total = prev_total
        phase_done = max(0, phase_done)
        phase_total = max(0, phase_total)
        if status_key == "done" and phase_total <= 0 and phase_done > 0:
            phase_total = phase_done
        if status_key == "done" and phase_total > 0:
            phase_done = phase_total
    elif derived_done is not None or derived_total is not None:
        phase_done = max(0, int(derived_done if derived_done is not None else 0))
        phase_total = max(0, int(derived_total if derived_total is not None else phase_done))
        if status_key == "done" and phase_total > 0:
            phase_done = phase_total
    elif status_key == "pending":
        phase_done, phase_total = 0, prev_total
    elif status_key in ("working", "failed"):
        # Keep prior totals when flipping to working without new counts (begin).
        phase_done, phase_total = prev_done, prev_total
    else:
        # done without explicit counts: preserve prior if any, else coarse 1/1
        if prev_total > 0:
            phase_done, phase_total = prev_total, prev_total
        else:
            phase_done, phase_total = 1, 1

    phase_row: dict[str, Any] = {
        "key": key,
        "label": display,
        "status": status_key,
        "done": phase_done,
        "total": phase_total,
    }
    if detail_metrics is not None:
        phase_row["metrics"] = list(detail_metrics)
    elif prev_phase.get("metrics"):
        phase_row["metrics"] = list(prev_phase.get("metrics") or [])
    if merged_units is not None:
        phase_row["units"] = merged_units
    by_key[key] = phase_row

    merged = [by_key[k] for k in order if k in by_key]
    stage, stage_label = _stage_from_phases(merged)
    if status_key == "working":
        stage, stage_label = key, display

    # Pill headline: prefer the active phase's real units when known.
    if status_key == "working" and phase_total > 0:
        units_done, units_total = phase_done, phase_total
        pill_units = str(unit_label or "items").strip() or "items"
    elif status_key == "working" and phase_done > 0 and phase_total <= 0:
        units_done, units_total = phase_done, 0
        pill_units = str(unit_label or "items").strip() or "items"
    else:
        units_done, units_total = _coarse_phase_counts(merged)
        pill_units = "phases"

    # Counts live on expandable phase bars; do not keep a separate last-phase-only card strip.
    return publish_task_live_progress(
        int(task_run_id),
        kind=kind_key,
        label=str(label or default_label).strip() or default_label,
        stage=stage,
        stage_label=stage_label,
        units_done=units_done,
        units_total=units_total,
        unit_label=pill_units,
        phases=merged,
        replace_phases=True,
        detail_sections=[],
    )


def append_or_update_live_phases(
    task_run_id: int,
    *,
    kind: str,
    label: str,
    phases: list[dict[str, Any]],
    stage: str | None = None,
    stage_label: str | None = None,
    units_done: int | None = None,
    units_total: int | None = None,
    unit_label: str = "phases",
    detail_sections: list[dict[str, Any]] | None = None,
    active_units: list[dict[str, Any]] | None = None,
    replace_active_units: bool = False,
) -> dict[str, Any] | None:
    """Merge phase updates into existing live without wiping prior bars."""
    prev = _load_summary(int(task_run_id)).get(LIVE_KEY)
    prev_phases: list[dict[str, Any]] = []
    if isinstance(prev, dict) and isinstance(prev.get("phases"), list):
        prev_phases = list(prev["phases"])
    merged = _merge_phases(prev_phases, phases, replace=False)
    if units_done is None or units_total is None:
        coarse_done, coarse_total = _coarse_phase_counts(merged)
        if units_done is None:
            units_done = coarse_done
        if units_total is None:
            units_total = coarse_total
    if not stage or not stage_label:
        auto_stage, auto_label = _stage_from_phases(merged)
        stage = stage or auto_stage
        stage_label = stage_label or auto_label
    return publish_task_live_progress(
        int(task_run_id),
        kind=kind,
        label=label,
        stage=stage,
        stage_label=stage_label,
        units_done=int(units_done or 0),
        units_total=int(units_total or 0),
        unit_label=unit_label,
        phases=merged,
        active_units=active_units,
        detail_sections=detail_sections,
        replace_active_units=replace_active_units,
        replace_phases=True,
    )


def _art_live_parent_identity(task_run_id: int, summary: dict[str, Any] | None = None) -> tuple[str, str, bool]:
    """Return (kind, label, merge_into_sync) for art publishes on this run."""
    task_key = _task_run_key_for_live(int(task_run_id))
    snap = summary if isinstance(summary, dict) else _load_summary(int(task_run_id))
    prev = snap.get(LIVE_KEY) if isinstance(snap.get(LIVE_KEY), dict) else {}
    prev_kind = str((prev or {}).get("kind") or "").strip().lower()
    if task_key == "full_sync" or prev_kind == "full_sync":
        return "full_sync", "Full sync", True
    return "art_refresh", "Art refresh", False


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
    # Never show Updating at 100% while any batch bar is still working (ghost or real).
    any_working = any(str(u.get("status") or "").lower() == "working" for u in by_index.values())
    if any_working and items_total > 0 and items_done >= items_total:
        items_done = items_total - 1
    return items_done, items_total


def _art_phase_rows(
    *,
    determining_status: str,
    determining_done: int = 0,
    determining_total: int = 0,
    updating_status: str = "pending",
    updating_done: int = 0,
    updating_total: int = 0,
    determining_metrics: list[dict[str, Any]] | None = None,
    updating_metrics: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Always publish both art phase bars so the run shape is stable from the start.

    Each phase always carries expandable count cards (zeroed / — until real values land).
    """
    from services.task_run_phases import empty_live_metrics_for_phase

    determining: dict[str, Any] = {
        "key": "determining",
        "label": "Determining",
        "done": int(determining_done or 0),
        "total": int(determining_total or 0),
        "status": determining_status,
        "metrics": list(determining_metrics)
        if determining_metrics is not None
        else empty_live_metrics_for_phase("determining"),
    }
    updating: dict[str, Any] = {
        "key": "updating",
        "label": "Updating",
        "done": int(updating_done or 0),
        "total": int(updating_total or 0),
        "status": updating_status,
        "metrics": list(updating_metrics)
        if updating_metrics is not None
        else empty_live_metrics_for_phase("updating"),
    }
    return [determining, updating]


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
    kind, label, merge_sync = _art_live_parent_identity(int(task_run_id))
    from services.task_run_phases import empty_live_metrics_for_phase

    upd_metrics = empty_live_metrics_for_phase("updating")
    for metric in upd_metrics:
        if str(metric.get("label") or "") == "Trigger":
            metric["value"] = trigger_label
    if skip_determination:
        det_metrics = [
            {"label": "Trigger", "value": trigger_label},
            {"label": "Scanned", "value": "— / —"},
            {"label": "Needs refresh", "value": "—"},
            {"label": "Reason", "value": "Skipped (settings apply)"},
        ]
        phases = _art_phase_rows(
            determining_status="skipped",
            updating_status="pending",
            determining_metrics=det_metrics,
            updating_metrics=upd_metrics,
        )
        # Batches may exist but workers can still be busy (e.g. startup sync).
        stage = "queued"
        stage_label = "Queued"
    else:
        det_metrics = [
            {"label": "Trigger", "value": trigger_label},
            {"label": "Scanned", "value": "— / —"},
            {"label": "Needs refresh", "value": "—"},
        ]
        phases = _art_phase_rows(
            determining_status="pending",
            updating_status="pending",
            determining_metrics=det_metrics,
            updating_metrics=upd_metrics,
        )
        stage = "determining"
        stage_label = "Determining"
    if merge_sync:
        return append_or_update_live_phases(
            int(task_run_id),
            kind=kind,
            label=label,
            phases=phases,
            stage=stage,
            stage_label=stage_label,
            units_done=0,
            units_total=0,
            unit_label="placeholders",
            detail_sections=[],
            active_units=[],
            replace_active_units=True,
        )
    return publish_task_live_progress(
        int(task_run_id),
        kind=kind,
        label=label,
        stage=stage,
        stage_label=stage_label,
        units_done=0,
        units_total=0,
        unit_label="placeholders",
        phases=phases,
        active_units=[],
        detail_sections=[],
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
    det_metrics = [
        {"label": "Trigger", "value": trigger.replace("_", " ").title() if trigger != "-" else "-"},
        {"label": "Scanned", "value": f"{scanned_n} / {total_n}"},
        {"label": "Needs refresh", "value": needed_n},
    ]
    art_phases = _art_phase_rows(
        determining_status="done" if det_done else "working",
        determining_done=scanned_n,
        determining_total=total_n,
        updating_status="pending",
        determining_metrics=det_metrics,
    )
    kind, label, merge_sync = _art_live_parent_identity(int(task_run_id))
    if merge_sync:
        return append_or_update_live_phases(
            int(task_run_id),
            kind=kind,
            label=label,
            phases=art_phases,
            stage="determining",
            stage_label="Determining",
            units_done=scanned_n,
            units_total=total_n,
            unit_label="placeholders",
            detail_sections=[],
            active_units=[],
            replace_active_units=True,
        )
    return publish_task_live_progress(
        int(task_run_id),
        kind=kind,
        label=label,
        stage="determining",
        stage_label="Determining",
        units_done=scanned_n,
        units_total=total_n,
        unit_label="placeholders",
        phases=art_phases,
        active_units=[],
        detail_sections=[],
        replace_active_units=True,
    )


def apply_art_refresh_live_into_summary(
    summary: dict[str, Any],
    *,
    task_run_id: int | None = None,
    active_batch: dict[str, Any] | None = None,
    replace_active_units: bool = False,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Derive art live fields into ``summary[live]`` (caller holds the row lock)."""
    art = summary.get("art_backfill") if isinstance(summary.get("art_backfill"), dict) else {}
    counts = art.get("counts") if isinstance(art.get("counts"), dict) else {}
    batches_done = int(art.get("batches_done") or 0)
    batches_total = int(art.get("batch_count") or 0)
    trigger = "-"
    if task_run_id is not None:
        trigger = _task_run_trigger(int(task_run_id)) or str(summary.get("trigger") or "").strip() or "-"
    else:
        trigger = str(summary.get("trigger") or "").strip() or "-"
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

    preserved_sections = _preserve_determination_section(
        prev_live,
        [run_section, outcomes_section],
        status=determining_status if determining_status in ("done", "skipped") else "done",
    )
    det_section_metrics: list[dict[str, Any]] = []
    for section in preserved_sections:
        if str(section.get("name") or "") == "Determination":
            det_section_metrics = list(section.get("metrics") or [])
            break
    if not det_section_metrics and isinstance(prev_det, dict) and prev_det.get("metrics"):
        det_section_metrics = list(prev_det.get("metrics") or [])
    updating_metrics = list(run_section.get("metrics") or []) + list(outcomes_section.get("metrics") or [])

    replace_units = replace_active_units
    # Ephemeral batch bars: show while Updating is working; clear once the phase finishes
    # (prune-after-N-seconds only runs on subsequent publishes, which stop when the run ends).
    if updating_status in ("done", "skipped", "pending") or not art_working:
        units: list[dict[str, Any]] | None = []
        replace_units = True
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
        determining_metrics=det_section_metrics or None,
        updating_metrics=updating_metrics,
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

    parent_kind, parent_label, merge_sync = ("art_refresh", "Art refresh", False)
    if task_run_id is not None:
        parent_kind, parent_label, merge_sync = _art_live_parent_identity(int(task_run_id), summary)

    if merge_sync:
        prev_phases = list(prev_live.get("phases") or []) if isinstance(prev_live.get("phases"), list) else []
        merged_phases = _merge_phases(prev_phases, phase_updates, replace=False)
        live = _build_live_payload(
            summary,
            kind=parent_kind,
            label=parent_label,
            stage=stage,
            stage_label=stage_label,
            units_done=headline_done,
            units_total=headline_total,
            unit_label="placeholders",
            phases=merged_phases,
            active_units=units,
            detail_sections=[],
            replace_active_units=replace_units,
            replace_phases=True,
            now=now,
        )
    else:
        live = _build_live_payload(
            summary,
            kind=parent_kind,
            label=parent_label,
            stage=stage,
            stage_label=stage_label,
            units_done=headline_done,
            units_total=headline_total,
            unit_label="placeholders",
            phases=phase_updates,
            active_units=units,
            detail_sections=[],
            replace_active_units=replace_units,
            now=now,
        )
    if live is None:
        return None
    summary[LIVE_KEY] = live
    return live


def publish_art_refresh_live(
    task_run_id: int,
    *,
    active_batch: dict[str, Any] | None = None,
    replace_active_units: bool = False,
) -> dict[str, Any] | None:
    """Art-refresh adapter: derive live fields from ``summary.art_backfill`` + phases."""
    from services.task_run_history import mutate_task_run_summary

    try:
        run_id = int(task_run_id)
    except Exception:
        return None
    if run_id <= 0:
        return None

    built: dict[str, Any] | None = None
    now = _utc_now()

    def _mutator(summary: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal built
        live = apply_art_refresh_live_into_summary(
            summary,
            task_run_id=run_id,
            active_batch=active_batch,
            replace_active_units=replace_active_units,
            now=now,
        )
        if live is None:
            prev = summary.get(LIVE_KEY) if isinstance(summary.get(LIVE_KEY), dict) else None
            built = prev if isinstance(prev, dict) else None
            return None
        built = live
        return summary

    mutate_task_run_summary(run_id, _mutator)
    return built
