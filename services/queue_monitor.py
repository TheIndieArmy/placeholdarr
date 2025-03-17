import threading
import time
import requests
from core.logger import logger
from core.config import settings
from services.integrations import get_sonarr_queue, get_radarr_queue, update_plex_title, strip_status_markers
from datetime import datetime, timezone, timedelta

# Global registry to track monitored media
MONITORED_MEDIA = {}

# Single timer for batch processing
BATCH_TIMER = None

# Lock for thread safety when modifying the registry
REGISTRY_LOCK = threading.RLock()

# Global cache for API responses to reduce API calls
API_CACHE = {
    'sonarr_queue': {'standard': {'data': None, 'timestamp': 0}, '4k': {'data': None, 'timestamp': 0}},
    'radarr_queue': {'standard': {'data': None, 'timestamp': 0}, '4k': {'data': None, 'timestamp': 0}},
    'series_id_map': {'standard': {}, '4k': {}},  # Maps TVDB ID to Sonarr series ID
    'episode_map': {'standard': {}, '4k': {}},
    'movie_map': {'standard': {}, '4k': {}}
}

# Cache expiry time in seconds
CACHE_EXPIRY = 10  # 10 seconds

# Make sure this function is properly defined
def add_to_monitor(media_data):
    """Add a media item to monitoring"""
    global BATCH_TIMER, MONITORED_MEDIA
    
    # Skip if title updates are disabled
    if settings.TITLE_UPDATES == "OFF":
        logger.debug(f"Title updates disabled, not monitoring: {media_data.get('title')}", extra={'emoji_type': 'debug'})
        return
    
    # Skip if already has file
    if media_data.get('hasFile', False):
        logger.debug(f"Skipping monitoring for item that already has file: {media_data.get('title')}", 
                   extra={'emoji_type': 'debug'})
        return
    
    # Create unique key based on media type
    if media_data['media_type'] == 'movie':
        media_key = f"movie_{media_data['radarr_id']}"
    else:  # episode
        media_key = f"episode_{media_data['tvdb_id']}_{media_data['season_number']}_{media_data['episode_number']}"
    
    # Log the episode_id for episodes to help debug
    if media_data['media_type'] == 'episode':
        episode_id = media_data.get('episode_id')
        if not episode_id:
            logger.error(f"Missing episode_id for {media_data.get('title')} - Queue tracking will fail!", 
                      extra={'emoji_type': 'error'})
    
    with REGISTRY_LOCK:
        # Add to registry if not already present
        if media_key not in MONITORED_MEDIA:
            # Common fields
            entry = {
                'media_type': media_data['media_type'],
                'title': media_data['title'],
                'rating_key': media_data['rating_key'],
                'is_4k': media_data.get('is_4k', False),
                'status': 'searching',
                'start_time': time.time(),
                'last_update_time': time.time(),
                'last_status': '',
                'attempts': 0,
                'retrying': False
            }
            
            # Media-specific fields
            if media_data['media_type'] == 'movie':
                entry.update({
                    'tmdb_id': media_data.get('tmdb_id'),
                    'radarr_id': media_data.get('radarr_id')
                })
            else:  # episode
                entry.update({
                    'tvdb_id': media_data.get('tvdb_id', None),
                    'season_number': media_data['season_number'],
                    'episode_number': media_data['episode_number'],
                    'series_title': media_data.get('series_title', 'Unknown Series'),
                    'episode_id': media_data.get('episode_id')  # Make sure episode_id is stored
                })
            
            MONITORED_MEDIA[media_key] = entry
            
            # Log addition
            logger.info(f"Added {media_data['media_type']} to monitor: {media_data['title']}", 
                      extra={'emoji_type': 'monitor'})
            
            # Update Plex with initial "Searching..." status (only for ALL mode)
            if settings.TITLE_UPDATES == "ALL":
                from services.integrations import update_plex_title
                update_plex_title(media_data['rating_key'], media_data['title'], "Searching...")
        
        # Start the batch timer if it's not already running and we're in ALL mode
        if settings.TITLE_UPDATES == "ALL" and (BATCH_TIMER is None or not BATCH_TIMER.is_alive()):
            start_batch_monitoring()

def remove_from_monitor(media_key):
    """Remove an item from the monitoring registry"""
    with REGISTRY_LOCK:
        if media_key in MONITORED_MEDIA:
            item = MONITORED_MEDIA[media_key]
            
            if item['media_type'] == 'movie':
                logger.debug(f"Removed movie from monitor: {item['title']}",
                           extra={'emoji_type': 'debug'})
            else:
                logger.debug(f"Removed episode from monitor: S{item['season_number']}E{item['episode_number']}",
                           extra={'emoji_type': 'debug'})
            
            del MONITORED_MEDIA[media_key]
        
        # If registry is empty, stop the timer
        if not MONITORED_MEDIA and BATCH_TIMER is not None:
            stop_batch_monitoring()

def start_batch_monitoring():
    """Start a timer to periodically check the status of all monitored media"""
    global BATCH_TIMER
    
    # Log the current title update setting to help with debugging
    logger.info(f"⚡ Queue monitor initializing. Title updates setting: '{settings.TITLE_UPDATES}'", extra={'emoji_type': 'process'})
    
    # If title updates are disabled, don't start monitoring
    if settings.TITLE_UPDATES == "OFF":
        logger.info("⚡ Title updates disabled, not starting monitoring", extra={'emoji_type': 'info'})
        return
        
    # Only do full monitoring for ALL mode
    if settings.TITLE_UPDATES == "ALL":
        check_interval = getattr(settings, 'CHECK_INTERVAL', 10)  # Default to 10 seconds
        
        # Create a timer that will run batch_check_media after the interval
        BATCH_TIMER = threading.Timer(check_interval, batch_check_media)
        BATCH_TIMER.daemon = True
        BATCH_TIMER.start()
        logger.info(f"⚡ Started batch monitoring timer (interval: {check_interval}s)", extra={'emoji_type': 'process'})
    else:
        logger.info(f"⚡ Full monitoring not enabled, using mode: {settings.TITLE_UPDATES}", extra={'emoji_type': 'info'})

