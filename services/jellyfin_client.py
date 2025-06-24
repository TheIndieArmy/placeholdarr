import os
from typing import Optional
from urllib.parse import quote
import requests
from core.config import settings
from core.logger import logger

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

def update_jellyfin_title_status(media_type=None, item_id=None, title=None, status=None, season=None, episode=None, year=None, **kwargs):
    if not settings.jellyfin_enabled or not settings.JELLYFIN_URL:
        return False
    """
    Update the display name and summary (Overview) of a Jellyfin item.
    """
    try:
# Ensure item_id is a valid Jellyfin GUID, not a TMDB/TVDB ID
        # If item_id is numeric (e.g., TMDB/TVDB), this will fail with 400
        if not item_id or not isinstance(item_id, str) or len(item_id) < 8:
            logger.error(f"Invalid or missing Jellyfin item_id: {item_id}", extra={'emoji_type': 'error'})
            return False

        url = build_jellyfin_url(f"Items/{item_id}")
        # Fetch current item info for summary
        resp = session.get(url)
        resp.raise_for_status()
        item = resp.json()
        current_summary = item.get('Overview', '') or ''
        new_summary = _prepend_status_to_summary(current_summary, status)
        # Always update title if provided
        payload = {}
        if title:
            payload["Name"] = title
        payload["Overview"] = new_summary
        resp2 = session.post(url, json=payload)
        resp2.raise_for_status()
        logger.info(f"Updated item {item_id} title to '{title}' and summary to '{new_summary}'", extra={'emoji_type': 'update'})
        return True
    except Exception as ex:
        logger.error(f"Title/summary update failed: {ex}", extra={'emoji_type': 'error'})
    return False

# Legacy alias for backward compatibility
update_jellyfin_title = update_jellyfin_title_status

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


def find_jellyfin_item_id(media_type, external_id, title, season=None, episode=None, year=None, **kwargs):
    """
    Find the Jellyfin item GUID by TMDB/TVDB ID or title (and optionally season/episode).
    Returns the Jellyfin item ID (GUID) or None if not found.
    """
    try:
        # Build filter params for /Items endpoint
        params = {}
        if media_type == "movie":
            if external_id:
                params["ExternalId"] = str(external_id)
            if title:
                params["SearchTerm"] = title
            if year:
                params["Years"] = year
        elif media_type == "tv":
            if external_id:
                params["ExternalId"] = str(external_id)
            if title:
                params["SearchTerm"] = title
            if year:
                params["Years"] = year
        url = build_jellyfin_url("Items")
        resp = session.get(url, params=params)
        resp.raise_for_status()
        items = resp.json().get("Items", [])
        if not items:
            logger.error(f"No Jellyfin item found for {media_type} {external_id} ({title})", extra={'emoji_type': 'error'})
            return None
        # For episodes, further filter by season/episode if provided
        if media_type == "tv" and season and episode:
            for item in items:
                if (item.get("SeasonNumber") == int(season) and item.get("IndexNumber") == int(episode)):
                    return item.get("Id")
        # Otherwise, return the first match
        return items[0].get("Id")
    except Exception as ex:
        logger.error(f"Failed to find Jellyfin item ID: {ex}", extra={'emoji_type': 'error'})
        return None


def test_jellyfin_connection() -> bool:
    """
    Test connectivity to the Jellyfin server by fetching public system info.
    """
    url = build_jellyfin_url('System/Info/Public')
    try:
        resp = session.get(url)
        resp.raise_for_status()
        logger.info("Connected to Jellyfin server.", extra={'emoji_type': 'info'})
        return True
    except Exception as ex:
        logger.error(f"Failed to connect to Jellyfin: {ex}", extra={'emoji_type': 'error'})
        return False

# Run a quick test at import time (optional)
if getattr(settings, "jellyfin_enabled", False):
    test_jellyfin_connection()
