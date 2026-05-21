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
    begin_full_sync,
    end_full_sync,
    release_pipeline,
    should_skip_lite_or_calendar,
    try_acquire_pipeline,
)
from services.source_of_truth.sync_runner import run_full_sync
from services.task_run_history import (
    begin_task_run,
    finish_task_run,
    record_skipped_task_run,
    update_task_run_summary,
)

TaskTrigger = Literal["scheduled", "manual", "startup"]


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
    run_id = f"full_sync:{trigger}:{uuid4()}"
    task_run_id = begin_task_run(task_key="full_sync", trigger=trigger)
    started_at = datetime.now(timezone.utc)
    begin_full_sync()
    result: dict[str, Any] = {"task_run_id": task_run_id, "run_id": run_id}
    try:
        sync_stats: dict[str, Any] = {}
        for instance in _configured_arr_instances():
            arr_type = str(instance.get("arr_type") or "").strip().lower()
            if arr_type not in {"radarr", "sonarr"}:
                continue
            key = run_full_sync(
                dry_run=False,
                batch_size=50,
                types=("movie",) if arr_type == "radarr" else ("series",),
                instance_key=str(instance.get("instance_key") or "").strip().lower() or None,
            )
            sync_stats[str(instance.get("instance_key") or arr_type)] = key

        result["arr_sync"] = sync_stats
        _record_progress(
            task_run_id=task_run_id,
            mode="full",
            started_at=started_at,
            current_phase="fs_scan",
            startup_sync_stats=sync_stats,
            determination_stats=None,
            materialization_stats=None,
        )

        acquire_pipeline_blocking()
        try:
            pipeline_stats = _run_self_healing_pipeline_inner(run_id, include_calendar_date_refresh=True)
            result["pipeline"] = pipeline_stats
        finally:
            release_pipeline()

        _record_progress(
            task_run_id=task_run_id,
            mode="full",
            started_at=started_at,
            current_phase="complete",
            startup_sync_stats=sync_stats,
            determination_stats=pipeline_stats.get("determination"),
            materialization_stats=pipeline_stats.get("materialization"),
            completed_at=datetime.now(timezone.utc),
        )
        completed_at = datetime.now(timezone.utc)
        finish_task_run(task_run_id, status="done", summary=result)
        _apply_schedule_after_success("full_sync", trigger)
        try:
            from services.source_of_truth.placeholder_art_reconciler import enqueue_placeholder_art_backfill_all

            art_out = enqueue_placeholder_art_backfill_all(source=f"full_sync:{trigger}")
            result["placeholder_art_backfill"] = art_out
        except Exception as art_exc:
            logger.warning(
                f"Full sync art backfill enqueue failed: {art_exc}",
                extra={"emoji_type": "warning"},
            )
        return result
    except Exception as exc:
        logger.exception("Full sync failed run_id=%s", run_id)
        _record_progress(
            task_run_id=task_run_id,
            mode="full",
            started_at=started_at,
            current_phase="failed",
            startup_sync_stats=result.get("arr_sync"),
            determination_stats=None,
            materialization_stats=None,
            completed_at=datetime.now(timezone.utc),
            failed=True,
            error_message=str(exc),
        )
        finish_task_run(task_run_id, status="failed", summary=result, error_message=str(exc))
        raise
    finally:
        end_full_sync()


