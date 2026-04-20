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


def _test_get_json(
    url: str,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """GET JSON object; used for Jellyfin/Emby System/Info style probes."""
    safe_url = _safe_url(url)
    try:
        response = requests.get(url, headers=headers or {}, params=params or {}, timeout=5)
        if response.status_code >= 400:
            return False, f'HTTP {response.status_code} from {safe_url}', None
        try:
            data = response.json()
        except ValueError:
            return False, f'Response was not valid JSON from {safe_url}', None
        if not isinstance(data, dict):
            return False, f'Unexpected JSON shape from {safe_url}', None
        return True, 'Connected', data
    except Exception as exc:
        return False, f'{type(exc).__name__} while connecting to {safe_url}', None


def _jellyfin_emby_signature_text(data: dict[str, Any]) -> str:
    """Stable text blob from public-ish System/Info fields (both stacks are Emby-forks)."""
    keys = ('ProductName', 'ServerName', 'Brand', 'Name', 'Title', 'Version')
    return ' '.join(str(data.get(k) or '') for k in keys).lower()


def _emby_system_info_url(base_url: str) -> str:
    """Match runtime Emby URL rules: base may already end with ``/emby`` — do not double the segment."""
    root = _normalize_url(base_url)
    lower = root.lower()
    if lower.endswith('/emby'):
        root = root[:-5].rstrip('/')
    return f'{root}/emby/System/Info'


def _payload_text_suggests_jellyfin(data: dict[str, Any]) -> bool:
    """Any top-level string mentions Jellyfin (covers custom ProductName on Jellyfin)."""
    if 'jellyfin' in _jellyfin_emby_signature_text(data):
        return True
    for v in data.values():
        if isinstance(v, str) and 'jellyfin' in v.lower():
            return True
    return False


def _assert_media_server_matches_payload(data: dict[str, Any], expected: str) -> tuple[bool, str]:
    """Reject Jellyfin vs Emby mix-ups: both expose /System/Info with X-Emby-Token auth."""
    want = str(expected or '').strip().lower()
    sig = _jellyfin_emby_signature_text(data)
    has_jellyfin = 'jellyfin' in sig
    has_emby = 'emby' in sig
    if want == 'jellyfin':
        if has_jellyfin:
            return True, 'Connected to Jellyfin'
        if has_emby:
            return False, (
                'This server identifies as Emby. Use the Emby connection (Emby base URL and API key), '
                'not the Jellyfin fields.'
            )
        return False, (
            'Could not confirm Jellyfin at this URL (response did not look like Jellyfin). '
            'Check the base URL (for example http://host:8096) and an API key from Jellyfin → Dashboard → API Keys.'
        )
    if want == 'emby':
        if _payload_text_suggests_jellyfin(data):
            return False, (
                'This server identifies as Jellyfin. Use the Jellyfin connection (Jellyfin URL and token), '
                'not the Emby fields.'
            )
        if has_emby:
            return True, 'Connected to Emby'
        # Many Emby installs return 200 from ``/emby/System/Info`` but use a custom ServerName / localized
        # ProductName that never contains the substring "emby". If we already authenticated that path,
        # treat the host as Emby unless the payload clearly says Jellyfin (handled above).
        return True, 'Connected to Emby'
    return False, 'Unsupported media server type'


def test_plex_connection(url: str, token: str) -> dict[str, Any]:
    endpoint = _normalize_url(url)
    ok, message = _test_get(f'{endpoint}/identity', headers={'X-Plex-Token': str(token or '').strip()})
    return {'ok': ok, 'message': message, 'service': 'plex'}


def test_jellyfin_connection(url: str, token: str) -> dict[str, Any]:
    endpoint = _normalize_url(url)
    headers = {'X-Emby-Token': str(token or '').strip(), 'Accept': 'application/json'}
    ok, message, data = _test_get_json(f'{endpoint}/System/Info', headers=headers)
    if not ok or data is None:
        return {'ok': False, 'message': message, 'service': 'jellyfin'}
    ok2, message2 = _assert_media_server_matches_payload(data, 'jellyfin')
    return {'ok': ok2, 'message': message2, 'service': 'jellyfin'}


def test_emby_connection(url: str, token: str) -> dict[str, Any]:
    headers = {'X-Emby-Token': str(token or '').strip(), 'Accept': 'application/json'}
    ok, message, data = _test_get_json(_emby_system_info_url(url), headers=headers)
    if not ok or data is None:
        return {'ok': False, 'message': message, 'service': 'emby'}
    ok2, message2 = _assert_media_server_matches_payload(data, 'emby')
    return {'ok': ok2, 'message': message2, 'service': 'emby'}


def test_arr_connection(url: str, api_key: str, arr_type: str) -> dict[str, Any]:
    """Ping *arr system/status and verify the server matches the expected app (Radarr vs Sonarr)."""
    expected = str(arr_type or '').strip().lower()
    if expected not in {'radarr', 'sonarr'}:
        return {'ok': False, 'message': 'Invalid ARR type', 'service': expected}

    endpoint = _build_endpoint(_normalize_url(url), 'system/status')
    safe_url = _safe_url(endpoint)
    try:
        response = requests.get(
            endpoint,
            params={'apikey': str(api_key or '').strip()},
            headers={'Accept': 'application/json'},
            timeout=5,
        )
        if response.status_code >= 400:
            return {'ok': False, 'message': f'HTTP {response.status_code} from {safe_url}', 'service': expected}
        data = response.json()
    except ValueError:
        return {
            'ok': False,
            'message': f'Response was not valid JSON from {safe_url}; check the URL points to Radarr or Sonarr (API v3).',
            'service': expected,
        }
    except Exception as exc:
        return {'ok': False, 'message': f'{type(exc).__name__} while connecting to {safe_url}', 'service': expected}

    if not isinstance(data, dict):
        return {'ok': False, 'message': 'Unexpected response from server.', 'service': expected}

    fingerprint = ' '.join(
        str(data.get(key) or '')
        for key in ('appName', 'instanceName', 'version', 'packageVersion', 'osName', 'runtimeVersion')
    ).lower()

    if expected == 'radarr' and 'radarr' in fingerprint:
        return {'ok': True, 'message': 'Connected to Radarr', 'service': expected}
    if expected == 'sonarr' and 'sonarr' in fingerprint:
        return {'ok': True, 'message': 'Connected to Sonarr', 'service': expected}

    other = 'sonarr' if expected == 'radarr' else 'radarr'
    if other in fingerprint:
        return {
            'ok': False,
            'message': (
                f'This server identifies as {other.title()}, but this slot is for {expected.title()}. '
                f'Use the {other.title()} column or change the URL.'
            ),
            'service': expected,
        }

    return {
        'ok': False,
        'message': (
            f'Could not confirm this host is {expected.title()} (expected “{expected}” in system status). '
            'Check the base URL reaches that app’s root (not another service on the same host).'
        ),
        'service': expected,
    }


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
