import os, glob, shutil, time, threading, requests, subprocess, platform, re, fnmatch, sys
from datetime import datetime, date
from typing import Type
from core.config import settings
from core.logger import logger
from services.postgres.models import Episode, Movie, Season, Series, SubFlow
from services.utils import (
    resolve_final_folder, sanitize_filename, strip_status_markers, get_series_folder,
    get_arr_config, write_nfo_for_placeholder, render_episode_nfo
)
from services.plex_client import plex
from services.utils import get_movie_by_id
from sqlalchemy.orm import Session
from services.postgres.models import Series


# Global variables
BASE_TITLES = {}
PROGRESS_FLAGS = {}
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
                    season_number=None, episode_range=None, episode_title=None, episode_id=None,
                    dummy_file_override=None):
    """Create a dummy video file in the appropriate location using the configured strategy"""
    try:
        # Determine the base path if not provided
        if not base_path:
            base_path = settings.DUMMY_MOVIE_LIBRARY_FOLDER if media_type == "movie" else settings.DUMMY_TV_LIBRARY_FOLDER

        # Clean title
        clean_title = sanitize_filename(title)
        clean_title = re.sub(r'\s*\(\d{4}\)', '', clean_title).strip()
        year_str = f" ({year})" if year else ""

        dummy_source = dummy_file_override or settings.DUMMY_FILE_PATH  # Valid dummy.mp4
        if not os.path.exists(dummy_source):
            logger.error(f"Dummy video file does not exist at {dummy_source}", extra={'emoji_type': 'error'})
            return None

        def create_placeholder_file(src, dst):
            if settings.PLACEHOLDER_STRATEGY == "copy":
                shutil.copy2(src, dst)
                logger.debug(f"Copied dummy file to {dst}", extra={'emoji_type': 'copy'})
            else:
                try:
                    os.link(src, dst)
                    logger.debug(f"Hardlinked dummy file to {dst}", extra={'emoji_type': 'link'})
                except OSError as e:
                    if e.errno == 18:  # Invalid cross-device link
                        shutil.copy2(src, dst)
                        logger.warning(f"Hardlink failed (cross-device); copied dummy file instead: {dst}", extra={'emoji_type': 'warning'})
                    else:
                        raise

        # Resolve final folder using canonical resolver (prefer ARR-provided folder paths)
        if media_type == 'tv' and season_number is not None:
            final_folder = resolve_final_folder(
                media_type='tv',
                title=title,
                year=year,
                media_id=media_id,
                season_number=season_number
            )

            if not final_folder:
                logger.error(f"No valid folder path for dummy file creation for {title} S{season_number}", extra={'emoji_type': 'error'})
                return None

            os.makedirs(final_folder, exist_ok=True)
            # Ensure folder has open permissions (match legacy behavior)
            try:
                # chmod the season folder and its parent series folder to be permissive
                os.chmod(final_folder, 0o777)
                parent = os.path.dirname(final_folder)
                if parent:
                    os.chmod(parent, 0o777)
            except Exception as e:
                logger.verbose(f"Failed to chmod season folder {final_folder}: {e}", extra={'emoji_type': 'debug'})

            if episode_range:
                start_ep, end_ep = episode_range
                for ep_num in range(int(start_ep), int(end_ep) + 1):
                    ep_title = episode_title or f"Episode {ep_num}"
                    file_name = f"{clean_title}{year_str} - s{season_number:02d}e{ep_num:02d} - {ep_title}.mp4"
                    file_path = os.path.join(final_folder, sanitize_filename(file_name))

                    if os.path.exists(file_path):
                        os.remove(file_path)
                    try:
                        create_placeholder_file(dummy_source, file_path)
                    except Exception as e:
                        logger.error(f"Error creating dummy file: {str(e)}", extra={'emoji_type': 'error'})
                        return None

                ep_title = episode_title or f"Episode {start_ep}"
                file_name = f"{clean_title}{year_str} - s{season_number:02d}e{start_ep:02d} - {ep_title}.mp4"
                return os.path.join(final_folder, sanitize_filename(file_name))

        else:
            final_folder = resolve_final_folder(
                media_type='movie',
                title=title,
                year=year,
                media_id=media_id
            )

            if not final_folder:
                logger.error(f"No valid folder path for dummy file creation for movie {title}", extra={'emoji_type': 'error'})
                return None

            os.makedirs(final_folder, exist_ok=True)
            # Ensure movie folder has open permissions (match legacy behavior)
            try:
                os.chmod(final_folder, 0o777)
            except Exception as e:
                logger.verbose(f"Failed to chmod movie folder {final_folder}: {e}", extra={'emoji_type': 'debug'})

            file_name = f"{clean_title}{year_str} (dummy).mp4"
            file_path = os.path.join(final_folder, sanitize_filename(file_name))

            if os.path.exists(file_path):
                os.remove(file_path)
            try:
                create_placeholder_file(dummy_source, file_path)
            except Exception as e:
                logger.error(f"Error creating dummy file: {str(e)}", extra={'emoji_type': 'error'})
                return None

            return file_path

    except Exception as e:
        logger.error(f"Error creating dummy file: {str(e)}", extra={'emoji_type': 'error'})
        return None
                        
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
        # Movie -> delegate to folder-based delete (passes tmdb via tvdb_id param)
        if model is Movie:
            movie = session.query(Movie).get(ent_id)
            if not movie:
                logger.error(f"Movie with ID {ent_id} not found", extra={'emoji_type': 'error'})
                return False

            logger.debug(f"Processing movie deletion: {movie.title} ({movie.year})", extra={'emoji_type': 'movie'})
            tmdb_id = getattr(movie, 'tmdbid', None) or getattr(movie, 'tmdb_id', None)
            is_4k = bool(getattr(movie, 'is_4k', False))
            movie_library = settings.DUMMY_MOVIE_LIBRARY_FOLDER_4K if is_4k else settings.DUMMY_MOVIE_LIBRARY_FOLDER

            # Delegate to the single source-of-truth for deletion. Pass the active
            # DB session so delete_dummy_files can perform DB updates atomically
            result = delete_dummy_files(
                media_type='movie',
                title=movie.title,
                year=movie.year,
                tvdb_id=tmdb_id,
                library_path=movie_library,
                session=session
            )
            logger.info(f"{'✅' if result else '❌'} Movie dummy file deletion {'succeeded' if result else 'failed'} for ID {ent_id}", extra={'emoji_type': 'success' if result else 'error'})
            return result

        # Episode -> delegate to folder-based delete (pass tvdb + season/episode)
        elif model is Episode:
            episode = session.query(Episode).get(ent_id)
            if not episode:
                logger.error(f"Episode with ID {ent_id} not found", extra={'emoji_type': 'error'})
                return False

            season = session.query(Season).get(episode.season_id) if episode.season_id else None
            series = session.query(Series).get(season.series_id) if season and season.series_id else None

            logger.debug(f"Processing episode deletion: {series.title if series else 'Unknown'} S{season.season_number if season else '?'}E{episode.episode_number}", extra={'emoji_type': 'tv'})
            is_4k = bool(getattr(episode, 'is_4k', False) or (series and getattr(series, 'is_4k', False)))
            tvdb = getattr(series, 'tvdbid', None) or getattr(episode, 'tvdb_id', None)
            tv_library = settings.TV_LIBRARY_FOLDER_4K if is_4k else settings.DUMMY_TV_LIBRARY_FOLDER

            # Delete individual episode dummy file, keep dummypath in DB (like movies)
            result = delete_dummy_files(
                media_type='tv',
                title=(series.title if series else ''),
                year=(series.year if series else None),
                tvdb_id=tvdb,
                library_path=tv_library,
                season_number=(season.season_number if season else None),
                episode_number=episode.episode_number if episode else None,
                session=session
            )
            logger.info(f"{'✅' if result else '❌'} Episode dummy file deletion {'succeeded' if result else 'failed'} for ID {ent_id}", extra={'emoji_type': 'success' if result else 'error'})
            return result

        else:
            logger.error(f'Unsupported model type for delete_dummy_file: {model}', extra={'emoji_type': 'error'})
            return False
    except Exception as e:
        logger.error(f"Error deleting dummy file for {model.__name__} ID {ent_id}: {e}", extra={'emoji_type': 'error'})
        return False

