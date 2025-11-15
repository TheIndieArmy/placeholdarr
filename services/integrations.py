import os, glob, shutil, time, threading, requests, subprocess, platform, re, fnmatch, sys
from typing import Type
from core.config import settings
from core.logger import logger
from services.postgres.models import Episode, Movie, Season, Series, SubFlow
from services.utils import (
    resolve_final_folder, sanitize_filename, strip_status_markers, get_arr_config
)
from services.plex_client import plex
import hashlib

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
                    dummy_file_override=None, folder_path=None, arr_root_folder=None, season_folder_name=None, relative_path=None):
    """
    Create a dummy video file in the correct location. Uses resolve_final_folder for path resolution.
    """
    import os
    try:
        dummy_source = dummy_file_override or settings.DUMMY_FILE_PATH
        if not os.path.exists(dummy_source):
            logger.error(f"Dummy video file does not exist at {dummy_source}", extra={'emoji_type': 'error'})
            return None
        final_folder = resolve_final_folder(media_type, title, year, media_id, season_number, folder_path, arr_root_folder, season_folder_name, relative_path)
        if not final_folder:
            logger.error("No valid folder path for dummy file creation.", extra={'emoji_type': 'error'})
            return None
        os.makedirs(final_folder, exist_ok=True)
        clean_title = sanitize_filename(title)
        clean_title = re.sub(r'\s*\(\d{4}\)', '', clean_title).strip()
        year_str = f" ({year})" if year else ""
        if media_type == 'tv' and season_number is not None and episode_range:
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
                    logger.debug(f"Created dummy file: {file_path}", extra={'emoji_type': 'create'})
                    os.utime(file_path, None)
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
                logger.debug(f"Created dummy file: {file_path}", extra={'emoji_type': 'create'})
                os.utime(file_path, None)
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
        logger.debug(f"[Plex] Attempting update – type={media_type} id={media_id} status={status} kwargs={kwargs}", 
                     extra={'emoji_type': 'debug'})
        try:
            success = update_plex_title_status(
                media_type=media_type,
                media_id=media_id,
                title=title,
                status=status,
                **kwargs
            )
            logger.debug(f"[Plex] Update {'succeeded' if success else 'failed'} for id={media_id} kwargs={kwargs}", 
                         extra={'emoji_type': 'debug'})
            results.append(success)
        except Exception as e:
            logger.error(f"[Plex] Exception updating id={media_id} kwargs={kwargs}: {e}", extra={'emoji_type': 'error'})
            results.append(False)

    # Jellyfin update
    if settings.jellyfin_enabled:
        from services.jellyfin_client import update_jellyfin_title_status
        logger.debug(f"[Jellyfin] Attempting update – type={media_type} id={media_id} status={status} kwargs={kwargs}", 
                     extra={'emoji_type': 'debug'})
        try:
            success = update_jellyfin_title_status(
                media_type=media_type,
                media_id=media_id,
                title=title,
                status=status,
                **kwargs
            )
            logger.debug(f"[Jellyfin] Update {'succeeded' if success else 'failed'} for id={media_id} kwargs={kwargs}", 
                         extra={'emoji_type': 'debug'})
            results.append(success)
        except Exception as e:
            logger.error(f"[Jellyfin] Exception updating id={media_id} kwargs={kwargs}: {e}", extra={'emoji_type': 'error'})
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

def delete_dummy_file(
    session: Session,
    ent_id: int,
    model: Type,
    action: str
):
    """Adapter for SubFlow worker signature.

    Derive the human-readable fields (title/year/id/library/season/episode)
    from the DB row and delegate the actual filesystem and DB updates to
    delete_dummy_files(..., session=session).
    """
    logger.info(f"🗑️ delete_dummy_file called for {model.__name__} ID {ent_id}, action: {action}", extra={'emoji_type': 'delete'})
    try:
        # Determine which Sonarr instance to use
        sonarr_url = settings.SONARR_URL_4K if is_4k else settings.SONARR_URL
        sonarr_api_key = settings.SONARR_API_KEY_4K if is_4k else settings.SONARR_API_KEY
        headers = {"X-Api-Key": sonarr_api_key}
        
        # --- Always resolve folder path for placeholder creation/deletion ---
        folder_path = file_path or None
        arr_root_folder = getattr(settings, 'SONARR_ROOT_FOLDER', None) or None

        # Get all series from Sonarr for efficient matching
        series_url = f"{sonarr_url}/series"
        series_response = requests.get(series_url, headers=headers)
        
        elif model is Episode:
            episode = dbSession.query(Episode).get(ent_id)
            if not episode:
                logger.error(f"Episode with ID {ent_id} not found", extra={'emoji_type': 'error'})
                return False
            
            episode.placeholder_status = status
            dbSession.commit()
            logger.info(f"Updated episode placeholder status to '{status}' for ID {ent_id}", extra={'emoji_type': 'update'})
            return True
        
        else:
            logger.error(f'Unsupported model type for update_placeholder_status: {model}', extra={'emoji_type': 'error'})
            return False
    except Exception as e:
        logger.error(f"Error updating placeholder status for {model.__name__} ID {ent_id}: {e}", extra={'emoji_type': 'error'})
        return False

