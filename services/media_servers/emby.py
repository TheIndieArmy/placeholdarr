from __future__ import annotations

import os
import time
from typing import Any

import requests

from core.config import settings
from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import AppConfig

_EMBY_USER_ID_CACHE: str | None = None
_EMBY_USER_ID_CACHE_KEY = "EMBY_CACHED_USER_ID"


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


def _get_persisted_emby_user_id() -> str | None:
    session = get_session()
    try:
        row = session.query(AppConfig).filter(AppConfig.key == _EMBY_USER_ID_CACHE_KEY).first()
        val = str((row.value if row else "") or "").strip()
        return val or None
    except Exception:
        return None
    finally:
        session.close()


def _set_persisted_emby_user_id(user_id: str | None) -> None:
    session = get_session()
    try:
        row = session.query(AppConfig).filter(AppConfig.key == _EMBY_USER_ID_CACHE_KEY).first()
        value = str(user_id or "").strip() or None
        if row is None:
            row = AppConfig(
                key=_EMBY_USER_ID_CACHE_KEY,
                value=value,
                value_type="string",
                restart_required=False,
                description="Cached Emby user id for direct metadata projection.",
            )
            session.add(row)
        else:
            row.value = value
            row.value_type = "string"
            row.restart_required = False
            row.description = "Cached Emby user id for direct metadata projection."
            session.add(row)
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def _invalidate_emby_user_id_cache() -> None:
    global _EMBY_USER_ID_CACHE
    _EMBY_USER_ID_CACHE = None
    _set_persisted_emby_user_id(None)


def _post_item_refresh(
    sess: requests.Session,
    item_id: str,
    *,
    recursive: bool,
    image_mode: str = "Default",
    metadata_mode: str = "Default",
    replace_images: bool = False,
    replace_metadata: bool = False,
) -> bool:
    endpoint = _build_url(f"Items/{str(item_id).strip()}/Refresh")
    params = {
        "Recursive": "true" if recursive else "false",
        "ImageRefreshMode": image_mode,
        "MetadataRefreshMode": metadata_mode,
        "ReplaceAllImages": "true" if replace_images else "false",
        "ReplaceAllMetadata": "true" if replace_metadata else "false",
    }
    resp = sess.post(endpoint, params=params, timeout=15)
    return resp.status_code in (200, 204)


def _lookup_emby_item_by_path(sess: requests.Session, path: str) -> dict[str, Any] | None:
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
            logger.debug(f"Emby path lookup returned {resp.status_code} for path={candidate}: {resp.text}")
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
        return items[0] if items else None
    except Exception as ex:
        logger.debug(f"Emby path lookup failed for path={candidate}: {ex}")
        return None


def _refresh_emby_item_by_path(sess: requests.Session, path: str) -> bool:
    item = _lookup_emby_item_by_path(sess, path)
    if not isinstance(item, dict):
        return False

    refreshed_any = False

    series_id = str(item.get("SeriesId") or "").strip()
    if series_id:
        try:
            if _post_item_refresh(sess, series_id, recursive=False):
                refreshed_any = True
        except Exception as ex:
            logger.debug(f"Emby series refresh failed for series_id={series_id}: {ex}")

    item_id = str(item.get("Id") or "").strip()
    if item_id:
        try:
            if _post_item_refresh(sess, item_id, recursive=True):
                refreshed_any = True
        except Exception as ex:
            logger.debug(f"Emby item refresh failed for item_id={item_id}: {ex}")

    return refreshed_any


def _post_media_updated(sess: requests.Session, paths: list[str], update_type: str = "Created") -> bool:
    """Post to Library/Media/Updated for changed media paths."""
    try:
        url = _build_url("Library/Media/Updated")
        payload = {"Updates": [{"Path": p, "UpdateType": update_type} for p in paths]}
        r = sess.post(url, json=payload, timeout=10)
        if r.status_code in (200, 204):
            return True
        logger.debug(f"Emby Library/Media/Updated returned {r.status_code}: {r.text}")
    except Exception as ex:
        logger.debug(f"Emby Library/Media/Updated failed: {ex}")

    return False


