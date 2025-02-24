import os, glob, shutil, time, threading, requests, subprocess, platform
from core.config import settings
from core.logger import logger
from services.utils import (
    sanitize_filename, strip_status_markers, get_series_folder,
    get_arr_config
)
from services.plex_client import plex

# Global variables
BASE_TITLES = {}
PROGRESS_FLAGS = {}
TIMER_LOCK = threading.Lock()
ACTIVE_SEARCH_TIMERS = {}
LAST_RADARR_SEARCH = {}

# Dummy File Management
def place_dummy_file(media_type, title, year, media_id, target_base_folder, season_number=None, episode_range=None, episode_id=None):
    clean_title = sanitize_filename(title)
    year_str = f" ({year})" if year else ''
    if media_type == 'movie':
        folder_name = f"{clean_title}{year_str} {{tmdb-{media_id}}}{{edition-Dummy}}"
        file_name = f"{clean_title}{year_str} (dummy).mp4"
        target_dir = os.path.join(target_base_folder, folder_name.strip())
    else:
        folder_name = f"{clean_title}{year_str} {{tvdb-{media_id}}}"
        season_str = f"Season {int(season_number):02d}" if season_number else ""
        target_dir = os.path.join(target_base_folder, folder_name.strip(), season_str)
        if episode_range and episode_range[0] == episode_range[1]:
            if episode_id:
                file_name = f"{clean_title} - s{int(season_number):02d}e{int(episode_range[0]):02d} (dummy) [ID:{episode_id}].mp4"
            else:
                logger.warning(f"Episode ID not provided for {title} S{season_number:02d}E{episode_range[0]:02d}", extra={'emoji_type': 'warning'})
                file_name = f"{clean_title} - s{int(season_number):02d}e{int(episode_range[0]):02d} (dummy) [ID:unknown].mp4"
        else:
            ep_range = f"e{episode_range[0]:02d}-e{episode_range[1]:02d}" if episode_range else "e01-e99"
            file_name = f"{clean_title} - s{int(season_number):02d}{ep_range} (dummy).mp4"
    os.makedirs(target_dir, exist_ok=True)
    target_path = os.path.join(target_dir, file_name)
    if os.path.exists(target_path):
        os.remove(target_path)

    try:
        if settings.PLACEHOLDER_STRATEGY == 'copy':
            shutil.copy(settings.DUMMY_FILE_PATH, target_path)
            logger.debug(f"Dummy file copied to: {target_path}", extra={'emoji_type': 'debug'})
        else:  # 'hardlink' strategy (default)
            try:
                os.link(settings.DUMMY_FILE_PATH, target_path)
                logger.debug(f"Dummy file hardlinked to: {target_path}", extra={'emoji_type': 'debug'})
            except OSError:
                logger.warning("Hardlink failed, falling back to copy", extra={'emoji_type': 'warning'})
                shutil.copy(settings.DUMMY_FILE_PATH, target_path)
                logger.debug(f"Dummy file copied to: {target_path} (fallback)", extra={'emoji_type': 'debug'})
    except Exception as e:
        logger.error(f"Failed to create dummy file: {e}", extra={'emoji_type': 'error'})
        raise

    return target_path

def delete_dummy_files(media_type, title, year, media_id, target_base_folder, season_number=None, episode_number=None):
    clean_title = sanitize_filename(title)
    year_str = f" ({year})" if year else ''
    if media_type == 'movie':
        folder_name = f"{clean_title}{year_str} {{tmdb-{media_id}}}"
        target_dir = os.path.join(target_base_folder, folder_name.strip())
        expected_file = os.path.join(target_dir, f"{clean_title}{year_str} (dummy).mp4")
        if os.path.exists(expected_file):
            os.remove(expected_file)
            logger.info(f"Deleted dummy file: {expected_file}", extra={'emoji_type': 'delete'})
        else:
            logger.info(f"No dummy file exists for movie {title}", extra={'emoji_type': 'info'})
    else:
        folder_name = f"{clean_title}{year_str} {{tvdb-{media_id}}}"
        target_dir = os.path.join(target_base_folder, folder_name.strip())
        if season_number:
            target_dir = os.path.join(target_dir, f"Season {int(season_number):02d}")
        if episode_number is not None:
            pattern = os.path.join(target_dir, f"*s{str(season_number).zfill(2)}e{str(episode_number).zfill(2)}*dummy*.mp4")
            for file_path in glob.glob(pattern):
                os.remove(file_path)
                logger.info(f"Deleted dummy file: {file_path}", extra={'emoji_type': 'delete'})
        else:
            logger.info(f"No episode provided; not deleting dummies for {title}", extra={'emoji_type': 'info'})

