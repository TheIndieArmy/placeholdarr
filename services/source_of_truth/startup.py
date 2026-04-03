from dataclasses import dataclass
import time
import uuid
from datetime import datetime, timezone

from core.config import settings
from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import ArrState
from services.source_of_truth.arr_api import fetch_radarr_history, fetch_sonarr_history
from services.source_of_truth.calendar_phase import run_calendar_phase
from services.source_of_truth.calendar_date_refresh import run_calendar_date_refresh
from services.source_of_truth.determiner import run_determination_pass, run_placeholder_link_reconcile
from services.source_of_truth.filesystem import scan_once_if_needed
from services.source_of_truth.materializer import run_materialization_pass
from services.source_of_truth.primer import run_primer_phase
from services.source_of_truth.status_reconciler import run_status_projection_reconciliation
from services.source_of_truth.sync_runner import run_full_sync, sync_radarr_movies_by_ids, sync_sonarr_series_by_ids


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


def _configured_arr_instances() -> list[dict]:
    instances: list[dict] = []
    if getattr(settings, 'RADARR_URL', None) and getattr(settings, 'RADARR_API_KEY', None):
        instances.append(
            {
                'instance_key': settings.RADARR_STD_INSTANCE_KEY,
                'arr_type': 'radarr',
                'content_type': 'movie',
                'base_url': settings.RADARR_URL,
                'api_key': settings.RADARR_API_KEY,
                'is_4k': False,
            }
        )
    if getattr(settings, 'RADARR_4K_URL', None) and getattr(settings, 'RADARR_4K_API_KEY', None):
        instances.append(
            {
                'instance_key': settings.RADARR_4K_INSTANCE_KEY,
                'arr_type': 'radarr',
                'content_type': 'movie',
                'base_url': settings.RADARR_4K_URL,
                'api_key': settings.RADARR_4K_API_KEY,
                'is_4k': True,
            }
        )
    if getattr(settings, 'SONARR_URL', None) and getattr(settings, 'SONARR_API_KEY', None):
        instances.append(
            {
                'instance_key': settings.SONARR_STD_INSTANCE_KEY,
                'arr_type': 'sonarr',
                'content_type': 'series',
                'base_url': settings.SONARR_URL,
                'api_key': settings.SONARR_API_KEY,
                'is_4k': False,
            }
        )
    if getattr(settings, 'SONARR_4K_URL', None) and getattr(settings, 'SONARR_4K_API_KEY', None):
        instances.append(
            {
                'instance_key': settings.SONARR_4K_INSTANCE_KEY,
                'arr_type': 'sonarr',
                'content_type': 'series',
                'base_url': settings.SONARR_4K_URL,
                'api_key': settings.SONARR_4K_API_KEY,
                'is_4k': True,
            }
        )
    return instances


def _get_or_create_arr_state(session, instance_key: str, arr_type: str) -> ArrState:
    row = session.query(ArrState).filter(ArrState.instance_key == instance_key).first()
    if row:
        return row
    row = ArrState(instance_key=instance_key, arr_type=arr_type)
    session.add(row)
    session.flush()
    return row


def _parse_history_max_id(events: list[dict]) -> int | None:
    max_id = None
    for event in events:
        ev_id = event.get('id')
        try:
            ev_id_int = int(ev_id)
        except Exception:
            continue
        if max_id is None or ev_id_int > max_id:
            max_id = ev_id_int
    return max_id


def _extract_radarr_movie_ids(events: list[dict]) -> set[int]:
    ids: set[int] = set()
    for event in events:
        movie_id = event.get('movieId')
        if movie_id is None and isinstance(event.get('movie'), dict):
            movie_id = event['movie'].get('id')
        try:
            if movie_id is not None:
                ids.add(int(movie_id))
        except Exception:
            continue
    return ids


def _extract_sonarr_series_ids(events: list[dict]) -> set[int]:
    ids: set[int] = set()
    for event in events:
        series_id = event.get('seriesId')
        if series_id is None and isinstance(event.get('series'), dict):
            series_id = event['series'].get('id')
        try:
            if series_id is not None:
                ids.add(int(series_id))
        except Exception:
            continue
    return ids


def _run_startup_full_for_instances(instances: list[dict], run_ids: list[str]) -> dict:
    stats = {'instances': 0, 'succeeded': 0, 'failed': 0}
    if not instances:
        return stats

    session = get_session()
    try:
        for instance in instances:
            stats['instances'] += 1
            suffix = '4k' if instance['is_4k'] else 'standard'
            try:
                run = _create_run(
                    instance['content_type'],
                    is_4k=instance['is_4k'],
                    run_note=f"startup {instance['arr_type']} {suffix} fullsync",
                )
                run_ids.append(run.run_id)
                row = _get_or_create_arr_state(session, instance['instance_key'], instance['arr_type'])
                row.first_full_sync_completed_at = datetime.now(timezone.utc)
                row.updated_at = datetime.now(timezone.utc)
                row.last_history_checked_at = datetime.now(timezone.utc)
                session.add(row)
                session.commit()
                stats['succeeded'] += 1
            except Exception as e:
                session.rollback()
                stats['failed'] += 1
                logger.error(
                    f"Startup full sync failed for {instance['instance_key']}: {e}",
                    extra={'emoji_type': 'error'},
                )
        return stats
    finally:
        session.close()


