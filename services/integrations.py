import os, glob, shutil, time, threading, requests, subprocess, platform, re, fnmatch, sys
from typing import Type
from core.config import settings
from core.logger import logger
from services.postgres.models import Episode, Movie, Season, Series, SubFlow
from services.utils import (
    resolve_final_folder, sanitize_filename, strip_status_markers, get_series_folder,
    get_arr_config
)
from services.plex_client import plex
from services.utils import get_movie_by_id
from sqlalchemy.orm import Session
from services.postgres.models import Series
from services.postgres.utils import safe_commit


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
                msg = f"Movie with ID {ent_id} not found"
                logger.error(msg, extra={'emoji_type': 'error'})
                raise Exception(msg)

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
                msg = f"Episode with ID {ent_id} not found"
                logger.error(msg, extra={'emoji_type': 'error'})
                raise Exception(msg)

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
            msg = f'Unsupported model type for delete_dummy_file: {model}'
            logger.error(msg, extra={'emoji_type': 'error'})
            raise Exception(msg)
    except Exception as e:
        logger.error(f"Error deleting dummy file for {model.__name__} ID {ent_id}: {e}", extra={'emoji_type': 'error'})
        raise e

def update_placeholder_status(dbSession: Session, ent_id: int, model: Type, action: str, status: str = None):
    """Update the status of a placeholder file"""
    try:
        if not status:
            # Explicitly keep status as None for imports/upgrades (real files)
            if 'import' in action or 'upgrade' in action:
                status = None
            elif 'add' in action:
                status = "Request"
            elif 'delete' in action:
                status = "Request"
                
        if model is Movie:
            movie = dbSession.query(Movie).get(ent_id)
            if not movie:
                logger.error(f"Movie with ID {ent_id} not found", extra={'emoji_type': 'error'})
                return False
            
            movie.placeholder_status = status
            safe_commit(dbSession, movie)
            logger.info(f"Updated movie placeholder status to '{status}' for ID {ent_id}", extra={'emoji_type': 'update'})
            return True
        
        elif model is Episode:
            episode = dbSession.query(Episode).get(ent_id)
            if not episode:
                logger.error(f"Episode with ID {ent_id} not found", extra={'emoji_type': 'error'})
                return False
            
            episode.placeholder_status = status
            safe_commit(dbSession, episode)
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
                            safe_commit(db_sess, movie)
                            logger.debug(f"Marked DB Movie deleted: {movie.title}", extra={'emoji_type': 'debug'})

                    elif media_type == 'tv':
                        series = db_sess.query(Series).filter_by(tvdbid=str(tvdb_id)).first() if tvdb_id else None
                        if series:
                            series.is_deleted = True
                            series.jellyfin_dummy_id = None
                            series.placeholder_exists = False
                            db_sess.add(series)
                            safe_commit(db_sess, series)
                            seasons = db_sess.query(Season).filter_by(series_id=series.id).all()
                            for s in seasons:
                                s.is_deleted = True
                                s.jellyfin_dummy_id = None
                                s.placeholder_exists = False
                                db_sess.add(s)
                                safe_commit(db_sess, s)
                                eps = db_sess.query(Episode).filter_by(season_id=s.id).all()
                                for ep in eps:
                                    ep.is_deleted = True
                                    ep.jellyfin_dummy_id = None
                                    ep.placeholder_exists = False
                                    db_sess.add(ep)
                                    safe_commit(db_sess, ep)
                            logger.debug(f"Marked DB Series, seasons, and episodes deleted for: {series.title}", extra={'emoji_type': 'debug'})
                except Exception as e:
                    logger.error(f"Failed to mark DB records deleted: {e}", extra={'emoji_type': 'error'})

        return True

    except Exception as e:
        logger.error(f"Error deleting placeholder: {e}", extra={'emoji_type': 'error'})
        raise e

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
                    safe_commit(session, movie)

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
            safe_commit(session, movie)
            logger.info(f"Created placeholder file for '{movie.title}'", extra={'emoji_type': 'success'})
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
                safe_commit(session, series)
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
                safe_commit(session, series)
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
                    safe_commit(session, special_ep)
                logger.info(f"Deleting special season {special_season.id} (season 0)", extra={'emoji_type': 'delete'})
                session.delete(special_season)
                safe_commit(session, special_season)
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
        sf_eps = session.query(SubFlow).filter(
            SubFlow.steps == "delayed_placeholders",
            SubFlow.series_id == series.id,
            SubFlow.status != "DONE"
        ).order_by(SubFlow.id).all()

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
                    safe_commit(session, new_subflow)
                
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
                safe_commit(session, subflow)
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
                safe_commit(session, subflow)
                continue

            # Ensure season info available
            season_rec = session.query(Season).get(ep.season_id) if ep.season_id else None
            if not season_rec:
                logger.error(f"Missing season for episode id {ep.id}", extra={'emoji_type': 'error'})
                subflow.status = "DONE"
                session.add(subflow)
                safe_commit(session, subflow)
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
                    safe_commit(session, subflow)
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
                                    safe_commit(session, series)
            except Exception:
                logger.debug("Sonarr lookup by TVDB failed or returned no data", extra={'emoji_type': 'debug'})

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
                safe_commit(session, ep)
                placeholder_count += 1
                logger.debug(f"Persisted placeholder for {series.title} S{season_num}E{episode_num}", extra={'emoji_type': 'debug'})
                
                # Only mark this subflow entry DONE if the dummy file was successfully created
                subflow.status = "DONE"
                session.add(subflow)
                safe_commit(session, subflow)
            else:
                failed_count += 1
                logger.error(f"Failed to create dummy file for {series.title} S{season_num}E{episode_num} - keeping SubFlow as PENDING for scheduler retry", extra={'emoji_type': 'error'})
                # Don't mark SubFlow as DONE - let scheduler retry it later
                
                # If this is the current episode that triggered the workflow, fail immediately
                if ep.id == current_ep.id:
                    logger.error(f"Current episode {current_ep.id} failed to create dummy file - failing workflow immediately", extra={'emoji_type': 'error'})
                    return False

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
        from services.postgres.models import Series as SeriesModel
        config = get_arr_config('tv', is_4k)
        if not config:
            logger.error("Sonarr config missing for enrichment", extra={'emoji_type': 'error'})
            return None

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
            except Exception as e:
                logger.error(f"Sonarr lookup by tvdb failed: {e}", extra={'emoji_type': 'error'})

        if not series_data:
            logger.debug("No Sonarr series data found during enrichment", extra={'emoji_type': 'debug'})
            return None

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
            has_files = bool(series_data.get('hasFile', False) or series_data.get('seasons'))
            monitored = bool(series_data.get('monitored', False))
            if series_data.get('id') and s.sonarrid != series_data.get('id'):
                s.sonarrid = series_data.get('id')
                changed = True
            if path and s.filepath != path:
                s.filepath = path
                changed = True
            if s.sonarr_monitored != monitored:
                s.sonarr_monitored = monitored
                changed = True
            if s.has_files != has_files:
                s.has_files = has_files
                changed = True

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

            return series_data
        finally:
            session.close()

    except Exception as e:
        logger.error(f"Enrichment failed: {e}", extra={'emoji_type': 'error'})
        return None