def update_placeholder_status(dbSession: Session, ent_id: int, model: Type, action: str, status: str = None):
    """Update the status of a placeholder file"""
    try:
        if not status and 'add' in action:
            status = "Request"
        if not status and 'delete' in action:
            status = "Request"
        if model is Movie:
            movie = dbSession.query(Movie).get(ent_id)
            if not movie:
                logger.error(f"Movie with ID {ent_id} not found", extra={'emoji_type': 'error'})
                return False
            
            movie.placeholder_status = status
            dbSession.commit()
            logger.info(f"Updated movie placeholder status to '{status}' for ID {ent_id}", extra={'emoji_type': 'update'})
            return True
        
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

    try:
        # Build the folder pattern
        folder_name = sanitize_filename(title)
        if year:
            folder_name += f" ({year})"

        # Add appropriate ID tag
        if media_type == 'tv':
            # Try both naming patterns: with and without (dummy) suffix
            folder_name_with_dummy = folder_name + f" {{tvdb-{tvdb_id}}} (dummy)"
            folder_name_without_dummy = folder_name + f" {{tvdb-{tvdb_id}}}"
            
            # Check which folder actually exists
            potential_folders = [
                os.path.join(library_path, folder_name_with_dummy),
                os.path.join(library_path, folder_name_without_dummy)
            ]
            
            dummy_folder = None
            for folder in potential_folders:
                if folder and os.path.exists(folder):
                    dummy_folder = folder
                    break
                    
            if not dummy_folder:
                logger.debug(f"TV dummy folder not found in any expected location: {potential_folders}", extra={'emoji_type': 'debug'})
                return True
        else:  # movie
            folder_name += f" {{tmdb-{tvdb_id}}}"
            dummy_folder = os.path.join(library_path, folder_name) if library_path else None
        logger.debug(f"Using dummy folder: {dummy_folder}", extra={'emoji_type': 'debug'})

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

                    # Check if any pattern matches and it's a video file
                    if any(pattern in file for pattern in patterns) and file.endswith(('.mp4', '.mkv', '.avi')):
                        file_path = os.path.join(season_dir, file)
                        logger.debug(f"Match found! Deleting: {file_path}", extra={'emoji_type': 'debug'})

                        try:
                            os.remove(file_path)
                            logger.info(f"Deleted placeholder file: {file_path}", extra={'emoji_type': 'delete'})
                            files_found = True
                        except Exception as e:
                            logger.error(f"Failed to delete file {file_path}: {e}", extra={'emoji_type': 'error'})

                
                # Handle case when no episode files found but season folder exists
                if not files_found:
                    logger.debug(f"No matching episode files found in {season_dir}", extra={'emoji_type': 'debug'})
                    
                    # Check if there are any episode files left in the entire series folder
                    # If not, clean up the whole series folder including NFO files
                    try:
                        all_episode_files = []
                        for root, dirs, files in os.walk(dummy_folder):
                            for file in files:
                                if file.endswith(('.mp4', '.mkv', '.avi')):  # Episode file extensions
                                    all_episode_files.append(file)
                        
                        if not all_episode_files:  # No episode files left in the entire series
                            logger.info(f"🧹 COMPLETE CLEANUP: No episode files left in series, removing everything", extra={'emoji_type': 'delete'})
                            # Remove the entire series folder including NFO files
                            import shutil
                            try:
                                shutil.rmtree(dummy_folder)
                                logger.info(f"🗑️ Deleted entire series folder: {dummy_folder}", extra={'emoji_type': 'delete'})
                            except OSError as rm_error:
                                # If rmtree fails, try manual cleanup
                                logger.warning(f"shutil.rmtree failed, attempting manual cleanup: {rm_error}", extra={'emoji_type': 'warning'})
                                try:
                                    # Remove all files first
                                    for root, dirs, files in os.walk(dummy_folder, topdown=False):
                                        for file in files:
                                            os.remove(os.path.join(root, file))
                                            logger.debug(f"Manually removed file: {file}", extra={'emoji_type': 'debug'})
                                        for dir in dirs:
                                            os.rmdir(os.path.join(root, dir))
                                            logger.debug(f"Manually removed directory: {dir}", extra={'emoji_type': 'debug'})
                                    # Finally remove the root folder
                                    os.rmdir(dummy_folder)
                                    logger.info(f"🗑️ Manual cleanup successful: {dummy_folder}", extra={'emoji_type': 'delete'})
                                except Exception as manual_error:
                                    logger.error(f"Manual cleanup also failed: {manual_error}", extra={'emoji_type': 'error'})
                        else:
                            logger.debug(f"Still have {len(all_episode_files)} episode files in series folder", extra={'emoji_type': 'debug'})
                            
                    except Exception as e:
                        logger.error(f"Failed to check/remove series folder: {e}", extra={'emoji_type': 'error'})
                if files_found:
                    # If season directory is empty after removals, try to remove it
                    try:
                        if os.path.exists(season_dir) and not os.listdir(season_dir):
                            os.rmdir(season_dir)
                            logger.info(f"Deleted empty season folder: {season_dir}", extra={'emoji_type': 'delete'})
                            
                            # Check if series folder is now empty and remove it too
                            try:
                                if os.path.exists(dummy_folder):
                                    remaining_contents = os.listdir(dummy_folder)
                                    # If only NFO files remain, delete them and the folder
                                    if remaining_contents and all(f.endswith('.nfo') for f in remaining_contents):
                                        logger.info(f"Only NFO files remain in series folder, cleaning up completely", extra={'emoji_type': 'delete'})
                                        for nfo_file in remaining_contents:
                                            nfo_path = os.path.join(dummy_folder, nfo_file)
                                            try:
                                                os.remove(nfo_path)
                                                logger.info(f"Deleted series NFO file: {nfo_path}", extra={'emoji_type': 'delete'})
                                            except Exception as e:
                                                logger.debug(f"Failed to delete NFO file {nfo_path}: {e}", extra={'emoji_type': 'debug'})
                                    
                                    # Now check if folder is empty and remove it
                                    if not os.listdir(dummy_folder):
                                        os.rmdir(dummy_folder)
                                        logger.info(f"Deleted empty series folder: {dummy_folder}", extra={'emoji_type': 'delete'})
                            except Exception as e:
                                logger.debug(f"Failed to remove series folder: {e}", extra={'emoji_type': 'debug'})
                                
                    except Exception as e:
                        logger.debug(f"Failed to remove season folder: {e}", extra={'emoji_type': 'debug'})
            else:
                # Season directory doesn't exist - check if series folder only has NFO files
                logger.debug(f"Season directory doesn't exist: {season_dir}", extra={'emoji_type': 'debug'})
                try:
                    if os.path.exists(dummy_folder):
                        remaining_contents = os.listdir(dummy_folder)
                        logger.debug(f"Series folder contents: {remaining_contents}", extra={'emoji_type': 'debug'})
                        
                        if not remaining_contents:
                            # Empty folder - remove it
                            os.rmdir(dummy_folder)
                            logger.info(f"Deleted empty series folder: {dummy_folder}", extra={'emoji_type': 'delete'})
                        else:
                            # Check for any remaining episode files
                            remaining_episodes = []
                            for root, dirs, files in os.walk(dummy_folder):
                                for file in files:
                                    if file.endswith(('.mp4', '.mkv', '.avi')):
                                        remaining_episodes.append(file)
                            
                            if not remaining_episodes:
                                logger.debug(f"No episode files found, but other files remain: {remaining_contents}", extra={'emoji_type': 'debug'})
                            else:
                                logger.debug(f"Series folder contains {len(remaining_episodes)} episode files, keeping it", extra={'emoji_type': 'debug'})
                except Exception as e:
                    logger.debug(f"Failed to check series folder cleanup: {e}", extra={'emoji_type': 'debug'})

                # **KEY FIX**: Don't update database for episodes - keep dummypath like movies!
                # DB updates removed to match movie behavior - preserve dummypath as historical record
                logger.debug(f"Episode file cleanup complete - preserving dummypath in database like movies", extra={'emoji_type': 'debug'})

        # Movies or entire TV series - delete the whole folder
        else:
            # For movies, find and delete the .mp4 file inside the folder, then remove empty folder
            if media_type == 'movie':
                try:
                    if dummy_folder and os.path.exists(dummy_folder):
                        # Find the movie file inside the folder
                        movie_files = []
                        for file in os.listdir(dummy_folder):
                            if file.endswith(('.mp4', '.mkv', '.avi', '.mov')):
                                movie_files.append(file)
                        
                        if movie_files:
                            for movie_file in movie_files:
                                file_path = os.path.join(dummy_folder, movie_file)
                                try:
                                    os.remove(file_path)
                                    logger.info(f"Deleted movie placeholder file: {file_path}", extra={'emoji_type': 'delete'})
                                except Exception as e:
                                    logger.error(f"Failed to delete movie file {file_path}: {e}", extra={'emoji_type': 'error'})
                        
                        # After deleting movie files, check if folder is empty or only has non-essential files
                        try:
                            remaining_files = os.listdir(dummy_folder)
                            # Only keep folder if it has important files (not just .nfo, .jpg, etc.)
                            important_files = [f for f in remaining_files if not f.endswith(('.nfo', '.jpg', '.png', '.txt', '.srt'))]
                            
                            if not important_files:  # Only metadata files left or empty
                                import shutil
                                shutil.rmtree(dummy_folder)
                                logger.info(f"Deleted empty movie folder: {dummy_folder}", extra={'emoji_type': 'delete'})
                            else:
                                logger.debug(f"Movie folder still contains important files: {important_files}", extra={'emoji_type': 'debug'})
                        except Exception as e:
                            logger.debug(f"Could not clean up movie folder {dummy_folder}: {e}", extra={'emoji_type': 'debug'})
                    else:
                        logger.debug(f"Movie folder doesn't exist: {dummy_folder}", extra={'emoji_type': 'debug'})
                except Exception as e:
                    logger.error(f"Failed to delete movie placeholder {dummy_folder}: {e}", extra={'emoji_type': 'error'})
            else:
                # TV series - delete the whole folder
                try:
                    import shutil
                    shutil.rmtree(dummy_folder)
                    logger.info(f"Deleted placeholder folder: {dummy_folder}", extra={'emoji_type': 'delete'})
                except Exception as e:
                    logger.error(f"Failed to delete placeholder folder {dummy_folder}: {e}", extra={'emoji_type': 'error'})

            # DB updates if session passed explicitly by caller (worker)
            db_sess = session

            if db_sess:
                try:
                    if media_type == 'movie':
                        # Movie: use tmdb id
                        movie = None
                        try:
                            movie = db_sess.query(Movie).filter_by(tmdbid=int(tvdb_id)).first() if tvdb_id is not None else None
                        except Exception:
                            movie = db_sess.query(Movie).filter_by(tmdbid=tvdb_id).first() if tvdb_id is not None else None
                        if movie:
                            movie.is_deleted = True
                            movie.jellyfin_dummy_id = None
                            movie.placeholder_exists = False
                            db_sess.add(movie)
                            db_sess.commit()
                            logger.debug(f"Marked DB Movie deleted: {movie.title}", extra={'emoji_type': 'debug'})

                    elif media_type == 'tv':
                        series = db_sess.query(Series).filter_by(tvdbid=str(tvdb_id)).first() if tvdb_id else None
                        if series:
                            series.is_deleted = True
                            series.jellyfin_dummy_id = None
                            series.placeholder_exists = False
                            db_sess.add(series)
                            seasons = db_sess.query(Season).filter_by(series_id=series.id).all()
                            for s in seasons:
                                s.is_deleted = True
                                s.jellyfin_dummy_id = None
                                s.placeholder_exists = False
                                db_sess.add(s)
                                eps = db_sess.query(Episode).filter_by(season_id=s.id).all()
                                for ep in eps:
                                    ep.is_deleted = True
                                    ep.jellyfin_dummy_id = None
                                    ep.placeholder_exists = False
                                    db_sess.add(ep)
                            db_sess.commit()
                            logger.debug(f"Marked DB Series, seasons, and episodes deleted for: {series.title}", extra={'emoji_type': 'debug'})
                except Exception as e:
                    logger.error(f"Failed to mark DB records deleted: {e}", extra={'emoji_type': 'error'})

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

def save_jellyfin_id(session, model, ent_id, jf_id):
    obj = session.query(model).get(ent_id)
    field = {
      Series: 'jellyfin_id',
      Season: 'jellyfin_id',
      Episode: 'jellyfin_id'
    }[model]
    if getattr(obj, field) != jf_id:
        setattr(obj, field, jf_id)
        session.add(obj)
        session.commit()
    return jf_id

