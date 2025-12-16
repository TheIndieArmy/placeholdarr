import os
import re
import time
from typing import List, Optional, Dict, Any, Callable, Tuple, Union, Type
from urllib.parse import quote
import requests
from core.config import settings
from core.logger import logger
from core.handler_logging import use_handler_session
from services.utils import strip_status_markers
from services.postgres.models import Movie, Series, Season, Episode, SubFlow
from services.postgres.utils import safe_commit
from services.nfo_manager import (
    create_movie_nfo, create_series_nfo, create_season_nfo, create_episode_nfo,
    write_nfo_file, update_nfo_status, delete_nfo_file, get_nfo_path
)
from urllib.parse import quote_plus
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
import inspect

# Initialize a shared session with default headers
session = requests.Session()
session.headers.update({
    'Authorization': f'MediaBrowser Token="{settings.JELLYFIN_TOKEN}"',
    'Accept': 'application/json',
    'Content-Type': 'application/json',
})


def build_jellyfin_url(endpoint: str) -> str:
    """
    Build a complete Jellyfin API URL from an endpoint path.

    Args:
        endpoint (str): API endpoint (e.g., 'Library/Media/Updated').

    Returns:
        str: Full URL to query.
    """
    base = settings.JELLYFIN_URL.rstrip('/') if settings.JELLYFIN_URL else ""
    clean = endpoint.lstrip('/')
    url = f"{base}/{clean}"
    logger.debug(f"Built Jellyfin URL: {url}", extra={'emoji_type': 'debug'})
    return url


def safe_get_json_items(url: str, timeout: int = 5) -> List[dict]:
    """
    Safely get JSON items from Jellyfin API, handling errors gracefully.
    
    Args:
        url: The API URL to query
        timeout: Request timeout in seconds
        
    Returns:
        List of items from the 'Items' field, or empty list on error
    """
    try:
        response = session.get(url, timeout=timeout)
        if response.status_code == 200:
            data = response.json()
            return data.get('Items', []) if isinstance(data, dict) else []
        else:
            logger.warning(f"Request returned status {response.status_code} for {url}", extra={'emoji_type': 'warning'})
            return []
    except requests.exceptions.JSONDecodeError as e:
        logger.error(f"JSON decode error for {url}: {e}", extra={'emoji_type': 'error'})
        return []
    except Exception as e:
        logger.error(f"Request error for {url}: {e}", extra={'emoji_type': 'error'})
        return []


def refresh_jellyfin_item(path: Union[str, List[str]], update_type: str = 'Created') -> bool:
    """
    Trigger scan/update for a file, directory—or multiple paths.

    Args:
        path (str or list): Absolute filesystem path(s) to file or folder.
        update_type (str): One of 'Created', 'Changed', 'Deleted', or 'None'.

    Returns:
        bool: True if Jellyfin accepted request (HTTP 204), False otherwise.
    """
    if not settings.jellyfin_enabled or not settings.JELLYFIN_URL:
        # If disabled, we probably shouldn't fail hard if it was just a stray call?
        # But for RCA, if we called it, we expected it to work.
        msg = "Jellyfin is not enabled or URL not configured"
        logger.warning(msg, extra={'emoji_type': 'warning'})
        raise Exception(msg)

    # Normalize input to list of targets
    if isinstance(path, str):
        paths = [path]
    else:
        paths = list(path)

    logger.debug(f"Starting refresh_jellyfin_item for {len(paths)} path(s)", extra={'emoji_type': 'debug'})

    updates = []
    for p in paths:
        target = os.path.dirname(p) if os.path.isfile(p) else p
        updates.append({"Path": target, "UpdateType": update_type})

    payload = {"Updates": updates}
    url = build_jellyfin_url('Library/Media/Updated')
    try:
        resp = session.post(url, json=payload)
        if resp.status_code == 204:
            for u in updates:
                logger.info(f"Triggered scan for: {u['Path']} ({u['UpdateType']})", extra={'emoji_type': 'refresh'})
            return True
        logger.error(f"Scan failed ({resp.status_code}): {resp.text}", extra={'emoji_type': 'error'})
    except Exception as ex:
        logger.error(f"Error during scan request: {ex}", extra={'emoji_type': 'error'})
        raise ex
    return False

def refresh_jellyfin_library(library_id: str,
                              recursive: bool = True,
                              image_mode: str = 'Default',
                              metadata_mode: str = 'Default',
                              replace_images: bool = False,
                              regenerate_trickplay: bool = False,
                              replace_metadata: bool = False) -> bool:
    """
    Refresh an entire Jellyfin library (container) by its Item ID.
    """
    params = {
        'Recursive': str(recursive).lower(),
        'ImageRefreshMode': image_mode,
        'MetadataRefreshMode': metadata_mode,
        'ReplaceAllImages': str(replace_images).lower(),
        'RegenerateTrickplay': str(regenerate_trickplay).lower(),
        'ReplaceAllMetadata': str(replace_metadata).lower()
    }
    query = '&'.join(f"{quote(k)}={quote(v)}" for k, v in params.items())
    url = build_jellyfin_url(f"Items/{library_id}/Refresh?{query}")
    try:
        resp = session.post(url)
        if resp.status_code == 204:
            logger.info(f"Library {library_id} refresh queued", extra={'emoji_type': 'refresh'})
            return True
        logger.error(f"Library refresh failed ({resp.status_code}): {resp.text}", extra={'emoji_type': 'error'})
    except Exception as ex:
        logger.error(f"Error during library refresh: {ex}", extra={'emoji_type': 'error'})
        raise ex
    return False


def _prepend_status_to_summary(summary, status):
    """Prepend status to summary, replacing any previous status marker."""
    import re
    if not summary:
        summary = ""
    # Remove any existing status marker at the start
    summary = re.sub(r"^\[.*?\]\s*", "", summary)
    if status:
        return f"[{status}] {summary}".strip()
    else:
                return summary.strip()

def retry_call(
    func: Callable[[], Any],
    on_error: Callable[[Exception], None],
    retry_interval: int,
    retry_timeout: int,
    success_condition: Callable[[Any], bool]
) -> Any:
    """
    Calls `func()` until `success_condition(result)` is True or until `retry_timeout` elapses.
    - On HTTP connection/read errors, uses exponential backoff (interval doubles) and extends timeout by interval.
    - On other failures (success_condition False without exception), waits fixed retry_interval.
    """
    start = time.time()
    interval = retry_interval
    deadline = retry_timeout
    last_result = None
    attempt = 0
    
    logger.debug(f"Starting retry_call with timeout={retry_timeout}s, interval={retry_interval}s", extra={'emoji_type': 'debug'})

    while time.time() - start < deadline:
        attempt += 1
        is_net_error = False
        try:
            result = func()
        except Exception as ex:
            on_error(ex)
            result = None
            # detect connection/read timeout
            if isinstance(ex, (requests.exceptions.ConnectionError,
                               requests.exceptions.ReadTimeout,
                               requests.exceptions.Timeout)):
                is_net_error = True

        last_result = result
        logger.debug(f"retry_call attempt {attempt}, result={result!r}", extra={'emoji_type': 'debug'})

        if success_condition(result):
            logger.debug(f"retry_call succeeded on attempt {attempt}", extra={'emoji_type': 'success'})
            return result

        if is_net_error:
            # exponential backoff for network errors
            logger.warning(f"Network error on attempt {attempt}, waiting {interval}s", extra={'emoji_type': 'warning'})
            time.sleep(interval)
            deadline += interval  # extend window
            interval *= 2
        else:
            # fixed interval for non-network failures
            logger.warning(f"Attempt {attempt} failed condition, waiting {retry_interval}s", extra={'emoji_type': 'warning'})
            success_outcome = success_condition(result)
            logger.debug(f"success_condition({result!r}) -> {success_outcome!r}", extra={'emoji_type': 'debug'})
            try:
                source = inspect.getsource(success_condition)
            except Exception:
                source = '<source unavailable>'
            logger.debug(f"success_condition source:\n{source}", extra={'emoji_type': 'debug'})

            time.sleep(retry_interval)

    logger.warning(
        f"retry_call timed out after {attempt} attempts "
        f"(total window={deadline}s), last_result={last_result!r}",
        extra={'emoji_type': 'timeout'}
    )
    return last_result


@use_handler_session
def create_jellyfin_nfo(dbsession: Session, ent_id: int, model: type, action: str, status: str = None) -> bool:
    """
    Create NFO file alongside placeholder for Jellyfin metadata.
    This function is called by the scheduler as a step in the flow.
    
    For episodes, it ensures the complete hierarchy (series -> season -> episode) 
    NFO files are created if they don't exist.

    Args:
        dbsession: SQLAlchemy session.
        ent_id: Entity ID (movie_id, series_id, season_id, or episode_id).
        model: Model type (Movie, Series, Season, or Episode).
        action: Action name from SubFlow.

    Returns:
        True if NFO creation succeeded, False otherwise.
    """
    if not settings.jellyfin_enabled:
        logger.info("Jellyfin is disabled, skipping NFO creation", extra={'emoji_type': 'skip'})
        return True

    logger.info(f"Creating NFO for {model.__name__} {ent_id}, action '{action}'", extra={'emoji_type': 'file'})
    
    try:
        entity = dbsession.query(model).get(ent_id)
        if not entity:
            msg = f"{model.__name__} {ent_id} not found"
            logger.error(msg, extra={'emoji_type': 'error'})
            raise Exception(msg)

        # Short-circuit for delete action if placeholder doesn't exist
        if 'delete' in action.lower() and hasattr(entity, 'placeholder_exists') and not entity.placeholder_exists:
            logger.info(f"Skipping NFO creation for {model.__name__} {ent_id} (delete action, no placeholder)", extra={'emoji_type': 'skip'})
            return True

        request_status = status if status else entity.placeholder_status  # Default status prefix
        
        if model == Movie:
            # Handle movie NFO creation
            placeholder_path = getattr(entity, 'dummypath', None)
            if not placeholder_path and 'delete' in action:
                logger.info(f"No placeholder path for Movie {ent_id} on delete action, skipping NFO creation", extra={'emoji_type': 'skip'})
                return True
            elif not placeholder_path:
                msg = f"No placeholder path found for Movie {ent_id}"
                logger.error(msg, extra={'emoji_type': 'error'})
                raise Exception(msg)

            nfo_content = create_movie_nfo(entity, request_status)
            if not nfo_content:
                msg = f"Failed to generate movie NFO content for {ent_id}"
                logger.error(msg, extra={'emoji_type': 'error'})
                raise Exception(msg)
            
            nfo_path = get_nfo_path(placeholder_path)
            if not nfo_path:
                msg = f"Could not determine NFO path for {placeholder_path}"
                logger.error(msg, extra={'emoji_type': 'error'})
                raise Exception(msg)
            
            success = write_nfo_file(nfo_content, nfo_path)
            if success:
                entity.nfo_path = nfo_path
                dbsession.add(entity)
                safe_commit(dbsession, entity)
                logger.info(f"Successfully created movie NFO: {nfo_path}", extra={'emoji_type': 'success'})
            
            return success
            
        elif model == Episode:
            # Handle episode NFO creation - create complete hierarchy
            episode = entity
            
            # Get season and series
            season = dbsession.query(Season).get(episode.season_id)
            if not season:
                msg = f"Season {episode.season_id} not found for episode {ent_id}"
                logger.error(msg, extra={'emoji_type': 'error'})
                raise Exception(msg)
                
            series = dbsession.query(Series).get(season.series_id)
            if not series:
                msg = f"Series {season.series_id} not found for season {episode.season_id}"
                logger.error(msg, extra={'emoji_type': 'error'})
                raise Exception(msg)
            
            logger.info(f"Creating NFO hierarchy for episode {episode.episode_number} of '{series.title}' S{season.season_number}", extra={'emoji_type': 'tv'})
            
            # Ensure we have the dummy paths
            episode_placeholder_path = getattr(episode, 'dummypath', None)
            if not episode_placeholder_path and 'delete' in action:
                logger.info(f"No placeholder path found for Episode {ent_id} on delete action, skipping NFO creation", extra={'emoji_type': 'skip'})
                return True
            elif not episode_placeholder_path:
                msg = f"No placeholder path found for Episode {ent_id}"
                logger.error(msg, extra={'emoji_type': 'error'})
                raise Exception(msg)
            
            # Create series NFO if it doesn't exist
            if not series.nfo_path or not os.path.exists(series.nfo_path):
                logger.info(f"Creating series NFO for '{series.title}'", extra={'emoji_type': 'file'})
                series_nfo_content = create_series_nfo(series, request_status)
                if series_nfo_content:
                    # Determine series folder from episode path
                    series_folder = os.path.dirname(os.path.dirname(episode_placeholder_path))  # Go up 2 levels from episode
                    series_nfo_path = os.path.join(series_folder, "tvshow.nfo")
                    
                    if write_nfo_file(series_nfo_content, series_nfo_path):
                        series.nfo_path = series_nfo_path
                        dbsession.add(series)
                        safe_commit(dbsession, series)
                        logger.info(f"Created series NFO: {series_nfo_path}", extra={'emoji_type': 'success'})
                    else:
                        logger.error(f"Failed to write series NFO to {series_nfo_path}", extra={'emoji_type': 'error'})
                else:
                    logger.error(f"Failed to generate series NFO content", extra={'emoji_type': 'error'})
            
            # Create season NFO if it doesn't exist
            if not season.nfo_path or not os.path.exists(season.nfo_path):
                logger.info(f"Creating season NFO for S{season.season_number}", extra={'emoji_type': 'file'})
                season_nfo_content = create_season_nfo(season, request_status)
                if season_nfo_content:
                    # Determine season folder from episode path
                    season_folder = os.path.dirname(episode_placeholder_path)  # Go up 1 level from episode
                    season_nfo_path = os.path.join(season_folder, "season.nfo")
                    
                    if write_nfo_file(season_nfo_content, season_nfo_path):
                        season.nfo_path = season_nfo_path
                        dbsession.add(season)
                        safe_commit(dbsession, season)
                        logger.info(f"Created season NFO: {season_nfo_path}", extra={'emoji_type': 'success'})
                    else:
                        logger.error(f"Failed to write season NFO to {season_nfo_path}", extra={'emoji_type': 'error'})
                else:
                    logger.error(f"Failed to generate season NFO content", extra={'emoji_type': 'error'})
            
            # Create episode NFO
            logger.info(f"Creating episode NFO for S{season.season_number}E{episode.episode_number}", extra={'emoji_type': 'file'})
            episode_nfo_content = create_episode_nfo(episode, request_status)
            if not episode_nfo_content:
                msg = f"Failed to generate episode NFO content"
                logger.error(msg, extra={'emoji_type': 'error'})
                raise Exception(msg)
            
            episode_nfo_path = get_nfo_path(episode_placeholder_path)
            if not episode_nfo_path:
                msg = f"Could not determine episode NFO path for {episode_placeholder_path}"
                logger.error(msg, extra={'emoji_type': 'error'})
                raise Exception(msg)
            
            success = write_nfo_file(episode_nfo_content, episode_nfo_path)
            if success:
                episode.nfo_path = episode_nfo_path
                dbsession.add(episode)
                safe_commit(dbsession, episode)
                logger.info(f"Successfully created episode NFO: {episode_nfo_path}", extra={'emoji_type': 'success'})
            
            return success
            
        elif model == Series:
            # Handle series NFO creation
            placeholder_path = getattr(entity, 'dummypath', None)
            if not placeholder_path and 'delete' in action:
                logger.info(f"No placeholder path for Series {ent_id} on delete action, skipping NFO creation", extra={'emoji_type': 'skip'})
                return True
            elif not placeholder_path:
                msg = f"No placeholder path found for Series {ent_id}"
                logger.error(msg, extra={'emoji_type': 'error'})
                raise Exception(msg)
            
            nfo_content = create_series_nfo(entity, request_status)
            if not nfo_content:
                msg = f"Failed to generate series NFO content for {ent_id}"
                logger.error(msg, extra={'emoji_type': 'error'})
                raise Exception(msg)
            
            # Series NFO goes in the series folder as tvshow.nfo
            series_folder = os.path.dirname(placeholder_path) if os.path.isfile(placeholder_path) else placeholder_path
            nfo_path = os.path.join(series_folder, "tvshow.nfo")
            
            success = write_nfo_file(nfo_content, nfo_path)
            if success:
                entity.nfo_path = nfo_path
                dbsession.add(entity)
                safe_commit(dbsession, entity)
                logger.info(f"Successfully created series NFO: {nfo_path}", extra={'emoji_type': 'success'})
            
            return success
            
        elif model == Season:
            # Handle season NFO creation
            season = entity
            placeholder_path = getattr(season, 'dummypath', None)
            if not placeholder_path and 'delete' in action:
                logger.info(f"No placeholder path for Season {ent_id} on delete action, skipping NFO creation", extra={'emoji_type': 'skip'})
                return True
            elif not placeholder_path:
                msg = f"No placeholder path found for Season {ent_id}"
                logger.error(msg, extra={'emoji_type': 'error'})
                raise Exception(msg)
            
            nfo_content = create_season_nfo(season, request_status)
            if not nfo_content:
                msg = f"Failed to generate season NFO content for {ent_id}"
                logger.error(msg, extra={'emoji_type': 'error'})
                raise Exception(msg)
            
            # Season NFO goes in the season folder as season.nfo
            season_folder = os.path.dirname(placeholder_path) if os.path.isfile(placeholder_path) else placeholder_path
            nfo_path = os.path.join(season_folder, "season.nfo")
            
            success = write_nfo_file(nfo_content, nfo_path)
            if success:
                entity.nfo_path = nfo_path
                dbsession.add(entity)
                safe_commit(dbsession, entity)
                logger.info(f"Successfully created season NFO: {nfo_path}", extra={'emoji_type': 'success'})
            
            return success
            
        else:
            msg = f"Unsupported model type: {model.__name__}"
            logger.error(msg, extra={'emoji_type': 'error'})
            raise Exception(msg)
        
    except Exception as e:
        logger.error(f"Failed to create NFO for {model.__name__} {ent_id}: {e}", extra={'emoji_type': 'error'})
        dbsession.rollback()
        raise e


