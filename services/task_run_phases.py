"""Structured phases for scheduled_task_run progress (Tasks UI expandable rows)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.logger import logger
from services.task_run_history import reopen_task_run, update_task_run_summary

FULL_SYNC_TASK_RUN_ID_KEY = "full_sync_task_run_id"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _duration_seconds(started: datetime | None, ended: datetime | None) -> float | None:
    if not started or not ended:
        return None
    s = started if started.tzinfo else started.replace(tzinfo=timezone.utc)
    e = ended if ended.tzinfo else ended.replace(tzinfo=timezone.utc)
    return max(0.0, (e - s).total_seconds())


def phases_from_summary(summary: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(summary, dict):
        return []
    raw = summary.get("phases")
    return list(raw) if isinstance(raw, list) else []


def _section_from_phase(phase: dict[str, Any]) -> dict[str, Any]:
    started = phase.get("started_at")
    ended = phase.get("ended_at")
    dur = phase.get("duration_seconds")
    if dur is None and started and ended:
        try:
            s = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            e = datetime.fromisoformat(str(ended).replace("Z", "+00:00"))
            dur = _duration_seconds(s, e)
        except Exception:
            dur = None
    return {
        "key": phase.get("key"),
        "name": str(phase.get("name") or phase.get("key") or "Phase"),
        "status": str(phase.get("status") or "pending").lower(),
        "started_at": started,
        "ended_at": ended,
        "duration_seconds": dur,
        "metrics": list(phase.get("metrics") or []),
    }


def build_progress_from_phases(
    *,
    task_run_id: int,
    mode: str,
    started_at: datetime,
    phases: list[dict[str, Any]],
    details: str | None = None,
    overall_status: str = "DONE",
    completed_at: datetime | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    """Build nested progress blob stored under summary.progress for Tasks UI."""
    display_mode = str(mode or "full").strip().lower() or "full"
    is_lite = display_mode == "lite"
    is_placeholder_refresh = display_mode == "placeholder_refresh"
    _order = {
        "arr_sync": 0,
        "fs_scan": 1,
        "determination": 2,
        "materialization": 3,
        "calendar": 4,
        "art_refresh": 5,
        "metadata_refresh": 6,
    }
    sorted_phases = sorted(
        phases,
        key=lambda p: (_order.get(str(p.get("key") or ""), 50), str(p.get("started_at") or "")),
    )
    sections = [_section_from_phase(p) for p in sorted_phases]
    any_working = any(str(s.get("status") or "").lower() == "working" for s in sections)
    running = overall_status.upper() == "WORKING" or any_working
    if not details:
        if is_placeholder_refresh:
            meta = next((p for p in phases if p.get("key") == "metadata_refresh"), None)
            art = next((p for p in phases if p.get("key") == "art_refresh"), None)
            parts: list[str] = []
            if meta and str(meta.get("status") or "").lower() != "skipped":
                parts.append("metadata")
            if art and str(art.get("status") or "").lower() != "skipped":
                parts.append("art")
            details = f"{' + '.join(parts) or 'placeholder'} refresh" if parts else "Placeholder refresh"
        else:
            mat = next((p for p in phases if p.get("key") == "materialization"), None)
            if mat and isinstance(mat.get("metrics"), list):
                created = next((m for m in mat["metrics"] if m.get("label") == "Placeholders created"), None)
                removed = next((m for m in mat["metrics"] if m.get("label") == "Placeholders removed"), None)
                c = created.get("value") if created else 0
                r = removed.get("value") if removed else 0
                details = f"Created {c} placeholders • removed {r} placeholders • Mode {display_mode}"
            else:
                details = f"Mode {display_mode}"

    sort_anchor = completed_at if completed_at else started_at
    return {
        "id": f"task-run-{task_run_id}",
        "type": "job",
        "job_type": (
            "placeholder_refresh_progress"
            if is_placeholder_refresh
            else ("lite_sync_progress" if is_lite else "full_sync_progress")
        ),
        "display_name": (
            "Placeholder Refresh Progress"
            if is_placeholder_refresh
            else ("Lite Sync Progress" if is_lite else "Full Sync Progress")
        ),
        "status": overall_status.upper(),
        "details": details,
        "error": error_message,
        "time": _iso(sort_anchor),
        "progress": {
            "running": running,
            "sections": sections,
        },
    }


class TaskRunPhaseTracker:
    """Record timed phases on a scheduled_task_run row."""

    def __init__(self, task_run_id: int, *, started_at: datetime | None = None) -> None:
        self.task_run_id = int(task_run_id)
        self.started_at = started_at or _utc_now()
        self._phases: list[dict[str, Any]] = []
        self._open: dict[str, datetime] = {}
        self._live_kind = _task_run_key(int(task_run_id))

    def _publish_live(
        self,
        key: str,
        name: str,
        status: str,
        metrics: list[dict[str, Any]] | None = None,
        *,
        done: int | None = None,
        total: int | None = None,
        unit_label: str | None = None,
        units: list[dict[str, Any]] | None = None,
    ) -> None:
        kind = str(self._live_kind or "").strip().lower()
        # calendar_sync is the task key; calendar_only remains accepted for older rows.
        if kind not in {"full_sync", "lite_sync", "calendar_sync", "calendar_only"}:
            return
        try:
            from services.task_live_progress import publish_sync_phase_live

            publish_sync_phase_live(
                self.task_run_id,
                kind=kind,
                phase_key=key,
                phase_label=name,
                status=status,
                detail_metrics=metrics,
                done=done,
                total=total,
                unit_label=unit_label,
                phase_units=units,
            )
        except Exception as exc:
            logger.debug("%s live phase publish skipped: %s", kind, exc, extra={"emoji_type": "debug"})

    def begin(self, key: str, name: str) -> None:
        key = str(key).strip().lower()
        now = _utc_now()
        self._open[key] = now
        seed_metrics = empty_live_metrics_for_phase(key)
        existing = next((p for p in self._phases if p.get("key") == key), None)
        if existing:
            existing["status"] = "working"
            existing["started_at"] = _iso(now)
            existing.pop("ended_at", None)
            existing.pop("duration_seconds", None)
            if not existing.get("metrics"):
                existing["metrics"] = seed_metrics
            metrics = list(existing.get("metrics") or seed_metrics)
        else:
            metrics = seed_metrics
            self._phases.append(
                {
                    "key": key,
                    "name": name,
                    "status": "working",
                    "started_at": _iso(now),
                    "metrics": list(metrics),
                }
            )
        self._persist()
        self._publish_live(key, name, "working", metrics)

    def end(
        self,
        key: str,
        *,
        status: str = "done",
        metrics: list[dict[str, Any]] | None = None,
        failed: bool = False,
        done: int | None = None,
        total: int | None = None,
        unit_label: str | None = None,
        units: list[dict[str, Any]] | None = None,
    ) -> None:
        key = str(key).strip().lower()
        now = _utc_now()
        started = self._open.pop(key, None)
        phase = next((p for p in self._phases if p.get("key") == key), None)
        if not phase:
            phase = {"key": key, "name": key, "metrics": []}
            self._phases.append(phase)
        if started:
            phase["started_at"] = _iso(started)
        phase["ended_at"] = _iso(now)
        if started:
            phase["duration_seconds"] = _duration_seconds(started, now)
        phase["status"] = "failed" if failed else str(status).lower()
        if metrics is not None:
            phase["metrics"] = metrics
        if units is not None:
            phase["units"] = units
        self._persist()
        live_status = "failed" if failed else str(status).lower()
        self._publish_live(
            key,
            str(phase.get("name") or key),
            live_status,
            metrics,
            done=done,
            total=total,
            unit_label=unit_label,
            units=units,
        )

    def progress(
        self,
        key: str,
        *,
        done: int | None = None,
        total: int | None = None,
        unit_label: str | None = None,
        metrics: list[dict[str, Any]] | None = None,
        units: list[dict[str, Any]] | None = None,
    ) -> None:
        """Mid-phase live counts (does not end the phase)."""
        key = str(key).strip().lower()
        phase = next((p for p in self._phases if p.get("key") == key), None)
        name = str((phase or {}).get("name") or key)
        if phase is not None:
            if metrics is not None:
                phase["metrics"] = metrics
            if units is not None:
                phase["units"] = units
            if metrics is not None or units is not None:
                self._persist()
        self._publish_live(
            key,
            name,
            "working",
            metrics,
            done=None if done is None else int(done or 0),
            total=None if total is None else int(total or 0),
            unit_label=unit_label,
            units=units,
        )

    def update_metrics(self, key: str, metrics: list[dict[str, Any]]) -> None:
        phase = next((p for p in self._phases if p.get("key") == key), None)
        if phase:
            phase["metrics"] = metrics
            self._persist()

    def phases(self) -> list[dict[str, Any]]:
        return list(self._phases)

    def _persist(self, *, extra_summary: dict[str, Any] | None = None) -> None:
        kind = str(self._live_kind or "").strip().lower()
        if kind == "lite_sync":
            progress_mode = "lite"
        elif kind == "placeholder_refresh":
            progress_mode = "placeholder_refresh"
        else:
            progress_mode = "full"
        payload: dict[str, Any] = {
            "phases": self._phases,
            "progress": build_progress_from_phases(
                task_run_id=self.task_run_id,
                mode=progress_mode,
                started_at=self.started_at,
                phases=self._phases,
                overall_status="WORKING" if any(p.get("status") == "working" for p in self._phases) else "DONE",
            ),
        }
        if extra_summary:
            payload.update(extra_summary)
        update_task_run_summary(self.task_run_id, payload)


def _arr_instance_kind(inst_key: str, inst: dict[str, Any]) -> str:
    """``radarr`` | ``sonarr`` | ``unknown`` for task UI formatting."""
    explicit = str(inst.get("arr_type") or "").strip().lower()
    if explicit in {"radarr", "sonarr"}:
        return explicit
    key = str(inst_key or "").strip().lower()
    if key.startswith("radarr") or key == "radarr":
        return "radarr"
    if key.startswith("sonarr") or key == "sonarr":
        return "sonarr"
    return "unknown"


def _format_arr_instance_value(inst_key: str, inst: dict[str, Any]) -> str:
    dur = float(inst.get("duration_seconds") or 0)
    kind = _arr_instance_kind(inst_key, inst)
    movies = int(inst.get("movies_updated") or inst.get("movies_seen") or 0)
    series = int(inst.get("series_updated") or inst.get("series_seen") or 0)
    episodes = int(inst.get("episodes_updated") or inst.get("episodes_seen") or 0)
    if kind == "radarr":
        return f"{movies} movies ({dur:.0f}s)"
    if kind == "sonarr":
        return f"{series} series, {episodes} eps ({dur:.0f}s)"
    parts = []
    if movies:
        parts.append(f"{movies} movies")
    if series or episodes:
        parts.append(f"{series} series, {episodes} eps")
    if not parts:
        parts.append("0 items")
    parts.append(f"({dur:.0f}s)")
    return " ".join(parts)


def metrics_from_arr_sync(sync_stats: dict[str, Any]) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    total_dur = 0.0
    for inst_key, inst in (sync_stats or {}).items():
        if not isinstance(inst, dict):
            continue
        total_dur += float(inst.get("duration_seconds") or 0)
        metrics.append(
            {
                "label": str(inst_key),
                "value": _format_arr_instance_value(str(inst_key), inst),
            }
        )
    metrics.insert(0, {"label": "Total ARR sync time", "value": f"{total_dur:.0f}s"})
    return metrics


def _fs_scan_status_label(scan_info: dict[str, Any]) -> str:
    reason = str(scan_info.get("reason") or "").strip().lower()
    if reason == "already_claimed":
        return "Skipped (already ran for this sync)"
    if reason == "no_roots":
        return "Skipped (no library roots configured)"
    if reason == "error":
        return "Failed"
    if reason != "ok":
        return reason or "--"
    if scan_info.get("incremental"):
        return "Completed (incremental)"
    if scan_info.get("full_scan"):
        return "Completed (full library walk)"
    return "Completed"


def _fs_scan_metrics(scan: dict[str, Any], scan_info: dict[str, Any]) -> list[dict[str, Any]]:
    """Filesystem phase metrics — ``scan.count`` is new DB rows, not files walked."""
    metrics: list[dict[str, Any]] = []
    files_seen = scan_info.get("files_seen")
    media_candidates = scan_info.get("media_candidates")
    if files_seen is not None:
        metrics.append({"label": "Files on disk (walked)", "value": int(files_seen)})
    if media_candidates is not None:
        metrics.append({"label": "Placeholder media files", "value": int(media_candidates)})
    metrics.append({"label": "New paths indexed", "value": int(scan.get("count") or 0)})
    stale = scan_info.get("stale_marked")
    if stale is not None and int(stale) > 0:
        metrics.append({"label": "Marked missing", "value": int(stale)})
    metrics.append({"label": "Status", "value": _fs_scan_status_label(scan_info)})
    return metrics


def metrics_from_lite_discovery(stats: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Count cards for lite Catalog discovery (ARR catalog diff + targeted sync)."""
    s = stats if isinstance(stats, dict) else {}
    movies = int(s.get("movies_seen") or 0)
    series = int(s.get("series_seen") or 0)
    episodes = int(s.get("episodes_seen") or 0)
    targeted = int(s.get("movies_targeted") or 0) + int(s.get("series_targeted") or 0)
    return [
        {"label": "Movies updated", "value": movies},
        {"label": "Series updated", "value": series},
        {"label": "Episodes updated", "value": episodes},
        {"label": "Titles targeted", "value": targeted},
    ]


