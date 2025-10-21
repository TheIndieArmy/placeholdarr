import os
import time
from typing import Optional, Any, Callable
import requests
from urllib.parse import quote_plus
from core.config import settings
from core.logger import logger

# Shared session for Emby
session = requests.Session()
session.headers.update({
    'X-Emby-Token': settings.EMBY_TOKEN,
    'Accept': 'application/json',
    'Content-Type': 'application/json',
})


def build_emby_url(endpoint: str) -> str:
    """
    Build a complete Emby API URL. Emby often uses the /emby/ prefix on the server.
    The provided EMBY_URL may or may not include the /emby prefix; this helper makes a best-effort.
    """
    base = settings.EMBY_URL.rstrip('/') if settings.EMBY_URL else ''
    clean = endpoint.lstrip('/')
    # If base already ends with /emby or the endpoint starts with emby, avoid duplication
    if base.endswith('/emby'):
        url = f"{base}/{clean}"
    else:
        # Compose with /emby/ to match Emby server API path
        url = f"{base}/emby/{clean}"
    logger.debug(f"Built Emby URL: {url}", extra={'emoji_type': 'debug'})
    return url


def get_emby_file_path(item_id: str, user_id: Optional[str] = None) -> str:
    """
    Retrieve an absolute filesystem path for an Emby item ID.

    If `user_id` is not supplied, this function will attempt to discover an admin user via /emby/Users.
    Returns an empty string on failure.
    """
    if not getattr(settings, 'emby_enabled', False) or not settings.EMBY_URL:
        return ''

    # Discover admin user if necessary
    if not user_id:
        try:
            users_url = build_emby_url('Users')
            resp = session.get(users_url, timeout=5)
            resp.raise_for_status()
            users = resp.json()
            for u in users:
                policy = u.get('Policy', {})
                if policy.get('IsAdministrator'):
                    user_id = u.get('Id')
                    break
            if not user_id:
                logger.error('No admin user found in Emby Users list', extra={'emoji_type': 'error'})
                return ''
        except Exception as ex:
            logger.error(f'Failed to fetch Emby users: {ex}', extra={'emoji_type': 'error'})
            return ''

    # Fetch item details under user context to obtain Path
    try:
        url = build_emby_url(f'Users/{user_id}/Items/{item_id}?fields=Path')
        resp = session.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        # Emby may include Path or MediaSources -> Path
        path = data.get('Path') or ''
        if not path:
            # Try MediaSources
            msrc = data.get('MediaSources') or []
            if msrc and isinstance(msrc, list):
                for s in msrc:
                    p = s.get('Path') or s.get('DisplayPath')
                    if p:
                        path = p
                        break
        if path:
            logger.info(f"Retrieved path for Emby item {item_id}: {path}", extra={'emoji_type': 'info'})
            return path
        logger.warning(f"Emby item {item_id} did not include a Path field", extra={'emoji_type': 'warning'})
    except Exception as ex:
        logger.error(f"get_emby_file_path: Exception for item {item_id}: {ex}", extra={'emoji_type': 'error'})

    return ''


def test_emby_connection() -> bool:
    """Simple connectivity test to Emby server."""
    url = build_emby_url('System/Info/Public')
    try:
        resp = session.get(url, timeout=5)
        resp.raise_for_status()
        return True
    except Exception:
        return False


# Run a quick connection test at import time if enabled
if getattr(settings, 'emby_enabled', False):
    try:
        if test_emby_connection():
            logger.info('Connected to Emby server', extra={'emoji_type': 'success'})
        else:
            logger.error('Failed to connect to Emby server', extra={'emoji_type': 'error'})
    except Exception as ex:
        logger.error(f'Failed to connect to Emby server: {ex}', extra={'emoji_type': 'error'})


def refresh_emby_item(path: str, update_type: str = 'Created') -> bool:
    """Best-effort: find an Emby item matching `path` (file or folder) and trigger a refresh for that item.
    Falls back to attempting a library-level refresh if item-level refresh fails.
    Returns True on success, False otherwise.
    """
    if not getattr(settings, 'emby_enabled', False) or not settings.EMBY_URL:
        return False

    # Build candidate paths to send to Emby. Avoid calling realpath() on
    # raw UNC or Windows-style paths because that can prefix the local CWD
    # (breaking matching on the server). Instead preserve the original
    # input and also send a couple common variants (forward-slash, normpath)
    # so Emby has the best chance to match the configured library folder.
    original = path
    if os.path.isfile(path):
        candidate = os.path.dirname(path)
    else:
        candidate = path

    candidates = []
    try:
        # Raw candidate as provided
        if candidate:
            candidates.append(candidate)

        # Add a forward-slash variant for servers that store paths with '/'
        if '\\' in candidate:
            alt = candidate.replace('\\', '/')
            if alt and alt not in candidates:
                candidates.append(alt)

        # Add a platform-normalized variant (without resolving symlinks)
        try:
            norm = os.path.normpath(candidate)
            if norm and norm not in candidates:
                candidates.append(norm)
        except Exception:
            pass
    except Exception:
        # If anything goes wrong building candidates, fall back to original
        candidates = [original]

    # Deduplicate while preserving order
    seen = set()
    uniq = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)

    if not uniq:
        logger.debug('No valid Emby path candidate was produced', extra={'emoji_type': 'debug'})
        return False

    # First, attempt targeted update notification (Library/Media/Updated)
    try:
        upd_ep = 'Library/Media/Updated'
        payload = {'Updates': [{'Path': c, 'UpdateType': update_type} for c in uniq]}
        logger.debug(f"Posting Emby Media/Updated payload: {payload}", extra={'emoji_type': 'debug'})
        r = session.post(build_emby_url(upd_ep), json=payload, timeout=10)
        if r.status_code in (200, 204):
            logger.info(f"Emby media update posted for {uniq}", extra={'emoji_type': 'refresh'})
            return True
        else:
            logger.debug(f"Emby media update {upd_ep} returned {r.status_code}: {r.text}")
    except Exception as ex:
        logger.debug(f"Emby media update attempt failed: {ex}")

    # Fallback: general library refresh
    try:
        lib_ep = 'Library/Refresh'
        fallback_path = uniq[0] if uniq else original
        r2 = session.post(build_emby_url(lib_ep), json={'Path': fallback_path}, timeout=10)
        if r2.status_code in (200, 204):
            logger.info(f"Emby library refresh queued for {fallback_path}", extra={'emoji_type': 'refresh'})
            return True
        else:
            logger.debug(f"Emby library refresh {lib_ep} returned {r2.status_code}: {r2.text}")
            return False
    except Exception as ex:
        logger.debug(f"Emby library refresh attempt failed: {ex}")
        return False
