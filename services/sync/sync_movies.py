"""
Movie sync logic for Placeholdarr: syncs all movies from Radarr (standard and 4K) to the local DB.
This file recreates the original implementation but keeps import-time side effects minimal
and gracefully handles optional dependencies (requests/apscheduler) so imports won't fail.
"""

import logging
from typing import List

logger = logging.getLogger("services.sync.sync_movies")

# Optional dependencies
try:
    import requests
except Exception:
    requests = None

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    HAVE_APSCHED = True
except Exception:
    BackgroundScheduler = None
    HAVE_APSCHED = False

from services.postgres.db import get_session
from services.postgres.models import Movie
from core.config import settings
from services.utils import is_4k_request


def _fetch_radarr_movies(radarr_url: str, api_key: str) -> List[dict]:
    if not requests:
        raise RuntimeError("requests library not available")
    url = f"{radarr_url.rstrip('/')}/api/v3/movie"
    headers = {"X-Api-Key": api_key}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def sync_movies_with_radarr(instance_name: str | None = None) -> None:
    """Sync movies for a specific Radarr instance ('standard' or '4k').
    If instance_name is None, sync both configured instances.
    This function performs database upserts keyed by (tmdbid, is_4k).
    """
    instances = []
    if instance_name in (None, "standard"):
        if getattr(settings, 'RADARR_URL', None) and getattr(settings, 'RADARR_API_KEY', None):
            instances.append({
                "name": "standard",
                "url": settings.RADARR_URL,
                "api_key": settings.RADARR_API_KEY,
                "library_folder": getattr(settings, 'MOVIE_LIBRARY_FOLDER', None),
            })
    if instance_name in (None, "4k"):
        if getattr(settings, 'RADARR_4K_URL', None) and getattr(settings, 'RADARR_4K_API_KEY', None):
            instances.append({
                "name": "4k",
                "url": settings.RADARR_4K_URL,
                "api_key": settings.RADARR_4K_API_KEY,
                "library_folder": getattr(settings, 'MOVIE_LIBRARY_4K_FOLDER', None),
            })

    if not instances:
        logger.debug("No Radarr instances configured for sync")
        return

    if not requests:
        logger.error("Cannot sync movies: 'requests' library not available", extra={'emoji_type': 'error'})
        return

    session = get_session()
    try:
        existing_movies = {(m.tmdbid, m.is_4k): m for m in session.query(Movie).all()}
        added = 0
        updated = 0
        for inst in instances:
            try:
                movies = _fetch_radarr_movies(inst['url'], inst['api_key'])
            except Exception:
                logger.exception(f"Failed to fetch movies from Radarr instance: {inst.get('name')}")
                continue

            for movie in movies:
                tmdbid = movie.get('tmdbId')
                file_path = movie.get('path') or ''
                is_4k = is_4k_request(file_path)
                key = (tmdbid, is_4k)
                db_movie = existing_movies.get(key)

                theater_release = movie.get('inCinemas')
                digital_release = movie.get('digitalRelease') or movie.get('physicalRelease')
                physical_release = movie.get('physicalRelease')

                if db_movie:
                    changed = False
                    updates = {
                        'title': movie.get('title'),
                        'year': movie.get('year'),
                        'radarrid': movie.get('id'),
                        'radarrpath': file_path,
                        'is_4k': is_4k,
                        'theater_release_date': theater_release,
                        'digital_release_date': digital_release,
                        'physical_release_date': physical_release,
                    }
                    for field, val in updates.items():
                        if getattr(db_movie, field, None) != val:
                            setattr(db_movie, field, val)
                            changed = True
                    if changed:
                        updated += 1
                else:
                    new_movie = Movie(
                        tmdbid=tmdbid,
                        title=movie.get('title'),
                        year=movie.get('year'),
                        radarrid=movie.get('id'),
                        radarrpath=file_path,
                        is_4k=is_4k,
                        theater_release_date=theater_release,
                        digital_release_date=digital_release,
                        physical_release_date=physical_release,
                        status='PENDING',
                    )
                    session.add(new_movie)
                    added += 1
        session.commit()
        logger.info(f"Movies synced: {added} added, {updated} updated")
    finally:
        session.close()


def schedule_all_syncs() -> None:
    """Schedule Radarr movie syncs based on settings. Safe to call at startup.

    Behavior:
    - If RADARR_SYNC_ON_STARTUP or RADARR_4K_SYNC_ON_STARTUP are set, run the corresponding sync once.
    - If RADARR_SYNC_CRON / RADARR_4K_SYNC_CRON are set and APScheduler is available, start cron jobs.
    """
    # Run startup syncs
    try:
        if getattr(settings, 'RADARR_SYNC_ON_STARTUP', False):
            logger.info("Running Radarr (standard) movie sync on startup...")
            sync_movies_with_radarr(instance_name='standard')
    except Exception:
        logger.exception('Failed running RADARR standard startup sync')

    try:
        if getattr(settings, 'RADARR_4K_SYNC_ON_STARTUP', False):
            logger.info("Running Radarr (4K) movie sync on startup...")
            sync_movies_with_radarr(instance_name='4k')
    except Exception:
        logger.exception('Failed running RADARR 4K startup sync')

    # Cron scheduling (use APScheduler if present)
    def _start_cron_safe(cron_str, target_fn, label):
        if not cron_str:
            return
        if not HAVE_APSCHED:
            logger.warning(f"Cron configured for {label} but APScheduler not installed; skipping scheduler")
            return
        try:
            parts = cron_str.replace('"', '').split()
            cron_kwargs = {k: v for k, v in zip(['minute', 'hour', 'day', 'month', 'day_of_week'], parts) if v != '*'}
            sched = BackgroundScheduler()
            sched.add_job(target_fn, 'cron', **cron_kwargs)
            sched.start()
            logger.info(f"Started {label} scheduler with cron: {cron_str}")
        except Exception:
            logger.exception(f"Failed to start scheduler for {label}")

    _start_cron_safe(getattr(settings, 'RADARR_SYNC_CRON', None), lambda: sync_movies_with_radarr(instance_name='standard'), 'Radarr (standard)')
    _start_cron_safe(getattr(settings, 'RADARR_4K_SYNC_CRON', None), lambda: sync_movies_with_radarr(instance_name='4k'), 'Radarr (4K)')