def _post_library_refresh(sess: requests.Session, primary_path: str = "") -> bool:
    """Trigger a plain Emby library refresh (useful when path indexing is delayed)."""
    try:
        url = _build_url("Library/Refresh")
        body: dict[str, str] = {}
        if primary_path:
            body["Path"] = primary_path
        r2 = sess.post(url, json=body, timeout=10)
        if r2.status_code in (200, 204):
            return True
        logger.debug(f"Emby Library/Refresh returned {r2.status_code}: {r2.text}")
    except Exception as ex:
        logger.debug(f"Emby Library/Refresh failed: {ex}")

    return False


def refresh_emby_paths(paths: set[str], *, update_type: str = "Created") -> dict[str, int]:
    """Notify Emby about changed paths and trigger exact item refresh when possible."""
    if not getattr(settings, "emby_enabled", False):
        return {"refreshed": 0, "failed": 0}
    if not paths:
        return {"refreshed": 0, "failed": 0}

    path_list = sorted(paths)
    abs_paths = [os.path.abspath(path) for path in path_list]
    sess = _session()

    logger.info(
        f"Emby scan triggered for {len(abs_paths)} item(s) ({update_type}) in single batched request.",
        extra={"emoji_type": "refresh"},
    )

    batch_ok = _post_media_updated(sess, abs_paths, update_type=update_type)
    # Only attempt targeted item refresh for existing files (creates). For deletes the
    # file is already gone so path lookup will always fail — skip straight to fallback.
    exact_paths = [path for path in abs_paths if os.path.isfile(path)]

    targeted_wait_seconds = float(getattr(settings, "EMBY_TARGETED_REFRESH_WAIT_SECONDS", 1.0) or 1.0)
    force_library_refresh_on_item_miss = bool(
        getattr(settings, "EMBY_FORCE_LIBRARY_REFRESH_ON_ITEM_MISS", True)
    )

    succeeded: dict[str, bool] = {path: bool(batch_ok) for path in abs_paths}
    item_refresh_count = 0
    retried_item_refresh_count = 0
    total_targeted_wait_seconds = 0.0
    forced_library_refresh = False
    for path in exact_paths:
        refreshed_now = _refresh_emby_item_by_path(sess, path)
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
                f"Emby fallback library refresh accepted after waiting {total_targeted_wait_seconds:.1f}s "
                f"(update_type={update_type}).",
                extra={"emoji_type": "info"},
            )

    refreshed = sum(1 for ok in succeeded.values() if ok)
    failed = len(abs_paths) - refreshed

    if refreshed:
        sample_paths = ', '.join(abs_paths[:5])
        logger.info(
            f"Emby refresh accepted for {refreshed}/{len(abs_paths)} path(s); "
            f"batch_ok={batch_ok} item_refreshes={item_refresh_count} targeted_misses={retried_item_refresh_count} "
            f"targeted_wait_seconds={total_targeted_wait_seconds:.1f} "
            f"forced_library_refresh={forced_library_refresh}",
            extra={"emoji_type": "success"},
        )
        logger.debug(
            f"Emby refreshed path sample (showing up to 5/{len(abs_paths)}): {sample_paths}",
            extra={"emoji_type": "debug"},
        )
        return {"refreshed": refreshed, "failed": failed}
    else:
        logger.warning(
            f"Emby refresh failed for {len(abs_paths)} path(s).",
            extra={"emoji_type": "warning"},
        )
        return {"refreshed": 0, "failed": len(abs_paths)}


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
            ok = _post_media_updated(sess, [root]) or _post_library_refresh(sess, root)
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


