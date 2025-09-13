from datetime import datetime, timezone
import logging
import threading
import requests
from services.jellyfin_client import update_jellyfin_title_status
from services.plex_client import update_plex_title_status
from sqlalchemy.orm import Session
from core.config import settings

from services.postgres.db import get_session
from services.postgres.models import Episode, Movie, SubFlow
from services.integrations import get_radarr_queue, get_sonarr_queue
from services.postgres.models import Series
import concurrent.futures

logger = logging.getLogger("queue_monitor")
logger.setLevel(logging.INFO)

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
            in_queue = session.query(SubFlow).filter_by(status='IN_QUEUE', action='playback').all()
            
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
                update_jellyfin_title_status(**kwargs)
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
                        'media_id': rec.tmdbid,
                        'title': rec.title,
                        'status': f"Downloading ({progress}%)",
                        'year': rec.year
                    }
                else:
                    # For episodes, get series info through relationships
                    series = rec.season.series if rec.season else None
                    if series:
                        update_params = {
                            'media_type': 'tv',
                            'media_id': series.tvdbid,
                            'title': series.title,
                            'status': f"Downloading ({progress}%)",
                            'season': rec.season_number,
                            'episode': rec.episode_number
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
                # Not in queue - check timeout
                elapsed = (now - sf.started_at).total_seconds()
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
    Check if a movie has a file in Radarr
    
    Args:
        radarr_id: Radarr movie ID
        is_4k: Whether to use 4K Radarr
    
    Returns:
        True if the movie has a file, False otherwise
    """
    try:
        base_url = settings.RADARR_4K_URL if is_4k else settings.RADARR_URL
        api_key = settings.RADARR_4K_API_KEY if is_4k else settings.RADARR_API_KEY
        
        url = f"{base_url}/movie/{radarr_id}"
        headers = {'X-Api-Key': api_key}
        
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        
        movie_data = response.json()
        return movie_data.get('hasFile', False)
    
    except Exception as e:
        logger.error(f"Error checking if movie has file: {e}", extra={'emoji_type': 'error'})
        return False


def check_episode_has_file(tvdb_id, season_number, episode_number, is_4k=False):
    """
    Check if an episode has a file in Sonarr
    
    Args:
        tvdb_id: TVDB ID for the series
        season_number: Season number
        episode_number: Episode number
        is_4k: Whether to use 4K Sonarr
    
    Returns:
        True if the episode has a file, False otherwise
    """
    try:        # Get series ID from the database instead of API call
        # Prefer getting series ID from the database instead of API call
        session_db = get_session()
        try:
            series = session_db.query(Series).filter_by(tvdbid=tvdb_id, is_4k=is_4k).first()
            series_id = series.sonarrid if series else None
        finally:
            session_db.close()
        session_db = get_session()
        try:
            # Find the series by tvdb_id and is_4k flag
            series = session_db.query(Series).filter_by(tvdbid=tvdb_id, is_4k=is_4k).first()
            series_id = series.sonarrid if series else None
        finally:
            session_db.close()        
        if not series_id:
            return False
        
        # Then get episode details
        base_url = settings.SONARR_4K_URL if is_4k else settings.SONARR_URL
        api_key = settings.SONARR_4K_API_KEY if is_4k else settings.SONARR_API_KEY
        
        url = f"{base_url}/episode"
        params = {'seriesId': series_id}
        headers = {'X-Api-Key': api_key}
        
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        
        episodes = response.json()
        
        # Find the specific episode
        for ep in episodes:
            if ep.get('seasonNumber') == season_number and ep.get('episodeNumber') == episode_number:
                return ep.get('hasFile', False)
        
        return False
    
    except Exception as e:
        logger.error(f"Error checking if episode has file: {e}", extra={'emoji_type': 'error'})
        return False

def handle_download_webhook(data):
    """Handle download completion events from *arr applications by marking the matching SubFlow DONE."""
    session = get_session()
    try:
        if 'movie' in data:
            movie = data['movie']
            radarr_id = movie.get('id')
            if radarr_id is None:
                return

            # find the in-queue subflow for this radarr download
            sf = (
                session.query(SubFlow)
                .filter_by(branch='playback', status='IN_QUEUE')
                .filter(SubFlow.movie_id.isnot(None))
                .filter(SubFlow.movie.has(radarrid=radarr_id))
                .first()
            )
            if sf:
                sf.status = 'DONE'
                session.add(sf)
                session.commit()

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