def delayed_placeholders(session: Session, ent_id: int, model: Type, action: str) -> bool:
    """
    Delay briefly then process placeholder actions for the specified media.
    For TV shows, creates placeholders for all episodes in the series.
    For movies, creates a single placeholder file.
    """
    delay_seconds = 3
    is_4k = False  # Default to standard definition
    
    # Handle different media types
    if model is Movie:
        # Movie case
        movie = session.query(Movie).get(ent_id)
        if not movie:
            logger.error(f"Movie with id {ent_id} not found", extra={'emoji_type': 'error'})
            return False
        
        # Check if the movie has is_4k attribute
        if hasattr(movie, 'is_4k') and movie.is_4k:
            is_4k = True
        
        # Delay before processing
        logger.debug(
            f"Delaying {delay_seconds}s before processing placeholder for movie '{movie.title}'",
            extra={'emoji_type': 'debug'}
        )
        time.sleep(delay_seconds)
        
        # Check if movie already has a real file
        from services.queue_monitor import check_movie_has_file
        # Prefer checking Radarr by its internal id (radarrid). If missing, try to enrich by TMDB
        radarr_id = getattr(movie, 'radarrid', None)
        tmdb_id = getattr(movie, 'tmdbid', None) or getattr(movie, 'tmdb_id', None)

        # If we have a Radarr id, ask Radarr directly
        if radarr_id:
            try:
                if check_movie_has_file(radarr_id, is_4k=is_4k):
                    logger.info(f"Skipping placeholder for {movie.title} (real file exists in Radarr)", extra={'emoji_type': 'skip'})
                    return True
            except Exception:
                # If the check errors, fall back to continuing the flow (we'll create a placeholder)
                logger.debug("Radarr file check failed, will continue with placeholder creation", extra={'emoji_type': 'debug'})

        # If no radarr id, try to enrich from Radarr using the TMDB id to discover the Radarr record
        if not radarr_id and tmdb_id:
            try:
                movie_data = enrich_movie_from_radarr(tmdb_id=tmdb_id, is_4k=is_4k)
                if movie_data and movie_data.get('id'):
                    # Persist discovered radarr id for future checks
                    movie.radarrid = movie_data.get('id')
                    session.add(movie)
                    session.commit()

                    # If Radarr already reports a file, skip placeholder
                    mf = movie_data.get('movieFile') or {}
                    if movie_data.get('hasFile') or mf:
                        logger.info(f"Skipping placeholder for {movie.title} (real file found via Radarr lookup)", extra={'emoji_type': 'skip'})
                        return True
            except Exception:
                logger.debug("Radarr enrichment failed or returned no data", extra={'emoji_type': 'debug'})
        
        # Select appropriate library folder based on 4K status
        movie_library = settings.DUMMY_MOVIE_LIBRARY_FOLDER_4K if is_4k else settings.DUMMY_MOVIE_LIBRARY_FOLDER
    
        # Create placeholder file
        dummy_path = place_dummy_file(
            "movie", 
            movie.title, 
            movie.year, 
            tmdb_id,
            movie_library
        )
    
    
        if dummy_path:
            movie.dummypath = dummy_path
            movie.placeholder_exists = True  # Mark that placeholder file exists
            session.add(movie)
            session.commit()
            logger.info(f"Created placeholder file for '{movie.title}'", extra={'emoji_type': 'success'})
            # Attempt to write an .nfo next to the placeholder using authoritative DB fields
            try:
                try:
                    # Refresh the ORM object to make sure we have committed values
                    session.refresh(movie)
                except Exception:
                    # Fallback to re-querying if refresh is not possible
                    movie = session.query(Movie).get(movie.id)

                overview = getattr(movie, 'radarr_overview', None)
                tmdbid = getattr(movie, 'tmdbid', None)
                imdbid = getattr(movie, 'imdbid', None) if hasattr(movie, 'imdbid') else None

                meta = {
                    'title': movie.title,
                    'year': movie.year,
                    'tmdbid': tmdbid,
                    'imdbid': imdbid,
                    'radarr_overview': overview
                }

                logger.debug(
                    f"Rendering movie NFO: title={movie.title!r} tmdb={tmdbid} imdb={imdbid} overview_len={len(overview) if overview else 0}",
                    extra={'emoji_type': 'debug'}
                )

                ok = write_nfo_for_placeholder(dummy_path, meta, media_type='movie', status='Request')
                if ok:
                    logger.debug(f"Wrote movie NFO for {dummy_path}", extra={'emoji_type': 'create'})
                else:
                    logger.debug(f"Failed to write movie NFO for {dummy_path}", extra={'emoji_type': 'warning'})
            except Exception as e:
                logger.debug(f"Exception writing movie NFO: {e}", extra={'emoji_type': 'debug'})
            return True
        else:
            logger.error(f"Failed to create placeholder file for movie '{movie.title}'", extra={'emoji_type': 'error'})
            return False
    
    elif model is Series:
        # Series case - process all episodes in the series
        series = session.query(Series).get(ent_id)
        if not series:
            logger.error(f"Series with id {ent_id} not found", extra={'emoji_type': 'error'})
            return False

        # Query all SubFlow rows for this series that are waiting on delayed_placeholders
        sf_eps = session.query(SubFlow).filter(
            SubFlow.steps == "delayed_placeholders",
            SubFlow.series_id == series.id,
            SubFlow.status != "DONE"
        ).order_by(SubFlow.id).all()

        # If no SubFlows, this means we're being called directly on a series
        # Find all episodes and process them
        if not sf_eps:
            episodes = session.query(Episode).join(Season).filter(
                Season.series_id == series.id,
                Episode.status == 'PENDING'
            ).all()
            
            if not episodes:
                logger.info(f"No pending episodes found for series '{series.title}'", extra={'emoji_type': 'info'})
                return True
            
            # For each episode, call delayed_placeholders recursively
            for episode in episodes:
                success = delayed_placeholders(session, episode.id, Episode, action)
                if not success:
                    logger.warning(f"Failed to create placeholder for episode {episode.id}", extra={'emoji_type': 'warning'})
        else:
            # Process existing SubFlows
            for subflow in sf_eps:
                if subflow.episode_id:
                    success = delayed_placeholders(session, subflow.episode_id, Episode, action)
                    if not success:
                        logger.warning(f"Failed to create placeholder for episode {subflow.episode_id}", extra={'emoji_type': 'warning'})

        # Set series dummy path based on episodes if not already set
        if not series.dummypath:
            episode = session.query(Episode).join(Season).filter(Season.series_id == series.id).first()
            if episode and episode.dummypath:
                # Extract series path from episode path
                series_path = '/'.join(episode.dummypath.split('/')[:-2])  # Remove 'Season XX/episode.mp4'
                series.dummypath = series_path
                session.add(series)
                session.commit()
                logger.debug(f"Set series dummy path to: {series_path}", extra={'emoji_type': 'debug'})

        # After all episode subflows, update series current_step_name to latest completed step if possible
        if hasattr(series, 'current_step_name'):
            last_done_subflow = session.query(SubFlow).filter(
                SubFlow.series_id == series.id,
                SubFlow.status == 'DONE',
                SubFlow.episode_id == None
            ).order_by(SubFlow.id.desc()).first()
            if last_done_subflow:
                series.current_step_name = last_done_subflow.steps
                session.add(series)
                session.commit()
        logger.info(f"Completed placeholder processing for series '{series.title}'", extra={'emoji_type': 'success'})
        return True


    elif model is Episode:
        # Look up the current episode, season, and series to determine the series_id
        current_ep = session.query(Episode).get(ent_id)
        if not current_ep:
            logger.error(f"Episode with id {ent_id} not found", extra={'emoji_type': 'error'})
            return False

        seas = session.query(Season).get(current_ep.season_id)
        if not seas:
            logger.error(f"Season for episode {ent_id} not found", extra={'emoji_type': 'error'})
            return False

        # If INCLUDE_SPECIALS is False, skip and remove specials
        include_specials = getattr(settings, 'INCLUDE_SPECIALS', False)
        if not include_specials:
            # Remove specials (season 0) and their episodes from DB
            specials_seasons = session.query(Season).filter(Season.series_id == seas.series_id, Season.season_number == 0).all()
            for special_season in specials_seasons:
                # Delete all episodes in this season
                special_episodes = session.query(Episode).filter(Episode.season_id == special_season.id).all()
                for special_ep in special_episodes:
                    logger.info(f"Deleting special episode {special_ep.id} (season 0)", extra={'emoji_type': 'delete'})
                    session.delete(special_ep)
                logger.info(f"Deleting special season {special_season.id} (season 0)", extra={'emoji_type': 'delete'})
                session.delete(special_season)
            session.commit()
            # If this episode is a special, skip placeholder creation
            if seas.season_number == 0:
                logger.info(f"Skipping placeholder for special episode {current_ep.id} (season 0) due to INCLUDE_SPECIALS=False", extra={'emoji_type': 'skip'})
                return True

        series = session.query(Series).get(seas.series_id)
        if not series:
            logger.error(f"Series for episode {ent_id} not found", extra={'emoji_type': 'error'})
            return False

        # --- Begin restored episode placeholder logic ---
        # Query all SubFlow rows for this series that are waiting on delayed_placeholders
        # NOTE: scheduler creates per-episode SubFlows with episode_id populated and
        # series_id sometimes left NULL. Include both cases so episode-level subflows
        # are processed by this batch placeholder creator.
        episode_ids = [e.id for e in session.query(Episode.id).join(Season).filter(Season.series_id == series.id).all()]
        sf_eps = (
            session.query(SubFlow)
            .filter(
                SubFlow.steps == "delayed_placeholders",
                SubFlow.status != "DONE"
            )
            .filter(
                (SubFlow.series_id == series.id) | (SubFlow.episode_id.in_(episode_ids))
            )
            .order_by(SubFlow.id)
            .all()
        )

        # If no pending SubFlows but episodes have dummypath without actual files, 
        # find episodes that need placeholder files recreated
        if not sf_eps:
            episodes_needing_files = []
            all_episodes = session.query(Episode).join(Season).filter(Season.series_id == series.id).all()
            
            for ep in all_episodes:
                has_real_file = getattr(ep, 'filepath', None)
                has_dummy_path = getattr(ep, 'dummypath', None)
                dummy_file_exists = has_dummy_path and os.path.exists(has_dummy_path) if has_dummy_path else False
                
                # Episode needs a placeholder if: no real file AND (no dummypath OR dummypath file missing)
                if not has_real_file and (not has_dummy_path or not dummy_file_exists):
                    episodes_needing_files.append(ep)
            
            # If episodes need placeholder files, create SubFlows for them
            if episodes_needing_files:
                logger.info(f"Found {len(episodes_needing_files)} episodes needing placeholder files for series '{series.title}'", extra={'emoji_type': 'info'})
                
                for ep in episodes_needing_files:
                    # Create a new SubFlow for this episode
                    new_subflow = SubFlow(
                        episode_id=ep.id,
                        series_id=series.id,
                        steps="delayed_placeholders",
                        status="PENDING",
                        branch="main",
                        action="handle_seriesadd"
                    )
                    session.add(new_subflow)
                    
                session.commit()
                
                # Re-query SubFlows now that we've created new ones
                sf_eps = session.query(SubFlow).filter(
                    SubFlow.steps == "delayed_placeholders",
                    SubFlow.series_id == series.id,
                    SubFlow.status != "DONE"
                ).order_by(SubFlow.id).all()

        # Determine 4K status from the current episode/series
        is_4k = False
        try:
            if hasattr(current_ep, 'is_4k') and current_ep.is_4k:
                is_4k = True
            elif hasattr(series, 'is_4k') and series.is_4k:
                is_4k = True
        except Exception:
            is_4k = False

        # Delay briefly before processing the batch
        logger.debug(
            f"Delaying {delay_seconds}s before processing placeholders for series '{series.title}'",
            extra={'emoji_type': 'debug'}
        )
        time.sleep(delay_seconds)

        # Select appropriate library folder based on 4K status
        tv_library = settings.TV_LIBRARY_FOLDER_4K if is_4k else settings.DUMMY_TV_LIBRARY_FOLDER

        placeholder_count = 0
        failed_count = 0

        # Normalize tvdb for the series and current episode
        normalized_series_tvdb = None
        try:
            normalized_series_tvdb = getattr(series, 'tvdbid', None) or getattr(series, 'tvdb_id', None)
        except Exception:
            normalized_series_tvdb = None

        # Iterate all pending subflows for this series and create placeholders
        for subflow in sf_eps:
            # Load the episode row referenced by the subflow
            if not subflow.episode_id:
                # If this SubFlow isn't an episode-level entry, skip
                continue

            ep = session.query(Episode).get(subflow.episode_id)
            if not ep:
                # Mark the subflow DONE to avoid reprocessing orphan entries
                subflow.status = "DONE"
                # Try to update current_step_name for orphaned episode if possible
                orphan_ep = session.query(Episode).get(subflow.episode_id)
                if orphan_ep and hasattr(orphan_ep, 'current_step_name'):
                    orphan_ep.current_step_name = subflow.steps
                    session.add(orphan_ep)
                session.add(subflow)
                session.commit()
                continue

            # Skip if real file exists or placeholder already set AND file actually exists
            has_real_file = getattr(ep, 'filepath', None)
            has_dummy_path = getattr(ep, 'dummypath', None)
            dummy_file_exists = has_dummy_path and os.path.exists(has_dummy_path) if has_dummy_path else False
            
            if has_real_file or dummy_file_exists:
                subflow.status = "DONE"
                # Update current_step_name on entity when SubFlow is marked DONE
                if hasattr(ep, 'current_step_name'):
                    ep.current_step_name = subflow.steps
                    session.add(ep)
                session.add(subflow)
                session.commit()
                continue

            # Ensure season info available
            season_rec = session.query(Season).get(ep.season_id) if ep.season_id else None
            if not season_rec:
                logger.error(f"Missing season for episode id {ep.id}", extra={'emoji_type': 'error'})
                subflow.status = "DONE"
                session.add(subflow)
                session.commit()
                continue

            season_num = season_rec.season_number
            episode_num = ep.episode_number
            episode_title = ep.title

            # Quick check: if episode already has a real file via queue monitor, skip
            from services.queue_monitor import check_episode_has_file
            try:
                if normalized_series_tvdb and check_episode_has_file(normalized_series_tvdb, season_num, episode_num, is_4k):
                    logger.info(
                        f"Skipping placeholder for {series.title} S{season_num}E{episode_num} (real file exists)",
                        extra={'emoji_type': 'skip'}
                    )
                    subflow.status = "DONE"
                    session.add(subflow)
                    session.commit()
                    continue
            except Exception:
                logger.debug("Episode file check failed, will continue with placeholder creation", extra={'emoji_type': 'debug'})

            # Attempt Sonarr enrichment once for the series if needed
            try:
                series_sonarrid = getattr(series, 'sonarrid', None)
                if not series_sonarrid and normalized_series_tvdb:
                    config = get_arr_config('tv', is_4k)
                    if config:
                        headers = {'X-Api-Key': config['api_key']}
                        lookup = requests.get(
                            f"{config['url']}/series/lookup",
                            params={'term': f"tvdb:{int(normalized_series_tvdb)}"},
                            headers=headers,
                            timeout=10
                        )
                        if lookup.ok:
                            results = lookup.json()
                            if isinstance(results, list) and results:
                                found = results[0]
                                if found.get('id'):
                                    series.sonarrid = found.get('id')
                                    session.add(series)
                                    session.commit()
            except Exception:
                logger.debug("Sonarr lookup by TVDB failed or returned no data", extra={'emoji_type': 'debug'})

            # Ensure Sonarr enrichment has been performed so episode-level overviews are available.
            # We only run enrichment when we don't already have an episode overview for this episode
            # and we have a normalized TVDB id to look up.
            try:
                ep_has_overview = getattr(ep, 'sonarr_episode_overview', None)
                if not ep_has_overview and normalized_tvdb:
                    logger.debug(f"Fetching Sonarr enrichment for TVDB {normalized_tvdb} to populate episode overview", extra={'emoji_type': 'debug'})
                    try:
                        # Call the existing enrichment routine which will persist episode overviews
                        enrich_series_from_sonarr(tvdb_id=int(normalized_tvdb) if normalized_tvdb else None, sonarr_id=getattr(series, 'sonarrid', None), is_4k=is_4k)
                    except Exception as e:
                        logger.debug(f"Sonarr enrichment call failed: {e}", extra={'emoji_type': 'debug'})

                    # Refresh the episode row so any persisted sonarr_episode_overview is available
                    try:
                        session.refresh(ep)
                    except Exception:
                        ep = session.query(Episode).get(ep.id)
            except Exception:
                logger.debug("Failed while attempting to enrich/refresh episode overview from Sonarr", extra={'emoji_type': 'debug'})

            # Create the placeholder file for this episode
            dummy_path = None
            try:
                dummy_path = place_dummy_file(
                    "tv",
                    series.title,
                    series.year,
                    normalized_series_tvdb,
                    tv_library,
                    season_number=season_num,
                    episode_range=(episode_num, episode_num),
                    episode_title=episode_title
                )
            except Exception as e:
                logger.error(f"Error creating dummy for {series.title} S{season_num}E{episode_num}: {e}", extra={'emoji_type': 'error'})

            if dummy_path:
                ep.dummypath = dummy_path
                ep.placeholder_exists = True  # Mark that placeholder file exists
                if hasattr(ep, 'current_step_name'):
                    ep.current_step_name = subflow.steps
                session.add(ep)
                # Commit immediately so external scanners and other workers see file presence
                session.commit()
                placeholder_count += 1
                logger.debug(f"Persisted placeholder for {series.title} S{season_num}E{episode_num}", extra={'emoji_type': 'debug'})

                # Write episode-level NFO next to placeholder using authoritative DB values
                try:
                    try:
                        session.refresh(ep)
                    except Exception:
                        ep = session.query(Episode).get(ep.id)

                    # Re-load season and series to ensure we have latest overviews
                    season_row = session.query(Season).get(ep.season_id) if ep.season_id else None
                    series_row = session.query(Series).get(season_row.series_id) if season_row and season_row.series_id else None

                    ep_overview = getattr(ep, 'sonarr_episode_overview', None)
                    series_tvdb = getattr(series_row, 'tvdbid', None) if series_row else None

                    # DEBUG: log exact overview value and length to diagnose missing overview in NFO
                    try:
                        logger.debug(f"Episode overview (repr) for ep.id={ep.id}: {ep_overview!r} (len={len(ep_overview) if ep_overview else 0})", extra={'emoji_type': 'debug'})
                    except Exception:
                        logger.debug(f"Episode overview logging failed for ep.id={ep.id}", extra={'emoji_type': 'debug'})

                    ep_meta = {
                        'title': ep.title,
                        'season': season_num,
                        'episode': episode_num,
                        'aired': getattr(ep, 'air_date', None),
                        'sonarr_episode_overview': ep_overview,
                        'tvdb': series_tvdb
                    }

                    logger.debug(
                        f"Rendering episode NFO: title={ep.title!r} S{season_num}E{episode_num} tvdb={series_tvdb} overview_len={len(ep_overview) if ep_overview else 0}",
                        extra={'emoji_type': 'debug'}
                    )

                    # DEBUG: render the XML here and log it so we can see whether the overview is present
                    try:
                        try:
                            rendered_xml = render_episode_nfo(ep_meta, status='Request')
                            logger.debug(f"Rendered episode XML for ep.id={ep.id} (len={len(rendered_xml)}): {rendered_xml[:500]!r}", extra={'emoji_type': 'debug'})
                        except Exception as e:
                            logger.debug(f"Failed to render episode XML for debug: {e}", extra={'emoji_type': 'debug'})
                    except Exception:
                        pass

                    ok = write_nfo_for_placeholder(dummy_path, ep_meta, media_type='tv', status='Request')
                    if ok:
                        logger.debug(f"Wrote episode NFO for {dummy_path}", extra={'emoji_type': 'create'})
                    else:
                        logger.debug(f"Failed to write episode NFO for {dummy_path}", extra={'emoji_type': 'warning'})
                except Exception as e:
                    logger.debug(f"Exception writing episode NFO: {e}", extra={'emoji_type': 'debug'})

            # Mark this subflow entry DONE and commit so it's not reprocessed
            subflow.status = "DONE"
            session.add(subflow)
            session.commit()

        if placeholder_count > 0:
            logger.info(
                f"Created {placeholder_count} placeholder files for '{series.title}'",
                extra={'emoji_type': 'success'}
            )
        
        if failed_count > 0:
            logger.warning(
                f"Failed to create {failed_count} placeholder files for '{series.title}' - these episodes will be retried by scheduler",
                extra={'emoji_type': 'warning'}
            )
        elif placeholder_count == 0:
            logger.info(f"No placeholders needed for '{series.title}'", extra={'emoji_type': 'info'})
        
        return True
    else:
        logger.error(f"Unsupported model type: {model.__name__}", extra={'emoji_type': 'error'})
        return False

