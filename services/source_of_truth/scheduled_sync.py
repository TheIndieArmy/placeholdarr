"""Scheduled and manual full/lite/calendar maintenance entry points."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from core.config import settings
from core.logger import logger
from services.source_of_truth.calendar_date_refresh import run_calendar_date_refresh
from services.source_of_truth.calendar_phase import run_calendar_phase
from services.source_of_truth.determiner import (
    run_determination_for_entities,
    run_determination_pass,
    run_placeholder_link_reconcile,
)
from services.source_of_truth.filesystem import scan_once_if_needed
from services.source_of_truth.materializer import run_materialization_for_entities, run_materialization_pass
from services.source_of_truth.placeholder_cleanup import run_orphan_placeholder_cleanup
from services.source_of_truth.startup import (
    _configured_arr_instances,
    _refresh_placeholder_presence_for_entities,
    _run_startup_lite_snapshot_for_instances,
)
from services.source_of_truth.sync_coordinator import (
    acquire_pipeline_blocking,
    end_full_sync,
    is_full_sync_active,
    release_pipeline,
    should_skip_lite_or_calendar,
    try_acquire_pipeline,
    try_begin_full_sync,
)
from services.source_of_truth.sync_runner import run_full_sync
from services.task_run_history import (
    begin_task_run,
    finish_task_run,
    record_skipped_task_run,
    update_task_run_summary,
)

_PG_DEADLOCK = "40P01"


def _is_deadlock_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        pgcode = getattr(getattr(current, "orig", None), "pgcode", None)
        if str(pgcode or "") == _PG_DEADLOCK:
            return True
        current = current.__cause__ or current.__context__
    return False


def _run_full_sync_with_deadlock_retry(**kwargs: Any) -> Any:
    try:
        return run_full_sync(**kwargs)
    except Exception as exc:
        if not _is_deadlock_error(exc):
            raise
        logger.warning(
            "Full sync hit a deadlock; retrying once: %s",
            exc,
            extra={"emoji_type": "warning"},
        )
        time.sleep(2)
        return run_full_sync(**kwargs)


TaskTrigger = Literal["scheduled", "manual", "startup"]


def _arr_instance_label(inst: dict[str, Any]) -> str:
    arr_type = str(inst.get("arr_type") or "").strip().lower()
    instance_key = str(inst.get("instance_key") or "").strip().lower()
    kind = "Radarr" if arr_type == "radarr" else "Sonarr" if arr_type == "sonarr" else "ARR"
    if not instance_key or instance_key == arr_type:
        return kind
    return f"{kind} · {instance_key}"


def _placeholder_ids_for_entity_rows(
    *,
    movie_row_ids: list[int] | None = None,
    episode_row_ids: list[int] | None = None,
) -> list[int]:
    from services.postgres.db import get_session
    from services.postgres.models import Placeholder

    mids = [int(x) for x in (movie_row_ids or []) if x is not None]
    eids = [int(x) for x in (episode_row_ids or []) if x is not None]
    if not mids and not eids:
        return []

    session = get_session()
    try:
        q = session.query(Placeholder.id).filter(Placeholder.has_placeholder == True)  # noqa: E712
        if mids and eids:
            q = q.filter((Placeholder.movie_id.in_(mids)) | (Placeholder.episode_id.in_(eids)))
        elif mids:
            q = q.filter(Placeholder.movie_id.in_(mids))
        else:
            q = q.filter(Placeholder.episode_id.in_(eids))
        return [int(r[0]) for r in q.all() if r and r[0] is not None]
    finally:
        session.close()


def _apply_schedule_after_success(task_key: str, trigger: TaskTrigger) -> None:
    """Manual and scheduled completions advance the persisted next-run clock."""
    if trigger not in ("scheduled", "manual"):
        return
    from services.source_of_truth.scheduler import reschedule_task_after_completion

    reschedule_task_after_completion(task_key)


def record_task_sync_progress(
    *,
    task_run_id: int,
    mode: str,
    started_at: datetime,
    current_phase: str,
    startup_sync_stats: dict | None,
    determination_stats: dict | None,
    materialization_stats: dict | None,
    completed_at: datetime | None = None,
    failed: bool = False,
    error_message: str | None = None,
) -> None:
    """Write phased progress into ``scheduled_task_run.summary`` for Tasks queue UI."""
    _record_progress(
        task_run_id=task_run_id,
        mode=mode,
        started_at=started_at,
        current_phase=current_phase,
        startup_sync_stats=startup_sync_stats,
        determination_stats=determination_stats,
        materialization_stats=materialization_stats,
        completed_at=completed_at,
        failed=failed,
        error_message=error_message,
    )


def _record_progress(
    *,
    task_run_id: int | None,
    mode: str,
    started_at: datetime,
    current_phase: str,
    startup_sync_stats: dict | None,
    determination_stats: dict | None,
    materialization_stats: dict | None,
    completed_at: datetime | None = None,
    failed: bool = False,
    error_message: str | None = None,
) -> None:
    from services.startup_sync_activity import record_startup_sync_progress

    if task_run_id is None:
        record_startup_sync_progress(
            mode=mode,
            started_at=started_at,
            current_phase=current_phase,
            startup_sync_stats=startup_sync_stats,
            determination_stats=determination_stats,
            materialization_stats=materialization_stats,
            completed_at=completed_at,
            failed=failed,
            error_message=error_message,
        )
        return

    from services.startup_sync_activity import _build_startup_sync_row

    row = _build_startup_sync_row(
        mode=mode,
        started_at=started_at,
        current_phase=current_phase,
        startup_sync_stats=startup_sync_stats,
        determination_stats=determination_stats,
        materialization_stats=materialization_stats,
        completed_at=completed_at,
        failed=failed,
        error_message=error_message,
    )
    update_task_run_summary(task_run_id, {"progress": row})


def _run_self_healing_pipeline_inner(run_id: str, *, include_calendar_date_refresh: bool) -> dict[str, Any]:
    stats: dict[str, Any] = {"run_id": run_id}
    if include_calendar_date_refresh:
        stats["calendar_date_refresh"] = run_calendar_date_refresh()

    scan_result = scan_once_if_needed(run_id, prefer_incremental=False)
    if isinstance(scan_result, tuple):
        scan_count, scan_info = scan_result
    else:
        scan_count, scan_info = scan_result, {"reason": "ok"}
    stats["scan"] = {"count": scan_count, "info": scan_info}

    stats["reconcile"] = run_placeholder_link_reconcile()
    stats["determination"] = run_determination_pass()
    stats["materialization"] = run_materialization_pass()
    stats["calendar"] = run_calendar_phase()
    stats["orphan_placeholders"] = run_orphan_placeholder_cleanup()
    return stats


def run_scheduled_full_sync(*, trigger: TaskTrigger = "scheduled") -> dict[str, Any]:
    """Full ARR sync for all instances, then blocking self-heal pipeline."""
    if is_full_sync_active():
        skip_id = record_skipped_task_run(
            task_key="full_sync",
            trigger=trigger,
            skip_reason="full_sync_in_progress",
        )
        logger.info(
            "Full sync skipped: another full sync is still running",
            extra={"emoji_type": "info"},
        )
        return {"skipped": True, "reason": "full_sync_in_progress", "task_run_id": skip_id}

    run_id = f"full_sync:{trigger}:{uuid4()}"
    task_run_id = begin_task_run(task_key="full_sync", trigger=trigger)
    started_at = datetime.now(timezone.utc)
    from services.task_run_phases import (
        TaskRunPhaseTracker,
        _load_summary,
        mark_follow_up_phase_skipped,
        metrics_from_arr_sync,
        metrics_from_pipeline,
        phases_from_summary,
        refresh_full_sync_task_progress,
        try_complete_full_sync_task_run,
    )

    from services.task_run_cancel import (
        TaskRunCancelled,
        ensure_task_run_not_cancelled,
        task_run_cancel_scope,
    )

    phases = TaskRunPhaseTracker(task_run_id, started_at=started_at)
    if not try_begin_full_sync():
        finish_task_run(
            task_run_id,
            status="skipped",
            summary={"skipped": True, "reason": "full_sync_in_progress"},
            skip_reason="full_sync_in_progress",
        )
        logger.info(
            "Full sync skipped after task open: another full sync claimed the lock",
            extra={"emoji_type": "info"},
        )
        return {"skipped": True, "reason": "full_sync_in_progress", "task_run_id": task_run_id}
    result: dict[str, Any] = {
        "task_run_id": task_run_id,
        "run_id": run_id,
        "mode": "full",
        "trigger": trigger,
        "task_run_started_at": started_at.isoformat(),
    }
    try:
        from services.task_live_progress import FULL_SYNC_CORE_PHASES, seed_sync_live_phases

        seed_sync_live_phases(
            task_run_id,
            kind="full_sync",
            label="Full sync",
            phase_defs=FULL_SYNC_CORE_PHASES,
        )
    except Exception as live_exc:
        logger.debug("full_sync live seed skipped: %s", live_exc, extra={"emoji_type": "debug"})
    try:
      with task_run_cancel_scope(task_run_id):
        phases.begin("arr_sync", "ARR catalog sync")
        sync_stats: dict[str, Any] = {}

        arr_instances = [
            inst
            for inst in _configured_arr_instances()
            if str(inst.get("arr_type") or "").strip().lower() in {"radarr", "sonarr"}
        ]
        arr_units: list[dict[str, Any]] = [
            {
                "key": str(inst.get("instance_key") or inst.get("arr_type") or f"arr_{idx}").strip().lower(),
                "label": _arr_instance_label(inst),
                "done": 0,
                "total": 0,
                "status": "pending",
            }
            for idx, inst in enumerate(arr_instances)
        ]
        if arr_units:
            phases.progress("arr_sync", units=list(arr_units), unit_label="titles")

        for inst_idx, instance in enumerate(arr_instances):
            ensure_task_run_not_cancelled(task_run_id)
            arr_type = str(instance.get("arr_type") or "").strip().lower()
            arr_units[inst_idx] = {
                **arr_units[inst_idx],
                "status": "working",
                "done": 0,
                "total": int(arr_units[inst_idx].get("total") or 0),
            }
            phases.progress("arr_sync", units=list(arr_units), unit_label="titles")

            def _on_arr_progress(done: int, total: int, *, _idx: int = inst_idx) -> None:
                arr_units[_idx] = {
                    **arr_units[_idx],
                    "status": "working",
                    "done": max(0, int(done or 0)),
                    "total": max(0, int(total or 0)),
                }
                phases.progress("arr_sync", units=list(arr_units), unit_label="titles")

            key = _run_full_sync_with_deadlock_retry(
                dry_run=False,
                batch_size=50,
                types=("movie",) if arr_type == "radarr" else ("series",),
                instance_key=str(instance.get("instance_key") or "").strip().lower() or None,
                run_template_backfill=False,
                on_progress=_on_arr_progress,
            )
            sync_stats[str(instance.get("instance_key") or arr_type)] = key
            try:
                final_done = int(
                    (key or {}).get("movies_seen")
                    or (key or {}).get("movies_updated")
                    or 0
                ) + int(
                    (key or {}).get("series_seen")
                    or (key or {}).get("series_updated")
                    or 0
                )
            except Exception:
                final_done = int(arr_units[inst_idx].get("done") or 0)
            final_total = max(final_done, int(arr_units[inst_idx].get("total") or 0))
            arr_units[inst_idx] = {
                **arr_units[inst_idx],
                "status": "done",
                "done": final_total if final_total > 0 else final_done,
                "total": final_total if final_total > 0 else final_done,
            }
            phases.progress("arr_sync", units=list(arr_units), unit_label="titles")

        result["arr_sync"] = sync_stats
        phases.end(
            "arr_sync",
            metrics=metrics_from_arr_sync(sync_stats),
            units=list(arr_units) if arr_units else None,
            unit_label="titles",
        )
        ensure_task_run_not_cancelled(task_run_id)

        pipeline_stats: dict[str, Any] = {"run_id": run_id}
        acquire_pipeline_blocking()
        try:
            ensure_task_run_not_cancelled(task_run_id)
            pipeline_stats["calendar_date_refresh"] = run_calendar_date_refresh()

            ensure_task_run_not_cancelled(task_run_id)
            phases.begin("fs_scan", "Filesystem scan")

            def _on_fs_progress(files_seen: int, _media_candidates: int) -> None:
                phases.progress(
                    "fs_scan",
                    done=int(files_seen or 0),
                    total=0,
                    unit_label="files",
                )

            scan_result = scan_once_if_needed(
                run_id,
                prefer_incremental=False,
                on_progress=_on_fs_progress,
            )
            if isinstance(scan_result, tuple):
                scan_count, scan_info = scan_result
            else:
                scan_count, scan_info = scan_result, {"reason": "ok"}
            pipeline_stats["scan"] = {"count": scan_count, "info": scan_info}
            pipeline_stats["reconcile"] = run_placeholder_link_reconcile()
            phases.end("fs_scan", metrics=metrics_from_pipeline(pipeline_stats).get("fs_scan"))

            ensure_task_run_not_cancelled(task_run_id)
            phases.begin("determination", "Status determination")

            def _on_det_progress(done: int, total: int) -> None:
                phases.progress(
                    "determination",
                    done=int(done or 0),
                    total=int(total or 0),
                    unit_label="rows",
                )

            pipeline_stats["determination"] = run_determination_pass(on_progress=_on_det_progress)
            phases.end("determination", metrics=metrics_from_pipeline(pipeline_stats).get("determination"))

            ensure_task_run_not_cancelled(task_run_id)
            phases.begin("materialization", "Placeholder materialization")
            pipeline_stats["materialization"] = run_materialization_pass()
            from services.source_of_truth.tag_placeholder_policy import (
                merge_tag_policy_materialization_into,
                sum_tag_policy_counts_from_arr_instance_stats,
            )

            pipeline_stats["materialization"] = merge_tag_policy_materialization_into(
                pipeline_stats.get("materialization") if isinstance(pipeline_stats.get("materialization"), dict) else {},
                sum_tag_policy_counts_from_arr_instance_stats(sync_stats),
            )
            phases.end("materialization", metrics=metrics_from_pipeline(pipeline_stats).get("materialization"))

            try:
                from services.source_of_truth.tv_density_rematerialize import (
                    clear_tv_density_rematerialize_pending,
                    is_tv_density_rematerialize_pending,
                )

                if is_tv_density_rematerialize_pending():
                    clear_tv_density_rematerialize_pending()
                    pipeline_stats["tv_density_rematerialize_pending_cleared"] = True
                    logger.info(
                        "Cleared TV density rematerialize pending after full sync materialization",
                        extra={"emoji_type": "info"},
                    )
            except Exception as dens_exc:
                logger.warning(
                    f"Could not clear TV density rematerialize pending after full sync: {dens_exc}",
                    extra={"emoji_type": "warning"},
                )

            ensure_task_run_not_cancelled(task_run_id)
            phases.begin("catalog_gc", "Catalog cleanup")
            # Seed count cards immediately so the phase is expandable while working.
            phases.progress(
                "catalog_gc",
                done=0,
                total=0,
                unit_label="rows",
                metrics=metrics_from_pipeline({"catalog_gc": {}}).get("catalog_gc"),
            )

            def _on_catalog_gc_progress(done: int, total: int, stats: dict | None = None) -> None:
                phases.progress(
                    "catalog_gc",
                    done=int(done or 0),
                    total=int(total or 0),
                    unit_label="rows",
                    metrics=metrics_from_pipeline({"catalog_gc": stats or {}}).get("catalog_gc"),
                )

            from services.source_of_truth.catalog_tombstone_purge import (
                purge_cleaned_deleted_catalog_batch,
            )

            pipeline_stats["catalog_gc"] = purge_cleaned_deleted_catalog_batch(
                on_progress=_on_catalog_gc_progress,
            )
            phases.end(
                "catalog_gc",
                metrics=metrics_from_pipeline(pipeline_stats).get("catalog_gc"),
            )

            ensure_task_run_not_cancelled(task_run_id)
            phases.begin("calendar", "Calendar status")
            phases.progress(
                "calendar",
                metrics=metrics_from_pipeline({"calendar": {}}).get("calendar"),
            )
            pipeline_stats["calendar"] = run_calendar_phase()
            pipeline_stats["orphan_placeholders"] = run_orphan_placeholder_cleanup()
            phases.end("calendar", metrics=metrics_from_pipeline(pipeline_stats).get("calendar"))

            result["pipeline"] = pipeline_stats
        finally:
            release_pipeline()

        ensure_task_run_not_cancelled(task_run_id)
        sync_completed_at = datetime.now(timezone.utc)
        result["sync_completed_at"] = sync_completed_at.isoformat()
        mat = pipeline_stats.get("materialization") if isinstance(pipeline_stats.get("materialization"), dict) else {}
        created_n = int(mat.get("created") or 0)
        deleted_n = int(mat.get("deleted") or 0)
        phase_list = phases.phases()
        result["phases"] = phase_list
        update_task_run_summary(task_run_id, result)

        follow_ups_started = False
        refresh_out: dict[str, Any] | None = None
        try:
            from services.source_of_truth.placeholder_refresh import run_placeholder_refresh_if_pending

            refresh_out = run_placeholder_refresh_if_pending(
                source=f"full_sync:{trigger}",
                task_run_id=task_run_id,
            )
            result["placeholder_refresh"] = refresh_out
            update_task_run_summary(task_run_id, {"placeholder_refresh": refresh_out})
            nfo_out = refresh_out.get("nfo_backfill") if isinstance(refresh_out.get("nfo_backfill"), dict) else None
            art_out = refresh_out.get("art_backfill") if isinstance(refresh_out.get("art_backfill"), dict) else None
            # Only surface follow-up phases when something was requested/enqueued.
            # not_requested skips are omitted so Tasks does not show permanent SKIPPED cards.
            nfo_enqueued = bool(nfo_out and nfo_out.get("enqueued"))
            art_enqueued = bool(art_out and art_out.get("enqueued"))
            follow_ups_started = bool(nfo_enqueued or art_enqueued)
        except Exception as refresh_exc:
            logger.warning(
                f"Full sync placeholder refresh enqueue failed: {refresh_exc}",
                extra={"emoji_type": "warning"},
            )
            mark_follow_up_phase_skipped(
                task_run_id,
                "metadata_refresh",
                "Metadata refresh",
                reason="enqueue_failed",
            )
            mark_follow_up_phase_skipped(
                task_run_id,
                "art_refresh",
                "Art refresh",
                reason="enqueue_failed",
            )

        try:
            from services.source_of_truth.poster_language_job import enqueue_poster_language_resolve_stale

            result["poster_language_resolve"] = enqueue_poster_language_resolve_stale(
                source=f"full_sync:{trigger}"
            )
        except Exception as lang_exc:
            logger.warning(
                f"Full sync poster language resolve enqueue failed: {lang_exc}",
                extra={"emoji_type": "warning"},
            )

        phase_list = phases_from_summary(_load_summary(task_run_id)) or phase_list
        if follow_ups_started:
            refresh_full_sync_task_progress(task_run_id, phases=phase_list, overall_status="WORKING")
        else:
            try_complete_full_sync_task_run(task_run_id)
        return result
    except TaskRunCancelled:
        logger.warning(
            "Full sync stopped by user run_id=%s task_run_id=%s",
            run_id,
            task_run_id,
            extra={"emoji_type": "warning"},
        )
        for key in list(phases._open.keys()):
            phases.end(key, failed=True, status="failed")
        result["cancelled"] = True
        result["phases"] = phases.phases()
        update_task_run_summary(task_run_id, result)
        return result
    except Exception as exc:
        logger.exception("Full sync failed run_id=%s", run_id)
        for key in list(phases._open.keys()):
            phases.end(key, failed=True, status="failed")
        try_complete_full_sync_task_run(
            task_run_id,
            failed=True,
            error_message=str(exc),
        )
        raise
    finally:
        end_full_sync()


def run_lite_sync(*, trigger: TaskTrigger = "scheduled", task_run_id: int | None = None) -> dict[str, Any]:
    """Catalog diff + targeted ARR sync, scoped determine/materialize (calendar is a separate task)."""
    if should_skip_lite_or_calendar():
        skip_id = record_skipped_task_run(
            task_key="lite_sync",
            trigger=trigger,
            skip_reason="full_sync_in_progress",
        )
        logger.info(
            "Lite sync skipped: full sync in progress",
            extra={"emoji_type": "info"},
        )
        return {"skipped": True, "reason": "full_sync_in_progress", "task_run_id": skip_id}

    if not try_acquire_pipeline():
        skip_id = record_skipped_task_run(
            task_key="lite_sync",
            trigger=trigger,
            skip_reason="pipeline_busy",
        )
        logger.warning("Lite sync skipped: pipeline busy", extra={"emoji_type": "warning"})
        return {"skipped": True, "reason": "pipeline_busy", "task_run_id": skip_id}

    from services.task_run_cancel import (
        TaskRunCancelled,
        ensure_task_run_not_cancelled,
        task_run_cancel_scope,
    )
    from services.task_run_phases import (
        TaskRunPhaseTracker,
        metrics_from_lite_discovery,
        metrics_from_pipeline,
    )

    own_run = task_run_id is None
    if own_run:
        task_run_id = begin_task_run(task_key="lite_sync", trigger=trigger)

    started_at = datetime.now(timezone.utc)
    result: dict[str, Any] = {"task_run_id": task_run_id}
    phases = TaskRunPhaseTracker(int(task_run_id), started_at=started_at)

    try:
        from services.task_live_progress import LITE_SYNC_CORE_PHASES, seed_sync_live_phases

        seed_sync_live_phases(
            int(task_run_id),
            kind="lite_sync",
            label="Lite sync",
            phase_defs=LITE_SYNC_CORE_PHASES,
        )
    except Exception as live_exc:
        logger.debug("lite_sync live seed skipped: %s", live_exc, extra={"emoji_type": "debug"})

    try:
      with task_run_cancel_scope(task_run_id):
        instances = [
            inst
            for inst in _configured_arr_instances()
            if str(inst.get("arr_type") or "").strip().lower() in {"radarr", "sonarr"}
        ]

        phases.begin("discovery", "Catalog discovery")
        arr_units: list[dict[str, Any]] = [
            {
                "key": str(inst.get("instance_key") or inst.get("arr_type") or f"arr_{idx}").strip().lower(),
                "label": _arr_instance_label(inst),
                "done": 0,
                "total": 0,
                "status": "pending",
            }
            for idx, inst in enumerate(instances)
        ]
        if arr_units:
            phases.progress("discovery", units=list(arr_units), unit_label="titles")

        def _on_instance_progress(index: int, status: str, done: int, total: int) -> None:
            if index < 0 or index >= len(arr_units):
                return
            arr_units[index] = {
                **arr_units[index],
                "status": status,
                "done": max(0, int(done or 0)),
                "total": max(0, int(total or 0)),
            }
            phases.progress("discovery", units=list(arr_units), unit_label="titles")

        from services.source_of_truth.lite_reconcile import (
            run_lite_startup_reconciliation_pre_discovery,
            run_specials_backfill_if_pending,
        )

        ensure_task_run_not_cancelled(task_run_id)
        specials_backfill_stats = run_specials_backfill_if_pending(instances=instances)
        ensure_task_run_not_cancelled(task_run_id)
        pre_movie_ids, pre_episode_ids, recon_stats = run_lite_startup_reconciliation_pre_discovery()
        ensure_task_run_not_cancelled(task_run_id)
        startup_sync_stats = _run_startup_lite_snapshot_for_instances(
            instances,
            seed_movie_row_ids=set(pre_movie_ids),
            seed_episode_row_ids=set(pre_episode_ids),
            lite_reconciliation_pre=recon_stats,
            on_instance_progress=_on_instance_progress,
        )
        specials_determination_ids = {
            int(x)
            for x in (specials_backfill_stats.get("determination_episode_row_ids") or [])
            if x is not None
        }
        if specials_determination_ids:
            merged_episode_ids = {
                int(x) for x in (startup_sync_stats.get("episode_row_ids") or []) if x is not None
            }
            merged_episode_ids.update(specials_determination_ids)
            startup_sync_stats["episode_row_ids"] = sorted(merged_episode_ids)
        startup_sync_stats["specials_backfill"] = specials_backfill_stats
        result["startup_sync"] = startup_sync_stats

        movie_row_ids = [int(x) for x in (startup_sync_stats.get("movie_row_ids") or []) if x is not None]
        episode_row_ids = [int(x) for x in (startup_sync_stats.get("episode_row_ids") or []) if x is not None]
        ensure_task_run_not_cancelled(task_run_id)
        result["placeholder_truth"] = _refresh_placeholder_presence_for_entities(
            movie_row_ids=movie_row_ids,
            episode_row_ids=episode_row_ids,
        )

        discovery_metrics = metrics_from_lite_discovery(startup_sync_stats)
        discovery_done = (
            int(startup_sync_stats.get("movies_seen") or 0)
            + int(startup_sync_stats.get("series_seen") or 0)
            + int(startup_sync_stats.get("episodes_seen") or 0)
        )
        phases.end(
            "discovery",
            metrics=discovery_metrics,
            units=list(arr_units) if arr_units else None,
            unit_label="titles",
            done=discovery_done,
            total=discovery_done,
        )

        ensure_task_run_not_cancelled(task_run_id)
        phases.begin("determination", "Status determination")
        if movie_row_ids or episode_row_ids:
            determination_stats = run_determination_for_entities(
                movie_ids=movie_row_ids,
                episode_ids=episode_row_ids,
            )
            det_metrics = metrics_from_pipeline({"determination": determination_stats}).get("determination")
            det_done = int(determination_stats.get("movies_total") or 0) + int(
                determination_stats.get("episodes_total") or 0
            )
            phases.end(
                "determination",
                metrics=det_metrics,
                done=det_done,
                total=det_done,
                unit_label="rows",
            )
        else:
            determination_stats = {
                "skipped": True,
                "reason": "no_lite_changes_detected",
            }
            phases.end(
                "determination",
                status="skipped",
                metrics=metrics_from_pipeline({"determination": {}}).get("determination"),
                done=0,
                total=0,
                unit_label="rows",
            )
        result["determination"] = determination_stats

        ensure_task_run_not_cancelled(task_run_id)
        phases.begin("materialization", "Placeholder materialization")
        if movie_row_ids or episode_row_ids:
            materialization_stats = run_materialization_for_entities(
                movie_ids=movie_row_ids,
                episode_ids=episode_row_ids,
                observation_source="scheduled_lite_materialization",
            )
        else:
            materialization_stats = {"skipped": True, "reason": "no_lite_changes_detected"}
        from services.source_of_truth.tag_placeholder_policy import merge_tag_policy_materialization_into

        materialization_stats = merge_tag_policy_materialization_into(
            materialization_stats,
            startup_sync_stats.get("tag_policies") if isinstance(startup_sync_stats, dict) else None,
        )
        result["materialization"] = materialization_stats
        mat_metrics = metrics_from_pipeline({"materialization": materialization_stats}).get("materialization")
        if materialization_stats.get("skipped"):
            phases.end(
                "materialization",
                status="skipped",
                metrics=mat_metrics,
                done=0,
                total=0,
                unit_label="placeholders",
            )
        else:
            mat_done = (
                int(materialization_stats.get("created") or 0)
                + int(materialization_stats.get("deleted") or 0)
                + int(materialization_stats.get("noop") or 0)
                + int(materialization_stats.get("errors") or 0)
            )
            phases.end(
                "materialization",
                metrics=mat_metrics,
                done=mat_done,
                total=mat_done,
                unit_label="placeholders",
            )

        result["orphan_placeholders"] = run_orphan_placeholder_cleanup()

        try:
            from services.source_of_truth.placeholder_art_reconciler import enqueue_placeholder_art_refresh

            ph_ids = _placeholder_ids_for_entity_rows(
                movie_row_ids=movie_row_ids,
                episode_row_ids=episode_row_ids,
            )
            if ph_ids:
                # Lite art enqueue has no task_run_id; leave art off this run's live (plan v1).
                result["placeholder_art_refresh"] = enqueue_placeholder_art_refresh(ph_ids)
        except Exception as art_exc:
            logger.warning(f"Lite sync art refresh enqueue failed: {art_exc}", extra={"emoji_type": "warning"})

        try:
            from services.source_of_truth.poster_language_job import enqueue_poster_language_resolve_stale

            result["poster_language_resolve"] = enqueue_poster_language_resolve_stale(source=f"lite_sync:{trigger}")
        except Exception as lang_exc:
            logger.warning(
                f"Lite sync poster language resolve enqueue failed: {lang_exc}",
                extra={"emoji_type": "warning"},
            )

        if own_run:
            finish_task_run(task_run_id, status="done", summary=result)
            _apply_schedule_after_success("lite_sync", trigger)
        return result
    except TaskRunCancelled:
        logger.warning(
            "Lite sync stopped by user task_run_id=%s",
            task_run_id,
            extra={"emoji_type": "warning"},
        )
        result["cancelled"] = True
        if own_run:
            # Abandon already marked stopped; keep that outcome.
            update_task_run_summary(int(task_run_id), result)
        return result
    except Exception as exc:
        logger.exception("Lite sync failed")
        try:
            for phase in phases.phases():
                if str(phase.get("status") or "").lower() == "working":
                    phases.end(str(phase.get("key") or "discovery"), failed=True, status="failed")
                    break
        except Exception:
            pass
        if own_run:
            finish_task_run(task_run_id, status="failed", summary=result, error_message=str(exc))
        raise
    finally:
        release_pipeline()


def run_calendar_only_maintenance(*, trigger: TaskTrigger = "manual") -> dict[str, Any]:
    """Date refresh + calendar phase (scheduled calendar sync / manual Tasks run)."""
    if should_skip_lite_or_calendar():
        skip_id = record_skipped_task_run(
            task_key="calendar_sync",
            trigger=trigger,
            skip_reason="full_sync_in_progress",
        )
        return {"skipped": True, "reason": "full_sync_in_progress", "task_run_id": skip_id}

    if not try_acquire_pipeline():
        skip_id = record_skipped_task_run(
            task_key="calendar_sync",
            trigger=trigger,
            skip_reason="pipeline_busy",
        )
        return {"skipped": True, "reason": "pipeline_busy", "task_run_id": skip_id}

    from services.task_run_phases import (
        TaskRunPhaseTracker,
        metrics_from_calendar_dates,
        metrics_from_pipeline,
    )

    task_run_id = begin_task_run(task_key="calendar_sync", trigger=trigger)
    started_at = datetime.now(timezone.utc)
    result: dict[str, Any] = {"task_run_id": task_run_id}
    phases = TaskRunPhaseTracker(int(task_run_id), started_at=started_at)

    try:
        try:
            from services.task_live_progress import CALENDAR_ONLY_PHASES, seed_sync_live_phases

            seed_sync_live_phases(
                int(task_run_id),
                kind="calendar_sync",
                label="Calendar sync",
                phase_defs=CALENDAR_ONLY_PHASES,
            )
        except Exception as live_exc:
            logger.debug("calendar_sync live seed skipped: %s", live_exc, extra={"emoji_type": "debug"})

        phases.begin("calendar_dates", "Calendar dates")
        result["calendar_date_refresh"] = run_calendar_date_refresh()
        calendar_dates = result["calendar_date_refresh"] if isinstance(result["calendar_date_refresh"], dict) else {}
        date_metrics = metrics_from_calendar_dates(calendar_dates)
        # Progress is Arr calendar rows checked (not only rows whose dates changed).
        date_total = int(calendar_dates.get("movie_rows_seen") or 0) + int(
            calendar_dates.get("episode_rows_seen") or 0
        )
        phases.end(
            "calendar_dates",
            metrics=date_metrics,
            done=date_total,
            total=date_total,
            unit_label="rows",
        )

        phases.begin("calendar", "Calendar status")
        result["calendar"] = run_calendar_phase()
        result["orphan_placeholders"] = run_orphan_placeholder_cleanup()
        cal_metrics = metrics_from_pipeline({"calendar": result.get("calendar") or {}}).get("calendar")
        cal_total = 0
        if isinstance(result.get("calendar"), dict):
            cal = result["calendar"]
            cal_total = int(cal.get("scanned") or 0)
            if cal_total <= 0:
                cal_total = int(cal.get("updated") or cal.get("changed") or cal.get("status_applied") or 0)
        phases.end(
            "calendar",
            metrics=cal_metrics,
            done=cal_total,
            total=cal_total,
            unit_label="placeholders",
        )
        finish_task_run(task_run_id, status="done", summary=result)
        _apply_schedule_after_success("calendar_sync", trigger)
        return result
    except Exception as exc:
        try:
            for phase in phases.phases():
                if str(phase.get("status") or "").lower() == "working":
                    phases.end(str(phase.get("key") or "calendar_dates"), failed=True, status="failed")
                    break
        except Exception:
            pass
        finish_task_run(task_run_id, status="failed", summary=result, error_message=str(exc))
        raise
    finally:
        release_pipeline()