def stop_batch_monitoring():
    """Stop the batch monitoring timer"""
    global BATCH_TIMER
    
    if BATCH_TIMER is not None:
        BATCH_TIMER.cancel()
        BATCH_TIMER = None
        logger.debug("Stopped batch media monitoring", extra={'emoji_type': 'debug'})

def update_media_status(media_key, status, progress=None):
    """Update the status for a specific media item (LOGGING ONLY FOR STAGE 1)"""
    with REGISTRY_LOCK:
        if media_key not in MONITORED_MEDIA:
            return
        
        media = MONITORED_MEDIA[media_key]
        current_status = media.get('last_status', '')
        
        # Only update if status has changed
        if current_status != status:
            # Get detailed info for logging
            if media['media_type'] == 'movie':
                title_str = f"{media['title']}"
            else:  # episode
                title_str = f"{media.get('series_title', 'Unknown')} S{media.get('season_number', 0):02d}E{media.get('episode_number', 0):02d}"
            
            # Log status change
            logger.info(f"Status change for {title_str}: {current_status} → {status}", extra={'emoji_type': 'status'})
            
            # Update status in registry
            media['last_status'] = status
            media['last_update_time'] = time.time()
            
            # DISABLED FOR STAGE 1: Title updates will be implemented in Stage 2
            # if settings.TITLE_UPDATES == "ALL":
            #     try:
            #         from services.integrations import update_plex_title
            #         update_plex_title(media['rating_key'], media['title'], status)
            #     except Exception as e:
            #         logger.error(f"Failed to update Plex title: {e}", extra={'emoji_type': 'error'})
            
            # Special handling for "Available" status
            if status == "Available":
                # Schedule removal from monitoring after a delay
                remove_timer = threading.Timer(
                    settings.AVAILABLE_CLEANUP_DELAY, 
                    remove_from_monitor, 
                    args=[media_key]
                )
                remove_timer.daemon = True
                remove_timer.start()
        
        # Always log progress updates if provided, even if status hasn't changed
        if progress is not None:
            if media['media_type'] == 'movie':
                title_str = f"{media['title']}"
            else:  # episode
                title_str = f"{media.get('series_title', 'Unknown')} S{media.get('season_number', 0):02d}E{media.get('episode_number', 0):02d}"
                
            logger.info(f"Download progress for {title_str}: {progress}%", extra={'emoji_type': 'progress'})
            media['progress'] = progress

def batch_check_media():
    """
    Main batch processing function that checks all monitored media.
    This function will reschedule itself until all media is processed.
    """
    logger.info("BATCH CHECK TRIGGERED - Examining monitored items...", extra={'emoji_type': 'debug'})

    try:
        logger.debug(f"Batch checking {len(MONITORED_MEDIA)} monitored items", extra={'emoji_type': 'debug'})
        
        # Group media by type and quality
        movies_standard = []
        movies_4k = []
        episodes_standard = []
        episodes_4k = []
        
        current_time = time.time()
        
        # First pass - check for timeouts and group items
        with REGISTRY_LOCK:
            for media_key, media_data in list(MONITORED_MEDIA.items()):
                # Check for timeout based on status
                if media_data['status'] == 'searching' or media_data['status'] == 'retrying':
                    start = media_data['start_time']
                    if current_time - start > settings.MAX_MONITOR_TIME:
                        # Media has timed out
                        update_media_status(media_key, "Not Found")
                        logger.info(f"Monitoring timed out for {media_data['title']}", 
                                  extra={'emoji_type': 'timeout'})
                        remove_from_monitor(media_key)
                        continue
                
                # Add to appropriate list for queue checking
                if media_data['media_type'] == 'movie':
                    if media_data['is_4k']:
                        movies_4k.append(media_data)
                    else:
                        movies_standard.append(media_data)
                else:  # episode
                    if media_data['is_4k']:
                        episodes_4k.append(media_data)
                    else:
                        episodes_standard.append(media_data)
        
        # IMPORTANT: Run the download status check - this handles queue status and progress
        _check_downloads_status()
        
        # REMOVE THE OTHER QUEUE CHECKS - or comment them out for now
        # Our debug logging will help determine if we need these
        # These likely duplicate queue checks and cause the back-and-forth status changes
        
        """
        # COMMENTED OUT to prevent duplicate queue checks:
        if movies_standard:
            process_movie_batch(movies_standard, False)
        
        if movies_4k:
            process_movie_batch(movies_4k, True)
        
        if episodes_standard:
            process_episode_batch(episodes_standard, False)
        
        if episodes_4k:
            process_episode_batch(episodes_4k, True)
        """
        
        # Reschedule if there are still media to monitor
        if MONITORED_MEDIA:
            check_interval = getattr(settings, 'CHECK_INTERVAL', 10)
            BATCH_TIMER = threading.Timer(check_interval, batch_check_media)
            BATCH_TIMER.daemon = True
            BATCH_TIMER.start()
            logger.debug(f"Next queue check scheduled in {check_interval} seconds", extra={'emoji_type': 'debug'})
            
    except Exception as e:
        logger.error(f"Error in batch check: {str(e)}", extra={'emoji_type': 'error'})
        # Still try to reschedule even after error
        if MONITORED_MEDIA:
            check_interval = getattr(settings, 'CHECK_INTERVAL', 10)
            BATCH_TIMER = threading.Timer(check_interval, batch_check_media)
            BATCH_TIMER.daemon = True
            BATCH_TIMER.start()
            logger.debug(f"Rescheduled after error in {check_interval} seconds", extra={'emoji_type': 'debug'})

