from __future__ import annotations

import os
import time
from typing import Any

import requests

from core.config import settings
from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import AppConfig

_JELLYFIN_USER_ID_CACHE: str | None = None
_JELLYFIN_USER_ID_CACHE_KEY = "JELLYFIN_CACHED_USER_ID"


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


def _get_persisted_jellyfin_user_id() -> str | None:
    session = get_session()
    try:
        row = session.query(AppConfig).filter(AppConfig.key == _JELLYFIN_USER_ID_CACHE_KEY).first()
        val = str((row.value if row else "") or "").strip()
        return val or None
    except Exception:
        return None
    finally:
        session.close()


def _set_persisted_jellyfin_user_id(user_id: str | None) -> None:
    session = get_session()
    try:
        row = session.query(AppConfig).filter(AppConfig.key == _JELLYFIN_USER_ID_CACHE_KEY).first()
        value = str(user_id or "").strip() or None
        if row is None:
            row = AppConfig(
                key=_JELLYFIN_USER_ID_CACHE_KEY,
                value=value,
                value_type="string",
                restart_required=False,
                description="Cached Jellyfin user id for direct metadata projection.",
            )
            session.add(row)
        else:
            row.value = value
            row.value_type = "string"
            row.restart_required = False
            row.description = "Cached Jellyfin user id for direct metadata projection."
            session.add(row)
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def _invalidate_jellyfin_user_id_cache() -> None:
    global _JELLYFIN_USER_ID_CACHE
    _JELLYFIN_USER_ID_CACHE = None
    _set_persisted_jellyfin_user_id(None)


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