def refresh_jellyfin_dummy(dbsession: Session, ent_id: int, model: type, action: str) -> bool:
    """
    Refresh Jellyfin with dummy file for a specific entity.
    This function is called by the scheduler for individual subflows.

    Args:
        dbsession: SQLAlchemy session.
        ent_id: Entity ID (movie_id or episode_id).
        model: Model type (Movie or Episode).
        action: Action name from SubFlow.

    Returns:
        True if refresh succeeded, False otherwise.
    """
    logger.info(f"Starting refresh_jellyfin_dummy for {model.__name__} ID {ent_id}, action '{action}'", extra={'emoji_type': 'processing'})

    # Get the entity (Movie or Episode) using the provided model and ID
    try:
        entity = dbsession.query(model).get(ent_id)
        if not entity:
            msg = f"{model.__name__} with ID {ent_id} not found"
            logger.error(msg, extra={'emoji_type': 'error'})
            raise Exception(msg)
    except Exception as e:
        logger.error(f"Failed to query {model.__name__} with ID {ent_id}: {e}", extra={'emoji_type': 'error'})
        raise e

    # Short-circuit for delete action if placeholder doesn't exist
    if 'delete' in action.lower() and hasattr(entity, 'placeholder_exists') and not entity.placeholder_exists:
        logger.info(f"Skipping dummy refresh for {model.__name__} {ent_id} (delete action, no placeholder)", extra={'emoji_type': 'skip'})
        return True

    # Get the dummy path
    dummy_path = getattr(entity, 'dummypath', None)
    
    if not dummy_path:
        msg = f"No dummy path found for {model.__name__} ID {ent_id}"
        logger.warning(msg, extra={'emoji_type': 'warning'})
        raise Exception(msg)

    logger.info(f"Found dummy path: {dummy_path}", extra={'emoji_type': 'dummy'})

    # Check if file physically exists
    import os
    file_exists = os.path.exists(dummy_path)
    
    # For delete actions, always trigger scan to clean up Jellyfin database
    if 'delete' in action.lower():
        if not file_exists:
            logger.info(f"Placeholder file {dummy_path} already deleted - triggering cleanup scan", extra={'emoji_type': 'scan'})
        else:
            logger.info(f"Placeholder file {dummy_path} still exists - triggering deletion scan", extra={'emoji_type': 'scan'})
        
        # Update d_exists status
        if hasattr(entity, 'placeholder_exists'):
            entity.placeholder_exists = False
            dbsession.add(entity)
            safe_commit(dbsession, entity)
        # Continue to trigger scan regardless of file existence
    # else:
    #     # For non-delete actions, create dummy file if missing
    #     if not file_exists:
    #         logger.warning(f"Dummy file missing, attempting to create: {dummy_path}", extra={'emoji_type': 'warning'})
    #         try:
    #             # Import here to avoid circular imports
    #             from services.integrations import place_dummy_file
    #             from services.postgres.models import Movie, Episode, Season, Series
                
    #             created_path = None
    #             if model == Movie:
    #                 movie = entity
    #                 # Determine library based on 4K status
    #                 library_path = settings.DUMMY_MOVIE_LIBRARY_FOLDER_4K if getattr(movie, 'is_4k', False) else settings.DUMMY_MOVIE_LIBRARY_FOLDER
    #                 created_path = place_dummy_file(
    #                     "movie", 
    #                     movie.title, 
    #                     movie.year, 
    #                     getattr(movie, 'tmdbid', None),
    #                     library_path
    #                 )
    #             elif model == Episode:
    #                 episode = entity
    #                 season = dbsession.query(Season).get(episode.season_id)
    #                 series = dbsession.query(Series).get(season.series_id)
    #                 # Determine library based on 4K status
    #                 library_path = settings.TV_LIBRARY_FOLDER_4K if getattr(series, 'is_4k', False) else settings.DUMMY_TV_LIBRARY_FOLDER
    #                 created_path = place_dummy_file(
    #                     "tv",
    #                     series.title,
    #                     series.year,
    #                     getattr(series, 'tvdbid', None),
    #                     library_path,
    #                     season_number=season.season_number,
    #                     episode_range=(episode.episode_number, episode.episode_number),
    #                     episode_title=episode.title
    #                 )
                
    #             if created_path:
    #                 logger.info(f"Successfully created missing dummy file: {created_path}", extra={'emoji_type': 'success'})
    #                 # Update the entity's dummypath if needed
    #                 if entity.dummypath != created_path:
    #                     entity.dummypath = created_path
    #                     dbsession.add(entity)
    #                     dbsession.commit()
    #                 file_exists = True
    #             else:
    #                 logger.error(f"Failed to create dummy file for {model.__name__} ID {ent_id}", extra={'emoji_type': 'error'})
                    
    #         except Exception as e:
    #             logger.error(f"Error creating dummy file: {e}", extra={'emoji_type': 'error'})
        
    #     # Update placeholder_exists status
    #     if hasattr(entity, 'placeholder_exists'):
    #         entity.placeholder_exists = file_exists
    #         dbsession.add(entity)
    #         dbsession.commit()

    # Determine update type from action
    action_lower = action.lower()
    if 'add' in action_lower:
        update_type = 'Created'
    elif 'delete' in action_lower:
        update_type = 'Deleted'
    elif 'import' in action_lower:
        update_type = 'Deleted'
    else:
        update_type = 'Updated'

    logger.info(f"Determined update type as '{update_type}' based on action '{action}'", extra={'emoji_type': 'update'})

    # Prepare paths to refresh (dummy file + NFO file if it exists)
    paths_to_refresh = [dummy_path]
    
    # Add NFO path if it exists
    nfo_path = getattr(entity, 'nfo_path', None)
    if nfo_path and os.path.exists(nfo_path):
        paths_to_refresh.append(nfo_path)
        logger.info(f"Including NFO file in refresh: {nfo_path}", extra={'emoji_type': 'file'})

    # Perform the refresh
    try:
        refresh_jellyfin_item(paths_to_refresh, update_type)
        path_list = ', '.join(paths_to_refresh)
        logger.info(f"Successfully called refresh_jellyfin_item with paths '{path_list}' and update type '{update_type}'", extra={'emoji_type': 'success'})
        
        # For delete actions, check if parent directories are empty and trigger additional scans
        if 'delete' in action_lower or 'import' in action_lower:
            try:
                from services.postgres.models import Episode, Season, Series
                
                if model == Episode:
                    # Check if season/series folders are empty after episode deletion
                    episode = entity
                    season = dbsession.query(Season).get(episode.season_id)
                    series = dbsession.query(Series).get(season.series_id)
                    
                    if episode.dummypath:
                        season_dir = os.path.dirname(episode.dummypath)
                        series_dir = os.path.dirname(season_dir)
                        
                        # Check if season folder is empty
                        if os.path.exists(season_dir):
                            try:
                                season_contents = os.listdir(season_dir)
                                if not season_contents:
                                    logger.info(f"🔍 Season folder is empty after episode deletion, triggering season cleanup: {season_dir}", extra={'emoji_type': 'scan'})
                                    refresh_jellyfin_item([season_dir], 'Deleted')
                                    
                                    # Check if series folder is empty
                                    if os.path.exists(series_dir):
                                        try:
                                            series_contents = os.listdir(series_dir)
                                            if not series_contents:
                                                logger.info(f"🔍 Series folder is empty after season cleanup, triggering series cleanup: {series_dir}", extra={'emoji_type': 'scan'})
                                                parent_dir = os.path.dirname(series_dir)
                                                refresh_jellyfin_item([parent_dir], 'Deleted')
                                        except Exception as e:
                                            logger.debug(f"Could not check series directory {series_dir}: {e}", extra={'emoji_type': 'debug'})
                            except Exception as e:
                                logger.debug(f"Could not check season directory {season_dir}: {e}", extra={'emoji_type': 'debug'})
                                
                elif model == Movie:
                    # Check if movie folder is empty after movie deletion
                    movie = entity
                    if movie.dummypath:
                        movie_dir = os.path.dirname(movie.dummypath) if os.path.isfile(movie.dummypath) else movie.dummypath
                        if os.path.exists(movie_dir):
                            try:
                                movie_contents = os.listdir(movie_dir)
                                if not movie_contents:
                                    logger.info(f"🔍 Movie folder is empty after deletion, triggering cleanup: {movie_dir}", extra={'emoji_type': 'scan'})
                                    parent_dir = os.path.dirname(movie_dir)
                                    refresh_jellyfin_item([parent_dir], 'Deleted')
                            except Exception as e:
                                logger.debug(f"Could not check movie directory {movie_dir}: {e}", extra={'emoji_type': 'debug'})
                                
            except Exception as e:
                logger.debug(f"Error during empty folder scan check: {e}", extra={'emoji_type': 'debug'})
        
        return True
    except Exception as e:
        logger.error(f"refresh_jellyfin_item failed: {e}", extra={'emoji_type': 'error'})
        return False