def mark_movie_monitored(movie_id, is_4k=False):
    """Mark a movie as monitored in Radarr"""
    try:
        config = get_arr_config('movie', is_4k)
        # Get current movie data
        response = requests.get(
            f"{config['url']}/movie/{movie_id}", 
            headers={'X-Api-Key': config['api_key']}
        )
        response.raise_for_status()
        movie_data = response.json()
        
        # Update monitored status if needed
        if not movie_data.get("monitored", False):
            movie_data["monitored"] = True
            update_response = requests.put(
                f"{config['url']}/movie/{movie_id}", 
                json=movie_data, 
                headers={'X-Api-Key': config['api_key']}
            )
            update_response.raise_for_status()
            logger.info(f"Movie {movie_data['title']} marked as monitored", extra={'emoji_type': 'info'})
            return True
        else:
            logger.debug(f"Movie {movie_data['title']} already monitored", extra={'emoji_type': 'debug'})
            return True
    except Exception as e:
        logger.error(f"Failed to mark movie as monitored: {e}", extra={'emoji_type': 'error'})
        return False

def api_monitor_episodes(series_id, episode_ids, is_4k=False):
    """
    Mark episodes as monitored in Sonarr.
    series_id: Database series ID.
    episode_ids: list of Sonarr episode IDs
    is_4k: use 4K Sonarr instance if True
    Also marks all other episodes in the series as unmonitored if they are still monitored.
    """
    logger.info(f"🎯 api_monitor_episodes called: series_id={series_id}, episode_ids={episode_ids}, is_4k={is_4k}")
    try:
        logger.debug(f"🔧 Getting Sonarr config for is_4k={is_4k}")
        config = get_arr_config('tv', is_4k)
        headers = {'X-Api-Key': config['api_key']}
        logger.debug(f"🔧 Config retrieved: {config['url']}")

        # Ensure the series is monitored
        series_url = f"{config['url']}/series/{series_id}"
        logger.debug(f"🔍 Fetching series from: {series_url}")
        series_response = requests.get(series_url, headers=headers)
        series_response.raise_for_status()
        series_data = series_response.json()
        logger.debug(f"✅ Series data retrieved: {series_data.get('title', 'Unknown')}")
        logger.debug(f"✅ Series data retrieved: {series_data.get('title', 'Unknown')}")
        if not series_data.get("monitored", False):
            logger.info(f"📺 Series '{series_data['title']}' is not monitored, marking as monitored")
            series_data["monitored"] = True
            update_series = requests.put(series_url, json=series_data, headers=headers)
            update_series.raise_for_status()
            logger.info(f"✅ Series '{series_data['title']}' marked as monitored", extra={'emoji_type': 'info'})
        else:
            logger.debug(f"✅ Series '{series_data['title']}' is already monitored")

        # Get all episodes for the series
        episodes_url = f"{config['url']}/episode"
        params = {"seriesId": series_id}
        logger.debug(f"🔍 Fetching episodes for series {series_id} from: {episodes_url}")
        episode_response = requests.get(episodes_url, params=params, headers=headers)
        episode_response.raise_for_status()
        episodes = episode_response.json()
        logger.debug(f"✅ Retrieved {len(episodes)} episodes from Sonarr")
        
        # Log the episode IDs we're looking for vs what Sonarr has
        sonarr_ep_ids = [ep['id'] for ep in episodes]
        logger.debug(f"🔍 Looking for episode IDs: {episode_ids}")
        logger.debug(f"🔍 Sonarr has episode IDs: {sonarr_ep_ids}")
        logger.debug(f"🔍 Matching IDs: {set(episode_ids) & set(sonarr_ep_ids)}")
        
        # Log first episode details for debugging
        if episodes:
            sample = episodes[0]
            logger.debug(f"📺 Sample episode from Sonarr: ID={sample.get('id')}, S{sample.get('seasonNumber')}E{sample.get('episodeNumber')}, Title='{sample.get('title')}'")

        updated = 0
        unmonitored = 0
        for ep in episodes:
            if ep['id'] in episode_ids:
                if not ep.get('monitored', False):
                    ep['monitored'] = True
                    update_ep_url = f"{config['url']}/episode/{ep['id']}"
                    update_ep = requests.put(update_ep_url, json=ep, headers=headers)
                    update_ep.raise_for_status()
                    updated += 1
                    logger.debug(f"✅ Monitored episode ID {ep['id']} (S{ep.get('seasonNumber')}E{ep.get('episodeNumber')})")
            else:
                if ep.get('monitored', False):
                    ep['monitored'] = False
                    update_ep_url = f"{config['url']}/episode/{ep['id']}"
                    update_ep = requests.put(update_ep_url, json=ep, headers=headers)
                    update_ep.raise_for_status()
                    unmonitored += 1

        logger.info(f"✅ Monitoring complete: {updated} monitored, {unmonitored} unmonitored")
        if updated > 0:
            logger.info(f"Marked {updated} episodes as monitored", extra={'emoji_type': 'info'})
        if unmonitored > 0:
            logger.info(f"Marked {unmonitored} episodes as unmonitored", extra={'emoji_type': 'info'})
        logger.info(f"🎯 api_monitor_episodes completed successfully")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to monitor episodes: {e}", exc_info=True, extra={'emoji_type': 'error'})
        return False


