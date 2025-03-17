import os, glob, shutil, time, threading, requests, subprocess, platform, re, fnmatch
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

def get_folder_path(media_type, base_path, title, year=None, media_id=None, season=None):
    """Generate folder path according to the convention"""
    if media_type == "movie":
        # Movie folder: "{Movie Title} ({Year}) {tmdb-123456}{edition-Dummy}"
        folder_name = f"{sanitize_filename(title)} ({year}) {{tmdb-{media_id}}}{{edition-Dummy}}"
        return os.path.join(base_path, folder_name)
    else:
        # Series folder: "{Series Title} ({year}) {tvdb-123456} (dummy)"
        folder_name = f"{sanitize_filename(title)} ({year}) {{tvdb-{media_id}}} (dummy)"
        # Add season folder
        season_folder = f"Season {season:02d}"
        return os.path.join(base_path, folder_name, season_folder)

def place_dummy_file(media_type, title, year=None, media_id=None, base_path=None, 
                    season_number=None, episode_range=None, episode_title=None, episode_id=None):
    """Create a dummy file in the appropriate location with the appropriate naming"""
    try:
        # Determine the base path if not provided
        if not base_path:
            if media_type == "movie":
                base_path = settings.MOVIE_LIBRARY_FOLDER
            else:
                base_path = settings.TV_LIBRARY_FOLDER
                
        # Prepare folder path
        folder_name = sanitize_filename(title)
        if year:
            folder_name += f" ({year})"
        
        # Add appropriate ID tag and dummy marker
        if media_type == 'tv':
            folder_name += f" {{tvdb-{media_id}}} (dummy)"
            
            # TV show - create season folder structure
            if season_number is not None:
                # Create season folder
                season_folder = f"Season {int(season_number):02d}"
                episode_folder = os.path.join(base_path, folder_name, season_folder)
                
                # Create directories if they don't exist
                os.makedirs(episode_folder, exist_ok=True)
                
                # If episode range is specified, create dummy files for each episode
                if episode_range:
                    start, end = episode_range
                    for episode_num in range(start, end + 1):
                        # Create file name with lowercase s##e## pattern
                        file_name = f"{title} - s{int(season_number):02d}e{int(episode_num):02d}.mp4"
                        if episode_title and episode_num == start:
                            # Include episode title for first episode if available
                            file_name = f"{title} - s{int(season_number):02d}e{int(episode_num):02d} - {episode_title}.mp4"
                        
                        file_path = os.path.join(episode_folder, sanitize_filename(file_name))
                        
                        # Create empty file if it doesn't exist
                        if not os.path.exists(file_path):
                            with open(file_path, 'w') as f:
                                # Add minimal metadata
                                f.write(f"Placeholder for {title} S{season_number}E{episode_num}")
                            
                            logger.info(f"Created dummy file for {title} S{season_number}E{episode_num} at {file_path}", 
                                      extra={'emoji_type': 'file'})
                        else:
                            logger.info(f"Re-created dummy file for {title} S{season_number}E{episode_num} at {file_path}", 
                                      extra={'emoji_type': 'file'})
                        
                        # Register this file with monitoring
                        from services.queue_monitor import add_to_monitor
                        if episode_id:
                            add_to_monitor('episode', title, episode_id=episode_id, tvdb_id=media_id,
                                      season_number=season_number, episode_number=episode_num)
                            
                        # Return the path of the first episode file
                        if episode_num == start:
                            first_episode_path = file_path
                
                return first_episode_path
                
        else:  # Movie
            folder_name += f" {{tmdb-{media_id}}} (dummy)"
            
            # Create movie folder
            movie_folder = os.path.join(base_path, folder_name)
            os.makedirs(movie_folder, exist_ok=True)
            
            # Create dummy movie file with title (Year) format
            file_name = f"{title} ({year}).mp4"
            file_path = os.path.join(movie_folder, sanitize_filename(file_name))
            
            # Create empty file if it doesn't exist
            if not os.path.exists(file_path):
                with open(file_path, 'w') as f:
                    f.write(f"Placeholder for {title} ({year})")
                    
                logger.info(f"Created dummy movie file for {title} ({year}) at {file_path}", 
                          extra={'emoji_type': 'file'})
            else:
                logger.info(f"Re-created dummy movie file for {title} ({year}) at {file_path}", 
                          extra={'emoji_type': 'file'})
                
            return file_path
                
    except Exception as e:
        logger.error(f"Error creating dummy file: {str(e)}", extra={'emoji_type': 'error'})
        return None

