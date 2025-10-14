import requests
import time
import threading
from typing import List, Dict, Optional
from core.config import settings
from services.instrumentation import instr

# Module-level session for connection reuse
_session = requests.Session()

# Simple TTL cache implementation for Sonarr episodes per-series
_episodes_cache = {}
_episodes_cache_lock = threading.Lock()
_EPISODES_TTL_SECONDS = 120

# Simple TTL cache for Radarr movies (avoid fetching entire movie list per-movie)
_radarr_cache = {}
_radarr_cache_lock = threading.Lock()
_RADARR_TTL_SECONDS = 120


def _get(url: str, params: dict = None, timeout: int = 10) -> Optional[List[Dict]]:
    start = time.time()
    try:
        resp = _session.get(url, params=params or {}, timeout=timeout)
        resp.raise_for_status()
        elapsed_ms = (time.time() - start) * 1000.0
        try:
            instr.record_api_call(url, elapsed_ms, resp.status_code)
        except Exception:
            pass
        return resp.json()
    except Exception:
        elapsed_ms = (time.time() - start) * 1000.0
        try:
            instr.record_api_call(url, elapsed_ms, getattr(resp, 'status_code', None) if 'resp' in locals() else None)
        except Exception:
            pass
        return None


def fetch_radarr_movies(url: str = None, api_key: str = None) -> List[Dict]:
    url = url or settings.RADARR_URL
    api_key = api_key or settings.RADARR_API_KEY
    if not url or not api_key:
        raise RuntimeError('Radarr URL/API key not configured')
    # If the configured URL already contains an API path segment, avoid duplicating it
    if '/api/' in url or url.rstrip('/').endswith('api'):
        endpoint = f"{url.rstrip('/')}/movie"
    else:
        endpoint = f"{url.rstrip('/')}/api/v3/movie"
    params = {'apikey': api_key}
    # Check simple TTL cache first to avoid repeated full-list fetches during large bursts
    now = time.time()
    cache_key = f"radarr_movies:{endpoint}"
    with _radarr_cache_lock:
        rec = _radarr_cache.get(cache_key)
        if rec and (now - rec['ts']) < _RADARR_TTL_SECONDS:
            return rec['value']

    data = _get(endpoint, params=params)
    if data is None:
        return []

    with _radarr_cache_lock:
        _radarr_cache[cache_key] = {'ts': now, 'value': data}
    return data or []


def fetch_sonarr_series(url: str = None, api_key: str = None) -> List[Dict]:
    url = url or settings.SONARR_URL
    api_key = api_key or settings.SONARR_API_KEY
    if not url or not api_key:
        raise RuntimeError('Sonarr URL/API key not configured')
    if '/api/' in url or url.rstrip('/').endswith('api'):
        endpoint = f"{url.rstrip('/')}/series"
    else:
        endpoint = f"{url.rstrip('/')}/api/v3/series"
    params = {'apikey': api_key}
    data = _get(endpoint, params=params)
    return data or []


def fetch_sonarr_series_by_id(series_id: int, url: str = None, api_key: str = None) -> Optional[Dict]:
    """Fetch a Sonarr series object by id using /series/{id}.

    Returns the series dict or None on error/not found.
    """
    url = url or settings.SONARR_URL
    api_key = api_key or settings.SONARR_API_KEY
    if not url or not api_key:
        raise RuntimeError('Sonarr URL/API key not configured')
    if '/api/' in url or url.rstrip('/').endswith('api'):
        endpoint = f"{url.rstrip('/')}/series/{series_id}"
    else:
        endpoint = f"{url.rstrip('/')}/api/v3/series/{series_id}"
    params = {'apikey': api_key}
    data = _get(endpoint, params=params)
    if not data:
        return None
    return data


def fetch_sonarr_episodes(series_id: int, url: str = None, api_key: str = None) -> List[Dict]:
    """Fetch episodes for a given Sonarr series id using the /episode endpoint.
    Returns a list of episode dicts or an empty list on error.
    """
    url = url or settings.SONARR_URL
    api_key = api_key or settings.SONARR_API_KEY
    if not url or not api_key:
        raise RuntimeError('Sonarr URL/API key not configured')
    if '/api/' in url or url.rstrip('/').endswith('api'):
        endpoint = f"{url.rstrip('/')}/episode"
    else:
        endpoint = f"{url.rstrip('/')}/api/v3/episode"
    params = {'apikey': api_key, 'seriesId': series_id}

    # Check TTL cache first
    now = time.time()
    cache_key = f"sonarr_episodes:{series_id}:{endpoint}"
    with _episodes_cache_lock:
        rec = _episodes_cache.get(cache_key)
        if rec and (now - rec['ts']) < _EPISODES_TTL_SECONDS:
            return rec['value']

    data = _get(endpoint, params=params)
    if data is None:
        return []

    # Store in cache
    with _episodes_cache_lock:
        _episodes_cache[cache_key] = {'ts': now, 'value': data}
    return data or []


def fetch_sonarr_episodefile(episode_file_id: int, url: str = None, api_key: str = None) -> Optional[Dict]:
    """Fetch a Sonarr episodeFile object by id using /episodeFile/{id}.

    Returns the episodeFile dict or None on error/not found.
    """
    url = url or settings.SONARR_URL
    api_key = api_key or settings.SONARR_API_KEY
    if not url or not api_key:
        raise RuntimeError('Sonarr URL/API key not configured')
    # endpoint like /api/v3/episodefile/{id}
    if '/api/' in url or url.rstrip('/').endswith('api'):
        endpoint = f"{url.rstrip('/')}/episodefile/{episode_file_id}"
    else:
        endpoint = f"{url.rstrip('/')}/api/v3/episodefile/{episode_file_id}"
    params = {'apikey': api_key}
    data = _get(endpoint, params=params)
    if not data:
        return None
    return data
