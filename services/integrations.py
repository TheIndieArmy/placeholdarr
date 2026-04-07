from core.config import settings
from core.logger import logger
import os
from urllib.parse import urlsplit, urlunsplit
from typing import Any

import requests


def _configured_arr_instances() -> list[tuple[str, str, str]]:
    return [
        (
            str(item.get('instance_key') or '').strip().lower(),
            str(item.get('arr_type') or '').strip().lower(),
            str(item.get('url') or '').strip(),
        )
        for item in (getattr(settings, 'configured_arr_instances', []) or [])
        if item.get('instance_key') and item.get('arr_type') in {'radarr', 'sonarr'} and item.get('url')
    ]


def _build_endpoint(base_url: str, resource: str) -> str:
    root = str(base_url or '').rstrip('/')
    if '/api/' in root or root.endswith('/api'):
        return f"{root}/{resource.lstrip('/')}"
    return f"{root}/api/v3/{resource.lstrip('/')}"


def _probe_arr_instance_live(instance_key: str, arr_type: str) -> bool:
    match = None
    for item in (getattr(settings, 'configured_arr_instances', []) or []):
        if str(item.get('instance_key') or '').strip().lower() != str(instance_key or '').strip().lower():
            continue
        if str(item.get('arr_type') or '').strip().lower() != str(arr_type or '').strip().lower():
            continue
        match = item
        break

    url = str((match or {}).get('url') or '').strip()
    api_key = str((match or {}).get('api_key') or '').strip()

    if not url or not api_key:
        return False

    endpoint = _build_endpoint(url, 'system/status')
    safe_url = endpoint
    try:
        parts = urlsplit(endpoint)
        safe_url = urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
    except Exception:
        pass

    try:
        response = requests.get(endpoint, params={'apikey': api_key}, timeout=5)
        response.raise_for_status()
        return True
    except Exception as e:
        logger.warning(
            f'ARR live startup check failed for {instance_key} url={safe_url} error_type={type(e).__name__}',
            extra={'emoji_type': 'warning'},
        )
        return False


def check_all_arr_webhooks() -> bool:
    """Startup ARR preflight check.

    Modes controlled by STARTUP_ARR_CHECK_MODE:
    - off: skip checks
    - config: verify at least one ARR instance is configured
    - live: probe each configured instance with a live API call and require at least one to be reachable
    """
    mode = str(getattr(settings, 'STARTUP_ARR_CHECK_MODE', 'live') or 'live').strip().lower()
    instances = _configured_arr_instances()

    if mode not in ('off', 'config', 'live'):
        logger.warning(
            f'Unknown STARTUP_ARR_CHECK_MODE={mode!r}; using live check',
            extra={'emoji_type': 'warning'},
        )
        mode = 'live'

    if mode == 'off':
        logger.info('ARR startup check skipped (STARTUP_ARR_CHECK_MODE=off)', extra={'emoji_type': 'info'})
        return True

    if mode == 'config':
        configured = bool(instances)
        if not configured:
            logger.debug('No ARR sources configured in startup config check mode', extra={'emoji_type': 'debug'})
        return configured

    # live (default)
    if not instances:
        logger.debug('No ARR sources configured in startup live check mode', extra={'emoji_type': 'debug'})
        return False

    reachable = 0
    for instance_key, arr_type, _ in instances:
        if _probe_arr_instance_live(instance_key, arr_type):
            reachable += 1

    if reachable == 0:
        logger.warning('No ARR instances reachable during startup live check', extra={'emoji_type': 'warning'})
        return False

    logger.info(
        f'ARR live startup check passed for {reachable}/{len(instances)} configured instance(s)',
        extra={'emoji_type': 'success'},
    )
    return True


def delete_dummy_file(path: str | None = None, *args, **kwargs) -> bool:
    """Best-effort placeholder file deletion compatibility helper."""
    if not path:
        return False
    try:
        if os.path.isfile(path):
            os.remove(path)
            return True
    except Exception as e:
        logger.debug(f'Failed to delete placeholder file {path}: {e}', extra={'emoji_type': 'debug'})
    return False


def update_placeholder_status(*args, **kwargs) -> bool:
    """Compatibility stub for action flows during rebuild."""
    return True


def _normalize_url(url: str) -> str:
    return str(url or '').strip().rstrip('/')


def _safe_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
    except Exception:
        return url


def _test_get(url: str, headers: dict[str, str] | None = None, params: dict[str, str] | None = None) -> tuple[bool, str]:
    safe_url = _safe_url(url)
    try:
        response = requests.get(url, headers=headers or {}, params=params or {}, timeout=5)
        if response.status_code >= 400:
            return False, f'HTTP {response.status_code} from {safe_url}'
        return True, 'Connected'
    except Exception as exc:
        return False, f'{type(exc).__name__} while connecting to {safe_url}'


def test_plex_connection(url: str, token: str) -> dict[str, Any]:
    endpoint = _normalize_url(url)
    ok, message = _test_get(f'{endpoint}/identity', headers={'X-Plex-Token': str(token or '').strip()})
    return {'ok': ok, 'message': message, 'service': 'plex'}


def test_jellyfin_connection(url: str, token: str) -> dict[str, Any]:
    endpoint = _normalize_url(url)
    ok, message = _test_get(
        f'{endpoint}/System/Info',
        headers={'X-Emby-Token': str(token or '').strip(), 'Accept': 'application/json'},
    )
    return {'ok': ok, 'message': message, 'service': 'jellyfin'}


def test_emby_connection(url: str, token: str) -> dict[str, Any]:
    endpoint = _normalize_url(url)
    ok, message = _test_get(
        f'{endpoint}/emby/System/Info',
        headers={'X-Emby-Token': str(token or '').strip(), 'Accept': 'application/json'},
    )
    return {'ok': ok, 'message': message, 'service': 'emby'}


def test_arr_connection(url: str, api_key: str, arr_type: str) -> dict[str, Any]:
    endpoint = _build_endpoint(_normalize_url(url), 'system/status')
    ok, message = _test_get(endpoint, params={'apikey': str(api_key or '').strip()})
    return {'ok': ok, 'message': message, 'service': arr_type}


def test_integration_connection(service: str, url: str, token_or_key: str) -> dict[str, Any]:
    service_key = str(service or '').strip().lower()
    if service_key == 'plex':
        return test_plex_connection(url, token_or_key)
    if service_key == 'jellyfin':
        return test_jellyfin_connection(url, token_or_key)
    if service_key == 'emby':
        return test_emby_connection(url, token_or_key)
    if service_key == 'radarr':
        return test_arr_connection(url, token_or_key, 'radarr')
    if service_key == 'sonarr':
        return test_arr_connection(url, token_or_key, 'sonarr')
    return {'ok': False, 'message': 'Unsupported service', 'service': service_key}
