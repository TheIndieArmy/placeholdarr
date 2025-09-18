import os
import re
import time
import inspect
from typing import List, Optional, Dict, Any, Callable, Tuple, Union, Type
from urllib.parse import quote, quote_plus
import requests
from plexapi.server import PlexServer
from core.config import settings
from core.logger import logger
from services.utils import strip_status_markers
from services.postgres.models import Movie, Series, Season, Episode, SubFlow
from sqlalchemy.orm import Session

# Initialize the Plex server connection
plex = None
if getattr(settings, "plex_enabled", False):
    try:
        plex = PlexServer(settings.PLEX_URL, settings.PLEX_TOKEN)
        logger.info("Connected to Plex server", extra={'emoji_type': 'success'})
    except Exception as e:
        logger.error(f"Failed to connect to Plex server: {e}", extra={'emoji_type': 'error'})
        plex = None


def build_plex_url(path: str) -> str:
    """
    Build a complete Plex API URL from an endpoint path.

    Args:
        path (str): API endpoint (e.g., 'library/sections').

    Returns:
        str: Full URL to query.
    """
    if not getattr(settings, "plex_enabled", False) or not settings.PLEX_URL:
        return ""
    
    base = settings.PLEX_URL.rstrip('/')
    clean_path = path.strip('/')
    
    url = f"{base}/{clean_path}"
    logger.debug(f"Built Plex URL: {url}", extra={'emoji_type': 'debug'})
    return url


def refresh_plex_item(path: Union[str, List[str]], update_type: str = 'Created') -> bool:
    """
    Trigger scan/update for a file, directory—or multiple paths.

    Args:
        path (str or list): Absolute filesystem path(s) to file or folder.
        update_type (str): One of 'Created', 'Changed', 'Deleted', or 'None'.

    Returns:
        bool: True if Plex accepted request, False otherwise.
    """
    if not getattr(settings, "plex_enabled", False) or not settings.PLEX_URL or not settings.PLEX_TOKEN:
        return False

    # Normalize input to list of targets
    if isinstance(path, str):
        paths = [path]
    else:
        paths = list(path)

    success = True
    for p in paths:
        target = os.path.dirname(p) if os.path.isfile(p) else p
        
        # Determine section ID from path
        section_id = None
        if any(target.startswith(folder) for folder in [settings.MOVIE_LIBRARY_FOLDER, settings.MOVIE_LIBRARY_4K_FOLDER] if folder):
            section_id = settings.PLEX_MOVIE_SECTION_ID
        elif any(target.startswith(folder) for folder in [settings.TV_LIBRARY_FOLDER, settings.TV_LIBRARY_4K_FOLDER] if folder):
            section_id = settings.PLEX_TV_SECTION_ID
        
        if not section_id:
            logger.error(f"Cannot determine section ID for path: {target}", extra={'emoji_type': 'error'})
            success = False
            continue
            
        url = build_plex_url(f"library/sections/{section_id}/refresh?path={quote(target)}")
        
        try:
            response = requests.get(url, headers={'X-Plex-Token': settings.PLEX_TOKEN})
            response.raise_for_status()
            logger.info(f"Triggered scan for: {target} ({update_type})", extra={'emoji_type': 'refresh'})
        except Exception as ex:
            logger.error(f"Scan failed for {target}: {ex}", extra={'emoji_type': 'error'})
            success = False
    
    return success


def refresh_plex_library(library_id: str,
                         recursive: bool = True,
                         image_mode: str = 'Default',
                         metadata_mode: str = 'Default',
                         replace_images: bool = False,
                         regenerate_trickplay: bool = False,
                         replace_metadata: bool = False) -> bool:
    """
    Refresh an entire Plex library (section) by its ID.
    """
    if not getattr(settings, "plex_enabled", False) or not settings.PLEX_URL or not settings.PLEX_TOKEN:
        return False
    
    try:
        # In Plex, we just need the section ID and whether it's recursive
        url = build_plex_url(f"library/sections/{library_id}/refresh")
        if recursive:
            url += "?deep=1"
            
        response = requests.get(url, headers={'X-Plex-Token': settings.PLEX_TOKEN})
        response.raise_for_status()
        logger.info(f"Library {library_id} refresh queued", extra={'emoji_type': 'refresh'})
        return True
    except Exception as ex:
        logger.error(f"Library refresh failed: {ex}", extra={'emoji_type': 'error'})
        return False


def _prepend_status_to_summary(summary, status):
    """Prepend status to summary, replacing any previous status marker."""
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
        logger.debug(f"🔁 retry_call attempt {attempt}, result={result!r}")

        if success_condition(result):
            logger.debug(f"✅ retry_call succeeded on attempt {attempt}")
            return result

        if is_net_error:
            # exponential backoff for network errors
            logger.warning(
                f"⚠️ network error on attempt {attempt}, waiting {interval}s"
            )
            time.sleep(interval)
            deadline += interval  # extend window
            interval *= 2
        else:
            # fixed interval for non-network failures
            logger.warning(
                f"⚠️ attempt {attempt} failed condition, waiting {retry_interval}s"
            )
            success_outcome = success_condition(result)
            logger.debug(f"🔍 success_condition({result!r}) -> {success_outcome!r}")
            try:
                source = inspect.getsource(success_condition)
            except Exception:
                source = '<source unavailable>'
            logger.debug(f"🖋 success_condition source:\n{source}")

            time.sleep(retry_interval)

    logger.warning(
        f"⚠️ retry_call timed out after {attempt} attempts "
        f"(total window={deadline}s), last_result={last_result!r}"
    )
    return last_result