def monitor_seasons_and_episodes(series_id, season_numbers, monitor_episodes=None, is_4k=False):
    """
    Monitor seasons in Sonarr with optional episode-level control.
    
    Args:
        series_id: Sonarr series ID
        season_numbers: List of season numbers to monitor
        monitor_episodes: If None, monitor all episodes in the seasons.
                         If list of episode IDs, only monitor those specific episodes
                         and unmonitor others in the same seasons (preserving previously monitored)
        is_4k: Whether to use 4K Sonarr instance
        
    Returns:
        bool: Success status
    """
    try:
        logger.info(f"🎯 Starting monitor_seasons_and_episodes for series {series_id}, seasons: {season_numbers}")
        config = get_arr_config('series', is_4k)
        if not config:
            logger.error("❌ Sonarr config missing", extra={'emoji_type': 'error'})
            return False

        headers = {'X-Api-Key': config['api_key']}
        base_url = config['url']

        # Get series data
        logger.debug(f"📡 Fetching series data from Sonarr...")
        r = requests.get(f"{base_url}/series/{series_id}", headers=headers, timeout=10)
        if r.status_code != 200:
            logger.error(f"❌ Failed to fetch series {series_id}: {r.status_code}", extra={'emoji_type': 'error'})
            return False

        series_data = r.json()
        logger.debug(f"📺 Series: {series_data.get('title', 'Unknown')}")

        # Get all episodes for the series
        logger.debug(f"📡 Fetching all episodes for series...")
        r = requests.get(f"{base_url}/episode", params={'seriesId': series_id}, headers=headers, timeout=10)
        if r.status_code != 200:
            logger.error(f"❌ Failed to fetch episodes: {r.status_code}", extra={'emoji_type': 'error'})
            return False

        all_episodes = r.json()
        logger.debug(f"📊 Found {len(all_episodes)} total episodes in series")

        # Filter episodes in target seasons
        season_episodes = [ep for ep in all_episodes if ep.get('seasonNumber') in season_numbers]
        logger.info(f"🎯 Found {len(season_episodes)} episodes in seasons {season_numbers}")

        if not season_episodes:
            logger.warning(f"⚠️ No episodes found in seasons {season_numbers}")
            return False

        # Determine which episodes to monitor
        monitored_count = 0
        unmonitored_count = 0
        unchanged_count = 0
        
        # Get currently monitored episodes BEFORE any changes - preserve their state
        currently_monitored = {ep['id'] for ep in all_episodes if ep.get('monitored', False)}
        logger.debug(f"📊 {len(currently_monitored)} episodes currently monitored (will preserve their state)")

        if monitor_episodes is None:
            # Monitor ALL episodes in these seasons
            logger.info(f"📺 Monitoring ALL episodes in seasons {season_numbers}")
            for ep in season_episodes:
                if not ep.get('monitored', False):
                    ep['monitored'] = True
                    monitored_count += 1
                else:
                    unchanged_count += 1
        else:
            # Only monitor specific episodes, unmonitor others in same season (preserving previously monitored)
            logger.info(f"🎯 Selective monitoring: {len(monitor_episodes)} episodes to monitor")
            
            for ep in season_episodes:
                ep_id = ep['id']
                was_monitored = ep_id in currently_monitored  # State BEFORE our changes
                is_monitored = ep.get('monitored', False)  # Current state in response
                
                if ep_id in monitor_episodes:
                    # Should be monitored
                    if not is_monitored:
                        ep['monitored'] = True
                        monitored_count += 1
                        logger.debug(f"✅ Monitoring S{ep['seasonNumber']}E{ep['episodeNumber']} (ID: {ep_id})")
                    else:
                        unchanged_count += 1
                else:
                    # Not in monitor list
                    if was_monitored:
                        # Was previously monitored - PRESERVE that state
                        unchanged_count += 1
                        logger.debug(f"🔒 Preserving monitored state for S{ep['seasonNumber']}E{ep['episodeNumber']} (ID: {ep_id})")
                    elif is_monitored:
                        # Wasn't previously monitored but is now - unmonitor it
                        ep['monitored'] = False
                        unmonitored_count += 1
                        logger.debug(f"❌ Unmonitoring S{ep['seasonNumber']}E{ep['episodeNumber']} (ID: {ep_id})")
                    else:
                        # Wasn't monitored and shouldn't be - leave it
                        unchanged_count += 1

        # Update episodes in Sonarr
        logger.info(f"📡 Updating episodes in Sonarr...")
        
        updated_successfully = 0
        
        # Determine which episodes to update
        if monitor_episodes is None:
            # Update all episodes in the seasons
            episodes_to_update = season_episodes
        else:
            # Only update episodes we're changing (monitoring or unmonitoring)
            episodes_to_update = [ep for ep in season_episodes 
                                 if ep['id'] in monitor_episodes or 
                                 (ep['id'] not in monitor_episodes and ep.get('monitored', False))]
        
        for ep in episodes_to_update:
            try:
                r = requests.put(f"{base_url}/episode/{ep['id']}", 
                                json=ep, 
                                headers=headers, 
                                timeout=10)
                if r.status_code == 202:
                    updated_successfully += 1
                else:
                    logger.warning(f"⚠️ Failed to update episode {ep['id']}: {r.status_code}")
            except Exception as e:
                logger.warning(f"⚠️ Exception updating episode {ep['id']}: {e}")

        # Update series and season monitoring status
        logger.info(f"📡 Updating series and season monitoring status...")
        
        # Update season monitoring - monitor only the specified seasons, unmonitor others
        if 'seasons' in series_data:
            for season in series_data['seasons']:
                season_num = season.get('seasonNumber')
                if season_num in season_numbers:
                    season['monitored'] = True
                    logger.debug(f"✅ Season {season_num}: monitored = True")
                else:
                    # Unmonitor seasons not in our list
                    season['monitored'] = False
                    logger.debug(f"❌ Season {season_num}: monitored = False")
        
        # Mark series as monitored
        series_data['monitored'] = True
        
        # Update series with all changes (series + season monitoring)
        try:
            r = requests.put(f"{base_url}/series/{series_id}", 
                            json=series_data, 
                            headers=headers, 
                            timeout=10)
            if r.status_code == 202:
                logger.info(f"✅ Series and seasons updated: monitored seasons {season_numbers}")
            else:
                logger.warning(f"⚠️ Failed to update series/season monitoring: {r.status_code}")
        except Exception as e:
            logger.warning(f"⚠️ Exception updating series/season monitoring: {e}")

        logger.info(f"✅ Season monitoring complete:")
        logger.info(f"   📺 Monitored: {monitored_count} episodes")
        logger.info(f"   ❌ Unmonitored: {unmonitored_count} episodes")
        logger.info(f"   🔒 Preserved: {unchanged_count} episodes")
        logger.info(f"   📡 API Updates: {updated_successfully}")
        
        return True
    except Exception as e:
        logger.error(f"❌ Failed to monitor seasons: {e}", exc_info=True, extra={'emoji_type': 'error'})
        return False


