"""
Movie sync logic for Placeholdarr: syncs all movies from Radarr (standard and 4K) to the local DB.
Uses consistent field determination and supports two instances.
"""

import logging
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from services.postgres.db import get_session
from services.postgres.models import Movie
from core.config import settings
from services.utils import is_4k_request

logger = logging.getLogger("services.sync.sync_movies")


def fetch_radarr_movies(radarr_url, api_key):
    base = radarr_url.rstrip('/')
    # If user included an API path already (e.g. /api or /api/v3), don't double-up the prefix
    if '/api/v' in base:
        url = f"{base}/movie"
    elif base.endswith('/api') or '/api' in base:
        url = f"{base}/v3/movie"
    else:
        url = f"{base}/api/v3/movie"
    headers = {"X-Api-Key": api_key}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def sync_movies_with_radarr(instance_name=None):
    """Sync movies for a specific Radarr instance ('standard' or '4k').
    If instance_name is None, sync both.
    """
    logger.info(f"Starting movie sync for instance: {instance_name or 'both'}")
    instances = []
    if instance_name is None or instance_name == "standard":
        if getattr(settings, 'RADARR_URL', None) and getattr(settings, 'RADARR_API_KEY', None):
            instances.append({
                "name": "standard",
                "url": settings.RADARR_URL,
                "api_key": settings.RADARR_API_KEY,
                "library_folder": getattr(settings, 'DUMMY_MOVIE_LIBRARY_FOLDER', None)
            })
    if instance_name is None or instance_name == "4k":
        if getattr(settings, 'RADARR_4K_URL', None) and getattr(settings, 'RADARR_4K_API_KEY', None):
            instances.append({
                "name": "4k",
                "url": settings.RADARR_4K_URL,
                "api_key": settings.RADARR_4K_API_KEY,
                "library_folder": getattr(settings, 'DUMMY_MOVIE_LIBRARY_4K_FOLDER', None)
            })

    session = get_session()
    from services.handlers import handle_movieadd_scheduler
    try:
        existing_movies = {(m.tmdbid, m.is_4k): m for m in session.query(Movie).all()}
        added = 0
        updated = 0
        enqueued = 0
        for instance in instances:
            logger.info(f"Fetching movies from Radarr instance '{instance.get('name')}' at {instance.get('url')}")
            try:
                movies = fetch_radarr_movies(instance['url'], instance['api_key'])
            except Exception:
                logger.exception(f"Failed to fetch movies from Radarr instance: {instance.get('name')}")
                continue

            for movie in movies:
                tmdbid = movie.get('tmdbId')
                movie_file = movie.get('movieFile') or {}
                moviefile_path = movie_file.get('path') or ''
                moviefile_size = movie_file.get('size') or movie_file.get('sizeInBytes')
                filepath = movie.get('path') or ''
                file_to_check = moviefile_path or filepath
                is_4k = is_4k_request(file_to_check)
                key = (tmdbid, is_4k)
                db_movie = existing_movies.get(key)

                theater_release = movie.get('inCinemas')
                digital_release = movie.get('digitalRelease') or movie.get('physicalRelease')
                physical_release = movie.get('physicalRelease')

                has_file = bool(movie.get('hasFile', False) or movie_file)
                radarr_release_status = movie.get('status') or movie_file.get('status')
                radarr_monitored = bool(movie.get('monitored', False))

                radarr_quality = None
                q = movie_file.get('quality') or movie.get('quality')
                if isinstance(q, dict):
                    radarr_quality = q.get('name') or (q.get('quality') or {}).get('name')

                if db_movie:
                    changed = False
                    mapping = {
                        'title': movie.get('title'),
                        'year': movie.get('year'),
                        'radarrid': movie.get('id'),
                        'filepath': filepath,
                        'moviefile_path': moviefile_path,
                        'moviefile_size': moviefile_size,
                        'has_file': has_file,
                        'radarr_quality': radarr_quality,
                        'radarr_release_status': radarr_release_status,
                        'radarr_monitored': radarr_monitored,
                        'is_4k': is_4k,
                        'theater_release_date': theater_release,
                        'digital_release_date': digital_release,
                        'physical_release_date': physical_release,
                    }
                    for field, new_val in mapping.items():
                        if getattr(db_movie, field, None) != new_val:
                            setattr(db_movie, field, new_val)
                            changed = True
                    if changed:
                        updated += 1
                else:
                    # If movie is unmonitored in Radarr and not present in DB, enqueue handle_movieadd
                    if not radarr_monitored:
                        # Prepare a minimal dict for the handler
                        movie_data = {
                            'movie': {
                                'tmdbId': tmdbid,
                                'title': movie.get('title'),
                                'year': movie.get('year'),
                                'id': movie.get('id'),
                                'monitored': radarr_monitored,
                                'folderPath': filepath,
                                'movieFile': movie_file,
                                'hasFile': has_file,
                                'quality': radarr_quality,
                                'status': radarr_release_status
                            }
                        }
                        # Enqueue the scheduler action
                        try:
                            job_scheduled = handle_movieadd_scheduler.enqueue(movie_data['movie'])
                            if job_scheduled:
                                enqueued += 1
                                logger.info(f"Enqueued 'handle_movieadd' for missing/unmonitored movie: {tmdbid}")
                        except Exception as e:
                            logger.error(f"Failed to enqueue 'handle_movieadd' for TMDB {tmdbid}: {e}")
                    # Still add to DB for tracking
                    new_movie = Movie(
                        tmdbid=tmdbid,
                        title=movie.get('title'),
                        year=movie.get('year'),
                        radarrid=movie.get('id'),
                        filepath=filepath,
                        moviefile_path=moviefile_path,
                        moviefile_size=moviefile_size,
                        has_file=has_file,
                        radarr_quality=radarr_quality,
                        radarr_release_status=radarr_release_status,
                        radarr_monitored=radarr_monitored,
                        is_4k=is_4k,
                        theater_release_date=theater_release,
                        digital_release_date=digital_release,
                        physical_release_date=physical_release,
                        status='PENDING'
                    )
                    session.add(new_movie)
                    added += 1
        session.commit()
        processed = added + updated
        logger.info(f"Movie sync complete: {added} added, {updated} updated, {enqueued} enqueued, {processed} total processed")
    finally:
        session.close()


def schedule_all_syncs():
    """Schedule Radarr syncs for both instances based on ENV settings.
    Handles startup syncs and cron scheduling.
    """
    # Startup syncs
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

    # Cron scheduling – parse simple cron-like strings split by whitespace into minute hour day month day_of_week
    def _start_cron(cron_str, target, label):
        if not cron_str:
            return
        try:
            parts = cron_str.replace('"', '').split()
            cron_kwargs = {k: v for k, v in zip(['minute', 'hour', 'day', 'month', 'day_of_week'], parts) if v != '*'}
            sched = BackgroundScheduler()
            sched.add_job(lambda: target(), 'cron', **cron_kwargs)
            sched.start()
            logger.info(f"{label} scheduler started with cron: {cron_str}")
        except Exception:
            logger.exception(f"Failed to start scheduler for {label}")

    _start_cron(getattr(settings, 'RADARR_SYNC_CRON', None), lambda: sync_movies_with_radarr(instance_name='standard'), 'Radarr (standard)')
    _start_cron(getattr(settings, 'RADARR_4K_SYNC_CRON', None), lambda: sync_movies_with_radarr(instance_name='4k'), 'Radarr (4K)')
