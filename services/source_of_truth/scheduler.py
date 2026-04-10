import logging
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from apscheduler.schedulers.background import BackgroundScheduler

from core.config import settings
from services.source_of_truth.determiner import run_determination_pass, run_placeholder_link_reconcile
from services.source_of_truth.filesystem import scan_once_if_needed
from services.source_of_truth.calendar_phase import run_calendar_phase
from services.source_of_truth.calendar_date_refresh import run_calendar_date_refresh
from services.source_of_truth.materializer import run_materialization_pass
from services.source_of_truth.status_reconciler import run_status_projection_reconciliation
from services.source_of_truth.sync_runner import run_full_sync


logger = logging.getLogger('services.source_of_truth.scheduler')
_scheduler = None
_pipeline_lock = threading.Lock()


def _run_self_healing_pipeline(run_id: str) -> None:
    """Run reconcile → determine → materialize → calendar → reconcile_statuses, serialized by a process-wide lock.

    If a pipeline run is already in progress (e.g. a concurrent scheduled job),
    the incoming run is skipped rather than blocked — the next interval will catch up.
    """
    if not _pipeline_lock.acquire(blocking=False):
        logger.warning(
            f"Pipeline already running, skipping scheduled run_id={run_id}",
            extra={'emoji_type': 'warning'},
        )
        return
    try:
        scan_result = scan_once_if_needed(run_id)
        if isinstance(scan_result, tuple):
            scan_count, scan_info = scan_result
        else:
            scan_count, scan_info = scan_result, {'reason': 'ok'}

        reconcile_stats = run_placeholder_link_reconcile()
        determination_stats = run_determination_pass()
        materialization_stats = run_materialization_pass()
        calendar_stats = run_calendar_phase()
        status_reconcile_stats = run_status_projection_reconciliation()
        logger.info(
            "Scheduled self-heal completed "
            f"run_id={run_id} scan_count={scan_count} scan_info={scan_info} "
            f"reconcile={reconcile_stats} determination={determination_stats} "
            f"materialization={materialization_stats} calendar={calendar_stats} "
            f"status_reconcile={status_reconcile_stats}"
        )
    finally:
        _pipeline_lock.release()



def _start_interval(interval_hours, target, label, disable_hint: str = 'FULL_SYNC_INTERVAL_HOURS'):
    if interval_hours <= 0:
        logger.info(f"{label} scheduler disabled ({disable_hint} <= 0)")
        return

    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler()

    safe_interval_hours = max(1, int(interval_hours))
    if safe_interval_hours != int(interval_hours):
        logger.warning(
            f"{label} interval clamped from {interval_hours}h to {safe_interval_hours}h (minimum is 1h)"
        )

    next_run = datetime.now(timezone.utc) + timedelta(hours=safe_interval_hours)
    job_id = f"source_of_truth:{label.lower().replace(' ', '_').replace('(', '').replace(')', '')}"
    try:
        _scheduler.add_job(
            target,
            'interval',
            hours=safe_interval_hours,
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            next_run_time=next_run,
        )
        logger.info(f"{label} scheduler started with interval: every {safe_interval_hours}h")
    except Exception:
        logger.exception(f"Failed to start scheduler for {label}")


def _run_all_syncs():
    """Run all configured ARR syncs sequentially then execute the shared self-healing pipeline once.

    Only syncs for ARR instances that have both a URL and API key configured are run,
    so unconfigured instances (e.g. 4K when no 4K Radarr/Sonarr is set up) are silently skipped.
    """
    run_id = f'scheduled:all:{uuid4()}'
    try:
        for instance in (getattr(settings, 'configured_arr_instances', []) or []):
            arr_type = str(instance.get('arr_type') or '').strip().lower()
            if arr_type not in {'radarr', 'sonarr'}:
                continue
            run_full_sync(
                dry_run=False,
                batch_size=50,
                types=('movie',) if arr_type == 'radarr' else ('series',),
                instance_key=str(instance.get('instance_key') or '').strip().lower() or None,
            )
        _run_self_healing_pipeline(run_id)
    except Exception:
        logger.exception('Scheduled full-sync failed')


def _run_calendar_only_syncs():
    """Run lightweight date refresh + calendar lifecycle/status reconciliation without full ARR sync."""
    run_id = f'scheduled:calendar:{uuid4()}'
    try:
        if not _pipeline_lock.acquire(blocking=False):
            logger.warning(
                f"Pipeline already running, skipping calendar-only run_id={run_id}",
                extra={'emoji_type': 'warning'},
            )
            return
        try:
            date_refresh_stats = run_calendar_date_refresh()
            determination_stats = run_determination_pass()
            materialization_stats = run_materialization_pass()
            calendar_stats = run_calendar_phase()
            status_reconcile_stats = run_status_projection_reconciliation()
            logger.info(
                "Scheduled calendar-only sync completed "
                f"run_id={run_id} date_refresh={date_refresh_stats} "
                f"determination={determination_stats} materialization={materialization_stats} "
                f"calendar={calendar_stats} status_reconcile={status_reconcile_stats}"
            )
        finally:
            _pipeline_lock.release()
    except Exception:
        logger.exception('Scheduled calendar-only sync failed')


def schedule_all_syncs():
    """Schedule a single interval-based source-of-truth fullsync job covering all configured ARR services."""
    interval_hours = getattr(settings, 'FULL_SYNC_INTERVAL_HOURS', 0)
    _start_interval(interval_hours, _run_all_syncs, 'All ARRs', 'FULL_SYNC_INTERVAL_HOURS')

    calendar_interval_hours = getattr(settings, 'CALENDAR_SYNC_INTERVAL_HOURS', 0)
    _start_interval(
        calendar_interval_hours,
        _run_calendar_only_syncs,
        'Calendar Date Refresh',
        'CALENDAR_SYNC_INTERVAL_HOURS',
    )

    global _scheduler
    if _scheduler and not _scheduler.running:
        _scheduler.start()
