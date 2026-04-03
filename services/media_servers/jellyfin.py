from __future__ import annotations

import os
import time
from typing import Any

import requests

from core.config import settings
from core.logger import logger


def _build_url(endpoint: str) -> str:
    base = str(getattr(settings, "JELLYFIN_URL", "") or "").rstrip("/")
    return f"{base}/{endpoint.lstrip('/')}"


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "X-Emby-Token": getattr(settings, "JELLYFIN_TOKEN", "") or "",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    return s


def _post_media_updated(sess: requests.Session, paths: list[str], update_type: str = "Created") -> bool:
    url = _build_url("Library/Media/Updated")
    payload = {"Updates": [{"Path": p, "UpdateType": update_type} for p in paths]}
    try:
        resp = sess.post(url, json=payload, timeout=15)
        if resp.status_code in (200, 204):
            return True
        logger.debug(f"Jellyfin Library/Media/Updated returned {resp.status_code}: {resp.text}")
    except Exception as ex:
        logger.debug(f"Jellyfin Library/Media/Updated failed: {ex}")
    return False


def _post_library_refresh(sess: requests.Session, primary_path: str = "") -> bool:
    url = _build_url("Library/Refresh")
    body: dict[str, str] = {}
    if primary_path:
        body["Path"] = primary_path
    try:
        resp = sess.post(url, json=body, timeout=15)
        if resp.status_code in (200, 204):
            return True
        logger.debug(f"Jellyfin Library/Refresh returned {resp.status_code}: {resp.text}")
    except Exception as ex:
        logger.debug(f"Jellyfin Library/Refresh failed: {ex}")
    return False


def _post_item_refresh(sess: requests.Session, item_id: str, *, recursive: bool) -> bool:
    endpoint = _build_url(f"Items/{str(item_id).strip()}/Refresh")
    params = {
        "Recursive": "true" if recursive else "false",
        "MetadataRefreshMode": "Default",
        "ImageRefreshMode": "Default",
        "ReplaceAllMetadata": "false",
        "ReplaceAllImages": "false",
    }
    resp = sess.post(endpoint, params=params, timeout=15)
    return resp.status_code in (200, 204)


def _lookup_jellyfin_item_by_path(sess: requests.Session, path: str) -> dict[str, Any] | None:
    candidate = str(path or "").strip()
    if not candidate:
        return None

    endpoint = _build_url("Items")
    params = {
        "Path": candidate,
        "Fields": "Path,SeriesId,Type,MediaSources",
        "Recursive": "true",
    }
    try:
        resp = sess.get(endpoint, params=params, timeout=10)
        if resp.status_code != 200:
            logger.debug(f"Jellyfin path lookup returned {resp.status_code} for path={candidate}: {resp.text}")
            return None
        body = resp.json() or {}
        items = body.get("Items") or []
        if not isinstance(items, list) or not items:
            return None

        normalized = os.path.abspath(candidate)
        for item in items:
            item_path = str(item.get("Path") or "").strip()
            if item_path and os.path.abspath(item_path) == normalized:
                return item
            for source in item.get("MediaSources") or []:
                source_path = str((source or {}).get("Path") or (source or {}).get("DisplayPath") or "").strip()
                if source_path and os.path.abspath(source_path) == normalized:
                    return item
        return items[0]
    except Exception as ex:
        logger.debug(f"Jellyfin path lookup failed for path={candidate}: {ex}")
        return None


def _refresh_jellyfin_item_by_path(sess: requests.Session, path: str) -> bool:
    item = _lookup_jellyfin_item_by_path(sess, path)
    if not isinstance(item, dict):
        return False

    refreshed_any = False

    series_id = str(item.get("SeriesId") or "").strip()
    if series_id:
        try:
            if _post_item_refresh(sess, series_id, recursive=False):
                refreshed_any = True
        except Exception as ex:
            logger.debug(f"Jellyfin series refresh failed for series_id={series_id}: {ex}")

    item_id = str(item.get("Id") or "").strip()
    if item_id:
        try:
            if _post_item_refresh(sess, item_id, recursive=True):
                refreshed_any = True
        except Exception as ex:
            logger.debug(f"Jellyfin item refresh failed for item_id={item_id}: {ex}")

    return refreshed_any