def process_movie_batch(movies, is_4k):
    """
    Process a batch of movies with a single queue request
    
    Args:
        movies: List of movie data dictionaries
        is_4k: Whether these are 4K movies
    """
    # Skip if empty
    if not movies:
        return
        
    # Get Radarr queue once for all movies
    try:
        queue = get_radarr_queue(is_4k)
        
        # Process each movie independently
        for movie in movies:
            media_key = f"movie_{movie['rating_key']}"
            
            # Find movie in queue
            queue_item = find_movie_in_queue(queue, movie)
            
            if queue_item:
                # Movie is in download queue
                process_movie_queue_item(media_key, movie, queue_item)
            else:
                # Not in queue, check if it has a file
                has_file = check_movie_has_file(movie['radarr_id'], is_4k)
                
                if has_file:
                    # Movie has been imported, mark as available
                    update_media_status(media_key, "Available")
                    
                    # Wait briefly for Plex to scan the new file
                    schedule_movie_available_cleanup(media_key, movie)
                elif movie.get('retrying', False):
                    # Still retrying, update status
                    update_media_status(media_key, "Retrying...")
                else:
                    # Still searching for initial release
                    update_media_status(media_key, "Searching...")
                    
                # Check Radarr history to see if we've had any activity
                check_movie_history(media_key, movie, is_4k)
    
    except Exception as e:
        logger.error(f"Error processing movie batch: {e}", extra={'emoji_type': 'error'})

def process_movie_queue_item(media_key, movie, queue_item):
    """Process a movie queue item and update status"""
    try:
        status = queue_item.get('status', '').lower()
        title = movie.get('title', 'Unknown Movie')
        
        # Debug logging for status
        logger.debug(f"Queue status for movie {title}: {status}", extra={'emoji_type': 'debug'})
        
        # Status based on queue item state
        if status == 'completed':
            update_media_status(media_key, "Processing...")
        
        elif status == 'downloading':
            # Get progress info
            try:
                size_remaining = float(queue_item.get('sizeleft', 0))
                size_total = float(queue_item.get('size', 0))
                
                if size_total > 0:
                    percent = int(100 - ((size_remaining / size_total) * 100))
                    logger.info(f"Download progress for {title}: {percent}%", 
                              extra={'emoji_type': 'download'})
                    update_media_status(media_key, f"Downloading {percent}%")
                else:
                    update_media_status(media_key, "Downloading...")
            except (ValueError, TypeError, ZeroDivisionError) as e:
                logger.error(f"Error calculating download percentage: {e}", extra={'emoji_type': 'error'})
                update_media_status(media_key, "Downloading...")
        
        elif status in ['delay', 'queued', 'paused']:
            update_media_status(media_key, "Queued")
        
        elif status == 'warning':
            update_media_status(media_key, "Warning")
        
        elif status == 'error':
            update_media_status(media_key, "Error")
    
    except Exception as e:
        logger.error(f"Error processing movie queue item: {e}", extra={'emoji_type': 'error'})

def schedule_movie_available_cleanup(media_key, movie):
    """Schedule removal of movie from monitoring after a delay to allow Plex scanning"""
    delay = getattr(settings, 'AVAILABLE_CLEANUP_DELAY', 10)  # Default 10 seconds
    
    def cleanup():
        with REGISTRY_LOCK:
            if media_key in MONITORED_MEDIA:
                logger.info(f"Movie available: {movie['title']}", extra={'emoji_type': 'success'})
                remove_from_monitor(media_key)
    
    timer = threading.Timer(delay, cleanup)
    timer.daemon = True
    timer.start()

def check_movie_history(media_key, movie, is_4k=False):
    """
    Check Radarr history to determine if a movie download was completed but not yet imported,
    or if it failed and should be marked as retrying
    """
    try:
        # Only check history if we're not already retrying
        with REGISTRY_LOCK:
            if media_key in MONITORED_MEDIA and MONITORED_MEDIA[media_key].get('retrying', False):
                return
        
        # Get movie history from Radarr
        history = get_radarr_history(movie['radarr_id'], is_4k)
        
        if not history:
            return
            
        # Sort by date descending to get most recent events
        history.sort(key=lambda x: x.get('date', ''), reverse=True)
        
        # Check most recent event
        latest_event = history[0] if history else None
        
        if not latest_event:
            return
            
        event_type = latest_event.get('eventType', '').lower()
        
        # If most recent event is grabbed but not followed by downloadFailed or downloadFolderImported
        if event_type == 'grabbed':
            # Download started but not completed yet - we should see it in queue soon
            # Calculate how long ago the grab happened
            try:
                event_date = datetime.fromisoformat(latest_event.get('date', '').replace('Z', '+00:00'))
                now = datetime.now().astimezone()
                minutes_since_grab = (now - event_date).total_seconds() / 60
                
                # If it was grabbed more than 5 minutes ago but not in queue, likely failed silently
                if minutes_since_grab > 5:
                    update_media_status(media_key, "Retrying...")
                    logger.info(f"Movie download may have failed silently, retrying: {movie['title']}", 
                              extra={'emoji_type': 'retry'})
            except Exception as e:
                logger.error(f"Error calculating grab time: {e}", extra={'emoji_type': 'error'})
        
        elif event_type == 'downloadfolderimported':
            # Movie was recently imported, should show up with hasFile=true soon
            update_media_status(media_key, "Processing...")
            logger.debug(f"Movie recently imported, waiting for file: {movie['title']}", extra={'emoji_type': 'debug'})
            
        elif event_type == 'downloadfailed':
            # Download failed, mark as retrying
            with REGISTRY_LOCK:
                if media_key in MONITORED_MEDIA:
                    MONITORED_MEDIA[media_key]['retrying'] = True
            
            update_media_status(media_key, "Retrying...")
            logger.info(f"Movie download failed, retrying: {movie['title']}", extra={'emoji_type': 'retry'})
    
    except Exception as e:
        logger.error(f"Error checking movie history: {e}", extra={'emoji_type': 'error'})