# Title update and scheduling functions
def schedule_episode_request_update(series_title, season_num, episode_num, media_id, delay=10, retries=5):
    def attempt_update(attempt=1):
        try:
            tv_section = plex.library.sectionByID(settings.PLEX_TV_SECTION_ID)
            show = tv_section.get(series_title)
            if not show:
                logger.debug(f"Show '{series_title}' not found on attempt {attempt}.", extra={'emoji_type': 'debug'})
                if attempt < retries:
                    threading.Timer(3, attempt_update, args=[attempt+1]).start()
                return

            episodes = show.episodes()
            target_ep = next((ep for ep in episodes if int(ep.index) == int(episode_num)), None)
            if target_ep:
                base = strip_status_markers(target_ep.title)
                new_title = f"{base} - [Request]"
                target_ep.editTitle(new_title)
                target_ep.reload()
                logger.info(f"Updated episode title for '{series_title}' S{season_num:02d}E{episode_num:02d} to: {new_title}",
                            extra={'emoji_type': 'update'})
                series_folder = get_series_folder("tv", settings.TV_LIBRARY_FOLDER, series_title, show.year, media_id)
                # persist rating key as needed...
            else:
                if attempt < retries:
                    logger.debug(f"Episode {episode_num} not found in '{series_title}' (attempt {attempt}). Retrying...", extra={'emoji_type': 'debug'})
                    threading.Timer(3, attempt_update, args=[attempt+1]).start()
        except Exception as e:
            logger.error(f"Failed to update '{series_title}' S{season_num:02d}E{episode_num:02d}: {e}", extra={'emoji_type': 'error'})

    threading.Timer(delay, attempt_update).start()

def schedule_movie_request_update(movie_title, media_id, delay=10, retries=5):
    def attempt_update(attempt=1):
        try:
            movie_section = plex.library.sectionByID(settings.PLEX_MOVIE_SECTION_ID)
            item = movie_section.get(movie_title)
            if item:
                base = strip_status_markers(item.title)
                new_title = f"{base} - [Request]"
                item.editTitle(new_title)
                item.reload()
                logger.info(f"Updated movie title for '{movie_title}' to: {new_title}", extra={'emoji_type': 'update'})
                series_folder = get_series_folder("movie", settings.MOVIE_LIBRARY_FOLDER, movie_title, item.year, media_id)
                # persist rating key as needed...
            else:
                if attempt < retries:
                    logger.debug(f"Movie '{movie_title}' not found (attempt {attempt}). Retrying...", extra={'emoji_type': 'debug'})
                    threading.Timer(3, attempt_update, args=[attempt+1]).start()
        except Exception as e:
            logger.error(f"Failed to update movie '{movie_title}': {e}", extra={'emoji_type': 'error'})

    threading.Timer(delay, attempt_update).start()

# Radarr integration functions
def trigger_radarr_search(movie_id, movie_title=None):
    try:
        response = requests.post(f"{settings.RADARR_URL}/command", json={'name': 'MoviesSearch', 'movieIds': [movie_id]}, headers={'X-Api-Key': settings.RADARR_API_KEY})
        response.raise_for_status()
        logger.debug(f"Radarr search triggered for movie id {movie_id}", extra={'emoji_type': 'debug'})
        if movie_title:
            logger.info(f"Triggered search for {movie_title}", extra={'emoji_type': 'search'})
        return True
    except Exception as e:
        logger.error(f"Radarr search failed: {e}", extra={'emoji_type': 'error'})
        return False