def refresh_jellyfin_paths(paths: set[str], *, update_type: str = "Created") -> dict[str, int]:
    """Notify Jellyfin about changed paths and trigger exact item refresh when possible."""
    if not getattr(settings, "jellyfin_enabled", False):
        return {"refreshed": 0, "failed": 0}
    if not paths:
        return {"refreshed": 0, "failed": 0}

    abs_paths = [os.path.abspath(path) for path in sorted(paths)]
    # Only attempt targeted item refresh for existing files (creates). For deletes the
    # file is already gone so path lookup will always fail — skip straight to fallback.
    exact_paths = [path for path in abs_paths if os.path.isfile(path)]

    sess = _session()
    batch_ok = _post_media_updated(sess, abs_paths, update_type=update_type)

    targeted_wait_seconds = float(getattr(settings, "JELLYFIN_TARGETED_REFRESH_WAIT_SECONDS", 1.0) or 1.0)
    force_library_refresh_on_item_miss = bool(
        getattr(settings, "JELLYFIN_FORCE_LIBRARY_REFRESH_ON_ITEM_MISS", True)
    )

    succeeded: dict[str, bool] = {path: bool(batch_ok) for path in abs_paths}
    item_refresh_count = 0
    retried_item_refresh_count = 0
    total_targeted_wait_seconds = 0.0
    forced_library_refresh = False

    for path in exact_paths:
        refreshed_now = _refresh_jellyfin_item_by_path(sess, path)
        if refreshed_now:
            succeeded[path] = True
            item_refresh_count += 1
        else:
            retried_item_refresh_count += 1

    # Fire fallback library refresh when:
    #   - no exact file paths exist (delete events: files already gone, paths are dirs), OR
    #   - targeted item refresh found nothing on existing files.
    # Guarded by abs_paths so we always have a root hint to pass.
    if force_library_refresh_on_item_miss and abs_paths and (not exact_paths or item_refresh_count == 0):
        if targeted_wait_seconds > 0:
            time.sleep(targeted_wait_seconds)
            total_targeted_wait_seconds = targeted_wait_seconds
        forced_library_refresh = _post_library_refresh(sess, abs_paths[0])
        if forced_library_refresh:
            for path in abs_paths:
                succeeded[path] = True
            logger.info(
                f"Jellyfin fallback library refresh accepted after waiting {total_targeted_wait_seconds:.1f}s "
                f"(update_type={update_type}).",
                extra={"emoji_type": "info"},
            )

    refreshed = sum(1 for ok in succeeded.values() if ok)
    failed = len(abs_paths) - refreshed

    if refreshed:
        logger.info(
            f"Jellyfin refresh accepted for {refreshed}/{len(abs_paths)} path(s); "
            f"batch_ok={batch_ok} item_refreshes={item_refresh_count} retried_item_refreshes={retried_item_refresh_count} "
            f"targeted_wait_seconds={total_targeted_wait_seconds:.1f} "
            f"forced_library_refresh={forced_library_refresh}: "
            f"{', '.join(abs_paths)}",
            extra={"emoji_type": "success"},
        )
    else:
        logger.warning(
            f"Jellyfin refresh failed for {len(abs_paths)} path(s).",
            extra={"emoji_type": "warning"},
        )

    return {"refreshed": refreshed, "failed": failed}


def refresh_jellyfin_sections(has_movies: bool, has_episodes: bool) -> dict[str, int]:
    """Trigger a Jellyfin library scan for movie and/or TV root folders.

    Jellyfin responds better to one broad root-folder scan than many
    small per-folder requests, so this sends the configured library root
    paths rather than individual sub-folders.
    """
    if not getattr(settings, "jellyfin_enabled", False):
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
            ok = _post_media_updated(sess, [root]) or _post_library_refresh(sess, root)
            if ok:
                refreshed += 1
                logger.info(
                    f"Jellyfin library scan triggered for root: {root}",
                    extra={"emoji_type": "refresh"},
                )
            else:
                failed += 1
                logger.warning(
                    f"Jellyfin library scan failed for root={root}",
                    extra={"emoji_type": "warning"},
                )
        except Exception as e:
            failed += 1
            logger.warning(
                f"Jellyfin section refresh failed for root={root}: {e}",
                extra={"emoji_type": "warning"},
            )

    return {"refreshed": refreshed, "failed": failed}


def refresh_jellyfin_item_metadata(item_id: str) -> bool:
    """Trigger metadata refresh for a specific Jellyfin item id."""
    if not getattr(settings, "jellyfin_enabled", False):
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
            f"Jellyfin item refresh returned {resp.status_code} for item_id={target}: {resp.text}",
            extra={"emoji_type": "warning"},
        )
    except Exception as e:
        logger.warning(
            f"Jellyfin item refresh failed for item_id={target}: {e}",
            extra={"emoji_type": "warning"},
        )
    return False
