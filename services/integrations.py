import os, glob, shutil, time, threading, requests, subprocess, platform, re, fnmatch, sys
from core.config import settings
from core.logger import logger
from services.utils import (
    sanitize_filename, strip_status_markers, get_arr_config
)
from services.plex_client import plex

# Global variables
BASE_TITLES = {}
PROGRESS_FLAGS = {}
LAST_RADARR_SEARCH = {}

def get_folder_path(media_type, base_path, title, year=None, media_id=None, season=None, folder_path=None):
    """Generate folder path according to the convention, or use provided folder_path."""
    if folder_path:
        return folder_path
    if media_type == "movie":
        folder_name = f"{sanitize_filename(title)} ({year}) {{tmdb-{media_id}}}"  # removed edition
        return os.path.join(base_path, folder_name)
    else:
        folder_name = f"{sanitize_filename(title)} ({year}) {{tvdb-{media_id}}}"  # removed (dummy)
        season_folder = f"Season {season:02d}" if season is not None else None
        if season_folder:
            return os.path.join(base_path, folder_name, season_folder)
        else:
            return os.path.join(base_path, folder_name)


def place_dummy_file(media_type, title, year=None, media_id=None, base_path=None, 
                    season_number=None, episode_range=None, episode_title=None, episode_id=None,
                    dummy_file_override=None, folder_path=None, arr_root_folder=None):
    """Create a dummy video file in the correct location. Always use folderPath from webhook as subfolder. ENV is root if set, else use folderPath as absolute. Fallback to legacy only if folderPath missing."""
    import os
    try:
        dummy_source = dummy_file_override or settings.DUMMY_FILE_PATH
        if not os.path.exists(dummy_source):
            logger.error(f"Dummy video file does not exist at {dummy_source}", extra={'emoji_type': 'error'})
            return None
        # Canonical path logic
        if folder_path:
            # If ENV is set, use as root, else use folderPath as absolute
            env_path = None
            if media_type == "movie":
                env_path = getattr(settings, "MOVIE_LIBRARY_FOLDER", None)
            elif media_type == "tv":
                env_path = getattr(settings, "TV_LIBRARY_FOLDER", None)
            if env_path and str(env_path).strip():
                # Compute relative path from arr_root_folder to folder_path
                if arr_root_folder and folder_path.startswith(arr_root_folder):
                    rel_subfolder = os.path.relpath(folder_path, arr_root_folder)
                    final_folder = os.path.join(env_path, rel_subfolder)
                else:
                    # If arr_root_folder not available, just append basename
                    final_folder = os.path.join(env_path, os.path.basename(folder_path))
            else:
                final_folder = folder_path
        else:
            # Fallback to legacy logic
            util_get_folder_path = get_folder_path
            final_folder = util_get_folder_path(
                media_type=media_type,
                base_path=base_path or env_path or '.',
                title=title,
                year=year,
                media_id=media_id,
                season=season_number if media_type == 'tv' else None
            )
            logger.warning(f"No folderPath in webhook; using legacy fallback: {final_folder}", extra={'emoji_type': 'warning'})
        os.makedirs(final_folder, exist_ok=True)
        clean_title = sanitize_filename(title)
        clean_title = re.sub(r'\s*\(\d{4}\)', '', clean_title).strip()
        year_str = f" ({year})" if year else ""
        if media_type == 'tv' and season_number is not None:
            if episode_range:
                start_ep, end_ep = episode_range
                for ep_num in range(int(start_ep), int(end_ep) + 1):
                    ep_title = episode_title or f"Episode {ep_num}"
                    file_name = f"{clean_title}{year_str} - s{season_number:02d}e{ep_num:02d} - {ep_title}.mp4"
                    file_path = os.path.join(final_folder, sanitize_filename(file_name))
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    try:
                        if settings.PLACEHOLDER_STRATEGY == "copy":
                            shutil.copy2(dummy_source, file_path)
                        else:
                            try:
                                os.link(dummy_source, file_path)
                            except OSError:
                                shutil.copy2(dummy_source, file_path)
                        logger.info(f"Created dummy file: {file_path}", extra={'emoji_type': 'create'})
                    except Exception as e:
                        logger.error(f"Error creating dummy file: {str(e)}", extra={'emoji_type': 'error'})
                        return None
                ep_title = episode_title or f"Episode {start_ep}"
                file_name = f"{clean_title}{year_str} - s{season_number:02d}e{start_ep:02d} - {ep_title}.mp4"
                return os.path.join(final_folder, sanitize_filename(file_name))
        else:
            file_name = f"{clean_title}{year_str} (dummy).mp4"
            file_path = os.path.join(final_folder, sanitize_filename(file_name))
            if os.path.exists(file_path):
                os.remove(file_path)
            try:
                if settings.PLACEHOLDER_STRATEGY == "copy":
                    shutil.copy2(dummy_source, file_path)
                else:
                    try:
                        os.link(dummy_source, file_path)
                    except OSError:
                        shutil.copy2(dummy_source, file_path)
                logger.info(f"Created dummy file: {file_path}", extra={'emoji_type': 'create'})
            except Exception as e:
                logger.error(f"Error creating dummy file: {str(e)}", extra={'emoji_type': 'error'})
                return None
            return file_path
    except Exception as e:
        logger.error(f"Error creating dummy file: {str(e)}", extra={'emoji_type': 'error'})
        return None

