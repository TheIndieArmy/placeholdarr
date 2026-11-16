"""In-memory connection status for Media and ARR integrations.

Probe successes (startup / periodic refresh / Settings Test) clear prior
connectivity failures so Settings ! heals when services come back.

Auth failures (HTTP 401/403) stay sticky until a successful Settings Test.
Opportunistic runtime successes do not paint green (avoids a single lucky call
clearing a real outage). Client request timeouts are ignored (no Settings !).
Failed Tests do not sticky (modal shows the error; Cancel leaves no !).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from core.config import settings
from core.logger import logger

_lock = threading.RLock()
_media: dict[str, dict[str, Any]] = {}
_arr: dict[str, dict[str, Any]] = {}
_last_refresh_at: str | None = None

_AUTH_HTTP_CODES = {401, 403}
_GATEWAY_HTTP_CODES = {502, 503, 504}
_CONNECTIVITY_HTTP_CODES = _AUTH_HTTP_CODES | _GATEWAY_HTTP_CODES

# Sources allowed to record ok=True (and clear clearable failures).
_PROBE_OK_SOURCES = frozenset({"startup", "probe", "test"})

# Boot sync waits for Arr; media is warn-only.
BOOT_ARR_WAIT_SECONDS = 600.0
BOOT_ARR_POLL_SECONDS = 5.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _entry(*, ok: bool, message: str, source: str, sticky_failure: bool = False, **extra: Any) -> dict[str, Any]:
    out = {
        "ok": bool(ok),
        "message": str(message or "").strip() or ("Connected" if ok else "Connection failed"),
        "checked_at": _now_iso(),
        "source": str(source or "").strip().lower() or "runtime",
        # Auth-only sticky: needs Settings Test. Connectivity failures are clearable by probes.
        "sticky_failure": bool(sticky_failure) and (not bool(ok)),
    }
    out.update(extra)
    return out


def is_connectivity_http_status(status_code: int | None) -> bool:
    try:
        code = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        return False
    return code in _CONNECTIVITY_HTTP_CODES


def is_auth_http_status(status_code: int | None) -> bool:
    try:
        code = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        return False
    return code in _AUTH_HTTP_CODES


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


def is_auth_failure_message(message: str) -> bool:
    """Detect auth failure wording from probes (HTTP 401/403)."""
    text = str(message or "").strip().lower()
    if not text:
        return False
    if "401" in text or "403" in text:
        return True
    if "unauthorized" in text or "forbidden" in text:
        return True
    return False


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


def _should_record_ok(*, source: str, existing: dict[str, Any] | None) -> bool:
    """Whether an ok=True update may replace the stored entry."""
    src = str(source or "").strip().lower() or "runtime"
    if src not in _PROBE_OK_SOURCES:
        # Opportunistic runtime successes never paint green.
        return False
    if existing and not existing.get("ok") and existing.get("sticky_failure") and src != "test":
        # Auth sticky until Settings Test.
        return False
    return True


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
        if ok:
            if not _should_record_ok(source=src, existing=existing):
                return
            _media[key] = _entry(ok=True, message=message, source=src, sticky_failure=False, service=key)
            return
        sticky = is_auth_failure_message(message)
        _media[key] = _entry(ok=False, message=message, source=src, sticky_failure=sticky, service=key)


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
        if ok:
            if not _should_record_ok(source=src, existing=existing):
                return
            _arr[iid] = _entry(
                ok=True,
                message=message,
                source=src,
                sticky_failure=False,
                instance_id=iid,
                arr_type=str(arr_type or "").strip().lower(),
                instance_key=str(instance_key or "").strip().lower(),
                label=str(label or "").strip(),
            )
            return
        sticky = is_auth_failure_message(message)
        _arr[iid] = _entry(
            ok=False,
            message=message,
            source=src,
            sticky_failure=sticky,
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


def configured_arr_instance_ids() -> list[str]:
    """Configured Radarr/Sonarr instance ids that boot sync should wait on."""
    ids: list[str] = []
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
        if not url or not api_key or not instance_id:
            continue
        ids.append(instance_id)
    return ids


def integrations_ready_for_boot_sync() -> bool:
    """True when every configured Arr instance currently probes ok (or none configured)."""
    ids = configured_arr_instance_ids()
    if not ids:
        return True
    with _lock:
        for iid in ids:
            entry = _arr.get(iid)
            if not entry or not entry.get("ok"):
                return False
    return True


def media_has_configured_failure() -> bool:
    """True when any configured media service is currently failing."""
    status = get_integration_status()
    return bool(status.get("media_has_failure"))


def wait_for_arr_boot_readiness(
    *,
    timeout_seconds: float = BOOT_ARR_WAIT_SECONDS,
    poll_seconds: float = BOOT_ARR_POLL_SECONDS,
) -> bool:
    """Probe until all configured Arr instances are ok, or timeout.

    Returns True when ready for boot sync. Media remaining down only logs a warning.
    """
    ids = configured_arr_instance_ids()
    if not ids:
        logger.info(
            "Boot Arr readiness: no *arr instances configured; not waiting",
            extra={"emoji_type": "info"},
        )
        return True

    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    poll = max(1.0, float(poll_seconds))
    attempt = 0
    while True:
        attempt += 1
        try:
            refresh_integration_connection_status()
        except Exception as exc:
            logger.warning(
                f"Boot Arr readiness probe failed: {exc}",
                extra={"emoji_type": "warning"},
            )
        if integrations_ready_for_boot_sync():
            if media_has_configured_failure():
                logger.warning(
                    "Boot Arr readiness: Arr ok but media still unreachable "
                    "(boot sync will continue; media is warn-only)",
                    extra={"emoji_type": "warning"},
                )
            else:
                logger.info(
                    f"Boot Arr readiness: all {len(ids)} configured Arr instance(s) reachable "
                    f"(attempt={attempt})",
                    extra={"emoji_type": "success"},
                )
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning(
                f"Boot Arr readiness timed out after {timeout_seconds:.0f}s "
                f"(configured_arr={len(ids)}); skipping hollow boot sync",
                extra={"emoji_type": "warning"},
            )
            return False
        logger.info(
            f"Boot Arr readiness: waiting for Arr "
            f"(attempt={attempt}, next_probe_in={min(poll, remaining):.0f}s)",
            extra={"emoji_type": "info"},
        )
        time.sleep(min(poll, remaining))


def refresh_integration_connection_status() -> dict[str, Any]:
    """Probe configured Media + ARR. Probe successes clear clearable connectivity fails."""
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
            record_media_status(service, ok=ok, message=message, source="probe")
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
                source="probe",
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