def run_lite_sync(*, trigger: TaskTrigger = "scheduled", task_run_id: int | None = None) -> dict[str, Any]:
    """Catalog diff + targeted ARR sync, calendar refresh, scoped determine/materialize, calendar phase."""
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

    own_run = task_run_id is None
    if own_run:
        task_run_id = begin_task_run(task_key="lite_sync", trigger=trigger)

    started_at = datetime.now(timezone.utc)
    result: dict[str, Any] = {"task_run_id": task_run_id}
    try:
        instances = _configured_arr_instances()
        _record_progress(
            task_run_id=task_run_id,
            mode="lite",
            started_at=started_at,
            current_phase="discovery",
            startup_sync_stats=None,
            determination_stats=None,
            materialization_stats=None,
        )

        from services.source_of_truth.lite_reconcile import (
            run_lite_startup_reconciliation_pre_discovery,
            run_specials_backfill_if_pending,
        )

        specials_backfill_stats = run_specials_backfill_if_pending(instances=instances)
        pre_movie_ids, pre_episode_ids, recon_stats = run_lite_startup_reconciliation_pre_discovery()
        startup_sync_stats = _run_startup_lite_snapshot_for_instances(
            instances,
            seed_movie_row_ids=set(pre_movie_ids),
            seed_episode_row_ids=set(pre_episode_ids),
            lite_reconciliation_pre=recon_stats,
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
        result["placeholder_truth"] = _refresh_placeholder_presence_for_entities(
            movie_row_ids=movie_row_ids,
            episode_row_ids=episode_row_ids,
        )

        phase_started = time.monotonic()
        result["calendar_date_refresh"] = run_calendar_date_refresh()
        logger.info(
            "Lite sync calendar_date_refresh elapsed_s=%.1f",
            time.monotonic() - phase_started,
            extra={"emoji_type": "info"},
        )

        _record_progress(
            task_run_id=task_run_id,
            mode="lite",
            started_at=started_at,
            current_phase="determination",
            startup_sync_stats=startup_sync_stats,
            determination_stats=None,
            materialization_stats=None,
        )

        if movie_row_ids or episode_row_ids:
            determination_stats = run_determination_for_entities(
                movie_ids=movie_row_ids,
                episode_ids=episode_row_ids,
            )
        else:
            determination_stats = {
                "skipped": True,
                "reason": "no_lite_changes_detected",
            }
        result["determination"] = determination_stats

        _record_progress(
            task_run_id=task_run_id,
            mode="lite",
            started_at=started_at,
            current_phase="materialization",
            startup_sync_stats=startup_sync_stats,
            determination_stats=determination_stats,
            materialization_stats=None,
        )

        if movie_row_ids or episode_row_ids:
            materialization_stats = run_materialization_for_entities(
                movie_ids=movie_row_ids,
                episode_ids=episode_row_ids,
                observation_source="scheduled_lite_materialization",
            )
        else:
            materialization_stats = {"skipped": True, "reason": "no_lite_changes_detected"}
        result["materialization"] = materialization_stats

        _record_progress(
            task_run_id=task_run_id,
            mode="lite",
            started_at=started_at,
            current_phase="complete",
            startup_sync_stats=startup_sync_stats,
            determination_stats=determination_stats,
            materialization_stats=materialization_stats,
            completed_at=datetime.now(timezone.utc),
        )

        result["calendar"] = run_calendar_phase()
        result["orphan_placeholders"] = run_orphan_placeholder_cleanup()

        try:
            from services.source_of_truth.placeholder_art_reconciler import enqueue_placeholder_art_refresh

            ph_ids = _placeholder_ids_for_entity_rows(
                movie_row_ids=movie_row_ids,
                episode_row_ids=episode_row_ids,
            )
            if ph_ids:
                result["placeholder_art_refresh"] = enqueue_placeholder_art_refresh(ph_ids)
        except Exception as art_exc:
            logger.warning(f"Lite sync art refresh enqueue failed: {art_exc}", extra={"emoji_type": "warning"})

        if own_run:
            finish_task_run(task_run_id, status="done", summary=result)
            _apply_schedule_after_success("lite_sync", trigger)
        return result
    except Exception as exc:
        logger.exception("Lite sync failed")
        _record_progress(
            task_run_id=task_run_id,
            mode="lite",
            started_at=started_at,
            current_phase="failed",
            startup_sync_stats=result.get("startup_sync"),
            determination_stats=result.get("determination"),
            materialization_stats=result.get("materialization"),
            completed_at=datetime.now(timezone.utc),
            failed=True,
            error_message=str(exc),
        )
        if own_run:
            finish_task_run(task_run_id, status="failed", summary=result, error_message=str(exc))
        raise
    finally:
        release_pipeline()


def run_calendar_only_maintenance(*, trigger: TaskTrigger = "manual") -> dict[str, Any]:
    """Date refresh + calendar phase only (manual from Tasks UI)."""
    if should_skip_lite_or_calendar():
        skip_id = record_skipped_task_run(
            task_key="calendar_only",
            trigger=trigger,
            skip_reason="full_sync_in_progress",
        )
        return {"skipped": True, "reason": "full_sync_in_progress", "task_run_id": skip_id}

    if not try_acquire_pipeline():
        skip_id = record_skipped_task_run(
            task_key="calendar_only",
            trigger=trigger,
            skip_reason="pipeline_busy",
        )
        return {"skipped": True, "reason": "pipeline_busy", "task_run_id": skip_id}

    task_run_id = begin_task_run(task_key="calendar_only", trigger=trigger)
    result: dict[str, Any] = {"task_run_id": task_run_id}
    try:
        result["calendar_date_refresh"] = run_calendar_date_refresh()
        result["calendar"] = run_calendar_phase()
        result["orphan_placeholders"] = run_orphan_placeholder_cleanup()
        finish_task_run(task_run_id, status="done", summary=result)
        return result
    except Exception as exc:
        finish_task_run(task_run_id, status="failed", summary=result, error_message=str(exc))
        raise
    finally:
        release_pipeline()