def process_episode_batch(episodes, is_4k):
    """Process a batch of episodes"""
    try:
        # Get queue once for all episodes
        queue = get_sonarr_queue(is_4k)
        logger.debug(f"Fetched Sonarr queue ({len(queue)} items)", extra={'emoji_type': 'debug'})
        
        # Process each episode
        for episode in episodes:
            if 'tvdb_id' not in episode or 'season_number' not in episode or 'episode_number' not in episode:
                logger.error(f"Missing required fields in episode data: {episode}", extra={'emoji_type': 'error'})
                continue
                
            media_key = f"episode_{episode['tvdb_id']}_{episode['season_number']}_{episode['episode_number']}"
            
            # Find episode in queue
            episode_in_queue = None
            for item in queue:
                # Check if this queue item is for our episode
                if ('episodeId' in item and 'episode' in item and 
                    item['episode'].get('seasonNumber') == episode['season_number'] and 
                    item['episode'].get('episodeNumber') == episode['episode_number']):
                    episode_in_queue = item
                    break
            
            if episode_in_queue:
                # Episode is in queue, update status
                logger.debug(f"Found episode in queue: {episode.get('series_title', 'Unknown')} S{episode.get('season_number', 0):02d}E{episode.get('episode_number', 0):02d}", 
                          extra={'emoji_type': 'debug'})
                process_episode_queue_item(media_key, episode, episode_in_queue)
            else:
                # Not in queue, keep searching status
                # Only log status change if it changed
                with REGISTRY_LOCK:
                    if media_key in MONITORED_MEDIA:
                        current_status = MONITORED_MEDIA[media_key].get('last_status', '')
                        if current_status != 'Searching...':
                            logger.debug(f"No queue item found for {episode.get('series_title', 'Unknown')} S{episode.get('season_number', 0):02d}E{episode.get('episode_number', 0):02d}, still searching.", 
                                      extra={'emoji_type': 'debug'})
                            update_media_status(media_key, "Searching...")
    
    except Exception as e:
        logger.error(f"Error processing episode batch: {e}", extra={'emoji_type': 'error'})

def find_episode_in_queue(queue, episode):
    """
    Find an episode in the Sonarr queue
    
    Args:
        queue: List of queue items
        episode: Episode data dictionary
    
    Returns:
        Queue item for the episode, or None if not found
    """
    tvdb_id = episode.get('tvdb_id')
    season_number = episode.get('season_number')
    episode_number = episode.get('episode_number')
    
    # Get or fetch series ID
    series_id = get_sonarr_series_id_by_tvdb(tvdb_id, episode.get('is_4k', False))
    
    for item in queue:
        if 'episode' not in item:
            continue
        
        ep_info = item['episode']
        
        # Check if this queue item matches our episode
        if (ep_info.get('seriesId') == series_id and
            ep_info.get('seasonNumber') == season_number and
            ep_info.get('episodeNumber') == episode_number):
            return item
    
    return None

def find_movie_in_queue(queue, movie):
    """
    Find a movie in the Radarr queue
    
    Args:
        queue: List of queue items
        movie: Movie data dictionary
    
    Returns:
        Queue item for the movie, or None if not found
    """
    radarr_id = movie.get('radarr_id')
    
    for item in queue:
        if 'movieId' in item and item['movieId'] == radarr_id:
            return item
    
    return None

def process_episode_queue_item(media_key, episode, queue_item):
    """Process an episode queue item and update status"""
    try:
        status = queue_item.get('status', '').lower()
        title = f"{episode.get('series_title', 'Unknown')} S{episode.get('season_number', 0):02d}E{episode.get('episode_number', 0):02d}"
        
        # Debug logging for queue item
        logger.debug(f"Processing queue item: {queue_item}", extra={'emoji_type': 'debug'})
        
        # Status based on queue item state
        if status == 'completed':
            update_media_status(media_key, "Processing...")
            
        elif status == 'downloading':
            # Get progress info
            size_left = queue_item.get('sizeleft', 0)
            size_total = queue_item.get('size', 0)
            logger.debug(f"Download sizes - Left: {size_left}, Total: {size_total}", extra={'emoji_type': 'debug'})
            
            try:
                size_remaining = float(size_left)
                size_total = float(size_total)
                
                if size_total > 0:
                    percent = int(100 - ((size_remaining / size_total) * 100))
                    logger.info(f"Download progress for {title}: {percent}%", extra={'emoji_type': 'download'})
                    update_media_status(media_key, f"Downloading {percent}%")
                else:
                    update_media_status(media_key, "Downloading...")
            except (ValueError, TypeError, ZeroDivisionError) as e:
                logger.error(f"Error calculating download percentage: {e}", extra={'emoji_type': 'error'})
                update_media_status(media_key, "Downloading...")
        
        elif status in ['delay', 'queued', 'paused']:
            update_media_status(media_key, "Queued")
        
        elif status == 'warning':
            update_media_status(media_key, "Warning")
        
        elif status == 'error':
            update_media_status(media_key, "Error")
        else:
            logger.debug(f"Unknown status '{status}' for {title}", extra={'emoji_type': 'debug'})
    
    except Exception as e:
        logger.error(f"Error processing episode queue item: {e}", extra={'emoji_type': 'error'})