def refresh_jellyfin_dummy_bulk(dbsession: Session, ent_id: int, model: type, action: str) -> bool:
    """
    Bulk-refresh all pending 'refresh_jellyfin_dummy' subflows in one call.
    This is the original bulk function, renamed to avoid confusion.

    Args:
        dbsession: SQLAlchemy session.
        ent_id: Entity ID provided by scheduler (ignored here).
        model: Model type provided by scheduler (ignored here).
        action: Action name provided by scheduler (ignored here).

    Returns:
        True if any refresh occurred, False otherwise.
    """
    logger.info("Starting bulk refresh for 'refresh_jellyfin_dummy' subflows", extra={'emoji_type': 'processing'})

    # 1) Query all QUEUED subflows whose current step is this function
    try:
        pending: list[SubFlow] = (
            dbsession.query(SubFlow)
            .filter(SubFlow.status == 'QUEUED')
            .all()
        )
        logger.debug(f"Found {len(pending)} QUEUED subflows", extra={'emoji_type': 'debug'})
    except Exception as e:
        logger.error(f"Failed to query SubFlows: {e}", extra={'emoji_type': 'error'})
        return False

    # Filter to those at this step index
    matching = []
    for sf in pending:
        steps = sf.steps.split(',')
        if sf.step_index < len(steps) and steps[sf.step_index] == 'refresh_jellyfin_dummy':
            matching.append(sf)

    logger.info(f"{len(matching)} subflows match the 'refresh_jellyfin_dummy' step", extra={'emoji_type': 'info'})

    if not matching:
        logger.info("No matching subflows to process. Exiting.", extra={'emoji_type': 'info'})
        return False

    # 2) Collect unique dummy paths
    paths = set()
    for sf in matching:
        obj = None
        if sf.episode_id:
            obj = dbsession.query(Episode).get(sf.episode_id)
        else:
            obj = dbsession.query(Movie).get(sf.movie_id)

        if obj and obj.dummypath:
            paths.add(obj.dummypath)

    logger.info(f"Collected {len(paths)} unique dummy paths to refresh", extra={'emoji_type': 'dummy'})

    if not paths:
        # Check if this is a delete action - if so, no dummy files is expected and OK
        sf_action = matching[0].action.lower()
        if 'delete' in sf_action:
            logger.info("No dummy paths found for delete action - this is expected if files were already deleted", extra={'emoji_type': 'info'})
            # Return success for delete actions with no paths - let scheduler handle advancement
            logger.info(f"Successfully processed {len(matching)} delete action subflows (no dummy files to refresh)", extra={'emoji_type': 'success'})
            return True
        else:
            logger.warning("No dummy paths found to refresh. This should be retried.", extra={'emoji_type': 'warning'})
            return False

    # 3) Determine update type from SubFlow action (not parent entity action)
    sf_action = matching[0].action.lower()

    if 'add' in sf_action:
        update_type = 'Created'
    elif any(k in sf_action for k in ('delete',)):
        update_type = 'Deleted'
    elif 'import' in sf_action:
        update_type = 'Created'  # Import events should create placeholders
    else:
        update_type = 'Updated'

    logger.info(f"Determined update type as '{update_type}' based on SubFlow action '{matching[0].action}'", extra={'emoji_type': 'update'})

    # 4) Perform bulk refresh
    try:
        refresh_jellyfin_item(list(paths), update_type)
        logger.info(f"Called refresh_jellyfin_item with {len(paths)} paths and update type '{update_type}'", extra={'emoji_type': 'update'})
    except Exception as e:
        logger.error(f"refresh_jellyfin_item failed: {e}", extra={'emoji_type': 'error'})
        return False

    # 5) Manually advance all processed SubFlows to next step (for bulk processing efficiency)
    from services.flow_manager import flow_manager
    
    advanced_count = 0
    for sf in matching:
        # Get the steps for this SubFlow
        steps = sf.steps.split(',') if sf.steps else []
        
        # Check if there are more steps after current one
        if sf.step_index + 1 < len(steps):
            # Advance to next step
            sf.step_index += 1
            next_step = steps[sf.step_index]
            
            # Set to PENDING so scheduler picks it up in next poll (not QUEUED which means "currently processing")
            sf.status = 'PENDING'
            dbsession.add(sf)
            advanced_count += 1
            
            logger.debug(f"Advanced SubFlow {sf.id} to step {sf.step_index}: {next_step}", extra={'emoji_type': 'step'})
        else:
            # This was the last step, mark as DONE
            sf.status = 'DONE'
            dbsession.add(sf)
            logger.debug(f"SubFlow {sf.id} completed all steps", extra={'emoji_type': 'success'})
    
    # Commit all the SubFlow updates
    dbsession.flush()
    
    logger.info(f"Bulk processed {len(matching)} subflows: {advanced_count} advanced to next step", extra={'emoji_type': 'success'})
    
    # Return success - let the scheduler handle SubFlow advancement
    # DO NOT mark SubFlows as DONE here - that's the scheduler's job
    logger.info(f"Successfully refreshed {len(matching)} subflows for 'refresh_jellyfin_dummy' step", extra={'emoji_type': 'success'})
    return True

def reset_jellyfin_id(dbsession: Session, ent_id: int, model: type, action:str) -> bool:
    """
    Reset the Jellyfin ID for the given entity.

    Args:
        dbsession: SQLAlchemy session.
        ent_id: Entity ID (movie_id, series_id, season_id, or episode_id).
        model: Model type (Movie, Series, Season, or Episode).
        action: Action name from SubFlow.
    Returns:
        True if reset succeeded, False otherwise.
    """
    logger.info(f"Resetting Jellyfin ID for {model.__name__} ID {ent_id}, action '{action}'", extra={'emoji_type': 'reset'})
    try:
        entity = dbsession.query(model).get(ent_id)
        if not entity:
            msg = f"{model.__name__} {ent_id} not found"
            logger.error(msg, extra={'emoji_type': 'error'})
            raise Exception(msg)

        # If delete action and placeholder is False, ONLY reset dummy ID
        if 'delete' in action.lower() and hasattr(entity, 'placeholder_exists') and not entity.placeholder_exists:
            logger.info(f"Partially resetting Jellyfin ID for {model.__name__} {ent_id} (delete action, no placeholder) - keeping real ID", extra={'emoji_type': 'reset'})
            entity.jellyfin_dummy_id = None
            entity.sonarr_progress = None
            entity.sonarr_status = None 
            # Do NOT reset jellyfin_id, filepath, or sonarr status
        else:
            # Full reset
            entity.jellyfin_id = None
            entity.jellyfin_dummy_id = None
            entity.filepath = None
            entity.sonarr_progress = None
            entity.sonarr_status = None
            
            # For episodes, also clear the series filepath
            if model == Episode:
                entity.season.series.filepath = None
                dbsession.add(entity.season.series)
        
        dbsession.add(entity)
        safe_commit(dbsession, entity)
        logger.info(f"Successfully processed Jellyfin ID reset for {model.__name__} {ent_id}", extra={'emoji_type': 'success'})
        return True
    except Exception as e:
        logger.error(f"Failed to reset Jellyfin ID for {model.__name__} ID {ent_id}: {e}", extra={'emoji_type': 'error'})
        dbsession.rollback()
        raise e

def refresh_jellyfin_arr_path(dbsession: Session, ent_id: int, model: type, action:str) -> bool:
    """
    Bulk-refresh all pending 'refresh_jellyfin_file' subflows in one call,
    using step_index and steps to find exact matches.

    Args:
        session: SQLAlchemy session.
        ent_id, model: provided by scheduler (ignored here for bulk logic).
    Returns:
        True if any refresh occurred, False otherwise.
    """
    logger.info("Starting bulk refresh for 'refresh_jellyfin_arr_path' subflows", extra={'emoji_type': 'processing'})
    
    # 1) Query all QUEUED subflows whose current step is this function
    pending: list[SubFlow] = (
        dbsession.query(SubFlow)
        .filter(
            SubFlow.status == 'QUEUED'
        )
        .all()
    )
    # Filter to those at this step index
    matching = []
    for sf in pending:
        steps = sf.steps.split(',')
        if sf.step_index < len(steps) and steps[sf.step_index] == 'refresh_jellyfin_arr_path':
            matching.append(sf)

    logger.info(f"Found {len(matching)} subflows matching 'refresh_jellyfin_arr_path' step", extra={'emoji_type': 'info'})
    
    if not matching:
        logger.debug("No matching subflows found for arr path refresh", extra={'emoji_type': 'debug'})
        return True

    # 2) Collect unique paths for all matching subflows
    paths = set()
    for sf in matching:
        if sf.episode_id:
            obj = dbsession.query(Episode).get(sf.episode_id)
        else:
            obj = dbsession.query(Movie).get(sf.movie_id)
        if obj and obj.filepath:
            paths.add(obj.filepath)

    logger.info(f"Collected {len(paths)} unique file paths to refresh", extra={'emoji_type': 'dummy'})
    
    if not paths:
        # Check if this is a delete action - if so, no files is expected and OK
        sf_action = matching[0].action.lower()
        if 'delete' in sf_action:
            logger.info("No file paths found for delete action - this is expected if files were already deleted", extra={'emoji_type': 'info'})
            # Return success for delete actions with no paths - let scheduler handle advancement
            logger.info(f"Successfully processed {len(matching)} delete action subflows (no file paths to refresh)", extra={'emoji_type': 'success'})
            return True
        else:
            msg = "No file paths found to refresh"
            logger.warning(msg, extra={'emoji_type': 'warning'})
            raise Exception(msg)

    # 3) Determine update type from SubFlow action (not parent entity action)
    sf_action = matching[0].action.lower()
    if any(k in sf_action for k in ('add',)):
        update_type = 'Created'
    elif any(k in sf_action for k in ('delete',)):
        update_type = 'Deleted'
    elif 'import' in sf_action:
        update_type = 'Created'  # Import events should create placeholders
    else:
        update_type = 'Updated'

    logger.info(f"Determined update type as '{update_type}' based on SubFlow action '{matching[0].action}'", extra={'emoji_type': 'update'})

    # 4) Bulk refresh call
    refresh_jellyfin_item(list(paths), update_type)

    # 5) Manually advance all processed SubFlows to next step (for bulk processing efficiency)
    advanced_count = 0
    for sf in matching:
        # Get the steps for this SubFlow
        steps = sf.steps.split(',') if sf.steps else []
        
        # Check if there are more steps after current one
        if sf.step_index + 1 < len(steps):
            # Advance to next step
            sf.step_index += 1
            next_step = steps[sf.step_index]
            
            # Keep as QUEUED so scheduler can pick up the next step
            sf.status = 'QUEUED'
            dbsession.add(sf)
            safe_commit(dbsession, sf)
            advanced_count += 1
            
            logger.debug(f"Advanced SubFlow {sf.id} to step {sf.step_index}: {next_step}", extra={'emoji_type': 'step'})
        else:
            # This was the last step, mark as DONE
            sf.status = 'DONE'
            dbsession.add(sf)
            safe_commit(dbsession, sf)
            logger.debug(f"SubFlow {sf.id} completed all steps", extra={'emoji_type': 'success'})
    
    # Commit all the SubFlow updates
    dbsession.flush()
    
    logger.info(f"Bulk processed {len(matching)} subflows: {advanced_count} advanced to next step", extra={'emoji_type': 'success'})

    # 5) Return success - let the scheduler handle SubFlow advancement  
    # DO NOT mark SubFlows as DONE here - that's the scheduler's job
    logger.info(f"Successfully refreshed {len(matching)} subflows for arr path refresh", extra={'emoji_type': 'success'})

    return True


def save_jellyfin_arr_id(dbsession, model, ent_id, match: dict):
    """
    Persist the Jellyfin item ID along with its matched title and overview.

    Args:
        dbsession: SQLAlchemy session
        model: one of (Movie, Series, Season, Episode)
        ent_id: primary key of the model instance
        jf_id: Jellyfin item ID
        match: dict from Jellyfin API containing at least 'Id', 'Name', and 'Overview'
    Returns:
        jf_id
    """
    obj = dbsession.query(model).get(ent_id)
    logger.debug(f"Saving Jellyfin arr ID for {model.__name__} {ent_id}", extra={'emoji_type': 'debug'})
    
    # ID field mapping
    id_field = {
        Movie:   'jellyfin_id',
        Series:  'jellyfin_id',
        Season:  'jellyfin_id',
        Episode: 'jellyfin_id'
    }[model]
    # Title & overview field mapping (ensure these columns exist)
    title_field = {
        Movie:   'jellyfin_title',
        Series:  'jellyfin_title',
        Season:  'jellyfin_title',
        Episode: 'jellyfin_title'
    }[model]
    overview_field = {
        Movie:   'jellyfin_overview',
        Series:  'jellyfin_overview',
        Season:  'jellyfin_overview',
        Episode: 'jellyfin_overview'
    }[model]
    
    # Check if this is a dummy path being saved to a real ID field
    jf_path = match.get('Path', '')
    if jf_path:
        # Normalize paths for comparison
        dummy_movie = (settings.DUMMY_MOVIE_LIBRARY_FOLDER or "").rstrip(os.sep)
        dummy_tv = (settings.DUMMY_TV_LIBRARY_FOLDER or "").rstrip(os.sep)
        
        # Check for dummy path contamination
        is_dummy = False
        if dummy_movie and jf_path.startswith(dummy_movie):
            is_dummy = True
        elif dummy_tv and jf_path.startswith(dummy_tv):
            is_dummy = True
            
        if is_dummy and id_field == 'jellyfin_id':
            msg = f"❌ BLOCKED: Attempted to save dummy path '{jf_path}' to real {model.__name__}.jellyfin_id. Skipping ID update."
            logger.warning(msg, extra={'emoji_type': 'warning'})
            # RAISE EXCEPTION so this error is captured in last_error_message
            raise Exception(msg)

    changed = False
    # Update ID
    jf_id = match.get('Id')
    if getattr(obj, id_field) != jf_id:
        setattr(obj, id_field, jf_id)
        changed = True
        logger.debug(f"Updated {model.__name__} {ent_id} arr ID: {jf_id}", extra={'emoji_type': 'update'})
    # Update title
    name = match.get('Name')
    if name and getattr(obj, title_field) != name:
        setattr(obj, title_field, name)
        changed = True
        logger.debug(f"Updated {model.__name__} {ent_id} title: {name}", extra={'emoji_type': 'update'})
    # Update overview
    overview = match.get('Overview')
    if overview and getattr(obj, overview_field) != overview:
        setattr(obj, overview_field, overview)
        changed = True
        logger.debug(f"Updated {model.__name__} {ent_id} overview", extra={'emoji_type': 'update'})

    if changed:
        dbsession.add(obj)
        safe_commit(dbsession, obj)
        logger.info(f"Saved arr data for {model.__name__} {ent_id}", extra={'emoji_type': 'success'})
    else:
        logger.debug(f"No changes needed for {model.__name__} {ent_id} arr data", extra={'emoji_type': 'debug'})
    return jf_id

def save_jellyfin_dummy_id(dbsession, model, ent_id, match: dict):
    """
    Persist the Jellyfin item ID along with its matched title and overview.

    Args:
        dbsession: SQLAlchemy session
        model: one of (Movie, Series, Season, Episode)
        ent_id: primary key of the model instance
        jf_id: Jellyfin item ID
        match: dict from Jellyfin API containing at least 'Id', 'Name', and 'Overview'
    Returns:
        jf_id
    """
    obj = dbsession.query(model).get(ent_id)
    logger.debug(f"Saving Jellyfin dummy ID for {model.__name__} {ent_id}", extra={'emoji_type': 'debug'})
    
    # ID field mapping
    id_field = {
        Movie:   'jellyfin_dummy_id',
        Series:  'jellyfin_dummy_id',
        Season:  'jellyfin_dummy_id',
        Episode: 'jellyfin_dummy_id'
    }[model]
    # Title & overview field mapping (ensure these columns exist)
    title_field = {
        Movie:   'jellyfin_title',
        Series:  'jellyfin_title',
        Season:  'jellyfin_title',
        Episode: 'jellyfin_title'
    }[model]
    overview_field = {
        Movie:   'jellyfin_overview',
        Series:  'jellyfin_overview',
        Season:  'jellyfin_overview',
        Episode: 'jellyfin_overview'
    }[model]
    
    changed = False
    # Update ID
    jf_id = match.get('Id')
    if getattr(obj, id_field) != jf_id:
        setattr(obj, id_field, jf_id)
        changed = True
        logger.debug(f"Updated {model.__name__} {ent_id} dummy ID: {jf_id}", extra={'emoji_type': 'dummy'})
    # Update title
    name = match.get('Name')
    if name and getattr(obj, title_field) != name:
        setattr(obj, title_field, name)
        changed = True
        logger.debug(f"Updated {model.__name__} {ent_id} title: {name}", extra={'emoji_type': 'dummy'})
    # Update overview
    overview = match.get('Overview')
    if overview and getattr(obj, overview_field) != overview:
        setattr(obj, overview_field, overview)
        changed = True
        logger.debug(f"Updated {model.__name__} {ent_id} overview", extra={'emoji_type': 'dummy'})

    if changed:
        dbsession.add(obj)
        safe_commit(dbsession, obj)
        logger.info(f"Saved dummy data for {model.__name__} {ent_id}", extra={'emoji_type': 'success'})
    else:
        logger.debug(f"No changes needed for {model.__name__} {ent_id} dummy data", extra={'emoji_type': 'debug'})
    return jf_id