def refresh_plex_dummy(dbsession: Session, ent_id: int, model: type, action: str) -> bool:
    """
    Bulk-refresh all pending 'refresh_plex_dummy' subflows in one call,
    using step_index and steps to find exact matches.

    Args:
        dbsession: SQLAlchemy session.
        ent_id, model: provided by scheduler (ignored here for bulk logic).
    Returns:
        True if any refresh occurred, False otherwise.
    """
    logger.info("Starting bulk refresh for 'refresh_plex_dummy' subflows", extra={'emoji_type': 'processing'})
    
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
        if sf.step_index < len(steps) and steps[sf.step_index] == 'refresh_plex_dummy':
            matching.append(sf)

    logger.info(f"Found {len(matching)} subflows matching 'refresh_plex_dummy' step", extra={'emoji_type': 'info'})

    if not matching:
        logger.debug("No matching subflows found for dummy refresh", extra={'emoji_type': 'debug'})
        return True

    # 2) Collect unique paths for all matching subflows
    paths = set()
    for sf in matching:
        if sf.episode_id:
            obj = dbsession.query(Episode).get(sf.episode_id)
        else:
            obj = dbsession.query(Movie).get(sf.movie_id)
        if obj and obj.dummypath:
            paths.add(obj.dummypath)

    logger.info(f"Collected {len(paths)} unique dummy paths to refresh", extra={'emoji_type': 'dummy'})

    if not paths:
        # Check if this is a delete action - if so, no files is expected and OK
        sf_action = matching[0].action.lower()
        if 'delete' in sf_action:
            logger.info("No dummy paths found for delete action - this is expected if files were already deleted", extra={'emoji_type': 'info'})
            # Return success for delete actions with no paths - let scheduler handle advancement
            logger.info(f"Successfully processed {len(matching)} delete action subflows (no dummy paths to refresh)", extra={'emoji_type': 'success'})
            return True
        else:
            logger.warning("No dummy paths found to refresh", extra={'emoji_type': 'warning'})
            return False

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
    refresh_plex_item(list(paths), update_type)

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
    logger.info(f"Successfully refreshed {len(matching)} subflows for 'refresh_plex_dummy' step", extra={'emoji_type': 'success'})
    return True


def refresh_plex_arr_path(dbsession: Session, ent_id: int, model: type, action:str) -> bool:
    """
    Bulk-refresh all pending 'refresh_plex_arr_path' subflows in one call,
    using step_index and steps to find exact matches.

    Args:
        dbsession: SQLAlchemy session.
        ent_id, model: provided by scheduler (ignored here for bulk logic).
    Returns:
        True if any refresh occurred, False otherwise.
    """
    logger.info("Starting bulk refresh for 'refresh_plex_arr_path' subflows", extra={'emoji_type': 'processing'})
    
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
        if sf.step_index < len(steps) and steps[sf.step_index] == 'refresh_plex_arr_path':
            matching.append(sf)

    logger.info(f"Found {len(matching)} subflows matching 'refresh_plex_arr_path' step", extra={'emoji_type': 'info'})

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

    logger.info(f"Collected {len(paths)} unique file paths to refresh", extra={'emoji_type': 'file'})

    if not paths:
        # Check if this is a delete action - if so, no files is expected and OK
        sf_action = matching[0].action.lower()
        if 'delete' in sf_action:
            logger.info("No file paths found for delete action - this is expected if files were already deleted", extra={'emoji_type': 'info'})
            # Return success for delete actions with no paths - let scheduler handle advancement
            logger.info(f"Successfully processed {len(matching)} delete action subflows (no file paths to refresh)", extra={'emoji_type': 'success'})
            return True
        else:
            logger.warning("No file paths found to refresh", extra={'emoji_type': 'warning'})
            return False

    # 3) Determine update type from SubFlow action (not parent entity action)
    sf_action = matching[0].action.lower()
    if any(k in sf_action for k in ('add', 'import')):
        update_type = 'Created'
    elif any(k in sf_action for k in ('delete',)):
        update_type = 'Deleted'
    else:
        update_type = 'Updated'

    logger.info(f"Determined update type as '{update_type}' based on SubFlow action '{matching[0].action}'", extra={'emoji_type': 'update'})

    # 4) Bulk refresh call
    refresh_plex_item(list(paths), update_type)

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
    logger.info(f"Successfully refreshed {len(matching)} subflows for arr path refresh", extra={'emoji_type': 'success'})

    return True


def save_plex_arr_id(dbsession, model, ent_id, match: dict):
    """
    Persist the Plex item ID along with its matched title and overview.

    Args:
        dbsession: SQLAlchemy session
        model: one of (Movie, Series, Season, Episode)
        ent_id: primary key of the model instance
        match: dict or PlexAPI object containing at least 'Id'/'ratingKey', 'Name'/'title', and 'Overview'/'summary'
    Returns:
        plex_id
    """
    obj = dbsession.query(model).get(ent_id)
    # ID field mapping
    id_field = {
        Movie:   'plex_id',
        Series:  'plex_id',
        Season:  'plex_id',
        Episode: 'plex_id'
    }[model]
    
    # Extract ID from match (might be a dict or PlexAPI object)
    plex_id = match.get('Id') if isinstance(match, dict) else (
        match.ratingKey if hasattr(match, 'ratingKey') else None
    )
    
    # Update ID
    if getattr(obj, id_field) != plex_id:
        setattr(obj, id_field, plex_id)
        dbsession.add(obj)
        dbsession.commit()
    return plex_id