def schedule_episode_available_cleanup(media_key, episode):
    """Schedule removal of episode from monitoring after a delay to allow Plex scanning"""
    delay = getattr(settings, 'AVAILABLE_CLEANUP_DELAY', 10)  # Default 10 seconds
    
    def cleanup():
        with REGISTRY_LOCK:
            if media_key in MONITORED_MEDIA:
                logger.info(f"Episode available: {episode['series_title']} S{episode['season_number']:02d}E{episode['episode_number']:02d}", 
                          extra={'emoji_type': 'success'})
                remove_from_monitor(media_key)
    
    timer = threading.Timer(delay, cleanup)
    timer.daemon = True
    timer.start()

def check_episode_history(media_key, episode, is_4k=False):
    """
    Check Sonarr history to determine if an episode download was completed but not yet imported,
    or if it failed and should be marked as retrying
    """
    try:
        # Only check history if we're not already retrying
        with REGISTRY_LOCK:
            if media_key in MONITORED_MEDIA and MONITORED_MEDIA[media_key].get('retrying', False):
                return
        
        # Get episode history from Sonarr
        history = get_sonarr_episode_history(
            episode['tvdb_id'], 
            episode['season_number'], 
            episode['episode_number'],
            is_4k
        )
        
        if not history:
            return
            
        # Sort by date descending to get most recent events
        history.sort(key=lambda x: x.get('date', ''), reverse=True)
        
        # Check most recent event
        latest_event = history[0] if history else None
        
        if not latest_event:
            return
            
        event_type = latest_event.get('eventType', '').lower()
        
        # If most recent event is grabbed but not followed by downloadFailed or downloadFolderImported
        if event_type == 'grabbed':
            # Download started but not completed yet - we should see it in queue soon
            # Calculate how long ago the grab happened
            try:
                event_date = datetime.fromisoformat(latest_event.get('date', '').replace('Z', '+00:00'))
                now = datetime.now().astimezone()
                minutes_since_grab = (now - event_date).total_seconds() / 60
                
                # If it was grabbed more than 5 minutes ago but not in queue, likely failed silently
                if minutes_since_grab > 5:
                    with REGISTRY_LOCK:
                        if media_key in MONITORED_MEDIA:
                            MONITORED_MEDIA[media_key]['retrying'] = True
                            MONITORED_MEDIA[media_key]['start_time'] = time.time()  # Reset timeout
                    
                    update_media_status(media_key, "Retrying...")
                    logger.info(f"Episode download failed, retrying: {episode['series_title']} S{episode['season_number']:02d}E{episode['episode_number']:02d}",
                              extra={'emoji_type': 'retry'})
            except Exception as e:
                logger.error(f"Error calculating grab time: {e}", extra={'emoji_type': 'error'})
        
        elif event_type == 'downloadfolderimported':
            # Episode was recently imported, should show up with hasFile=true soon
            update_media_status(media_key, "Processing...")
            logger.debug(f"Episode recently imported, waiting for file: {episode['series_title']} S{episode['season_number']:02d}E{episode['episode_number']:02d}", 
                       extra={'emoji_type': 'debug'})
            
        elif event_type == 'downloadfailed':
            # Download failed, mark as retrying
            with REGISTRY_LOCK:
                if media_key in MONITORED_MEDIA:
                    MONITORED_MEDIA[media_key]['retrying'] = True
                    MONITORED_MEDIA[media_key]['start_time'] = time.time()  # Reset timeout
            
            update_media_status(media_key, "Retrying...")
            logger.info(f"Episode download failed, retrying: {episode['series_title']} S{episode['season_number']:02d}E{episode['episode_number']:02d}", 
                      extra={'emoji_type': 'retry'})
    
    except Exception as e:
        logger.error(f"Error checking episode history: {e}", extra={'emoji_type': 'error'})

# Additional functions for API interaction

def get_radarr_history(movie_id, is_4k=False):
    """
    Get history for a specific movie from Radarr
    
    Args:
        movie_id: Radarr movie ID
        is_4k: Whether to use 4K Radarr
        
    Returns:
        List of history records
    """
    try:
        base_url = settings.RADARR_4K_URL if is_4k else settings.RADARR_URL
        api_key = settings.RADARR_4K_API_KEY if is_4k else settings.RADARR_API_KEY
        
        params = {'movieId': movie_id, 'pageSize': 10}
        url = f"{base_url}/history"
        headers = {'X-Api-Key': api_key}
        
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        
        data = response.json()
        return data.get('records', [])
    
    except Exception as e:
        logger.error(f"Error getting Radarr history: {e}", extra={'emoji_type': 'error'})
        return []

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