def verify_arr_scan_jellyfin(dbsession: Session, ent_id: int, model: Type, action) -> bool:
    """
    Process a "verify_dummy_scan_jellyfin" step for one entity:

    Movie:
      - Search Jellyfin by title, match playback path to dummypath
      - Save Jellyfin movie ID, mark its SubFlow DONE

    Episode:
      - Determine series from episode, dedupe by folder name
      - Cache series and season IDs
      - Fetch all queued Episode SubFlows for that series
      - For each, match dummypath to playback path, save episode ID, mark SubFlow DONE

    Returns True if Episode and any SubFlow was updated; False otherwise.
    """
    logger.info(f"Starting verify_arr_scan_jellyfin for {model.__name__} {ent_id}", extra={'emoji_type': 'processing'})
    import os
    
    # MOVIE CASE
    user_id = get_admin_user()
    if model is Movie:
        m = dbsession.query(Movie).get(ent_id)
        if not m:
            logger.warning(f"Movie {ent_id} not found", extra={'emoji_type': 'warning'})
            return False
        
        # For delete actions, check if arr file actually exists (filepath might be None for deleted movies)
        if 'delete' in action.lower():
            if not m.filepath or not os.path.exists(m.filepath):
                logger.info(f"Arr file {'(no filepath)' if not m.filepath else m.filepath} already deleted - verification complete", extra={'emoji_type': 'success'})
                # Update placeholder_exists status
                if hasattr(m, 'placeholder_exists'):
                    m.placeholder_exists = False
                    dbsession.add(m)
                    safe_commit(dbsession, m)
                return True
        
        # For non-delete actions, filepath is required
        if not m.filepath:
            logger.warning(f"Movie {ent_id} missing filepath", extra={'emoji_type': 'warning'})
            return False # gotta add step to enrich filepath then retry
        
        if m.jellyfin_id:
            logger.debug(f"Movie {ent_id} already has jellyfin_id: {m.jellyfin_id}", extra={'emoji_type': 'debug'})
            it = None
            try:
                movie_url = build_jellyfin_url(f"Items?{m.jellyfin_id}/?userId={user_id}&includeItemTypes=Movie&fields=ProviderIds,Name,ProductionYear,Overview,Path")
                def success_movie(res):
                    if res.status_code in (200, 204, 404):
                        if res.status_code == 200:
                            res = res.json()
                            return (
                                res and isinstance(res, dict) and
                                res.get('Items') and isinstance(res['Items'], list) and
                                len(res['Items']) > 0
                            )
                        return True
                    return False
                response = retry_call(
                    func=lambda: session.get(movie_url, timeout=5),
                    on_error=lambda ex: logger.error(f"Movie search error: {ex}"),
                    retry_interval=3, retry_timeout=9,  # 3 attempts max
                    success_condition=lambda res: success_movie(res)
                )
                items = response.json().get('Items', []) if response and response.status_code == 200 else []
                for candidate in items:
                    c_path = candidate.get('Path', '')
                    if (
                        int(candidate.get("ProviderIds", {}).get("Tmdb", -1)) == m.tmdbid and
                        c_path == m.filepath and
                        (not m.year or int(candidate.get("ProductionYear", -1)) == m.year)
                    ):
                        it = candidate
                        break
                    
                    # Check if the existing ID points to a dummy path (Poisoned ID)
                    dummy_movie = (settings.DUMMY_MOVIE_LIBRARY_FOLDER or "").rstrip(os.sep)
                    if c_path and dummy_movie and c_path.startswith(dummy_movie):
                        logger.warning(f"Detected poisoned Movie ID {m.jellyfin_id} pointing to dummy library. Resetting.", extra={'emoji_type': 'reset'})
                        m.jellyfin_id = None
                        dbsession.add(m)
                        safe_commit(dbsession, m)
                        # We don't break here, we continue looking for match or fall through to search
                        
            except Exception as ex:
                logger.error(f"Error fetching Jellyfin item by ID: {ex}", extra={'emoji_type': 'error'})
            if it:
                save_jellyfin_arr_id(dbsession, Movie, m.id, it)
                return True # If we matched existing ID, we are good
            elif not m.jellyfin_id:
                logger.info(f"Existing ID was invalid/poisoned, falling back to search", extra={'emoji_type': 'search'})
            return True
        clean_title = re.sub(r"\s*\(\d{4}\)$", "", m.title)
        url = build_jellyfin_url(
            f"Items?searchTerm={quote_plus(clean_title)}"
            "&includeItemTypes=Movie&recursive=true&fields=ProviderIds,Name,ProductionYear,Overview,Path"
        )
        def filter_movies(res):
            import os as os_module  # Import locally to avoid scope issues
            if res.status_code not in (200, 204, 404):
                return False
            if res.status_code == 404:
                return True
            response_items = res.json().get('Items', [])
            dummy_movie_base = (settings.DUMMY_MOVIE_LIBRARY_FOLDER or "").rstrip(os_module.sep)
            return [
                item for item in (response_items or [])
                if (
                    int(item.get("ProviderIds", {}).get("Tmdb", -1)) == m.tmdbid and 
                    item.get('Path','') == m.filepath and
                    (not dummy_movie_base or not item.get('Path', '').startswith(dummy_movie_base))
                )
                and (not m.year or int(item.get("ProductionYear", -1)) == m.year)
            ]
        response = retry_call(
            func=lambda: session.get(url, timeout=5),
            on_error=lambda ex: logger.error(f"Movie search error: {ex}"),
            retry_interval=3, retry_timeout=9,  # 3 attempts max
            success_condition=lambda res: res.status_code in (200, 204, 404)
        )
        items = filter_movies(response) if response else []
        for it in items:
            if it.get('ProviderIds', {}).get('Tmdb') != m.tmdbid and it.get('Path',{}) != m.filepath:
                continue
            save_jellyfin_arr_id(dbsession, Movie, m.id, it)
            return True
        return False

    # EPISODE CASE
    ep = dbsession.query(Episode).get(ent_id)
    if not ep:
        logger.warning(f"Episode {ent_id} not found", extra={'emoji_type': 'warning'})
        return False
    
    # For delete actions, check if arr file actually exists (filepath might be None for deleted episodes)
    import os
    if 'delete' in action.lower():
        if not ep.filepath or not os.path.exists(ep.filepath):
            logger.info(f"Arr file {'(no filepath)' if not ep.filepath else ep.filepath} already deleted - verification complete", extra={'emoji_type': 'success'})
            # Update placeholder_exists status
            if hasattr(ep, 'placeholder_exists'):
                ep.placeholder_exists = False
                dbsession.add(ep)
                safe_commit(dbsession, ep)
            return True
    
    # For non-delete actions, filepath is required
    if not ep.filepath:
        logger.warning(f"Episode {ent_id} missing filepath", extra={'emoji_type': 'warning'})
        return False
    
    seas = dbsession.query(Season).get(ep.season_id)
    series = dbsession.query(Series).get(seas.series_id)
    logger.info(f"Processing episode {ent_id} for series '{series.title}' S{ep.season_number}E{ep.episode_number}", extra={'emoji_type': 'tv'})

    existing = False
    # 1) Series ID with folder-based dedupe
    if series.jellyfin_id:
        jf_series_id = series.jellyfin_id
        existing = True
        logger.debug(f"Using existing series ID: {jf_series_id}", extra={'emoji_type': 'debug'})
    else:
        logger.info(f"Searching for series '{series.title}' in Jellyfin", extra={'emoji_type': 'search'})
        clean_title = re.sub(r"\s*\(\d{4}\)$", "", series.title)
        url = build_jellyfin_url(
            f"Items?searchTerm={quote_plus(clean_title)}"
            "&includeItemTypes=Series&recursive=true&fields=ProviderIds,Path"
        )
        
        # Use series.filepath from database as the folder to match
        series_folder = series.filepath if series.filepath else None
        if series_folder:
            logger.debug(f"Using series folder from database: {series_folder}", extra={'emoji_type': 'debug'})
        else:
            logger.debug(f"No series.filepath found, will match on title and TVDB ID only", extra={'emoji_type': 'debug'})
        
        def pick_series(items: List[dict]) -> List[dict]:
            import os as os_module  # Import locally to avoid scope issues
            matched = []
            dummy_tv_base = (settings.DUMMY_TV_LIBRARY_FOLDER or "").rstrip(os_module.sep)
            
            for s in (items or []):
                tvdb_match = (s.get("ProviderIds", {}).get("Tvdb") and 
                             str(s["ProviderIds"]["Tvdb"]) == str(series.tvdbid))
                if not tvdb_match:
                    continue
                    
                title_match = clean_title in s.get("Name", "")
                if not title_match:
                    continue
                
                # If we have a series_folder, also match on path
                s_path = s.get('Path', '')
                
                # CRITICAL: Skip if this series is in the dummy matching folder
                if dummy_tv_base and s_path and s_path.startswith(dummy_tv_base):
                    logger.debug(f"Skipping series '{s.get('Name')}' - resides in dummy path: {s_path}", extra={'emoji_type': 'skip'})
                    continue
                    
                if series_folder:
                    if s_path == series_folder:
                        matched.append(s)
                else:
                    # No folder to match, just use title + TVDB
                    matched.append(s)
            return matched
        raw_items = retry_call(
            func=lambda: safe_get_json_items(url),
            on_error=lambda ex: logger.error(f"Series search error: {ex}", extra={'emoji_type': 'error'}),
            retry_interval=3, retry_timeout=9,  # 3 attempts max
            success_condition=lambda res: bool(pick_series(res))
        ) or []
        items = pick_series(raw_items)
        
        logger.debug(f"Found {len(items)} series candidates from search", extra={'emoji_type': 'debug'})
        
        match = None
        if series_folder:
            match = next(
                (it for it in items
                if it.get('ProviderIds', {}).get('Tvdb') == str(series.tvdbid)
                and it.get('Path') == series_folder),
                None
            )
            if match:
                logger.info(f"Matched series '{match.get('Name', 'Unknown')}' with TVDB ID {series.tvdbid} and path {series_folder}", extra={'emoji_type': 'success'})
            else:
                logger.warning(f"No series match found for TVDB ID {series.tvdbid} in folder '{series_folder}'", extra={'emoji_type': 'warning'})
        else:
            # No folder to match, just pick first TVDB + title match
            match = next(
                (it for it in items
                if it.get('ProviderIds', {}).get('Tvdb') == str(series.tvdbid)
                and clean_title in it.get('Name', '')),
                None
            )
            if match:
                logger.info(f"Matched series '{match.get('Name', 'Unknown')}' with TVDB ID {series.tvdbid} (no folder match)", extra={'emoji_type': 'success'})
        
        if not match:
            logger.error(f"No series match found for '{series.title}' with TVDB ID {series.tvdbid}", extra={'emoji_type': 'search'})
            return False
        jf_series_id = match['Id']
        save_jellyfin_arr_id(dbsession, Series, series.id, match)

    # 2) Cache season IDs
    seas_url = build_jellyfin_url(
        f"Users/{user_id}/Items?ParentId={jf_series_id}"
        "&includeItemTypes=Season&fields=IndexNumber,Id,Path"
    )
    season = dbsession.query(Season).get(ep.season_id)
    logger.info(f"Looking for season {season.season_number} in Jellyfin series {jf_series_id}", extra={'emoji_type': 'search'})
    
    def pick_seasons(items: List[dict]) -> List[dict]:
        return [
            ss for ss in (items or [])
            if ss.get("IndexNumber") == season.season_number
        ]
    seasons = retry_call(
        func=lambda: safe_get_json_items(seas_url),
        on_error=lambda ex: logger.error(f"Season list error for series {series.id}: {ex}", extra={'emoji_type': 'error'}),
        retry_interval=3,
        retry_timeout=9,  # 3 attempts max
        success_condition=lambda res: pick_seasons(res)
    ) or []

    logger.debug(f"Found {len(seasons)} seasons for series", extra={'emoji_type': 'debug'})

    if len(seasons) == 0 and series.jellyfin_id:
        logger.warning(f"No seasons found using jellyfin_id {jf_series_id}, clearing stale ID to force re-search", extra={'emoji_type': 'warning'})
        series.jellyfin_id = None
        dbsession.add(series)
        safe_commit(dbsession, series)
        return False

    season_map = {s['IndexNumber']: s for s in seasons if 'IndexNumber' in s}
    for num, it in season_map.items():
        logger.debug(f"Processing season {num} mapping to database", extra={'emoji_type': 'processing'})
        db_seas = (
            dbsession.query(Season)
            .filter_by(series_id=series.id, season_number=num)
            .first()
        )
        if db_seas and not db_seas.jellyfin_id:
            logger.info(f"Saving Jellyfin season ID for season {num}", extra={'emoji_type': 'success'})
            save_jellyfin_arr_id(dbsession, Season, db_seas.id, it)

    # 3) Process queued episode SubFlows for this series
    ep_ids = [e.id for e in dbsession.query(Episode).join(Season).filter(Season.series_id == series.id)]
    logger.debug(f"Found {len(ep_ids)} total episodes for series {series.id}", extra={'emoji_type': 'search'})
    
    queued = dbsession.query(SubFlow).filter(
        SubFlow.status == 'QUEUED',
        SubFlow.action == action,
        SubFlow.episode_id.in_(ep_ids)
    ).all()
    if not queued:
        logger.info(f"No queued {action} episodes found for series {series.id}", extra={'emoji_type': 'info'})
        return False
        
    logger.info(f"Processing {len(queued)} queued episode subflows for series {series.id}", extra={'emoji_type': 'processing'})
    matched = False

    # 4) Match each queued episode by path
    season_data = season_map.get(ep.season_number)
    if not season_data:
        logger.warning(f"No Jellyfin season found for season number {ep.season_number} in series {series.id}", extra={'emoji_type': 'warning'})
        return False
    
    jf_seas_id = season_data.get('Id')
    logger.debug(f"Using Jellyfin season ID {jf_seas_id} for season {ep.season_number}", extra={'emoji_type': 'debug'})
    if not jf_seas_id:
        logger.warning(f"Season data found but no ID for season {ep.season_number}", extra={'emoji_type': 'warning'})
        return False
    
    epi_url = build_jellyfin_url(
            f"Users/{user_id}/Items?ParentId={jf_seas_id}"
            "&includeItemTypes=Episode&fields=IndexNumber,Id,Path,Overview,Name"
        )
    logger.debug(f"Searching for episodes in season with URL: {epi_url}", extra={'emoji_type': 'search'})
    def pick_episode(items: List[dict]) -> List[dict]:
        return [
            ss for ss in (items or [])
            if ss.get("IndexNumber") == ep.episode_number
            and ss.get("Path") == ep.filepath
        ]
    eps = retry_call(
        func=lambda: safe_get_json_items(epi_url),
        on_error=lambda ex: logger.error(f"Episode list error for season {jf_seas_id}: {ex}", extra={'emoji_type': 'error'}),
        retry_interval=3,
        retry_timeout=9,  # 3 attempts max
        success_condition=lambda res: bool(pick_episode(res))
    ) or []
    
    logger.debug(f"Found {len(eps)} episodes to process from Jellyfin", extra={'emoji_type': 'debug'})
    
    for sf in queued:
        logger.debug(f"Processing subflow {sf.id} for episode {sf.episode_id}", extra={'emoji_type': 'processing'})
        steps = sf.steps.split(',')
        if sf.step_index < len(steps) and steps[sf.step_index] != 'verify_arr_scan_jellyfin': ## change the step index to -1
            logger.debug(f"Skipping subflow {sf.id} - wrong step or step index", extra={'emoji_type': 'debug'})
            continue
        ep2 = dbsession.query(Episode).get(sf.episode_id)
        if ep2.season_number != season.season_number:
            logger.debug(f"Skipping episode {ep2.id} - different season ({ep2.season_number} vs {season.season_number})", extra={'emoji_type': 'debug'})
            continue

        logger.debug(f"Matching episode {ep2.episode_number} with path {ep2.filepath}", extra={'emoji_type': 'search'})
        for it in eps:
            if it['IndexNumber'] != ep2.episode_number:
                continue
            if it['Path'] == ep2.filepath:
                logger.info(f"Successfully matched episode {ep2.episode_number} in Jellyfin", extra={'emoji_type': 'success'})
                save_jellyfin_arr_id(dbsession, Episode, ep2.id, it)
                if steps.index('verify_arr_scan_jellyfin') + 1 < len(steps):
                    sf.step_index = steps.index('verify_arr_scan_jellyfin')+1
                    sf.status = 'PENDING'  # Set to PENDING so scheduler picks it up
                else:
                    sf.status = 'DONE'
                dbsession.add(sf)
                safe_commit(dbsession, sf)
                if ep2.id == ent_id:
                    matched = True
                break
    if not matched:
        # Generate detailed error info for debugging
        candidates = []
        dummy_found = False
        dummy_base_tv = (settings.DUMMY_TV_LIBRARY_FOLDER or "").rstrip(os.sep)
        
        for it in eps:
            c_path = it.get('Path', '')
            candidates.append(f"(Idx: {it.get('IndexNumber')}, Path: {c_path})")
            # detect if we are finding dummy items
            if c_path and dummy_base_tv and c_path.startswith(dummy_base_tv):
                dummy_found = True
        
        candidates_str = ", ".join(candidates) if candidates else "None"
        msg = f"No matching Jellyfin episode found for {ep2.filepath if 'ep2' in locals() else 'unknown'} (Season {season.season_number}). Found candidates: {candidates_str}"
        
        logger.warning(msg, extra={'emoji_type': 'warning'})
        
        # Log variables for debugging
        logger.debug(f"Poison check vars: existing={existing}, dummy_found={dummy_found}, eps_count={len(eps) if eps else 0}", extra={'emoji_type': 'debug'})
        
        # Aggressive POISONED ID check
        if dummy_found or (existing and not eps):
            logger.warning(f"Detected potential POISONED Series ID {jf_series_id} (found dummy items or empty result while expecting real). Resetting Series/Season IDs to force fresh search.", extra={'emoji_type': 'reset'})
            
            # Reset Series ID
            series.jellyfin_id = None
            dbsession.add(series)
            
            # Reset Season IDs - AGGRESSIVE CLEANUP
            # If the Series is poisoned, likely ALL seasons are poisoned. Reset them all.
            all_seasons = dbsession.query(Season).filter(Season.series_id == series.id).all()
            for s in all_seasons:
                s.jellyfin_id = None
                dbsession.add(s)
            
            # Reset Episode ID just in case
            ep.jellyfin_id = None
            dbsession.add(ep)
            
            safe_commit(dbsession)
            raise Exception(f"Reset stale/poisoned Series ID {jf_series_id} pointing to dummy library (and all {len(all_seasons)} seasons) - will retry search")

        if existing:
            logger.warning(f"Existing series ID {jf_series_id} may be stale - consider resetting to force re-search", extra={'emoji_type': 'warning'})
            for s in seasons:
                db_seas = (
                    dbsession.query(Season)
                    .filter_by(series_id=series.id, season_number=s['IndexNumber'])
                    .first()
                )
                if db_seas and db_seas.jellyfin_id:
                    db_seas.jellyfin_id = None
                    dbsession.add(db_seas)
            series.jellyfin_id = None
            dbsession.add(series)
            safe_commit(dbsession, series)

        # CRITICAL: Raise exception so scheduler records the specific error
        raise Exception(msg)

    logger.info(f"Episode matching completed for series {series.id}, matched: {matched}", extra={'emoji_type': 'success' if matched else 'warning'})
    return matched

