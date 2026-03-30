from __future__ import annotations

import os

import requests

from core.config import settings
from core.logger import logger


def _build_url(endpoint: str) -> str:
    """Build Emby API URL, applying the /emby/ prefix that Emby servers require.

    The configured EMBY_URL may or may not already end with /emby — this helper
    avoids doubling the prefix in either case.
    """
    base = str(getattr(settings, "EMBY_URL", "") or "").rstrip("/")
    clean = endpoint.lstrip("/")
    if base.endswith("/emby"):
        return f"{base}/{clean}"
    return f"{base}/emby/{clean}"


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "X-Emby-Token": getattr(settings, "EMBY_TOKEN", "") or "",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    return s


def _post_media_updated(sess: requests.Session, paths: list[str], update_type: str = "Created") -> bool:
    """Post to Library/Media/Updated, falling back to Library/Refresh on failure."""
    try:
        url = _build_url("Library/Media/Updated")
        payload = {"Updates": [{"Path": p, "UpdateType": update_type} for p in paths]}
        r = sess.post(url, json=payload, timeout=10)
        if r.status_code in (200, 204):
            return True
        logger.debug(f"Emby Library/Media/Updated returned {r.status_code}: {r.text}")
    except Exception as ex:
        logger.debug(f"Emby Library/Media/Updated failed: {ex}")

    # Fallback: general library refresh
    try:
        url = _build_url("Library/Refresh")
        primary = paths[0] if paths else ""
        r2 = sess.post(url, json={"Path": primary}, timeout=10)
        if r2.status_code in (200, 204):
            return True
        logger.debug(f"Emby Library/Refresh returned {r2.status_code}: {r2.text}")
    except Exception as ex:
        logger.debug(f"Emby Library/Refresh failed: {ex}")

    return False


def refresh_emby_paths(folders: set[str]) -> dict[str, int]:
    """Notify Emby about changed folders via Library/Media/Updated (batched single POST)."""
    if not getattr(settings, "emby_enabled", False):
        return {"refreshed": 0, "failed": 0}
    if not folders:
        return {"refreshed": 0, "failed": 0}

    folder_list = sorted(folders)
    abs_folders = [os.path.abspath(f) for f in folder_list]
    sess = _session()

    logger.info(
        f"Emby scan triggered for {len(abs_folders)} item(s) in single batched request.",
        extra={"emoji_type": "refresh"},
    )

    # Send all changed folders in a single aggregated POST to Library/Media/Updated.
    # This is more efficient than per-folder requests and allows Emby to prioritize
    # indexing updates in a single operation.
    ok = _post_media_updated(sess, abs_folders, update_type="Created")
    
    if ok:
        logger.info(
            f"Emby batched scan request accepted for {len(abs_folders)} item(s): {', '.join(abs_folders)}",
            extra={"emoji_type": "success"},
        )
        return {"refreshed": len(abs_folders), "failed": 0}
    else:
        logger.warning(
            f"Emby batched scan request failed for {len(abs_folders)} item(s).",
            extra={"emoji_type": "warning"},
        )
        return {"refreshed": 0, "failed": len(abs_folders)}


def refresh_emby_sections(has_movies: bool, has_episodes: bool) -> dict[str, int]:
    """Trigger an Emby library scan for movie and/or TV root folders."""
    if not getattr(settings, "emby_enabled", False):
        return {"refreshed": 0, "failed": 0}

    roots: list[str] = []
    if has_movies:
        for key in ("MOVIE_LIBRARY_FOLDER", "MOVIE_LIBRARY_4K_FOLDER"):
            val = getattr(settings, key, None)
            if val:
                roots.append(val)
    if has_episodes:
        for key in ("TV_LIBRARY_FOLDER", "TV_LIBRARY_4K_FOLDER"):
            val = getattr(settings, key, None)
            if val:
                roots.append(val)

    if not roots:
        return {"refreshed": 0, "failed": 0}

    refreshed = 0
    failed = 0
    sess = _session()

    for root in dict.fromkeys(roots):
        try:
            ok = _post_media_updated(sess, [root])
            if ok:
                refreshed += 1
                logger.info(
                    f"Emby library scan triggered for root: {root}",
                    extra={"emoji_type": "refresh"},
                )
            else:
                failed += 1
                logger.warning(
                    f"Emby library scan failed for root={root}",
                    extra={"emoji_type": "warning"},
                )
        except Exception as e:
            failed += 1
            logger.warning(
                f"Emby section refresh failed for root={root}: {e}",
                extra={"emoji_type": "warning"},
            )

    return {"refreshed": refreshed, "failed": failed}


def refresh_emby_item_metadata(item_id: str) -> bool:
    """Trigger metadata refresh for a specific Emby item id."""
    if not getattr(settings, "emby_enabled", False):
        return False

    target = str(item_id or "").strip()
    if not target:
        return False

    sess = _session()
    endpoint = _build_url(f"Items/{target}/Refresh")
    params = {
        "MetadataRefreshMode": "Default",
        "ImageRefreshMode": "None",
        "ReplaceAllMetadata": "false",
        "ReplaceAllImages": "false",
    }
    try:
        resp = sess.post(endpoint, params=params, timeout=15)
        if resp.status_code in (200, 204):
            return True
        logger.warning(
            f"Emby item refresh returned {resp.status_code} for item_id={target}: {resp.text}",
            extra={"emoji_type": "warning"},
        )
    except Exception as e:
        logger.warning(
            f"Emby item refresh failed for item_id={target}: {e}",
            extra={"emoji_type": "warning"},
        )
    return False
