from datetime import datetime, timezone
import logging
import threading
import requests
from services.plex_client import update_plex_title_status
from sqlalchemy.orm import Session
from core.config import settings

from services.postgres.db import get_session
from services.postgres.models import Episode, Movie, SubFlow, Season
from services.integrations import get_radarr_queue, get_sonarr_queue
from services.postgres.models import Series
import concurrent.futures
from services.integrations import update_placeholder_status
from services.jellyfin_client import create_jellyfin_nfo
from core.logger import logger

# logger = logging.getLogger("queue_monitor")
# logger.setLevel(logging.INFO)

POLL_INTERVAL = 10      # seconds
IDLE_TIMEOUT  = 120     # seconds without any pending playback jobs

class ProgressMonitor:
    """
    Event-driven monitoring of download progress that only runs when needed.
    """
    def __init__(self):
        self.is_monitoring = False
        # Thread locks to prevent concurrent updates to each service
        self._jellyfin_lock = threading.Lock()
        self._plex_lock = threading.Lock()
        self._monitor_thread = None
        self._stop_event = threading.Event()
        logger.info("ProgressMonitor initialized (not monitoring yet)")

    def start_monitoring(self):
        """Start monitoring when there's actually something to monitor"""
        if not self.is_monitoring:
            self._stop_event.clear()
            self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self._monitor_thread.start()
            self.is_monitoring = True
            logger.info("Started queue monitoring", extra={'emoji_type': 'start'})
        
    def stop_monitoring(self):
        """Stop monitoring when queue is empty"""
        if self.is_monitoring:
            self._stop_event.set()
            if self._monitor_thread and self._monitor_thread.is_alive():
                self._monitor_thread.join(timeout=5)
            self.is_monitoring = False
            logger.info("Stopped queue monitoring", extra={'emoji_type': 'stop'})

    def _monitor_loop(self):
        """Main monitoring loop running in background thread"""
        idle_count = 0
        max_idle_cycles = IDLE_TIMEOUT // POLL_INTERVAL
        
        while not self._stop_event.is_set():
            try:
                has_items = self.poll()
                
                if has_items:
                    idle_count = 0
                else:
                    idle_count += 1
                    if idle_count >= max_idle_cycles:
                        logger.info("No items found for extended period, stopping monitoring")
                        break
                
            except Exception as e:
                logger.error(f"Monitor loop error: {e}", extra={'emoji_type': 'error'})
            
            # Wait for next poll or stop signal
            self._stop_event.wait(POLL_INTERVAL)
        
        self.is_monitoring = False
        
    def poll(self):
        """Check for subflows and return whether any items were found"""
        session = get_session()
        try:
            now = datetime.now(timezone.utc)
            in_queue = session.query(SubFlow).filter_by(status='IN_QUEUE', action='playback', steps='monitoring').all()
            
            # If no items in queue, return False
            if not in_queue:
                return False
                
            # Determine which queues we actually need based on subflows
            needed_queues = set()
            
            for sf in in_queue:
                if sf.movie_id:
                    rec = session.query(Movie).get(sf.movie_id)
                    queue_key = 'radarr_4k' if rec.is_4k else 'radarr_standard'
                    needed_queues.add(queue_key)
                elif sf.episode_id:
                    rec = session.query(Episode).get(sf.episode_id)
                    # Get is_4k from series through relationships
                    is_4k = rec.season.series.is_4k if rec.season and rec.season.series else False
                    queue_key = 'sonarr_4k' if is_4k else 'sonarr_standard'
                    needed_queues.add(queue_key)
            
            # Only fetch the queues we actually need
            queues = {}
            for queue_key in needed_queues:
                if queue_key == 'radarr_standard':
                    queues[queue_key] = get_radarr_queue(is_4k=False)
                elif queue_key == 'radarr_4k':
                    queues[queue_key] = get_radarr_queue(is_4k=True)
                elif queue_key == 'sonarr_standard':
                    queues[queue_key] = get_sonarr_queue(is_4k=False)
                elif queue_key == 'sonarr_4k':
                    queues[queue_key] = get_sonarr_queue(is_4k=True)
            
            logger.debug(f"Processing {len(in_queue)} subflows with {len(queues)} queues: {list(queues.keys())}", 
                        extra={'emoji_type': 'process'})
            
            # Process all subflows with the cached queues
            # Process subflows sequentially to prevent rate limiting on media servers
            for sf in in_queue:
                if self._stop_event.is_set():
                    break
                self.process_subflow(sf, session, now, queues)
                
            session.commit()
            return True
            
        except Exception as e:
            session.rollback()
            logger.error(f"ProgressMonitor error: {e}", extra={'emoji_type': 'error'})
            return False
        finally:
            session.close()

    def _update_jellyfin_locked(self, **kwargs):
        """Thread-safe Jellyfin update with lock"""
        with self._jellyfin_lock:
            try:
                update_placeholder_status(kwargs.get('session'), ent_id=kwargs.get('ent_id'), model=kwargs.get('model'), action=kwargs.get('action'), status = kwargs.get('status'))
                create_jellyfin_nfo(kwargs.get('session'), ent_id=kwargs.get('ent_id'), model=kwargs.get('model'), action=kwargs.get('action'), status = kwargs.get('status'))

                # update_jellyfin_title_status(**kwargs)
            except Exception as e:
                logger.error(f"Error updating Jellyfin: {str(e)}", extra={'emoji_type': 'error'})

    def _update_plex_locked(self, **kwargs):
        """Thread-safe Plex update with lock"""
        with self._plex_lock:
            try:
                update_plex_title_status(**kwargs)
            except Exception as e:
                logger.error(f"Error updating Plex: {str(e)}", extra={'emoji_type': 'error'})

    def process_subflow(self, sf: SubFlow, session: Session, now: datetime, queues: dict):
        """Process a subflow using pre-fetched queues"""
        # Mark as in progress
        sf.status = 'IN_PROGRESS'
        session.add(sf)
        session.commit()
        
        try:
            # Determine which queue to check and what key to use
            if sf.movie_id:
                rec = session.query(Movie).get(sf.movie_id)
                arr_id = rec.radarrid
                queue_key = 'radarr_4k' if rec.is_4k else 'radarr_standard'
                item_key = 'movieId'
            else:
                rec = session.query(Episode).get(sf.episode_id)
                arr_id = rec.sonarrid
                # Get is_4k from series through relationships
                is_4k = rec.season.series.is_4k if rec.season and rec.season.series else False
                queue_key = 'sonarr_4k' if is_4k else 'sonarr_standard'
                item_key = 'episodeId'
                
            # Get the appropriate pre-fetched queue
            queue = queues.get(queue_key, [])
            
            # Find ALL matches in the pre-fetched queue
            matches = [it for it in queue if it.get(item_key) == arr_id]
            
            if matches:
                # Use the most advanced match for status updates
                matches.sort(key=lambda x: (
                    100 - (x.get('sizeleft', 0) or 0) / (x.get('size', 1) or 1) * 100 
                    if x.get('size', 0) else 0
                ), reverse=True)
                
                match = matches[0]
                
                # Calculate status and progress
                status = match.get('status', '').lower()
                size = match.get('size', 0) or 0
                left = match.get('sizeleft', 0) or 0
                progress = int(100 - (left/size*100)) if size > 0 else 0
                
                # Update DB record
                rec_field_status = 'radarr_status' if sf.movie_id else 'sonarr_status'
                rec_field_prog = 'radarr_progress' if sf.movie_id else 'sonarr_progress'
                setattr(rec, rec_field_status, status)
                setattr(rec, rec_field_prog, progress)
                session.add(rec)
                
                # Prepare update parameters
                update_params = {}
                if sf.movie_id:
                    update_params = {
                        'media_type': 'movie',
                        'model': Movie,
                        'ent_id': rec.id,
                        'action': 'playback',
                        'media_id': rec.tmdbid,
                        'title': rec.title,
                        'status': f"Downloading ({progress}%)",
                        'year': rec.year,
                        'session': session
                    }
                else:
                    # For episodes, get series info through relationships
                    series = rec.season.series if rec.season else None
                    if series:
                        update_params = {
                            'media_type': 'tv',
                            'model': Episode,
                            'ent_id': rec.id,
                            'action': 'playback',
                            'media_id': series.tvdbid,
                            'title': series.title,
                            'status': f"Downloading ({progress}%)",
                            'season': rec.season_number,
                            'episode': rec.episode_number,
                            'session': session
                        }
                
                # Send updates to both services concurrently but with locks
                if update_params:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                        futures = []
                        
                        # Submit Jellyfin update if enabled
                        if hasattr(settings, 'jellyfin_enabled') and settings.jellyfin_enabled:
                            futures.append(executor.submit(self._update_jellyfin_locked, **update_params))
                        
                        # Submit Plex update if enabled
                        if hasattr(settings, 'plex_enabled') and settings.plex_enabled:
                            futures.append(executor.submit(self._update_plex_locked, **update_params))
                        
                        # Wait for both to complete
                        concurrent.futures.wait(futures)
                
                # Update SubFlow status based on download status
                if status in ('completed', 'imported'):
                    sf.status = 'DONE'
                    session.add(sf)
                    logger.info(f"SubFlow {sf.id} DONE", extra={'emoji_type': 'success'})
                else:
                    # Still in progress, keep IN_QUEUE status
                    sf.status = 'IN_QUEUE'
                    session.add(sf)
            else:
                # Not in queue - check timeout based on when SubFlow was created
                elapsed = (now - sf.created_time.replace(tzinfo=timezone.utc)).total_seconds()
                timeout = getattr(settings, 'QUEUE_TIMEOUT_SECONDS', 200)
                
                if elapsed > timeout:
                    sf.status = 'FAILED'
                    session.add(sf)
                    logger.info(f"SubFlow {sf.id} FAILED (timeout after {timeout}s)", extra={'emoji_type': 'timeout'})
                else:
                    # Not timed out yet, put back in queue
                    sf.status = 'IN_QUEUE'
                    session.add(sf)
            
            # Commit all changes
            session.commit()
            
        except Exception as e:
            # Log error and rollback
            logger.error(f"Error processing subflow {sf.id}: {str(e)}", extra={'emoji_type': 'error'})
            session.rollback()
            
            # Put back in queue to retry later
            sf.status = 'IN_QUEUE'
            session.add(sf)
            session.commit()

