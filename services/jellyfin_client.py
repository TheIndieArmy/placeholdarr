import os
import re
import time
from typing import List, Optional, Any, Callable
from urllib.parse import quote
import requests
from core.config import settings
from core.logger import logger
from services.utils import strip_status_markers
from urllib.parse import quote_plus
import inspect

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

        # success?
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
    retry_interval: int = 30,
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

    if media_type == "tv":
        start = time.time()
        updated_any = False

        # Track what we've already updated
        processed_series = set()               # series IDs
        processed_seasons = set()              # season IDs
        processed_episodes = set()             # (series_id, season_id, episode_number) tuples

        while time.time() - start < retry_timeout:
            # 1) discover matching series duplicates
            clean_title = re.sub(r'\s*\(\d{4}\)$', '', title)
            series_url = build_jellyfin_url(
                f"Users/{user_id}/Items?searchTerm={quote_plus(clean_title)}"
                "&includeItemTypes=Series&recursive=true&fields=ProviderIds,Name,Overview"
            )

            def fetch_series():
                return session.get(series_url, timeout=5).json().get("Items", [])

            def pick_series(items: List[dict]) -> List[dict]:
                return [
                    s for s in (items or [])
                    if s.get("ProviderIds", {}).get("Tvdb")
                    and (not media_id or int(s["ProviderIds"]["Tvdb"]) == media_id)
                    and s["Id"] not in processed_series
                ]

            series_list = retry_call(
                func=fetch_series,
                on_error=lambda ex: logger.error(f"Series search error: {ex}"),
                retry_interval=retry_interval,
                retry_timeout=retry_interval,
                success_condition=lambda res: bool(pick_series(res))
            ) or []
            candidates = pick_series(series_list)
            logger.debug(f"Discovered {len(candidates)} new series duplicates")

            # 1a) update metadata for each new series
            for s in candidates:
                sid = s["Id"]
                orig = strip_status_markers(s.get("Name", title))
                new_name = f"{orig} - [{status}]" if status else orig
                new_ovr = _prepend_status_to_summary(s.get("Overview", ""), status)
                if update_item_metadata(sid, new_name, new_ovr, user_id, retry_interval, retry_timeout):
                    updated_any = True
                    processed_series.add(sid)

            # 2) discover seasons and build pending slots
            pending = []
            for s in candidates:
                sid = s["Id"]
                seas_url = build_jellyfin_url(
                    f"Users/{user_id}/Items?ParentId={sid}"
                    "&IncludeItemTypes=Season&Recursive=false&fields=Name,Overview,IndexNumber"
                )

                def fetch_seasons():
                    return session.get(seas_url, timeout=5).json().get("Items", [])

                def pick_seasons(items: List[dict]) -> List[dict]:
                    return [
                        ss for ss in (items or [])
                        if ss.get("IndexNumber") == season
                        and ss["Id"] not in processed_seasons
                    ]

                seasons = retry_call(
                    func=fetch_seasons,
                    on_error=lambda ex: logger.error(f"Season list error for series {sid}: {ex}"),
                    retry_interval=retry_interval,
                    retry_timeout=retry_interval,
                    success_condition=lambda res: bool(pick_seasons(res))
                ) or []

                matched_seasons = pick_seasons(seasons)
                logger.debug(f"Series {sid} has {len(matched_seasons)} new matching seasons")
                for ss in matched_seasons:
                    seas_id = ss["Id"]
                    # update season metadata once
                    orig_s = strip_status_markers(ss.get("Name", f"Season {season}"))
                    new_s = f"{orig_s} - [{status}]" if status else orig_s
                    ovr_s = _prepend_status_to_summary(ss.get("Overview", ""), status)
                    if update_item_metadata(seas_id, new_s, ovr_s, user_id, retry_interval, retry_timeout):
                        updated_any = True
                        processed_seasons.add(seas_id)

                    # queue for episode update
                    pending.append((sid, seas_id))

            if not pending:
                logger.debug(f"No new seasons found yet, retrying after {retry_interval}s")
                time.sleep(retry_interval)
                continue

            # 3) for each pending, try to find and update episode
            for sid, seas_id in list(pending):
                epi_url = build_jellyfin_url(
                    f"Users/{user_id}/Items?ParentId={seas_id}"
                    "&IncludeItemTypes=Episode&Recursive=false&fields=Name,Overview,IndexNumber"
                )
                try:
                    eps = session.get(epi_url, timeout=5).json().get("Items", [])
                except Exception as ex:
                    logger.error(f"Episode list error for season {seas_id}: {ex}")
                    continue

                ep_matches = [
                    e for e in eps
                    if e.get("IndexNumber") == episode
                    and (sid, seas_id, episode) not in processed_episodes
                ]
                if not ep_matches:
                    logger.debug(f"Series {sid}, season {seas_id} missing episode {episode}")
                    continue

                for epm in ep_matches:
                    orig_e = strip_status_markers(epm.get("Name", title))
                    new_e = f"{orig_e} - [{status}]" if status else orig_e
                    ovr_e = _prepend_status_to_summary(epm.get("Overview", ""), status)
                    if update_item_metadata(epm["Id"], new_e, ovr_e, user_id, retry_interval, retry_timeout):
                        updated_any = True
                        processed_episodes.add((sid, seas_id, episode))

                # remove this slot whether updated or not to avoid infinite loop
                pending.remove((sid, seas_id))
                logger.debug(f"Removed season {seas_id} from queue, {len(pending)} slots left")

            if not pending:
                break

            logger.debug(f"Sleeping {retry_interval}s before next pass")
            time.sleep(retry_interval)

        if pending:
            logger.warning(f"⚠️ Timeout; pending slots: {pending}")
        return updated_any
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
    except Exception:
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

