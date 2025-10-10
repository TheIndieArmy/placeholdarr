import requests
from typing import List, Dict, Optional
from core.config import settings


def _get(url: str, params: dict = None, timeout: int = 10) -> Optional[List[Dict]]:
    try:
        resp = requests.get(url, params=params or {}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception:
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
    data = _get(endpoint, params=params)
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
    data = _get(endpoint, params=params)
    return data or []