def enrich_movie_from_radarr(tmdb_id=None, radarr_id=None, is_4k=False):
    """Fetch authoritative movie data from Radarr and update local DB record.
    Prefers Radarr internal ID; falls back to lookup by TMDB when needed.
    This is safe to run in a background thread/process.
    Returns the Radarr movie JSON on success, or None on failure.
    """
    try:
        # Lazy import to avoid circular deps at module import time
        from services.postgres.db import get_session
        from services.postgres.models import Movie
        config = get_arr_config('movie', is_4k)
        if not config:
            logger.error("Radarr config missing for enrichment", extra={'emoji_type': 'error'})
            return None

        # Optional operator-configurable short delay to avoid races where Radarr
        # hasn't yet recorded a just-added file. Default 0 (no delay) to match
        # non-DB behavior. Operators can set ENRICHMENT_DELAY_SECONDS in settings.
        delay_seconds = getattr(settings, 'ENRICHMENT_DELAY_SECONDS', 0)
        if delay_seconds:
            logger.debug(f"Delaying {delay_seconds}s before Radarr enrichment for tmdb={tmdb_id} radarr={radarr_id}", extra={'emoji_type': 'debug'})
            time.sleep(delay_seconds)

        headers = {'X-Api-Key': config['api_key']}
        base_url = config['url']
        movie_data = None

        # Try direct fetch by Radarr internal id first
        if radarr_id:
            try:
                r = requests.get(f"{base_url}/movie/{radarr_id}", headers=headers, timeout=10)
                if r.status_code == 200:
                    movie_data = r.json()
                else:
                    logger.debug(f"Radarr movie fetch by id {radarr_id} returned {r.status_code}")
            except Exception as e:
                logger.error(f"Radarr direct fetch failed: {e}", extra={'emoji_type': 'error'})

        # Fallback: lookup by TMDB
        if movie_data is None and tmdb_id:
            try:
                r = requests.get(f"{base_url}/movie/lookup", params={'term': f"tmdb:{int(tmdb_id)}"}, headers=headers, timeout=10)
                r.raise_for_status()
                results = r.json()
                if isinstance(results, list) and results:
                    movie_data = results[0]
                    # If lookup returned radarr id, try to fetch full movie by id for complete fields
                    _id = movie_data.get('id')
                    if _id:
                        try:
                            r2 = requests.get(f"{base_url}/movie/{_id}", headers=headers, timeout=10)
                            if r2.status_code == 200:
                                movie_data = r2.json()
                        except Exception:
                            pass
            except Exception as e:
                logger.error(f"Radarr lookup by tmdb failed: {e}", extra={'emoji_type': 'error'})

        if not movie_data:
            logger.debug("No Radarr movie data found during enrichment", extra={'emoji_type': 'debug'})
            return None

        # Map fields into DB
        session = get_session()
        try:
            # Try to find movie by tmdbid + is_4k, fallback to radarrid
            m = None
            if tmdb_id is not None:
                m = session.query(Movie).filter_by(tmdbid=int(tmdb_id), is_4k=is_4k).first()
            if not m and movie_data.get('id'):
                m = session.query(Movie).filter_by(radarrid=movie_data.get('id')).first()

            if not m:
                logger.warning(f"Enrichment: local Movie not found for tmdb={tmdb_id} radarr={radarr_id}", extra={'emoji_type': 'warning'})
                return movie_data

            # movieFile may be nested
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

            # also capture overview/plot from Radarr for NFO generation
            overview = movie_data.get('overview') or movie_data.get('plot') or movie_data.get('description') or None

            # Update DB fields conservatively
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

            # persist overview if present
            if overview and getattr(m, 'radarr_overview', None) != overview:
                m.radarr_overview = overview
                changed = True

            # also update radarrid if missing
            if movie_data.get('id') and m.radarrid != movie_data.get('id'):
                m.radarrid = movie_data.get('id')
                changed = True

            # Update NFO metadata fields from Radarr data
            overview = movie_data.get('overview')
            if overview and m.plot != overview:
                m.plot = overview
                changed = True
            
            # Update other metadata fields if available
            original_title = movie_data.get('originalTitle')
            if original_title and m.originaltitle != original_title:
                m.originaltitle = original_title
                changed = True
            
            sort_title = movie_data.get('sortTitle')
            if sort_title and m.sorttitle != sort_title:
                m.sorttitle = sort_title
                changed = True
            
            runtime = movie_data.get('runtime')
            if runtime and m.runtime != runtime:
                m.runtime = runtime
                changed = True
            
            # Handle ratings
            ratings = movie_data.get('ratings', [])
            if ratings and isinstance(ratings, list):
                # Use first rating (usually IMDb)
                rating_value = ratings[0].get('value') if ratings else None
                if rating_value and m.rating != rating_value:
                    m.rating = rating_value
                    changed = True
            
            # Handle studio/distributor
            studio = movie_data.get('studio')
            if studio and m.studio != studio:
                m.studio = studio
                changed = True
            
            # Handle genres (convert list to comma-separated string)
            genres = movie_data.get('genres', [])
            if genres and isinstance(genres, list):
                genre_str = ', '.join(genres)
                if m.genres != genre_str:
                    m.genres = genre_str
                    changed = True
            
            # Handle year (from release date)
            digital_release = movie_data.get('digitalRelease')
            physical_release = movie_data.get('physicalRelease')
            in_cinemas = movie_data.get('inCinemas')
            # Use the earliest available date for year
            release_date = None
            for date_field in [digital_release, physical_release, in_cinemas]:
                if date_field:
                    try:
                        year = int(date_field.split('-')[0])
                        if not release_date or year < release_date:
                            release_date = year
                    except (ValueError, IndexError):
                        pass
            if release_date and m.year != release_date:
                m.year = release_date
                changed = True
            
            # Handle IMDb ID from external IDs
            imdb_id = movie_data.get('imdbId')
            if not imdb_id:
                # Try to get from external IDs if available
                external_ids = movie_data.get('externalIds', {})
                if isinstance(external_ids, dict):
                    imdb_id = external_ids.get('imdb')
            if imdb_id and m.imdb_id != imdb_id:
                m.imdb_id = imdb_id
                changed = True
            
            # Handle director (from credits)
            credits = movie_data.get('credits', {})
            if isinstance(credits, dict):
                crew = credits.get('crew', [])
                for person in crew:
                    if isinstance(person, dict) and person.get('job') == 'Director':
                        director_name = person.get('name')
                        if director_name and m.director != director_name:
                            m.director = director_name
                            changed = True
                        break
            
            # Handle images
            images = movie_data.get('images', [])
            if images and isinstance(images, list):
                for image in images:
                    if isinstance(image, dict):
                        cover_type = image.get('coverType')
                        url = image.get('remoteUrl')
                        if url:
                            # Convert relative URLs to full URLs
                            if url.startswith('/'):
                                url = base_url.replace('/api/v3', '') + url
                            
                            if cover_type == 'poster' and m.poster_url != url:
                                m.poster_url = url
                                changed = True
                            elif cover_type == 'fanart' and m.fanart_url != url:
                                m.fanart_url = url
                                changed = True

            if changed:
                session.add(m)
                session.commit()
                logger.info(f"Enriched movie {m.tmdbid} from Radarr", extra={'emoji_type': 'update'})
            else:
                logger.debug(f"Enrichment for movie {m.tmdbid} found no changes", extra={'emoji_type': 'debug'})

            return movie_data
        finally:
            session.close()

    except Exception as e:
        logger.error(f"Enrichment failed: {e}", extra={'emoji_type': 'error'})
        return None