def get_sonarr_episode_history(tvdb_id, season_number, episode_number, is_4k=False):
    """
    Get history for a specific episode from Sonarr
    
    Args:
        tvdb_id: TVDB ID
        season_number: Season number
        episode_number: Episode number
        is_4k: Whether to use 4K Sonarr
        
    Returns:
        List of history records
    """
    try:
        # First get series ID and episode ID
        series_id = get_sonarr_series_id_by_tvdb(tvdb_id, is_4k)
        
        if not series_id:
            return []
            
        episode_id = get_sonarr_episode_id(series_id, season_number, episode_number, is_4k)
        
        if not episode_id:
            return []
            
        # Get history for this episode
        base_url = settings.SONARR_4K_URL if is_4k else settings.SONARR_URL
        api_key = settings.SONARR_4K_API_KEY if is_4k else settings.SONARR_API_KEY
        
        params = {'episodeId': episode_id, 'pageSize': 10}
        url = f"{base_url}/history"
        headers = {'X-Api-Key': api_key}
        
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        
        data = response.json()
        return data.get('records', [])
    
    except Exception as e:
        logger.error(f"Error getting Sonarr episode history: {e}", extra={'emoji_type': 'error'})
        return []

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
    try:
        # First get the series ID
        series_id = get_sonarr_series_id_by_tvdb(tvdb_id, is_4k)
        
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

def get_sonarr_series_id_by_tvdb(tvdb_id, is_4k=False):
    """
    Get Sonarr series ID from TVDB ID with caching
    
    Args:
        tvdb_id: TVDB ID
        is_4k: Whether to use 4K Sonarr
    
    Returns:
        Sonarr series ID, or None if not found
    """
    cache_key = '4k' if is_4k else 'standard'
    
    # Try to get from cache first
    if str(tvdb_id) in API_CACHE['series_id_map'][cache_key]:
        return API_CACHE['series_id_map'][cache_key][str(tvdb_id)]
    
    # Not in cache, need to fetch
    try:
        base_url = settings.SONARR_4K_URL if is_4k else settings.SONARR_URL
        api_key = settings.SONARR_4K_API_KEY if is_4k else settings.SONARR_API_KEY
        
        url = f"{base_url}/series"
        headers = {'X-Api-Key': api_key}
        
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        
        series_list = response.json()
        
        # Find the series with matching TVDB ID
        for series in series_list:
            if str(series.get('tvdbId')) == str(tvdb_id):
                series_id = series.get('id')
                # Cache the result
                API_CACHE['series_id_map'][cache_key][str(tvdb_id)] = series_id
                return series_id
        
        return None
    
    except Exception as e:
        logger.error(f"Error finding series ID for TVDB ID {tvdb_id}: {e}", extra={'emoji_type': 'error'})
        return None

def get_sonarr_episode_id(series_id, season_number, episode_number, is_4k=False):
    """
    Get Sonarr episode ID from series ID and season/episode numbers
    
    Args:
        series_id: Sonarr series ID
        season_number: Season number
        episode_number: Episode number
        is_4k: Whether to use 4K Sonarr
        
    Returns:
        Episode ID or None if not found
    """
    try:
        base_url = settings.SONARR_4K_URL if is_4k else settings.SONARR_URL
        api_key = settings.SONARR_4K_API_KEY if is_4k else settings.SONARR_API_KEY
        
        params = {'seriesId': series_id}
        url = f"{base_url}/episode"
        headers = {'X-Api-Key': api_key}
        
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        
        episodes = response.json()
        
        for ep in episodes:
            if ep.get('seasonNumber') == season_number and ep.get('episodeNumber') == episode_number:
                return ep.get('id')
        
        return None
    
    except Exception as e:
        logger.error(f"Error getting Sonarr episode ID: {e}", extra={'emoji_type': 'error'})
        return None

