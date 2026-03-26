import threading
import time
from typing import Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import requests
from requests import RequestException
from requests.exceptions import HTTPError

from core.config import settings
from core.logger import logger


_session = requests.Session()
_cache_lock = threading.Lock()
_cache = {}
_DEFAULT_TTL_SECONDS = 120


def _cache_get(key: str, ttl_seconds: int = _DEFAULT_TTL_SECONDS):
    with _cache_lock:
        rec = _cache.get(key)
        if not rec:
            return None
        if (time.time() - rec['ts']) > ttl_seconds:
            return None
        return rec['value']


def _cache_set(key: str, value):
    with _cache_lock:
        _cache[key] = {'ts': time.time(), 'value': value}


def _build_endpoint(base_url: str, resource: str) -> str:
    root = base_url.rstrip('/')
    if '/api/' in root or root.endswith('/api'):
        return f"{root}/{resource.lstrip('/')}"
    return f"{root}/api/v3/{resource.lstrip('/')}"


def _get_json(url: str, params: dict, timeout: int = 30):
    safe_url = url
    try:
        parts = urlsplit(url)
        safe_url = urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
    except Exception:
        safe_url = url
    try:
        response = _session.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except HTTPError as e:
        response = getattr(e, 'response', None)
        status_code = getattr(response, 'status_code', None)
        reason = getattr(response, 'reason', None)
        logger.error(
            f'ARR request failed url={safe_url} status={status_code} reason={reason}',
            extra={'emoji_type': 'error'},
        )
        return None
    except RequestException as e:
        logger.error(
            f'ARR request failed url={safe_url} error_type={type(e).__name__}',
            extra={'emoji_type': 'error'},
        )
        return None
    except Exception as e:
        logger.error(
            f'ARR request failed url={safe_url} error_type={type(e).__name__}',
            extra={'emoji_type': 'error'},
        )
        return None


def fetch_radarr_movies(url: Optional[str] = None, api_key: Optional[str] = None) -> List[Dict]:
    url = url or settings.RADARR_URL
    api_key = api_key or settings.RADARR_API_KEY
    if not url or not api_key:
        return []

    endpoint = _build_endpoint(url, 'movie')
    cache_key = f'radarr_movies:{endpoint}'
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = _get_json(endpoint, {'apikey': api_key}) or []
    _cache_set(cache_key, data)
    return data


def fetch_radarr_movie(movie_id: int, url: Optional[str] = None, api_key: Optional[str] = None) -> Optional[Dict]:
    """Fetch one Radarr movie by id."""
    url = url or settings.RADARR_URL
    api_key = api_key or settings.RADARR_API_KEY
    if not url or not api_key or not movie_id:
        return None

    endpoint = _build_endpoint(url, f'movie/{int(movie_id)}')
    cache_key = f'radarr_movie:{endpoint}'
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = _get_json(endpoint, {'apikey': api_key})
    if isinstance(data, dict):
        _cache_set(cache_key, data)
        return data
    return None


def fetch_sonarr_series(url: Optional[str] = None, api_key: Optional[str] = None) -> List[Dict]:
    url = url or settings.SONARR_URL
    api_key = api_key or settings.SONARR_API_KEY
    if not url or not api_key:
        return []

    endpoint = _build_endpoint(url, 'series')
    cache_key = f'sonarr_series:{endpoint}'
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = _get_json(endpoint, {'apikey': api_key}) or []
    _cache_set(cache_key, data)
    return data


def fetch_sonarr_series_item(series_id: int, url: Optional[str] = None, api_key: Optional[str] = None) -> Optional[Dict]:
    """Fetch one Sonarr series by id."""
    url = url or settings.SONARR_URL
    api_key = api_key or settings.SONARR_API_KEY
    if not url or not api_key or not series_id:
        return None

    endpoint = _build_endpoint(url, f'series/{int(series_id)}')
    cache_key = f'sonarr_series_item:{endpoint}'
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = _get_json(endpoint, {'apikey': api_key})
    if isinstance(data, dict):
        _cache_set(cache_key, data)
        return data
    return None


def fetch_sonarr_episodes(series_id: int, url: Optional[str] = None, api_key: Optional[str] = None) -> List[Dict]:
    url = url or settings.SONARR_URL
    api_key = api_key or settings.SONARR_API_KEY
    if not url or not api_key:
        return []

    endpoint = _build_endpoint(url, 'episode')
    cache_key = f'sonarr_episodes:{endpoint}:{series_id}'
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = _get_json(endpoint, {'apikey': api_key, 'seriesId': series_id}) or []
    _cache_set(cache_key, data)
    return data


def fetch_sonarr_episodefile(episode_file_id: int, url: Optional[str] = None, api_key: Optional[str] = None) -> Optional[Dict]:
    """Fetch a Sonarr episodefile payload by id.

    Used as a conditional fallback when /episode returns episodeFileId but omits
    an embedded episodeFile object.
    """
    url = url or settings.SONARR_URL
    api_key = api_key or settings.SONARR_API_KEY
    if not url or not api_key or not episode_file_id:
        return None

    endpoint = _build_endpoint(url, f'episodefile/{int(episode_file_id)}')
    cache_key = f'sonarr_episodefile:{endpoint}'
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = _get_json(endpoint, {'apikey': api_key})
    if isinstance(data, dict):
        _cache_set(cache_key, data)
        return data
    return None