def delete_dummy_files(media_type, title, year, tvdb_id=None, library_path=None, season_number=None, episode_number=None, folder_path=None, arr_root_folder=None, season_folder_name=None, session: Session = None):
    """Delete placeholder files once real files are downloaded.

    Accepts an explicit SQLAlchemy `session` via kwarg `session` when
    invoked as a worker step. For backwards compatibility callers that don't
    pass `session` will only have filesystem changes applied.
    """
    import os

def delete_dummy_files(media_type, title, year, tvdb_id=None, library_path=None, season_number=None, episode_number=None, folder_path=None, arr_root_folder=None, season_folder_name=None, relative_path=None):
    """
    Delete placeholder files. Uses resolve_final_folder for path resolution.
    """
    import os
    try:
        final_folder = resolve_final_folder(media_type, title, year, tvdb_id, season_number, folder_path, arr_root_folder, season_folder_name, relative_path)
        if not final_folder:
            logger.error("No valid folder path for dummy file deletion.", extra={'emoji_type': 'error'})
            return False
        # For TV episodes, delete only the specific episode file in the correct season folder
        if media_type == 'tv' and season_number is not None and episode_number is not None:
            patterns = [
                f"s{season_number:02d}e{episode_number:02d}",
                f"S{season_number:02d}E{episode_number:02d}",
                f" - s{season_number:02d}e{episode_number:02d}",
                f" - S{season_number:02d}E{episode_number:02d}"
            ]
            files_found = False
            if os.path.exists(final_folder):
                for file in os.listdir(final_folder):
                    if any(pattern in file for pattern in patterns):
                        file_path = os.path.join(final_folder, file)
                        try:
                            os.remove(file_path)
                            logger.info(f"Deleted placeholder file: {file_path}", extra={'emoji_type': 'delete'})
                            files_found = True
                        except Exception as e:
                            logger.error(f"Failed to delete file {file_path}: {e}", extra={'emoji_type': 'error'})
                if not os.listdir(final_folder):
                    os.rmdir(final_folder)
                    logger.info(f"Deleted empty season folder: {final_folder}", extra={'emoji_type': 'delete'})
                if not files_found:
                    logger.debug(f"No matching episode files found in {final_folder}", extra={'emoji_type': 'debug'})
            else:
                logger.debug(f"Season directory not found: {final_folder}", extra={'emoji_type': 'debug'})
        else:
            # Movies or entire TV series - delete the whole folder
            if os.path.exists(final_folder):
                shutil.rmtree(final_folder)
                logger.info(f"Deleted placeholder folder: {final_folder}", extra={'emoji_type': 'delete'})
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