# Create the global instance
progress_monitor = ProgressMonitor()

# Function to call from handlers and flows
def trigger_monitoring():
    """Call this when you add items to the SubFlow queue"""
    progress_monitor.start_monitoring()


def check_movie_has_file(radarr_id, is_4k=False):
    """
    Fetch Radarr movie data and update the local Movie record with useful fields.
    Returns True if Radarr reports a file exists (hasFile or movieFile present), False otherwise.
    """
    if not radarr_id:
        return False

    try:
        # Lazy import to avoid circular deps
        from services.postgres.db import get_session
        from services.utils import get_arr_config
        config = get_arr_config('movie', is_4k)
        if not config:
            logger.error("Radarr config missing for has-file check", extra={'emoji_type': 'error'})
            return False

        headers = {'X-Api-Key': config['api_key']}
        base_url = config['url']

        url = f"{base_url}/movie/{radarr_id}"
        r = requests.get(url, headers=headers, timeout=10)

        if r.status_code == 200:
            movie_data = r.json()

            # Map useful fields into DB
            session = get_session()
            try:
                m = session.query(Movie).filter_by(radarrid=int(radarr_id)).first()
                # Best effort: if not found, try to match by tmdbid
                if not m and movie_data.get('tmdbId'):
                    m = session.query(Movie).filter_by(tmdbid=int(movie_data.get('tmdbId'))).first()

                if m:
                    mf = movie_data.get('movieFile') or {}
                    file_path = mf.get('path') or movie_data.get('folderPath') or None
                    file_size = mf.get('size') or mf.get('sizeInBytes')
                    try:
                        if file_size is not None:
                            file_size = int(file_size)
                    except Exception:
                        file_size = None

                    has_file = bool(movie_data.get('hasFile', False) or mf)
                    quality = None
                    q = mf.get('quality') or movie_data.get('quality')
                    if isinstance(q, dict):
                        quality = q.get('name') or (q.get('quality') or {}).get('name')

                    monitored = bool(movie_data.get('monitored', False))
                    release_status = movie_data.get('status')

                    changed = False
                    if file_path and m.moviefile_path != file_path:
                        m.moviefile_path = file_path
                        changed = True
                    if file_size is not None and m.moviefile_size != file_size:
                        m.moviefile_size = file_size
                        changed = True
                    if m.has_file != has_file:
                        m.has_file = has_file
                        changed = True
                    if quality and m.radarr_quality != quality:
                        m.radarr_quality = quality
                        changed = True
                    if m.radarr_monitored != monitored:
                        m.radarr_monitored = monitored
                        changed = True
                    if release_status and m.radarr_release_status != release_status:
                        m.radarr_release_status = release_status
                        changed = True
                    if movie_data.get('id') and m.radarrid != movie_data.get('id'):
                        m.radarrid = movie_data.get('id')
                        changed = True

                    if changed:
                        session.add(m)
                        session.commit()
                        logger.info(f"Enriched movie {m.tmdbid} from Radarr during has-file check", extra={'emoji_type': 'update'})
                # else: no local movie found to enrich
            finally:
                session.close()

            return bool(movie_data.get('hasFile', False) or movie_data.get('movieFile'))

        elif r.status_code == 404:
            logger.debug(f"Radarr movie id {radarr_id} not found (404)", extra={'emoji_type': 'debug'})
            return False
        else:
            logger.error(f"Unexpected Radarr response {r.status_code} for movie {radarr_id}: {r.text}", extra={'emoji_type': 'error'})
            return False

    except requests.RequestException as re:
        logger.error(f"Request error checking Radarr movie {radarr_id}: {re}", extra={'emoji_type': 'error'})
        return False
    except Exception as e:
        logger.error(f"Error checking if movie has file: {e}", extra={'emoji_type': 'error'})
        return False