def enrich_series_from_sonarr(tvdb_id=None, sonarr_id=None, is_4k=False):
    """
    Fetch authoritative series data from Sonarr and update local DB record.
    Prefers Sonarr internal ID; falls back to lookup by TVDB when needed.
    Returns the Sonarr series JSON on success, or None on failure.
    """
    try:
        from services.postgres.db import get_session
        from services.postgres.models import Series as SeriesModel, Season as SeasonModel, Episode as EpisodeModel
        config = get_arr_config('tv', is_4k)
        if not config:
            logger.error("Sonarr config missing for enrichment", extra={'emoji_type': 'error'})
            return None

        # Optional operator-configurable short delay to avoid races where Sonarr
        # hasn't yet recorded a just-added file. Default 0 (no delay) to match
        # non-DB behavior. Operators can set ENRICHMENT_DELAY_SECONDS in settings.
        delay_seconds = getattr(settings, 'ENRICHMENT_DELAY_SECONDS', 0)
        if delay_seconds:
            logger.debug(f"Delaying {delay_seconds}s before Sonarr enrichment for tvdb={tvdb_id} sonarr={sonarr_id}", extra={'emoji_type': 'debug'})
            time.sleep(delay_seconds)

        headers = {'X-Api-Key': config['api_key']}
        base_url = config['url']
        series_data = None

        # Try direct fetch by Sonarr internal id first
        if sonarr_id:
            try:
                r = requests.get(f"{base_url}/series/{sonarr_id}", headers=headers, timeout=10)
                if r.status_code == 200:
                    series_data = r.json()
                else:
                    logger.debug(f"Sonarr series fetch by id {sonarr_id} returned {r.status_code}")
                    # If Sonarr explicitly returned 404, record that so we can mark local series deleted later
                    if r.status_code == 404:
                        series_not_found = True
            except Exception as e:
                logger.error(f"Sonarr direct fetch failed: {e}", extra={'emoji_type': 'error'})

        # Fallback: lookup by TVDB
        if series_data is None and tvdb_id:
            try:
                r = requests.get(f"{base_url}/series/lookup", params={'term': f"tvdb:{int(tvdb_id)}"}, headers=headers, timeout=10)
                r.raise_for_status()
                results = r.json()
                if isinstance(results, list) and results:
                    found = results[0]
                    # If lookup returned sonarr id, try to fetch full series by id for complete fields
                    _id = found.get('id')
                    if _id:
                        try:
                            r2 = requests.get(f"{base_url}/series/{_id}", headers=headers, timeout=10)
                            if r2.status_code == 200:
                                series_data = r2.json()
                        except Exception:
                            pass
                else:
                    # Lookup returned empty list -> the series is not present in Sonarr (for this TVDB)
                    lookup_no_results = True
            except Exception as e:
                logger.error(f"Sonarr lookup by tvdb failed: {e}", extra={'emoji_type': 'error'})

        if not series_data:
            logger.debug("No Sonarr series data found during enrichment", extra={'emoji_type': 'debug'})
            # If Sonarr explicitly returned 404 for the series id, or the lookup returned no results
            # and we have a local Series that previously had a sonarrid, mark it deleted to keep DB consistent.
            session = get_session()
            try:
                s = None
                if tvdb_id is not None:
                    try:
                        s = session.query(SeriesModel).filter_by(tvdbid=int(tvdb_id)).first()
                    except Exception:
                        s = session.query(SeriesModel).filter_by(tvdbid=str(tvdb_id)).first()
                # If we found a local Series and Sonarr confirmed it is missing, mark deleted
                if s and (('series_not_found' in locals() and series_not_found) or ('lookup_no_results' in locals() and lookup_no_results)):
                    try:
                        if getattr(s, 'sonarrid', None):
                            s.is_deleted = True
                            s.has_files = False
                            session.add(s)
                            session.commit()
                            logger.info(f"Marked Series TVDB {s.tvdbid} as deleted because Sonarr returned no record", extra={'emoji_type': 'delete'})
                    except Exception:
                        session.rollback()
                # Close session and return since there's nothing to enrich
                return None
            finally:
                session.close()

        # Map fields into DB
        session = get_session()
        try:
            s = None
            if tvdb_id is not None:
                try:
                    s = session.query(SeriesModel).filter_by(tvdbid=int(tvdb_id)).first()
                except Exception:
                    s = session.query(SeriesModel).filter_by(tvdbid=str(tvdb_id)).first()
            if not s and series_data.get('id'):
                s = session.query(SeriesModel).filter_by(sonarrid=series_data.get('id')).first()

            if not s:
                logger.warning(f"Enrichment: local Series not found for tvdb={tvdb_id} sonarr={sonarr_id}", extra={'emoji_type': 'warning'})
                return series_data

            changed = False
            # Sonarr fields
            path = series_data.get('path') or series_data.get('folderPath') or series_data.get('folder')
            # Determine whether Sonarr reports any files for this series.
            # Sonarr's `hasFile` is authoritative; otherwise inspect per-season flags
            has_files = bool(series_data.get('hasFile', False))
            seasons_info = series_data.get('seasons') or []
            if not has_files and seasons_info:
                # Inspect seasons for indicators of files: Sonarr may include per-season 'hasFile' or 'statistics'.
                for sd in seasons_info:
                    try:
                        if sd.get('hasFile'):
                            has_files = True
                            break
                        stats = sd.get('statistics') or {}
                        if int(stats.get('episodeFileCount', 0)) > 0:
                            has_files = True
                            break
                    except Exception:
                        continue

            monitored = bool(series_data.get('monitored', False))
            if s.sonarrid != series_data.get('id'):
                s.sonarrid = series_data.get('id')
                changed = True
            if path and s.filepath != path:
                s.filepath = path
                changed = True
            # Sonarr is the source of truth for monitored/has_files flags.
            # Update DB to match Sonarr's report (allow setting to False when Sonarr says no files).
            if s.sonarr_monitored != monitored:
                s.sonarr_monitored = monitored
                changed = True
            if s.has_files != has_files:
                s.has_files = has_files
                changed = True
                logger.debug(f"Set series.has_files={has_files} for series {s.tvdbid} based on Sonarr data", extra={'emoji_type': 'debug'})

            # If this series was previously marked deleted but Sonarr returned a record, unmark it
            try:
                if getattr(s, 'is_deleted', False):
                    s.is_deleted = False
                    changed = True
                    logger.info(f"Unmarked is_deleted for Series TVDB {s.tvdbid} because Sonarr returned a record", extra={'emoji_type': 'update'})
            except Exception:
                pass

            # capture series-level overview if present
            series_overview = series_data.get('overview') or series_data.get('description') or None
            if series_overview and getattr(s, 'sonarr_series_overview', None) != series_overview:
                s.sonarr_series_overview = series_overview
                changed = True

            # capture season-level overview if present in series_data['seasons']
            for sd in seasons_info:
                try:
                    sn = sd.get('seasonNumber')
                    so = sd.get('overview') or sd.get('description') or None
                    if sn is not None and so:
                        season_row = session.query(SeasonModel).filter_by(series_id=s.id, season_number=int(sn)).first()
                        if season_row and getattr(season_row, 'sonarr_season_overview', None) != so:
                            season_row.sonarr_season_overview = so
                            session.add(season_row)
                            # don't mark changed here for series-level commit; we'll commit after episode updates
                except Exception:
                    pass

            # Update NFO metadata fields from Sonarr data
            overview = series_data.get('overview')
            if overview and s.plot != overview:
                s.plot = overview
                changed = True
            
            # Update other metadata fields if available
            sort_title = series_data.get('sortTitle')
            if sort_title and s.sorttitle != sort_title:
                s.sorttitle = sort_title
                changed = True
            
            # Set original title (usually same as title for TV series)
            original_title = series_data.get('originalTitle') or series_data.get('title')
            if original_title and s.originaltitle != original_title:
                s.originaltitle = original_title
                changed = True
            
            # Handle network/studio
            network = series_data.get('network')
            if network and s.studio != network:
                s.studio = network
                changed = True
            
            # Handle genres (convert list to comma-separated string)
            genres = series_data.get('genres', [])
            if genres and isinstance(genres, list):
                genre_str = ', '.join(genres)
                if s.genres != genre_str:
                    s.genres = genre_str
                    changed = True
            
            # Handle year (from first aired date)
            first_aired = series_data.get('firstAired')
            if first_aired:
                try:
                    # Extract year from date string (format: YYYY-MM-DD)
                    year = int(first_aired.split('-')[0])
                    if s.year != year:
                        s.year = year
                        changed = True
                except (ValueError, IndexError):
                    pass
            
            # Handle premiered date (first aired)
            if first_aired:
                try:
                    from datetime import datetime
                    premiered_date = datetime.strptime(first_aired, '%Y-%m-%d').date()
                    if s.premiered != premiered_date:
                        s.premiered = premiered_date
                        changed = True
                except (ValueError, TypeError):
                    pass
            
            # Handle IMDb ID from external IDs
            imdb_id = series_data.get('imdbId')
            if not imdb_id:
                # Try to get from external IDs if available
                external_ids = series_data.get('externalIds', {})
                if isinstance(external_ids, dict):
                    imdb_id = external_ids.get('imdb')
            if imdb_id and s.imdb_id != imdb_id:
                s.imdb_id = imdb_id
                changed = True
            
            # Handle images
            images = series_data.get('images', [])
            if images and isinstance(images, list):
                for image in images:
                    if isinstance(image, dict):
                        cover_type = image.get('coverType')
                        url = image.get('remoteUrl')
                        if url:
                            # Convert relative URLs to full URLs
                            if url.startswith('/'):
                                url = base_url.replace('/api/v3', '') + url
                            
                            if cover_type == 'poster' and s.poster_url != url:
                                s.poster_url = url
                                changed = True
                            elif cover_type == 'fanart' and s.fanart_url != url:
                                s.fanart_url = url
                                changed = True

            if changed:
                session.add(s)
                session.commit()
                logger.info(f"Enriched series {s.tvdbid} from Sonarr", extra={'emoji_type': 'update'})
            else:
                logger.debug(f"Enrichment for series {s.tvdbid} found no changes", extra={'emoji_type': 'debug'})

            # Attempt to fetch episode-level overviews and persist them
            # Prefer the Sonarr series quality profile name when available. Fall back
            # to deriving a representative series quality from episode file qualities
            # only when the profile name cannot be obtained.
            quality_list = []
            profile_name = None
            try:
                # Sonarr exposes the series quality profile as an id on the series
                qp_id = series_data.get('qualityProfileId') or (series_data.get('qualityProfile') or {}).get('id')
                if qp_id:
                    try:
                        qp_resp = requests.get(f"{base_url}/qualityProfile/{qp_id}", headers=headers, timeout=10)
                        if qp_resp.ok:
                            qp = qp_resp.json()
                            profile_name = qp.get('name') or None
                            if profile_name and getattr(s, 'sonarr_quality', None) != profile_name:
                                s.sonarr_quality = profile_name
                                session.add(s)
                                session.commit()
                                logger.info(f"Set series.sonarr_quality={profile_name} from Sonarr quality profile for series {s.tvdbid}", extra={'emoji_type': 'update'})
                    except Exception:
                        # Non-fatal: if quality profile fetch fails, continue and fall back later
                        profile_name = None
            except Exception:
                profile_name = None
            try:
                eps_resp = requests.get(f"{base_url}/episode", params={'seriesId': series_data.get('id')}, headers=headers, timeout=10)
                if eps_resp.ok:
                    eps = eps_resp.json()
                    for ep in eps:
                        try:
                            season_num = ep.get('seasonNumber')
                            ep_num = ep.get('episodeNumber')
                            ep_overview = ep.get('overview') or ep.get('description') or None
                            if not ep_overview:
                                # even if overview missing, still consider persisting other fields like file info
                                pass
                            # find season/episode rows
                            season_row = session.query(SeasonModel).filter_by(series_id=s.id, season_number=int(season_num)).first()
                            if not season_row:
                                continue
                            episode_row = session.query(EpisodeModel).filter_by(season_id=season_row.id, episode_number=int(ep_num)).first()
                            if episode_row:
                                # persist overview if provided (don't clear existing overview with None)
                                if ep_overview and getattr(episode_row, 'sonarr_episode_overview', None) != ep_overview:
                                    episode_row.sonarr_episode_overview = ep_overview
                                    session.add(episode_row)

                                # Also persist episodeFile details when Sonarr provides them
                                try:
                                    ef = ep.get('episodeFile') or {}
                                    # Determine has_file from Sonarr's flags or presence of episodeFile
                                    has_file = bool(ep.get('hasFile', False) or ef)

                                    # Path - prefer absolute path, fall back to relativePath
                                    ef_path = ef.get('path') or ef.get('relativePath') or None

                                    # Size: Sonarr may use 'size' or 'sizeInBytes'
                                    ef_size = ef.get('size') or ef.get('sizeInBytes')
                                    try:
                                        if ef_size is not None:
                                            ef_size = int(ef_size)
                                    except Exception:
                                        ef_size = None

                                    # Quality: nested object or simple value
                                    ef_quality = None
                                    q = ef.get('quality') or ep.get('quality') or {}
                                    if isinstance(q, dict):
                                        ef_quality = q.get('name') or (q.get('quality') or {}).get('name')
                                    elif q:
                                        ef_quality = str(q)

                                    if ef_quality:
                                        quality_list.append(ef_quality)

                                    # Sonarr path - useful for Jellyfin matching (store absolute file path when available)
                                    sonarrpath_val = ef_path or getattr(season_row, 'sonarrpath', None) or getattr(s, 'sonarrpath', None)

                                    # Initialize updated flag for this episode row
                                    updated = False

                                    # Persist Sonarr episode id if present
                                    try:
                                        sonarr_episode_id = ep.get('id')
                                        if sonarr_episode_id and getattr(episode_row, 'sonarrid', None) != sonarr_episode_id:
                                            episode_row.sonarrid = sonarr_episode_id
                                            updated = True
                                    except Exception:
                                        pass

                                    # Persist sonarr_status if present on the episode object
                                    try:
                                        sonarr_episode_status = ep.get('status') or ep.get('episodeFile', {}).get('status') if ep else None
                                        if sonarr_episode_status and getattr(episode_row, 'sonarr_status', None) != sonarr_episode_status:
                                            episode_row.sonarr_status = sonarr_episode_status
                                            updated = True
                                    except Exception:
                                        pass

                                    # Persist air date if available (airDate or airDateUtc)
                                    try:
                                        air_date_val = ep.get('airDate') or ep.get('airDateUtc')
                                        if air_date_val:
                                            # Normalize to date if possible
                                            try:
                                                # Sonarr returns 'YYYY-MM-DD' or ISO datetime
                                                d = None
                                                if 'T' in air_date_val:
                                                    d = datetime.fromisoformat(air_date_val.replace('Z', '+00:00')).date()
                                                else:
                                                    d = datetime.strptime(air_date_val, '%Y-%m-%d').date()
                                                if d and getattr(episode_row, 'air_date', None) != d:
                                                    episode_row.air_date = d
                                                    updated = True
                                            except Exception:
                                                # ignore parse errors
                                                pass
                                    except Exception:
                                        pass

                                    # Update episode file metadata to match Sonarr (source of truth).
                                    # Set has_file to Sonarr's value and set/clear file fields accordingly.
                                    if getattr(episode_row, 'has_file', None) != has_file:
                                        episode_row.has_file = has_file
                                        updated = True

                                    if has_file:
                                        if ef_path and getattr(episode_row, 'episodefile_path', None) != ef_path:
                                            episode_row.episodefile_path = ef_path
                                            updated = True
                                        if ef_size is not None and getattr(episode_row, 'episodefile_size', None) != ef_size:
                                            episode_row.episodefile_size = ef_size
                                            updated = True
                                        if ef_quality and getattr(episode_row, 'sonarr_quality', None) != ef_quality:
                                            episode_row.sonarr_quality = ef_quality
                                            updated = True
                                        if sonarrpath_val and getattr(episode_row, 'sonarrpath', None) != sonarrpath_val:
                                            episode_row.sonarrpath = sonarrpath_val
                                            updated = True
                                    else:
                                        # Sonarr reports no file for this episode: clear file-specific fields
                                        if getattr(episode_row, 'episodefile_path', None) is not None:
                                            episode_row.episodefile_path = None
                                            updated = True
                                        if getattr(episode_row, 'episodefile_size', None) is not None:
                                            episode_row.episodefile_size = None
                                            updated = True
                                        if getattr(episode_row, 'sonarr_quality', None) is not None:
                                            episode_row.sonarr_quality = None
                                            updated = True
                                        if getattr(episode_row, 'sonarrpath', None) is not None:
                                            episode_row.sonarrpath = None
                                            updated = True

                                    if updated:
                                        session.add(episode_row)
                                        session.commit()
                                        logger.debug(f"Enriched episode row id={episode_row.id}: sonarrid={getattr(episode_row,'sonarrid',None)} air_date={getattr(episode_row,'air_date',None)} sonarr_status={getattr(episode_row,'sonarr_status',None)}", extra={'emoji_type':'debug'})
                                except Exception:
                                    # Be resilient: don't fail enrichment if episodeFile parsing fails
                                    logger.debug("Episode file parsing failed during enrichment", extra={'emoji_type':'debug'})
                                    pass
                        except Exception:
                            continue
                    # commit any episode/season overview changes
                    session.commit()
                    # NOTE: per-episode SubFlow creation was intentionally removed here
                    # to keep SubFlow creation owned by the scheduler/webhook path.
                    # Enrichment should only persist authoritative DB fields. The
                    # scheduler will run the configured flow steps (which include
                    # enrichment as a step) and handle placeholder creation.
                    # Derive a representative series-level quality from episode qualities
                    # only if we couldn't obtain a Sonarr quality profile name above.
                    try:
                        if quality_list and not profile_name:
                            from collections import Counter
                            most_common = Counter(quality_list).most_common(1)[0][0]
                            if getattr(s, 'sonarr_quality', None) != most_common:
                                s.sonarr_quality = most_common
                                session.add(s)
                                session.commit()
                                logger.info(f"Set series.sonarr_quality={most_common} for series {s.tvdbid}", extra={'emoji_type': 'update'})
                    except Exception:
                        # non-fatal if computing series quality fails
                        pass
            except Exception:
                logger.debug("Failed to fetch/persist Sonarr episode overviews", extra={'emoji_type': 'debug'})

            return series_data
        finally:
            session.close()

    except Exception as e:
        logger.error(f"Enrichment failed: {e}", extra={'emoji_type': 'error'})
        return None