def emby_search_items(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Run GET /Items with arbitrary query params; returns Items list or []."""
    if not getattr(settings, "emby_enabled", False):
        return []
    sess = _session()
    try:
        resp = sess.get(_build_url("Items"), params=params, timeout=15)
        if resp.status_code != 200:
            logger.debug(
                f"Emby Items search returned {resp.status_code}: {resp.text}",
                extra={"emoji_type": "debug"},
            )
            return []
        body = resp.json() or {}
        items = body.get("Items") or []
        return items if isinstance(items, list) else []
    except Exception as ex:
        logger.debug(f"Emby Items search failed: {ex}", extra={"emoji_type": "debug"})
        return []


def _emby_user_id(sess: requests.Session) -> str | None:
    global _EMBY_USER_ID_CACHE
    if _EMBY_USER_ID_CACHE:
        return _EMBY_USER_ID_CACHE
    persisted = _get_persisted_emby_user_id()
    if persisted:
        _EMBY_USER_ID_CACHE = persisted
        return persisted
    try:
        resp = sess.get(_build_url("Users"), timeout=10)
        if resp.status_code != 200:
            return None
        users = resp.json() or []
        if not isinstance(users, list) or not users:
            return None
        for user in users:
            if not isinstance(user, dict):
                continue
            policy = user.get("Policy") or {}
            if isinstance(policy, dict) and policy.get("IsAdministrator"):
                uid = str(user.get("Id") or "").strip()
                if uid:
                    _EMBY_USER_ID_CACHE = uid
                    _set_persisted_emby_user_id(uid)
                    return uid
        uid = str(users[0].get("Id") or "").strip()
        if uid:
            _EMBY_USER_ID_CACHE = uid
            _set_persisted_emby_user_id(uid)
            return uid
    except Exception:
        return None
    return None


def _emby_get_item_for_update(sess: requests.Session, item_id: str) -> tuple[dict[str, Any] | None, str | None]:
    uid = _emby_user_id(sess)
    endpoint = _build_url(f"Users/{uid}/Items/{item_id}" if uid else f"Items/{item_id}")
    resp = sess.get(endpoint, params={"Fields": "Overview,Name"}, timeout=15)
    if resp.status_code == 200:
        return resp.json() or {}, uid
    if uid and resp.status_code in (401, 403, 404):
        _invalidate_emby_user_id_cache()
        uid = _emby_user_id(sess)
        if uid:
            resp_retry = sess.get(_build_url(f"Users/{uid}/Items/{item_id}"), params={"Fields": "Overview,Name"}, timeout=15)
            if resp_retry.status_code == 200:
                return resp_retry.json() or {}, uid
    if uid:
        resp2 = sess.get(_build_url(f"Items/{item_id}"), params={"Fields": "Overview,Name"}, timeout=15)
        if resp2.status_code == 200:
            return resp2.json() or {}, None
    return None, uid


def update_emby_item_text(item_id: str, *, title: str, overview: str) -> bool:
    """Directly set Emby item Name/Overview via Items/{id} payload update."""
    if not getattr(settings, "emby_enabled", False):
        return False
    target = str(item_id or "").strip()
    if not target:
        return False
    sess = _session()
    try:
        full, uid = _emby_get_item_for_update(sess, target)
        if not isinstance(full, dict):
            logger.debug(f"Emby direct update lookup failed item_id={target}", extra={"emoji_type": "debug"})
            return False
        full["Name"] = str(title or "")
        full["Overview"] = str(overview or "")
        post = sess.post(_build_url(f"Items/{target}"), json=full, timeout=20)
        if post.status_code not in (200, 204):
            minimal = {"Id": target, "Name": full["Name"], "Overview": full["Overview"]}
            post2 = sess.post(_build_url(f"Items/{target}"), json=minimal, timeout=20)
            if post2.status_code not in (200, 204):
                logger.warning(
                    f"Emby direct update failed item_id={target} status={post.status_code}/{post2.status_code}",
                    extra={"emoji_type": "warning"},
                )
                return False
        verify_endpoint = _build_url(f"Users/{uid}/Items/{target}" if uid else f"Items/{target}")
        verify = sess.get(verify_endpoint, params={"Fields": "Overview,Name"}, timeout=15)
        if uid and verify.status_code in (401, 403, 404):
            _invalidate_emby_user_id_cache()
        if verify.status_code == 200:
            latest = verify.json() or {}
            return str(latest.get("Name") or "") == full["Name"] and str(latest.get("Overview") or "") == full["Overview"]
        return True
    except Exception as ex:
        logger.warning(f"Emby direct update failed item_id={target}: {ex}", extra={"emoji_type": "warning"})
        return False


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
        "ImageRefreshMode": "Default",
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