def check_episode_has_file(tvdb_id, season_number, episode_number, is_4k=False):
    """
    Fetch Sonarr series/episode data and update the local Series/Episode records.
    Returns True if Sonarr reports the episode has a file, False otherwise.
    """
    if not tvdb_id:
        return False

    try:
        from services.postgres.db import get_session
        from services.utils import get_arr_config

        session = get_session()
        try:
            series = session.query(Series).filter_by(tvdbid=tvdb_id, is_4k=is_4k).first()
            if not series:
                return False

            # Ensure we have a Sonarr internal id; try lookup by TVDB when missing
            sonarr_id = getattr(series, 'sonarrid', None)
            if not sonarr_id:
                config = get_arr_config('tv', is_4k)
                if config:
                    headers = {'X-Api-Key': config['api_key']}
                    try:
                        lookup = requests.get(f"{config['url']}/series/lookup", params={'term': f"tvdb:{int(series.tvdbid)}"}, headers=headers, timeout=10)
                        if lookup.ok:
                            results = lookup.json()
                            if isinstance(results, list) and results:
                                found = results[0]
                                if found.get('id'):
                                    series.sonarrid = found.get('id')
                                    session.add(series)
                                    session.commit()
                                    sonarr_id = series.sonarrid
                    except Exception:
                        logger.debug("Sonarr lookup by TVDB failed during has-file check", extra={'emoji_type': 'debug'})

            if not sonarr_id:
                return False

            # Fetch episodes for the series from Sonarr and update matching episode entry
            config = get_arr_config('tv', is_4k)
            if not config:
                logger.error("Sonarr config missing for has-file check", extra={'emoji_type': 'error'})
                return False

            headers = {'X-Api-Key': config['api_key']}
            url = f"{config['url']}/episode"
            params = {'seriesId': sonarr_id}
            r = requests.get(url, params=params, headers=headers, timeout=10)
            r.raise_for_status()
            episodes = r.json()

            # Find the specific episode
            target = None
            for ep in episodes:
                if ep.get('seasonNumber') == season_number and ep.get('episodeNumber') == episode_number:
                    target = ep
                    break

            if not target:
                return False

            # Update DB episode record if we can find it
            try:
                # find season and episode records
                season = session.query(Season).filter_by(series_id=series.id, season_number=season_number).first()
                if not season:
                    # nothing to update locally
                    pass
                else:
                    episode = session.query(Episode).filter_by(season_id=season.id, episode_number=episode_number).first()
                    if episode:
                        mf = target.get('hasFile', False) and target.get('episodeFile') or {}
                        file_path = mf.get('path') if isinstance(mf, dict) else None
                        file_size = mf.get('size') if isinstance(mf, dict) else None
                        try:
                            if file_size is not None:
                                file_size = int(file_size)
                        except Exception:
                            file_size = None

                        has_file = bool(target.get('hasFile', False) or mf)
                        quality = None
                        q = (target.get('quality') or {})
                        if isinstance(q, dict):
                            quality = q.get('quality', {}).get('name') or q.get('name')

                        monitored = bool(series.sonarr_monitored if hasattr(series, 'sonarr_monitored') else False)

                        changed = False
                        if file_path and episode.episodefile_path != file_path:
                            episode.episodefile_path = file_path
                            changed = True
                        if file_size is not None and episode.episodefile_size != file_size:
                            episode.episodefile_size = file_size
                            changed = True
                        if episode.has_file != has_file:
                            episode.has_file = has_file
                            changed = True
                        if quality and episode.sonarr_quality != quality:
                            episode.sonarr_quality = quality
                            changed = True
                        if episode.sonarr_monitored != monitored:
                            episode.sonarr_monitored = monitored
                            changed = True

                        if changed:
                            session.add(episode)
                            session.commit()
                            logger.info(f"Enriched episode S{season_number}E{episode_number} for series {series.title} from Sonarr", extra={'emoji_type': 'update'})
            except Exception:
                logger.debug("Failed to update local Episode record during Sonarr has-file check", extra={'emoji_type': 'debug'})

            return bool(target.get('hasFile', False) or target.get('episodeFile'))

        finally:
            session.close()

    except requests.RequestException as re:
        logger.error(f"Request error checking Sonarr series {tvdb_id}: {re}", extra={'emoji_type': 'error'})
        return False
    except Exception as e:
        logger.error(f"Error checking if episode has file: {e}", extra={'emoji_type': 'error'})
        return False

