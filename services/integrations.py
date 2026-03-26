from core.config import settings
from core.logger import logger
import os


def check_all_arr_webhooks() -> bool:
    """Return True when at least one ARR service appears configured.

    This keeps startup behavior deterministic while the webhook registration
    lifecycle is rebuilt in dedicated modules.
    """
    candidates = [
        (getattr(settings, 'RADARR_URL', None), getattr(settings, 'RADARR_API_KEY', None)),
        (getattr(settings, 'RADARR_4K_URL', None), getattr(settings, 'RADARR_4K_API_KEY', None)),
        (getattr(settings, 'SONARR_URL', None), getattr(settings, 'SONARR_API_KEY', None)),
        (getattr(settings, 'SONARR_4K_URL', None), getattr(settings, 'SONARR_4K_API_KEY', None)),
    ]
    configured = any(url and key for url, key in candidates)
    if not configured:
        logger.debug('No ARR webhook sources configured yet', extra={'emoji_type': 'debug'})
    return configured


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