def verify_dummy_scan_jellyfin(dbsession: Session, ent_id: int, model: Type, action) -> bool:
    """
    Process a "verify_dummy_scan_jellyfin" step for one entity:

    Movie:
      - Search Jellyfin by title, match playback path to dummypath
      - Save Jellyfin movie ID, mark its SubFlow DONE

    Episode:
      - Determine series from episode, dedupe by folder name
      - Cache series and season IDs
      - Fetch all queued Episode SubFlows for that series
      - For each, match dummypath to playback path, save episode ID, mark SubFlow DONE

    Returns True if Episode and any SubFlow was updated; False otherwise.
    """
    logger.info(f"Starting verify_dummy_scan_jellyfin for {model.__name__} {ent_id} with action {action}", extra={'emoji_type': 'processing'})
    # MOVIE CASE
    user_id = get_admin_user()
    if model is Movie:
        logger.debug("Processing movie case", extra={'emoji_type': 'movie'})
        m = dbsession.query(Movie).get(ent_id)
        if not m:
            logger.warning(f"Movie {ent_id} not found", extra={'emoji_type': 'warning'})
            return False
            
        # Short-circuit for delete action if placeholder doesn't exist
        if 'delete' in action.lower() and hasattr(m, 'placeholder_exists') and not m.placeholder_exists:
            logger.info(f"Skipping dummy verify for Movie {ent_id} (delete action, no placeholder)", extra={'emoji_type': 'skip'})
            return True
        
        # For delete actions, check if placeholder file actually exists (dummypath might be None for deleted movies)
        import os
        if 'delete' in action.lower():
            if not m.dummypath or not os.path.exists(m.dummypath):
                logger.info(f"Placeholder file {'(no dummypath)' if not m.dummypath else m.dummypath} already deleted - verification complete", extra={'emoji_type': 'success'})
                # Update placeholder_exists status
                if hasattr(m, 'placeholder_exists'):
                    m.placeholder_exists = False
                    dbsession.add(m)
                    safe_commit(dbsession, m)
                return True
        
        # For non-delete actions, dummypath is required
        if not m.dummypath:
            logger.warning(f"Movie {ent_id} missing dummypath", extra={'emoji_type': 'warning'})
            return False
            
        logger.debug(f"Movie {m.id} has dummypath: {m.dummypath}", extra={'emoji_type': 'debug'})
        if m.jellyfin_dummy_id:
            logger.debug(f"Movie {m.id} already has Jellyfin ID, checking if still exists", extra={'emoji_type': 'search'})
            it = None
            try:
                # Fix URL format: use Items/{id} instead of Items?{id}/
                movie_url = build_jellyfin_url(f"Items/{m.jellyfin_dummy_id}?userId={user_id}&fields=ProviderIds,Name,ProductionYear,Overview,Path")
                logger.debug(f"Checking movie existence with URL: {movie_url}", extra={'emoji_type': 'debug'})
                logger.debug(f"Movie jellyfin_dummy_id: {m.jellyfin_dummy_id}, user_id: {user_id}", extra={'emoji_type': 'debug'})

                def success_movie(res):
                    if res.status_code in (200, 204, 404):
                        if res.status_code == 200:
                            data = res.json()
                            # For single item query, response is the item directly, not wrapped in Items array
                            return data and isinstance(data, dict) and data.get('Id')
                        return True  # 404 means item doesn't exist, which is also a valid response
                    return False
                
                response = retry_call(
                    func=lambda: session.get(movie_url, timeout=5),
                    on_error=lambda ex: logger.error(f"Movie search error: {ex}", extra={'emoji_type': 'error'}),
                    retry_interval=3, retry_timeout=9,  # 3 attempts max
                    success_condition=lambda res: success_movie(res)
                )
                
                items = []
                if response and response.status_code == 200:
                    data = response.json()
                    # Single item response - wrap in array for compatibility
                    items = [data] if data and data.get('Id') else []
                
                logger.debug(f"Found {len(items)} items when checking movie", extra={'emoji_type': 'debug'})
                for candidate in items:
                    if (
                        int(candidate.get("ProviderIds", {}).get("Tmdb", -1)) == m.tmdbid and
                        candidate.get('Path', '') == m.dummypath and
                        (not m.year or int(candidate.get("ProductionYear", -1)) == m.year)
                    ):
                        logger.info(f"Found existing movie match in Jellyfin: {candidate.get('Name', 'Unknown')}", extra={'emoji_type': 'success'})
                        it = candidate
                        break
            except Exception as ex:
                logger.error(f"Error fetching Jellyfin item by ID: {ex}", extra={'emoji_type': 'error'})
            if it:
                logger.info(f"Saving existing Jellyfin movie ID for movie {m.id}", extra={'emoji_type': 'success'})
                save_jellyfin_dummy_id(dbsession, Movie, m.id, it)
                return True
            else:
                logger.warning(f"Movie {m.id} has jellyfin_dummy_id but item not found in Jellyfin - clearing old ID and searching by title", extra={'emoji_type': 'warning'})
                # Clear the old jellyfin_dummy_id so we can search by title/tmdb instead
                m.jellyfin_dummy_id = None
                dbsession.add(m)
                safe_commit(dbsession, m)
                # Fall through to search by title
            
        logger.debug(f"Searching for new movie match: {m.title}", extra={'emoji_type': 'search'})
        clean_title = re.sub(r"\s*\(\d{4}\)$", "", m.title)
        url = build_jellyfin_url(
            f"Items?searchTerm={quote_plus(clean_title)}"
            "&includeItemTypes=Movie&recursive=true&fields=ProviderIds,Name,ProductionYear,Overview,Path"
        )
        logger.debug(f"Movie search URL: {url}", extra={'emoji_type': 'debug'})
        def filter_movies(res):
            if res.status_code not in (200, 204, 404):
                return False
            if res.status_code == 404:
                return True
            response_items = res.json().get('Items', [])
            return [
                item for item in (response_items or [])
                if (
                    int(item.get("ProviderIds", {}).get("Tmdb", -1)) == m.tmdbid and item.get('Path',{}) == m.dummypath
                )
                and (not m.year or int(item.get("ProductionYear", -1)) == m.year)
            ]
        response = retry_call(
            func=lambda: session.get(url, timeout=5),
            on_error=lambda ex: logger.error(f"Movie search error: {ex}", extra={'emoji_type': 'error'}),
            retry_interval=3, retry_timeout=9,  # 3 attempts max
            success_condition=lambda res: bool(filter_movies(res))
        )
        
        items = filter_movies(response) if response else []
        
        logger.debug(f"Found {len(items)} candidate movies from search", extra={'emoji_type': 'debug'})
        
        for it in items:
            if it.get('ProviderIds', {}).get('Tmdb') != m.tmdbid and it.get('Path',{}) != m.dummypath:
                logger.debug(f"Skipping movie candidate due to TMDB/path mismatch", extra={'emoji_type': 'debug'})
                continue
            logger.info(f"Successfully matched movie '{it.get('Name', 'Unknown')}' in Jellyfin", extra={'emoji_type': 'success'})
            save_jellyfin_dummy_id(dbsession, Movie, m.id, it)
            return True
        
        # Prepare failure message if loop finishes without match
        # Generate detailed error info for debugging
        candidates = []
        for it in items:
            candidates.append(f"(Name: {it.get('Name')}, Id: {it.get('Id')}, Path: {it.get('Path')})")
        
        candidates_str = ", ".join(candidates) if candidates else "None"
        msg = f"No matching movie found in Jellyfin for {m.title} (TVDB: {m.tmdbid}, Path: {m.dummypath}). Found candidates: {candidates_str}"
        
        logger.warning(msg, extra={'emoji_type': 'warning'})
        # CRITICAL: Raise exception so scheduler records the specific error
        raise Exception(msg)
        
        return False

    # EPISODE CASE
    logger.debug("Processing episode case", extra={'emoji_type': 'tv'})
    ep = dbsession.query(Episode).get(ent_id)
    if not ep:
        logger.warning(f"Episode {ent_id} not found", extra={'emoji_type': 'warning'})
        return False
        
    # Short-circuit for delete action if placeholder doesn't exist
    if 'delete' in action.lower() and hasattr(ep, 'placeholder_exists') and not ep.placeholder_exists:
        logger.info(f"Skipping dummy verify for Episode {ent_id} (delete action, no placeholder)", extra={'emoji_type': 'skip'})
        return True
    
    # For delete actions, check if placeholder file actually exists (dummypath might be None for deleted episodes)
    import os
    if 'delete' in action.lower():
        if not ep.dummypath or not os.path.exists(ep.dummypath):
            logger.info(f"Placeholder file {'(no dummypath)' if not ep.dummypath else ep.dummypath} already deleted - verification complete", extra={'emoji_type': 'success'})
            # Update placeholder_exists status
            if hasattr(ep, 'placeholder_exists'):
                ep.placeholder_exists = False
                dbsession.add(ep)
                safe_commit(dbsession, ep)
            return True
    
    # For non-delete actions, dummypath is required
    if not ep.dummypath:
        logger.warning(f"Episode {ent_id} missing dummypath", extra={'emoji_type': 'warning'})
        return False
    
    logger.debug(f"Episode {ep.id} has dummypath: {ep.dummypath}", extra={'emoji_type': 'debug'})
    
    seas = dbsession.query(Season).get(ep.season_id)
    series = dbsession.query(Series).get(seas.series_id)
    logger.info(f"Processing episode {ep.episode_number} of season {seas.season_number} for series '{series.title}'", extra={'emoji_type': 'tv'})
    # user = get_admin_user()

    # 1) Series ID with folder-based dedupe
    if series.jellyfin_dummy_id:
        logger.debug(f"Using existing Jellyfin dummy series ID: {series.jellyfin_dummy_id}", extra={'emoji_type': 'success'})
        jf_series_id = series.jellyfin_dummy_id
    else:
        logger.debug(f"Searching for series '{series.title}' in Jellyfin", extra={'emoji_type': 'search'})
        clean_title = re.sub(r"\s*\(\d{4}\)$", "", series.title)
        url = build_jellyfin_url(
            f"Items?searchTerm={quote_plus(clean_title)}"
            "&includeItemTypes=Series&recursive=true&fields=ProviderIds,Path"
        )
        logger.debug(f"Series search URL: {url}", extra={'emoji_type': 'debug'})
        folder = None
        try:
            import os as os_module  # Explicit import to avoid scoping issues
            base = settings.DUMMY_TV_LIBRARY_FOLDER.rstrip(os_module.sep) + os_module.sep
            rel = ep.dummypath.replace(base, '')
            folder_name = rel.split(os_module.sep)[0]
            folder = base + folder_name  # Full path to the series folder
            logger.debug(f"Extracted folder '{folder}' from episode path", extra={'emoji_type': 'debug'})
        except Exception as e:
            logger.error(f"Error locating Series dummy path: {e}", extra={'emoji_type': 'error'})
            return False
        def pick_series(items: List[dict]) -> List[dict]:
            return [
                s for s in (items or [])
                if s.get("ProviderIds", {}).get("Tvdb")
                and str(s["ProviderIds"]["Tvdb"]) == str(series.tvdbid)
                and s.get('Path') == folder
            ]
        items = retry_call(
            func=lambda: safe_get_json_items(url),
            on_error=lambda ex: logger.error(f"Series search error: {ex}", extra={'emoji_type': 'error'}),
            retry_interval=3, retry_timeout=9,  # 3 attempts max
            success_condition=lambda res: bool(pick_series(res))
        ) or []
        
        logger.debug(f"Found {len(items)} series candidates from search", extra={'emoji_type': 'debug'})
        match = None
        if folder:
            match = next(
                (it for it in items
                if it.get('ProviderIds', {}).get('Tvdb') == str(series.tvdbid)
                and it.get('Path') == folder),
                None
            )
            if match:
                logger.info(f"Matched series '{match.get('Name', 'Unknown')}' with TVDB ID {series.tvdbid}", extra={'emoji_type': 'success'})
            else:
                logger.warning(f"No series match found for TVDB ID {series.tvdbid} in folder '{folder}'", extra={'emoji_type': 'warning'})   
        if not match:
            logger.error(f"Failed to find series match in Jellyfin", extra={'emoji_type': 'error'})
            return False
        jf_series_id = match['Id']
        logger.info(f"Saving Jellyfin dummy series ID {jf_series_id} for series {series.id}", extra={'emoji_type': 'success'})
        save_jellyfin_dummy_id(dbsession, Series, series.id, match)

    # 2) Cache season IDs
    seas_url = build_jellyfin_url(
        f"Users/{user_id}/Items?ParentId={jf_series_id}"
        "&includeItemTypes=Season&fields=IndexNumber,Id"
    )
    season = dbsession.query(Season).get(ep.season_id)
    logger.info(f"Looking for season {season.season_number} in Jellyfin series {jf_series_id}", extra={'emoji_type': 'search'})
    
    def pick_seasons(items: List[dict]) -> List[dict]:
        return [
            ss for ss in (items or [])
            if ss.get("IndexNumber") == season.season_number
        ]
    seasons = retry_call(
        func=lambda: safe_get_json_items(seas_url),
        on_error=lambda ex: logger.error(f"Season list error for series {series.id}: {ex}", extra={'emoji_type': 'error'}),
        retry_interval=3,
        retry_timeout=9,  # 3 attempts max
        success_condition=lambda res: pick_seasons(res)
    ) or []

    logger.debug(f"Found {len(seasons)} seasons for series", extra={'emoji_type': 'debug'})
    
    # If we got 0 seasons and we used a cached jellyfin_dummy_id, the ID might be stale
    # Clear it and return False to trigger a retry that will re-search
    if len(seasons) == 0 and series.jellyfin_dummy_id:
        logger.warning(f"No seasons found using jellyfin_dummy_id {jf_series_id}, clearing stale ID to force re-search", extra={'emoji_type': 'warning'})
        series.jellyfin_dummy_id = None
        dbsession.add(series)
        safe_commit(dbsession, series)
        return False
    
    season_map = {s['IndexNumber']: s for s in seasons if 'IndexNumber' in s}
    for num, it in season_map.items():
        logger.debug(f"Processing season {num} mapping to database", extra={'emoji_type': 'processing'})
        db_seas = (
            dbsession.query(Season)
            .filter_by(series_id=series.id, season_number=num)
            .first()
        )
        if db_seas and not db_seas.jellyfin_dummy_id:
            logger.info(f"Saving Jellyfin season ID for season {num}", extra={'emoji_type': 'success'})
            save_jellyfin_dummy_id(dbsession, Season, db_seas.id, it)

    # 3) Process queued episode SubFlows for this series
    ep_ids = [e.id for e in dbsession.query(Episode).join(Season).filter(Season.series_id == series.id)]
    queued = dbsession.query(SubFlow).filter(
        SubFlow.status.in_(['QUEUED', 'FAILED']),
        SubFlow.action == action,
        SubFlow.episode_id.in_(ep_ids)
    ).all()
    if not queued:
        return False
    matched = False

    # 4) Match each queued episode by path
    season_data = season_map.get(ep.season_number)
    if not season_data:
        logger.warning(f"No Jellyfin season found for season number {ep.season_number} in series {series.id}", extra={'emoji_type': 'warning'})
        return False
    
    jf_seas_id = season_data.get('Id')
    if not jf_seas_id:
        logger.warning(f"Season data found but no ID for season {ep.season_number}", extra={'emoji_type': 'warning'})
        return False
    
    epi_url = build_jellyfin_url(
            f"Users/{user_id}/Items?ParentId={jf_seas_id}"
            "&includeItemTypes=Episode&fields=IndexNumber,Id,Path,Overview,Name"
        )
    def pick_episode(items: List[dict]) -> List[dict]:
        return [
            ss for ss in (items or [])
            if ss.get("IndexNumber") == ep.episode_number
            and ss.get("Path") == ep.dummypath
        ]
    eps = retry_call(
        func=lambda: safe_get_json_items(epi_url),
        on_error=lambda ex: logger.error(f"❌ Episode list error for season {jf_seas_id}: {ex}"),
        retry_interval=3,
        retry_timeout=9,  # 3 attempts max
        success_condition=lambda res: bool(pick_episode(res))
    ) or []
    for sf in queued:
        steps = sf.steps.split(',')
        if sf.step_index < len(steps) and steps[sf.step_index] != 'verify_dummy_scan_jellyfin': ## change the step index to -1
            continue
        ep2 = dbsession.query(Episode).get(sf.episode_id)
        if ep2.season_number != seas.season_number:
            continue

        for it in eps:
            if it['IndexNumber'] != ep2.episode_number:
                continue
            if it['Path'] == ep2.dummypath:
                save_jellyfin_dummy_id(dbsession, Episode, ep2.id, it)
                if sf.step_index + 1 < len(steps):
                    old_step = sf.step_index
                    sf.step_index += 1
                    sf.status = 'PENDING'  # Set to PENDING so scheduler picks it up
                    # Update entity's current_step_name to match new step_index
                    ep2.current_step_name = steps[sf.step_index]
                    logger.verbose(f"SubFlow {sf.id} for episode {ep2.id} advanced from step index {old_step} to {sf.step_index}", extra={'emoji_type': 'info'})
                else:
                    sf.status = 'DONE'
                    # Update entity's current_step_name to match final step
                    ep2.current_step_name = steps[sf.step_index]
                dbsession.add(sf)
                safe_commit(dbsession, sf)
                if ep2.id == ent_id:
                    matched = True
                break
    if not matched:
        logger.warning(f"⚠️ No matching Jellyfin episode found for {ep2.dummypath} in season {jf_seas_id}")

    return matched