def search_in_radarr(tmdb_id, rating_key, is_4k=False):
    """Search for a movie in Radarr"""
    config = get_arr_config('movie', is_4k)
    # Validate tmdb_id is an integer
    try:
        tmdb_id_int = int(tmdb_id)
    except (ValueError, TypeError):
        logger.error(f"Invalid TMDB ID received: {tmdb_id}", extra={'emoji_type': 'error'})
        return False
    try:
        movies_response = requests.get(f"{config['url']}/movie", headers={'X-Api-Key': config['api_key']})
        movies_response.raise_for_status()
        movies = movies_response.json()
        if not isinstance(movies, list):
            logger.error(f"Expected list from Radarr /movie endpoint but got {type(movies)}", extra={'emoji_type': 'error'})
            return False
        
        existing = [m for m in movies if int(m.get("tmdbId", 0)) == tmdb_id_int]
        if existing:
            movie_data = existing[0]
            logger.info(f"Movie already exists in Radarr: {movie_data['title']}", extra={'emoji_type': 'info'})
            if not movie_data.get("monitored", False):
                movie_data["monitored"] = True
                put_response = requests.put(f"{config['url']}/movie/{movie_data['id']}", json=movie_data, headers={'X-Api-Key': config['api_key']})
                put_response.raise_for_status()
                logger.info(f"Movie {movie_data['title']} marked as monitored", extra={'emoji_type': 'monitored'})
            now = time.time()
            if rating_key not in LAST_RADARR_SEARCH or (now - LAST_RADARR_SEARCH[rating_key] >= 30):
                LAST_RADARR_SEARCH[rating_key] = now
                trigger_radarr_search(movie_data['id'], movie_data['title'])
            else:
                logger.debug("Manual search already triggered recently; skipping duplicate search", extra={'emoji_type': 'debug'})
            # Do not schedule further timer retries if TMDB ID is invalid
            return True

        lookup = requests.get(f"{config['url']}/movie/lookup", params={'term': f"tmdb:{tmdb_id_int}"}, headers={'X-Api-Key': config['api_key']})
        lookup.raise_for_status()
        movie_data = lookup.json()[0]
        payload = {
            'title': movie_data['title'],
            'qualityProfileId': 7,
            'tmdbId': int(movie_data['tmdbId']),
            'year': int(movie_data['year']),
            'rootFolderPath': '/mnt/user/data/infinite/movies',
            'monitored': True,
            'addOptions': {
                'searchForMovie': True,
                'addMethod': 'manual',
                'monitor': 'movieOnly'
            }
        }
        response = requests.post(f"{config['url']}/movie", json=payload, headers={'X-Api-Key': config['api_key']})
        response.raise_for_status()
        logger.info(f"Added movie: {movie_data['title']}", extra={'emoji_type': 'success'})
        now = time.time()
        if rating_key not in LAST_RADARR_SEARCH or (now - LAST_RADARR_SEARCH[rating_key] >= 30):
            LAST_RADARR_SEARCH[rating_key] = now
            trigger_radarr_search(response.json()['id'], movie_data['title'])
        else:
            logger.debug("Manual search already triggered recently; skipping duplicate search", extra={'emoji_type': 'debug'})
        return True

    except Exception as e:
        logger.error(f"Radarr operation failed: {e}", extra={'emoji_type': 'error'})
        return False