def save_plex_dummy_id(dbsession, model, ent_id, match: dict):
    """
    Persist the Plex item ID along with its matched title and overview.

    Args:
        dbsession: SQLAlchemy session
        model: one of (Movie, Series, Season, Episode)
        ent_id: primary key of the model instance
        match: dict or PlexAPI object containing at least 'Id'/'ratingKey', 'Name'/'title', and 'Overview'/'summary'
    Returns:
        plex_id
    """
    obj = dbsession.query(model).get(ent_id)
    # ID field mapping
    id_field = {
        Movie:   'plex_dummy_id',
        Series:  'plex_dummy_id',
        Season:  'plex_dummy_id',
        Episode: 'plex_dummy_id'
    }[model]
    # Title & overview field mapping (ensure these columns exist)
    title_field = {
        Movie:   'plex_title',
        Series:  'plex_title',
        Season:  'plex_title',
        Episode: 'plex_title'
    }[model]
    overview_field = {
        Movie:   'plex_overview',
        Series:  'plex_overview',
        Season:  'plex_overview',
        Episode: 'plex_overview'
    }[model]
    
    changed = False
    
    # Extract values (handling both dict and PlexAPI objects)
    if isinstance(match, dict):
        plex_id = match.get('Id')
        name = match.get('Name')
        overview = match.get('Overview')
    else:
        plex_id = match.ratingKey if hasattr(match, 'ratingKey') else None
        name = match.title if hasattr(match, 'title') else None
        overview = match.summary if hasattr(match, 'summary') else None
    
    # Update ID
    if plex_id and getattr(obj, id_field) != plex_id:
        setattr(obj, id_field, plex_id)
        changed = True
    # Update title
    if name and getattr(obj, title_field) != name:
        setattr(obj, title_field, name)
        changed = True
    # Update overview
    if overview and getattr(obj, overview_field) != overview:
        setattr(obj, overview_field, overview)
        changed = True

    if changed:
        dbsession.add(obj)
        dbsession.commit()
    return plex_id


def verify_arr_scan_plex(dbsession: Session, ent_id: int, model: Type, action) -> bool:
    """
    Process a "verify_arr_scan_plex" step for one entity:

    Movie:
      - Search Plex by title, match playback path to filepath
      - Save Plex movie ID, mark its SubFlow DONE

    Episode:
      - Determine series from episode, find by TVDB ID
      - Cache series and season IDs
      - Fetch all queued Episode SubFlows for that series
      - For each, match filepath to playback path, save episode ID, mark SubFlow DONE

    Returns True if Episode and any SubFlow was updated; False otherwise.
    """
    if not plex:
        logger.error("Plex server not available", extra={'emoji_type': 'error'})
        return False
        
    # MOVIE CASE
    if model is Movie:
        m = dbsession.query(Movie).get(ent_id)
        if not m or not m.filepath:
            return False
            
        # Try to find the movie by TMDB ID
        movie_section = plex.library.sectionByID(settings.PLEX_MOVIE_SECTION_ID)
        movie = None
        
        for plexMovie in movie_section.all():
            # Try to match by TMDB ID in GUID
            for guid in plexMovie.guids:
                if f'tmdb://{m.tmdbid}' in guid.id:
                    movie = plexMovie
                    break
                    
            # Try to match by filepath
            if not movie and hasattr(plexMovie, 'locations'):
                for location in plexMovie.locations:
                    if location == m.filepath:
                        movie = plexMovie
                        break
                        
            if movie:
                break
                
        if movie:
            # Create dict representation for save function
            match_dict = {
                'Id': movie.ratingKey,
                'Name': movie.title,
                'Overview': movie.summary if hasattr(movie, 'summary') else ''
            }
            save_plex_arr_id(dbsession, Movie, m.id, match_dict)
            return True
            
        return False

    # EPISODE CASE
    ep = dbsession.query(Episode).get(ent_id)
    if not ep or not ep.filepath:
        return False
        
    seas = dbsession.query(Season).get(ep.season_id)
    series = dbsession.query(Series).get(seas.series_id)
    
    # 1) Find series by TVDB ID
    tv_section = plex.library.sectionByID(settings.PLEX_TV_SECTION_ID)
    plex_series = None
    
    for s in tv_section.all():
        for guid in s.guids:
            if f'tvdb://{series.tvdbid}' in guid.id:
                plex_series = s
                break
        if plex_series:
            break
            
    if not plex_series:
        logger.warning(f"⚠️ Series with TVDB ID {series.tvdbid} not found in Plex", extra={'emoji_type': 'warning'})
        return False
        
    # Save series ID
    series_match = {
        'Id': plex_series.ratingKey,
        'Name': plex_series.title,
        'Overview': plex_series.summary if hasattr(plex_series, 'summary') else ''
    }
    save_plex_arr_id(dbsession, Series, series.id, series_match)
    
    # 2) Find season
    plex_season = None
    for s in plex_series.seasons():
        if s.index == seas.season_number:
            plex_season = s
            break
            
    if not plex_season:
        logger.warning(f"⚠️ Season {seas.season_number} not found for series {plex_series.title}", extra={'emoji_type': 'warning'})
        return False
        
    # Save season ID
    season_match = {
        'Id': plex_season.ratingKey,
        'Name': plex_season.title,
        'Overview': plex_season.summary if hasattr(plex_season, 'summary') else ''
    }
    save_plex_arr_id(dbsession, Season, seas.id, season_match)
    
    # 3) Process queued episode SubFlows for this series
    ep_ids = [e.id for e in dbsession.query(Episode).join(Season).filter(Season.series_id == series.id)]
    queued = dbsession.query(SubFlow).filter(
        SubFlow.status == 'QUEUED',
        SubFlow.action == action,
        SubFlow.episode_id.in_(ep_ids)
    ).all()
    
    if not queued:
        return False
        
    matched = False
    
    # 4) Match each queued episode
    for sf in queued:
        steps = sf.steps.split(',')
        if sf.step_index < len(steps) and steps[sf.step_index] != 'verify_arr_scan_plex':
            continue
            
        ep2 = dbsession.query(Episode).get(sf.episode_id)
        if not ep2 or ep2.season_number != seas.season_number:
            continue
            
        plex_episode = None
        for e in plex_season.episodes():
            if e.index == ep2.episode_number:
                # Check filepath match if possible
                if hasattr(e, 'locations') and e.locations and e.locations[0] == ep2.filepath:
                    plex_episode = e
                    break
                # Otherwise just match by number
                plex_episode = e
                
        if plex_episode:
            episode_match = {
                'Id': plex_episode.ratingKey,
                'Name': plex_episode.title,
                'Overview': plex_episode.summary if hasattr(plex_episode, 'summary') else ''
            }
            save_plex_arr_id(dbsession, Episode, ep2.id, episode_match)
            sf.status = 'DONE'
            dbsession.add(sf)
            if ep2.id == ent_id:
                matched = True
    
    if not matched:
        logger.warning(f"⚠️ No matching Plex episode found for {ep.filepath}", extra={'emoji_type': 'warning'})
        
    dbsession.commit()
    return matched