def _check_downloads_status():
    """Check status of monitored downloads and update progress"""
    try:
        with REGISTRY_LOCK:
            if not MONITORED_MEDIA:
                return
                
            # Log how many items we're monitoring
            logger.debug(f"Checking status for {len(MONITORED_MEDIA)} items in monitoring queue", 
                      extra={'emoji_type': 'debug'})
        
        # Check Sonarr queue
        sonarr_queue = {}
        try:
            sonarr_url = f"{settings.SONARR_URL}/queue"
            headers = {'X-Api-Key': settings.SONARR_API_KEY}
            response = requests.get(sonarr_url, headers=headers)
            if response.status_code == 200:
                queue_data = response.json()
                records = queue_data.get('records', [])
                
                logger.debug(f"🐛 Fetched Sonarr queue ({len(records)} items)", extra={'emoji_type': 'debug'})
                
                # Detailed debug logging for all queue items
                for item in records:
                    episode_id = item.get('episodeId')
                    title = item.get('title', 'Unknown')
                    status = item.get('status', 'Unknown')
                    
                    # Calculate progress
                    size = item.get('size', 0)
                    size_left = item.get('sizeleft', 0)
                    progress = 0
                    if size > 0:
                        progress = 100 - (size_left / size * 100)
                        
                    logger.debug(f"Queue item: {title} | ID: {episode_id} | Status: {status} | Progress: {progress:.1f}%", 
                                extra={'emoji_type': 'debug'})
                
                for item in records:
                    episode_id = item.get('episodeId')
                    
                    # Track by episode ID and include progress percentage
                    if episode_id:
                        status = item.get('status', 'Unknown')
                        # Avoid division by zero
                        if item.get('size', 0) > 0:
                            progress = item.get('sizeleft', 0) / item.get('size', 1) * 100
                            progress = 100 - progress  # Convert to percentage complete
                        else:
                            progress = 0
                            
                        # Map Sonarr statuses to our status strings
                        if status.lower() == 'downloading':
                            status = "Downloading"
                        elif status.lower() == 'completed':
                            status = "Downloaded"
                        elif status.lower() == 'queued':
                            status = "Queued"
                            
                        sonarr_queue[str(episode_id)] = {
                            'status': status,
                            'progress': round(progress, 1)
                        }
        except Exception as e:
            logger.error(f"Failed to check Sonarr queue: {e}", extra={'emoji_type': 'error'})
            
        # Check Radarr queue
        radarr_queue = {}
        try:
            radarr_url = f"{settings.RADARR_URL}/queue"
            headers = {'X-Api-Key': settings.RADARR_API_KEY}
            response = requests.get(radarr_url, headers=headers)
            if response.status_code == 200:
                queue_data = response.json()
                for item in queue_data.get('records', []):
                    movie_id = item.get('movieId')
                    
                    # Track by movie ID and include progress percentage
                    if movie_id:
                        status = item.get('status', 'Unknown')
                        # Avoid division by zero
                        if item.get('size', 0) > 0:
                            progress = item.get('sizeleft', 0) / item.get('size', 1) * 100
                            progress = 100 - progress  # Convert to percentage complete
                        else:
                            progress = 0
                            
                        # Map Radarr statuses to our status strings
                        if status.lower() == 'downloading':
                            status = "Downloading"
                        elif status.lower() == 'completed':
                            status = "Downloaded"
                        elif status.lower() == 'queued':
                            status = "Queued"
                            
                        radarr_queue[str(movie_id)] = {
                            'status': status,
                            'progress': round(progress, 1)
                        }
        except Exception as e:
            logger.error(f"Failed to check Radarr queue: {e}", extra={'emoji_type': 'error'})
        
        # Update status of monitored items
        with REGISTRY_LOCK:
            for key, media in list(MONITORED_MEDIA.items()):
                try:
                    if media['media_type'] == 'episode':
                        # Check if episode is in queue
                        episode_id = media.get('episode_id')
                        title_str = f"{media.get('series_title', 'Unknown')} S{media.get('season_number', 0):02d}E{media.get('episode_number', 0):02d}"
                        
                        # Debug missing episode_id
                        if not episode_id:
                            logger.error(f"Missing episode_id for {title_str} - Queue matching will fail", extra={'emoji_type': 'error'})
                            continue
                            
                        # If episode is in queue, update status and progress
                        if str(episode_id) in sonarr_queue:
                            queue_info = sonarr_queue[str(episode_id)]
                            logger.debug(f"Match found in queue! Updating {title_str} - Status: {queue_info['status']} | Progress: {queue_info['progress']}%", 
                                      extra={'emoji_type': 'debug'})
                            update_media_status(key, queue_info['status'], progress=queue_info['progress'])
                            
                            # Reset attempts counter if we found the item in queue
                            if 'queue_absent_count' in media:
                                media['queue_absent_count'] = 0
                        else:
                            # Episode not in queue - different handling based on previous status:
                            previous_status = media.get('last_status', '')
                            
                            # If previously Downloaded, item may have been imported
                            if previous_status == "Downloaded":
                                # Mark as Importing - this could change to "Available" when import complete
                                update_media_status(key, "Importing")
                                logger.info(f"Status change for {title_str}: Downloaded → Importing", 
                                         extra={'emoji_type': 'status_change'})
                                
                                # If we've been in Importing status for a while, check if the file exists
                                if 'importing_start_time' not in media:
                                    media['importing_start_time'] = time.time()
                                elif time.time() - media['importing_start_time'] > 60:  # 60 seconds
                                    # Check if we have a file now by calling check_episode_has_file
                                    has_file = check_episode_has_file(
                                        media.get('tvdb_id'), 
                                        media.get('season_number'), 
                                        media.get('episode_number'),
                                        media.get('is_4k', False)
                                    )
                                    
                                    if has_file:
                                        # File imported successfully
                                        update_media_status(key, "Available")
                                        logger.info(f"Episode available: {title_str}", 
                                                  extra={'emoji_type': 'success'})
                                        
                                        # Schedule removal
                                        schedule_episode_available_cleanup(key, media)
                            
                            # If previously Downloading, it could be a silent failure or moved to importing
                            elif previous_status == "Downloading":
                                # Keep track of how many checks the item has been absent from queue
                                if 'queue_absent_count' not in media:
                                    media['queue_absent_count'] = 1
                                else:
                                    media['queue_absent_count'] += 1
                                
                                # After 2 checks (6 seconds) absent from queue, check history
                                if media['queue_absent_count'] >= 2:
                                    # Use the existing check_episode_history function
                                    # Build the required episode dict
                                    episode_data = {
                                        'tvdb_id': media.get('tvdb_id'),
                                        'season_number': media.get('season_number'),
                                        'episode_number': media.get('episode_number'),
                                        'series_title': media.get('series_title', 'Unknown Series')
                                    }
                                    
                                    # Call the history check function
                                    check_episode_history(key, episode_data, media.get('is_4k', False))
                                    
                                    # Reset counter after check
                                    media['queue_absent_count'] = 0
                            
                            # If item was never in queue or is searching/retrying
                            elif previous_status in ["", "Searching...", "Retrying..."]:
                                # Continue showing the current status, no need to update
                                logger.debug(f"No queue item found for {title_str} (episode_id: {episode_id}), status remains {previous_status or 'Searching...'}", 
                                          extra={'emoji_type': 'debug'})
                            
                            else:
                                # For any other previous status, if not in queue, just log it
                                logger.debug(f"No queue item found for {title_str} (episode_id: {episode_id}), was {previous_status}", 
                                          extra={'emoji_type': 'debug'})
                                
                    elif media['media_type'] == 'movie':
                        # Apply similar logic for movies
                        # [Movie handling code similar to episode handling]
                        movie_id = media.get('radarr_id')
                        title_str = f"{media.get('title', 'Unknown Movie')}"
                        
                        # Similar checks and status updates for movies
                        # [Implementation omitted for brevity]
                        pass
                            
                except Exception as e:
                    logger.error(f"Error updating item {key}: {e}", extra={'emoji_type': 'error'})
                    
    except Exception as e:
        logger.error(f"Error in download status check: {str(e)}", extra={'emoji_type': 'error'})