# Sonarr integration functions would follow a similar pattern.
def search_in_sonarr(tvdb_id, rating_key, episode_mode=False, is_4k=False):
    """Search for a series in Sonarr and optionally trigger a search"""
    try:
        config = get_arr_config('tv', is_4k)
        # First check if series exists
        existing_response = requests.get(
            f"{config['url']}/series", 
            params={'tvdbId': tvdb_id}, 
            headers={'X-Api-Key': config['api_key']}
        )
        existing_response.raise_for_status()
        
        if existing_response.status_code == 200 and existing_response.json():
            series = existing_response.json()[0]
            logger.info(f"Series already exists in Sonarr: {series['title']}", extra={'emoji_type': 'info'})
            
            # Always update monitored status
            if not series.get("monitored", False):
                series["monitored"] = True
                update_response = requests.put(
                    f"{config['url']}/series/{series['id']}", 
                    json=series,
                    headers={'X-Api-Key': config['api_key']}
                )
                update_response.raise_for_status()
                logger.info(f"Series {series['title']} marked as monitored", extra={'emoji_type': 'monitored'})
            
            # In episode mode, just return the series ID, don't trigger search
            if episode_mode:
                return series['id']
                
            # Only trigger series-wide search if not in episode mode
            trigger_sonarr_search(series['id'], series_title=series['title'], is_4k=is_4k)
            return series['id']
        
        # If series doesn't exist, look it up and add it
        lookup_response = requests.get(
            f"{config['url']}/series/lookup", 
            params={'term': f"tvdb:{tvdb_id}"},
            headers={'X-Api-Key': config['api_key']}
        )
        lookup_response.raise_for_status()
        series_data = lookup_response.json()[0]
        
        payload = {
            'title': series_data['title'],
            'qualityProfileId': 3,
            'titleSlug': series_data['titleSlug'],
            'tvdbId': series_data['tvdbId'],
            'year': series_data['year'],
            'rootFolderPath': '/mnt/user/data/infinite/tv',
            'monitored': True,
            'addOptions': {'searchForMissingEpisodes': True},
            'seasons': []
        }
        
        # Add all seasons as monitored
        for season in series_data.get('seasons', []):
            if season.get('seasonNumber', 0) > 0:  # Skip season 0
                payload['seasons'].append({
                    'seasonNumber': season['seasonNumber'],
                    'monitored': True
                })
        
        add_response = requests.post(
            f"{config['url']}/series",
            json=payload,
            headers={'X-Api-Key': config['api_key']}
        )
        add_response.raise_for_status()
        added_series = add_response.json()
        logger.info(f"Added series: {series_data['title']}", extra={'emoji_type': 'success'})
        
        if not episode_mode:
            trigger_sonarr_search(added_series['id'], added_series['title'])
        
        return added_series['id']
        
    except Exception as e:
        logger.error(f"Sonarr operation failed: {e}", extra={'emoji_type': 'error'})
        return None

def trigger_sonarr_search(series_id, season_number=None, episode_ids=None, series_title=None, is_4k=False):
    """Trigger episode search in Sonarr"""
    try:
        config = get_arr_config('tv', is_4k)
        
        # Simplified command structure - this is key
        command = {
            'name': 'EpisodeSearch',
            'episodeIds': [int(episode_ids)] if isinstance(episode_ids, str) else episode_ids
        }

        response = requests.post(
            f"{config['url']}/command",
            json=command,
            headers={'X-Api-Key': config['api_key']}
        )
        response.raise_for_status()
        logger.info(f"Triggered episode search for {series_title or f'series {series_id}'}", 
                   extra={'emoji_type': 'search'})
        return True
    except Exception as e:
        logger.error(f"Sonarr search failed: {e}", extra={'emoji_type': 'error'})
        return False

def trigger_sonarr_episode_search(episode_id):
    """Trigger a specific episode search in Sonarr"""
    try:
        episode_id_int = int(episode_id)
        response = requests.post(
            f"{settings.SONARR_URL}/command",
            json={'name': 'EpisodeSearch', 'episodeIds': [episode_id_int]},
            headers={'X-Api-Key': settings.SONARR_API_KEY}
        )
        response.raise_for_status()
        logger.debug(f"Sonarr episode search triggered for episode id {episode_id_int}", extra={'emoji_type': 'debug'})
        return True
    except Exception as e:
        logger.error(f"Sonarr episode search failed: {e}", extra={'emoji_type': 'error'})
        return False