def _run_startup_lite_history_for_instances(instances: list[dict]) -> dict:
    stats = {
        'instances': 0,
        'instances_failed': 0,
        'events_seen': 0,
        'events_with_ids': 0,
        'targeted_sync_runs': 0,
    }
    if not instances:
        return stats

    session = get_session()
    try:
        for instance in instances:
            stats['instances'] += 1
            instance_key = instance['instance_key']
            try:
                row = _get_or_create_arr_state(session, instance_key, instance['arr_type'])
                start_id = int(row.last_history_id or 0)

                if instance['arr_type'] == 'radarr':
                    events = fetch_radarr_history(start_id=start_id, url=instance['base_url'], api_key=instance['api_key'])
                    target_ids = _extract_radarr_movie_ids(events)
                    if target_ids:
                        sync_stats = sync_radarr_movies_by_ids(
                            target_ids,
                            base_url=instance['base_url'],
                            api_key=instance['api_key'],
                            is_4k=bool(instance['is_4k']),
                            instance_key=instance_key,
                        )
                        stats['targeted_sync_runs'] += 1
                        logger.info(
                            f"Startup lite targeted movie sync for {instance_key}: {sync_stats}",
                            extra={'emoji_type': 'info'},
                        )
                else:
                    events = fetch_sonarr_history(start_id=start_id, url=instance['base_url'], api_key=instance['api_key'])
                    target_ids = _extract_sonarr_series_ids(events)
                    if target_ids:
                        sync_stats = sync_sonarr_series_by_ids(
                            target_ids,
                            base_url=instance['base_url'],
                            api_key=instance['api_key'],
                            is_4k=bool(instance['is_4k']),
                            instance_key=instance_key,
                        )
                        stats['targeted_sync_runs'] += 1
                        logger.info(
                            f"Startup lite targeted series sync for {instance_key}: {sync_stats}",
                            extra={'emoji_type': 'info'},
                        )

                max_id = _parse_history_max_id(events)
                stats['events_seen'] += len(events)
                stats['events_with_ids'] += len(target_ids)
                row.last_history_checked_at = datetime.now(timezone.utc)
                row.updated_at = datetime.now(timezone.utc)
                if max_id is not None and max_id > int(row.last_history_id or 0):
                    row.last_history_id = max_id
                session.add(row)
                session.commit()
            except Exception as e:
                session.rollback()
                stats['instances_failed'] += 1
                logger.warning(
                    f"Startup lite history sync failed for {instance_key}: {e}",
                    extra={'emoji_type': 'warning'},
                )

        return stats
    finally:
        session.close()


def _resolve_startup_sync_mode(instances: list[dict]) -> str:
    mode = str(getattr(settings, 'STARTUP_SYNC_MODE', 'auto') or 'auto').strip().lower()
    if mode in ('off', 'full', 'lite'):
        return mode

    if mode != 'auto':
        logger.warning(
            f"Unknown STARTUP_SYNC_MODE={mode!r}; defaulting to auto",
            extra={'emoji_type': 'warning'},
        )

    if not instances:
        return 'off'

    session = get_session()
    try:
        for instance in instances:
            row = session.query(ArrState).filter(ArrState.instance_key == instance['instance_key']).first()
            if not row or row.first_full_sync_completed_at is None:
                return 'full'
    finally:
        session.close()
    return 'lite'


def run_startup_source_of_truth() -> dict:
    """Execute configured startup fullsyncs, filesystem scan, determine, and materialize."""
    run_ids: list[str] = []
    instances = _configured_arr_instances()
    selected_mode = _resolve_startup_sync_mode(instances)
    startup_sync_stats: dict = {}

    logger.info(
        f"Startup sync mode selected: {selected_mode} (configured_instances={len(instances)})",
        extra={'emoji_type': 'info'},
    )

    if selected_mode == 'full':
        startup_sync_stats = _run_startup_full_for_instances(instances, run_ids)
    elif selected_mode == 'lite':
        startup_sync_stats = _run_startup_lite_history_for_instances(instances)
    elif selected_mode == 'off':
        startup_sync_stats = {'instances': len(instances), 'skipped': True}
    else:
        # Defensive fallback. _resolve_startup_sync_mode should prevent this branch.
        startup_sync_stats = {'instances': len(instances), 'skipped': True, 'reason': f'unknown_mode:{selected_mode}'}

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
        'startup_sync_mode': selected_mode,
        'fullsync_ran': selected_mode == 'full' and bool(run_ids),
        'startup_sync': startup_sync_stats,
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
