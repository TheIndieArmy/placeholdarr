"""
Series sync logic for Placeholdarr: syncs all series from Sonarr (standard and 4K) to the local DB.
If a series is found in Sonarr but missing from the DB (or is unmonitored in Sonarr and not present in the DB),
it will enqueue a handle_seriesadd action to the scheduler.
"""

import logging
import requests
from services.postgres.db import get_session
from services.postgres.models import Series
from core.config import settings
from services.utils import is_4k_request

logger = logging.getLogger("services.sync.sync_series")

def fetch_sonarr_series(sonarr_url, api_key):
    base = sonarr_url.rstrip('/')
    if '/api/v' in base:
        url = f"{base}/series"
    elif base.endswith('/api') or '/api' in base:
        url = f"{base}/v3/series"
    else:
        url = f"{base}/api/v3/series"
    headers = {"X-Api-Key": api_key}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()

def sync_series_with_sonarr(instance_name=None):
    """Sync series for a specific Sonarr instance ('standard' or '4k').
    If instance_name is None, sync both.
    """
    from services.handlers import handle_seriesadd_scheduler
    session = get_session()
    try:
        instances = []
        if instance_name is None or instance_name == "standard":
            if getattr(settings, 'SONARR_URL', None) and getattr(settings, 'SONARR_API_KEY', None):
                instances.append({
                    "name": "standard",
                    "url": settings.SONARR_URL,
                    "api_key": settings.SONARR_API_KEY,
                })
        if instance_name is None or instance_name == "4k":
            if getattr(settings, 'SONARR_4K_URL', None) and getattr(settings, 'SONARR_4K_API_KEY', None):
                instances.append({
                    "name": "4k",
                    "url": settings.SONARR_4K_URL,
                    "api_key": settings.SONARR_4K_API_KEY,
                })
        existing_series = {(s.tvdbid, s.is_4k): s for s in session.query(Series).all()}
        added = 0
        updated = 0
        enqueued = 0
        for instance in instances:
            logger.info(f"Fetching series from Sonarr instance '{instance.get('name')}' at {instance.get('url')}")
            try:
                series_list = fetch_sonarr_series(instance['url'], instance['api_key'])
            except Exception:
                logger.exception(f"Failed to fetch series from Sonarr instance: {instance.get('name')}")
                continue
            for series in series_list:
                tvdbid = series.get('tvdbId')
                title = series.get('title')
                year = series.get('year')
                sonarrid = series.get('id')
                filepath = series.get('path') or ''
                is_4k = is_4k_request(filepath)
                key = (tvdbid, is_4k)
                db_series = existing_series.get(key)
                sonarr_monitored = bool(series.get('monitored', False))
                sonarr_quality = None
                q = series.get('qualityProfileId')
                if q:
                    sonarr_quality = str(q)
                if db_series:
                    changed = False
                    mapping = {
                        'title': title,
                        'year': year,
                        'sonarrid': sonarrid,
                        'filepath': filepath,
                        'sonarr_monitored': sonarr_monitored,
                        'is_4k': is_4k,
                        'sonarr_quality': sonarr_quality,
                    }
                    for field, new_val in mapping.items():
                        if getattr(db_series, field, None) != new_val:
                            setattr(db_series, field, new_val)
                            changed = True
                    if changed:
                        updated += 1
                else:
                    # If series is unmonitored in Sonarr and not present in DB, enqueue handle_seriesadd
                    if not sonarr_monitored:
                        series_data = {
                            'series': {
                                'tvdbId': tvdbid,
                                'title': title,
                                'year': year,
                                'id': sonarrid,
                                'monitored': sonarr_monitored,
                                'path': filepath,
                                'qualityProfileId': sonarr_quality
                            },
                            'episodes': []
                        }
                        try:
                            job_scheduled = handle_seriesadd_scheduler.enqueue(series_data['series'])
                            if job_scheduled:
                                enqueued += 1
                                logger.info(f"Enqueued 'handle_seriesadd' for missing/unmonitored series: {tvdbid}")
                        except Exception as e:
                            logger.error(f"Failed to enqueue 'handle_seriesadd' for TVDB {tvdbid}: {e}")
                    new_series = Series(
                        tvdbid=tvdbid,
                        title=title,
                        year=year,
                        sonarrid=sonarrid,
                        filepath=filepath,
                        sonarr_monitored=sonarr_monitored,
                        is_4k=is_4k,
                        sonarr_quality=sonarr_quality,
                        status='PENDING'
                    )
                    session.add(new_series)
                    added += 1
        session.commit()
        processed = added + updated
        logger.info(f"Series sync complete: {added} added, {updated} updated, {enqueued} enqueued, {processed} total processed")
    finally:
        session.close()
