import threading
import time
from datetime import date
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


def _default_radarr_endpoint() -> tuple[str, str]:
    return settings.resolve_arr_endpoint("radarr", role="primary")


def _default_sonarr_endpoint() -> tuple[str, str]:
    return settings.resolve_arr_endpoint("sonarr", role="primary")


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


def _request_json(
    method: str,
    url: str,
    *,
    params: Optional[dict] = None,
    payload: Optional[dict] = None,
    api_key: Optional[str] = None,
    timeout: int = 30,
):
    safe_url = url
    try:
        parts = urlsplit(url)
        safe_url = urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
    except Exception:
        safe_url = url

    headers = {}
    if api_key:
        headers['X-Api-Key'] = api_key

    try:
        response = _session.request(
            method=method.upper(),
            url=url,
            params=params,
            json=payload,
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        if not response.text:
            return {}
        return response.json()
    except HTTPError as e:
        response = getattr(e, 'response', None)
        status_code = getattr(response, 'status_code', None)
        reason = getattr(response, 'reason', None)
        logger.error(
            f'ARR write failed method={method.upper()} url={safe_url} status={status_code} reason={reason}',
            extra={'emoji_type': 'error'},
        )
        return None
    except RequestException as e:
        logger.error(
            f'ARR write failed method={method.upper()} url={safe_url} error_type={type(e).__name__}',
            extra={'emoji_type': 'error'},
        )
        return None
    except Exception as e:
        logger.error(
            f'ARR write failed method={method.upper()} url={safe_url} error_type={type(e).__name__}',
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
    url = url or _default_radarr_endpoint()[0]
    api_key = api_key or _default_radarr_endpoint()[1]
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
    url = url or _default_radarr_endpoint()[0]
    api_key = api_key or _default_radarr_endpoint()[1]
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
    url = url or _default_sonarr_endpoint()[0]
    api_key = api_key or _default_sonarr_endpoint()[1]
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
    url = url or _default_sonarr_endpoint()[0]
    api_key = api_key or _default_sonarr_endpoint()[1]
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
    url = url or _default_sonarr_endpoint()[0]
    api_key = api_key or _default_sonarr_endpoint()[1]
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
    url = url or _default_sonarr_endpoint()[0]
    api_key = api_key or _default_sonarr_endpoint()[1]
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


def fetch_radarr_calendar(
    start_date: date,
    end_date: date,
    url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> List[Dict]:
    """Fetch Radarr calendar rows for a date range.

    Returns movie payloads that include release/date metadata without requiring
    a full movie index pull.
    """
    url = url or _default_radarr_endpoint()[0]
    api_key = api_key or _default_radarr_endpoint()[1]
    if not url or not api_key:
        return []

    endpoint = _build_endpoint(url, 'calendar')
    start_text = start_date.isoformat()
    end_text = end_date.isoformat()
    cache_key = f'radarr_calendar:{endpoint}:{start_text}:{end_text}'
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = _get_json(
        endpoint,
        {
            'apikey': api_key,
            'start': start_text,
            'end': end_text,
            'includeSeries': 'false',
        },
    ) or []
    _cache_set(cache_key, data)
    return data


def fetch_sonarr_calendar(
    start_date: date,
    end_date: date,
    url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> List[Dict]:
    """Fetch Sonarr calendar rows for a date range.

    Returns episode payloads with airDate metadata and series identifiers.
    """
    url = url or _default_sonarr_endpoint()[0]
    api_key = api_key or _default_sonarr_endpoint()[1]
    if not url or not api_key:
        return []

    endpoint = _build_endpoint(url, 'calendar')
    start_text = start_date.isoformat()
    end_text = end_date.isoformat()
    cache_key = f'sonarr_calendar:{endpoint}:{start_text}:{end_text}'
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = _get_json(
        endpoint,
        {
            'apikey': api_key,
            'start': start_text,
            'end': end_text,
            'includeSeries': 'true',
        },
    ) or []
    _cache_set(cache_key, data)
    return data


def _fetch_arr_history(
    *,
    endpoint: str,
    api_key: str,
    start_id: int | None = None,
    page_size: int = 250,
    max_pages: int = 50,
    cache_prefix: str,
) -> List[Dict]:
    records: List[Dict] = []
    page = 1
    safe_start = int(start_id or 0)
    safe_page_size = max(1, int(page_size))
    safe_max_pages = max(1, int(max_pages))

    while page <= safe_max_pages:
        cache_key = f'{cache_prefix}:{endpoint}:start={safe_start}:page={page}:size={safe_page_size}'
        payload = _cache_get(cache_key, ttl_seconds=30)
        if payload is None:
            params = {
                'apikey': api_key,
                'page': page,
                'pageSize': safe_page_size,
                'sortDirection': 'ascending',
            }
            if safe_start > 0:
                params['startId'] = safe_start
            payload = _get_json(endpoint, params) or {}
            _cache_set(cache_key, payload)

        if isinstance(payload, list):
            page_records = [row for row in payload if isinstance(row, dict)]
        elif isinstance(payload, dict):
            raw_records = payload.get('records') or payload.get('history') or []
            page_records = [row for row in raw_records if isinstance(row, dict)]
        else:
            page_records = []

        if not page_records:
            break

        records.extend(page_records)

        if len(page_records) < safe_page_size:
            break
        page += 1

    return records


def fetch_radarr_history(
    start_id: int | None = None,
    *,
    page_size: int = 250,
    max_pages: int = 50,
    url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> List[Dict]:
    url = url or _default_radarr_endpoint()[0]
    api_key = api_key or _default_radarr_endpoint()[1]
    if not url or not api_key:
        return []

    endpoint = _build_endpoint(url, 'history')
    return _fetch_arr_history(
        endpoint=endpoint,
        api_key=api_key,
        start_id=start_id,
        page_size=page_size,
        max_pages=max_pages,
        cache_prefix='radarr_history',
    )


def fetch_sonarr_history(
    start_id: int | None = None,
    *,
    page_size: int = 250,
    max_pages: int = 50,
    url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> List[Dict]:
    url = url or _default_sonarr_endpoint()[0]
    api_key = api_key or _default_sonarr_endpoint()[1]
    if not url or not api_key:
        return []

    endpoint = _build_endpoint(url, 'history')
    return _fetch_arr_history(
        endpoint=endpoint,
        api_key=api_key,
        start_id=start_id,
        page_size=page_size,
        max_pages=max_pages,
        cache_prefix='sonarr_history',
    )


def trigger_radarr_movie_search(
    movie_id: int,
    *,
    url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> bool:
    """Trigger a targeted Radarr movie search command."""
    url = url or _default_radarr_endpoint()[0]
    api_key = api_key or _default_radarr_endpoint()[1]
    if not url or not api_key or not movie_id:
        return False

    endpoint = _build_endpoint(url, 'command')
    payload = {'name': 'MoviesSearch', 'movieIds': [int(movie_id)]}
    result = _request_json('POST', endpoint, payload=payload, api_key=api_key)
    return result is not None


def set_radarr_movie_monitored(
    movie_id: int,
    monitored: bool = True,
    *,
    url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> bool:
    """Set Radarr monitored state for a movie by id."""
    url = url or _default_radarr_endpoint()[0]
    api_key = api_key or _default_radarr_endpoint()[1]
    if not url or not api_key or not movie_id:
        return False

    endpoint = _build_endpoint(url, f'movie/{int(movie_id)}')
    movie_payload = _request_json('GET', endpoint, params={'apikey': api_key})
    if not isinstance(movie_payload, dict):
        return False

    movie_payload['monitored'] = bool(monitored)
    updated = _request_json('PUT', endpoint, payload=movie_payload, api_key=api_key)
    return updated is not None


def fetch_sonarr_episode_item(
    episode_id: int,
    *,
    url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[Dict]:
    """Fetch one Sonarr episode by id."""
    url = url or _default_sonarr_endpoint()[0]
    api_key = api_key or _default_sonarr_endpoint()[1]
    if not url or not api_key or not episode_id:
        return None

    endpoint = _build_endpoint(url, f'episode/{int(episode_id)}')
    cache_key = f'sonarr_episode_item:{endpoint}'
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = _get_json(endpoint, {'apikey': api_key})
    if isinstance(data, dict):
        _cache_set(cache_key, data)
        return data
    return None


def set_sonarr_episode_monitored(
    episode_ids: List[int],
    monitored: bool = True,
    *,
    url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Dict[str, int]:
    """Set monitored state for Sonarr episode ids one-by-one."""
    url = url or _default_sonarr_endpoint()[0]
    api_key = api_key or _default_sonarr_endpoint()[1]
    if not url or not api_key:
        return {'updated': 0, 'failed': len(episode_ids or [])}

    updated = 0
    failed = 0
    for raw_id in episode_ids or []:
        try:
            episode_id = int(raw_id)
        except Exception:
            failed += 1
            continue

        endpoint = _build_endpoint(url, f'episode/{episode_id}')
        episode_payload = _request_json('GET', endpoint, params={'apikey': api_key})
        if not isinstance(episode_payload, dict):
            failed += 1
            continue

        episode_payload['monitored'] = bool(monitored)
        resp = _request_json('PUT', endpoint, payload=episode_payload, api_key=api_key)
        if resp is None:
            failed += 1
        else:
            updated += 1

    return {'updated': updated, 'failed': failed}


def set_sonarr_series_monitored(
    series_id: int,
    monitored: bool = True,
    *,
    include_specials: bool = False,
    season_numbers: Optional[List[int]] = None,
    url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> bool:
    """Set series monitored state and optionally specific seasons."""
    url = url or _default_sonarr_endpoint()[0]
    api_key = api_key or _default_sonarr_endpoint()[1]
    if not url or not api_key or not series_id:
        return False

    endpoint = _build_endpoint(url, f'series/{int(series_id)}')
    series_payload = _request_json('GET', endpoint, params={'apikey': api_key})
    if not isinstance(series_payload, dict):
        return False

    series_payload['monitored'] = bool(monitored)
    target_seasons = {int(s) for s in season_numbers or []}
    if target_seasons:
        seasons = series_payload.get('seasons') or []
        for season in seasons:
            try:
                sn = int(season.get('seasonNumber'))
            except Exception:
                continue
            if sn == 0 and not include_specials:
                continue
            if sn in target_seasons:
                season['monitored'] = bool(monitored)

    result = _request_json('PUT', endpoint, payload=series_payload, api_key=api_key)
    return result is not None


def trigger_sonarr_search(
    *,
    series_id: int,
    episode_ids: Optional[List[int]] = None,
    season_number: Optional[int] = None,
    url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> bool:
    """Trigger Sonarr search at episode, season, or series scope."""
    url = url or _default_sonarr_endpoint()[0]
    api_key = api_key or _default_sonarr_endpoint()[1]
    if not url or not api_key or not series_id:
        return False

    endpoint = _build_endpoint(url, 'command')
    if episode_ids:
        payload = {'name': 'episodeSearch', 'episodeIds': [int(x) for x in episode_ids]}
    elif season_number is not None:
        payload = {
            'name': 'seasonSearch',
            'seriesId': int(series_id),
            'seasonNumber': int(season_number),
        }
    else:
        payload = {'name': 'seriesSearch', 'seriesId': int(series_id)}

    result = _request_json('POST', endpoint, payload=payload, api_key=api_key)
    return result is not None