def enrich_movie_metadata(session, ent_id, model, action):
    """Enrich movie with metadata from Radarr before creating NFO"""
    if model != Movie:
        logger.info(f"Skipping enrichment for non-Movie entity: {model.__name__}", extra={'emoji_type': 'skip'})
        return True
    
    try:
        movie = session.query(Movie).get(ent_id)
        if not movie:
            logger.error(f"Movie {ent_id} not found for enrichment", extra={'emoji_type': 'error'})
            return False
        
        logger.info(f"Enriching movie metadata from Radarr: {movie.title}", extra={'emoji_type': 'update'})
        
        # Call enrichment with movie's TMDB ID and 4K status
        radarr_data = enrich_movie_from_radarr(
            tmdb_id=movie.tmdbid,
            radarr_id=movie.radarrid,
            is_4k=movie.is_4k
        )
        
        if radarr_data:
            logger.info(f"Successfully enriched movie {movie.title} with Radarr metadata", extra={'emoji_type': 'success'})
            return True
        else:
            logger.warning(f"Failed to enrich movie {movie.title} - continuing anyway", extra={'emoji_type': 'warning'})
            # Don't fail the entire flow if enrichment fails
            return True
            
    except Exception as e:
        logger.error(f"Error during movie enrichment: {e}", extra={'emoji_type': 'error'})
        # Don't fail the entire flow if enrichment fails
        return True