def cleanup_episode_placeholder(series_title, series_year=None, season_number=None, episode_number=None):
    """Remove placeholder files for a specific episode or series"""
    try:
        # Construct the dummy folder name (same logic as in place_dummy_file)
        folder_name = series_title
        if series_year:
            folder_name += f" ({series_year})"
        folder_name += " {tvdb-*} (dummy)"
        
        logger.debug(f"Cleaning up placeholders for {series_title} ({series_year})", extra={'emoji_type': 'debug'})
        
        # Get the TV library path
        tv_library_path = settings.TV_LIBRARY_FOLDER
        
        # Search for matching dummy folders
        matching_folders = []
        for item in os.listdir(tv_library_path):
            item_path = os.path.join(tv_library_path, item)
            
            # Use glob pattern matching
            if fnmatch.fnmatch(item, folder_name) and os.path.isdir(item_path):
                matching_folders.append(item_path)
                logger.debug(f"Found matching dummy folder: {item_path}", extra={'emoji_type': 'debug'})
        
        if not matching_folders:
            logger.debug(f"No matching dummy folders found for {folder_name}", extra={'emoji_type': 'debug'})
            return False
        
        # If no specific episode is provided, clean up the entire series
        if season_number is None or episode_number is None:
            for folder_path in matching_folders:
                shutil.rmtree(folder_path)
                logger.info(f"Removed dummy folder: {folder_path}", extra={'emoji_type': 'cleanup'})
            return True
        
        # Otherwise, clean up just the specific episode
        season_folder = f"Season {season_number:02d}"
        episode_pattern = f"*s{season_number:02d}e{episode_number:02d}*"
        
        logger.debug(f"Looking for episode files matching: {episode_pattern} in season folder: {season_folder}", 
                   extra={'emoji_type': 'debug'})
        
        found_files = False
        for folder_path in matching_folders:
            season_path = os.path.join(folder_path, season_folder)
            
            if os.path.exists(season_path):
                logger.debug(f"Found season folder: {season_path}", extra={'emoji_type': 'debug'})
                
                for item in os.listdir(season_path):
                    logger.debug(f"Checking file: {item} against pattern: {episode_pattern}", extra={'emoji_type': 'debug'})
                    
                    if fnmatch.fnmatch(item.lower(), episode_pattern.lower()):
                        file_path = os.path.join(season_path, item)
                        os.remove(file_path)
                        logger.info(f"Removed placeholder: {file_path}", extra={'emoji_type': 'cleanup'})
                        found_files = True
            else:
                logger.debug(f"Season folder not found: {season_path}", extra={'emoji_type': 'debug'})
        
        if not found_files:
            logger.debug(f"No matching episode files found for {episode_pattern}", extra={'emoji_type': 'debug'})
            
        return True
        
    except Exception as e:
        logger.error(f"Error cleaning up placeholder: {str(e)}", extra={'emoji_type': 'error'})
        return False

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

def search_in_radarr(tmdb_id, rating_key, is_4k=False, title=None, imdb_id=None, year=None):
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
def search_in_sonarr(tvdb_id, rating_key, season_number=None, episode_number=None, is_4k=False):
    """Search for a series in Sonarr but don't automatically mark as monitored"""
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
        
        return added_series['id']
        
    except Exception as e:
        logger.error(f"Sonarr operation failed: {e}", extra={'emoji_type': 'error'})
        return None