def update_title_status(media_type, media_id, title, status, **kwargs):
    """Abstract update of media title status on Plex and/or Jellyfin"""
    results = []

    # Plex update
    if settings.plex_enabled:
        from services.plex_client import update_plex_title_status
        logger.debug(f"[Plex] Attempting update – type={media_type} id={media_id} status={status}", 
                     extra={'emoji_type': 'debug'})
        try:
            success = update_plex_title_status(
                media_type=media_type,
                media_id=media_id,
                title=title,
                status=status,
                **kwargs
            )
            logger.debug(f"[Plex] Update {'succeeded' if success else 'failed'} for id={media_id}", 
                         extra={'emoji_type': 'debug'})
            results.append(success)
        except Exception as e:
            logger.error(f"[Plex] Exception updating id={media_id}: {e}", extra={'emoji_type': 'error'})
            results.append(False)

    # Jellyfin update
    if settings.jellyfin_enabled:
        from services.jellyfin_client import update_jellyfin_title_status
        logger.debug(f"[Jellyfin] Attempting update – type={media_type} id={media_id} status={status}", 
                     extra={'emoji_type': 'debug'})
        try:
            success = update_jellyfin_title_status(
                media_type=media_type,
                media_id=media_id,
                title=title,
                status=status,
                **kwargs
            )
            logger.debug(f"[Jellyfin] Update {'succeeded' if success else 'failed'} for id={media_id}", 
                         extra={'emoji_type': 'debug'})
            results.append(success)
        except Exception as e:
            logger.error(f"[Jellyfin] Exception updating id={media_id}: {e}", extra={'emoji_type': 'error'})
            results.append(False)

    # No server case
    if not results:
        logger.error("No media server configured for title update", extra={'emoji_type': 'error'})
        return False

    # Return True if at least one succeeded
    overall = any(results)
    logger.debug(f"update_title_status overall result: {overall}", extra={'emoji_type': 'debug'})
    return overall

# Title update and scheduling functions
def schedule_episode_request_update(series_title, season_num, episode_num, media_id, delay=10, retries=5):
    """Schedule an update to the episode title with [Request] tag"""
    def attempt_update(attempt=1):
        try:
            # Use our new ID-based title update function
            result = update_title_status(
                media_type='tv',
                media_id=media_id,
                title=series_title,
                status='Request',
                season=season_num,
                episode=episode_num
            )
            
            if result:
                logger.debug(f"Successfully scheduled update for '{series_title}' S{season_num:02d}E{episode_num:02d}",
                           extra={'emoji_type': 'debug'})
            else:
                # If update fails but we have retries left, try again
                if attempt < retries:
                    threading.Timer(delay, attempt_update, args=[attempt+1]).start()
                else:
                    logger.error(f"Failed to update title for '{series_title}' S{season_num:02d}E{episode_num:02d} after {retries} attempts", 
                               extra={'emoji_type': 'error'})
                               
        except Exception as e:
            logger.error(f"Error updating title for '{series_title}' S{season_num:02d}E{episode_num:02d}: {e}", 
                       extra={'emoji_type': 'error'})
            # Try again if we have retries left
            if attempt < retries:
                threading.Timer(delay, attempt_update, args=[attempt+1]).start()

    # Start the first attempt after initial delay
    threading.Timer(delay, attempt_update).start()