def verify_dummy_scan_plex(dbsession: Session, ent_id: int, model: Type, action) -> bool:
    """
    Process a "verify_dummy_scan_plex" step for one entity:

    Movie:
      - Search Plex by title, match playback path to dummypath
      - Save Plex movie ID, mark its SubFlow DONE

    Episode:
      - Determine series from episode, find by TVDB ID
      - Cache series and season IDs
      - Fetch all queued Episode SubFlows for that series
      - For each, match dummypath to playback path, save episode ID, mark SubFlow DONE

    Returns True if Episode and any SubFlow was updated; False otherwise.
    """
    if not plex:
        logger.error("Plex server not available", extra={'emoji_type': 'error'})
        return False
        
    # MOVIE CASE
    if model is Movie:
        m = dbsession.query(Movie).get(ent_id)
        if not m or not m.dummypath:
            return False
            
        # Try to find the movie by TMDB ID
        movie_section = plex.library.sectionByID(settings.PLEX_MOVIE_SECTION_ID)
        movie = None
        
        for plexMovie in movie_section.all():
            # Try to match by TMDB ID in GUID
            for guid in plexMovie.guids:
                if f'tmdb://{m.tmdbid}' in guid.id:
                    movie = plexMovie
                    break
                    
            # Try to match by dummypath
            if not movie and hasattr(plexMovie, 'locations'):
                for location in plexMovie.locations:
                    if location == m.dummypath:
                        movie = plexMovie
                        break
                        
            if movie:
                break
                
        if movie:
            # Create dict representation for save function
            match_dict = {
                'Id': movie.ratingKey,
                'Name': movie.title,
                'Overview': movie.summary if hasattr(movie, 'summary') else ''
            }
            save_plex_dummy_id(dbsession, Movie, m.id, match_dict)
            return True
            
        return False

    # EPISODE CASE
    ep = dbsession.query(Episode).get(ent_id)
    if not ep or not ep.dummypath:
        return False
        
    seas = dbsession.query(Season).get(ep.season_id)
    series = dbsession.query(Series).get(seas.series_id)
    
    # 1) Find series by TVDB ID
    tv_section = plex.library.sectionByID(settings.PLEX_TV_SECTION_ID)
    plex_series = None
    
    for s in tv_section.all():
        for guid in s.guids:
            if f'tvdb://{series.tvdbid}' in guid.id:
                plex_series = s
                break
        if plex_series:
            break
            
    if not plex_series:
        logger.warning(f"⚠️ Series with TVDB ID {series.tvdbid} not found in Plex", extra={'emoji_type': 'warning'})
        return False
        
    # Save series ID
    series_match = {
        'Id': plex_series.ratingKey,
        'Name': plex_series.title,
        'Overview': plex_series.summary if hasattr(plex_series, 'summary') else ''
    }
    save_plex_dummy_id(dbsession, Series, series.id, series_match)
    
    # 2) Find season
    plex_season = None
    for s in plex_series.seasons():
        if s.index == seas.season_number:
            plex_season = s
            break
            
    if not plex_season:
        logger.warning(f"⚠️ Season {seas.season_number} not found for series {plex_series.title}", extra={'emoji_type': 'warning'})
        return False
        
    # Save season ID
    season_match = {
        'Id': plex_season.ratingKey,
        'Name': plex_season.title,
        'Overview': plex_season.summary if hasattr(plex_season, 'summary') else ''
    }
    save_plex_dummy_id(dbsession, Season, seas.id, season_match)
    
    # 3) Process queued episode SubFlows for this series
    ep_ids = [e.id for e in dbsession.query(Episode).join(Season).filter(Season.series_id == series.id)]
    queued = dbsession.query(SubFlow).filter(
        SubFlow.status == 'QUEUED',
        SubFlow.action == action,
        SubFlow.episode_id.in_(ep_ids)
    ).all()
    
    if not queued:
        return False
        
    matched = False
    
    # 4) Match each queued episode
    for sf in queued:
        steps = sf.steps.split(',')
        if sf.step_index < len(steps) and steps[sf.step_index] != 'verify_dummy_scan_plex':
            continue
            
        ep2 = dbsession.query(Episode).get(sf.episode_id)
        if not ep2 or ep2.season_number != seas.season_number:
            continue
            
        plex_episode = None
        for e in plex_season.episodes():
            if e.index == ep2.episode_number:
                # Check dummypath match if possible
                if hasattr(e, 'locations') and e.locations and e.locations[0] == ep2.dummypath:
                    plex_episode = e
                    break
                # Otherwise just match by number
                plex_episode = e
                
        if plex_episode:
            episode_match = {
                'Id': plex_episode.ratingKey,
                'Name': plex_episode.title,
                'Overview': plex_episode.summary if hasattr(plex_episode, 'summary') else ''
            }
            save_plex_dummy_id(dbsession, Episode, ep2.id, episode_match)
            sf.status = 'DONE'
            dbsession.add(sf)
            if ep2.id == ent_id:
                matched = True
    
    if not matched:
        logger.warning(f"⚠️ No matching Plex episode found for {ep.dummypath}", extra={'emoji_type': 'warning'})
        
    dbsession.commit()
    return matched