def trigger_sonarr_search(series_id, season_number=None, episode_ids=None, series_title="Unknown Series", is_4k=False):
    """Trigger a search in Sonarr for episodes"""
    try:
        # Get Sonarr configuration
        config = get_arr_config('sonarr', is_4k)
        url = config.get('url')
        api_key = config.get('api_key')
        
        if not url or not api_key:
            logger.error("Sonarr configuration missing", extra={'emoji_type': 'error'})
            return False
            
        headers = {'X-Api-Key': api_key}
        
        # Determine search type and prepare request
        if episode_ids:
            # Search for specific episodes (batch support)
            data = {
                'name': 'episodeSearch',
                'episodeIds': episode_ids
            }
            log_message = f"Triggered episode search for {series_title} ({len(episode_ids)} episodes)"
            
        elif season_number is not None:
            # Search for a season
            data = {
                'name': 'seasonSearch',
                'seriesId': series_id,
                'seasonNumber': season_number
            }
            log_message = f"Triggered season search for {series_title} S{season_number:02d}"
            
        else:
            # Search for entire series
            data = {
                'name': 'seriesSearch',
                'seriesId': series_id
            }
            log_message = f"Triggered series search for {series_title}"
            
        # Send search command to Sonarr
        r = requests.post(f"{url}/command", json=data, headers=headers)
        r.raise_for_status()
        
        # Log single message for the search operation
        logger.info(log_message, extra={'emoji_type': 'search'})
        
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

def get_episodes_for_lookahead(series_id, current_season, current_episode, lookahead=5):
    """
    Get episodes for lookahead processing with proper range limiting and specials handling
    """
    logger.debug(f"Selecting episodes starting from S{current_season}E{current_episode} with lookahead {lookahead}", 
                extra={'emoji_type': 'debug'})
    
    # Get all episodes for the series from Sonarr
    url = f"{settings.SONARR_URL}/episode"
    params = {'seriesId': series_id}
    headers = {'X-Api-Key': settings.SONARR_API_KEY}
    
    try:
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        all_episodes = response.json()
    except Exception as e:
        logger.error(f"Failed to fetch episodes: {str(e)}", extra={'emoji_type': 'error'})
        return [], False
    
    # Determine if we include specials
    include_specials = getattr(settings, 'INCLUDE_SPECIALS', False)
    
    # Filter episodes based on season number
    if include_specials:
        episodes = all_episodes
    else:
        episodes = [ep for ep in all_episodes if ep.get('seasonNumber', 0) > 0]
    
    # Find the absolute last episode in the series
    if episodes:
        last_episode = max(episodes, key=lambda x: (x.get('seasonNumber', 0), x.get('episodeNumber', 0)))
        last_season = last_episode.get('seasonNumber', 0)
        last_episode_num = last_episode.get('episodeNumber', 0)
    else:
        last_season = 0
        last_episode_num = 0
    
    # Get max episode number for each season for range calculation
    max_episodes_by_season = {}
    for ep in episodes:
        season = ep.get('seasonNumber', 0)
        episode = ep.get('episodeNumber', 0)
        max_episodes_by_season[season] = max(episode, max_episodes_by_season.get(season, 0))
    
    # Calculate the end point of the lookahead range
    range_end_season = current_season
    range_end_episode = current_episode + lookahead
    
    # If we exceed episode count in this season, roll over to next season
    while (range_end_season in max_episodes_by_season and 
           range_end_episode > max_episodes_by_season[range_end_season]):
        # Calculate how many episodes to carry over
        overflow = range_end_episode - max_episodes_by_season[range_end_season]
        # Move to next season
        range_end_season += 1
        # Start from episode 1, plus overflow
        range_end_episode = overflow
    
    # Check if our range extends to or beyond the last episode
    reached_end = (range_end_season > last_season or 
                  (range_end_season == last_season and range_end_episode >= last_episode_num))
    
    # Filter episodes within range that don't have files
    filtered_episodes = []
    for ep in episodes:
        season = ep.get('seasonNumber', 0)
        episode = ep.get('episodeNumber', 0)
        
        # Episode is within range if:
        # 1. It's after current position (same season & later episode OR later season)
        # 2. It's within the end range boundary
        # 3. It doesn't have a file
        if ((season > current_season or (season == current_season and episode >= current_episode)) and
            (season < range_end_season or (season == range_end_season and episode <= range_end_episode)) and
            not ep.get('hasFile', False)):
            filtered_episodes.append(ep)
    
    # Log the episodes we're going to monitor
    if filtered_episodes:
        start_ep = filtered_episodes[0]
        end_ep = filtered_episodes[-1]
        start_season = start_ep.get('seasonNumber')
        start_episode = start_ep.get('episodeNumber')
        end_season = end_ep.get('seasonNumber')
        end_episode = end_ep.get('episodeNumber')
        
        if start_season == end_season:
            logger.info(f"Episode Selection: Monitoring S{start_season}E{start_episode}-E{end_episode}", 
                       extra={'emoji_type': 'info'})
        else:
            logger.info(f"Episode Selection: Monitoring episodes across seasons S{start_season}E{start_episode} to S{end_season}E{end_episode}", 
                       extra={'emoji_type': 'info'})
        
        if reached_end:
            logger.info("End of Episodes Detection: Reached end of known episodes, will mark entire series as monitored", 
                       extra={'emoji_type': 'info'})
    else:
        logger.warning("No episodes found to monitor", extra={'emoji_type': 'warning'})
    
    return filtered_episodes, reached_end

