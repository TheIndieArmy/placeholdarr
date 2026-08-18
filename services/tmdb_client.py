"""Thin TMDB API client for the Collections rule builder.

Supports trending / popular / upcoming / discover / list plus person credits,
company, keyword, and collection pages (paste a themoviedb.org URL or numeric
id). Also metadata helpers for the builder UI (genres, watch providers, regions).
Responses are cached in-process with a TTL so scheduled runs and UI previews do
not hammer TMDB rate limits.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Optional

import requests

from core.config import settings
from core.logger import logger

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_HTTP_TIMEOUT_SECONDS = 20

# Discover/list pagination guardrail: TMDB pages hold 20 items.
MAX_PAGES_PER_SOURCE = 25

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


_TMDB_RESOURCE_RE = re.compile(
    r"themoviedb\.org/(person|company|keyword|collection|list)/(\d+)",
    re.IGNORECASE,
)


def parse_tmdb_resource_id(value: str, expected: str) -> str:
    """Accept a TMDB URL or bare numeric id for person/company/keyword/collection/list."""
    text = str(value or "").strip()
    if not text:
        raise TmdbError(f"TMDB {expected} source needs a URL or numeric id")
    match = _TMDB_RESOURCE_RE.search(text)
    if match:
        kind = match.group(1).lower()
        if kind != expected:
            raise TmdbError(f"That TMDB URL is a {kind} page, not a {expected} page")
        return match.group(2)
    digits = re.match(r"^(\d+)\b", text)
    if digits:
        return digits.group(1)
    raise TmdbError(f"Could not parse a TMDB {expected} id from {text!r}")


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
    last_air_date = raw.get("last_air_date") or None
    last_year = None
    if last_air_date and len(str(last_air_date)) >= 4 and str(last_air_date)[:4].isdigit():
        last_year = int(str(last_air_date)[:4])
    elif raw.get("last_year"):
        try:
            last_year = int(raw.get("last_year") or 0) or None
        except (TypeError, ValueError):
            last_year = None
    return {
        "tmdb_id": int(tmdb_id),
        "title": title,
        "year": year,
        "date": date or None,
        "last_air_date": last_air_date,
        "last_year": last_year,
        "popularity": raw.get("popularity"),
        "vote_average": raw.get("vote_average"),
        "vote_count": raw.get("vote_count"),
        "poster_path": raw.get("poster_path"),
        "genre_ids": [int(g) for g in (raw.get("genre_ids") or []) if g is not None],
        "original_language": raw.get("original_language") or None,
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
    list_id = parse_tmdb_resource_id(str(list_id), "list")
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


def fetch_person_credits(person_ref: str, media_type: str, limit: int = 500) -> list[dict[str, Any]]:
    """All unique movie or TV credits for a person (cast and crew)."""
    person_id = parse_tmdb_resource_id(person_ref, "person")
    kind = "movie" if media_type == "movie" else "tv"
    limit = max(1, min(int(limit or 500), 1000))
    cache_key = _cache_key(f"/person/{person_id}/{kind}_credits", {"_limit": limit})
    cached = _cache_get(cache_key, _CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    data = _request(f"/person/{person_id}/{kind}_credits")
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for bucket in ("cast", "crew"):
        for raw in data.get(bucket) or []:
            if not isinstance(raw, dict):
                continue
            normalized = _normalize_item(raw, media_type)
            if not normalized or normalized["tmdb_id"] in seen:
                continue
            seen.add(normalized["tmdb_id"])
            rows.append(normalized)
    rows.sort(key=lambda item: float(item.get("popularity") or 0), reverse=True)
    items = rows[:limit]
    _cache_set(cache_key, items)
    return items


def fetch_company(company_ref: str, media_type: str, limit: int = 200) -> list[dict[str, Any]]:
    company_id = parse_tmdb_resource_id(company_ref, "company")
    kind = "movie" if media_type == "movie" else "tv"
    return _fetch_paged(f"/discover/{kind}", {"with_companies": company_id}, media_type, limit)


def fetch_keyword(keyword_ref: str, media_type: str, limit: int = 200) -> list[dict[str, Any]]:
    keyword_id = parse_tmdb_resource_id(keyword_ref, "keyword")
    kind = "movie" if media_type == "movie" else "tv"
    return _fetch_paged(f"/discover/{kind}", {"with_keywords": keyword_id}, media_type, limit)


def fetch_collection(collection_ref: str, media_type: str, limit: int = 200) -> list[dict[str, Any]]:
    """TMDB movie collection (e.g. Star Wars). TV recipes get an empty set."""
    if media_type != "movie":
        return []
    collection_id = parse_tmdb_resource_id(collection_ref, "collection")
    limit = max(1, min(int(limit or 200), 500))
    cache_key = _cache_key(f"/collection/{collection_id}", {"_limit": limit})
    cached = _cache_get(cache_key, _CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    data = _request(f"/collection/{collection_id}")
    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in data.get("parts") or []:
        normalized = _normalize_item(raw, "movie")
        if not normalized or normalized["tmdb_id"] in seen:
            continue
        seen.add(normalized["tmdb_id"])
        items.append(normalized)
        if len(items) >= limit:
            break
    _cache_set(cache_key, items)
    return items


def search_title(title: str, year: Optional[int], media_type: str) -> Optional[int]:
    """Best-effort first TMDB search hit for AniList (and similar) title matching."""
    query = str(title or "").strip()
    if not query:
        return None
    kind = "movie" if media_type == "movie" else "tv"
    params: dict[str, Any] = {"query": query}
    if year:
        params["year" if kind == "movie" else "first_air_date_year"] = int(year)
    cache_key = _cache_key(f"/search/{kind}", params)
    cached = _cache_get(cache_key, _CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    data = _request(f"/search/{kind}", params)
    tmdb_id = None
    for raw in data.get("results") or []:
        normalized = _normalize_item(raw, media_type)
        if normalized:
            tmdb_id = int(normalized["tmdb_id"])
            break
    _cache_set(cache_key, tmdb_id)
    return tmdb_id


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