def get_admin_user():
    """
    Get the first admin user ID from Plex.
    In Plex, the server owner is always an admin.
    """
    if not plex:
        logger.error("Plex server not available", extra={'emoji_type': 'error'})
        return None
    
    try:
        for user in plex.myPlexAccount().users():
            if user.admin:
                return user.id
        # Return the owner as a fallback
        return plex.myPlexAccount().id
    except Exception as ex:
        logger.error(f"❌ Cannot find admin user: {ex}", extra={"emoji_type": "error"})
        return None


def update_plex_title_status(
    dbsession: Session,
    ent_id: int,
    model: Type,
    action: str = "status_update",
    retry_interval: int = 30,
    retry_timeout: int = 600
) -> bool:
    """
    Update title & summary in Plex for a Movie or Episode hierarchy.

    Simplified lookup: for placeholder updates we scan the configured Plex
    section's items (section.all()) and match ratingKey against the
    stored plex_dummy_id (or plex_id when not a placeholder). This mirrors
    the simplest successful method used by the standalone test script.
    """
    if not plex:
        logger.error("Plex server not available", extra={'emoji_type': 'error'})
        return False

    updated_any = False

    try:
        # MOVIE
        if model is Movie:
            movie = dbsession.query(Movie).get(ent_id)
            if not movie:
                return False

            # For placeholder updates we require the dummy id; otherwise use plex_id
            if movie.placeholder_status:
                target_id = getattr(movie, 'plex_dummy_id', None)
                if not target_id:
                    logger.warning(f"⚠️ Movie {movie.id} has placeholder_status but no plex_dummy_id", extra={'emoji_type': 'warning'})
                    return False
            else:
                target_id = getattr(movie, 'plex_id', None)
                if not target_id:
                    logger.warning(f"⚠️ Movie {movie.id} has no plex_id to update", extra={'emoji_type': 'warning'})
                    return False

            # Scan the configured movie section for the matching ratingKey
            try:
                movie_section = plex.library.sectionByID(settings.PLEX_MOVIE_SECTION_ID)
            except Exception as ex:
                logger.error(f"❌ Cannot access movie section {settings.PLEX_MOVIE_SECTION_ID}: {ex}", extra={'emoji_type': 'error'})
                return False

            movie_obj = None
            try:
                for m in movie_section.all():
                    try:
                        rk = getattr(m, 'ratingKey', None)
                        if rk is None:
                            continue
                        if str(rk) == str(target_id):
                            movie_obj = m
                            break
                    except Exception:
                        continue
            except Exception as ex:
                logger.error(f"❌ Error scanning movie section: {ex}", extra={'emoji_type': 'error'})
                return False

            if not movie_obj:
                logger.error(f"❌ Movie with ID {target_id} not found by scanning movie section", extra={"emoji_type": "error"})
                return False

            current_summary = movie_obj.summary if hasattr(movie_obj, 'summary') else ""
            new_summary = _prepend_status_to_summary(current_summary, movie.placeholder_status)

            def do_update():
                movie_obj.editSummary(new_summary)
                movie_obj.reload()
                return True

            success = retry_call(
                func=do_update,
                on_error=lambda ex: logger.error(f"❌ Failed to update movie {target_id}: {ex}"),
                retry_interval=retry_interval,
                retry_timeout=retry_timeout,
                success_condition=lambda res: res is True
            )

            if success:
                updated_any = True

            return updated_any

        # EPISODE (and parent series/season) case
        ep = dbsession.query(Episode).get(ent_id)
        if not ep:
            return False

        seas = dbsession.query(Season).get(ep.season_id)
        series = dbsession.query(Series).get(seas.series_id) if seas else None

        try:
            tv_section = plex.library.sectionByID(settings.PLEX_TV_SECTION_ID)
        except Exception as ex:
            logger.error(f"❌ Cannot access TV section {settings.PLEX_TV_SECTION_ID}: {ex}", extra={'emoji_type': 'error'})
            return False

        # Update Series (scan by ratingKey when placeholder)
        if series and series.placeholder_status:
            series_target = getattr(series, 'plex_dummy_id', None)
            if not series_target:
                logger.warning(f"⚠️ Series {series.id} has placeholder_status but no plex_dummy_id", extra={'emoji_type': 'warning'})
            else:
                series_obj = None
                try:
                    for s in tv_section.all():
                        try:
                            if str(getattr(s, 'ratingKey', None)) == str(series_target):
                                series_obj = s
                                break
                        except Exception:
                            continue
                except Exception as ex:
                    logger.error(f"❌ Error scanning TV section for series: {ex}", extra={'emoji_type': 'error'})
                    series_obj = None

                if series_obj:
                    current_summary = series_obj.summary if hasattr(series_obj, 'summary') else ""
                    new_summary = _prepend_status_to_summary(current_summary, series.placeholder_status)

                    def update_series():
                        series_obj.editSummary(new_summary)
                        series_obj.reload()
                        return True

                    success = retry_call(
                        func=update_series,
                        on_error=lambda ex: logger.error(f"❌ Failed to update series {series_target}: {ex}"),
                        retry_interval=retry_interval,
                        retry_timeout=retry_timeout,
                        success_condition=lambda res: res is True
                    )

                    if success:
                        updated_any = True

        # Update Season (scan by ratingKey when placeholder)
        if seas and seas.placeholder_status:
            season_target = getattr(seas, 'plex_dummy_id', None)
            if not season_target:
                logger.warning(f"⚠️ Season {seas.id} has placeholder_status but no plex_dummy_id", extra={'emoji_type': 'warning'})
            else:
                season_obj = None
                try:
                    for s in tv_section.all():
                        try:
                            if str(getattr(s, 'ratingKey', None)) == str(season_target):
                                season_obj = s
                                break
                        except Exception:
                            continue
                except Exception as ex:
                    logger.error(f"❌ Error scanning TV section for season: {ex}", extra={'emoji_type': 'error'})
                    season_obj = None

                if season_obj:
                    current_summary = season_obj.summary if hasattr(season_obj, 'summary') else ""
                    new_summary = _prepend_status_to_summary(current_summary, seas.placeholder_status)

                    def update_season():
                        season_obj.editSummary(new_summary)
                        season_obj.reload()
                        return True

                    success = retry_call(
                        func=update_season,
                        on_error=lambda ex: logger.error(f"❌ Failed to update season {season_target}: {ex}"),
                        retry_interval=retry_interval,
                        retry_timeout=retry_timeout,
                        success_condition=lambda res: res is True
                    )

                    if success:
                        updated_any = True

        # Update Episode (scan by ratingKey for dummy item)
        if ep.placeholder_status:
            episode_target = getattr(ep, 'plex_dummy_id', None)
            if not episode_target:
                logger.warning(f"⚠️ Episode {ep.id} has placeholder_status but no plex_dummy_id", extra={'emoji_type': 'warning'})
            else:
                episode_obj = None
                try:
                    for e in tv_section.all():
                        try:
                            if str(getattr(e, 'ratingKey', None)) == str(episode_target):
                                episode_obj = e
                                break
                        except Exception:
                            continue
                except Exception as ex:
                    logger.error(f"❌ Error scanning TV section for episode: {ex}", extra={'emoji_type': 'error'})
                    episode_obj = None

                if episode_obj:
                    current_summary = episode_obj.summary if hasattr(episode_obj, 'summary') else ""
                    new_summary = _prepend_status_to_summary(current_summary, ep.placeholder_status)

                    def update_episode():
                        episode_obj.editSummary(new_summary)
                        episode_obj.reload()
                        return True

                    success = retry_call(
                        func=update_episode,
                        on_error=lambda ex: logger.error(f"❌ Failed to update episode {episode_target}: {ex}"),
                        retry_interval=retry_interval,
                        retry_timeout=retry_timeout,
                        success_condition=lambda res: res is True
                    )

                    if success:
                        updated_any = True
        else:
            # No placeholder for episode: attempt to update real episode item only if plex_id present
            episode_target = getattr(ep, 'plex_id', None)
            if episode_target:
                episode_obj = None
                try:
                    for e in tv_section.all():
                        try:
                            if str(getattr(e, 'ratingKey', None)) == str(episode_target):
                                episode_obj = e
                                break
                        except Exception:
                            continue
                except Exception as ex:
                    logger.error(f"❌ Error scanning TV section for episode: {ex}", extra={'emoji_type': 'error'})
                    episode_obj = None

                if episode_obj:
                    current_summary = episode_obj.summary if hasattr(episode_obj, 'summary') else ""
                    new_summary = _prepend_status_to_summary(current_summary, ep.placeholder_status)

                    def update_episode_real():
                        episode_obj.editSummary(new_summary)
                        episode_obj.reload()
                        return True

                    success = retry_call(
                        func=update_episode_real,
                        on_error=lambda ex: logger.error(f"❌ Failed to update episode {episode_target}: {ex}"),
                        retry_interval=retry_interval,
                        retry_timeout=retry_timeout,
                        success_condition=lambda res: res is True
                    )

                    if success:
                        updated_any = True

        return updated_any

    except Exception as ex:
        logger.error(f"❌ Error in update_plex_title_status: {ex}", extra={"emoji_type": "error"})
        return False