def enrich_series_metadata(session, ent_id, model, action):
    """Enrich series with metadata from Sonarr before creating NFO"""
    try:
        # Handle both direct Series calls and Episode-based SubFlows
        if model == Series:
            series = session.query(Series).get(ent_id)
        elif model == Episode:
            # When called from Episode SubFlow, get the series through episode->season->series
            episode = session.query(Episode).get(ent_id)
            if not episode or not episode.season:
                logger.error(f"Episode {ent_id} or its season not found for series enrichment", extra={'emoji_type': 'error'})
                return False
            series = episode.season.series
        else:
            logger.info(f"Skipping series enrichment for unsupported entity: {model.__name__}", extra={'emoji_type': 'skip'})
            return True
        
        if not series:
            logger.error(f"Series not found for enrichment", extra={'emoji_type': 'error'})
            return False
        
        logger.info(f"Enriching series metadata from Sonarr: {series.title}", extra={'emoji_type': 'update'})
        
        # Call enrichment with series' TVDB ID and 4K status
        sonarr_data = enrich_series_from_sonarr(
            tvdb_id=series.tvdbid,
            sonarr_id=series.sonarrid,
            is_4k=series.is_4k
        )
        
        if sonarr_data:
            logger.info(f"Successfully enriched series {series.title} with Sonarr metadata", extra={'emoji_type': 'success'})
            return True
        else:
            logger.error(f"Failed to enrich series {series.title} - no data from Sonarr", extra={'emoji_type': 'error'})
            return False
            
    except Exception as e:
        logger.error(f"Error during series enrichment: {e}", extra={'emoji_type': 'error'})
        return False

def enrich_season_metadata(session, ent_id, model, action):
    """Enrich season with metadata from Sonarr before creating NFO"""
    try:
        from services.postgres.models import Season
        
        # Handle both direct Season calls and Episode-based SubFlows
        if model.__name__ == 'Season':
            season = session.query(Season).get(ent_id)
        elif model == Episode:
            # When called from Episode SubFlow, get the season through episode->season
            episode = session.query(Episode).get(ent_id)
            if not episode or not episode.season:
                logger.error(f"Episode {ent_id} or its season not found for season enrichment", extra={'emoji_type': 'error'})
                return False
            season = episode.season
        else:
            logger.info(f"Skipping season enrichment for unsupported entity: {model.__name__}", extra={'emoji_type': 'skip'})
            return True
        
        if not season:
            logger.error(f"Season not found for enrichment", extra={'emoji_type': 'error'})
            return False
        
        logger.info(f"Enriching season metadata from Sonarr: {season.title} S{season.season_number}", extra={'emoji_type': 'update'})
        
        # Get the parent series
        series = season.series
        if not series:
            logger.error(f"No parent series found for season {season.id}", extra={'emoji_type': 'error'})
            return False
        
        # Call series enrichment to get complete data including seasons
        sonarr_data = enrich_series_from_sonarr(
            tvdb_id=series.tvdbid,
            sonarr_id=series.sonarrid,
            is_4k=series.is_4k
        )
        
        if not sonarr_data:
            logger.error(f"Failed to get series data from Sonarr for season enrichment", extra={'emoji_type': 'error'})
            return False
        
        if sonarr_data and 'seasons' in sonarr_data:
            # Find matching season in Sonarr data
            seasons_data = sonarr_data.get('seasons', [])
            matching_season = None
            for season_data in seasons_data:
                if season_data.get('seasonNumber') == season.season_number:
                    matching_season = season_data
                    break
            
            if matching_season:
                changed = False
                
                # Update season-specific metadata
                if matching_season.get('monitored') is not None:
                    monitored = bool(matching_season.get('monitored'))
                    if season.sonarr_monitored != monitored:
                        season.sonarr_monitored = monitored
                        changed = True
                
                # Season typically doesn't have separate TVDB/IMDb IDs in Sonarr
                # but some do, so let's check
                season_tvdb_id = matching_season.get('tvdbId')
                if season_tvdb_id and season.tvdbid != season_tvdb_id:
                    season.tvdbid = season_tvdb_id
                    changed = True
                
                # Season images
                images = matching_season.get('images', [])
                if images:
                    for image in images:
                        if isinstance(image, dict):
                            cover_type = image.get('coverType')
                            url = image.get('remoteUrl')
                            if url:
                                config = get_arr_config('tv', series.is_4k)
                                if config and url.startswith('/'):
                                    url = config['url'].replace('/api/v3', '') + url
                                
                                if cover_type == 'poster' and season.poster_url != url:
                                    season.poster_url = url
                                    changed = True
                                elif cover_type == 'fanart' and season.fanart_url != url:
                                    season.fanart_url = url
                                    changed = True
                
                if changed:
                    session.add(season)
                    session.commit()
                    logger.info(f"Successfully enriched season {season.title} S{season.season_number}", extra={'emoji_type': 'success'})
                else:
                    logger.debug(f"No changes needed for season {season.title} S{season.season_number}", extra={'emoji_type': 'debug'})
            else:
                logger.error(f"Season {season.season_number} not found in Sonarr data", extra={'emoji_type': 'error'})
                return False
        else:
            logger.error(f"No seasons data in Sonarr response", extra={'emoji_type': 'error'})
            return False
        
        return True
            
    except Exception as e:
        logger.error(f"Error during season enrichment: {e}", extra={'emoji_type': 'error'})
        return False

