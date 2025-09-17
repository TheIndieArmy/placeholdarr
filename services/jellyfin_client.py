import os
import re
import time
from typing import List, Optional, Dict, Any, Callable, Tuple, Union, Type
from urllib.parse import quote
import requests
from core.config import settings
from core.logger import logger
from services.utils import strip_status_markers
from services.postgres.models import Movie, Series, Season, Episode, SubFlow
from urllib.parse import quote_plus
from sqlalchemy.orm import Session
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
        logger.warning("Jellyfin is not enabled or URL not configured", extra={'emoji_type': 'warning'})
        return False

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


def refresh_jellyfin_dummy(dbsession: Session, ent_id: int, model: type, action: str) -> bool:
    """
    Bulk-refresh all pending 'refresh_jellyfin_dummy' subflows in one call,
    using step_index and steps to find exact matches.

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
    logger.info(f"Successfully refreshed {len(matching)} subflows for 'refresh_jellyfin_dummy' step", extra={'emoji_type': 'success'})
    return True

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
            logger.warning("No file paths found to refresh", extra={'emoji_type': 'warning'})
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
    
    # Update ID
    jf_id = match.get('Id')
    if getattr(obj, id_field) != jf_id:
        setattr(obj, id_field, jf_id)
        dbsession.add(obj)
        dbsession.commit()
        logger.info(f"Updated {model.__name__} {ent_id} with Jellyfin ID: {jf_id}", extra={'emoji_type': 'update'})
    else:
        logger.debug(f"{model.__name__} {ent_id} already has Jellyfin ID: {jf_id}", extra={'emoji_type': 'debug'})
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
        dbsession.commit()
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
    
    # MOVIE CASE
    user_id = get_admin_user()
    if model is Movie:
        m = dbsession.query(Movie).get(ent_id)
        if not m or not m.radarrpath:
            logger.warning(f"Movie {ent_id} not found or missing radarrpath", extra={'emoji_type': 'warning'})
            return False
        if m.jellyfin_movie_id:
            logger.debug(f"Movie {ent_id} already has jellyfin_movie_id: {m.jellyfin_movie_id}", extra={'emoji_type': 'debug'})
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
                items = retry_call(
                    func=lambda: session.get(movie_url, timeout=5),
                    on_error=lambda ex: logger.error(f"Movie search error: {ex}"),
                    retry_interval=3, retry_timeout=30,
                    success_condition=lambda res: success_movie(res)
                ) or []
                for candidate in items:
                    if (
                        int(candidate.get("ProviderIds", {}).get("Tmdb", -1)) == m.tmdbid and
                        candidate.get('Path', '') == m.radarrpath and
                        (not m.year or int(candidate.get("ProductionYear", -1)) == m.year)
                    ):
                        it = candidate
                        break
            except Exception as ex:
                logger.error(f"Error fetching Jellyfin item by ID: {ex}", extra={'emoji_type': 'error'})
            if it:
                save_jellyfin_arr_id(dbsession, Movie, m.id, it)
            return True
        clean_title = re.sub(r"\s*\(\d{4}\)$", "", m.title)
        url = build_jellyfin_url(
            f"Items?searchTerm={quote_plus(clean_title)}"
            "&includeItemTypes=Movie&recursive=true&fields=ProviderIds,Name,ProductionYear,Overview,Path"
        )
        def filter_movies(res):
            if res.status_code not in (200, 204, 404):
                return False
            if res.status_code == 404:
                return True
            it = res.json().get('Items', [])
            return [
                it for it in (items or [])
                if (
                    int(it.get("ProviderIds", {}).get("Tmdb", -1)) == m.tmdbid and it.get('Path',{}) == m.radarrpath
                )
                and (not m.year or int(it.get("ProductionYear", -1)) == m.year)
            ]
        items = retry_call(
            func=lambda: session.get(url, timeout=5),
            on_error=lambda ex: logger.error(f"Movie search error: {ex}"),
            retry_interval=3, retry_timeout=30,
            success_condition=lambda res: bool(filter_movies(res))
        ) or []
        for it in items:
            if it.get('ProviderIds', {}).get('Tmdb') != m.tmdbid and it.get('Path',{}) != m.radarrpath:
                continue
            save_jellyfin_arr_id(dbsession, Movie, m.id, it)
            return True
        return False

    # EPISODE CASE
    ep = dbsession.query(Episode).get(ent_id)
    if not ep or not ep.sonarrpath:
        logger.warning(f"Episode {ent_id} not found or missing sonarrpath", extra={'emoji_type': 'warning'})
        return False
    seas = dbsession.query(Season).get(ep.season_id)
    series = dbsession.query(Series).get(seas.series_id)
    logger.info(f"Processing episode {ent_id} for series '{series.title}' S{ep.season_number}E{ep.episode_number}", extra={'emoji_type': 'tv'})

    # 1) Series ID with folder-based dedupe
    if series.jellyfin_series_id:
        jf_series_id = series.jellyfin_series_id
        logger.debug(f"Using existing series ID: {jf_series_id}", extra={'emoji_type': 'debug'})
    else:
        logger.info(f"Searching for series '{series.title}' in Jellyfin", extra={'emoji_type': 'search'})
        clean_title = re.sub(r"\s*\(\d{4}\)$", "", series.title)
        url = build_jellyfin_url(
            f"Items?searchTerm={quote_plus(clean_title)}"
            "&includeItemTypes=Series&recursive=true&fields=ProviderIds,Path"
        )
        folder = None
        try:
            base = settings.TV_LIBRARY_PATH.rstrip(os.sep) + os.sep
            rel = ep.sonarrpath.replace(base, '')
            folder = rel.split(os.sep)[0]
            logger.debug(f"Extracted folder '{folder}' from path: {ep.sonarrpath}", extra={'emoji_type': 'debug'})
        except Exception as e:
            logger.error(f"Error locating Series folder path: {e}", extra={'emoji_type': 'error'})
            return False
        def pick_series(items: List[dict]) -> List[dict]:
            return [
                s for s in (items or [])
                if s.get("ProviderIds", {}).get("Tvdb")
                and s["ProviderIds"]["Tvdb"] == series.tvdbid
                and s.get('Path') == folder
            ]
        items = retry_call(
            func=lambda: session.get(url, timeout=5).json().get('Items', []),
            on_error=lambda ex: logger.error(f"Series search error: {ex}", extra={'emoji_type': 'error'}),
            retry_interval=3, retry_timeout=30,
            success_condition=lambda res: bool(pick_series(res))
        ) or []
        
        logger.debug(f"Found {len(items)} series candidates from search", extra={'emoji_type': 'debug'})
        
        match = None
        if folder:
            match = next(
                (it for it in items
                if it.get('ProviderIds', {}).get('Tvdb') == series.tvdbid
                and it.get('Path') == folder),
                None
            )
            if match:
                logger.info(f"Matched series '{match.get('Name', 'Unknown')}' with TVDB ID {series.tvdbid}", extra={'emoji_type': 'success'})
            else:
                logger.warning(f"No series match found for TVDB ID {series.tvdbid} in folder '{folder}'", extra={'emoji_type': 'warning'})
        if not match:
            return False
        jf_series_id = match['Id']
        save_jellyfin_arr_id(dbsession, Series, series.id, match)

    # 2) Cache season IDs
    seas_url = build_jellyfin_url(
        f"Items?ParentId={jf_series_id}"
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
        func=lambda: session.get(seas_url, timeout=5).json().get('Items', []),
        on_error=lambda ex: logger.error(f"Season list error for series {series.id}: {ex}", extra={'emoji_type': 'error'}),
        retry_interval=3,
        retry_timeout=30,
        success_condition=lambda res: pick_seasons(res)
    ) or []

    logger.debug(f"Found {len(seasons)} seasons for series", extra={'emoji_type': 'debug'})

    season_map = {s['IndexNumber']: s for s in seasons}
    for num, it in season_map.items():
        logger.debug(f"Processing season {num} mapping to database", extra={'emoji_type': 'processing'})
        db_seas = (
            dbsession.query(Season)
            .filter_by(series_id=series.id, season_number=num)
            .first()
        )
        if db_seas and not db_seas.jellyfin_season_id:
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
    jf_seas_id = season_map.get(ep.season_number)['Id']
    logger.debug(f"Using Jellyfin season ID {jf_seas_id} for season {ep.season_number}", extra={'emoji_type': 'debug'})
    if not jf_seas_id:
        logger.warning(f"No Jellyfin season ID found for season {ep.season_number}", extra={'emoji_type': 'warning'})
        return False
    epi_url = build_jellyfin_url(
            f"Items?ParentId={jf_seas_id}"
            "&includeItemTypes=Episode&fields=IndexNumber,Id,Path,Overview,Name"
        )
    logger.debug(f"Searching for episodes in season with URL: {epi_url}", extra={'emoji_type': 'search'})
    def pick_episode(items: List[dict]) -> List[dict]:
        return [
            ss for ss in (items or [])
            if ss.get("IndexNumber") == ep.episode_number
            and ss.get("Path") == ep.sonarrpath
        ]
    eps = retry_call(
        func=lambda: session.get(epi_url, timeout=5).json().get('Items', []),
        on_error=lambda ex: logger.error(f"Episode list error for season {jf_seas_id}: {ex}", extra={'emoji_type': 'error'}),
        retry_interval=3,
        retry_timeout=30,
        success_condition=lambda res: bool(pick_episode(res))
    ) or []
    
    logger.debug(f"Found {len(eps)} episodes to process from Jellyfin", extra={'emoji_type': 'debug'})
    
    for sf in queued:
        logger.debug(f"Processing subflow {sf.id} for episode {sf.episode_id}", extra={'emoji_type': 'processing'})
        steps = sf.steps.split(',')
        if sf.step_index < len(steps) and steps[sf.step_index] != 'verify_scan_jellyfin': ## change the step index to -1
            logger.debug(f"Skipping subflow {sf.id} - wrong step or step index", extra={'emoji_type': 'debug'})
            continue
        ep2 = dbsession.query(Episode).get(sf.episode_id)
        if ep2.season_number != season.season_number:
            logger.debug(f"Skipping episode {ep2.id} - different season ({ep2.season_number} vs {season.season_number})", extra={'emoji_type': 'debug'})
            continue

        logger.debug(f"Matching episode {ep2.episode_number} with path {ep2.sonarrpath}", extra={'emoji_type': 'search'})
        for it in eps:
            if it['IndexNumber'] != ep2.episode_number:
                continue
            if it['Path'] == ep2.sonarrpath:
                logger.info(f"Successfully matched episode {ep2.episode_number} in Jellyfin", extra={'emoji_type': 'success'})
                save_jellyfin_arr_id(dbsession, Episode, ep2.id, it)
                sf.status = 'DONE'
                dbsession.add(sf)
                if ep2.id == ent_id:
                    matched = True
                break
    if not matched:
        logger.warning(f"No matching Jellyfin episode found for {ep2.sonarrpath} in season {jf_seas_id}", extra={'emoji_type': 'warning'})

    logger.info(f"Episode matching completed for series {series.id}, matched: {matched}", extra={'emoji_type': 'success' if matched else 'warning'})
    dbsession.commit()
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
        if not m or not m.dummypath:
            logger.warning(f"Movie {ent_id} not found or missing dummypath", extra={'emoji_type': 'warning'})
            return False
        logger.debug(f"Movie {m.id} has dummypath: {m.dummypath}", extra={'emoji_type': 'debug'})
        if m.jellyfin_movie_id:
            logger.debug(f"Movie {m.id} already has Jellyfin ID, checking if still exists", extra={'emoji_type': 'search'})
            it = None
            try:
                movie_url = build_jellyfin_url(f"Items?{m.jellyfin_id}/?userId={user_id}&includeItemTypes=Movie&fields=ProviderIds,Name,ProductionYear,Overview,Path")
                logger.debug(f"Checking movie existence with URL: {movie_url}", extra={'emoji_type': 'debug'})
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
                items = retry_call(
                    func=lambda: session.get(movie_url, timeout=5),
                    on_error=lambda ex: logger.error(f"Movie search error: {ex}", extra={'emoji_type': 'error'}),
                    retry_interval=3, retry_timeout=30,
                    success_condition=lambda res: success_movie(res)
                ) or []
                
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
                logger.warning(f"Movie {m.id} has jellyfin_id but item not found in Jellyfin - should retry", extra={'emoji_type': 'warning'})
                return False
            
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
            it = res.json().get('Items', [])
            return [
                it for it in (items or [])
                if (
                    int(it.get("ProviderIds", {}).get("Tmdb", -1)) == m.tmdbid and it.get('Path',{}) == m.dummypath
                )
                and (not m.year or int(it.get("ProductionYear", -1)) == m.year)
            ]
        items = retry_call(
            func=lambda: session.get(url, timeout=5),
            on_error=lambda ex: logger.error(f"Movie search error: {ex}", extra={'emoji_type': 'error'}),
            retry_interval=3, retry_timeout=30,
            success_condition=lambda res: bool(filter_movies(res))
        ) or []
        
        logger.debug(f"Found {len(items)} candidate movies from search", extra={'emoji_type': 'debug'})
        
        for it in items:
            if it.get('ProviderIds', {}).get('Tmdb') != m.tmdbid and it.get('Path',{}) != m.dummypath:
                logger.debug(f"Skipping movie candidate due to TMDB/path mismatch", extra={'emoji_type': 'debug'})
                continue
            logger.info(f"Successfully matched movie '{it.get('Name', 'Unknown')}' in Jellyfin", extra={'emoji_type': 'success'})
            save_jellyfin_dummy_id(dbsession, Movie, m.id, it)
            return True
        
        logger.warning(f"No matching movie found in Jellyfin for {m.title}", extra={'emoji_type': 'warning'})
        return False

    # EPISODE CASE
    logger.debug("Processing episode case", extra={'emoji_type': 'tv'})
    ep = dbsession.query(Episode).get(ent_id)
    if not ep or not ep.dummypath:
        logger.warning(f"Episode {ent_id} not found or missing dummypath", extra={'emoji_type': 'warning'})
        return False
    logger.debug(f"Episode {ep.id} has dummypath: {ep.dummypath}", extra={'emoji_type': 'debug'})
    
    seas = dbsession.query(Season).get(ep.season_id)
    series = dbsession.query(Series).get(seas.series_id)
    logger.info(f"Processing episode {ep.episode_number} of season {seas.season_number} for series '{series.title}'", extra={'emoji_type': 'tv'})
    # user = get_admin_user()

    # 1) Series ID with folder-based dedupe
    if series.jellyfin_series_id:
        logger.debug(f"Using existing Jellyfin series ID: {series.jellyfin_series_id}", extra={'emoji_type': 'success'})
        jf_series_id = series.jellyfin_series_id
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
            base = settings.TV_LIBRARY_PATH.rstrip(os.sep) + os.sep
            rel = ep.dummypath.replace(base, '')
            folder = rel.split(os.sep)[0]
            logger.debug(f"Extracted folder '{folder}' from episode path", extra={'emoji_type': 'debug'})
        except Exception as e:
            logger.error(f"Error locating Series dummy path: {e}", extra={'emoji_type': 'error'})
            return False
        def pick_series(items: List[dict]) -> List[dict]:
            return [
                s for s in (items or [])
                if s.get("ProviderIds", {}).get("Tvdb")
                and s["ProviderIds"]["Tvdb"] == series.tvdbid
                and s.get('Path') == folder
            ]
        items = retry_call(
            func=lambda: session.get(url, timeout=5).json().get('Items', []),
            on_error=lambda ex: logger.error(f"Series search error: {ex}", extra={'emoji_type': 'error'}),
            retry_interval=3, retry_timeout=30,
            success_condition=lambda res: bool(pick_series(res))
        ) or []
        
        logger.debug(f"Found {len(items)} series candidates from search", extra={'emoji_type': 'debug'})
        match = None
        if folder:
            match = next(
                (it for it in items
                if it.get('ProviderIds', {}).get('Tvdb') == series.tvdbid
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
        logger.info(f"Saving Jellyfin series ID {jf_series_id} for series {series.id}", extra={'emoji_type': 'success'})
        save_jellyfin_dummy_id(dbsession, Series, series.id, match)

    # 2) Cache season IDs
    seas_url = build_jellyfin_url(
        f"Items?ParentId={jf_series_id}"
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
        func=lambda: session.get(seas_url, timeout=5).json().get('Items', []),
        on_error=lambda ex: logger.error(f"Season list error for series {series.id}: {ex}", extra={'emoji_type': 'error'}),
        retry_interval=3,
        retry_timeout=30,
        success_condition=lambda res: pick_seasons(res)
    ) or []

    logger.debug(f"Found {len(seasons)} seasons for series", extra={'emoji_type': 'debug'})
    
    season_map = {s['IndexNumber']: s for s in seasons}
    for num, it in season_map.items():
        logger.debug(f"Processing season {num} mapping to database", extra={'emoji_type': 'processing'})
        db_seas = (
            dbsession.query(Season)
            .filter_by(series_id=series.id, season_number=num)
            .first()
        )
        if db_seas and not db_seas.jellyfin_season_id:
            logger.info(f"Saving Jellyfin season ID for season {num}", extra={'emoji_type': 'success'})
            save_jellyfin_dummy_id(dbsession, Season, db_seas.id, it)

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

    # 4) Match each queued episode by path
    jf_seas_id = season_map.get(ep.season_number)['Id']
    if not jf_seas_id:
        return False
    epi_url = build_jellyfin_url(
            f"Items?ParentId={jf_seas_id}"
            "&includeItemTypes=Episode&fields=IndexNumber,Id,Path,Overview,Name"
        )
    def pick_episode(items: List[dict]) -> List[dict]:
        return [
            ss for ss in (items or [])
            if ss.get("IndexNumber") == ep.episode_number
            and ss.get("Path") == ep.dummypath
        ]
    eps = retry_call(
        func=lambda: session.get(epi_url, timeout=5).json().get('Items', []),
        on_error=lambda ex: logger.error(f"❌ Episode list error for season {jf_seas_id}: {ex}"),
        retry_interval=3,
        retry_timeout=30,
        success_condition=lambda res: bool(pick_episode(res))
    ) or []
    for sf in queued:
        steps = sf.steps.split(',')
        if sf.step_index < len(steps) and steps[sf.step_index] != 'verify_scan_jellyfin': ## change the step index to -1
            continue
        ep2 = dbsession.query(Episode).get(sf.episode_id)
        if ep2.season_number != season.season_number:
            continue

        for it in eps:
            if it['IndexNumber'] != ep2.episode_number:
                continue
            if it['Path'] == ep2.dummypath:
                save_jellyfin_dummy_id(dbsession, Episode, ep2.id, it)
                sf.status = 'DONE'
                dbsession.add(sf)
                if ep2.id == ent_id:
                    matched = True
                break
    if not matched:
        logger.warning(f"⚠️ No matching Jellyfin episode found for {ep2.dummypath} in season {jf_seas_id}")

    dbsession.commit()
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


def update_jellyfin_title_status(
    dbsession: Session,
    ent_id: int,
    model: Type,
    action: str = "status_update",
    retry_interval: int = 30,
    retry_timeout: int = 600
) -> bool:
    """
    Update title & overview in Jellyfin for a Movie or Episode hierarchy.

    Movie: patch its own item using placeholder status.
    Episode: patch Series, Season, and Episode items if needed.
    Returns True if any update succeeded.
    """
    logger.info(f"Updating Jellyfin title status for {model.__name__} {ent_id}", extra={'emoji_type': 'status'})
    
    updated_any = False

    targets = []
    # prepare update DTOs based on current status fields
    if model is Movie:
        movie = dbsession.query(Movie).get(ent_id)
        if not movie:
            logger.warning(f"Movie {ent_id} not found", extra={'emoji_type': 'warning'})
            return False
        
        # If movie is marked as deleted, skip updating title status (return success to avoid endless retries)
        if movie.is_deleted:
            logger.info(f"Movie {ent_id} is deleted, skipping title status update", extra={'emoji_type': 'skip'})
            return True
            
        if not movie.jellyfin_id:
            # If movie has no jellyfin_id, it may have been deleted from Jellyfin or not yet scanned
            # Return True to avoid endless retries (especially for partially deleted movies)
            logger.warning(f"Movie {ent_id} missing jellyfin_id, skipping title status update", extra={'emoji_type': 'warning'})
            return True
        orig = strip_status_markers(movie.jellyfin_title)
        new_name = f"{orig} - [{movie.placeholder_status}]" if movie.placeholder_status else orig
        new_ovr = _prepend_status_to_summary(movie.jellyfin_overview, movie.placeholder_status)
        targets.append((movie.jellyfin_id, new_name, new_ovr))
        logger.debug(f"Preparing movie title update: '{new_name}'", extra={'emoji_type': 'debug'})
    else:
        ep = dbsession.query(Episode).get(ent_id)
        if not ep:
            logger.warning(f"Episode {ent_id} not found", extra={'emoji_type': 'warning'})
            return False
            
        # If episode is marked as deleted, skip updating title status (return success to avoid endless retries)
        if ep.is_deleted:
            logger.info(f"Episode {ent_id} is deleted, skipping title status update", extra={'emoji_type': 'skip'})
            return True
            
        if not ep.jellyfin_id:
            # If episode has no jellyfin_id, it may have been deleted from Jellyfin or not yet scanned
            # Return True to avoid endless retries (especially for partially deleted episodes)
            logger.warning(f"Episode {ent_id} missing jellyfin_id, skipping title status update", extra={'emoji_type': 'warning'})
            return True
        seas = dbsession.query(Season).get(ep.season_id)
        series = dbsession.query(Series).get(seas.series_id)
        # Series
        if series and series.jellyfin_id:
            orig = strip_status_markers(series.jellyfin_title)
            new_name = f"{orig} - [{series.placeholder_status}]" if series.placeholder_status else orig
            new_ovr = _prepend_status_to_summary(series.jellyfin_overview, series.placeholder_status)
            targets.append((series.jellyfin_id, new_name, new_ovr))
        # Season
        if seas and seas.jellyfin_id:
            orig = strip_status_markers(seas.jellyfin_title)
            new_name = f"{orig} - [{seas.placeholder_status}]" if seas.placeholder_status else orig
            new_ovr = _prepend_status_to_summary(seas.jellyfin_overview, seas.placeholder_status)
            targets.append((seas.jellyfin_id, new_name, new_ovr))
        # Episode
        orig = strip_status_markers(ep.jellyfin_title)
        new_name = f"{orig} - [{ep.placeholder_status}]" if ep.placeholder_status else orig
        new_ovr = _prepend_status_to_summary(ep.jellyfin_overview, ep.placeholder_status)
        targets.append((ep.jellyfin_id, new_name, new_ovr))
        logger.debug(f"Preparing episode title update: '{new_name}'", extra={'emoji_type': 'debug'})

    # perform updates
    logger.info(f"Updating {len(targets)} Jellyfin items", extra={'emoji_type': 'update'})
    for item_id, name, overview in targets:
        dto = {"Name": name, "Overview": overview}
        url = build_jellyfin_url(f"Items/{item_id}/Fields")

        def do_patch():
            resp = session.patch(url, json=dto, timeout=5)
            return resp.status_code

        def on_err(ex: Exception):
            logger.error(f"Failed to update item {item_id}: {ex}", extra={'emoji_type': 'error'})

        success = retry_call(
            func=do_patch,
            on_error=on_err,
            retry_interval=retry_interval,
            retry_timeout=retry_timeout,
            success_condition=lambda code: code == 204
        )
        if success:
            updated_any = True
            logger.info(f"Successfully updated Jellyfin item {item_id}", extra={'emoji_type': 'success'})
        else:
            logger.warning(f"Failed to update Jellyfin item {item_id}", extra={'emoji_type': 'warning'})

    logger.info(f"Jellyfin title status update completed, success: {updated_any}", extra={'emoji_type': 'success' if updated_any else 'warning'})
    return updated_any

def retry_failed_jellyfin_title_updates(
    dbsession: Session,
    ent_id: int,
    model: Type,
    action: str
) -> None:
    """
    For every SubFlow queued at the 'update_jellyfin_title_status' step:
      - If ALL of its related DB records (Movie or Series→Season→Episode) already have
        their jellyfin_title and jellyfin_overview updated to include placeholder_status,
        mark that SubFlow DONE.
      - Otherwise, reset its step_index back to the update step and clear retry_count
        so it will be retried on the next poll.
    """
    logger.info(f"Starting retry_failed_jellyfin_title_updates for {model.__name__} {ent_id}", extra={'emoji_type': 'processing'})
    
    # 1) Collect all queued SubFlows currently pointing at update step
    queued = dbsession.query(SubFlow).filter_by(status='QUEUED').all()
    logger.debug(f"Found {len(queued)} total queued subflows", extra={'emoji_type': 'debug'})
    
    to_retry = []
    for sf in queued:
        steps = sf.steps.split(',')
        if sf.step_index < len(steps) and steps[sf.step_index] == 'retry_failed_title_updates':
            try:
                update_idx = steps.index('update_jellyfin_title_status')
                to_retry.append((sf, update_idx))
                logger.debug(f"Found subflow {sf.id} to retry at step index {update_idx}", extra={'emoji_type': 'debug'})
            except ValueError:
                logger.warning(f"Subflow {sf.id} missing 'update_jellyfin_title_status' step", extra={'emoji_type': 'warning'})
    
    logger.info(f"Found {len(to_retry)} subflows ready for title update retry", extra={'emoji_type': 'retry'})

    successful_retries = 0
    failed_retries = 0
    
    for sf, update_idx in to_retry:
        logger.debug(f"Processing subflow {sf.id} for title update verification", extra={'emoji_type': 'processing'})
        
        # 2) Build a list of records to verify
        records = []
        if sf.movie_id:
            m = dbsession.query(Movie).get(sf.movie_id)
            if m and m.jellyfin_id:
                records.append((m, m.placeholder_status))
                logger.debug(f"Added movie {m.id} to verification list", extra={'emoji_type': 'movie'})
        else:
            ep = dbsession.query(Episode).get(sf.episode_id)
            seas = dbsession.query(Season).get(ep.season_id) if ep else None
            series = dbsession.query(Series).get(seas.series_id) if seas else None
            
            # always check series, then season, then episode
            if series and series.jellyfin_id:
                records.append((series, series.placeholder_status))
                logger.debug(f"Added series {series.id} to verification list", extra={'emoji_type': 'tv'})
            if seas and seas.jellyfin_id:
                records.append((seas, seas.placeholder_status))
                logger.debug(f"Added season {seas.id} to verification list", extra={'emoji_type': 'tv'})
            if ep and ep.jellyfin_id:
                records.append((ep, ep.placeholder_status))
                logger.debug(f"Added episode {ep.id} to verification list", extra={'emoji_type': 'tv'})

        logger.debug(f"Verifying {len(records)} records for subflow {sf.id}", extra={'emoji_type': 'debug'})

        # 3) Verify each record’s title/overview
        all_ok = True
        for record, placeholder in records:
            orig = strip_status_markers(record.jellyfin_title)
            desired_name = f"{orig} - [{placeholder}]" if placeholder else orig
            desired_ovr  = _prepend_status_to_summary(record.jellyfin_overview, placeholder)
            if record.jellyfin_title != desired_name or record.jellyfin_overview != desired_ovr:
                all_ok = False
                break

        # 4) Mark DONE if OK, otherwise rewind for retry
        if all_ok:
            sf.status = 'DONE'
        else:
            sf.step_index  = update_idx
            sf.retry_count = 0
            sf.status      = 'QUEUED'

        dbsession.add(sf)

    try:
        dbsession.commit()
        logger.info(f"Title update retry completed: {successful_retries} successful, {failed_retries} failed", extra={'emoji_type': 'success' if successful_retries > 0 else 'info'})
    except Exception as e:
        logger.error(f"Failed to commit title update retry changes: {e}", extra={'emoji_type': 'error'})
        return False
    
    return successful_retries > 0

def get_jellyfin_file_path(item_id: str, user_id: Optional[str] = None) -> str:
    if not settings.jellyfin_enabled or not settings.JELLYFIN_URL:
        return ''
    """
    Retrieve the absolute filesystem path for a given Jellyfin item ID.

    You must supply the Jellyfin User ID to fetch disk paths. If not provided,
    it auto-discovers via the Users endpoint.
    """
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

def get_jellyfin_file_path(item_id: str, user_id: Optional[str] = None) -> str:
    """
    Return the file path for a Jellyfin item, optionally for a specific user.
    """
    logger.debug(f"Getting file path for Jellyfin item {item_id}", extra={'emoji_type': 'debug'})
    try:
        if user_id:
            url = build_jellyfin_url(f"Users/{user_id}/Items/{item_id}")
        else:
            url = build_jellyfin_url(f"Items/{item_id}")
        resp = session.get(url)
        if resp.status_code == 200:
            item = resp.json()
            return item.get("Path", "")
        else:
            logger.warning(f"get_jellyfin_file_path: Failed to fetch item {item_id} (status {resp.status_code})", extra={'emoji_type': 'warning'})
            return ""
    except Exception as ex:
        logger.error(f"get_jellyfin_file_path: Exception for item {item_id}: {ex}", extra={'emoji_type': 'error'})
        return ""