def retry_failed_plex_title_updates(
    dbsession: Session,
    ent_id: int,
    model: Type,
    action: str
) -> bool:
    """
    For every SubFlow queued at the 'update_plex_title_status' step:
      - If ALL of its related DB records (Movie or Series→Season→Episode) already have
        their plex_title and plex_overview updated to include placeholder_status,
        mark that SubFlow DONE.
      - Otherwise, reset its step_index back to the update step and clear retry_count
        so it will be retried on the next poll.
    """
    # 1) Collect all queued SubFlows currently pointing at update step
    queued = dbsession.query(SubFlow).filter_by(status='QUEUED').all()
    to_retry = []
    for sf in queued:
        steps = sf.steps.split(',')
        if sf.step_index < len(steps) and steps[sf.step_index] == 'retry_failed_plex_title_updates':
            to_retry.append((sf, steps.index('update_plex_title_status')))

    all_ok = True
    for sf, update_idx in to_retry:
        # 2) Build a list of records to verify
        records = []
        if sf.movie_id:
            m = dbsession.query(Movie).get(sf.movie_id)
            if m and m.plex_id:
                records.append((m, m.placeholder_status))
        else:
            ep = dbsession.query(Episode).get(sf.episode_id)
            seas = dbsession.query(Season).get(ep.season_id) if ep else None
            series = dbsession.query(Series).get(seas.series_id) if seas else None
            # always check series, then season, then episode
            if series and series.plex_id:
                records.append((series, series.placeholder_status))
            if seas and seas.plex_id:
                records.append((seas, seas.placeholder_status))
            if ep and ep.plex_id:
                records.append((ep, ep.placeholder_status))

        # 3) Verify each record's title/overview using PlexAPI
        record_ok = True
        for record, placeholder in records:
            try:
                # Find the item in Plex
                item = None
                for section in plex.library.sections():
                    try:
                        item = section.getByRatingKey(record.plex_id)
                        break
                    except:
                        continue
                
                if not item:
                    record_ok = False
                    break
                    
                current_summary = item.summary if hasattr(item, 'summary') else ""
                desired_summary = _prepend_status_to_summary(current_summary, placeholder)
                
                if current_summary != desired_summary:
                    record_ok = False
                    break
            except Exception as ex:
                logger.error(f"❌ Error verifying Plex item {record.plex_id}: {ex}", extra={"emoji_type": "error"})
                record_ok = False
                break

        # 4) Mark DONE if OK, otherwise rewind for retry
        if record_ok:
            sf.status = 'DONE'
        else:
            sf.step_index = update_idx
            sf.retry_count = 0
            sf.status = 'QUEUED'
            all_ok = False

        dbsession.add(sf)

    dbsession.commit()
    return all_ok