def enrich_episode_metadata(session, ent_id, model, action):
    """Enrich episode with metadata from Sonarr before creating NFO"""
    try:
        # Handle both direct Episode calls and Episode-based SubFlows  
        if model == Episode:
            episode = session.query(Episode).get(ent_id)
        else:
            logger.info(f"Skipping episode enrichment for non-Episode entity: {model.__name__}", extra={'emoji_type': 'skip'})
            return True
        
        if not episode:
            logger.error(f"Episode {ent_id} not found for enrichment", extra={'emoji_type': 'error'})
            return False
        
        logger.info(f"Enriching episode metadata from Sonarr: {episode.title} S{episode.season.season_number if episode.season else '?'}E{episode.episode_number}", extra={'emoji_type': 'update'})
        
        # Get the parent series through season
        series = episode.season.series if episode.season else None
        if not series:
            logger.error(f"No parent series found for episode {episode.id}", extra={'emoji_type': 'error'})
            return False
        
        # Get episode data from Sonarr
        config = get_arr_config('tv', series.is_4k)
        if not config:
            logger.error(f"No Sonarr config for episode enrichment", extra={'emoji_type': 'error'})
            return False
        
        # Check if series has sonarrid
        if not series.sonarrid:
            logger.error(f"Series {series.title} has no sonarrid - cannot enrich episodes", extra={'emoji_type': 'error'})
            return False
        
        headers = {'X-Api-Key': config['api_key']}
        base_url = config['url']
        
        try:
            # Get episodes for the series
            r = requests.get(f"{base_url}/episode", params={'seriesId': series.sonarrid}, headers=headers, timeout=10)
            if r.status_code == 200:
                episodes_data = r.json()
                
                # Find matching episode
                matching_episode = None
                for ep_data in episodes_data:
                    if (ep_data.get('seasonNumber') == episode.season.season_number and 
                        ep_data.get('episodeNumber') == episode.episode_number):
                        matching_episode = ep_data
                        break
                
                if matching_episode:
                    changed = False
                    
                    # Update episode metadata
                    overview = matching_episode.get('overview')
                    if overview and episode.plot != overview:
                        episode.plot = overview
                        changed = True
                    
                    runtime = matching_episode.get('runtime')
                    if runtime and episode.runtime != runtime:
                        episode.runtime = runtime
                        changed = True
                    
                    # Episode TVDB ID
                    tvdb_id = matching_episode.get('tvdbId')
                    if tvdb_id and episode.tvdbid != tvdb_id:
                        episode.tvdbid = tvdb_id
                        changed = True
                    
                    # Air date
                    air_date = matching_episode.get('airDate')
                    if air_date:
                        try:
                            from datetime import datetime
                            parsed_date = datetime.strptime(air_date, '%Y-%m-%d').date()
                            if episode.air_date != parsed_date:
                                episode.air_date = parsed_date
                                changed = True
                        except (ValueError, TypeError):
                            pass
                    
                    ep_sonarrid = matching_episode.get('id')
                    if ep_sonarrid and episode.sonarrid != ep_sonarrid:
                        episode.sonarrid = ep_sonarrid
                        changed = True
           
                    # Episode thumbnail
                    images = matching_episode.get('images', [])
                    for image in images:
                        if isinstance(image, dict) and image.get('coverType') == 'screenshot':
                            url = image.get('remoteUrl')
                            if url:
                                if url.startswith('/'):
                                    url = base_url.replace('/api/v3', '') + url
                                if episode.thumb_url != url:
                                    episode.thumb_url = url
                                    changed = True
                            break
                    
                    if changed:
                        session.add(episode)
                        session.commit()
                        logger.info(f"Successfully enriched episode {episode.title}", extra={'emoji_type': 'success'})
                    else:
                        logger.debug(f"No changes needed for episode {episode.title}", extra={'emoji_type': 'debug'})
                else:
                    logger.error(f"Episode S{episode.season.season_number}E{episode.episode_number} not found in Sonarr data", extra={'emoji_type': 'error'})
                    return False
            else:
                logger.error(f"Failed to get episodes from Sonarr: {r.status_code}", extra={'emoji_type': 'error'})
                return False
        
        except Exception as e:
            logger.error(f"Error fetching episode data from Sonarr: {e}", extra={'emoji_type': 'error'})
            return False
        
        return True
            
    except Exception as e:
        logger.error(f"Error during episode enrichment: {e}", extra={'emoji_type': 'error'})
        return False