def monitor_episodes(series_id, episode_ids, monitor=True):
    """Mark multiple episodes as monitored/unmonitored in batch"""
    try:
        # Get episode details first to preserve other properties
        url = f"{settings.SONARR_URL}/episode"
        params = {'seriesId': series_id}
        headers = {'X-Api-Key': settings.SONARR_API_KEY}
        
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        episodes = response.json()
        
        # Filter to requested episodes and update monitored status
        to_update = [
            {**ep, 'monitored': monitor}
            for ep in episodes if ep['id'] in episode_ids
        ]
        
        # Update episodes in batch
        if to_update:
            for ep in to_update:
                update_url = f"{settings.SONARR_URL}/episode/{ep['id']}"
                update_response = requests.put(update_url, json=ep, headers=headers)
                update_response.raise_for_status()
                
            logger.info(f"Marked {len(to_update)} episodes as {'monitored' if monitor else 'unmonitored'}", 
                      extra={'emoji_type': 'monitored'})
            return True
        return False
    except Exception as e:
        logger.error(f"Failed to update episode monitored status: {str(e)}", extra={'emoji_type': 'error'})
        return False

def mark_series_monitored(series_id, mark_seasons=False, include_specials=False):
    """Mark series as monitored, with options to control season monitoring"""
    try:
        # Get series details
        url = f"{settings.SONARR_URL}/series/{series_id}"
        headers = {'X-Api-Key': settings.SONARR_API_KEY}
        
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        series = response.json()
        
        # Always mark series as monitored
        series['monitored'] = True
        
        # Optionally mark seasons as monitored
        if mark_seasons:
            for season in series.get('seasons', []):
                season_number = season.get('seasonNumber', -1)
                # Mark normal seasons, only mark specials if requested
                if season_number > 0 or (season_number == 0 and include_specials):
                    season['monitored'] = True
        
        # Update the series
        update_response = requests.put(url, json=series, headers=headers)
        update_response.raise_for_status()
        
        log_message = f"Marked series '{series.get('title')}' as monitored"
        if mark_seasons:
            log_message += " with all seasons"
            if not include_specials:
                log_message += " (except specials)"
        logger.info(log_message, extra={'emoji_type': 'monitored'})
        return True
    except Exception as e:
        logger.error(f"Failed to mark series as monitored: {str(e)}", extra={'emoji_type': 'error'})
        return False