def schedule_movie_request_update(movie_title, media_id, year=None, delay=10, retries=5):
    """Schedule an update to the movie title with [Request] tag"""
    def attempt_update(attempt=1):
        try:
            # Use our new ID-based title update function
            result = update_title_status(
                media_type='movie',
                media_id=media_id,
                title=movie_title,
                status='Request',
                year=year
            )
            
            if result:
                logger.debug(f"Successfully scheduled update for movie '{movie_title}'",
                           extra={'emoji_type': 'debug'})
            else:
                # If update fails but we have retries left, try again
                if attempt < retries:
                    logger.debug(f"Retrying movie title update for '{movie_title}' (attempt {attempt}/{retries})", 
                               extra={'emoji_type': 'debug'})
                    threading.Timer(delay, attempt_update, args=[attempt+1]).start()
                else:
                    logger.error(f"Failed to update title for '{movie_title}' after {retries} attempts", 
                               extra={'emoji_type': 'error'})
                               
        except Exception as e:
            logger.error(f"Error updating title for '{movie_title}': {e}", 
                       extra={'emoji_type': 'error'})
            # Try again if we have retries left
            if attempt < retries:
                threading.Timer(delay, attempt_update, args=[attempt+1]).start()

    # Start the first attempt after initial delay
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
            return movie_data['id']

        lookup = requests.get(f"{config['url']}/movie/lookup", params={'term': f"tmdb:{tmdb_id_int}"}, headers={'X-Api-Key': config['api_key']})
        lookup.raise_for_status()
        movie_data = lookup.json()[0]
        payload = {
            'title': movie_data['title'],
            'qualityProfileId': 7,
            'tmdbId': int(movie_data['tmdbId']),
            'year': int(movie_data['year']),
            'rootFolderPath': settings.MOVIE_LIBRARY_FOLDER,  # Use .env value
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
            'rootFolderPath': settings.TV_LIBRARY_FOLDER,  # Use .env value
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

def delete_dummy_files(media_type, title, year, tvdb_id=None, library_path=None, season_number=None, episode_number=None, folder_path=None, arr_root_folder=None):
    """Delete placeholder files. If ENV is set, use ENV as root and append basename(folderPath). If ENV is blank, use folderPath directly. Also check legacy fallback."""
    import os
    try:
        env_path = None
        if media_type == "movie":
            env_path = getattr(settings, "MOVIE_LIBRARY_FOLDER", None)
        elif media_type == "tv":
            env_path = getattr(settings, "TV_LIBRARY_FOLDER", None)
        if folder_path:
            if env_path and str(env_path).strip():
                dummy_folder = os.path.join(env_path, os.path.basename(folder_path))
            else:
                dummy_folder = folder_path
        else:
            # Fallback to legacy logic
            folder_name = sanitize_filename(title)
            if year:
                folder_name += f" ({year})"
            if media_type == 'tv':
                folder_name += f" {{tvdb-{tvdb_id}}} (dummy)"
            else:
                folder_name += f" {{tmdb-{tvdb_id}}}{{edition-Dummy}}"
            dummy_folder = os.path.join(library_path or '.', folder_name)
            logger.warning(f"No folderPath in webhook; using legacy fallback: {dummy_folder}", extra={'emoji_type': 'warning'})
        logger.debug(f"Looking for dummy folder: {dummy_folder}", extra={'emoji_type': 'debug'})
        # Delete dummy at canonical path
        if os.path.exists(dummy_folder):
            shutil.rmtree(dummy_folder)
            logger.info(f"Deleted placeholder folder: {dummy_folder}", extra={'emoji_type': 'delete'})
        else:
            logger.info(f"Dummy folder not found at canonical path: {dummy_folder}", extra={'emoji_type': 'info'})
        # Also check legacy fallback if not already checked
        if folder_path:
            folder_name = sanitize_filename(title)
            if year:
                folder_name += f" ({year})"
            if media_type == 'tv':
                folder_name += f" {{tvdb-{tvdb_id}}} (dummy)"
            else:
                folder_name += f" {{tmdb-{tvdb_id}}}{{edition-Dummy}}"
            legacy_folder = os.path.join(library_path or '.', folder_name)
            if legacy_folder != dummy_folder and os.path.exists(legacy_folder):
                shutil.rmtree(legacy_folder)
                logger.info(f"Deleted legacy placeholder folder: {legacy_folder}", extra={'emoji_type': 'delete'})
        # TV episode file deletion logic unchanged
        # ...existing code for TV episode file deletion...
        return True
    except Exception as e:
        logger.error(f"Error deleting placeholder: {e}", extra={'emoji_type': 'error'})
        return False

def check_arr_webhook(arr_name, arr_url, api_key, webhook_url):
    try:
        headers = {'X-Api-Key': api_key}
        response = requests.get(f"{arr_url}/notification", headers=headers, timeout=10)
        response.raise_for_status()
        notifications = response.json()
        found = False
        for n in notifications:
            if n.get('implementation', '').lower() == 'webhook':
                for field in n.get('fields', []):
                    if field.get('name') == 'url' and webhook_url in str(field.get('value', '')):
                        found = True
                        break
            if found:
                break
        if found:
            logger.info(f"{arr_name} webhook for Placeholdarr is configured.", extra={'emoji_type': 'success'})
            return True
        else:
            logger.warning(f"{arr_name} webhook for Placeholdarr is NOT configured! Please add a webhook in {arr_name} Connect settings pointing to {webhook_url}.", extra={'emoji_type': 'warning'})
            return False
    except Exception as e:
        logger.error(f"Failed to check {arr_name} webhook configuration: {e}", extra={'emoji_type': 'error'})
        return False


def check_all_arr_webhooks():
    # Allow skipping the webhook check for advanced/test setups
    if os.getenv('PLACEHOLDARR_SKIP_WEBHOOK_CHECK', '').lower() == 'true':
        logger.warning("PLACEHOLDARR_SKIP_WEBHOOK_CHECK is set. Skipping all webhook checks! Calendar sync will start regardless of webhook status.", extra={'emoji_type': 'warning'})
        return True

    # Allow user to override the webhook URL
    override_webhook_url = os.getenv('PLACEHOLDARR_WEBHOOK_URL')

    arr_urls = [
        getattr(settings, 'RADARR_URL', None),
        getattr(settings, 'RADARR_4K_URL', None),
        getattr(settings, 'SONARR_URL', None),
        getattr(settings, 'SONARR_4K_URL', None)
    ]
    arr_urls = [u for u in arr_urls if u and 'localhost' not in u and '127.0.0.1' not in u]
    if override_webhook_url:
        webhook_url = override_webhook_url
        logger.info(f"Using user-specified webhook URL for checks: {webhook_url}", extra={'emoji_type': 'info'})
    elif arr_urls:
        import urllib.parse
        parsed = urllib.parse.urlparse(arr_urls[0])
        host = parsed.hostname
        scheme = parsed.scheme
        port = os.getenv('PLACEHOLDARR_PORT') or getattr(settings, 'PLACEHOLDARR_PORT', 8001)
        webhook_url = f"{scheme}://{host}:{port}/webhook"
    else:
        port = os.getenv('PLACEHOLDARR_PORT') or getattr(settings, 'PLACEHOLDARR_PORT', 8001)
        webhook_url = f"http://localhost:{port}/webhook"

    arrs = []
    # Radarr
    if getattr(settings, 'RADARR_URL', None) and getattr(settings, 'RADARR_API_KEY', None):
        arrs.append(('Radarr', settings.RADARR_URL.rstrip('/'), settings.RADARR_API_KEY))
    # Radarr 4K
    if getattr(settings, 'RADARR_4K_URL', None) and getattr(settings, 'RADARR_4K_API_KEY', None) and settings.RADARR_4K_URL.strip():
        arrs.append(('Radarr 4K', settings.RADARR_4K_URL.rstrip('/'), settings.RADARR_4K_API_KEY))
    # Sonarr
    if getattr(settings, 'SONARR_URL', None) and getattr(settings, 'SONARR_API_KEY', None):
        arrs.append(('Sonarr', settings.SONARR_URL.rstrip('/'), settings.SONARR_API_KEY))
    # Sonarr 4K
    if getattr(settings, 'SONARR_4K_URL', None) and getattr(settings, 'SONARR_4K_API_KEY', None) and settings.SONARR_4K_URL.strip():
        arrs.append(('Sonarr 4K', settings.SONARR_4K_URL.rstrip('/'), settings.SONARR_4K_API_KEY))

    if not arrs:
        logger.error("No *arr services are configured.", extra={'emoji_type': 'error'})
        return False

    missing = []
    status_msgs = []
    for arr_name, arr_url, api_key in arrs:
        configured = bool(arr_url and api_key)
        webhook_ok = check_arr_webhook(arr_name, arr_url, api_key, webhook_url)
        status_msgs.append(f"{arr_name} (configured: {'yes' if configured else 'no'}, webhook: {'yes' if webhook_ok else 'no'})")
        if not webhook_ok:
            missing.append(arr_name)
    logger.info(f"Waiting for all configured *arrs to have webhooks set up. Detected: {', '.join(status_msgs)}.", extra={'emoji_type': 'info'})
    if missing:
        logger.warning(f"Webhooks still missing for: {', '.join(missing)}. Calendar sync will not start until all are ready.", extra={'emoji_type': 'warning'})
        return False
    return True