def handle_download_webhook(data):
    """Handle download webhook to update monitored items"""
    try:
        items_removed = 0
        
        if 'episodes' in data and len(data['episodes']) > 0:
            # Episode download
            episode = data['episodes'][0]
            series = data.get('series', {})
            
            episode_id = episode.get('id')
            tvdb_id = series.get('tvdbId')
            season_number = episode.get('seasonNumber')
            episode_number = episode.get('episodeNumber')
            series_title = series.get('title', '')
            
            logger.debug(f"Processing download webhook for {series_title} S{season_number:02d}E{episode_number:02d} (ID: {episode_id})", 
                       extra={'emoji_type': 'debug'})
            
            # Generate keys to look for in the monitored media dictionary
            keys_to_remove = []
            
            with REGISTRY_LOCK:
                # First check: Look for exact episode ID match
                for key, media in list(MONITORED_MEDIA.items()):
                    if media.get('media_type') == 'episode':
                        # Check by direct episode_id match
                        if media.get('episode_id') == episode_id:
                            keys_to_remove.append(key)
                            logger.info(f"Found monitored item by episode ID: {key} - {series_title} S{season_number:02d}E{episode_number:02d}", 
                                      extra={'emoji_type': 'cleanup'})
                        
                        # Also check by tvdb_id + season + episode
                        elif (media.get('tvdb_id') == tvdb_id and 
                              media.get('season_number') == season_number and 
                              media.get('episode_number') == episode_number):
                            keys_to_remove.append(key)
                            logger.info(f"Found monitored item by series+season+episode: {key} - {series_title} S{season_number:02d}E{episode_number:02d}", 
                                      extra={'emoji_type': 'cleanup'})
                
                # Now remove the found items from monitoring
                for key in keys_to_remove:
                    if key in MONITORED_MEDIA:
                        logger.info(f"Download complete: Removing {key} from monitoring", 
                                extra={'emoji_type': 'cleanup'})
                        remove_from_monitor(key)
                        items_removed += 1
                
                if not keys_to_remove:
                    logger.debug(f"No matching monitored items found for {series_title} S{season_number:02d}E{episode_number:02d}", 
                               extra={'emoji_type': 'debug'})
        
        elif 'movie' in data:
            # Movie download
            movie = data.get('movie', {})
            movie_title = movie.get('title', 'Unknown')
            movie_id = movie.get('id')
            tmdb_id = movie.get('tmdbId')
            
            logger.debug(f"Processing download webhook for movie: {movie_title} (ID: {movie_id})", 
                       extra={'emoji_type': 'debug'})
            
            keys_to_remove = []
            
            with REGISTRY_LOCK:
                # First check: Look for exact movie ID match
                for key, media in list(MONITORED_MEDIA.items()):
                    if media.get('media_type') == 'movie':
                        # Check by direct movie_id match
                        if media.get('movie_id') == movie_id:
                            keys_to_remove.append(key)
                            logger.info(f"Found monitored movie by ID: {key} - {movie_title}", 
                                      extra={'emoji_type': 'cleanup'})
                        
                        # Also check by tmdb_id
                        elif media.get('tmdb_id') == tmdb_id:
                            keys_to_remove.append(key)
                            logger.info(f"Found monitored movie by TMDB ID: {key} - {movie_title}", 
                                      extra={'emoji_type': 'cleanup'})
                
                # Now remove the found items from monitoring
                for key in keys_to_remove:
                    if key in MONITORED_MEDIA:
                        logger.info(f"Download complete: Removing {key} from monitoring", 
                                  extra={'emoji_type': 'cleanup'})
                        remove_from_monitor(key)
                        items_removed += 1
                
                if not keys_to_remove:
                    logger.debug(f"No matching monitored items found for movie {movie_title}", 
                               extra={'emoji_type': 'debug'})
        
        return items_removed > 0  # Return True if we removed at least one item
        
    except Exception as e:
        logger.error(f"Error processing download webhook: {e}", 
                   extra={'emoji_type': 'error'})
        return False

# Make sure the monitor thread starts when this module is imported
logger.info("Starting queue monitoring", extra={'emoji_type': 'process'})

# Make sure this appears at the very bottom of the file
logger.info("⚡ QUEUE MONITOR: Module loaded, initializing monitoring", extra={'emoji_type': 'process'})

# Start the monitoring process - use a single initialization
try:
    start_batch_monitoring()
    logger.info("Queue monitoring initialized successfully", extra={'emoji_type': 'process'})
except Exception as e:
    logger.error(f"Failed to start queue monitoring: {e}", extra={'emoji_type': 'error'})