def check_series_ready_for_enrichment(session, ent_id, model, action):
    """
    Check if all episodes in a series have completed delayed_placeholders.
    If all are ready, create ONE series-level enrichment SubFlow.
    Then wait for that enrichment to complete before returning True.
    
    Uses trigger_id to ensure each trigger group has its own enrichment cycle.
    This allows multiple independent workflows for the same series.
    
    This is a barrier step that:
    1. Waits for all episodes FROM THIS TRIGGER to finish placeholders
    2. Creates series enrichment SubFlow (once per trigger)
    3. Waits for enrichment to complete
    4. Returns True when enrichment is DONE
    """
    from services.postgres.models import SubFlow, Episode, Season, Series
    
    # Get the series ID and trigger_id based on the model type
    if model == Episode:
        episode = session.query(Episode).get(ent_id)
        if not episode or not episode.season:
            logger.error(f"Episode {ent_id} not found or has no season", extra={'emoji_type': 'error'})
            return False
        series_id = episode.season.series_id
        
        logger.debug(f"🔍 Looking for SubFlow: episode_id={ent_id}, series_id={series_id}, action={action}", extra={'emoji_type': 'debug'})
        
        # Get trigger_id from the current episode SubFlow - filter by episode_id + action + status
        # This avoids hardcoding step names
        current_sf = session.query(SubFlow).filter(
            SubFlow.episode_id == ent_id,
            SubFlow.series_id == series_id,
            SubFlow.action == action,
            SubFlow.status.in_(['QUEUED'])  # Active SubFlows only
        ).order_by(SubFlow.id.desc()).first()  # Get most recent if multiple
        
        if current_sf:
            logger.debug(f"✅ Found SubFlow {current_sf.id}: trigger_id={current_sf.trigger_id}, status={current_sf.status}, steps={current_sf.steps}", extra={'emoji_type': 'debug'})
            trigger_id = current_sf.trigger_id
        else:
            logger.warning(f"❌ No QUEUED SubFlow found for episode_id={ent_id}, series_id={series_id}, action={action}", extra={'emoji_type': 'warning'})
            
            # Try to find ANY SubFlow for this episode to debug
            all_sfs = session.query(SubFlow).filter(
                SubFlow.episode_id == ent_id,
                SubFlow.action == action
            ).all()
            
            if all_sfs:
                logger.debug(f"🔍 Found {len(all_sfs)} SubFlow(s) for episode {ent_id} (any status):", extra={'emoji_type': 'debug'})
                for sf in all_sfs:
                    logger.debug(f"   SubFlow {sf.id}: status={sf.status}, trigger_id={sf.trigger_id}, steps={sf.steps}, step_index={sf.step_index}", extra={'emoji_type': 'debug'})
            else:
                logger.warning(f"❌ No SubFlows found at all for episode_id={ent_id}, action={action}", extra={'emoji_type': 'warning'})
            
            trigger_id = None
        
    elif model == Series:
        series_id = ent_id
        
        logger.debug(f"🔍 Looking for SubFlow: series_id={series_id}, action={action}", extra={'emoji_type': 'debug'})
        
        # For series-level calls, get trigger_id from any active SubFlow for this series
        current_sf = session.query(SubFlow).filter(
            SubFlow.series_id == series_id,
            SubFlow.action == action,
            SubFlow.status.in_(['QUEUED'])
        ).order_by(SubFlow.id.desc()).first()
        
        if current_sf:
            logger.debug(f"✅ Found SubFlow {current_sf.id}: trigger_id={current_sf.trigger_id}, status={current_sf.status}", extra={'emoji_type': 'debug'})
            trigger_id = current_sf.trigger_id
        else:
            logger.warning(f"❌ No PENDING/QUEUED SubFlow found for series_id={series_id}, action={action}", extra={'emoji_type': 'warning'})
            trigger_id = None
    else:
        logger.error(f"check_series_ready_for_enrichment expects Episode or Series model, got {model.__name__}", extra={'emoji_type': 'error'})
        return False
    
    if trigger_id is None:
        logger.warning(f"⚠️ No trigger_id found for {model.__name__} {ent_id} - current_sf was {'None' if not current_sf else 'found but had None trigger_id'}", extra={'emoji_type': 'warning'})
    
    # STEP 1: Check if enrichment SubFlow already exists FOR THIS TRIGGER and handle its status
    existing_enrichment = session.query(SubFlow).filter(
        SubFlow.series_id == series_id,
        SubFlow.steps == "enrich_comprehensive_metadata",
        SubFlow.episode_id.is_(None),  # Series-level SubFlow
        SubFlow.trigger_id == trigger_id  # CRITICAL: Same trigger group
    ).first()
    
    if existing_enrichment:
        # Enrichment SubFlow exists for this trigger - check its status
        if existing_enrichment.status == 'DONE':
            # Enrichment complete! Episode can proceed
            logger.debug(f"Series {series_id} enrichment complete for trigger {trigger_id} (SubFlow {existing_enrichment.id})", extra={'emoji_type': 'success'})
            return True
        elif existing_enrichment.status in ['PENDING', 'QUEUED']:
            # Enrichment in progress - wait for it
            logger.debug(f"Waiting for series {series_id} enrichment (trigger {trigger_id}, SubFlow {existing_enrichment.id} status: {existing_enrichment.status})", extra={'emoji_type': 'wait'})
            return False  # Keep episode SubFlow PENDING until enrichment is DONE
        elif existing_enrichment.status == 'FAILED':
            # Enrichment failed - proceed anyway with warning
            logger.warning(f"Series {series_id} enrichment FAILED for trigger {trigger_id} - proceeding anyway", extra={'emoji_type': 'warning'})
            return True
        else:
            # Unknown status - proceed with warning
            logger.warning(f"Series {series_id} enrichment has status {existing_enrichment.status} (trigger {trigger_id}) - proceeding anyway", extra={'emoji_type': 'warning'})
            return True
    
    # STEP 2: No enrichment SubFlow exists for this trigger yet - check if all episodes FROM THIS TRIGGER are ready
    # Check how many episodes FROM THIS TRIGGER are still on delayed_placeholders (excluding current one if Episode)
    query = session.query(SubFlow).filter(
        SubFlow.series_id == series_id,
        SubFlow.steps == "delayed_placeholders",
        SubFlow.status != "DONE",
        SubFlow.trigger_id == trigger_id  # CRITICAL: Only count SubFlows from THIS trigger
    )
    
    # If this is being called from an Episode that just completed, exclude it from the count
    if model == Episode:
        query = query.filter(SubFlow.episode_id != ent_id)
    
    remaining_placeholders = query.count()
    
    logger.debug(f"Series {series_id} (trigger {trigger_id}): {remaining_placeholders} episodes still on delayed_placeholders (excluding current)", extra={'emoji_type': 'debug'})
    
    # Check if all episodes FROM THIS TRIGGER have completed their delayed_placeholders step
    if remaining_placeholders > 0:
        # Not all episodes from this trigger are ready yet - wait for them to complete
        logger.verbose(f"Series {series_id} (trigger {trigger_id}) not ready: {remaining_placeholders} episodes still processing placeholders", extra={'emoji_type': 'wait'})
        return False  # This episode waits
    
    # STEP 3: All episodes from this trigger ready! Create the series-level enrichment SubFlow for THIS TRIGGER
    logger.info(f"🚀 All episodes ready for trigger {trigger_id}! Creating series-level enrichment for series {series_id}", extra={'emoji_type': 'success'})
    
    try:
        # Double-check if series-level enrichment SubFlow was just created by another episode FROM THIS TRIGGER
        existing_enrichment = session.query(SubFlow).filter(
            SubFlow.series_id == series_id,
            SubFlow.action == action,
            SubFlow.steps == "enrich_comprehensive_metadata",
            SubFlow.episode_id.is_(None),  # Series-level
            SubFlow.trigger_id == trigger_id  # CRITICAL: Same trigger group
        ).first()
        
        if existing_enrichment:
            logger.debug(f"Series {series_id} enrichment SubFlow {existing_enrichment.id} already exists for trigger {trigger_id} (created by another episode)", extra={'emoji_type': 'debug'})
            # Fall through to STEP 4 to wait for it
        else:
            # Create the series-level enrichment SubFlow FOR THIS TRIGGER
            series_subflow = SubFlow(
                series_id=series_id,
                episode_id=None,  # Series-level, not episode-specific
                action=action,
                steps="enrich_comprehensive_metadata",
                branch=str(series_id),
                status="PENDING",  # PENDING so polling picks it up
                trigger_id=trigger_id,  # CRITICAL: maintain trigger grouping
                step_index=0
            )
            session.add(series_subflow)
            session.commit()
            
            logger.info(f"✅ Created series-level enrichment SubFlow {series_subflow.id} for series {series_id} (trigger_id={trigger_id})", extra={'emoji_type': 'success'})
            
    except Exception as e:
        logger.warning(f"Failed to create enrichment SubFlow for series {series_id} (trigger {trigger_id}): {e}", extra={'emoji_type': 'warning'})
    
    # STEP 4: Wait for enrichment to complete
    # The enrichment SubFlow now exists (either we just created it or another episode did)
    # Return False so this episode stays PENDING and will check again on next poll
    logger.info(f"⏳ Episode {ent_id} waiting for series {series_id} enrichment (trigger {trigger_id}) to complete", extra={'emoji_type': 'wait'})
    return False  # Episode waits for enrichment to finish


