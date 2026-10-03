"""In-memory connection status for Media and ARR integrations.

Failures are sticky until a successful Settings Test clears them.
Runtime successes do not clear a failure.
Client request timeouts are ignored (no sticky Settings !).
Failed Tests do not sticky (modal shows the error; Cancel leaves no !).
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from core.config import settings
from core.logger import logger

_lock = threading.RLock()
_media: dict[str, dict[str, Any]] = {}
_arr: dict[str, dict[str, Any]] = {}
_last_refresh_at: str | None = None

_CONNECTIVITY_HTTP_CODES = {401, 403, 502, 503, 504}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _entry(*, ok: bool, message: str, source: str, **extra: Any) -> dict[str, Any]:
    out = {
        "ok": bool(ok),
        "message": str(message or "").strip() or ("Connected" if ok else "Connection failed"),
        "checked_at": _now_iso(),
        "source": str(source or "").strip().lower() or "runtime",
        "sticky_failure": (not bool(ok)),
    }
    out.update(extra)
    return out


def is_connectivity_http_status(status_code: int | None) -> bool:
    try:
        code = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        return False
    return code in _CONNECTIVITY_HTTP_CODES


def is_timeout_exception(exc: BaseException) -> bool:
    """True for client-side request timeouts (not HTTP 504 gateway responses)."""
    try:
        from requests.exceptions import Timeout
    except Exception:
        Timeout = ()  # type: ignore[misc, assignment]
    if Timeout and isinstance(exc, Timeout):
        return True
    name = type(exc).__name__.lower()
    return "timeout" in name


def is_timeout_failure_message(message: str) -> bool:
    """Detect timeout wording from probes / ARR helpers (e.g. ReadTimeout, Timed out after Ns)."""
    text = str(message or "").strip().lower()
    if not text:
        return False
    return "timed out" in text or "timeout" in text


def is_connectivity_exception(exc: BaseException) -> bool:
    """True for connection errors and auth/gateway HTTP failures.

    Client request timeouts are excluded so a slow reply does not sticky the Settings !.
    """
    if is_timeout_exception(exc):
        return False
    try:
        import requests
        from requests.exceptions import ConnectionError as ReqConnectionError
        from requests.exceptions import HTTPError
    except Exception:
        return False

    if isinstance(exc, ReqConnectionError):
        return True
    if isinstance(exc, requests.exceptions.RequestException) and not isinstance(exc, HTTPError):
        # DNS failures, connection reset, etc.
        return True
    if isinstance(exc, HTTPError):
        response = getattr(exc, "response", None)
        return is_connectivity_http_status(getattr(response, "status_code", None))
    return False


def record_media_status(service: str, *, ok: bool, message: str, source: str = "runtime") -> None:
    key = str(service or "").strip().lower()
    if key not in {"plex", "jellyfin", "emby"}:
        return
    if not ok and is_timeout_failure_message(message):
        # Timeouts are transient; do not sticky the Settings warning !.
        return
    src = str(source or "runtime").strip().lower()
    with _lock:
        existing = _media.get(key)
        if ok and src != "test" and existing and not existing.get("ok"):
            # Sticky until a successful Test.
            return
        if ok and src == "runtime":
            return
        _media[key] = _entry(ok=ok, message=message, source=src, service=key)

def clear_media_status(service: str) -> None:
    key = str(service or "").strip().lower()
    with _lock:
        _media.pop(key, None)


def record_arr_status(
    *,
    instance_id: str,
    arr_type: str,
    instance_key: str = "",
    label: str = "",
    ok: bool,
    message: str,
    source: str = "runtime",
) -> None:
    iid = str(instance_id or "").strip().lower()
    if not iid:
        return
    if not ok and is_timeout_failure_message(message):
        # Timeouts are transient; do not sticky the Settings warning !.
        return
    src = str(source or "runtime").strip().lower()
    with _lock:
        existing = _arr.get(iid)
        if ok and src != "test" and existing and not existing.get("ok"):
            return
        if ok and src == "runtime":
            return
        _arr[iid] = _entry(
            ok=ok,
            message=message,
            source=src,
            instance_id=iid,
            arr_type=str(arr_type or "").strip().lower(),
            instance_key=str(instance_key or "").strip().lower(),
            label=str(label or "").strip(),
        )

def clear_arr_status(instance_id: str) -> None:
    iid = str(instance_id or "").strip().lower()
    with _lock:
        _arr.pop(iid, None)


def resolve_arr_instance_meta(*, base_url: str = "", api_key: str = "", instance_key: str = "") -> dict[str, str] | None:
    """Map a request URL / key back to a configured ARR instance."""
    want_key = str(instance_key or "").strip().lower()
    want_url = _normalize_host(base_url)
    want_api = str(api_key or "").strip()
    for item in getattr(settings, "configured_arr_instances", []) or []:
        arr_type = str(item.get("arr_type") or "").strip().lower()
        if arr_type not in {"radarr", "sonarr"}:
            continue
        iid = str(item.get("instance_id") or item.get("id") or "").strip().lower()
        ikey = str(item.get("instance_key") or "").strip().lower()
        if not iid:
            iid = f"{arr_type}:{ikey}" if ikey else ""
        if not iid:
            continue
        if want_key and ikey == want_key:
            return {
                "instance_id": iid,
                "arr_type": arr_type,
                "instance_key": ikey,
                "label": str(item.get("label") or ikey or iid),
            }
        item_url = _normalize_host(str(item.get("url") or ""))
        item_api = str(item.get("api_key") or "").strip()
        if want_url and item_url and want_url == item_url:
            if want_api and item_api and want_api != item_api:
                continue
            return {
                "instance_id": iid,
                "arr_type": arr_type,
                "instance_key": ikey,
                "label": str(item.get("label") or ikey or iid),
            }
    return None


def mark_arr_connectivity_failure(
    *,
    message: str,
    base_url: str = "",
    api_key: str = "",
    instance_key: str = "",
    instance_id: str = "",
    arr_type: str = "",
) -> None:
    meta = None
    iid = str(instance_id or "").strip().lower()
    if iid:
        meta = {
            "instance_id": iid,
            "arr_type": str(arr_type or "").strip().lower(),
            "instance_key": str(instance_key or "").strip().lower(),
            "label": "",
        }
    else:
        meta = resolve_arr_instance_meta(base_url=base_url, api_key=api_key, instance_key=instance_key)
    if not meta:
        return
    record_arr_status(
        instance_id=meta["instance_id"],
        arr_type=meta.get("arr_type") or arr_type,
        instance_key=meta.get("instance_key") or "",
        label=meta.get("label") or "",
        ok=False,
        message=message,
        source="runtime",
    )


def mark_media_connectivity_failure(service: str, *, message: str) -> None:
    record_media_status(service, ok=False, message=message, source="runtime")


def get_integration_status() -> dict[str, Any]:
    with _lock:
        media = {k: dict(v) for k, v in _media.items()}
        arr = {k: dict(v) for k, v in _arr.items()}
        checked_at = _last_refresh_at
    media_fail = any(not bool(v.get("ok")) for v in media.values())
    arr_fail = any(not bool(v.get("ok")) for v in arr.values())
    return {
        "checked_at": checked_at,
        "media": media,
        "arr": arr,
        "media_has_failure": media_fail,
        "arr_has_failure": arr_fail,
        "settings_has_failure": media_fail or arr_fail,
    }


def refresh_integration_connection_status() -> dict[str, Any]:
    """Probe configured Media + ARR. Failures stick; successes do not clear a sticky fail."""
    from services.integrations import (
        test_arr_connection,
        test_emby_connection,
        test_jellyfin_connection,
        test_plex_connection,
    )

    media_specs = (
        ("plex", bool(getattr(settings, "ENABLE_PLEX", False)), getattr(settings, "PLEX_URL", None), getattr(settings, "PLEX_TOKEN", None), test_plex_connection),
        ("jellyfin", bool(getattr(settings, "ENABLE_JELLYFIN", False)), getattr(settings, "JELLYFIN_URL", None), getattr(settings, "JELLYFIN_TOKEN", None), test_jellyfin_connection),
        ("emby", bool(getattr(settings, "ENABLE_EMBY", False)), getattr(settings, "EMBY_URL", None), getattr(settings, "EMBY_TOKEN", None), test_emby_connection),
    )
    seen_media: set[str] = set()
    for service, enabled, url, token, tester in media_specs:
        url_s = str(url or "").strip()
        token_s = str(token or "").strip()
        if not enabled or not url_s or not token_s:
            continue
        seen_media.add(service)
        try:
            result = tester(url_s, token_s)
            ok = bool(result.get("ok"))
            message = str(result.get("message") or "")
        except Exception as exc:
            ok = False
            message = f"{type(exc).__name__} while testing {service}"
        if not ok and is_timeout_failure_message(message):
            logger.info(
                f"Media connection check timed out for {service} (not marking Settings !): {message}",
                extra={"emoji_type": "info"},
            )
        else:
            record_media_status(service, ok=ok, message=message, source="startup")
            if not ok:
                logger.warning(
                    f"Media connection check failed for {service}: {message}",
                    extra={"emoji_type": "warning"},
                )

    seen_arr: set[str] = set()
    for item in getattr(settings, "configured_arr_instances", []) or []:
        arr_type = str(item.get("arr_type") or "").strip().lower()
        if arr_type not in {"radarr", "sonarr"}:
            continue
        url = str(item.get("url") or "").strip()
        api_key = str(item.get("api_key") or "").strip()
        instance_key = str(item.get("instance_key") or "").strip().lower()
        instance_id = str(item.get("instance_id") or item.get("id") or "").strip().lower()
        if not instance_id:
            instance_id = f"{arr_type}:{instance_key}" if instance_key else ""
        label = str(item.get("label") or instance_key or instance_id).strip()
        if not url or not api_key or not instance_id:
            continue
        seen_arr.add(instance_id)
        try:
            result = test_arr_connection(url, api_key, arr_type)
            ok = bool(result.get("ok"))
            message = str(result.get("message") or "")
        except Exception as exc:
            ok = False
            message = f"{type(exc).__name__} while testing {label or instance_id}"
        if not ok and is_timeout_failure_message(message):
            logger.info(
                f"ARR connection check timed out for {label or instance_id} (not marking Settings !): {message}",
                extra={"emoji_type": "info"},
            )
        else:
            record_arr_status(
                instance_id=instance_id,
                arr_type=arr_type,
                instance_key=instance_key,
                label=label,
                ok=ok,
                message=message,
                source="startup",
            )
            if not ok:
                logger.warning(
                    f"ARR connection check failed for {label or instance_id}: {message}",
                    extra={"emoji_type": "warning"},
                )

    with _lock:
        global _last_refresh_at
        for key in list(_media.keys()):
            if key not in seen_media:
                _media.pop(key, None)
        for key in list(_arr.keys()):
            if key not in seen_arr:
                _arr.pop(key, None)
        _last_refresh_at = _now_iso()

    status = get_integration_status()
    media_total = len(status.get("media") or {})
    arr_total = len(status.get("arr") or {})
    media_ok = media_total - sum(1 for v in (status.get("media") or {}).values() if not v.get("ok"))
    arr_ok = arr_total - sum(1 for v in (status.get("arr") or {}).values() if not v.get("ok"))
    logger.info(
        f"Integration connection check: media {media_ok}/{media_total} ok, ARR {arr_ok}/{arr_total} ok",
        extra={"emoji_type": "success" if not status.get("settings_has_failure") else "warning"},
    )
    return status


def _normalize_host(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text if "://" in text else f"http://{text}")
        host = (parts.hostname or "").lower()
        port = parts.port
        if port:
            return f"{host}:{port}"
        return host
    except Exception:
        return text.rstrip("/").lower()