def metrics_from_calendar_dates(stats: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Count cards for calendar date refresh."""
    s = stats if isinstance(stats, dict) else {}
    return [
        {"label": "Movies checked", "value": int(s.get("movie_rows_seen") or 0)},
        {"label": "Episodes checked", "value": int(s.get("episode_rows_seen") or 0)},
        {"label": "Movie dates updated", "value": int(s.get("movie_rows_updated") or 0)},
        {"label": "Episode dates updated", "value": int(s.get("episode_rows_updated") or 0)},
    ]


def metrics_from_pipeline(pipeline: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Return metrics lists keyed by phase key."""
    out: dict[str, list[dict[str, Any]]] = {}
    scan = pipeline.get("scan") if isinstance(pipeline.get("scan"), dict) else {}
    scan_info = scan.get("info") if isinstance(scan.get("info"), dict) else {}
    out["fs_scan"] = _fs_scan_metrics(scan, scan_info)
    det = pipeline.get("determination") if isinstance(pipeline.get("determination"), dict) else {}
    # Same card set for full (library walk) and lite (scoped) determination.
    out["determination"] = [
        {"label": "Movies evaluated", "value": int(det.get("movies_total") or 0)},
        {"label": "Episodes evaluated", "value": int(det.get("episodes_total") or 0)},
        {"label": "Already has placeholder", "value": int(det.get("placeholder_exists") or 0)},
        {"label": "Not yet aired", "value": int(det.get("not_needed_not_yet_aired") or det.get("not_needed") or 0)},
        {"label": "Air date unknown", "value": int(det.get("not_needed_air_date_unknown") or 0)},
        {
            "label": "Tombstones skipped",
            "value": int(det.get("movies_skipped_tombstone") or 0)
            + int(det.get("episodes_skipped_tombstone") or 0),
        },
    ]
    mat = pipeline.get("materialization") if isinstance(pipeline.get("materialization"), dict) else {}
    out["materialization"] = [
        {"label": "Placeholders created", "value": int(mat.get("created") or 0)},
        {"label": "Placeholders removed", "value": int(mat.get("deleted") or 0)},
        {"label": "Already up to date", "value": int(mat.get("noop") or 0)},
        {"label": "Errors", "value": int(mat.get("errors") or 0)},
    ]
    gc = pipeline.get("catalog_gc") if isinstance(pipeline.get("catalog_gc"), dict) else {}
    out["catalog_gc"] = [
        {"label": "Movies purged", "value": int(gc.get("movies_purged") or 0)},
        {"label": "Series purged", "value": int(gc.get("series_purged") or 0)},
        {"label": "Seasons purged", "value": int(gc.get("seasons_purged") or 0)},
        {"label": "Episodes purged", "value": int(gc.get("episodes_purged") or 0)},
        {"label": "Skipped (not ready)", "value": int(gc.get("skipped_not_ready") or 0)},
    ]
    cal = pipeline.get("calendar") if isinstance(pipeline.get("calendar"), dict) else {}
    out["calendar"] = [
        {"label": "Placeholders scanned", "value": int(cal.get("scanned") or 0)},
        {
            "label": "Calendar updates",
            "value": int(
                cal.get("updated")
                or cal.get("changed")
                or cal.get("status_applied")
                or 0
            ),
        },
    ]
    out["discovery"] = metrics_from_lite_discovery(
        pipeline.get("discovery") if isinstance(pipeline.get("discovery"), dict) else pipeline.get("startup_sync")
    )
    out["calendar_dates"] = metrics_from_calendar_dates(
        pipeline.get("calendar_dates")
        if isinstance(pipeline.get("calendar_dates"), dict)
        else pipeline.get("calendar_date_refresh")
    )
    return out


def empty_live_metrics_for_phase(phase_key: str) -> list[dict[str, Any]]:
    """Zeroed / placeholder count cards so any live phase is expandable from seed / begin."""
    key = str(phase_key or "").strip().lower()
    if key == "arr_sync":
        return [{"label": "Total ARR sync time", "value": "0s"}]
    if key == "fs_scan":
        return [
            {"label": "Files on disk (walked)", "value": 0},
            {"label": "Placeholder media files", "value": 0},
            {"label": "New paths indexed", "value": 0},
            {"label": "Status", "value": "Pending"},
        ]
    if key == "determining":
        return [
            {"label": "Trigger", "value": "—"},
            {"label": "Scanned", "value": "— / —"},
            {"label": "Needs refresh", "value": "—"},
        ]
    if key == "updating":
        return [
            {"label": "Trigger", "value": "—"},
            {"label": "Source", "value": "—"},
            {"label": "Placeholders", "value": "— / —"},
            {"label": "Batches", "value": "— / —"},
            {"label": "Movie posters", "value": 0},
            {"label": "Series posters", "value": 0},
            {"label": "Season posters", "value": 0},
            {"label": "Episode stills", "value": 0},
            {"label": "Created total", "value": 0},
        ]
    shells = metrics_from_pipeline({})
    return list(shells.get(key) or [])


ART_COUNT_KEYS = ("movie", "series", "season", "episode")


def empty_art_counts() -> dict[str, int]:
    return {k: 0 for k in ART_COUNT_KEYS}


def merge_art_counts(into: dict[str, int], batch: dict[str, Any] | None) -> None:
    if not isinstance(batch, dict):
        return
    for key in ART_COUNT_KEYS:
        into[key] = int(into.get(key, 0)) + int(batch.get(key, 0))


def art_metrics_from_counts(counts: dict[str, int], *, batches_done: int = 0, batches_total: int = 0) -> list[dict[str, Any]]:
    metrics = [
        {"label": "Movie posters", "value": int(counts.get("movie", 0))},
        {"label": "Series posters", "value": int(counts.get("series", 0))},
        {"label": "Season posters", "value": int(counts.get("season", 0))},
        {"label": "Episode stills", "value": int(counts.get("episode", 0))},
    ]
    if batches_total > 0:
        metrics.append({"label": "Batches completed", "value": f"{batches_done} / {batches_total}"})
    return metrics


def _task_run_id_payload_match(task_run_id: int):
    """Match full_sync_task_run_id stored as JSON string or number."""
    from sqlalchemy import String, cast, or_

    from services.postgres.models import Job

    tid = str(int(task_run_id))
    col = Job.payload[FULL_SYNC_TASK_RUN_ID_KEY]
    legacy = Job.payload["art_backfill_task_run_id"]
    return or_(
        col.as_string() == tid,
        cast(col, String) == tid,
        legacy.as_string() == tid,
        cast(legacy, String) == tid,
    )


def _placeholder_refresh_task_run_id_payload_match(task_run_id: int):
    from sqlalchemy import String, cast, or_

    from services.postgres.models import Job
    from services.source_of_truth.placeholder_refresh import PLACEHOLDER_REFRESH_TASK_RUN_ID_KEY

    tid = str(int(task_run_id))
    col = Job.payload[PLACEHOLDER_REFRESH_TASK_RUN_ID_KEY]
    return or_(col.as_string() == tid, cast(col, String) == tid)


def _linked_task_run_job_match(task_run_id: int):
    from sqlalchemy import or_

    return or_(_task_run_id_payload_match(task_run_id), _placeholder_refresh_task_run_id_payload_match(task_run_id))


def _pending_jobs_for_full_sync_task(task_run_id: int, *, exclude_job_id: int | None = None) -> bool:
    """True while any follow-up job is still queued or claimed for this task run.

    Pass ``exclude_job_id`` when checking from inside a follow-up job handler: the worker
    keeps that row ``CLAIMED`` until the handler returns, so counting it would block
    parent task completion forever on the last art/NFO batch.
    """
    from services.postgres.models import Job

    session = get_session()
    try:
        q = session.query(Job.id).filter(
            Job.job_type.in_(("placeholder_art_refresh", "nfo_refresh")),
            Job.status.in_(["PENDING", "CLAIMED"]),
            _linked_task_run_job_match(task_run_id),
        )
        if exclude_job_id is not None:
            q = q.filter(Job.id != int(exclude_job_id))
        return q.first() is not None
    finally:
        session.close()


def _task_run_key(task_run_id: int) -> str:
    from services.postgres.models import ScheduledTaskRun

    session = get_session()
    try:
        row = session.query(ScheduledTaskRun).filter(ScheduledTaskRun.id == int(task_run_id)).first()
        return str(row.task_key or "").strip().lower() if row else ""
    finally:
        session.close()


def try_complete_linked_task_run(
    task_run_id: int,
    *,
    failed: bool = False,
    error_message: str | None = None,
    exclude_job_id: int | None = None,
) -> bool:
    if _task_run_key(task_run_id) == "placeholder_refresh":
        from services.source_of_truth.placeholder_refresh import try_complete_placeholder_refresh_task_run

        return try_complete_placeholder_refresh_task_run(
            int(task_run_id),
            failed=failed,
            error_message=error_message,
            exclude_job_id=exclude_job_id,
        )
    return try_complete_full_sync_task_run(
        int(task_run_id),
        failed=failed,
        error_message=error_message,
        exclude_job_id=exclude_job_id,
    )


def get_session():
    from services.postgres.db import get_session as _gs

    return _gs()


def mark_follow_up_phase_skipped(task_run_id: int, phase_key: str, name: str, *, reason: str) -> None:
    summary = _load_summary(task_run_id)
    phases = phases_from_summary(summary) or list(summary.get("phases") or [])
    phases = [p for p in phases if p.get("key") != phase_key]
    phases.append(
        {
            "key": phase_key,
            "name": name,
            "status": "skipped",
            "metrics": [{"label": "Reason", "value": reason}],
        }
    )
    _save_phases(task_run_id, phases, extra=summary)
    if _task_run_key(int(task_run_id)) == "full_sync":
        try:
            from services.task_live_progress import publish_sync_phase_live

            # Art follow-up uses determining/updating bars on live; map legacy art_refresh skip.
            live_key = "updating" if str(phase_key) == "art_refresh" else str(phase_key)
            live_label = "Updating" if live_key == "updating" else name
            if str(phase_key) == "art_refresh":
                publish_sync_phase_live(
                    int(task_run_id),
                    kind="full_sync",
                    phase_key="determining",
                    phase_label="Determining",
                    status="skipped",
                    detail_metrics=[{"label": "Reason", "value": reason}],
                )
            publish_sync_phase_live(
                int(task_run_id),
                kind="full_sync",
                phase_key=live_key,
                phase_label=live_label,
                status="skipped",
                detail_metrics=[{"label": "Reason", "value": reason}],
            )
        except Exception as exc:
            logger.debug("full_sync live follow-up skip publish skipped: %s", exc, extra={"emoji_type": "debug"})


def refresh_full_sync_task_progress(
    task_run_id: int,
    *,
    phases: list[dict[str, Any]] | None = None,
    overall_status: str = "WORKING",
) -> None:
    summary = _load_summary(task_run_id)
    phase_list = phases if phases is not None else phases_from_summary(summary) or list(summary.get("phases") or [])
    started_at = _utc_now()
    if summary.get("task_run_started_at"):
        try:
            started_at = datetime.fromisoformat(str(summary["task_run_started_at"]).replace("Z", "+00:00"))
        except Exception:
            pass
    progress = build_progress_from_phases(
        task_run_id=task_run_id,
        mode=str(summary.get("mode") or "full"),
        started_at=started_at,
        phases=phase_list,
        overall_status=overall_status,
    )
    # Do not rewrite ``live`` from this unlocked read; concurrent art/NFO heartbeats
    # may have a newer live payload under mutate_task_run_summary.
    payload = {**summary, "phases": phase_list, "progress": progress}
    payload.pop("live", None)
    update_task_run_summary(task_run_id, payload)


def try_complete_full_sync_task_run(
    task_run_id: int,
    *,
    failed: bool = False,
    error_message: str | None = None,
    exclude_job_id: int | None = None,
) -> bool:
    """Finish the task run once all full-sync follow-up jobs (art, NFO) have drained."""
    if _pending_jobs_for_full_sync_task(task_run_id, exclude_job_id=exclude_job_id):
        logger.debug(
            f"full_sync task_run_id={task_run_id} still waiting on follow-up jobs",
            extra={"emoji_type": "debug"},
        )
        return False
    summary = _load_summary(task_run_id)
    phases = phases_from_summary(summary) or list(summary.get("phases") or [])
    complete_full_sync_task_run(task_run_id, phases=phases, failed=failed, error_message=error_message)
    return True


def begin_art_backfill_phase(
    task_run_id: int,
    *,
    art_run_id: str,
    placeholder_count: int,
    batch_count: int,
    source: str,
    determination_skipped: bool = False,
) -> None:
    now = _utc_now()
    summary = _load_summary(task_run_id)
    phases = phases_from_summary(summary)
    if not phases and isinstance(summary.get("phases"), list):
        phases = list(summary["phases"])
    empty_queue = int(placeholder_count or 0) <= 0 and int(batch_count or 0) <= 0
    phase = {
        "key": "art_refresh",
        "name": "Art refresh",
        "status": "done" if empty_queue else "working",
        "started_at": _iso(now),
        "metrics": [
            {"label": "Placeholders queued", "value": placeholder_count},
            {"label": "Batches", "value": batch_count},
            {"label": "Source", "value": source},
        ],
    }
    if empty_queue:
        phase["ended_at"] = _iso(now)
        phase["metrics"] = [
            {"label": "Placeholders queued", "value": 0},
            {"label": "Batches", "value": 0},
            {"label": "Source", "value": source},
            {"label": "Reason", "value": "Nothing needed a refresh"},
        ]
    phases = [p for p in phases if p.get("key") != "art_refresh"] + [phase]
    reopen_task_run(task_run_id)
    summary = {**summary, "art_backfill": {
        "run_id": art_run_id,
        "batch_count": batch_count,
        "batches_done": 0 if not empty_queue else 0,
        "items_total": int(placeholder_count),
        "items_done": 0,
        "status": "done" if empty_queue else "working",
        "source": source,
        "determination_skipped": bool(determination_skipped),
        "counts": empty_art_counts(),
    }}
    _save_phases(task_run_id, phases, extra=summary)
    refresh_full_sync_task_progress(
        task_run_id,
        phases=phases,
        overall_status="DONE" if empty_queue else "WORKING",
    )
    try:
        from services.task_live_progress import publish_art_refresh_live

        publish_art_refresh_live(int(task_run_id), replace_active_units=True)
    except Exception as exc:
        logger.debug("art live progress begin skipped: %s", exc, extra={"emoji_type": "debug"})


def begin_nfo_backfill_phase(
    task_run_id: int,
    *,
    nfo_run_id: str,
    placeholder_count: int,
    batch_count: int,
    source: str,
) -> None:
    now = _utc_now()
    summary = _load_summary(task_run_id)
    phases = phases_from_summary(summary) or list(summary.get("phases") or [])
    phase = {
        "key": "metadata_refresh",
        "name": "Metadata refresh",
        "status": "working",
        "started_at": _iso(now),
        "metrics": [
            {"label": "Placeholders queued", "value": placeholder_count},
            {"label": "Batches", "value": batch_count},
            {"label": "Source", "value": source},
        ],
    }
    phases = [p for p in phases if p.get("key") != "metadata_refresh"] + [phase]
    reopen_task_run(task_run_id)
    summary = {
        **summary,
        "nfo_backfill": {
            "run_id": nfo_run_id,
            "batch_count": batch_count,
            "batches_done": 0,
            "status": "working",
        },
    }
    _save_phases(task_run_id, phases, extra=summary)
    refresh_full_sync_task_progress(task_run_id, phases=phases, overall_status="WORKING")
    if _task_run_key(int(task_run_id)) == "full_sync":
        try:
            from services.task_live_progress import publish_sync_phase_live

            publish_sync_phase_live(
                int(task_run_id),
                kind="full_sync",
                phase_key="metadata_refresh",
                phase_label="Metadata refresh",
                status="working",
                detail_metrics=[
                    {"label": "Placeholders queued", "value": placeholder_count},
                    {"label": "Batches", "value": batch_count},
                    {"label": "Source", "value": source},
                ],
            )
        except Exception as exc:
            logger.debug("full_sync metadata live begin skipped: %s", exc, extra={"emoji_type": "debug"})


def finalize_nfo_backfill_phase(task_run_id: int, nfo_run_id: str, *, failed: bool = False) -> None:
    summary = _load_summary(task_run_id)
    nfo = summary.get("nfo_backfill") if isinstance(summary.get("nfo_backfill"), dict) else {}
    if str(nfo.get("run_id") or "") != str(nfo_run_id):
        return
    now = _utc_now()
    phases = phases_from_summary(summary) or list(summary.get("phases") or [])
    for phase in phases:
        if phase.get("key") != "metadata_refresh":
            continue
        started_raw = phase.get("started_at")
        try:
            started = datetime.fromisoformat(str(started_raw).replace("Z", "+00:00"))
        except Exception:
            started = None
        phase["ended_at"] = _iso(now)
        if started:
            phase["duration_seconds"] = _duration_seconds(started, now)
        phase["status"] = "failed" if failed else "done"
        batches_done = int(nfo.get("batches_done", 0))
        batches_total = int(nfo.get("batch_count", 0))
        phase["metrics"] = [
            {"label": "Batches completed", "value": f"{batches_done} / {batches_total}"},
            {"label": "NFO run id", "value": nfo_run_id[:12]},
        ]
    nfo["status"] = "failed" if failed else "done"
    summary["nfo_backfill"] = nfo
    _save_phases(task_run_id, phases, extra=summary)
    if _task_run_key(int(task_run_id)) == "full_sync":
        try:
            from services.task_live_progress import publish_sync_phase_live

            publish_sync_phase_live(
                int(task_run_id),
                kind="full_sync",
                phase_key="metadata_refresh",
                phase_label="Metadata refresh",
                status="failed" if failed else "done",
            )
        except Exception as exc:
            logger.debug("full_sync metadata live finalize skipped: %s", exc, extra={"emoji_type": "debug"})


def accumulate_art_backfill_counts(
    task_run_id: int,
    art_run_id: str,
    batch_counts: dict[str, Any],
    *,
    exclude_job_id: int | None = None,
    items_processed: int | None = None,
    completed_batch: dict[str, Any] | None = None,
) -> None:
    """Increment art counters under a row lock; optionally mark a batch done in the same write."""
    from services.task_run_history import mutate_task_run_summary

    should_finalize = False
    finalize_run_id = ""

    def _mutator(summary: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal should_finalize, finalize_run_id
        art = summary.get("art_backfill") if isinstance(summary.get("art_backfill"), dict) else {}
        if str(art.get("run_id") or "") != str(art_run_id):
            return None
        art = dict(art)
        counts = empty_art_counts()
        merge_art_counts(counts, art.get("counts"))
        merge_art_counts(counts, batch_counts)
        art["counts"] = counts
        art["batches_done"] = int(art.get("batches_done", 0)) + 1
        try:
            delta = int(items_processed) if items_processed is not None else 0
        except Exception:
            delta = 0
        if delta > 0:
            art["items_done"] = int(art.get("items_done") or 0) + delta
        summary["art_backfill"] = art
        phases = phases_from_summary(summary)
        for phase in phases:
            if phase.get("key") != "art_refresh":
                continue
            phase["metrics"] = art_metrics_from_counts(
                counts,
                batches_done=int(art.get("batches_done", 0)),
                batches_total=int(art.get("batch_count", 0)),
            )
            batch_total = int(art.get("batch_count", 0))
            batch_done = int(art.get("batches_done", 0))
            if (
                batch_total > 0
                and batch_done >= batch_total
                and not _pending_jobs_for_full_sync_task(task_run_id, exclude_job_id=exclude_job_id)
            ):
                phase["status"] = "done"
                if not phase.get("ended_at"):
                    phase["ended_at"] = _iso(_utc_now())
        started_raw = summary.get("started_at") if isinstance(summary.get("started_at"), str) else None
        started_at = _utc_now()
        if started_raw:
            try:
                started_at = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
            except Exception:
                pass
        mode = str(summary.get("mode") or "full")
        any_working = any(str(p.get("status") or "").lower() == "working" for p in phases)
        progress = build_progress_from_phases(
            task_run_id=task_run_id,
            mode=mode,
            started_at=started_at,
            phases=phases,
            overall_status="WORKING" if any_working else "DONE",
        )
        summary["phases"] = phases
        summary["progress"] = progress
        try:
            from services.task_live_progress import apply_art_refresh_live_into_summary

            apply_art_refresh_live_into_summary(
                summary,
                task_run_id=int(task_run_id),
                active_batch=completed_batch,
            )
        except Exception as exc:
            logger.debug("art live progress accumulate skipped: %s", exc, extra={"emoji_type": "debug"})
        batch_total = int(art.get("batch_count", 0))
        batch_done = int(art.get("batches_done", 0))
        run_id = str(art.get("run_id") or "").strip()
        if (
            run_id
            and batch_total > 0
            and batch_done >= batch_total
            and not _pending_jobs_for_full_sync_task(task_run_id, exclude_job_id=exclude_job_id)
        ):
            should_finalize = True
            finalize_run_id = run_id
        return summary

    mutate_task_run_summary(int(task_run_id), _mutator)
    if should_finalize and finalize_run_id:
        finalize_art_backfill_phase(task_run_id, finalize_run_id, exclude_job_id=exclude_job_id)


def complete_full_sync_task_run(
    task_run_id: int,
    *,
    phases: list[dict[str, Any]] | None = None,
    failed: bool = False,
    error_message: str | None = None,
) -> None:
    """Close a full-sync task run after all phases (including art) finish."""
    from services.task_run_history import finish_task_run

    summary = _load_summary(task_run_id)
    if phases is not None:
        summary["phases"] = phases
    started_at = _utc_now()
    if isinstance(summary.get("started_at"), str):
        try:
            started_at = datetime.fromisoformat(str(summary["started_at"]).replace("Z", "+00:00"))
        except Exception:
            pass
    elif summary.get("task_run_started_at"):
        try:
            started_at = datetime.fromisoformat(str(summary["task_run_started_at"]).replace("Z", "+00:00"))
        except Exception:
            pass

    completed_at = _utc_now()
    mat_phase = next((p for p in (phases or phases_from_summary(summary)) if p.get("key") == "materialization"), None)
    created_n = 0
    deleted_n = 0
    if mat_phase and isinstance(mat_phase.get("metrics"), list):
        for m in mat_phase["metrics"]:
            if m.get("label") == "Placeholders created":
                created_n = int(m.get("value") or 0)
            if m.get("label") == "Placeholders removed":
                deleted_n = int(m.get("value") or 0)

    phase_list = phases if phases is not None else phases_from_summary(summary)
    progress = build_progress_from_phases(
        task_run_id=task_run_id,
        mode=str(summary.get("mode") or "full"),
        started_at=started_at,
        phases=phase_list,
        details=f"Created {created_n} placeholders • removed {deleted_n} placeholders • Mode full",
        overall_status="FAILED" if failed else "DONE",
        completed_at=completed_at,
        error_message=error_message,
    )
    finish_task_run(
        task_run_id,
        status="failed" if failed else "done",
        summary={**summary, "phases": phase_list, "progress": progress, "completed_at": _iso(completed_at)},
        error_message=error_message,
    )

    duration_s = _duration_seconds(started_at, completed_at)
    duration_label = f"{duration_s:.1f}s" if duration_s is not None else "unknown"
    status_label = "failed" if failed else "done"
    if failed:
        logger.warning(
            f"Full sync task {status_label} · task_run_id={task_run_id} · duration={duration_label} · "
            f"created={created_n} · removed={deleted_n}"
            + (f" · error={error_message}" if error_message else ""),
            extra={"emoji_type": "error"},
        )
    else:
        logger.info(
            f"Full sync task {status_label} · task_run_id={task_run_id} · duration={duration_label} · "
            f"created={created_n} · removed={deleted_n}",
            extra={"emoji_type": "success"},
        )

    trigger = str(summary.get("trigger") or "").strip().lower()
    if not failed and trigger in ("scheduled", "manual"):
        from services.source_of_truth.scheduler import reschedule_after_full_sync_success

        reschedule_after_full_sync_success()


def reconcile_stuck_art_backfill_tasks() -> int:
    """Repair full-sync runs stuck WORKING after art (or all follow-ups) have actually finished."""
    from services.postgres.db import get_session
    from services.postgres.models import ScheduledTaskRun

    session = get_session()
    fixed = 0
    try:
        rows = (
            session.query(ScheduledTaskRun)
            .filter(
                ScheduledTaskRun.task_key == "full_sync",
                ScheduledTaskRun.status == "working",
            )
            .all()
        )
        for row in rows:
            summary = row.summary if isinstance(row.summary, dict) else {}
            art = summary.get("art_backfill") if isinstance(summary.get("art_backfill"), dict) else {}
            batch_total = int(art.get("batch_count", 0))
            batch_done = int(art.get("batches_done", 0))
            run_id = str(art.get("run_id") or "").strip()
            art_status = str(art.get("status") or "").lower()
            batches_complete = batch_total > 0 and batch_done >= batch_total

            if _pending_jobs_for_full_sync_task(int(row.id)):
                continue

            if art_status == "done" and batches_complete:
                if try_complete_full_sync_task_run(int(row.id)):
                    fixed += 1
                continue

            if art_status != "working" or not batches_complete or not run_id:
                continue
            finalize_art_backfill_phase(int(row.id), run_id)
            fixed += 1
    finally:
        session.close()
    if fixed:
        logger.info(
            f"reconciled {fixed} stuck full_sync task run(s) after art backfill",
            extra={"emoji_type": "success"},
        )
    return fixed


def finalize_art_backfill_phase(
    task_run_id: int,
    art_run_id: str,
    *,
    failed: bool = False,
    exclude_job_id: int | None = None,
) -> None:
    summary = _load_summary(task_run_id)
    art = summary.get("art_backfill") if isinstance(summary, dict) and isinstance(summary.get("art_backfill"), dict) else {}
    if str(art.get("run_id") or "") != str(art_run_id):
        return
    now = _utc_now()
    phases = phases_from_summary(summary)
    for phase in phases:
        if phase.get("key") != "art_refresh":
            continue
        started_raw = phase.get("started_at")
        try:
            started = datetime.fromisoformat(str(started_raw).replace("Z", "+00:00"))
        except Exception:
            started = None
        phase["ended_at"] = _iso(now)
        if started:
            phase["duration_seconds"] = _duration_seconds(started, now)
        phase["status"] = "failed" if failed else "done"
        counts = art.get("counts") if isinstance(art.get("counts"), dict) else empty_art_counts()
        phase["metrics"] = art_metrics_from_counts(
            counts,
            batches_done=int(art.get("batches_done", 0)),
            batches_total=int(art.get("batch_count", 0)),
        )
    art["status"] = "failed" if failed else "done"
    summary = {**summary, "art_backfill": art}
    _save_phases(task_run_id, phases, extra=summary)
    try:
        from services.task_live_progress import publish_art_refresh_live

        # Drop ephemeral batch bars from the stored live payload (no further publishes to prune them).
        publish_art_refresh_live(int(task_run_id), replace_active_units=True)
    except Exception as exc:
        logger.debug("art live progress finalize skipped: %s", exc, extra={"emoji_type": "debug"})
    if not try_complete_linked_task_run(task_run_id, failed=failed, exclude_job_id=exclude_job_id):
        logger.info(
            f"task_run_id={task_run_id} art phase closed; task row still open "
            f"(follow-up jobs may remain)",
            extra={"emoji_type": "info"},
        )


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


def _save_phases(task_run_id: int, phases: list[dict[str, Any]], *, extra: dict[str, Any] | None = None, art_backfill: dict | None = None) -> None:
    summary = extra if extra is not None else _load_summary(task_run_id)
    started_raw = summary.get("started_at") if isinstance(summary.get("started_at"), str) else None
    started_at = _utc_now()
    if started_raw:
        try:
            started_at = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
        except Exception:
            pass
    mode = str(summary.get("mode") or "full")
    any_working = any(str(p.get("status") or "").lower() == "working" for p in phases)
    progress = build_progress_from_phases(
        task_run_id=task_run_id,
        mode=mode,
        started_at=started_at,
        phases=phases,
        overall_status="WORKING" if any_working else "DONE",
    )
    payload: dict[str, Any] = {**summary, "phases": phases, "progress": progress}
    if art_backfill is not None:
        payload["art_backfill"] = art_backfill
    # Preserve concurrent ``summary.live`` writers (do not clobber with a stale load).
    payload.pop("live", None)
    update_task_run_summary(task_run_id, payload)
