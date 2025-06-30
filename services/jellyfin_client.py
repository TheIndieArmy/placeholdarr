import os
import re
import time
from typing import Optional, Dict, Any, Callable
from urllib.parse import quote
import requests
from core.config import settings
from core.logger import logger
from services.utils import strip_status_markers
from urllib.parse import quote_plus

# Initialize a shared session with default headers
session = requests.Session()
session.headers.update({
    'X-Emby-Token': settings.JELLYFIN_TOKEN,
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

def retry_call(
    func: Callable[[], Any],
    on_error: Callable[[Exception], None],
    retry_interval: int,
    retry_timeout: int,
    success_condition: Callable[[Any], bool]
) -> Any:
    start = time.time()
    last_result = None
    attempt = 0

    while time.time() - start < retry_timeout:
        attempt += 1
        try:
            result = func()
        except Exception as ex:
            on_error(ex)
            result = None

        last_result = result

        # <-- DEBUG LOGGING ON EVERY ATTEMPT -->
        logger.debug(f"🔁 retry_call attempt {attempt}, result={result!r}")

        if success_condition(result):
            logger.debug(f"✅ retry_call succeeded on attempt {attempt}")
            return result

        time.sleep(retry_interval)

    logger.warning(f"⚠️ retry_call timed out after {attempt} attempts, last_result={last_result!r}")
    return last_result

def update_item_metadata(
    item_id: str,
    new_name: str,
    new_overview: str,
    user_id: str,
    retry_interval: int,
    retry_timeout: int,
    verify_delay: int = 60
) -> bool:
    """
    Fetch item DTO, update Name & Overview with retry, then verify post-update.
    If verification fails after verify_delay, re-attempt update once.
    """
    dto_url = build_jellyfin_url(f"Items/{item_id}?userId={user_id}")

    # Fetch current DTO
    try:
        dto = session.get(dto_url, timeout=5).json()
    except Exception as ex:
        logger.error(f"❌ Fetch for update failed for item {item_id}: {ex}", extra={"emoji_type": "error"})
        return False

    original = dto.get("Name", "<unknown>")
    dto["Name"] = new_name
    dto["Overview"] = new_overview

    # POST update with retry
    def do_post():
        session.post(dto_url, json=dto, timeout=5).raise_for_status()
        return True

    def post_error(ex: Exception):
        logger.error(f"❌ POST update failed for item {item_id}: {ex}", extra={"emoji_type": "error"})

    updated = retry_call(
        func=do_post,
        on_error=post_error,
        retry_interval=retry_interval,
        retry_timeout=retry_timeout,
        success_condition=lambda res: res is True
    )

    if not updated:
        return False

    logger.info(f"🔄 Updated item {item_id} ('{original}' -> '{new_name}')", extra={"emoji_type": "update"})

    # Verification: wait then fetch to confirm
    time.sleep(verify_delay)
    try:
        verified = session.get(dto_url, timeout=5).json().get("Name") == new_name
    except Exception as ex:
        logger.error(f"❌ Verification fetch failed for item {item_id}: {ex}", extra={"emoji_type": "error"})
        verified = False

    if not verified:
        logger.warning(f"⚠️ Verification failed for item {item_id}, retrying update", extra={"emoji_type": "warn"})
        # Attempt one more update
        retry_call(
            func=do_post,
            on_error=post_error,
            retry_interval=retry_interval,
            retry_timeout=retry_timeout,
            success_condition=lambda res: res is True
        )
        logger.info(f"🔄 Retried update for item {item_id} ('{original}' -> '{new_name}')", extra={"emoji_type": "update"})
    return True

def update_jellyfin_title_status(
    media_type: str,
    media_id: Optional[int],
    title: str,
    status: Optional[str] = None,
    year: Optional[int] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    retry_interval: int = 15,
    retry_timeout: int = 600
) -> bool:
    # 1) discover admin user
    try:
        users = session.get(build_jellyfin_url("Users"), timeout=5).json()
        admin = next(u for u in users if u.get("Policy", {}).get("IsAdministrator"))
        user_id = admin["Id"]
    except Exception as ex:
        logger.error(f"❌ Cannot find admin user: {ex}", extra={"emoji_type": "error"})
        return False

    def not_empty_list(res):
        return bool(res and isinstance(res, list) and len(res) > 0)

    if media_type == "movie":
        params = {
            "searchTerm": title,
            "includeItemTypes": "Movie",
            "recursive": "true",
            "fields": "ProviderIds,Name,ProductionYear,Overview"
        }
        qs = "&".join(f"{k}={quote_plus(str(v))}" for k, v in params.items())
        search_url = build_jellyfin_url(f"Users/{user_id}/Items?{qs}")

        def fetch_movies():
            return session.get(search_url, timeout=5).json().get("Items", [])

        # def filter_movies(items):
        #     return [it for it in items or [] if (not media_id or int(it.get("ProviderIds", {}).get("Tmdb", -1)) == media_id)
        #             and (not year or int(it.get("ProductionYear", -1)) == year)]
        def filter_movies(items):
            return [
                it for it in (items or [])
                if (not media_id or (
                        int(it.get("ProviderIds", {}).get("Tmdb", -1)) == media_id
                        or str(it.get("Id", "")) == str(media_id)
                    )
                )
                and (not year or int(it.get("ProductionYear", -1)) == year)
            ]

        items = retry_call(
            func=fetch_movies,
            on_error=lambda ex: logger.error(f"❌ Movie search error: {ex}", extra={"emoji_type": "error"}),
            retry_interval=retry_interval,
            retry_timeout=retry_timeout,
            success_condition=lambda res: bool(filter_movies(res))
        )
        hits = filter_movies(items)
        if not hits:
            logger.error(f"❌ No movie match for '{title}' TMDb={media_id}", extra={"emoji_type": "error"})
            return False

        itm = hits[0]
        orig_name = strip_status_markers(itm.get("Name", title))
        new_name = f"{orig_name} - [{status}]" if status else orig_name
        new_ovr = _prepend_status_to_summary(itm.get("Overview", ""), status)
        return update_item_metadata(itm["Id"], new_name, new_ovr, user_id, retry_interval, retry_timeout)

    elif media_type == "tv":
        # Series level
        clean_title = re.sub(r'\s*\(\d{4}\)$', '', title)
        series_url = build_jellyfin_url(
            f"Users/{user_id}/Items?searchTerm={quote_plus(title)}&includeItemTypes=Series&recursive=true&fields=ProviderIds,Name,Overview"
        )

        def fetch_series():
            return session.get(series_url, timeout=5).json().get("Items", [])

        def pick_series(items):
            return next((s for s in items or []
                         if (not media_id or int(s.get("ProviderIds", {}).get("Tvdb", -1)) == media_id)), None)

        series = retry_call(
            func=fetch_series,
            on_error=lambda ex: logger.error(f"❌ Series search error: {ex}", extra={"emoji_type": "error"}),
            retry_interval=retry_interval,
            retry_timeout=retry_timeout,
            success_condition=lambda res: pick_series(res) is not None
        )
        series = pick_series(series)
        if not series:
            logger.error(f"❌ Series '{title}' TVDb={media_id} not found", extra={"emoji_type": "error"})
            return False

        sid = series["Id"]
        orig_s = strip_status_markers(series.get("Name", title))
        new_s = f"{orig_s} - [{status}]" if status else orig_s
        o_s = _prepend_status_to_summary(series.get("Overview", ""), status)
        if not update_item_metadata(sid, new_s, o_s, user_id, retry_interval, retry_timeout):
            return False

        # Season level
        if season is not None:
            seas_url = build_jellyfin_url(
                f"Users/{user_id}/Items?ParentId={sid}&IncludeItemTypes=Season&Recursive=false&fields=Name,Overview,IndexNumber"
            )
            seasons = retry_call(
                func=lambda: session.get(seas_url, timeout=5).json().get("Items", []),
                on_error=lambda ex: logger.error(f"❌ Season list error: {ex}", extra={"emoji_type": "error"}),
                retry_interval=retry_interval,
                retry_timeout=retry_timeout,
                success_condition=not_empty_list
            ) or []
            seas = next((ss for ss in seasons if ss.get("IndexNumber") == season), None)
            if not seas:
                logger.error(f"❌ Season {season} not found", extra={"emoji_type": "error"})
                return False
            if not update_item_metadata(
                seas["Id"],
                f"{strip_status_markers(seas.get('Name', f'Season {season}'))} - [{status}]" if status else strip_status_markers(seas.get('Name', f'Season {season}')),
                _prepend_status_to_summary(seas.get("Overview", ""), status),
                user_id, retry_interval, retry_timeout
            ):
                return False

        # Episode level
        if season is not None and episode is not None:
            epi_url = build_jellyfin_url(
                f"Users/{user_id}/Items?ParentId={seas['Id']}&IncludeItemTypes=Episode&Recursive=false&fields=Name,Overview,IndexNumber"
            )
            def fetch_eps():
                return session.get(epi_url, timeout=5).json().get("Items", [])

            def pick_ep(eps_list):
                return next((e for e in eps_list if e.get("IndexNumber") == episode), None)

            epm = retry_call(
                func=fetch_eps,
                on_error=lambda ex: logger.error(f"❌ Episode list error: {ex}", extra={"emoji_type": "error"}),
                retry_interval=retry_interval,
                retry_timeout=retry_timeout,
                success_condition=lambda lst: pick_ep(lst) is not None
            )
            epm = pick_ep(epm or [])
            if not epm:
                logger.error(f"❌ Episode S{season:02d}E{episode:02d} not found after retry", extra={"emoji_type": "error"})
                return False

            orig_e = strip_status_markers(epm.get("Name", title))
            new_e = f"{orig_e} - [{status}]" if status else orig_e
            o_e = _prepend_status_to_summary(epm.get("Overview", ""), status)
            return update_item_metadata(
                epm["Id"], new_e, o_e, user_id, retry_interval, retry_timeout
            )
    return True
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
