from dataclasses import dataclass
import time
import uuid

from core.config import settings
from core.logger import logger
from services.source_of_truth.calendar_phase import run_calendar_phase
from services.source_of_truth.calendar_date_refresh import run_calendar_date_refresh
from services.source_of_truth.determiner import run_determination_pass, run_placeholder_link_reconcile
from services.source_of_truth.filesystem import scan_once_if_needed
from services.source_of_truth.materializer import run_materialization_pass
from services.source_of_truth.primer import run_primer_phase
from services.source_of_truth.status_reconciler import run_status_projection_reconciliation
from services.source_of_truth.sync_runner import run_full_sync


@dataclass
class FullSyncRunRef:
    run_id: str


def _create_run(content_type: str, is_4k: bool = False, run_note: str | None = None) -> FullSyncRunRef:
    suffix = '4k' if is_4k else 'standard'
    run_id = f'fullsync:{content_type}:{suffix}:{uuid.uuid4()}'
    sync_types = ('movie',) if content_type == 'movie' else ('series',)
    logger.info(
        f"Starting startup {content_type} fullsync ({suffix}) run {run_id} ({run_note or 'no note'})",
        extra={'emoji_type': 'gear'},
    )
    run_full_sync(dry_run=False, types=sync_types, is_4k=is_4k)
    logger.info(f"Finished startup {content_type} fullsync ({suffix}) run {run_id}", extra={'emoji_type': 'success'})
    return FullSyncRunRef(run_id=run_id)


def capture_movies_fullsync_and_create_run(run_note: str | None = None) -> FullSyncRunRef:
    return _create_run('movie', is_4k=False, run_note=run_note)


def capture_series_fullsync_and_create_run(run_note: str | None = None) -> FullSyncRunRef:
    return _create_run('series', is_4k=False, run_note=run_note)


def run_startup_source_of_truth() -> dict:
    """Execute configured startup fullsyncs, filesystem scan, determine, and materialize."""
    run_ids: list[str] = []

    if getattr(settings, 'RADARR_SYNC_ON_STARTUP', False):
        run_ids.append(capture_movies_fullsync_and_create_run('startup movie fullsync').run_id)

    if getattr(settings, 'SONARR_SYNC_ON_STARTUP', False):
        run_ids.append(capture_series_fullsync_and_create_run('startup tv fullsync').run_id)

    if getattr(settings, 'RADARR_4K_SYNC_ON_STARTUP', False):
        run = _create_run('movie', is_4k=True, run_note='startup movie 4k fullsync')
        run_ids.append(run.run_id)

    if getattr(settings, 'SONARR_4K_SYNC_ON_STARTUP', False):
        run = _create_run('series', is_4k=True, run_note='startup tv 4k fullsync')
        run_ids.append(run.run_id)

    scan_run_id = run_ids[0] if run_ids else f'fullsync:startup:{int(time.time())}'
    scan_result = scan_once_if_needed(scan_run_id)
    if isinstance(scan_result, tuple):
        scan_count, scan_info = scan_result
    else:
        scan_count, scan_info = scan_result, {'reason': 'ok'}

    reconcile_stats = run_placeholder_link_reconcile()
    calendar_date_refresh_stats = run_calendar_date_refresh()
    determination_stats = run_determination_pass()
    primer_stats = run_primer_phase()
    materialization_stats = run_materialization_pass()
    calendar_stats = run_calendar_phase()
    status_reconcile_stats = run_status_projection_reconciliation()

    return {
        'ran': True,
        'fullsync_ran': bool(run_ids),
        'run_ids': run_ids,
        'scan': {
            'count': scan_count,
            'info': scan_info,
        },
        'reconcile': reconcile_stats,
        'calendar_date_refresh': calendar_date_refresh_stats,
        'determination': determination_stats,
        'primer': primer_stats,
        'materialization': materialization_stats,
        'calendar': calendar_stats,
        'status_reconcile': status_reconcile_stats,
        'primer_cleanup': {},
    }