def get_admin_user():
    logger.debug("Looking for Jellyfin admin user", extra={'emoji_type': 'debug'})
    try:
        users = session.get(build_jellyfin_url("Users"), timeout=5).json()
        admin = next(u for u in users if u.get("Policy", {}).get("IsAdministrator"))
        user_id = admin["Id"]
        logger.debug(f"Found admin user: {admin.get('Name', 'Unknown')} ({user_id})", extra={'emoji_type': 'success'})
        return user_id
    except Exception as ex:
        logger.error(f"Cannot find admin user: {ex}", extra={"emoji_type": "error"})
        return False


def update_jellyfin_nfo_status(
    dbsession: Session,
    ent_id: int,
    model: Type,
    action: str = "status_update",
    new_status: str = "Request"
) -> bool:
    """
    Update status in NFO file and trigger Jellyfin refresh.
    This replaces the old API-based title update approach.

    Args:
        dbsession: SQLAlchemy session.
        ent_id: Entity ID.
        model: Model type (Movie, Series, Season, or Episode).
        action: Action name from SubFlow.
        new_status: New status to set in NFO (e.g., "Downloaded", "Request")

    Returns:
        True if NFO update and refresh succeeded, False otherwise.
    """
    if not settings.jellyfin_enabled:
        logger.info("Jellyfin is disabled, skipping NFO status update", extra={'emoji_type': 'skip'})
        return True

    logger.info(f"Updating NFO status for {model.__name__} {ent_id} to '{new_status}'", extra={'emoji_type': 'status'})
    
    try:
        entity = dbsession.query(model).get(ent_id)
        if not entity:
            logger.warning(f"{model.__name__} {ent_id} not found", extra={'emoji_type': 'warning'})
            return False
        
        # If entity is marked as deleted, skip updating NFO status
        if hasattr(entity, 'is_deleted') and entity.is_deleted:
            logger.info(f"{model.__name__} {ent_id} is deleted, skipping NFO status update", extra={'emoji_type': 'skip'})
            return True
        
        # Get NFO file path
        nfo_path = getattr(entity, 'nfo_path', None)
        if not nfo_path:
            logger.warning(f"No NFO path found for {model.__name__} {ent_id}", extra={'emoji_type': 'warning'})
            return False
        
        # Update NFO file status
        success = update_nfo_status(nfo_path, new_status)
        if not success:
            return False
        
        # Trigger Jellyfin refresh for the NFO file and placeholder
        dummy_path = getattr(entity, 'dummypath', None)
        if dummy_path:
            paths_to_refresh = [nfo_path]
            if os.path.exists(dummy_path):
                paths_to_refresh.append(dummy_path)
            
            try:
                refresh_jellyfin_item(paths_to_refresh, 'Modified')
                path_list = ', '.join(paths_to_refresh)
                logger.info(f"Successfully triggered Jellyfin refresh for updated NFO: {path_list}", extra={'emoji_type': 'success'})
            except Exception as e:
                logger.warning(f"NFO updated but Jellyfin refresh failed: {e}", extra={'emoji_type': 'warning'})
                # Still return True since NFO was updated successfully
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to update NFO status for {model.__name__} {ent_id}: {e}", extra={'emoji_type': 'error'})
        return False


def final_cleanup_orphaned_nfo_files(dbsession, episode, deleted_files):
    """
    Final cleanup check for orphaned season.nfo and tvshow.nfo files.
    This runs as a fallback to catch files missed due to race conditions.
    
    Args:
        dbsession: Database session
        episode: The episode entity that was just processed
        deleted_files: List of files already deleted (for logging)
    """
    try:
        from services.postgres.models import Episode, Season, Series
        
        if not episode.dummypath:
            return
            
        season = dbsession.query(Season).get(episode.season_id) if episode.season_id else None
        series = dbsession.query(Series).get(season.series_id) if season else None
        
        if not (season and series):
            return
            
        logger.debug(f"🧹 FINAL CLEANUP: Checking for orphaned NFO files after episode {episode.id} deletion", extra={'emoji_type': 'debug'})
        
        # Check for orphaned season.nfo
        season_folder = os.path.dirname(episode.dummypath)
        season_nfo_path = os.path.join(season_folder, "season.nfo")
        
        if os.path.exists(season_nfo_path):
            # Double-check if this season really has no more active episodes
            active_episodes_in_season = dbsession.query(Episode).filter(
                Episode.season_id == season.id,
                Episode.is_deleted == False
            ).count()
            
            if active_episodes_in_season == 0:
                logger.info(f"🧹 FINAL CLEANUP: Found orphaned season.nfo with 0 active episodes, deleting", extra={'emoji_type': 'delete'})
                if delete_nfo_file(season_nfo_path):
                    deleted_files.append(f"orphaned season NFO: season.nfo")
        
        # Check for orphaned tvshow.nfo
        series_folder = os.path.dirname(season_folder)
        tvshow_nfo_path = os.path.join(series_folder, "tvshow.nfo")
        
        if os.path.exists(tvshow_nfo_path):
            # Double-check if this series really has no more active episodes
            active_episodes_in_series = dbsession.query(Episode).join(Season).filter(
                Season.series_id == series.id,
                Episode.is_deleted == False
            ).count()
            
            if active_episodes_in_series == 0:
                logger.info(f"🧹 FINAL CLEANUP: Found orphaned tvshow.nfo with 0 active episodes, deleting", extra={'emoji_type': 'delete'})
                if delete_nfo_file(tvshow_nfo_path):
                    deleted_files.append(f"orphaned series NFO: tvshow.nfo")
                    
                    # Clean up empty directories
                    cleanup_empty_series_structure(series_folder, deleted_files)
        
        if any("orphaned" in f for f in deleted_files):
            logger.info(f"🧹 FINAL CLEANUP: Removed orphaned NFO files that were missed due to race conditions", extra={'emoji_type': 'success'})
            
    except Exception as e:
        logger.debug(f"Error during final NFO cleanup: {e}", extra={'emoji_type': 'debug'})