def get_plex_file_path(item_id: str, user_id: Optional[str] = None) -> str:
    """
    Retrieve the absolute filesystem path for a given Plex item ID.
    """
    if not plex:
        logger.error("Plex server not available", extra={'emoji_type': 'error'})
        return ''
        
    try:
        # In PlexAPI, we can get an item by its rating key across all sections
        for section in plex.library.sections():
            try:
                item = section.getByRatingKey(item_id)
                if hasattr(item, 'locations') and item.locations:
                    path = item.locations[0]
                    logger.info(f"Retrieved path for item {item_id}: {path}", extra={'emoji_type': 'info'})
                    return path
            except:
                continue
                
        logger.warning(f"Plex item {item_id} not found or has no file path", extra={'emoji_type': 'warning'})
    except Exception as ex:
        logger.error(f"Failed to get file path for {item_id}: {ex}", extra={'emoji_type': 'error'})

    return ''


def test_plex_connection() -> bool:
    """Test connectivity to the Plex server by fetching public system info."""
    if not getattr(settings, "plex_enabled", False) or not settings.PLEX_URL or not settings.PLEX_TOKEN:
        return False
        
    try:
        url = build_plex_url('system/agents')
        headers = {'X-Plex-Token': settings.PLEX_TOKEN}
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        return True
    except Exception as ex:
        return False


def test_plex_endpoints():
    """Test key Plex API endpoints needed for operation."""
    try:
        # Test library sections endpoint
        url = build_plex_url('library/sections')
        headers = {'X-Plex-Token': settings.PLEX_TOKEN}
        resp = requests.get(url, headers=headers, timeout=5)
        resp.raise_for_status()
        logger.info("Plex /library/sections endpoint accessible", extra={'emoji_type': 'success'})
        
        # Test system endpoint
        url = build_plex_url('system/agents')
        resp = requests.get(url, headers=headers, timeout=5)
        resp.raise_for_status()
        logger.info("Plex /system/agents endpoint accessible", extra={'emoji_type': 'success'})
    except Exception as ex:
        logger.error(f"Plex API endpoint test failed: {ex}", extra={'emoji_type': 'error'})