def handle_download_webhook(data):
    """Handle download completion events from *arr applications by marking the matching SubFlow DONE."""
    session = get_session()
    try:
        if 'movie' in data:
            # Movie download completed
            movie = data.get('movie', {})
            tmdb_id = movie.get('tmdbId')
            radarr_id = movie.get('id')  # Radarr internal ID
            title = movie.get('title')

            if not radarr_id:
                logger.warning(f"Movie download webhook missing Radarr ID for '{title}'", extra={'emoji_type': 'warning'})
                return

            # Remove from monitoring using Radarr ID (to match add_to_monitor)
            media_key = f"movie_{radarr_id}"
            remove_from_monitor(media_key)
            logger.debug(f"Removed movie '{title}' (Radarr ID: {radarr_id}) from monitoring", extra={'emoji_type': 'debug'})
            
        elif 'episodes' in data and 'series' in data:
            series = data['series']
            episodes = data['episodes']
            for ep in episodes:
                season = ep.get('seasonNumber')
                number = ep.get('episodeNumber')
                sonarr_id = ep.get('id')
                if sonarr_id is None:
                    continue

                sf = (
                    session.query(SubFlow)
                    .filter_by(branch='playback', status='IN_QUEUE')
                    .filter(SubFlow.episode_id.isnot(None))
                    .filter(SubFlow.episode.has(sonarrid=sonarr_id))
                    .first()
                )
                if sf:
                    sf.status = 'DONE'
                    session.add(sf)
            session.commit()

    except Exception as e:
        session.rollback()
        logger.error(f"Error handling download webhook: {e}", extra={'emoji_type': 'error'})
    finally:
        session.close()
# Deprecation warnings for old-style monitoring functions
def check_tv_has_file(*args, **kwargs):
    logger.warning(
        "[DEPRECATED] check_tv_has_file() is deprecated. Use add_to_monitor() instead.",
        extra={'emoji_type': 'warning'}
    )

def check_media_has_file(*args, **kwargs):
    logger.warning(
        "[DEPRECATED] check_media_has_file() is deprecated. Use add_to_monitor() instead.",
        extra={'emoji_type': 'warning'}
    )

def check_has_file(*args, **kwargs):
    logger.warning(
        "[DEPRECATED] check_has_file() is deprecated. Use add_to_monitor() instead.",
        extra={'emoji_type': 'warning'}
    )

def update_plex_title(*args, **kwargs):
    logger.warning(
        "[DEPRECATED] update_plex_title() is deprecated. Title updates are now handled by the registry-based system.",
        extra={'emoji_type': 'warning'}
    )