def cleanup_empty_series_structure(series_folder, deleted_files):
    """
    Clean up empty season and series directories and trigger Jellyfin refresh.
    
    Args:
        series_folder: Path to the series folder
        deleted_files: List of deleted files for logging context
    """
    try:
        if not series_folder or not os.path.exists(series_folder):
            return
        
        directories_cleaned = []
        logger.info(f"🧹 SERIES CLEANUP: Checking series structure for empty directories: {series_folder}", extra={'emoji_type': 'delete'})
        
        # Check for empty season directories first
        try:
            series_contents = os.listdir(series_folder)
            season_dirs = [d for d in series_contents if os.path.isdir(os.path.join(series_folder, d)) and d.startswith('Season')]
            
            for season_dir_name in season_dirs:
                season_path = os.path.join(series_folder, season_dir_name)
                try:
                    season_contents = os.listdir(season_path)
                    if not season_contents:
                        os.rmdir(season_path)
                        directories_cleaned.append(f"empty season folder: {season_dir_name}")
                        logger.info(f"🗑️ Deleted empty season directory: {season_path}", extra={'emoji_type': 'delete'})
                except Exception as e:
                    logger.debug(f"Failed to remove season directory {season_path}: {e}", extra={'emoji_type': 'debug'})
            
            # Check if series directory is now empty
            updated_series_contents = os.listdir(series_folder)
            if not updated_series_contents:
                parent_dir = os.path.dirname(series_folder)
                os.rmdir(series_folder)
                directories_cleaned.append(f"empty series folder: {os.path.basename(series_folder)}")
                logger.info(f"🗑️ Deleted empty series directory: {series_folder}", extra={'emoji_type': 'delete'})
                
                # Trigger Jellyfin refresh for parent directory to reflect series deletion
                try:
                    refresh_jellyfin_item([parent_dir], 'Deleted')
                    logger.info(f"🔄 Triggered Jellyfin refresh after complete series cleanup: {parent_dir}", extra={'emoji_type': 'refresh'})
                except Exception as refresh_e:
                    logger.warning(f"Failed to refresh Jellyfin after series cleanup: {refresh_e}", extra={'emoji_type': 'warning'})
            else:
                logger.debug(f"Series directory not empty after season cleanup: {updated_series_contents}", extra={'emoji_type': 'debug'})
                
        except Exception as e:
            logger.debug(f"Failed to cleanup series structure {series_folder}: {e}", extra={'emoji_type': 'debug'})
        
        # Log cleanup summary
        if directories_cleaned:
            logger.info(f"🧹 SERIES CLEANUP SUMMARY: Removed {len(directories_cleaned)} empty directories: {', '.join(directories_cleaned)}", extra={'emoji_type': 'delete'})
        else:
            logger.debug(f"🧹 SERIES CLEANUP: No empty directories found to clean up in {series_folder}", extra={'emoji_type': 'debug'})
            
    except Exception as e:
        logger.debug(f"Error during series structure cleanup: {e}", extra={'emoji_type': 'debug'})


def cleanup_empty_directories(entity, model, deleted_files):
    """
    Clean up empty season and series directories after NFO deletion.
    
    Args:
        entity: The database entity (Episode, Movie, Series)
        model: The model type 
        deleted_files: List of deleted files for logging context
    """
    if not deleted_files:
        return  # No NFO files were deleted, no cleanup needed
    
    try:
        import os
        import shutil
        from services.postgres.models import Episode, Season, Series
        
        logger.info(f"🧹 DIRECTORY CLEANUP: Starting empty directory cleanup after NFO deletion", extra={'emoji_type': 'delete'})
        
        directories_cleaned = []
        
        if model.__name__ == 'Episode':
            # For episodes, check if season folder is empty, then series folder
            episode = entity
            if hasattr(episode, 'dummypath') and episode.dummypath:
                # Get season directory from episode path
                season_dir = os.path.dirname(episode.dummypath)
                if os.path.exists(season_dir):
                    # Check if season directory is empty
                    try:
                        season_contents = os.listdir(season_dir)
                        if not season_contents:
                            os.rmdir(season_dir)
                            directories_cleaned.append(f"empty season folder: {os.path.basename(season_dir)}")
                            logger.info(f"🗑️ Deleted empty season directory: {season_dir}", extra={'emoji_type': 'delete'})
                            
                            # Now check if series directory is empty
                            series_dir = os.path.dirname(season_dir)
                            if os.path.exists(series_dir):
                                try:
                                    series_contents = os.listdir(series_dir)
                                    if not series_contents:
                                        os.rmdir(series_dir)
                                        directories_cleaned.append(f"empty series folder: {os.path.basename(series_dir)}")
                                        logger.info(f"🗑️ Deleted empty series directory: {series_dir}", extra={'emoji_type': 'delete'})
                                        

                                    else:
                                        logger.debug(f"Series directory not empty after season cleanup: {series_contents}", extra={'emoji_type': 'debug'})
                                except Exception as e:
                                    logger.debug(f"Failed to remove series directory {series_dir}: {e}", extra={'emoji_type': 'debug'})
                        else:
                            logger.debug(f"Season directory not empty: {season_contents}", extra={'emoji_type': 'debug'})
                    except Exception as e:
                        logger.debug(f"Failed to check/remove season directory {season_dir}: {e}", extra={'emoji_type': 'debug'})
        
        elif model.__name__ == 'Series':
            # For series deletion, clean up all season directories and then series directory
            series = entity
            if hasattr(series, 'dummypath') and series.dummypath and os.path.exists(series.dummypath):
                # Get all season directories in the series folder
                try:
                    series_contents = os.listdir(series.dummypath)
                    season_dirs = [d for d in series_contents if os.path.isdir(os.path.join(series.dummypath, d)) and d.startswith('Season')]
                    
                    # Remove empty season directories
                    for season_dir_name in season_dirs:
                        season_path = os.path.join(series.dummypath, season_dir_name)
                        try:
                            season_contents = os.listdir(season_path)
                            if not season_contents:
                                os.rmdir(season_path)
                                directories_cleaned.append(f"empty season folder: {season_dir_name}")
                                logger.info(f"🗑️ Deleted empty season directory: {season_path}", extra={'emoji_type': 'delete'})
                        except Exception as e:
                            logger.debug(f"Failed to remove season directory {season_path}: {e}", extra={'emoji_type': 'debug'})
                    
                    # Now check if series directory is empty
                    updated_series_contents = os.listdir(series.dummypath)
                    if not updated_series_contents:
                        parent_dir = os.path.dirname(series.dummypath)
                        os.rmdir(series.dummypath)
                        directories_cleaned.append(f"empty series folder: {os.path.basename(series.dummypath)}")
                        logger.info(f"🗑️ Deleted empty series directory: {series.dummypath}", extra={'emoji_type': 'delete'})
                        

                    else:
                        logger.debug(f"Series directory not empty: {updated_series_contents}", extra={'emoji_type': 'debug'})
                except Exception as e:
                    logger.debug(f"Failed to cleanup series directory {series.dummypath}: {e}", extra={'emoji_type': 'debug'})
        
        elif model.__name__ == 'Movie':
            # For movies, check if movie directory is empty after NFO deletion
            movie = entity
            if hasattr(movie, 'dummypath') and movie.dummypath:
                movie_dir = os.path.dirname(movie.dummypath) if os.path.isfile(movie.dummypath) else movie.dummypath
                if os.path.exists(movie_dir):
                    try:
                        movie_contents = os.listdir(movie_dir)
                        if not movie_contents:
                            parent_dir = os.path.dirname(movie_dir)
                            os.rmdir(movie_dir)
                            directories_cleaned.append(f"empty movie folder: {os.path.basename(movie_dir)}")
                            logger.info(f"🗑️ Deleted empty movie directory: {movie_dir}", extra={'emoji_type': 'delete'})
                            

                        else:
                            logger.debug(f"Movie directory not empty: {movie_contents}", extra={'emoji_type': 'debug'})
                    except Exception as e:
                        logger.debug(f"Failed to check/remove movie directory {movie_dir}: {e}", extra={'emoji_type': 'debug'})
        
        # Log cleanup summary
        if directories_cleaned:
            logger.info(f"🧹 DIRECTORY CLEANUP SUMMARY: Removed {len(directories_cleaned)} empty directories: {', '.join(directories_cleaned)}", extra={'emoji_type': 'delete'})
        else:
            logger.debug(f"🧹 DIRECTORY CLEANUP: No empty directories found to clean up", extra={'emoji_type': 'debug'})
            
    except Exception as e:
        logger.debug(f"Error during directory cleanup: {e}", extra={'emoji_type': 'debug'})


def delete_jellyfin_nfo(dbsession: Session, ent_id: int, model: type, action: str) -> bool:
    """
    Delete NFO files and clean up leftover metadata for a specific entity.
    This function is called by the scheduler as part of delete flows.
    
    For Episodes: Deletes episode.nfo, season.nfo if no episodes remain, tvshow.nfo if series is empty
    For Movies: Deletes movie.nfo
    For Series: Deletes all NFO files in the series hierarchy

    Args:
        dbsession: SQLAlchemy session.
        ent_id: Entity ID.
        model: Model type (Movie, Series, Season, or Episode).
        action: Action name from SubFlow.

    Returns:
        True if NFO deletion succeeded and verification passed, False otherwise.
    """
    if not settings.jellyfin_enabled:
        logger.info("Jellyfin is disabled, skipping NFO deletion", extra={'emoji_type': 'skip'})
        return True

    logger.info(f"🗑️ NFO DELETION: Starting NFO cleanup for {model.__name__} {ent_id}, action '{action}'", extra={'emoji_type': 'delete'})
    
    try:
        entity = dbsession.query(model).get(ent_id)
        if not entity:
            logger.warning(f"{model.__name__} {ent_id} not found", extra={'emoji_type': 'warning'})
            return True  # Consider it success if entity doesn't exist
        
        deleted_files = []
        verification_failed = []
        
        if model.__name__ == 'Episode':
            # Handle episode NFO deletion with cleanup
            episode = entity
            season = dbsession.query(Season).get(episode.season_id) if episode.season_id else None
            series = dbsession.query(Series).get(season.series_id) if season and season.series_id else None
            
            # Delete episode NFO file
            episode_nfo_path = getattr(episode, 'nfo_path', None)
            if episode_nfo_path and os.path.exists(episode_nfo_path):
                if delete_nfo_file(episode_nfo_path):
                    deleted_files.append(f"episode NFO: {os.path.basename(episode_nfo_path)}")
                else:
                    verification_failed.append(f"episode NFO: {episode_nfo_path}")
            episode.nfo_path = None
            dbsession.add(episode)
            safe_commit(dbsession, episode)
            
            # Check if we should delete season.nfo (if no other episodes remain)
            # Use SELECT FOR UPDATE to prevent race conditions with concurrent episode deletions
            if season and series:
                # Lock the season row to prevent concurrent checks
                locked_season = dbsession.query(Season).filter(Season.id == season.id).with_for_update().first()
                
                # For series delete operations, check episodes not being deleted in this batch
                if action == 'handle_seriesdelete':
                    # Count episodes that aren't being processed for deletion (different action or not PENDING/QUEUED for seriesdelete)
                    remaining_episodes = dbsession.query(Episode).filter(
                        Episode.season_id == season.id,
                        Episode.id != episode.id,
                        Episode.is_deleted == False,
                        or_(
                            Episode.action != 'handle_seriesdelete',
                            and_(Episode.action == 'handle_seriesdelete', Episode.status.in_(['DONE', 'CANCELLED', 'FAILED']))
                        )
                    ).count()
                else:
                    # For individual episode deletions, use original logic
                    remaining_episodes = dbsession.query(Episode).filter(
                        Episode.season_id == season.id,
                        Episode.id != episode.id,
                        Episode.is_deleted == False  # Only count non-deleted episodes
                    ).count()
                
                logger.debug(f"Season {season.id}: {remaining_episodes} episodes remaining after episode {episode.id} deletion (action: {action})", extra={'emoji_type': 'debug'})
                
                if remaining_episodes == 0:
                    # Get season folder path from episode path
                    if episode.dummypath:
                        season_folder = os.path.dirname(episode.dummypath)
                        season_nfo_path = os.path.join(season_folder, "season.nfo")
                        if os.path.exists(season_nfo_path):
                            if delete_nfo_file(season_nfo_path):
                                deleted_files.append(f"season NFO: season.nfo")
                                logger.info(f"🗑️ Last episode in season - deleted season.nfo for season {season.id}", extra={'emoji_type': 'delete'})
                            else:
                                verification_failed.append(f"season NFO: {season_nfo_path}")
                        season.nfo_path = None
                        dbsession.add(series)
                        safe_commit(dbsession, season)
                # Check if we should delete tvshow.nfo (if no seasons with episodes remain)
                # Lock the series row to prevent concurrent checks
                locked_series = dbsession.query(Series).filter(Series.id == series.id).with_for_update().first()
                
                # For series delete operations, check episodes not being deleted in this batch
                if action == 'handle_seriesdelete':
                    # Count seasons that have episodes not being processed for deletion
                    remaining_seasons_with_episodes = dbsession.query(Season).join(Episode).filter(
                        Season.series_id == series.id,
                        Episode.id != episode.id,
                        Episode.is_deleted == False,
                        or_(
                            Episode.action != 'handle_seriesdelete',
                            and_(Episode.action == 'handle_seriesdelete', Episode.status.in_(['DONE', 'CANCELLED', 'FAILED']))
                        )
                    ).distinct().count()
                else:
                    # For individual episode deletions, use original logic
                    remaining_seasons_with_episodes = dbsession.query(Season).join(Episode).filter(
                        Season.series_id == series.id,
                        Episode.id != episode.id,
                        Episode.is_deleted == False  # Only count non-deleted episodes
                    ).count()
                
                logger.debug(f"Series {series.id}: {remaining_seasons_with_episodes} seasons with episodes remaining after episode {episode.id} deletion (action: {action})", extra={'emoji_type': 'debug'})
                
                if remaining_seasons_with_episodes == 0:
                    # Get series folder path from episode path  
                    if episode.dummypath:
                        series_folder = os.path.dirname(os.path.dirname(episode.dummypath))
                        tvshow_nfo_path = os.path.join(series_folder, "tvshow.nfo")
                        if os.path.exists(tvshow_nfo_path):
                            if delete_nfo_file(tvshow_nfo_path):
                                deleted_files.append(f"series NFO: tvshow.nfo")
                                logger.info(f"🗑️ Last episode in series - deleted tvshow.nfo for series {series.id}", extra={'emoji_type': 'delete'})
                            else:
                                verification_failed.append(f"series NFO: {tvshow_nfo_path}")
                        series.nfo_path = None
                        dbsession.add(series)
                        safe_commit(dbsession, series)
                        # Clean up empty series folder structure
                        cleanup_empty_series_structure(series_folder, deleted_files)
                                
        elif model.__name__ == 'Movie':
            # Delete movie NFO file
            movie_nfo_path = getattr(entity, 'nfo_path', None)
            if movie_nfo_path and os.path.exists(movie_nfo_path):
                if delete_nfo_file(movie_nfo_path):
                    deleted_files.append(f"movie NFO: {os.path.basename(movie_nfo_path)}")
                    entity.nfo_path = None
                    dbsession.add(entity)
                    safe_commit(dbsession, entity)
                else:
                    verification_failed.append(f"movie NFO: {movie_nfo_path}")
                    
        elif model.__name__ == 'Series':
            # Delete all NFO files in series hierarchy
            series = entity
            seasons = dbsession.query(Season).filter_by(series_id=series.id).all()
            
            for season in seasons:
                episodes = dbsession.query(Episode).filter_by(season_id=season.id).all()
                for episode in episodes:
                    episode_nfo_path = getattr(episode, 'nfo_path', None)
                    if episode_nfo_path and os.path.exists(episode_nfo_path):
                        if delete_nfo_file(episode_nfo_path):
                            deleted_files.append(f"episode NFO: {os.path.basename(episode_nfo_path)}")
                        else:
                            verification_failed.append(f"episode NFO: {episode_nfo_path}")
                    episode.nfo_path = None
                    dbsession.add(episode)
                    safe_commit(dbsession, episode)
                season.nfo_path = None
                dbsession.add(season)
                safe_commit(dbsession, season)
            # Delete series NFO files (tvshow.nfo) if series has a dummy path
            if series.dummypath and os.path.exists(series.dummypath):
                tvshow_nfo_path = os.path.join(series.dummypath, "tvshow.nfo")
                if os.path.exists(tvshow_nfo_path):
                    if delete_nfo_file(tvshow_nfo_path):
                        deleted_files.append(f"series NFO: tvshow.nfo")
                    else:
                        verification_failed.append(f"series NFO: {tvshow_nfo_path}")
            series.nfo_path = None
            dbsession.add(series)
            safe_commit(dbsession, series)
        # Commit database changes
        if deleted_files:
            logger.info(f"🗑️ NFO DELETION SUMMARY: Removed {len(deleted_files)} NFO files: {', '.join(deleted_files)}", extra={'emoji_type': 'delete'})
        
        # Clean up empty directories after NFO deletion
        cleanup_empty_directories(entity, model, deleted_files)
        
        # Final cleanup check for leftover NFO files (race condition fallback)
        if model.__name__ == 'Episode' and deleted_files:
            # After deleting episode NFO, do a final check for orphaned season/series NFO files
            final_cleanup_orphaned_nfo_files(dbsession, entity, deleted_files)
        
        # Verification check
        if verification_failed:
            logger.error(f"❌ NFO DELETION FAILED: {len(verification_failed)} files could not be deleted: {', '.join(verification_failed)}", extra={'emoji_type': 'error'})
            return False
        
        # Success - all NFO files deleted and verified
        logger.info(f"✅ NFO DELETION COMPLETE: Successfully cleaned up all NFO files for {model.__name__} {ent_id}", extra={'emoji_type': 'success'})
        return True
        
    except Exception as e:
        logger.error(f"❌ NFO DELETION ERROR: Failed to delete NFO for {model.__name__} {ent_id}: {e}", extra={'emoji_type': 'error'})
        dbsession.rollback()
        return False


