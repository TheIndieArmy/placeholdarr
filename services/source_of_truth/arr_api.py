import re
import threading
import time
from collections import OrderedDict
from datetime import date
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import requests
from requests import RequestException, Timeout
from requests.exceptions import HTTPError

from core.config import settings
from core.logger import logger


_session = requests.Session()
_cache_lock = threading.Lock()
_cache: OrderedDict[str, dict] = OrderedDict()
_DEFAULT_TTL_SECONDS = 120
_CACHE_MAX_ENTRIES = 512


def cache_stats() -> dict[str, int]:
    """Return ARR HTTP cache size for phase metric logs."""
    with _cache_lock:
        return {"entries": len(_cache), "max_entries": _CACHE_MAX_ENTRIES}


def clear_arr_cache() -> None:
    """Drop all cached ARR responses (tests or manual reset)."""
    with _cache_lock:
        _cache.clear()
# Bulk *arr reads (e.g. GET /api/v3/movie) can exceed 30s on large libraries over user-share I/O.
ARR_HTTP_TIMEOUT_SECONDS = 120
# Collections add: *arr often never finishes POST /movie/import (or /series/import)
# even for ~10 titles — the add still proceeds server-side. Wait briefly for a body,
# then poll the library until titles appear (or this deadline).
ARR_LOOKUP_TIMEOUT_SECONDS = 30
ARR_ADD_TIMEOUT_SECONDS = 15
ARR_ADD_RECONCILE_SECONDS = 90
ARR_ADD_RECONCILE_INTERVAL_SECONDS = 3
ARR_IMPORT_CHUNK_SIZE = 20

_last_write_error: dict[str, str | bool | int | None] = {}


def _cache_get(key: str, ttl_seconds: int = _DEFAULT_TTL_SECONDS):
    with _cache_lock:
        rec = _cache.get(key)
        if not rec:
            return None
        if (time.time() - rec['ts']) > ttl_seconds:
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        return rec['value']


def _cache_set(key: str, value):
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
        _cache[key] = {'ts': time.time(), 'value': value}
        while len(_cache) > _CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)


def _build_endpoint(base_url: str, resource: str) -> str:
    root = base_url.rstrip('/')
    if '/api/' in root or root.endswith('/api'):
        return f"{root}/{resource.lstrip('/')}"
    return f"{root}/api/v3/{resource.lstrip('/')}"


def _default_radarr_endpoint() -> tuple[str, str]:
    return settings.resolve_arr_endpoint("radarr", role="primary")


def _default_sonarr_endpoint() -> tuple[str, str]:
    return settings.resolve_arr_endpoint("sonarr", role="primary")


