from dataclasses import dataclass
import os
import time
import uuid
from datetime import datetime, timezone

from core.config import settings
from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import ArrState, Movie, Series
from services.source_of_truth.arr_api import (
    fetch_radarr_history,
    fetch_radarr_movies,
    fetch_sonarr_history,
    fetch_sonarr_series,
)
from services.source_of_truth.calendar_phase import run_calendar_phase
from services.source_of_truth.calendar_date_refresh import run_calendar_date_refresh
from services.source_of_truth.determiner import run_determination_pass, run_placeholder_link_reconcile
from services.source_of_truth.filesystem import scan_once_if_needed
from services.source_of_truth.materializer import run_materialization_pass
from services.source_of_truth.placeholder_cleanup import run_orphan_placeholder_cleanup
from services.source_of_truth.sync_runner import run_full_sync, sync_radarr_movies_by_ids, sync_sonarr_series_by_ids


@dataclass
class FullSyncRunRef:
    run_id: str


def _normalize_path(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return os.path.normpath(text)


def _radarr_path_drift_movie_ids(session, *, instance_key: str, base_url: str, api_key: str) -> set[int]:
    """Detect Radarr items whose current API path differs from stored DB path."""
    rows = (
        session.query(Movie.radarrid, Movie.radarrpath)
        .filter(Movie.instance_key == instance_key, Movie.radarrid.isnot(None), Movie.is_deleted == False)  # noqa: E712
        .all()
    )
    if not rows:
        return set()

    api_movies = fetch_radarr_movies(base_url, api_key) or []
    api_path_by_id: dict[int, str | None] = {}
    for item in api_movies:
        if not isinstance(item, dict):
            continue
        mid = item.get("id")
        try:
            mid_int = int(mid)
        except Exception:
            continue
        api_path_by_id[mid_int] = _normalize_path(
            item.get("path") or item.get("folderPath") or item.get("rootFolderPath")
        )

    changed: set[int] = set()
    for row_id, row_path in rows:
        try:
            movie_id = int(row_id)
        except Exception:
            continue
        if movie_id not in api_path_by_id:
            continue
        if _normalize_path(row_path) != api_path_by_id[movie_id]:
            changed.add(movie_id)
    return changed


def _sonarr_path_drift_series_ids(session, *, instance_key: str, base_url: str, api_key: str) -> set[int]:
    """Detect Sonarr series whose current API path differs from stored DB path."""
    rows = (
        session.query(Series.sonarrid, Series.sonarrpath)
        .filter(Series.instance_key == instance_key, Series.sonarrid.isnot(None), Series.is_deleted == False)  # noqa: E712
        .all()
    )
    if not rows:
        return set()

    api_series = fetch_sonarr_series(base_url, api_key) or []
    api_path_by_id: dict[int, str | None] = {}
    for item in api_series:
        if not isinstance(item, dict):
            continue
        sid = item.get("id")
        try:
            sid_int = int(sid)
        except Exception:
            continue
        api_path_by_id[sid_int] = _normalize_path(item.get("path") or item.get("folderPath") or item.get("rootFolderPath"))

    changed: set[int] = set()
    for row_id, row_path in rows:
        try:
            series_id = int(row_id)
        except Exception:
            continue
        if series_id not in api_path_by_id:
            continue
        if _normalize_path(row_path) != api_path_by_id[series_id]:
            changed.add(series_id)
    return changed


def _create_run(content_type: str, is_secondary: bool = False, run_note: str | None = None, instance_key: str | None = None) -> FullSyncRunRef:
    suffix = 'secondary' if is_secondary else 'primary'
    run_id = f'fullsync:{content_type}:{suffix}:{uuid.uuid4()}'
    sync_types = ('movie',) if content_type == 'movie' else ('series',)
    logger.info(
        f"Starting startup {content_type} fullsync ({suffix}) run {run_id} ({run_note or 'no note'})",
        extra={'emoji_type': 'gear'},
    )
    run_full_sync(dry_run=False, types=sync_types, instance_key=instance_key)
    logger.info(f"Finished startup {content_type} fullsync ({suffix}) run {run_id}", extra={'emoji_type': 'success'})
    return FullSyncRunRef(run_id=run_id)


def capture_movies_fullsync_and_create_run(run_note: str | None = None) -> FullSyncRunRef:
    return _create_run('movie', is_secondary=False, run_note=run_note)


def capture_series_fullsync_and_create_run(run_note: str | None = None) -> FullSyncRunRef:
    return _create_run('series', is_secondary=False, run_note=run_note)


def _configured_arr_instances() -> list[dict]:
    instances: list[dict] = []
    for item in (getattr(settings, 'configured_arr_instances', []) or []):
        arr_type = str(item.get('arr_type') or '').strip().lower()
        instance_key = str(item.get('instance_key') or '').strip().lower()
        base_url = str(item.get('url') or '').strip()
        api_key = str(item.get('api_key') or '').strip()
        if arr_type not in {'radarr', 'sonarr'} or not instance_key or not base_url or not api_key:
            continue
        instances.append(
            {
                'instance_key': instance_key,
                'arr_type': arr_type,
                'content_type': 'movie' if arr_type == 'radarr' else 'series',
                'base_url': base_url,
                'api_key': api_key,
                'role': str(item.get('role') or 'primary').strip().lower() or 'primary',
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
            is_secondary = str(instance.get('role') or 'primary').strip().lower() != 'primary'
            suffix = 'secondary' if is_secondary else 'primary'
            try:
                run = _create_run(
                    instance['content_type'],
                    is_secondary=is_secondary,
                    run_note=f"startup {instance['arr_type']} {suffix} fullsync",
                    instance_key=instance['instance_key'],
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
        'path_drift_ids': 0,
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
                    drift_ids = _radarr_path_drift_movie_ids(
                        session,
                        instance_key=instance_key,
                        base_url=instance['base_url'],
                        api_key=instance['api_key'],
                    )
                    target_ids = set(target_ids) | set(drift_ids)
                    stats['path_drift_ids'] += len(drift_ids)
                    if target_ids:
                        sync_stats = sync_radarr_movies_by_ids(
                            target_ids,
                            base_url=instance['base_url'],
                            api_key=instance['api_key'],
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
                    drift_ids = _sonarr_path_drift_series_ids(
                        session,
                        instance_key=instance_key,
                        base_url=instance['base_url'],
                        api_key=instance['api_key'],
                    )
                    target_ids = set(target_ids) | set(drift_ids)
                    stats['path_drift_ids'] += len(drift_ids)
                    if target_ids:
                        sync_stats = sync_sonarr_series_by_ids(
                            target_ids,
                            base_url=instance['base_url'],
                            api_key=instance['api_key'],
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
    settings.REFRESH_TRIGGER_SUPPRESSED = True
    try:
        try:
            from services.source_of_truth.arr_instance_reconcile import tombstone_unconfigured_arr_rows

            detach_stats = tombstone_unconfigured_arr_rows(str(getattr(settings, 'ARR_INSTANCES_JSON', '') or ''))
            if any(
                int(detach_stats.get(key) or 0)
                for key in ('movies_tombstoned', 'series_tombstoned', 'seasons_tombstoned', 'episodes_tombstoned')
            ):
                logger.info(
                    f'Startup: tombstoned DB rows for instance keys not in ARR_INSTANCES_JSON: '
                    f"movies={detach_stats.get('movies_tombstoned')} series={detach_stats.get('series_tombstoned')} "
                    f"seasons={detach_stats.get('seasons_tombstoned')} episodes={detach_stats.get('episodes_tombstoned')}",
                    extra={'emoji_type': 'info'},
                )
        except Exception as exc:
            logger.warning(
                f'Startup ARR instance-key tombstone pass failed (non-fatal): {exc}',
                extra={'emoji_type': 'warning'},
            )

        run_ids: list[str] = []
        instances = _configured_arr_instances()
        selected_mode = _resolve_startup_sync_mode(instances)
        startup_sync_stats: dict = {}

        logger.info(
            f"Startup sync mode selected: {selected_mode} (configured_instances={len(instances)})",
            extra={'emoji_type': 'info'},
        )
        started_at = datetime.now(timezone.utc)

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
        # Primer phase removed to accelerate startup and simplify sync pipeline.
        primer_stats = {"skipped": True, "reason": "deprecated"}
        materialization_stats = run_materialization_pass()
        calendar_stats = run_calendar_phase()
        orphan_placeholder_stats = run_orphan_placeholder_cleanup()
        status_reconcile_stats = {"skipped": True, "reason": "deprecated"}

        result = {
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
            'orphan_placeholders': orphan_placeholder_stats,
            'status_reconcile': status_reconcile_stats,
        }
        try:
            from services.activity_markers import record_startup_source_of_truth_activity

            record_startup_source_of_truth_activity(
                mode=selected_mode,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
            )
        except Exception:
            logger.debug(
                "Startup activity marker persistence skipped",
                extra={'emoji_type': 'debug'},
                exc_info=True,
            )
        return result
    finally:
        settings.REFRESH_TRIGGER_SUPPRESSED = False