# Monitoring functions:
def check_media_has_file(media_id, base_title, rating_key, media_type='movie', attempts=0, season_number=None, episode_number=None, start_time=None, is_4k=False):
    """Generic function to check if media has file and monitor downloads"""
    try:
        config = get_arr_config(media_type, is_4k)
        if start_time is None:
            start_time = time.time()
        
        # Handle timeout
        if time.time() - start_time > settings.MAX_MONITOR_TIME:
            try:
                section = plex.library.sectionByID(config['section_id'])
                item = section.fetchItem(int(rating_key))
                base = strip_status_markers(item.title)
                
                new_title = f"{base} - {'Not Available' if PROGRESS_FLAGS.get(f'{rating_key}_retrying', False) else 'Not Found'}"
                logger.error(f"{'Retry' if PROGRESS_FLAGS.get(f'{rating_key}_retrying', False) else 'Initial search'} timeout reached for '{base_title}'", 
                           extra={'emoji_type': 'error'})
                
                item.editTitle(new_title)
                item.reload()
            except Exception as e:
                logger.error(f"Failed to update Plex title on timeout: {e}", extra={'emoji_type': 'error'})
            with TIMER_LOCK:
                ACTIVE_SEARCH_TIMERS.pop(rating_key, None)
            return

        # Query *arr API for media info
        if media_type == 'movie':
            response = requests.get(f"{config['url']}/movie", headers={'X-Api-Key': config['api_key']})
            response.raise_for_status()
            items = response.json()
            target_item = next((m for m in items if int(m.get(config['id_type'], 0)) == int(media_id)), None)
            item_id = target_item['id'] if target_item else None
        else:
            # Get series first, then episode
            series_response = requests.get(f"{config['url']}/series", params={config['id_type']: media_id}, 
                                        headers={'X-Api-Key': config['api_key']})
            series_response.raise_for_status()
            series_list = series_response.json()
            
            if series_list:
                series = series_list[0]
                episodes_response = requests.get(f"{config['url']}/episode", params={'seriesId': series['id']}, 
                                              headers={'X-Api-Key': config['api_key']})
                episodes_response.raise_for_status()
                episodes = episodes_response.json()

                # Filter episodes based on search type
                if config['search_type'] == 'episode':
                    target_episodes = [ep for ep in episodes 
                                    if int(ep.get('seasonNumber', 0)) == int(season_number)
                                    and int(ep.get('episodeNumber', 0)) == int(episode_number)]
                elif config['search_type'] == 'season':
                    target_episodes = [ep for ep in episodes 
                                    if int(ep.get('seasonNumber', 0)) == int(season_number)]
                else:  # series
                    target_episodes = episodes

                # Check if all target episodes have files
                all_available = all(ep.get('hasFile', False) for ep in target_episodes)
                any_downloading = False
                progress = 0
                downloading_count = 0

                # Check queue status for all relevant episodes
                queue_response = requests.get(f"{config['url']}/queue", headers={'X-Api-Key': config['api_key']})
                queue_response.raise_for_status()
                queue_data = queue_response.json()
                queue_items = queue_data.get('records', [])
                
                for ep in target_episodes:
                    queue_item = next((qi for qi in queue_items if qi.get(config['queue_id_field']) == ep.get('id')), None)
                    if queue_item:
                        any_downloading = True
                        downloading_count += 1
                        progress += (1 - (queue_item.get('sizeleft', 0) / queue_item.get('size', 1))) * 100

                # Update Plex title based on status
                section = plex.library.sectionByID(config['section_id'])
                item = section.fetchItem(int(rating_key))
                base = strip_status_markers(item.title)

                if all_available:
                    new_title = f"{base} - Available"
                    item.editTitle(new_title)
                    item.reload()
                    logger.info(f"Updated Plex title to Available for '{base_title}'", extra={'emoji_type': 'info'})
                    delete_dummy_files(media_type, base_title, series.get('year'), media_id, 
                                    config['library_folder'], season_number, episode_number)
                    with TIMER_LOCK:
                        ACTIVE_SEARCH_TIMERS.pop(rating_key, None)
                    PROGRESS_FLAGS.pop(rating_key, None)
                    return
                elif any_downloading:
                    # Kill search timer on first download detection
                    if not PROGRESS_FLAGS.get(rating_key, False):
                        with TIMER_LOCK:
                            ACTIVE_SEARCH_TIMERS.pop(rating_key, None)
                        logger.info(f"Search completed successfully for {base_title}, monitoring download", 
                                  extra={'emoji_type': 'success'})

                    avg_progress = progress / downloading_count if downloading_count > 0 else 0
                    new_title = f"{base} - Downloading {int(avg_progress)}%"
                    PROGRESS_FLAGS[rating_key] = True
                    logger.info(f"Download progress for {base_title}: {int(avg_progress)}%", extra={'emoji_type': 'progress'})
                    item.editTitle(new_title)
                    item.reload()
                else:
                    # Handle searching/retrying states
                    if PROGRESS_FLAGS.get(rating_key, False):
                        start_time = time.time()
                        new_title = f"{base} - Retrying..."
                        PROGRESS_FLAGS[rating_key] = False
                        PROGRESS_FLAGS[f"{rating_key}_retrying"] = True
                        logger.info(f"Queue item disappeared for {base_title}. Starting new search.", 
                                  extra={'emoji_type': 'warning'})
                    elif PROGRESS_FLAGS.get(f"{rating_key}_retrying", False):
                        new_title = f"{base} - Retrying..."
                        logger.debug(f"Still retrying search for {base_title}", extra={'emoji_type': 'debug'})
                    else:
                        new_title = f"{base} - Searching..."
                        logger.debug(f"No queue item found for {base_title}; still searching.", 
                                   extra={'emoji_type': 'debug'})
                    item.editTitle(new_title)
                    item.reload()

        # Continue polling
        if attempts < settings.CHECK_MAX_ATTEMPTS:
            timer = threading.Timer(settings.CHECK_INTERVAL, check_media_has_file, 
                                 args=[media_id, base_title, rating_key, media_type, attempts+1, 
                                       season_number, episode_number, start_time])
            with TIMER_LOCK:
                ACTIVE_SEARCH_TIMERS[rating_key] = timer
            timer.start()
        else:
            logger.error(f"Maximum attempts reached for file check of '{base_title}'", extra={'emoji_type': 'error'})
            try:
                item = plex.fetchItem(rating_key)
                base = strip_status_markers(item.title)
                new_title = f"{base} - Not Found"
                item.editTitle(new_title)
                item.reload()
            except Exception as e:
                logger.error(f"Failed to update Plex title on max attempts: {e}", extra={'emoji_type': 'error'})
            with TIMER_LOCK:
                ACTIVE_SEARCH_TIMERS.pop(rating_key, None)

    except Exception as e:
        logger.error(f"{media_type.title()} file check failed: {e}", extra={'emoji_type': 'error'})
        with TIMER_LOCK:
            ACTIVE_SEARCH_TIMERS.pop(rating_key, None)

def check_has_file(tmdb_id, base_title, rating_key, attempts=0, start_time=None):
    return check_media_has_file(tmdb_id, base_title, rating_key, 'movie', attempts, start_time=start_time)

def check_tv_has_file(tvdb_id, base_title, rating_key, attempts=0, season_number=None, episode_number=None, start_time=None):
    return check_media_has_file(tvdb_id, base_title, rating_key, 'episode', attempts, season_number, episode_number, start_time)

# For brevity, any additional integration functions (including Sonarr functions) are implemented similarly.
def update_plex_title(rating_key, base_title, status):
    """Update a Plex item's title using PlexAPI directly rather than URL construction"""
    try:
        # Get the item directly using PlexAPI
        item = plex.fetchItem(int(rating_key))
        base_title = strip_status_markers(base_title)
        new_title = f"{base_title} - {status}"
        # Use PlexAPI's built-in title update
        item.editTitle(new_title)
        item.reload()
        logger.info(f"Updated Plex title to: {new_title}", extra={'emoji_type': 'update'})
    except Exception as e:
        logger.error(f"Failed to update Plex title for {rating_key}: {str(e)}", extra={'emoji_type': 'error'})