def jellyfin_search_items(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Run GET /Items with arbitrary query params; returns Items list or []."""
    if not getattr(settings, "jellyfin_enabled", False):
        return []
    sess = _session()
    try:
        resp = sess.get(_build_url("Items"), params=params, timeout=15)
        if resp.status_code != 200:
            logger.debug(
                f"Jellyfin Items search returned {resp.status_code}: {resp.text}",
                extra={"emoji_type": "debug"},
            )
            return []
        body = resp.json() or {}
        items = body.get("Items") or []
        return items if isinstance(items, list) else []
    except Exception as ex:
        logger.debug(f"Jellyfin Items search failed: {ex}", extra={"emoji_type": "debug"})
        return []


def jellyfin_get_item_fields(item_id: str, fields: str = "ProviderIds,Name,ProductionYear") -> dict[str, Any] | None:
    """Fetch one Jellyfin item payload by id for identity validation."""
    if not getattr(settings, "jellyfin_enabled", False):
        return None
    target = str(item_id or "").strip()
    if not target:
        return None
    sess = _session()
    try:
        uid = _jellyfin_user_id(sess)
        endpoint = _build_url(f"Users/{uid}/Items/{target}" if uid else f"Items/{target}")
        resp = sess.get(endpoint, params={"Fields": fields}, timeout=15)
        if uid and resp.status_code in (401, 403, 404):
            _invalidate_jellyfin_user_id_cache()
            uid = _jellyfin_user_id(sess)
            if uid:
                resp = sess.get(_build_url(f"Users/{uid}/Items/{target}"), params={"Fields": fields}, timeout=15)
        if resp.status_code == 200:
            body = resp.json() or {}
            return body if isinstance(body, dict) else None
        # fallback to global route
        resp2 = sess.get(_build_url(f"Items/{target}"), params={"Fields": fields}, timeout=15)
        if resp2.status_code == 200:
            body = resp2.json() or {}
            return body if isinstance(body, dict) else None
    except Exception as ex:
        logger.debug(f"Jellyfin get item failed item_id={target}: {ex}", extra={"emoji_type": "debug"})
    return None


def _jellyfin_user_id(sess: requests.Session) -> str | None:
    global _JELLYFIN_USER_ID_CACHE
    if _JELLYFIN_USER_ID_CACHE:
        return _JELLYFIN_USER_ID_CACHE
    persisted = _get_persisted_jellyfin_user_id()
    if persisted:
        _JELLYFIN_USER_ID_CACHE = persisted
        return persisted
    try:
        resp = sess.get(_build_url("Users"), timeout=10)
        if resp.status_code != 200:
            return None
        users = resp.json() or []
        if not isinstance(users, list) or not users:
            return None
        uid = str(users[0].get("Id") or "").strip()
        if uid:
            _JELLYFIN_USER_ID_CACHE = uid
            _set_persisted_jellyfin_user_id(uid)
            return uid
    except Exception:
        return None
    return None


def _jellyfin_get_item_for_update(sess: requests.Session, item_id: str) -> tuple[dict[str, Any] | None, str | None]:
    uid = _jellyfin_user_id(sess)
    endpoint = _build_url(f"Users/{uid}/Items/{item_id}" if uid else f"Items/{item_id}")
    resp = sess.get(endpoint, params={"Fields": "Overview,Name"}, timeout=15)
    if resp.status_code == 200:
        return resp.json() or {}, uid
    if uid and resp.status_code in (401, 403, 404):
        _invalidate_jellyfin_user_id_cache()
        uid = _jellyfin_user_id(sess)
        if uid:
            resp_retry = sess.get(_build_url(f"Users/{uid}/Items/{item_id}"), params={"Fields": "Overview,Name"}, timeout=15)
            if resp_retry.status_code == 200:
                return resp_retry.json() or {}, uid
    # Fallback to global item route if user-scoped fetch fails.
    if uid:
        resp2 = sess.get(_build_url(f"Items/{item_id}"), params={"Fields": "Overview,Name"}, timeout=15)
        if resp2.status_code == 200:
            return resp2.json() or {}, None
    return None, uid


def update_jellyfin_item_text(item_id: str, *, title: str, overview: str) -> bool:
    """Directly set Jellyfin item Name/Overview via Items/{id} payload update."""
    if not getattr(settings, "jellyfin_enabled", False):
        return False
    target = str(item_id or "").strip()
    if not target:
        return False
    sess = _session()
    try:
        full, uid = _jellyfin_get_item_for_update(sess, target)
        if not isinstance(full, dict):
            logger.debug(f"Jellyfin direct update lookup failed item_id={target}", extra={"emoji_type": "debug"})
            return False
        full["Name"] = str(title or "")
        full["Overview"] = str(overview or "")
        endpoint = _build_url(f"Items/{target}")
        minimal = {"Id": target, "Name": full["Name"], "Overview": full["Overview"]}

        attempts: list[tuple[str, int, str]] = []

        def _try(method: str, payload: dict[str, Any]) -> bool:
            try:
                if method == "PUT":
                    resp = sess.put(endpoint, json=payload, timeout=20)
                else:
                    resp = sess.post(endpoint, json=payload, timeout=20)
                body = (resp.text or "").replace("\n", " ")[:240]
                attempts.append((method, int(resp.status_code), body))
                return resp.status_code in (200, 204)
            except Exception as ex:
                attempts.append((method, -1, str(ex)[:240]))
                return False

        # Match historical test script behavior order with robust fallbacks.
        ok = (
            _try("PUT", full)
            or _try("POST", full)
            or _try("PUT", minimal)
            or _try("POST", minimal)
        )
        if not ok:
            compact = " | ".join(f"{m}:{c}:{b!r}" for m, c, b in attempts)
            logger.warning(
                f"Jellyfin direct update failed item_id={target} attempts={compact}",
                extra={"emoji_type": "warning"},
            )
            return False
        verify_endpoint = _build_url(f"Users/{uid}/Items/{target}" if uid else f"Items/{target}")
        verify = sess.get(verify_endpoint, params={"Fields": "Overview,Name"}, timeout=15)
        if uid and verify.status_code in (401, 403, 404):
            _invalidate_jellyfin_user_id_cache()
        if verify.status_code == 200:
            latest = verify.json() or {}
            return str(latest.get("Name") or "") == full["Name"] and str(latest.get("Overview") or "") == full["Overview"]
        logger.info(
            f"Jellyfin direct update accepted item_id={target} verify_status={verify.status_code}",
            extra={"emoji_type": "info"},
        )
        return True
    except Exception as ex:
        logger.warning(f"Jellyfin direct update failed item_id={target}: {ex}", extra={"emoji_type": "warning"})
        return False


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

def get_jellyfin_file_path(item_id: str, user_id: str | None = None) -> str:
    """
    Fetch the file path for a Jellyfin item by ItemId (and optional UserId).
    Returns the file path as a string, or an empty string if not found.
    """
    if not item_id or not getattr(settings, "jellyfin_enabled", False):
        return ""
    sess = _session()
    endpoint = _build_url(f"Users/{user_id}/Items/{item_id}" if user_id else f"Items/{item_id}")
    params = {"Fields": "Path,MediaSources"}
    try:
        resp = sess.get(endpoint, params=params, timeout=10)
        if resp.status_code != 200:
            logger.warning(f"get_jellyfin_file_path: Failed to fetch item {item_id} (status {resp.status_code})", extra={'emoji_type': 'warning'})
            return ""
        item = resp.json() or {}
        # Try direct Path field
        path = item.get("Path")
        if path:
            return path
        # Try MediaSources
        for source in item.get("MediaSources") or []:
            source_path = source.get("Path") or source.get("DisplayPath")
            if source_path:
                return source_path
        return ""
    except Exception as ex:
        logger.error(f"get_jellyfin_file_path: Exception for item {item_id}: {ex}", extra={'emoji_type': 'error'})
        return ""