def enrich_comprehensive_metadata(session, ent_id, model, action):
    """Comprehensive enrichment: series, seasons, and episodes metadata from Sonarr
    
    This function is called by a SERIES-LEVEL SubFlow (not per episode).
    It enriches the entire series, all seasons, and all episodes in one go.
    """
    try:
        # Handle both direct Series calls and Episode-based SubFlows
        if model == Series:
            # For series-level SubFlows, ent_id should be the series_id
            if ent_id is None:
                logger.error(f"Series ID is None for comprehensive enrichment", extra={'emoji_type': 'error'})
                return False
            series = session.query(Series).get(ent_id)
        elif model == Episode:
            # When called from Episode SubFlow (shouldn't happen anymore), get the series
            episode = session.query(Episode).get(ent_id)
            if not episode or not episode.season:
                logger.error(f"Episode {ent_id} or its season not found for comprehensive enrichment", extra={'emoji_type': 'error'})
                return False
            series = episode.season.series
        else:
            logger.info(f"Skipping comprehensive enrichment for unsupported entity: {model.__name__}", extra={'emoji_type': 'skip'})
            return True
        
        if not series:
            logger.error(f"Series not found for comprehensive enrichment (ent_id={ent_id})", extra={'emoji_type': 'error'})
            return False
        
        logger.info(f"🔄 Starting comprehensive metadata enrichment for series: {series.title}", extra={'emoji_type': 'update'})
        
        # Step 1: Enrich Series metadata
        logger.info(f"📺 Enriching series metadata...", extra={'emoji_type': 'update'})
        series_success = enrich_series_metadata(session, ent_id, model, action)
        if not series_success:
            logger.warning(f"Series metadata enrichment failed, continuing with seasons/episodes", extra={'emoji_type': 'warning'})
        
        # Step 2: Enrich Season metadata for all seasons
        seasons = session.query(Season).filter(Season.series_id == series.id).all()
        
        enriched_seasons = 0
        for season in seasons:
            try:
                logger.info(f"📁 Enriching season {season.season_number} metadata...", extra={'emoji_type': 'update'})
                # Call season enrichment with the season directly
                season_success = enrich_season_metadata(session, season.id, Season, action)
                if season_success:
                    enriched_seasons += 1
            except Exception as e:
                logger.error(f"Failed to enrich season {season.id}: {e}", extra={'emoji_type': 'error'})
                continue
        
        logger.info(f"✅ Enriched {enriched_seasons}/{len(seasons)} seasons", extra={'emoji_type': 'success'})
        
        # Step 3: Enrich Episode metadata for all episodes
        episodes = session.query(Episode).join(Season).filter(
            Season.series_id == series.id
        ).all()
        
        if not episodes:
            logger.info(f"No episodes found for series {series.title}", extra={'emoji_type': 'info'})
            return True
        
        logger.info(f"🎬 Enriching metadata for {len(episodes)} episodes...", extra={'emoji_type': 'update'})
        enriched_episodes = 0
        failed_episodes = 0
        for episode in episodes:
            try:
                # Call episode enrichment with the episode directly
                episode_success = enrich_episode_metadata(session, episode.id, Episode, action)
                if episode_success:
                    enriched_episodes += 1
                else:
                    failed_episodes += 1
            except Exception as e:
                logger.error(f"Failed to enrich episode {episode.id}: {e}", extra={'emoji_type': 'error'})
                failed_episodes += 1
                continue
        
        logger.info(f"✅ Comprehensive enrichment completed: Series {'✓' if series_success else '✗'}, {enriched_seasons}/{len(seasons)} seasons, {enriched_episodes}/{len(episodes)} episodes", extra={'emoji_type': 'success'})
        
        # Check if enrichment failed for critical components
        if not series_success:
            logger.error(f"Series metadata enrichment failed - cannot proceed", extra={'emoji_type': 'error'})
            return False
        
        if failed_episodes > 0:
            logger.error(f"Failed to enrich {failed_episodes}/{len(episodes)} episodes - enrichment incomplete", extra={'emoji_type': 'error'})
            return False
        
        # Mark series as enriched to prevent duplicate enrichment
        if hasattr(series, 'metadata_enriched'):
            series.metadata_enriched = True
            session.add(series)
            session.commit()
        
        # After series-level enrichment, create episode-level SubFlows for the next step in flow
        if model == Series:
            logger.info(f"🔄 Creating episode SubFlows for next step in flow", extra={'emoji_type': 'process'})
            
            # Get all episodes in this series that need to continue the flow
            episodes_to_process = session.query(Episode).join(Season).filter(
                Season.series_id == series.id
            ).all()
            
            created_count = 0
            for episode in episodes_to_process:
                # Check if episode already has a SubFlow for the branching step
                existing_branch_flow = session.query(SubFlow).filter(
                    SubFlow.episode_id == episode.id,
                    SubFlow.action == action,
                    SubFlow.steps.like("%jellyfin%")  # Contains jellyfin branching
                ).first()
                
                if not existing_branch_flow:
                    # Create episode-level SubFlow that will hit the branching step in the flow
                    # The flow manager will handle the jellyfin/plex branching automatically
                    episode_subflow = SubFlow(
                        series_id=series.id,
                        episode_id=episode.id,
                        action=action,
                        steps="create_jellyfin_nfo,refresh_jellyfin_dummy,verify_dummy_scan_jellyfin,update_placeholder_status,verify_dummy_scan_jellyfin",
                        branch="jellyfin",  # Default to jellyfin branch
                        status="PENDING",
                        step_index=0  # Start at first step in the branch
                    )
                    session.add(episode_subflow)
                    
                    # Update episode to reflect its new step
                    episode.current_step_name = "create_jellyfin_nfo"
                    episode.status = 'PENDING'
                    session.add(episode)
                    
                    created_count += 1
            
            session.commit()
            logger.info(f"✅ Created {created_count} episode SubFlows for platform processing", extra={'emoji_type': 'success'})
        
        return True
        
    except Exception as e:
        logger.error(f"Error during comprehensive enrichment: {e}", exc_info=True, extra={'emoji_type': 'error'})
        return False