def enrich_from_arr(payload: dict = None, media_type: str = None, tvdb_id: int = None, tmdb_id: int = None, arr_id: int = None, is_4k: bool = False):
    """
    Generic dispatcher that inspects provided payload or explicit ids and calls the
    appropriate ARR-specific enrichment function. Returns a normalized dict with
    canonical fields so callers can treat Radarr and Sonarr the same.
    """
    try:
        # Determine media type
        mt = media_type
        if not mt and isinstance(payload, dict):
            if 'series' in payload:
                mt = 'tv'
            elif 'movie' in payload:
                mt = 'movie'

        # Use explicit ids if provided, otherwise extract from payload
        if not tvdb_id and isinstance(payload, dict):
            tvdb_id = payload.get('series', {}).get('tvdbId') if payload.get('series') else None
        if not tmdb_id and isinstance(payload, dict):
            tmdb_id = payload.get('movie', {}).get('tmdbId') if payload.get('movie') else None
        if not arr_id and isinstance(payload, dict):
            arr_id = (payload.get('series') or {}).get('id') or (payload.get('movie') or {}).get('id')

        if mt == 'tv':
            resp = enrich_series_from_sonarr(tvdb_id=tvdb_id, sonarr_id=arr_id, is_4k=is_4k)
            # Normalize result
            if resp is None:
                return {'type': 'tv', 'tvdb': tvdb_id, 'sonarr_id': arr_id, 'overview': None, 'changed': False}
            overview = resp.get('overview') or resp.get('description')
            return {'type': 'tv', 'tvdb': tvdb_id, 'sonarr_id': resp.get('id') or arr_id, 'overview': overview, 'changed': True}

        elif mt == 'movie':
            resp = enrich_movie_from_radarr(tmdb_id=tmdb_id, radarr_id=arr_id, is_4k=is_4k)
            if resp is None:
                return {'type': 'movie', 'tmdb': tmdb_id, 'radarr_id': arr_id, 'overview': None, 'changed': False}
            overview = resp.get('overview') or resp.get('plot') or resp.get('description')
            return {'type': 'movie', 'tmdb': tmdb_id, 'radarr_id': resp.get('id') or arr_id, 'overview': overview, 'changed': True}

        else:
            logger.debug("enrich_from_arr could not determine media type", extra={'emoji_type': 'debug'})
            return None

    except Exception as e:
        logger.error(f"enrich_from_arr dispatcher error: {e}", extra={'emoji_type': 'error'})
        return None


# Legacy background enrichment helper removed. Enrichment should be scheduled
# and executed by the ActionScheduler (use flow_enrich_series/flow_enrich_movie
# or the job worker enqueue_enrichment_job for manual/test invocation).

def flow_enrich_series(session, ent_id: int, model: Type, action: str) -> bool:
    """Scheduler-compatible step to run Sonarr enrichment for a series.

    This adapter matches the signature expected by ActionScheduler step functions
    (session, ent_id, model, action). It looks up the series' TVDB id from the
    DB and calls the existing enrichment routine. Return True on success.
    """
    try:
        # Lazy import of models to avoid top-level circular imports
        from services.postgres.models import Series as SeriesModel, Episode as EpisodeModel, Season as SeasonModel

        series = None

        # If scheduler passed an Episode context (common when creating per-episode SubFlows),
        # resolve the Episode -> Season -> Series chain to find the correct Series id.
        if model is not None and getattr(model, '__name__', '').lower() == 'episode':
            ep = session.query(EpisodeModel).get(ent_id)
            if not ep:
                logger.error(f"flow_enrich_series: Episode id {ent_id} not found", extra={'emoji_type': 'error'})
                return False
            season = session.query(SeasonModel).get(ep.season_id) if getattr(ep, 'season_id', None) else None
            series_id = season.series_id if season else None
            if not series_id:
                logger.error(f"flow_enrich_series: Could not resolve Series for Episode id {ent_id}", extra={'emoji_type': 'error'})
                return False
            series = session.query(SeriesModel).get(series_id)
        else:
            # Default: ent_id is a Series id
            series = session.query(SeriesModel).get(ent_id)

        if not series:
            logger.error(f"flow_enrich_series: Series id {ent_id if series is None else series.id} not found", extra={'emoji_type': 'error'})
            return False

        tvdb = getattr(series, 'tvdbid', None) or getattr(series, 'tvdb_id', None)
        sonarr_id = getattr(series, 'sonarrid', None)
        is_4k = bool(getattr(series, 'is_4k', False))
        enrich_series_from_sonarr(tvdb_id=tvdb, sonarr_id=sonarr_id, is_4k=is_4k)
        return True
    except Exception as e:
        logger.error(f"flow_enrich_series failed: {e}", extra={'emoji_type': 'error'})
        return False


def flow_enrich_movie(session, ent_id: int, model: Type, action: str) -> bool:
    """Scheduler-compatible step to run Radarr enrichment for a movie."""
    try:
        from services.postgres.models import Movie as MovieModel
        movie = session.query(MovieModel).get(ent_id)
        if not movie:
            logger.error(f"flow_enrich_movie: Movie id {ent_id} not found", extra={'emoji_type': 'error'})
            return False

        tmdb = getattr(movie, 'tmdbid', None)
        radarr_id = getattr(movie, 'radarrid', None)
        is_4k = bool(getattr(movie, 'is_4k', False))
        enrich_movie_from_radarr(tmdb_id=tmdb, radarr_id=radarr_id, is_4k=is_4k)
        return True
    except Exception as e:
        logger.error(f"flow_enrich_movie failed: {e}", extra={'emoji_type': 'error'})
        return False