def get_jellyfin_file_path(item_id: str, user_id: Optional[str] = None) -> str:
    """
    Retrieve the absolute filesystem path for a given Jellyfin item ID.

    You must supply the Jellyfin User ID to fetch disk paths. If not provided,
    it auto-discovers via the Users endpoint.
    """
    if not settings.jellyfin_enabled or not settings.JELLYFIN_URL:
        return ''
    # Discover user ID if not provided
    if not user_id:
        try:
            users_url = build_jellyfin_url("Users")
            resp = session.get(users_url, timeout=5)
            resp.raise_for_status()
            users = resp.json()
            # Find first admin user
            for u in users:
                policy = u.get('Policy', {})
                if policy.get('IsAdministrator'):
                    user_id = u.get('Id')
                    break
            if not user_id:
                logger.error("No admin user found in Jellyfin Users list", extra={'emoji_type': 'error'})
                return ''
        except Exception as ex:
            logger.error(f"Failed to fetch Jellyfin users: {ex}", extra={'emoji_type': 'error'})
            return ''
    # Fetch the disk path for the item under the user context
    try:
        url = build_jellyfin_url(f"Users/{user_id}/Items/{item_id}?fields=Path")
        resp = session.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        path = data.get('Path', '')
        if path:
            logger.info(f"Retrieved path for item {item_id}: {path}", extra={'emoji_type': 'info'})
            return path
        logger.warning(f"Jellyfin item {item_id} did not include a Path field", extra={'emoji_type': 'warning'})
    except Exception as ex:
        logger.error(f"Failed to get file path for {item_id}: {ex}", extra={'emoji_type': 'error'})

    return ''

def test_jellyfin_connection() -> bool:
    """Test connectivity to the Jellyfin server by fetching public system info."""
    url = build_jellyfin_url('System/Info/Public')
    logger.debug("Testing Jellyfin connection", extra={'emoji_type': 'debug'})
    try:
        resp = session.get(url)
        resp.raise_for_status()
        logger.info("Jellyfin connection test successful", extra={'emoji_type': 'success'})
        return True
    except Exception as ex:
        logger.error(f"Jellyfin connection test failed: {ex}", extra={'emoji_type': 'error'})
        return False

def test_jellyfin_endpoints():
    """Test key Jellyfin API endpoints needed for operation."""
    logger.info("Testing Jellyfin API endpoints", extra={'emoji_type': 'processing'})
    try:
        # /Users endpoint
        url = build_jellyfin_url("Users")
        resp = session.get(url, timeout=5)
        resp.raise_for_status()
        logger.info("Jellyfin /Users endpoint accessible", extra={'emoji_type': 'success'})
        # /Items endpoint
        url = build_jellyfin_url("Items")
        resp = session.get(url, timeout=5)
        resp.raise_for_status()
        logger.info("Jellyfin /Items endpoint accessible", extra={'emoji_type': 'success'})
    except Exception as ex:
        logger.error(f"Jellyfin API endpoint test failed: {ex}", extra={'emoji_type': 'error'})

# Run connection test at import time (same pattern as Plex)
if getattr(settings, "jellyfin_enabled", False):
    try:
        if test_jellyfin_connection():
            logger.info("Connected to Jellyfin server", extra={'emoji_type': 'success'})
            test_jellyfin_endpoints()
        else:
            logger.error("Failed to connect to Jellyfin server", extra={'emoji_type': 'error'})
    except Exception as ex:
        logger.error(f"Failed to connect to Jellyfin server: {ex}", extra={'emoji_type': 'error'})

def verify_title_status_jellyfin(
    dbsession: Session,
    ent_id: int,
    model: Type,
    action: str
) -> bool:
    """
    Verify that placeholder_status is properly embedded in jellyfin_title and jellyfin_overview.
    
    For Movies/Episodes: checks that if placeholder_status is not empty/null, it appears in square 
    brackets at the beginning of jellyfin_title and jellyfin_overview (e.g., "[Monitored] Title").
    
    If the status is missing from either field, resets the SubFlow back to refresh_jellyfin_dummy 
    step so it rescans and updates the DB properly.
    
    Returns:
        bool: True if status is properly embedded or not needed, False to retry from refresh step
    """
    logger.info(f"Starting verify_title_status_jellyfin for {model.__name__} {ent_id}", extra={'emoji_type': 'processing'})
    
    # Handle both Movies and Episodes
    if model == Movie:
        entity = dbsession.query(Movie).get(ent_id)
        entity_type = 'Movie'
        subflow_filter = {'movie_id': ent_id}
    elif model == Episode:
        entity = dbsession.query(Episode).get(ent_id)
        entity_type = 'Episode'
        subflow_filter = {'episode_id': ent_id}
    else:
        logger.debug(f"Skipping verify_title_status_jellyfin for unsupported model {model.__name__}", extra={'emoji_type': 'debug'})
        return True
    
    if not entity:
        logger.warning(f"{entity_type} {ent_id} not found in database", extra={'emoji_type': 'warning'})
        return False

    if 'import' in action.lower() or 'upgrade' in action.lower():
        if entity.placeholder_status:
            logger.warning(
                f"Detected leftover placeholder_status '{entity.placeholder_status}' during {action}. Resetting to None.", 
                extra={'emoji_type': 'repair'}
            )
            entity.placeholder_status = None
            # We need to commit this change so it persists
            try:
                dbsession.add(entity)
                dbsession.commit()
            except Exception as e:
                logger.error(f"Failed to reset placeholder_status: {e}", extra={'emoji_type': 'error'})
                dbsession.rollback()

    title_ok = False
    expected_prefix = f"[{entity.placeholder_status}]"
    # If no placeholder_status or it's empty, nothing to verify
    if not entity.placeholder_status or entity.placeholder_status.strip() == '':
        logger.debug(f"{entity_type} {ent_id} has no placeholder_status, skipping verification", extra={'emoji_type': 'debug'})
        if not entity.jellyfin_title.startswith('['):
            return True
        else:
            title_ok = False
            logger.debug(f"{entity_type} {ent_id} jellyfin_title has incorrect status prefix", extra={'emoji_type': 'debug'})

    else:
        if entity.jellyfin_title and entity.jellyfin_title.startswith(expected_prefix):
            title_ok = True
            logger.debug(f"{entity_type} {ent_id} jellyfin_title has correct status prefix", extra={'emoji_type': 'success'})
        else:
            logger.warning(
                f"{entity_type} {ent_id} jellyfin_title missing status prefix. "
                f"Expected: '{expected_prefix}...', Got: '{entity.jellyfin_title}'",
                extra={'emoji_type': 'warning'}
            )
        
        # Check jellyfin_overview
        overview_ok = False
        if entity.jellyfin_overview and entity.jellyfin_overview.startswith(expected_prefix):
            overview_ok = True
            logger.debug(f"{entity_type} {ent_id} jellyfin_overview has correct status prefix", extra={'emoji_type': 'success'})
        else:
            logger.warning(
                f"{entity_type} {ent_id} jellyfin_overview missing status prefix. "
                f"Expected: '{expected_prefix}...', Got: '{entity.jellyfin_overview}'",
                extra={'emoji_type': 'warning'}
            )
        
    # If both are OK, we're done
    if title_ok and overview_ok:
        logger.info(f"{entity_type} {ent_id} has properly embedded status in Jellyfin metadata", extra={'emoji_type': 'success'})
        return True
    
    # Otherwise, need to reset to refresh_jellyfin_dummy step
    logger.warning(
        f"{entity_type} {ent_id} needs status re-embedding. Resetting SubFlow to refresh_jellyfin_dummy step",
        extra={'emoji_type': 'repair'}
    )
    
    # Find ANY SubFlow for this entity with the current action that contains refresh_jellyfin_dummy
    # (might be a different SubFlow from the one currently executing verify_title_status_jellyfin)
    all_subflows = dbsession.query(SubFlow).filter_by(
        action=action,
        **subflow_filter
    ).filter(
        SubFlow.status.in_(['QUEUED', 'PENDING', 'DONE'])
    ).all()
    
    subflow_to_reset = None
    refresh_idx = None
    
    for sf in all_subflows:
        steps = sf.steps.split(',')
        if 'refresh_jellyfin_dummy' in steps and 'import' not in action:
            try:
                refresh_idx = steps.index('refresh_jellyfin_dummy')
                # Prioritize create_jellyfin_nfo if it exists (to regenerate broken NFOs)
                if 'create_jellyfin_nfo' in steps:
                    refresh_idx = steps.index('create_jellyfin_nfo')
                    logger.debug(f"Found create_jellyfin_nfo at index {refresh_idx}, resetting to that", extra={'emoji_type': 'debug'})

                subflow_to_reset = sf
                logger.debug(
                    f"Found SubFlow {sf.id} with target reset step at index {refresh_idx}",
                    extra={'emoji_type': 'debug'}
                )
                break
            except ValueError:
                continue
        elif 'refresh_jellyfin_arr_path' in steps and 'import' in action:
            try:
                refresh_idx = steps.index('refresh_jellyfin_arr_path')
                # Prioritize create_jellyfin_nfo if it exists (to regenerate broken NFOs)
                if 'create_jellyfin_nfo' in steps:
                    refresh_idx = steps.index('create_jellyfin_nfo')
                    logger.debug(f"Found create_jellyfin_nfo at index {refresh_idx}, resetting to that", extra={'emoji_type': 'debug'})
                
                subflow_to_reset = sf
                logger.debug(
                    f"Found SubFlow {sf.id} with target reset step at index {refresh_idx}",
                    extra={'emoji_type': 'debug'}
                )
                break
            except ValueError:
                continue

    if not subflow_to_reset:
        logger.error(
            f"No SubFlow found for {entity_type} {ent_id} action {action} containing 'refresh_jellyfin_dummy' step",
            extra={'emoji_type': 'error'}
        )
        return False
    
    logger.info(
        f"Resetting SubFlow {subflow_to_reset.id} from step {subflow_to_reset.step_index} to step {refresh_idx} (refresh_jellyfin_dummy)",
        extra={'emoji_type': 'refresh'}
    )
    subflow_to_reset.step_index = refresh_idx
    subflow_to_reset.status = 'PENDING'
    subflow_to_reset.retry_count = 0
    subflow_to_reset.barrier_released = False  # Reset barrier flag too
    dbsession.add(subflow_to_reset)
    safe_commit(dbsession, subflow_to_reset)
    logger.info(f"SubFlow {subflow_to_reset.id} reset to refresh_jellyfin_dummy for re-scanning", extra={'emoji_type': 'success'})
    return False  # Return False so current step doesn't advance



