import os
from typing import Optional
from urllib.parse import quote
import requests
from core.config import settings
from core.logger import logger
import re

# Initialize a shared session with default headers
session = requests.Session()
session.headers.update({
    'X-MediaBrowser-Token': settings.JELLYFIN_TOKEN,
    'Content-Type': 'application/json'
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


def refresh_jellyfin_item(path: str, update_type: str = 'Created') -> bool:
    if not settings.jellyfin_enabled or not settings.JELLYFIN_URL:
        return False
    """
    Trigger a scan/update for a specific file or directory.

    Args:
        path (str): Absolute filesystem path to file or folder.
        update_type (str): One of 'Created', 'Changed', 'Deleted', or 'None'.

    Returns:
        bool: True if Jellyfin accepted the update (HTTP 204), False otherwise.
    """
    target = os.path.dirname(path) if os.path.isfile(path) else path
    payload = {"Updates": [{"Path": target, "UpdateType": update_type}]}
    url = build_jellyfin_url('Library/Media/Updated')
    try:
        resp = session.post(url, json=payload)
        if resp.status_code == 204:
            logger.info(f"Triggered scan for: {target}", extra={'emoji_type': 'refresh'})
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

def get_first_jellyfin_user_id():
    url = build_jellyfin_url("Users")
    try:
        resp = session.get(url)
        resp.raise_for_status()
        users = resp.json()
        user_id = None
        # Prefer admin user, but fallback to first user if no admin found
        for u in users:
            policy = u.get('Policy', {})
            if policy.get('IsAdministrator'):
                user_id = u.get('Id')
                break
        if not user_id and users:
            logger.warning(f"No admin user found. Users returned: {users}", extra={'emoji_type': 'warning'})
            user_id = users[0].get('Id')
            logger.warning("No admin user found in Jellyfin Users list, using first user as fallback.", extra={'emoji_type': 'warning'})
        if not user_id:
            logger.error(f"No valid user found in Jellyfin Users list. Full users response: {users}", extra={'emoji_type': 'error'})
            return None
        return user_id
    except Exception as ex:
        logger.error(f"Failed to fetch Jellyfin users: {ex}", extra={'emoji_type': 'error'})
        return None

def fetch_full_episode_object(item_id):
    """
    Fetch the full episode object from Jellyfin using the only known working method: /Users/{userId}/Items/{id}.
    """
    user_id = get_first_jellyfin_user_id()
    if not user_id:
        logger.error(f"No valid Jellyfin user ID found for fetch.", extra={'emoji_type': 'error'})
        return None
    user_url = build_jellyfin_url(f"Users/{user_id}/Items/{item_id}")
    user_resp = session.get(user_url)
    if user_resp.status_code == 200:
        logger.debug(f"Fetched episode object via /Users/{{userId}}/Items/{{id}}", extra={'emoji_type': 'debug'})
        return user_resp.json()
    else:
        logger.error(f"/Users/{{userId}}/Items/{{id}} failed: {user_resp.status_code} {user_resp.text}", extra={'emoji_type': 'error'})
        return None

def fetch_full_movie_object(item_id):
    """
    Fetch the full movie object from Jellyfin using the only known working method: /Users/{userId}/Items/{id}.
    """
    user_id = get_first_jellyfin_user_id()
    if not user_id:
        logger.error(f"No valid Jellyfin user ID found for fetch.", extra={'emoji_type': 'error'})
        return None
    user_url = build_jellyfin_url(f"Users/{user_id}/Items/{item_id}")
    user_resp = session.get(user_url)
    if user_resp.status_code == 200:
        logger.debug(f"Fetched movie object via /Users/{{userId}}/Items/{{id}}", extra={'emoji_type': 'debug'})
        return user_resp.json()
    else:
        logger.error(f"/Users/{{userId}}/Items/{{id}} failed: {user_resp.status_code} {user_resp.text}", extra={'emoji_type': 'error'})
        return None

def update_jellyfin_title_status(media_type=None, item_id=None, title=None, status=None, season=None, episode=None, year=None, **kwargs):
    logger.info(f"[Jellyfin Update] Called with: media_type={media_type}, item_id={item_id}, title={title}, status={status}, season={season}, episode={episode}, year={year}, kwargs={kwargs}", extra={'emoji_type': 'debug'})
    if not settings.jellyfin_enabled or not settings.JELLYFIN_URL:
        logger.warning(f"[Jellyfin Update] Skipping: Jellyfin not enabled or URL missing.", extra={'emoji_type': 'warning'})
        return False
    try:
        if not item_id or not isinstance(item_id, str) or len(item_id) < 8:
            logger.error(f"[Jellyfin Update] Invalid or missing Jellyfin item_id: {item_id}", extra={'emoji_type': 'error'})
            return False
        if media_type == "tv":
            full_obj = fetch_full_episode_object(item_id)
        elif media_type == "movie":
            full_obj = fetch_full_movie_object(item_id)
        else:
            logger.error(f"[Jellyfin Update] Only TV episode and movie updates are supported in robust mode.", extra={'emoji_type': 'error'})
            return False
        if full_obj:
            current_overview = full_obj.get('Overview', '') or ''
            new_overview = _prepend_status_to_summary(current_overview, status)
            if title:
                full_obj["Name"] = title
            full_obj["Overview"] = new_overview
            url = build_jellyfin_url(f"Items/{item_id}")
            logger.info(f"[Jellyfin Update] POSTing full object to {url}", extra={'emoji_type': 'debug'})
            resp = session.post(url, json=full_obj)
            if resp.status_code in (200, 204):
                logger.info(f"[Jellyfin Update] SUCCESS: Updated item {item_id} title to '{title}' and overview.", extra={'emoji_type': 'update'})
                return True
            else:
                logger.error(f"[Jellyfin Update] FAIL: POST status {resp.status_code}: {resp.text}", extra={'emoji_type': 'error'})
        else:
            logger.error(f"[Jellyfin Update] Could not fetch full object for POST.", extra={'emoji_type': 'error'})
        return False
    except Exception as ex:
        logger.error(f"[Jellyfin Update] EXCEPTION: Title/overview update failed: {ex}", extra={'emoji_type': 'error'})
    return False

def find_jellyfin_episode_id(series_tvdb_id, series_title, season_num, episode_num):
    """
    Robustly find a Jellyfin episode item ID by searching all series matching TVDB or title, and all their episodes.
    """
    url = build_jellyfin_url("Items")
    params = {"IncludeItemTypes": "Series", "Recursive": "true", "Fields": "ProviderIds,Name"}
    resp = session.get(url, params=params)
    resp.raise_for_status()
    items = resp.json().get("Items", [])
    # Collect all matching series IDs
    matching_series = []
    if series_tvdb_id:
        for item in items:
            prov = item.get("ProviderIds")
            if prov and str(prov.get("Tvdb")) == str(series_tvdb_id):
                matching_series.append(item)
    if not matching_series and series_title:
        for item in items:
            if item.get("Name", "").strip().lower() == series_title.strip().lower():
                matching_series.append(item)
    if not matching_series:
        logger.error(f"[Jellyfin Lookup] Could not find series for {series_title} (TVDB: {series_tvdb_id})", extra={'emoji_type': 'error'})
        return None
    # For each matching series, search for the episode
    for series in matching_series:
        series_id = series.get("Id")
        episode_params = {
            "ParentId": series_id,
            "IncludeItemTypes": "Episode",
            "Recursive": "true",
            "Fields": "ProviderIds,Name,SeasonNumber,IndexNumber,Path,Overview"
        }
        resp = session.get(url, params=episode_params)
        resp.raise_for_status()
        episodes = resp.json().get("Items", [])
        for ep in episodes:
            if ep.get("SeasonNumber") == int(season_num) and ep.get("IndexNumber") == int(episode_num):
                return ep.get("Id")
        for ep in episodes:
            path = ep.get("Path") or ep.get("Name") or ""
            m = re.search(r"[sS](\d{2})[eE](\d{2})", path)
            if m:
                s, e = map(int, m.groups())
                if s == int(season_num) and e == int(episode_num):
                    return ep.get("Id")
    logger.error(f"[Jellyfin Lookup] Could not find episode S{season_num}E{episode_num} for series {series_title}", extra={'emoji_type': 'error'})
    return None

def find_jellyfin_item_id(media_type, external_id, title, season=None, episode=None, year=None, **kwargs):
    if media_type == "tv" and season and episode:
        return find_jellyfin_episode_id(external_id, title, season, episode)
    elif media_type == "movie":
        url = build_jellyfin_url("Items")
        params = {"IncludeItemTypes": "Movie", "Recursive": "true", "Fields": "ProviderIds,Name,ProductionYear"}
        resp = session.get(url, params=params)
        resp.raise_for_status()
        items = resp.json().get("Items", [])
        # Prefer TMDB/IMDB/TVDB ID match
        if external_id:
            for item in items:
                prov = item.get("ProviderIds")
                if prov and (str(prov.get("Tmdb")) == str(external_id) or str(prov.get("Imdb")) == str(external_id) or str(prov.get("Tvdb")) == str(external_id)):
                    return item.get("Id")
        # Fallback: match by title and year
        if title:
            for item in items:
                if item.get("Name", "").strip().lower() == title.strip().lower():
                    if not year or not item.get("ProductionYear") or int(item.get("ProductionYear")) == int(year):
                        return item.get("Id")
        logger.error(f"[Jellyfin Lookup] Could not find movie for {title} (TMDB/IMDB/TVDB: {external_id}, Year: {year})", extra={'emoji_type': 'error'})
        return None
    logger.error(f"[Jellyfin Lookup] Only robust TV episode and movie lookup is supported in this mode.", extra={'emoji_type': 'error'})
    return None

def test_jellyfin_connection() -> bool:
    """Test connectivity to the Jellyfin server by fetching public system info."""
    url = build_jellyfin_url('System/Info/Public')
    try:
        resp = session.get(url)
        resp.raise_for_status()
        return True
    except Exception as ex:
        return False

def test_jellyfin_endpoints():
    """Test key Jellyfin API endpoints needed for operation."""
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