def batch_poll_and_update_plex_status(tvdb_id, title, episodes, max_attempts=10, initial_delay=0, throttle=0.2):
    """
    Batch poll Plex for summary readiness and update episode summaries with correct status/countdown.
    Args:
        tvdb_id: TVDB ID of the show
        title: Title of the show
        episodes: List of dicts with keys: season_num, episode_num, air_date (datetime), episode_title
        max_attempts: Max polling attempts
        initial_delay: Delay before first poll (0 for instant)
        throttle: Throttle between updates
    """
    import time
    from datetime import datetime, timedelta
    from services.plex_client import batch_update_plex_episode_status, find_show_by_id
    from services.calendar_sync import _parse_air_date
    
    def _local_date(dt):
        return dt.astimezone().date() if dt and hasattr(dt, 'tzinfo') and dt.tzinfo else dt.date() if dt else None
    def _get_unaired_status(air_date):
        now = datetime.now()
        today = now.date()
        tomorrow = today + timedelta(days=1)
        if not air_date:
            return None
        local_air_date = _local_date(air_date)
        if local_air_date == today:
            return "Airing today"
        elif local_air_date == tomorrow:
            return "Airing in 1 day"
        elif local_air_date > today:
            days_left = (local_air_date - today).days
            return f"Airing in {days_left} days"
        return None
    ep_lookup = {(int(ep['season_num']), int(ep['episode_num'])): ep for ep in episodes}
    done = set()
    attempt = 0
    prev_done_count = 0
    # Polling ramp-up: 1s, 5s, 10s, 20s, then 20s for all subsequent polls
    poll_schedule = [1, 5, 10, 20]
    delay = initial_delay
    while attempt < max_attempts:
        show = find_show_by_id(tvdb_id, title)
        if not show:
            logger.error(f"[BatchPoll] Could not find show with TVDB ID {tvdb_id}", extra={'emoji_type': 'error'})
            return False
        all_eps = list(show.episodes())
        plex_ep_map = {}
        for ep in all_eps:
            try:
                s = getattr(ep, 'seasonNumber', None) or getattr(ep, 'season', None)
                e = getattr(ep, 'episodeNumber', None) or getattr(ep, 'index', None)
                if s is not None and e is not None:
                    plex_ep_map[(int(s), int(e))] = ep
            except Exception as ex:
                logger.warning(f"[BatchPoll] Error mapping episode: {ex}", extra={'emoji_type': 'skip'})
        status_map = {}
        for key, ep_data in ep_lookup.items():
            if key in done:
                continue
            season, episode = key
            air_date = ep_data.get('air_date')
            # Ensure both air_date and now are comparable (naive or aware)
            if air_date and hasattr(air_date, 'tzinfo') and air_date.tzinfo:
                now = datetime.now(air_date.tzinfo)
            else:
                now = datetime.now()
            aired = air_date and air_date <= now
            plex_ep = plex_ep_map.get(key)
            if not plex_ep:
                continue  # Not yet in Plex
            current_summary = getattr(plex_ep, 'summary', '') or ''
            if aired:
                if current_summary.strip() != '':
                    status_map[key] = "Request"
                    done.add(key)
            else:
                status_map[key] = _get_unaired_status(air_date)
                done.add(key)
        batch_start = time.time()
        if status_map:
            batch_update_plex_episode_status(tvdb_id, title, status_map, throttle=throttle)
        batch_duration = time.time() - batch_start
        if len(done) == prev_done_count and attempt > 0:
            logger.info(f"[BatchPoll] No new episodes ready since last poll. Finishing early and updating all remaining as blank.", extra={'emoji_type': 'info'})
            remaining_status_map = {}
            for key, ep_data in ep_lookup.items():
                if key not in done:
                    air_date = ep_data.get('air_date')
                    if air_date and hasattr(air_date, 'tzinfo') and air_date.tzinfo:
                        now = datetime.now(air_date.tzinfo)
                    else:
                        now = datetime.now()
                    aired = air_date and air_date <= now
                    if aired:
                        remaining_status_map[key] = "Request"
                    else:
                        remaining_status_map[key] = _get_unaired_status(air_date)
            if remaining_status_map:
                batch_update_plex_episode_status(tvdb_id, title, remaining_status_map, throttle=throttle)
            logger.info(f"[BatchPoll] Early finish: updated all remaining episodes for '{title}'", extra={'emoji_type': 'success'})
            return True
        prev_done_count = len(done)
        if len(done) == len(ep_lookup):
            logger.info(f"[BatchPoll] All {len(done)} episodes updated for '{title}'", extra={'emoji_type': 'success'})
            return True
        attempt += 1
        # Ramp-up poll interval: 1, 5, 10, 20, then 20s for all subsequent polls
        if attempt < len(poll_schedule):
            wait_time = max(0, poll_schedule[attempt] - batch_duration)
        else:
            wait_time = max(0, 20 - batch_duration)
        logger.info(f"[BatchPoll] {len(done)}/{len(ep_lookup)} episodes updated for '{title}'. Polling again in {wait_time:.2f}s...", extra={'emoji_type': 'debug'})
        if wait_time > 0:
            time.sleep(wait_time)
    logger.warning(f"[BatchPoll] Polling ended. Forcing update of all remaining episodes for '{title}'", extra={'emoji_type': 'warning'})
    remaining_status_map = {}
    for key, ep_data in ep_lookup.items():
        if key not in done:
            air_date = ep_data.get('air_date')
            if air_date and hasattr(air_date, 'tzinfo') and air_date.tzinfo:
                now = datetime.now(air_date.tzinfo)
            else:
                now = datetime.now()
            aired = air_date and air_date <= now
            if aired:
                remaining_status_map[key] = "Request"
            else:
                remaining_status_map[key] = _get_unaired_status(air_date)
    if remaining_status_map:
        batch_update_plex_episode_status(tvdb_id, title, remaining_status_map, throttle=throttle)
    logger.info(f"[BatchPoll] Final forced update: updated all remaining episodes for '{title}'", extra={'emoji_type': 'success'})
    return True