# Run connection test at import time
if getattr(settings, "plex_enabled", False):
    try:
        if test_plex_connection():
            logger.info("Connected to Plex server", extra={'emoji_type': 'success'})
            test_plex_endpoints()
        else:
            logger.error("Failed to connect to Plex server", extra={'emoji_type': 'error'})
    except Exception as ex:
        logger.error(f"Failed to connect to Plex server: {ex}", extra={'emoji_type': 'error'})


def find_show_by_id(tvdb_id, title=None):
    """
    Find a TV show in Plex library using TVDB ID as primary identifier
    Falls back to title matching if ID matching fails
    
    Args:
        tvdb_id: TVDB ID of the show
        title: Title to use as fallback (optional)
    
    Returns:
        Plex show object or None if not found
    """
    if not getattr(settings, "plex_enabled", False) or not settings.PLEX_URL or not settings.PLEX_TOKEN:
        return None
    
    try:
        if not plex:
            logger.error("Plex server not available", extra={'emoji_type': 'error'})
            return None
            
        tv_section = plex.library.sectionByID(settings.PLEX_TV_SECTION_ID)
        show = None
        
        # Method 1: Match by TVDB ID in GUID (most reliable)
        all_shows = tv_section.all()
        for s in all_shows:
            for guid in s.guids:
                if f'tvdb://{tvdb_id}' in guid.id:
                    show = s
                    logger.debug(f"Found show by TVDB ID in metadata: '{s.title}'", 
                               extra={'emoji_type': 'debug'})
                    return show
        
        # Method 2: Look for TVDB ID in folder path
        for s in all_shows:
            if hasattr(s, 'locations') and s.locations:
                for location in s.locations:
                    if f"tvdb-{tvdb_id}" in location.lower():
                        show = s
                        logger.debug(f"Found show by TVDB ID in path: '{s.title}'", 
                                   extra={'emoji_type': 'debug'})
                        return show
        
        # Method 3: Fallback to title matching
        if title:
            # Clean title without year for matching
            clean_title = title
            if '(' in title and ')' in title:
                clean_title = title.split('(')[0].strip()
            
            show = tv_section.get(clean_title)
            if show:
                logger.debug(f"Found show by title (fallback): '{show.title}'", 
                           extra={'emoji_type': 'debug'})
                return show
        
        if not show:
            logger.debug(f"Show with TVDB ID {tvdb_id} not found in Plex library", 
                       extra={'emoji_type': 'debug'})
        
        return show
        
    except Exception as e:
        logger.error(f"Error finding show by ID: {e}", extra={'emoji_type': 'error'})
        return None


def find_movie_by_id(tmdb_id, title=None, year=None):
    """
    Find a movie in Plex library using TMDB ID as primary identifier
    Falls back to title matching if ID matching fails
    
    Args:
        tmdb_id: TMDB ID of the movie
        title: Title to use as fallback (optional)
        year: Year to use with title matching (optional)
    
    Returns:
        Plex movie object or None if not found
    """
    if not getattr(settings, "plex_enabled", False) or not settings.PLEX_URL or not settings.PLEX_TOKEN:
        return None
    
    try:
        if not plex:
            logger.error("Plex server not available", extra={'emoji_type': 'error'})
            return None
            
        movie_section = plex.library.sectionByID(settings.PLEX_MOVIE_SECTION_ID)
        movie = None
        
        # Method 1: Match by TMDB ID in GUID (most reliable)
        all_movies = movie_section.all()
        for m in all_movies:
            for guid in m.guids:
                if f'tmdb://{tmdb_id}' in guid.id:
                    movie = m
                    logger.debug(f"Found movie by TMDB ID in metadata: '{m.title}'", 
                               extra={'emoji_type': 'debug'})
                    return movie
        
        # Method 2: Look for TMDB ID in folder path
        for m in all_movies:
            if hasattr(m, 'locations') and m.locations:
                for location in m.locations:
                    if f"tmdb-{tmdb_id}" in location.lower():
                        movie = m
                        logger.debug(f"Found movie by TMDB ID in path: '{m.title}'", 
                                   extra={'emoji_type': 'debug'})
                        return movie
        
        # Method 3: Fallback to title+year matching
        if title:
            # Clean title without year for matching
            clean_title = title
            if '(' in title and ')' in title:
                clean_title = title.split('(')[0].strip()
            
            # Try with title and year if available
            if year:
                for m in all_movies:
                    if (m.title.lower() == clean_title.lower() and 
                        hasattr(m, 'year') and m.year == int(year)):
                        movie = m
                        logger.debug(f"Found movie by title and year: '{m.title} ({m.year})'", 
                                   extra={'emoji_type': 'debug'})
                        return movie
            
            # Try with just title
            movie = movie_section.get(clean_title)
            if movie:
                logger.debug(f"Found movie by title (fallback): '{movie.title}'", 
                           extra={'emoji_type': 'debug'})
                return movie
        
        if not movie:
            logger.debug(f"Movie with TMDB ID {tmdb_id} not found in Plex library", 
                       extra={'emoji_type': 'debug'})
        
        return movie
        
    except Exception as e:
        logger.error(f"Error finding movie by ID: {e}", extra={'emoji_type': 'error'})
        return None