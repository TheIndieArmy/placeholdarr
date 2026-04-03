from core.config import settings
from core.logger import logger
import os
from urllib.parse import urlsplit, urlunsplit

import requests


def _configured_arr_instances() -> list[tuple[str, str, str]]:
    instances: list[tuple[str, str, str]] = []
    if getattr(settings, 'RADARR_URL', None) and getattr(settings, 'RADARR_API_KEY', None):
        instances.append((settings.RADARR_STD_INSTANCE_KEY, 'radarr', settings.RADARR_URL))
    if getattr(settings, 'RADARR_4K_URL', None) and getattr(settings, 'RADARR_4K_API_KEY', None):
        instances.append((settings.RADARR_4K_INSTANCE_KEY, 'radarr', settings.RADARR_4K_URL))
    if getattr(settings, 'SONARR_URL', None) and getattr(settings, 'SONARR_API_KEY', None):
        instances.append((settings.SONARR_STD_INSTANCE_KEY, 'sonarr', settings.SONARR_URL))
    if getattr(settings, 'SONARR_4K_URL', None) and getattr(settings, 'SONARR_4K_API_KEY', None):
        instances.append((settings.SONARR_4K_INSTANCE_KEY, 'sonarr', settings.SONARR_4K_URL))
    return instances


def _build_endpoint(base_url: str, resource: str) -> str:
    root = str(base_url or '').rstrip('/')
    if '/api/' in root or root.endswith('/api'):
        return f"{root}/{resource.lstrip('/')}"
    return f"{root}/api/v3/{resource.lstrip('/')}"


def _probe_arr_instance_live(instance_key: str, arr_type: str) -> bool:
    if arr_type == 'radarr':
        is_4k = instance_key == settings.RADARR_4K_INSTANCE_KEY
        url = settings.RADARR_4K_URL if is_4k else settings.RADARR_URL
        api_key = settings.RADARR_4K_API_KEY if is_4k else settings.RADARR_API_KEY
    else:
        is_4k = instance_key == settings.SONARR_4K_INSTANCE_KEY
        url = settings.SONARR_4K_URL if is_4k else settings.SONARR_URL
        api_key = settings.SONARR_4K_API_KEY if is_4k else settings.SONARR_API_KEY

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
    mode = str(getattr(settings, 'STARTUP_ARR_CHECK_MODE', 'config') or 'config').strip().lower()
    instances = _configured_arr_instances()

    if mode == 'off':
        logger.info('ARR startup check skipped (STARTUP_ARR_CHECK_MODE=off)', extra={'emoji_type': 'info'})
        return True

    if mode == 'config':
        configured = bool(instances)
        if not configured:
            logger.debug('No ARR sources configured in startup config check mode', extra={'emoji_type': 'debug'})
        return configured

    if mode == 'live':
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

    logger.warning(f'Unknown STARTUP_ARR_CHECK_MODE={mode!r}; falling back to config check', extra={'emoji_type': 'warning'})
    return bool(instances)


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