def _get_json(url: str, params: dict, timeout: int = ARR_HTTP_TIMEOUT_SECONDS):
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
    timeout: int = ARR_HTTP_TIMEOUT_SECONDS,
    isolated: bool = False,
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

    # Import POSTs can hang until client timeout. Use a throwaway Session so the
    # shared pool is free for library polls after we give up waiting for a body.
    client = requests.Session() if isolated else _session
    try:
        response = client.request(
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
        _last_write_error.clear()
        _last_write_error.update(
            {
                "timed_out": False,
                "status_code": int(status_code) if status_code else None,
                "message": f"HTTP {status_code} {reason or ''}".strip(),
            }
        )
        return None
    except Timeout as e:
        logger.warning(
            f'ARR write timed out method={method.upper()} url={safe_url} timeout={timeout}s '
            f'error_type={type(e).__name__}; *arr may still apply the write',
            extra={'emoji_type': 'warning'},
        )
        _last_write_error.clear()
        _last_write_error.update(
            {
                "timed_out": True,
                "status_code": None,
                "message": f"did not respond within {timeout}s; it may still add the title(s)",
            }
        )
        return None
    except RequestException as e:
        logger.error(
            f'ARR write failed method={method.upper()} url={safe_url} error_type={type(e).__name__}',
            extra={'emoji_type': 'error'},
        )
        _last_write_error.clear()
        _last_write_error.update({"timed_out": False, "status_code": None, "message": str(e) or type(e).__name__})
        return None
    except Exception as e:
        logger.error(
            f'ARR write failed method={method.upper()} url={safe_url} error_type={type(e).__name__}',
            extra={'emoji_type': 'error'},
        )
        _last_write_error.clear()
        _last_write_error.update({"timed_out": False, "status_code": None, "message": str(e) or type(e).__name__})
        return None
    finally:
        if isolated:
            try:
                client.close()
            except Exception:
                pass


def fetch_radarr_movies(
    url: Optional[str] = None,
    api_key: Optional[str] = None,
    *,
    bypass_cache: bool = False,
) -> List[Dict]:
    url = url or _default_radarr_endpoint()[0]
    api_key = api_key or _default_radarr_endpoint()[1]
    if not url or not api_key:
        return []

    endpoint = _build_endpoint(url, 'movie')
    cache_key = f'radarr_movies:{endpoint}'
    if not bypass_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    data = _get_json(endpoint, {'apikey': api_key}, timeout=30 if bypass_cache else ARR_HTTP_TIMEOUT_SECONDS) or []
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


def fetch_sonarr_series(
    url: Optional[str] = None,
    api_key: Optional[str] = None,
    *,
    bypass_cache: bool = False,
) -> List[Dict]:
    url = url or _default_sonarr_endpoint()[0]
    api_key = api_key or _default_sonarr_endpoint()[1]
    if not url or not api_key:
        return []

    endpoint = _build_endpoint(url, 'series')
    cache_key = f'sonarr_series:season_images:{endpoint}'
    if not bypass_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    data = _get_json(
        endpoint,
        {'apikey': api_key, 'includeSeasonImages': 'true'},
        timeout=30 if bypass_cache else ARR_HTTP_TIMEOUT_SECONDS,
    ) or []
    _cache_set(cache_key, data)
    return data


def fetch_sonarr_series_item(series_id: int, url: Optional[str] = None, api_key: Optional[str] = None) -> Optional[Dict]:
    """Fetch one Sonarr series by id."""
    url = url or _default_sonarr_endpoint()[0]
    api_key = api_key or _default_sonarr_endpoint()[1]
    if not url or not api_key or not series_id:
        return None

    endpoint = _build_endpoint(url, f'series/{int(series_id)}')
    cache_key = f'sonarr_series_item:season_images:{endpoint}'
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = _get_json(endpoint, {'apikey': api_key, 'includeSeasonImages': 'true'})
    if isinstance(data, dict):
        _cache_set(cache_key, data)
        return data
    return None


def fetch_sonarr_episodes(
    series_id: int,
    url: Optional[str] = None,
    api_key: Optional[str] = None,
    *,
    season_number: Optional[int] = None,
) -> List[Dict]:
    """Fetch Sonarr episodes for a series.

    ``season_number=None`` returns every episode (one ``seriesId`` request).

    For **season 0 (specials)** we intentionally do **not** pass ``seasonNumber=0``
    to Sonarr: many stacks omit ``0`` from the query string (treated as empty),
    reverse proxies drop it, or older Sonarr builds respond with ``[]``. Instead
    we reuse the same full list as a normal sync and filter to ``seasonNumber==0``
    in Python — same network cost as one unfiltered call, correct results.

    For any **other** explicit season, we still pass Sonarr's ``seasonNumber``
    query param and trim client-side.
    """
    url = url or _default_sonarr_endpoint()[0]
    api_key = api_key or _default_sonarr_endpoint()[1]
    if not url or not api_key:
        return []

    endpoint = _build_endpoint(url, 'episode')
    # Sonarr defaults omit embedded episodeFile and images on list responses; single GET /episode/{id}
    # always maps with includeEpisodeFile/includeImages true. Request both on bulk sync (one call/series).
    base_params: Dict = {
        'apikey': api_key,
        'seriesId': series_id,
        'includeEpisodeFile': 'true',
        'includeImages': 'true',
    }

    # Specials: never rely on ?seasonNumber=0 — fetch full list, filter locally.
    if season_number is not None and int(season_number) == 0:
        cache_all_key = f'sonarr_episodes:rich:{endpoint}:{series_id}:all'
        cached_all = _cache_get(cache_all_key)
        if cached_all is not None:
            all_rows = cached_all
        else:
            raw = _get_json(endpoint, base_params)
            all_rows = raw if isinstance(raw, list) else []
            _cache_set(cache_all_key, all_rows)
        return [
            x
            for x in all_rows
            if isinstance(x, dict) and int(x.get('seasonNumber') if x.get('seasonNumber') is not None else -1) == 0
        ]

    cache_scope = 'all' if season_number is None else str(int(season_number))
    cache_key = f'sonarr_episodes:rich:{endpoint}:{series_id}:{cache_scope}'
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    params = dict(base_params)
    if season_number is not None:
        params['seasonNumber'] = int(season_number)

    raw = _get_json(endpoint, params)
    data = raw if isinstance(raw, list) else []
    if season_number is not None:
        want = int(season_number)
        data = [x for x in data if isinstance(x, dict) and int(x.get('seasonNumber') if x.get('seasonNumber') is not None else -1) == want]
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


def trigger_radarr_refresh_monitored_downloads(*, is_4k: bool = False) -> bool:
    """Ask Radarr to run its Refresh Monitored Downloads task (updates queue sooner than the built-in timer)."""
    base_url, api_key = settings.resolve_arr_endpoint("radarr", is_4k=is_4k)
    if not base_url or not api_key:
        return False
    endpoint = _build_endpoint(base_url, "command")
    result = _request_json(
        "POST",
        endpoint,
        payload={"name": "RefreshMonitoredDownloads"},
        api_key=api_key,
        timeout=ARR_HTTP_TIMEOUT_SECONDS,
    )
    return result is not None


def trigger_sonarr_refresh_monitored_downloads(*, is_4k: bool = False) -> bool:
    """Ask Sonarr to run its Refresh Monitored Downloads task."""
    base_url, api_key = settings.resolve_arr_endpoint("sonarr", is_4k=is_4k)
    if not base_url or not api_key:
        return False
    endpoint = _build_endpoint(base_url, "command")
    result = _request_json(
        "POST",
        endpoint,
        payload={"name": "RefreshMonitoredDownloads"},
        api_key=api_key,
        timeout=ARR_HTTP_TIMEOUT_SECONDS,
    )
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


def _coerce_lookup_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _first_lookup_row(result: Any) -> Optional[dict]:
    if isinstance(result, list):
        return result[0] if result and isinstance(result[0], dict) else None
    return result if isinstance(result, dict) else None


def _lookup_year(row: dict) -> Optional[int]:
    try:
        parsed = int(row.get("year") or 0)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _pick_title_lookup(result: Any, *, title: str, year: Optional[int]) -> Optional[dict]:
    """Prefer an exact title (and year when known); otherwise the first *arr hit."""
    rows = result if isinstance(result, list) else [result] if isinstance(result, dict) else []
    dicts = [row for row in rows if isinstance(row, dict)]
    if not dicts:
        return None
    want = str(title or "").strip().lower()
    want_year = _coerce_lookup_int(year)
    exact = [row for row in dicts if str(row.get("title") or "").strip().lower() == want]
    if want_year:
        year_exact = [row for row in exact if _lookup_year(row) == want_year]
        if year_exact:
            return year_exact[0]
    if exact:
        return exact[0]
    return dicts[0]


def _arr_lookup(url: str, api_key: str, resource: str, term: str) -> Any:
    endpoint = _build_endpoint(url, resource)
    return _get_json(endpoint, {"apikey": api_key, "term": term}, timeout=ARR_LOOKUP_TIMEOUT_SECONDS)


def lookup_movie(
    *,
    url: str,
    api_key: str,
    tmdb_id: Optional[int] = None,
    imdb_id: Optional[str] = None,
    title: Optional[str] = None,
    year: Optional[int] = None,
) -> Optional[dict]:
    """Resolve a movie in Radarr: TMDB id, then IMDb, then title search."""
    if not url or not api_key:
        return None
    tmdb = _coerce_lookup_int(tmdb_id)
    imdb = str(imdb_id or "").strip() or None
    terms: list[str] = []
    if tmdb:
        terms.append(f"tmdb:{tmdb}")
    if imdb:
        terms.append(f"imdb:{imdb}")
    for term in terms:
        hit = _first_lookup_row(_arr_lookup(url, api_key, "movie/lookup", term))
        if hit:
            return hit
    name = str(title or "").strip()
    if not name:
        return None
    query = f"{name} {int(year)}" if _coerce_lookup_int(year) else name
    return _pick_title_lookup(_arr_lookup(url, api_key, "movie/lookup", query), title=name, year=year)


def lookup_series(
    *,
    url: str,
    api_key: str,
    tvdb_id: Optional[int] = None,
    tmdb_id: Optional[int] = None,
    imdb_id: Optional[str] = None,
    title: Optional[str] = None,
    year: Optional[int] = None,
) -> Optional[dict]:
    """Resolve a series in Sonarr: TVDB, then TMDB, then IMDb, then title search."""
    if not url or not api_key:
        return None
    terms: list[str] = []
    tvdb = _coerce_lookup_int(tvdb_id)
    tmdb = _coerce_lookup_int(tmdb_id)
    imdb = str(imdb_id or "").strip() or None
    if tvdb:
        terms.append(f"tvdb:{tvdb}")
    if tmdb:
        terms.append(f"tmdb:{tmdb}")
    if imdb:
        terms.append(f"imdb:{imdb}")
    for term in terms:
        hit = _first_lookup_row(_arr_lookup(url, api_key, "series/lookup", term))
        if hit:
            return hit
    name = str(title or "").strip()
    if not name:
        return None
    query = f"{name} {int(year)}" if _coerce_lookup_int(year) else name
    return _pick_title_lookup(_arr_lookup(url, api_key, "series/lookup", query), title=name, year=year)


def normalize_arr_tag_label(label: str) -> str:
    """Match Radarr/Sonarr tag slugs: trim and collapse whitespace to dashes."""
    text = str(label or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text


def ensure_arr_tag(*, url: str, api_key: str, label: str) -> Optional[int]:
    """Return the id of an existing tag, creating it if needed."""
    name = normalize_arr_tag_label(label)
    if not name or not url or not api_key:
        return None
    endpoint = _build_endpoint(url, "tag")
    existing = _get_json(endpoint, {"apikey": api_key}, timeout=20)
    if isinstance(existing, list):
        for row in existing:
            if isinstance(row, dict) and str(row.get("label") or "").strip().lower() == name.lower():
                try:
                    return int(row["id"])
                except (TypeError, ValueError, KeyError):
                    continue
    created = _request_json("POST", endpoint, payload={"label": name}, api_key=api_key, timeout=20)
    if isinstance(created, dict) and created.get("id") is not None:
        try:
            return int(created["id"])
        except (TypeError, ValueError):
            return None
    return None


def _existing_arr_id(lookup: dict) -> bool:
    try:
        return int(lookup.get("id") or 0) > 0
    except (TypeError, ValueError):
        return False


def _arr_write_failure_message(arr_name: str) -> str:
    extra = _last_write_error.get("message")
    if extra:
        return f"{arr_name} {extra}"
    return f"{arr_name} rejected the add (already present or invalid payload)"


def _movie_add_payload(
    lookup: dict,
    *,
    quality_profile_id: int,
    root_folder_path: str,
    monitored: bool,
    search: bool,
    tag_ids: Optional[List[int]] = None,
) -> dict:
    payload = dict(lookup)
    payload.pop("id", None)
    payload["qualityProfileId"] = int(quality_profile_id)
    payload["rootFolderPath"] = str(root_folder_path).rstrip("/")
    payload["monitored"] = bool(monitored)
    payload["minimumAvailability"] = payload.get("minimumAvailability") or "released"
    payload["addOptions"] = {"searchForMovie": bool(search)}
    if tag_ids:
        payload["tags"] = [int(t) for t in tag_ids]
    return payload


def _series_add_payload(
    lookup: dict,
    *,
    quality_profile_id: int,
    root_folder_path: str,
    monitored: bool,
    search: bool,
    tag_ids: Optional[List[int]] = None,
) -> dict:
    payload = dict(lookup)
    payload.pop("id", None)
    payload["qualityProfileId"] = int(quality_profile_id)
    payload["rootFolderPath"] = str(root_folder_path).rstrip("/")
    payload["monitored"] = bool(monitored)
    payload["addOptions"] = {"searchForMissingEpisodes": bool(search), "monitor": "all" if monitored else "none"}
    if tag_ids:
        payload["tags"] = [int(t) for t in tag_ids]
    seasons = payload.get("seasons")
    if isinstance(seasons, list) and monitored:
        for season in seasons:
            if isinstance(season, dict) and int(season.get("seasonNumber") or 0) != 0:
                season["monitored"] = True
    return payload


def _lookup_identity_key(lookup: dict, *, movie: bool) -> Optional[str]:
    if movie:
        tmdb = lookup.get("tmdbId") or lookup.get("tmdb_id")
        if tmdb:
            return f"tmdb:{int(tmdb)}"
        imdb = lookup.get("imdbId") or lookup.get("imdb_id")
        if imdb:
            return f"imdb:{imdb}"
        return None
    tvdb = lookup.get("tvdbId") or lookup.get("tvdb_id")
    if tvdb:
        return f"tvdb:{int(tvdb)}"
    tmdb = lookup.get("tmdbId") or lookup.get("tmdb_id")
    if tmdb:
        return f"tmdb:{int(tmdb)}"
    imdb = lookup.get("imdbId") or lookup.get("imdb_id")
    if imdb:
        return f"imdb:{imdb}"
    return None


def _imported_rows_by_identity(imported: Any, *, movie: bool) -> dict[str, dict]:
    mapping: dict[str, dict] = {}
    rows = imported if isinstance(imported, list) else [imported] if isinstance(imported, dict) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _lookup_identity_key(row, movie=movie)
        if key:
            mapping[key] = row
    return mapping


def _apply_arr_id(lookup: dict, row: dict) -> None:
    try:
        arr_id = int(row.get("id") or 0)
    except (TypeError, ValueError):
        arr_id = 0
    if arr_id > 0:
        lookup["id"] = arr_id


def _refresh_lookup(*, url: str, api_key: str, lookup: dict, movie: bool) -> Optional[dict]:
    if movie:
        tmdb = lookup.get("tmdbId") or lookup.get("tmdb_id")
        imdb = lookup.get("imdbId") or lookup.get("imdb_id")
        tmdb_id = None
        try:
            tmdb_id = int(tmdb) if tmdb else None
        except (TypeError, ValueError):
            tmdb_id = None
        return lookup_movie(url=url, api_key=api_key, tmdb_id=tmdb_id, imdb_id=str(imdb) if imdb else None)
    tvdb = lookup.get("tvdbId") or lookup.get("tvdb_id")
    tmdb = lookup.get("tmdbId") or lookup.get("tmdb_id")
    imdb = lookup.get("imdbId") or lookup.get("imdb_id")
    tvdb_id = None
    tmdb_id = None
    try:
        tvdb_id = int(tvdb) if tvdb else None
    except (TypeError, ValueError):
        tvdb_id = None
    try:
        tmdb_id = int(tmdb) if tmdb else None
    except (TypeError, ValueError):
        tmdb_id = None
    return lookup_series(
        url=url,
        api_key=api_key,
        tvdb_id=tvdb_id,
        tmdb_id=tmdb_id,
        imdb_id=str(imdb) if imdb else None,
    )


ArrAddProgress = Callable[[dict[str, Any]], None]
ArrAddIndexStatus = Callable[[int, str, Optional[str]], None]


def _notify_index_status(
    on_status: Optional[ArrAddIndexStatus],
    index: int,
    status: str,
    error: Optional[str] = None,
) -> None:
    if on_status is None:
        return
    try:
        on_status(index, status, error)
    except Exception:
        pass


def _arr_add_item_key(item: Any, instance_key: str) -> str:
    tmdb = getattr(item, "tmdb_id", None)
    tvdb = getattr(item, "tvdb_id", None)
    imdb = getattr(item, "imdb_id", None) or ""
    title = getattr(item, "title", None) or ""
    year = getattr(item, "year", None) or ""
    return f"{instance_key}:{tmdb or ''}:{tvdb or ''}:{imdb}:{title}:{year}"


def _emit_arr_add_progress(
    on_progress: Optional[ArrAddProgress],
    event: dict[str, Any],
) -> None:
    if on_progress is None:
        return
    try:
        on_progress(event)
    except Exception:
        pass


def _library_identity_map(*, url: str, api_key: str, movie: bool) -> dict[str, dict]:
    if movie:
        library = fetch_radarr_movies(url, api_key, bypass_cache=True)
    else:
        endpoint = _build_endpoint(url, "series")
        library = _get_json(endpoint, {"apikey": api_key}, timeout=30) or []
    return _imported_rows_by_identity(library if isinstance(library, list) else [], movie=movie)


def _reconcile_unconfirmed_chunk(
    *,
    url: str,
    api_key: str,
    chunk: list[tuple[int, dict, dict]],
    results: list[Optional[tuple[dict, str, Optional[str]]]],
    movie: bool,
    failure_message: str,
    on_status: Optional[ArrAddIndexStatus] = None,
) -> None:
    """Poll *arr until titles from a hung/empty import actually appear in the library."""
    pending: list[tuple[int, dict]] = [(index, lookup) for index, lookup, _payload in chunk]
    if not pending:
        return
    arr_name = "Radarr" if movie else "Sonarr"
    logger.info(
        f"{arr_name} import body missing; polling library for {len(pending)} title(s) "
        f"up to {ARR_ADD_RECONCILE_SECONDS}s",
        extra={"emoji_type": "info"},
    )
    deadline = time.monotonic() + ARR_ADD_RECONCILE_SECONDS
    while pending and time.monotonic() < deadline:
        by_key = _library_identity_map(url=url, api_key=api_key, movie=movie)
        still: list[tuple[int, dict]] = []
        for index, lookup in pending:
            key = _lookup_identity_key(lookup, movie=movie)
            row = by_key.get(key) if key else None
            if row and _existing_arr_id(row):
                _apply_arr_id(lookup, row)
                results[index] = (lookup, "ok", None)
                _notify_index_status(on_status, index, "ok")
                continue
            still.append((index, lookup))
        pending = still
        if pending:
            remaining = max(0.0, deadline - time.monotonic())
            time.sleep(min(ARR_ADD_RECONCILE_INTERVAL_SECONDS, remaining) if remaining else 0)
    leftover = list(pending)
    for index, lookup in leftover:
        refreshed = _refresh_lookup(url=url, api_key=api_key, lookup=lookup, movie=movie)
        if refreshed and _existing_arr_id(refreshed):
            _apply_arr_id(lookup, refreshed)
            results[index] = (lookup, "ok", None)
            _notify_index_status(on_status, index, "ok")
        else:
            results[index] = (lookup, "error", failure_message)
            _notify_index_status(on_status, index, "error", failure_message)


def _import_body_usable(imported: Any) -> bool:
    if imported is None:
        return False
    if isinstance(imported, list):
        return bool(imported)
    if isinstance(imported, dict):
        return bool(imported)
    return False


def _finish_import_chunk(
    *,
    url: str,
    api_key: str,
    chunk: list[tuple[int, dict, dict]],
    imported: Any,
    results: list[Optional[tuple[dict, str, Optional[str]]]],
    movie: bool,
    arr_name: str,
    on_status: Optional[ArrAddIndexStatus] = None,
) -> None:
    if not _import_body_usable(imported):
        _reconcile_unconfirmed_chunk(
            url=url,
            api_key=api_key,
            chunk=chunk,
            results=results,
            movie=movie,
            failure_message=_arr_write_failure_message(arr_name),
            on_status=on_status,
        )
        return
    by_key = _imported_rows_by_identity(imported, movie=movie)
    unconfirmed: list[tuple[int, dict, dict]] = []
    for index, lookup, payload in chunk:
        key = _lookup_identity_key(lookup, movie=movie)
        row = by_key.get(key) if key else None
        if row and _existing_arr_id(row):
            _apply_arr_id(lookup, row)
            results[index] = (lookup, "ok", None)
            _notify_index_status(on_status, index, "ok")
        elif key and key in by_key:
            results[index] = (lookup, "ok", None)
            _notify_index_status(on_status, index, "ok")
        else:
            unconfirmed.append((index, lookup, payload))
    if unconfirmed:
        _reconcile_unconfirmed_chunk(
            url=url,
            api_key=api_key,
            chunk=unconfirmed,
            results=results,
            movie=movie,
            failure_message=f"{arr_name} did not confirm this title in the import response",
            on_status=on_status,
        )


def add_movies_to_radarr(
    *,
    url: str,
    api_key: str,
    lookups: list[dict],
    quality_profile_id: int,
    root_folder_path: str,
    monitored: bool,
    search: bool,
    tag_ids: Optional[List[int]] = None,
    on_status: Optional[ArrAddIndexStatus] = None,
) -> list[tuple[dict, str, Optional[str]]]:
    """Bulk-add looked-up movies via POST /movie/import.

    Returns one (lookup, status, error) per input lookup, in the same order.
    Lookups that succeed (including after a timeout reconcile) have ARR ``id`` set.
    """
    results: list[Optional[tuple[dict, str, Optional[str]]]] = [None] * len(lookups)
    pending: list[tuple[int, dict, dict]] = []
    for index, lookup in enumerate(lookups):
        if _existing_arr_id(lookup):
            results[index] = (lookup, "skipped", "Already in this Radarr instance")
            _notify_index_status(on_status, index, "skipped", "Already in this Radarr instance")
            continue
        payload = _movie_add_payload(
            lookup,
            quality_profile_id=quality_profile_id,
            root_folder_path=root_folder_path,
            monitored=monitored,
            search=search,
            tag_ids=tag_ids,
        )
        pending.append((index, lookup, payload))
        results[index] = (lookup, "ok", None)

    endpoint = _build_endpoint(url, "movie/import")
    for chunk_start in range(0, len(pending), ARR_IMPORT_CHUNK_SIZE):
        chunk = pending[chunk_start : chunk_start + ARR_IMPORT_CHUNK_SIZE]
        payloads = [item[2] for item in chunk]
        imported = _request_json(
            "POST",
            endpoint,
            payload=payloads,
            api_key=api_key,
            timeout=ARR_ADD_TIMEOUT_SECONDS,
            isolated=True,
        )
        _finish_import_chunk(
            url=url,
            api_key=api_key,
            chunk=chunk,
            imported=imported,
            results=results,
            movie=True,
            arr_name="Radarr",
            on_status=on_status,
        )
    for i, row in enumerate(results):
        if row is None:
            results[i] = (lookups[i], "error", "Radarr import did not return a result")
            _notify_index_status(on_status, i, "error", "Radarr import did not return a result")
    return [row if row is not None else (lookups[i], "error", "Radarr import did not return a result") for i, row in enumerate(results)]


def add_series_to_sonarr_bulk(
    *,
    url: str,
    api_key: str,
    lookups: list[dict],
    quality_profile_id: int,
    root_folder_path: str,
    monitored: bool,
    search: bool,
    tag_ids: Optional[List[int]] = None,
    on_status: Optional[ArrAddIndexStatus] = None,
) -> list[tuple[dict, str, Optional[str]]]:
    """Bulk-add looked-up series via POST /series/import."""
    results: list[Optional[tuple[dict, str, Optional[str]]]] = [None] * len(lookups)
    pending: list[tuple[int, dict, dict]] = []
    for index, lookup in enumerate(lookups):
        if _existing_arr_id(lookup):
            results[index] = (lookup, "skipped", "Already in this Sonarr instance")
            _notify_index_status(on_status, index, "skipped", "Already in this Sonarr instance")
            continue
        payload = _series_add_payload(
            lookup,
            quality_profile_id=quality_profile_id,
            root_folder_path=root_folder_path,
            monitored=monitored,
            search=search,
            tag_ids=tag_ids,
        )
        pending.append((index, lookup, payload))
        results[index] = (lookup, "ok", None)

    endpoint = _build_endpoint(url, "series/import")
    for chunk_start in range(0, len(pending), ARR_IMPORT_CHUNK_SIZE):
        chunk = pending[chunk_start : chunk_start + ARR_IMPORT_CHUNK_SIZE]
        payloads = [item[2] for item in chunk]
        imported = _request_json(
            "POST",
            endpoint,
            payload=payloads,
            api_key=api_key,
            timeout=ARR_ADD_TIMEOUT_SECONDS,
            isolated=True,
        )
        _finish_import_chunk(
            url=url,
            api_key=api_key,
            chunk=chunk,
            imported=imported,
            results=results,
            movie=False,
            arr_name="Sonarr",
            on_status=on_status,
        )
    for i, row in enumerate(results):
        if row is None:
            results[i] = (lookups[i], "error", "Sonarr import did not return a result")
            _notify_index_status(on_status, i, "error", "Sonarr import did not return a result")
    return [row if row is not None else (lookups[i], "error", "Sonarr import did not return a result") for i, row in enumerate(results)]


def add_movie_to_radarr(
    *,
    url: str,
    api_key: str,
    lookup: dict,
    quality_profile_id: int,
    root_folder_path: str,
    monitored: bool,
    search: bool,
    tag_ids: Optional[List[int]] = None,
) -> tuple[str, Optional[str]]:
    """Returns (status, error) where status is ok|skipped|error."""
    row = add_movies_to_radarr(
        url=url,
        api_key=api_key,
        lookups=[lookup],
        quality_profile_id=quality_profile_id,
        root_folder_path=root_folder_path,
        monitored=monitored,
        search=search,
        tag_ids=tag_ids,
    )[0]
    return row[1], row[2]


def add_series_to_sonarr(
    *,
    url: str,
    api_key: str,
    lookup: dict,
    quality_profile_id: int,
    root_folder_path: str,
    monitored: bool,
    search: bool,
    tag_ids: Optional[List[int]] = None,
) -> tuple[str, Optional[str]]:
    row = add_series_to_sonarr_bulk(
        url=url,
        api_key=api_key,
        lookups=[lookup],
        quality_profile_id=quality_profile_id,
        root_folder_path=root_folder_path,
        monitored=monitored,
        search=search,
        tag_ids=tag_ids,
    )[0]
    return row[1], row[2]


def add_missing_titles(
    *,
    media_type: str,
    url: str,
    api_key: str,
    items: list[Any],
    quality_profile_id: int,
    root_folder_path: str,
    monitored: bool,
    search: bool,
    tag_ids: Optional[List[int]] = None,
    instance_key: str = "",
    on_progress: Optional[ArrAddProgress] = None,
) -> list[dict[str, Any]]:
    """Lookup each title, then bulk-import into Radarr or Sonarr."""

    def result_title(item: Any, lookup: Optional[dict] = None) -> str:
        title = str((lookup or {}).get("title") or getattr(item, "title", None) or "Untitled")
        year = getattr(item, "year", None)
        if lookup:
            raw_year = lookup.get("year")
            try:
                year = int(raw_year) if raw_year else year
            except (TypeError, ValueError):
                pass
        return f"{title} ({year})" if year else title

    def row(title: str, status: str, error: Optional[str] = None) -> dict[str, Any]:
        return {"title": title, "instance_key": instance_key, "status": status, "error": error}

    def emit(item: Any, title: str, status: str, error: Optional[str] = None) -> None:
        _emit_arr_add_progress(
            on_progress,
            {
                "type": "item",
                "item_key": _arr_add_item_key(item, instance_key),
                "title": title,
                "instance_key": instance_key,
                "status": status,
                "error": error,
            },
        )

    results: list[dict[str, Any]] = []
    pending_items: list[Any] = []
    pending_lookups: list[dict] = []
    ingest_lookups: list[dict] = []
    movie = media_type == "movie"
    for item in items:
        display = result_title(item)
        emit(item, display, "adding")
        try:
            if movie:
                lookup = lookup_movie(
                    url=url,
                    api_key=api_key,
                    tmdb_id=getattr(item, "tmdb_id", None),
                    imdb_id=getattr(item, "imdb_id", None),
                    title=getattr(item, "title", None),
                    year=getattr(item, "year", None),
                )
                missing = "Radarr lookup found nothing"
            else:
                lookup = lookup_series(
                    url=url,
                    api_key=api_key,
                    tvdb_id=getattr(item, "tvdb_id", None),
                    tmdb_id=getattr(item, "tmdb_id", None),
                    imdb_id=getattr(item, "imdb_id", None),
                    title=getattr(item, "title", None),
                    year=getattr(item, "year", None),
                )
                missing = "Sonarr lookup found nothing"
            if not lookup:
                results.append(row(display, "error", missing))
                emit(item, display, "error", missing)
                continue
            pending_items.append(item)
            pending_lookups.append(lookup)
        except Exception as extra:
            results.append(row(display, "error", str(extra)))
            emit(item, display, "error", str(extra))
    if not pending_lookups:
        return results

    def on_status(index: int, status: str, error: Optional[str] = None) -> None:
        item = pending_items[index]
        lookup = pending_lookups[index]
        emit(item, result_title(item, lookup), status, error)

    adder = add_movies_to_radarr if movie else add_series_to_sonarr_bulk
    added = adder(
        url=url,
        api_key=api_key,
        lookups=pending_lookups,
        quality_profile_id=quality_profile_id,
        root_folder_path=root_folder_path,
        monitored=monitored,
        search=search,
        tag_ids=tag_ids,
        on_status=on_status,
    )
    for item, lookup, (_lookup, status, error) in zip(pending_items, pending_lookups, added):
        arr_id = None
        try:
            arr_id = int(lookup.get("id") or 0) or None
        except (TypeError, ValueError):
            arr_id = None
        results.append({
            "title": result_title(item, lookup),
            "instance_key": instance_key,
            "status": status,
            "error": error,
            "arr_id": arr_id,
        })
        if status in {"ok", "skipped"} and arr_id:
            ingest_lookups.append(lookup)
    if ingest_lookups and instance_key:
        try:
            from services.handlers import enqueue_synthetic_arr_adds

            enqueue_synthetic_arr_adds(
                instance_key=instance_key,
                movie=movie,
                lookups=ingest_lookups,
            )
        except Exception as exc:
            logger.warning(
                f"Failed to enqueue placeholder ingest after ARR add: {exc}",
                extra={"emoji_type": "warning"},
            )
    return results

