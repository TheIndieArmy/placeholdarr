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
    base = settings.JELLYFIN_URL.rstrip('/')
    clean = endpoint.lstrip('/')
    url = f"{base}/{clean}"
    logger.debug(f"Built Jellyfin URL: {url}", extra={'emoji_type': 'debug'})
    return url


def refresh_jellyfin_item(path: str, update_type: str = 'Created') -> bool:
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


def update_jellyfin_title_status(item_id: str, new_title: str) -> bool:
    """
    Update the display name of a Jellyfin item.

    Args:
        item_id (str): GUID of the item to rename.
        new_title (str): New name to set.

    Returns:
        bool: True if update succeeded (HTTP 200), False otherwise.
    """
    url = build_jellyfin_url(f"Items/{item_id}")
    payload = {"Name": new_title}
    try:
        resp = session.post(url, json=payload)
        resp.raise_for_status()
        logger.info(f"Updated item {item_id} title to '{new_title}'", extra={'emoji_type': 'update'})
        return True
    except Exception as ex:
        logger.error(f"Title update failed: {ex}", extra={'emoji_type': 'error'})
    return False

# Legacy alias for backward compatibility
update_jellyfin_title = update_jellyfin_title_status

def get_jellyfin_file_path(item_id: str, user_id: Optional[str] = None) -> str:
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
test_jellyfin_connection()