def monitor_season(series_id, season_number):
    """Mark a specific season as monitored"""
    try:
        # Get series details
        url = f"{settings.SONARR_URL}/series/{series_id}"
        headers = {'X-Api-Key': settings.SONARR_API_KEY}
        
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        series = response.json()
        
        # Mark series as monitored
        series['monitored'] = True
        
        # Mark the specific season as monitored
        for season in series.get('seasons', []):
            if season.get('seasonNumber') == int(season_number):
                season['monitored'] = True
                break
        
        # Update the series
        update_response = requests.put(url, json=series, headers=headers)
        update_response.raise_for_status()
        
        logger.info(f"Marked season {season_number} of '{series.get('title')}' as monitored", 
                  extra={'emoji_type': 'monitored'})
        return True
    except Exception as e:
        logger.error(f"Failed to mark season as monitored: {str(e)}", extra={'emoji_type': 'error'})
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
                episodes = episodes.response.json()

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
                    
                    # Make sure we use the actual title, not a placeholder
                    display_title = strip_status_markers(base_title)
                    if '{episode_title}' in base_title:
                        display_title = f"Episode S{season_number:02d}E{episode_number:02d}"
                    
                    logger.info(f"Updated Plex title to Available for '{display_title}'", 
                              extra={'emoji_type': 'info'})
                    
                    # Delete placeholder files when download is complete
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
                    
                    # Format proper title for logging
                    display_title = strip_status_markers(base_title)
                    if '{episode_title}' in base_title:
                        display_title = f"Episode S{season_number:02d}E{episode_number:02d}"
                        
                    logger.info(f"Download progress for {display_title}: {int(avg_progress)}%", 
                              extra={'emoji_type': 'progress'})
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
                        logger.debug(f"No queue item found for {base_title}, still searching.", 
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

def check_tv_has_file(tvdb_id, base_title, rating_key, attempts=0, season_number=None, episode_number=None, start_time=None, is_4k=False):
    """Monitor episode download status and update Plex title accordingly"""
    try:
        config = get_arr_config('tv', is_4k)
        if start_time is None:
            start_time = time.time()
        
        # Handle timeout
        if time.time() - start_time > settings.MAX_MONITOR_TIME:
            try:
                section = plex.library.sectionByID(config['section_id'])
                item = section.fetchItem(int(rating_key))
                base = strip_status_markers(item.title)
                
                new_title = f"{base} - {'Not Available' if PROGRESS_FLAGS.get(f'{rating_key}_retrying', False) else 'Not Found'}"
                logger.error(f"{'Retry' if PROGRESS_FLAGS.get(f'{rating_key}_retrying', False) else 'Initial search'} timeout reached for episode S{season_number}E{episode_number}", 
                           extra={'emoji_type': 'error'})
                
                item.editTitle(new_title)
                item.reload()
            except Exception as e:
                logger.error(f"Failed to update Plex title on timeout: {e}", extra={'emoji_type': 'error'})
            with TIMER_LOCK:
                ACTIVE_SEARCH_TIMERS.pop(f"{rating_key}_{season_number}_{episode_number}", None)
            return

        # Get series first
        series_response = requests.get(f"{config['url']}/series", params={config['id_type']: tvdb_id}, 
                                    headers={'X-Api-Key': config['api_key']})
        series_response.raise_for_status()
        series_list = series_response.json()
        
        if not series_list:
            logger.error(f"Series with TVDB ID {tvdb_id} not found", extra={'emoji_type': 'error'})
            with TIMER_LOCK:
                ACTIVE_SEARCH_TIMERS.pop(f"{rating_key}_{season_number}_{episode_number}", None)
            return
            
        series = series_list[0]
        
        # Get the specific episode
        episodes_response = requests.get(f"{config['url']}/episode", params={'seriesId': series['id']}, 
                                      headers={'X-Api-Key': config['api_key']})
        episodes_response.raise_for_status()
        episodes = episodes_response.json()
        
        # Find the target episode
        target_episode = next((ep for ep in episodes 
                            if int(ep.get('seasonNumber', 0)) == int(season_number)
                            and int(ep.get('episodeNumber', 0)) == int(episode_number)), None)
                            
        if not target_episode:
            logger.error(f"Episode S{season_number}E{episode_number} not found for series {series['title']}", 
                       extra={'emoji_type': 'error'})
            with TIMER_LOCK:
                ACTIVE_SEARCH_TIMERS.pop(f"{rating_key}_{season_number}_{episode_number}", None)
            return
                            
        # Check if episode has file
        has_file = target_episode.get('hasFile', False)

        # Check queue for download status
        queue_response = requests.get(f"{config['url']}/queue", headers={'X-Api-Key': config['api_key']})
        queue_response.raise_for_status()
        queue_data = queue_response.json()
        queue_items = queue_data.get('records', [])
        
        # Find this episode in queue
        queue_item = next((qi for qi in queue_items 
                        if qi.get('episodeId') == target_episode.get('id')), None)
        
        # Get Plex item for title updates
        section = plex.library.sectionByID(config['section_id'])
        item = section.fetchItem(int(rating_key))
        base = strip_status_markers(item.title)
        episode_key = f"{rating_key}_{season_number}_{episode_number}"

        # Handle status updates
        if has_file:
            new_title = f"{base} - Available"
            item.editTitle(new_title)
            item.reload()
            
            logger.info(f"Updated Plex title to Available for '{series['title']}' S{season_number}E{episode_number}", 
                      extra={'emoji_type': 'info'})
            
            # Delete placeholder file
            delete_dummy_files('tv', series['title'], series.get('year'), tvdb_id, 
                            config['library_folder'], season_number, episode_number)
            
            with TIMER_LOCK:
                ACTIVE_SEARCH_TIMERS.pop(episode_key, None)
            PROGRESS_FLAGS.pop(episode_key, None)
            return
            
        elif queue_item:
            # Kill search timer on first download detection
            if not PROGRESS_FLAGS.get(episode_key, False):
                with TIMER_LOCK:
                    ACTIVE_SEARCH_TIMERS.pop(episode_key, None)
                logger.info(f"Search completed successfully for {series['title']} S{season_number}E{episode_number}, monitoring download", 
                          extra={'emoji_type': 'success'})

            progress = (1 - (queue_item.get('sizeleft', 0) / queue_item.get('size', 1))) * 100
            new_title = f"{base} - Downloading {int(progress)}%"
            PROGRESS_FLAGS[episode_key] = True
            
            logger.info(f"Download progress for {series['title']} S{season_number}E{episode_number}: {int(progress)}%", 
                      extra={'emoji_type': 'progress'})
            item.editTitle(new_title)
            item.reload()
            
        else:
            # Handle searching/retrying states
            if PROGRESS_FLAGS.get(episode_key, False):
                start_time = time.time()
                new_title = f"{base} - Retrying..."
                PROGRESS_FLAGS[episode_key] = False
                PROGRESS_FLAGS[f"{episode_key}_retrying"] = True
                logger.info(f"Queue item disappeared for {series['title']} S{season_number}E{episode_number}. Starting new search.", 
                          extra={'emoji_type': 'warning'})
            elif PROGRESS_FLAGS.get(f"{episode_key}_retrying", False):
                new_title = f"{base} - Retrying..."
                logger.debug(f"Still retrying search for {series['title']} S{season_number}E{episode_number}", 
                           extra={'emoji_type': 'debug'})
            else:
                new_title = f"{base} - Searching..."
                logger.debug(f"No queue item found for {series['title']} S{season_number}E{episode_number}, still searching.", 
                           extra={'emoji_type': 'debug'})
            
            item.editTitle(new_title)
            item.reload()

        # Continue polling
        if attempts < settings.CHECK_MAX_ATTEMPTS:
            timer = threading.Timer(
                settings.CHECK_INTERVAL, 
                check_tv_has_file, 
                args=[tvdb_id, base_title, rating_key, attempts+1, season_number, episode_number, start_time, is_4k]
            )
            with TIMER_LOCK:
                ACTIVE_SEARCH_TIMERS[episode_key] = timer
            timer.start()
        else:
            logger.error(f"Maximum attempts reached for episode {series['title']} S{season_number}E{episode_number}", 
                       extra={'emoji_type': 'error'})
            try:
                item = plex.fetchItem(rating_key)
                base = strip_status_markers(item.title)
                new_title = f"{base} - Not Found"
                item.editTitle(new_title)
                item.reload()
            except Exception as e:
                logger.error(f"Failed to update Plex title on max attempts: {e}", extra={'emoji_type': 'error'})
            with TIMER_LOCK:
                ACTIVE_SEARCH_TIMERS.pop(episode_key, None)

    except Exception as e:
        logger.error(f"Episode status check failed: {e}", extra={'emoji_type': 'error'})
        with TIMER_LOCK:
            ACTIVE_SEARCH_TIMERS.pop(f"{rating_key}_{season_number}_{episode_number}", None)

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

def check_has_file(media_type, arr_id, title, rating_key, is_4k=False, attempts=0, start_time=None):
    """Movie-specific wrapper for check_media_has_file"""
    return check_media_has_file(media_type, arr_id, title, rating_key, is_4k=is_4k, attempts=attempts, start_time=start_time)

def get_sonarr_queue(is_4k=False):
    """Get current queue items from Sonarr"""
    try:
        base_url = settings.SONARR_4K_URL if is_4k else settings.SONARR_URL
        api_key = settings.SONARR_4K_API_KEY if is_4k else settings.SONARR_API_KEY
        
        url = f"{base_url}/queue"
        params = {'pageSize': 50}
        headers = {'X-Api-Key': api_key}
        
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        
        data = response.json()
        return data.get('records', [])
    
    except Exception as e:
        logger.error(f"Error fetching Sonarr queue: {e}", extra={'emoji_type': 'error'})
        return []

def get_radarr_queue(is_4k=False):
    """Get current queue items from Radarr"""
    try:
        base_url = settings.RADARR_4K_URL if is_4k else settings.RADARR_URL
        api_key = settings.RADARR_4K_API_KEY if is_4k else settings.RADARR_API_KEY
        
        url = f"{base_url}/queue"
        params = {'pageSize': 50}
        headers = {'X-Api-Key': api_key}
        
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        
        data = response.json()
        return data.get('records', [])
    
    except Exception as e:
        logger.error(f"Error fetching Radarr queue: {e}", extra={'emoji_type': 'error'})
        return []

def search_in_sonarr(tvdb_id=None, title=None, year=None, rating_key=None, season_number=None, episode_number=None, is_4k=False, file_path=None):
    """
    Find a TV series in Sonarr using multiple fallback methods:
    1. Path-based ID matching (most reliable)
    2. TVDB ID matching
    3. Title matching
    Returns the series ID if found, None otherwise
    """
    try:
        # Determine which Sonarr instance to use
        sonarr_url = settings.SONARR_URL_4K if is_4k else settings.SONARR_URL
        sonarr_api_key = settings.SONARR_API_KEY_4K if is_4k else settings.SONARR_API_KEY
        headers = {"X-Api-Key": sonarr_api_key}
        
        # Get all series from Sonarr for efficient matching
        series_url = f"{sonarr_url}/series"
        series_response = requests.get(series_url, headers=headers)
        
        if series_response.status_code != 200:
            logger.error(f"Failed to get series from Sonarr: {series_response.text}", extra={'emoji_type': 'error'})
            return None
            
        all_series = series_response.json()
        
        # METHOD 1: Try to match by filepath ID (most reliable)
        if file_path:
            # Extract ID using regex pattern matching
            imdb_match = re.search(r'{imdb-([^}]+)}', file_path)
            tvdb_match = re.search(r'{tvdb-(\d+)}', file_path)
            tmdb_match = re.search(r'{tmdb-(\d+)}', file_path)
            
            if imdb_match:
                path_id = imdb_match.group(1)
                for series in all_series:
                    if series.get('imdbId') == path_id:
                        logger.info(f"Found series in Sonarr by path IMDB ID: {series['title']}", extra={'emoji_type': 'info'})
                        return series['id']
            
            if tvdb_match:
                path_id = tvdb_match.group(1)
                for series in all_series:
                    if str(series.get('tvdbId')) == str(path_id):
                        logger.info(f"Found series in Sonarr by path TVDB ID: {series['title']}", extra={'emoji_type': 'info'})
                        return series['id']
            
            if tmdb_match:
                path_id = tmdb_match.group(1)
                for series in all_series:
                    if str(series.get('tmdbId')) == str(path_id):
                        logger.info(f"Found series in Sonarr by path TMDB ID: {series['title']}", extra={'emoji_type': 'info'})
                        return series['id']
                        
            # Also try to match by series folder name in path
            path_parts = file_path.split('/')
            for idx, part in enumerate(path_parts):
                if idx > 0 and idx < len(path_parts) - 1 and 'Season' in path_parts[idx+1]:
                    series_folder = part
                    for series in all_series:
                        if series_folder in series.get('path', ''):
                            logger.info(f"Found series in Sonarr by folder path match: {series['title']}", extra={'emoji_type': 'info'})
                            return series['id']
        
        # METHOD 2: Try TVDB ID matching (next most reliable)
        if tvdb_id:
            for series in all_series:
                if str(series.get('tvdbId')) == str(tvdb_id):
                    logger.info(f"Found series in Sonarr by TVDB ID: {series['title']}", extra={'emoji_type': 'info'})
                    return series['id']
        
        # METHOD 3: Try title matching (least reliable but good fallback)
        if title:
            # Try exact match first
            for series in all_series:
                if series.get('title', '').lower() == title.lower():
                    logger.info(f"Found series in Sonarr by title: {series['title']}", extra={'emoji_type': 'info'})
                    return series['id']
        
        # If we get here, series wasn't found
        logger.warning(f"Series not found in Sonarr: {'TVDB:'+str(tvdb_id) if tvdb_id else title}", extra={'emoji_type': 'warning'})
        return None
        
    except Exception as e:
        logger.error(f"Error finding series in Sonarr: {e}", extra={'emoji_type': 'error'})
        return None

def delete_dummy_files(media_type, title, year, tvdb_id=None, library_path=None, season_number=None, episode_number=None):
    """Delete placeholder files once real files are downloaded"""
    try:
        # Build the folder pattern
        folder_name = sanitize_filename(title)
        if year:
            folder_name += f" ({year})"
        
        # Add appropriate ID tag
        if media_type == 'tv':
            folder_name += f" {{tvdb-{tvdb_id}}} (dummy)"
        else:  # movie
            folder_name += f" {{tmdb-{tvdb_id}}} (dummy)"
            
        dummy_folder = os.path.join(library_path, folder_name)
        logger.debug(f"Looking for dummy folder: {dummy_folder}", extra={'emoji_type': 'debug'})
        
        # Check if the folder exists
        if not os.path.exists(dummy_folder):
            logger.debug(f"Dummy folder not found: {dummy_folder}", extra={'emoji_type': 'debug'})
            return
        
        # TV show - delete specific episode file
        if media_type == 'tv' and season_number is not None and episode_number is not None:
            season_dir = os.path.join(dummy_folder, f"Season {int(season_number):02d}")
            
            # Check if season folder exists
            if os.path.exists(season_dir):
                # Log what we're looking for
                logger.debug(f"Looking for episode files in {season_dir} matching pattern s{season_number:02d}e{episode_number:02d}", 
                           extra={'emoji_type': 'debug'})
                
                # Look for files matching the episode pattern
                files_found = False
                for file in os.listdir(season_dir):
                    logger.debug(f"Checking file: {file}", extra={'emoji_type': 'debug'})
                    
                    # Use more pattern variations to match all possible formats
                    patterns = [
                        f"s{int(season_number):02d}e{int(episode_number):02d}",  # "s01e01" format
                        f"S{int(season_number):02d}E{int(episode_number):02d}",  # "S01E01" format
                        f" - s{int(season_number):02d}e{int(episode_number):02d}",  # " - s01e01"
                        f" - S{int(season_number):02d}E{int(episode_number):02d}"   # " - S01E01"
                    ]
                    
                    # Check if any pattern matches
                    if any(pattern in file for pattern in patterns):
                        file_path = os.path.join(season_dir, file)
                        logger.debug(f"Match found! Deleting: {file_path}", extra={'emoji_type': 'debug'})
                        
                        try:
                            os.remove(file_path)
                            logger.info(f"Deleted placeholder file: {file_path}", extra={'emoji_type': 'delete'})
                            files_found = True
                        except Exception as e:
                            logger.error(f"Failed to delete file {file_path}: {e}", extra={'emoji_type': 'error'})
                
                if not files_found:
                    logger.debug(f"No matching episode files found in {season_dir}", extra={'emoji_type': 'debug'})
            else:
                logger.debug(f"Season directory not found: {season_dir}", extra={'emoji_type': 'debug'})
        
        # Movies or entire TV series - delete the whole folder
        else:
            # Only try to remove if it's not a TV show with season/episode specified
            if media_type == 'movie' or (season_number is None and episode_number is None):
                if os.path.exists(dummy_folder):
                    shutil.rmtree(dummy_folder)
                    logger.info(f"Deleted placeholder folder: {dummy_folder}", extra={'emoji_type': 'delete'})
        
        return True
            
    except Exception as e:
        logger.error(f"Error deleting placeholder: {e}", extra={'emoji_type': 'error'})
        return False