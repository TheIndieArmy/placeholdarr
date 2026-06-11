"""Thin TMDB API client for the Collections rule builder.

Supports the source blocks (trending / popular / upcoming / discover / list) plus
metadata helpers used by the builder UI (genres, watch providers, regions).
Responses are cached in-process with a TTL so scheduled runs and UI previews do
not hammer TMDB rate limits.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Optional

import requests

from core.config import settings
from core.logger import logger

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_HTTP_TIMEOUT_SECONDS = 20

# Discover/list pagination guardrail: TMDB pages hold 20 items.
MAX_PAGES_PER_SOURCE = 10

_CACHE_TTL_SECONDS = 12 * 3600
_META_CACHE_TTL_SECONDS = 24 * 3600

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}

# Courtesy throttle: TMDB allows ~50 req/s; we stay far below it so scheduled runs,
# previews, and explain calls can never burst into their limiter.
_MIN_REQUEST_INTERVAL_SECONDS = 0.25
_throttle_lock = threading.Lock()
_last_request_at = 0.0


def _throttle() -> None:
    global _last_request_at
    with _throttle_lock:
        wait = _MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _retry_after_seconds(resp: Any, default: float = 2.0, cap: float = 10.0) -> float:
    try:
        return min(max(float(resp.headers.get("Retry-After", default)), 0.5), cap)
    except (TypeError, ValueError):
        return default


class TmdbError(Exception):
    """Raised when TMDB is unconfigured or a request fails."""


def tmdb_configured() -> bool:
    return bool(getattr(settings, "TMDB_API_KEY", None))


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cache_get(key: str, ttl: float) -> Any | None:
    with _cache_lock:
        hit = _cache.get(key)
    if not hit:
        return None
    stored_at, value = hit
    if (time.monotonic() - stored_at) > ttl:
        return None
    return value


def _cache_set(key: str, value: Any) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)


def _request(path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    api_key = getattr(settings, "TMDB_API_KEY", None)
    if not api_key:
        raise TmdbError("TMDB API key is not configured")

    params = dict(params or {})
    headers = {"Accept": "application/json"}
    # v4 read access tokens are JWTs (contain dots); v3 keys are plain hex strings.
    if "." in api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        params["api_key"] = api_key

    url = f"{TMDB_BASE_URL}{path}"
    try:
        _throttle()
        resp = requests.get(url, params=params, headers=headers, timeout=TMDB_HTTP_TIMEOUT_SECONDS)
        if resp.status_code == 429:
            # One retry honoring Retry-After; TMDB rate limits are short-lived.
            delay = _retry_after_seconds(resp)
            logger.warning(
                f"TMDB rate limited (429) on {path}; retrying in {delay:.1f}s",
                extra={"emoji_type": "warning"},
            )
            time.sleep(delay)
            _throttle()
            resp = requests.get(url, params=params, headers=headers, timeout=TMDB_HTTP_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise TmdbError(f"TMDB request failed: {exc}") from exc

    if resp.status_code == 429:
        raise TmdbError("TMDB rate limit exceeded (429) — try again shortly")
    if resp.status_code == 401:
        raise TmdbError("TMDB rejected the API key (401). Check the TMDB API Key setting.")
    if resp.status_code == 404:
        raise TmdbError(f"TMDB resource not found: {path}")
    if resp.status_code != 200:
        raise TmdbError(f"TMDB returned HTTP {resp.status_code} for {path}")
    try:
        return resp.json()
    except ValueError as exc:
        raise TmdbError(f"TMDB returned invalid JSON for {path}") from exc


def _cache_key(path: str, params: dict[str, Any]) -> str:
    parts = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return f"{path}?{parts}"


def _normalize_item(raw: dict[str, Any], media_type: str) -> dict[str, Any] | None:
    tmdb_id = raw.get("id")
    if not tmdb_id:
        return None
    if media_type == "movie":
        title = raw.get("title") or raw.get("original_title") or ""
        date = raw.get("release_date") or ""
    else:
        title = raw.get("name") or raw.get("original_name") or ""
        date = raw.get("first_air_date") or ""
    year = None
    if date and len(date) >= 4 and date[:4].isdigit():
        year = int(date[:4])
    return {
        "tmdb_id": int(tmdb_id),
        "title": title,
        "year": year,
        "date": date or None,
        "popularity": raw.get("popularity"),
        "vote_average": raw.get("vote_average"),
        "poster_path": raw.get("poster_path"),
    }


def _fetch_paged(path: str, params: dict[str, Any], media_type: str, limit: int) -> list[dict[str, Any]]:
    """Fetch results pages until `limit` items collected or pages are exhausted."""
    limit = max(1, min(int(limit or 100), MAX_PAGES_PER_SOURCE * 20))
    cache_key = _cache_key(path, {**params, "_limit": limit})
    cached = _cache_get(cache_key, _CACHE_TTL_SECONDS)
    if cached is not None:
        return cached

    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    page = 1
    total_pages = 1
    while page <= total_pages and page <= MAX_PAGES_PER_SOURCE and len(items) < limit:
        data = _request(path, {**params, "page": page})
        total_pages = int(data.get("total_pages") or 1)
        for raw in data.get("results") or []:
            normalized = _normalize_item(raw, media_type)
            if not normalized or normalized["tmdb_id"] in seen:
                continue
            seen.add(normalized["tmdb_id"])
            items.append(normalized)
            if len(items) >= limit:
                break
        page += 1

    _cache_set(cache_key, items)
    return items


# ---------------------------------------------------------------------------
# Source endpoints
# ---------------------------------------------------------------------------

def fetch_trending(media_type: str, window: str = "week", limit: int = 100) -> list[dict[str, Any]]:
    kind = "movie" if media_type == "movie" else "tv"
    window = window if window in ("day", "week") else "week"
    return _fetch_paged(f"/trending/{kind}/{window}", {}, media_type, limit)


def fetch_popular(media_type: str, limit: int = 100) -> list[dict[str, Any]]:
    kind = "movie" if media_type == "movie" else "tv"
    return _fetch_paged(f"/{kind}/popular", {}, media_type, limit)


def fetch_upcoming(media_type: str, limit: int = 100) -> list[dict[str, Any]]:
    """Upcoming movies / currently-airing TV."""
    if media_type == "movie":
        return _fetch_paged("/movie/upcoming", {}, media_type, limit)
    return _fetch_paged("/tv/on_the_air", {}, media_type, limit)


def fetch_discover(
    media_type: str,
    *,
    genre_ids: Optional[list[int]] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    provider_ids: Optional[list[int]] = None,
    watch_region: Optional[str] = None,
    min_vote_average: Optional[float] = None,
    sort_by: str = "popularity.desc",
    limit: int = 100,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"sort_by": sort_by}
    if genre_ids:
        params["with_genres"] = ",".join(str(g) for g in genre_ids)
    if provider_ids:
        params["with_watch_providers"] = "|".join(str(p) for p in provider_ids)
        params["watch_region"] = (watch_region or "US").upper()
    if min_vote_average is not None:
        params["vote_average.gte"] = min_vote_average
        params["vote_count.gte"] = 25
    if media_type == "movie":
        if year_from:
            params["primary_release_date.gte"] = f"{int(year_from)}-01-01"
        if year_to:
            params["primary_release_date.lte"] = f"{int(year_to)}-12-31"
        return _fetch_paged("/discover/movie", params, "movie", limit)
    if year_from:
        params["first_air_date.gte"] = f"{int(year_from)}-01-01"
    if year_to:
        params["first_air_date.lte"] = f"{int(year_to)}-12-31"
    return _fetch_paged("/discover/tv", params, "tv", limit)


def fetch_list(list_id: int | str, media_type: str, limit: int = 200) -> list[dict[str, Any]]:
    """Fetch a public TMDB v3 list, filtered to the requested media type."""
    limit = max(1, min(int(limit or 200), 500))
    cache_key = _cache_key(f"/list/{list_id}", {"_limit": limit, "_mt": media_type})
    cached = _cache_get(cache_key, _CACHE_TTL_SECONDS)
    if cached is not None:
        return cached

    wanted = "movie" if media_type == "movie" else "tv"
    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    page = 1
    total_pages = 1
    while page <= total_pages and page <= 25 and len(items) < limit:
        data = _request(f"/list/{list_id}", {"page": page})
        total_pages = int(data.get("total_pages") or 1)
        for raw in data.get("items") or []:
            raw_type = raw.get("media_type") or ("movie" if raw.get("title") else "tv")
            if raw_type != wanted:
                continue
            normalized = _normalize_item(raw, media_type)
            if not normalized or normalized["tmdb_id"] in seen:
                continue
            seen.add(normalized["tmdb_id"])
            items.append(normalized)
            if len(items) >= limit:
                break
        page += 1

    _cache_set(cache_key, items)
    return items


# ---------------------------------------------------------------------------
# Metadata helpers for the builder UI
# ---------------------------------------------------------------------------

def fetch_genres(media_type: str) -> list[dict[str, Any]]:
    kind = "movie" if media_type == "movie" else "tv"
    cache_key = f"meta:genres:{kind}"
    cached = _cache_get(cache_key, _META_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    data = _request(f"/genre/{kind}/list")
    genres = [
        {"id": int(g["id"]), "name": str(g.get("name") or "")}
        for g in (data.get("genres") or [])
        if g.get("id")
    ]
    _cache_set(cache_key, genres)
    return genres


def fetch_watch_providers(media_type: str, watch_region: str = "US") -> list[dict[str, Any]]:
    kind = "movie" if media_type == "movie" else "tv"
    region = (watch_region or "US").upper()
    cache_key = f"meta:providers:{kind}:{region}"
    cached = _cache_get(cache_key, _META_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    data = _request(f"/watch/providers/{kind}", {"watch_region": region})
    providers = sorted(
        (
            {
                "id": int(p["provider_id"]),
                "name": str(p.get("provider_name") or ""),
                "priority": p.get("display_priority"),
            }
            for p in (data.get("results") or [])
            if p.get("provider_id")
        ),
        key=lambda p: (p["priority"] if p["priority"] is not None else 999, p["name"]),
    )
    _cache_set(cache_key, providers)
    return providers


def fetch_regions() -> list[dict[str, Any]]:
    cache_key = "meta:regions"
    cached = _cache_get(cache_key, _META_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    data = _request("/watch/providers/regions")
    regions = sorted(
        (
            {"code": str(r["iso_3166_1"]), "name": str(r.get("english_name") or r["iso_3166_1"])}
            for r in (data.get("results") or [])
            if r.get("iso_3166_1")
        ),
        key=lambda r: r["name"],
    )
    _cache_set(cache_key, regions)
    return regions


def verify_api_key() -> bool:
    """Lightweight configuration check used by the UI."""
    if not tmdb_configured():
        return False
    try:
        _request("/configuration")
        return True
    except TmdbError as exc:
        logger.warning(f"TMDB key verification failed: {exc}", extra={"emoji_type": "warning"})
        return False
