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
from urllib.parse import parse_qs, urlparse

import requests

from core.config import settings
from core.logger import logger

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_HTTP_TIMEOUT_SECONDS = 20
# Transient network failures (timeouts, connection drops): try again before giving up.
TMDB_TRANSIENT_ATTEMPTS = 3
TMDB_TRANSIENT_BACKOFF_SECONDS = (1.0, 2.0, 4.0)

# Discover/list pagination guardrail: TMDB pages hold 20 items.
# TMDB discover/list endpoints report at most 500 pages (20 results each → 10k).
MAX_PAGES_PER_SOURCE = 500
MAX_ITEMS_PER_SOURCE = MAX_PAGES_PER_SOURCE * 20

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


class TmdbPagedResult(list):
    """Paged fetch results. ``partial`` is set when paging stopped early after a failure."""

    partial: bool = False
    failed_at_page: int | None = None
    error: str | None = None


def _as_paged_result(
    items: list[dict[str, Any]],
    *,
    partial: bool = False,
    failed_at_page: int | None = None,
    error: str | None = None,
) -> TmdbPagedResult:
    out = TmdbPagedResult(items)
    out.partial = bool(partial)
    out.failed_at_page = failed_at_page
    out.error = error
    return out


_TMDB_RESOURCE_RE = re.compile(
    r"themoviedb\.org/(person|company|keyword|collection|list)/(\d+)",
    re.IGNORECASE,
)

# Discover sorts the TMDB website exposes on keyword/company movie+TV tabs.
_TMDB_DISCOVER_SORTS = frozenset(
    {
        "popularity.asc",
        "popularity.desc",
        "vote_average.asc",
        "vote_average.desc",
        "vote_count.asc",
        "vote_count.desc",
        "primary_release_date.asc",
        "primary_release_date.desc",
        "release_date.asc",
        "release_date.desc",
        "first_air_date.asc",
        "first_air_date.desc",
        "original_title.asc",
        "original_title.desc",
        "title.asc",
        "title.desc",
        "revenue.asc",
        "revenue.desc",
    }
)
DEFAULT_DISCOVER_SORT = "popularity.desc"


def parse_tmdb_resource_kind(value: str) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    match = _TMDB_RESOURCE_RE.search(text)
    if match:
        return match.group(1).lower()
    return None


def parse_tmdb_sort_by(value: str, media_type: str = "movie") -> str:
    """Read ``sort_by`` from a TMDB URL. Bare ids and unknown values use popularity.desc."""
    text = str(value or "").strip()
    raw = None
    if "sort_by=" in text.lower() or "?" in text:
        try:
            raw = (parse_qs(urlparse(text).query).get("sort_by") or [None])[0]
        except (TypeError, ValueError):
            raw = None
    return normalize_tmdb_sort_by(raw, media_type)


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


def normalize_tmdb_sort_by(sort_by: Optional[str], media_type: str = "movie") -> str:
    raw = str(sort_by or "").strip()
    if raw in _TMDB_DISCOVER_SORTS:
        value = raw
    else:
        value = DEFAULT_DISCOVER_SORT
    if media_type != "movie" and value in {
        "primary_release_date.asc",
        "primary_release_date.desc",
        "release_date.asc",
        "release_date.desc",
    }:
        direction = "desc" if value.endswith(".desc") else "asc"
        return f"first_air_date.{direction}"
    if media_type == "movie" and value in {"first_air_date.asc", "first_air_date.desc"}:
        direction = "desc" if value.endswith(".desc") else "asc"
        return f"primary_release_date.{direction}"
    return value


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
        raise TmdbError("TMDB API key is not configured (Settings → Optional APIs)")

    params = dict(params or {})
    headers = {"Accept": "application/json"}
    # v4 read access tokens are JWTs (contain dots); v3 keys are plain hex strings.
    if "." in api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        params["api_key"] = api_key

    url = f"{TMDB_BASE_URL}{path}"
    last_network_error: Exception | None = None

    for attempt in range(1, TMDB_TRANSIENT_ATTEMPTS + 1):
        try:
            _throttle()
            resp = requests.get(url, params=params, headers=headers, timeout=TMDB_HTTP_TIMEOUT_SECONDS)
            if resp.status_code == 429:
                delay = _retry_after_seconds(resp)
                logger.warning(
                    f"TMDB rate limited (429) on {path}; waiting {delay:.1f}s "
                    f"(attempt {attempt}/{TMDB_TRANSIENT_ATTEMPTS})",
                    extra={"emoji_type": "warning"},
                )
                time.sleep(delay)
                _throttle()
                resp = requests.get(url, params=params, headers=headers, timeout=TMDB_HTTP_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            last_network_error = exc
            if attempt >= TMDB_TRANSIENT_ATTEMPTS:
                break
            backoff = TMDB_TRANSIENT_BACKOFF_SECONDS[
                min(attempt - 1, len(TMDB_TRANSIENT_BACKOFF_SECONDS) - 1)
            ]
            logger.warning(
                f"TMDB request failed on {path} ({exc}); retrying in {backoff:.1f}s "
                f"(attempt {attempt}/{TMDB_TRANSIENT_ATTEMPTS})",
                extra={"emoji_type": "warning"},
            )
            time.sleep(backoff)
            continue

        if resp.status_code == 429:
            if attempt >= TMDB_TRANSIENT_ATTEMPTS:
                raise TmdbError("TMDB rate limit exceeded (429) — try again shortly")
            delay = _retry_after_seconds(resp)
            logger.warning(
                f"TMDB still rate limited (429) on {path}; waiting {delay:.1f}s "
                f"(attempt {attempt}/{TMDB_TRANSIENT_ATTEMPTS})",
                extra={"emoji_type": "warning"},
            )
            time.sleep(delay)
            continue
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

    raise TmdbError(f"TMDB request failed: {last_network_error}") from last_network_error


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
        "overview": raw.get("overview") or None,
    }


def _fetch_paged(path: str, params: dict[str, Any], media_type: str, limit: int) -> list[dict[str, Any]]:
    """Fetch results pages until `limit` items collected or pages are exhausted.

    Respects the in-process request throttle and transient retries in ``_request``.
    Page budget follows the requested limit (capped at TMDB's 500-page ceiling).

    If a page fails after some items were collected, returns those items as a
    partial result (does not cache) instead of discarding the whole fetch.
    """
    limit = max(1, min(int(limit or 100), MAX_ITEMS_PER_SOURCE))
    max_pages = min(MAX_PAGES_PER_SOURCE, max(1, (limit + 19) // 20))
    cache_key = _cache_key(path, {**params, "_limit": limit})
    cached = _cache_get(cache_key, _CACHE_TTL_SECONDS)
    if cached is not None:
        return cached

    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    page = 1
    total_pages = 1
    while page <= total_pages and page <= max_pages and len(items) < limit:
        if page == 1 or page % 10 == 0:
            logger.info(
                f"TMDB fetch {path}: page {page}/{min(total_pages, max_pages)} "
                f"({len(items)}/{limit} items so far)",
                extra={"emoji_type": "info"},
            )
        try:
            data = _request(path, {**params, "page": page})
        except TmdbError as exc:
            if items:
                logger.warning(
                    f"TMDB fetch {path} failed at page {page} after {len(items)} item(s); "
                    f"keeping partial results ({exc})",
                    extra={"emoji_type": "warning"},
                )
                return _as_paged_result(
                    items,
                    partial=True,
                    failed_at_page=page,
                    error=str(exc),
                )
            raise
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

    result = _as_paged_result(items)
    _cache_set(cache_key, list(result))
    return result


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
    limit = max(1, min(int(limit or 200), MAX_ITEMS_PER_SOURCE))
    max_pages = min(MAX_PAGES_PER_SOURCE, max(1, (limit + 19) // 20))
    cache_key = _cache_key(f"/list/{list_id}", {"_limit": limit, "_mt": media_type})
    cached = _cache_get(cache_key, _CACHE_TTL_SECONDS)
    if cached is not None:
        return cached

    wanted = "movie" if media_type == "movie" else "tv"
    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    page = 1
    total_pages = 1
    while page <= total_pages and page <= max_pages and len(items) < limit:
        try:
            data = _request(f"/list/{list_id}", {"page": page})
        except TmdbError as exc:
            if items:
                logger.warning(
                    f"TMDB list {list_id} failed at page {page} after {len(items)} item(s); "
                    f"keeping partial results ({exc})",
                    extra={"emoji_type": "warning"},
                )
                return _as_paged_result(
                    items,
                    partial=True,
                    failed_at_page=page,
                    error=str(exc),
                )
            raise
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

    result = _as_paged_result(items)
    _cache_set(cache_key, list(result))
    return result


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


def fetch_company(
    company_ref: str,
    media_type: str,
    limit: int = 200,
    sort_by: Optional[str] = None,
) -> list[dict[str, Any]]:
    company_id = parse_tmdb_resource_id(company_ref, "company")
    kind = "movie" if media_type == "movie" else "tv"
    sort_by = normalize_tmdb_sort_by(sort_by or parse_tmdb_sort_by(company_ref, media_type), media_type)
    return _fetch_paged(
        f"/discover/{kind}",
        {"with_companies": company_id, "sort_by": sort_by},
        media_type,
        limit,
    )


def fetch_keyword(
    keyword_ref: str,
    media_type: str,
    limit: int = 200,
    sort_by: Optional[str] = None,
) -> list[dict[str, Any]]:
    keyword_id = parse_tmdb_resource_id(keyword_ref, "keyword")
    kind = "movie" if media_type == "movie" else "tv"
    sort_by = normalize_tmdb_sort_by(sort_by or parse_tmdb_sort_by(keyword_ref, media_type), media_type)
    return _fetch_paged(
        f"/discover/{kind}",
        {"with_keywords": keyword_id, "sort_by": sort_by},
        media_type,
        limit,
    )


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


TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/original"
_IMAGES_CACHE_TTL_SECONDS = 3600.0


def tmdb_poster_cdn_url(file_path: str | None) -> str | None:
    path = str(file_path or "").strip()
    if not path:
        return None
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{TMDB_IMAGE_BASE}{path}"


def _poster_lang_key(iso_639_1: Any) -> str:
    raw = str(iso_639_1 or "").strip().lower()
    return raw if raw else "null"


def _dedupe_langs(langs: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in langs:
        lang = str(raw or "").strip().lower()
        if not lang or lang == "null" or lang in seen:
            continue
        seen.add(lang)
        ordered.append(lang)
    return ordered


def include_image_language_param(sought_languages: list[str]) -> str:
    """Build TMDB include_image_language for exact sought codes only (no en/null padding)."""
    ordered = _dedupe_langs(sought_languages)
    return ",".join(ordered) if ordered else "en"


def pick_exact_poster_from_images(
    images_payload: dict[str, Any] | None,
    *,
    sought_languages: list[str],
) -> tuple[str | None, str | None]:
    """Return ``(file_path, matched_iso_639_1)`` for the first exact sought-language hit.

    Does not fall back to English, null-language, or arbitrary posters.
    """
    posters = (images_payload or {}).get("posters") if isinstance(images_payload, dict) else None
    if not isinstance(posters, list) or not posters:
        return None, None
    order = _dedupe_langs(sought_languages)
    if not order:
        return None, None
    by_lang: dict[str, list[dict[str, Any]]] = {}
    for entry in posters:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("file_path") or "").strip()
        if not path:
            continue
        key = _poster_lang_key(entry.get("iso_639_1"))
        if key == "null":
            continue
        by_lang.setdefault(key, []).append(entry)
    for lang in order:
        candidates = by_lang.get(lang) or []
        if not candidates:
            continue
        path = str(candidates[0].get("file_path") or "").strip() or None
        if path:
            return path, lang
    return None, None


def fetch_movie_original_language(tmdb_id: int) -> str | None:
    tid = int(tmdb_id)
    cache_key = f"/movie/{tid}?fields=original_language"
    cached = _cache_get(cache_key, _IMAGES_CACHE_TTL_SECONDS)
    if cached is not None:
        text = str(cached).strip().lower()
        return text or None
    data = _request(f"/movie/{tid}")
    lang = str((data or {}).get("original_language") or "").strip().lower() or None
    _cache_set(cache_key, lang or "")
    return lang


def fetch_tv_original_language(tmdb_id: int) -> str | None:
    tid = int(tmdb_id)
    cache_key = f"/tv/{tid}?fields=original_language"
    cached = _cache_get(cache_key, _IMAGES_CACHE_TTL_SECONDS)
    if cached is not None:
        text = str(cached).strip().lower()
        return text or None
    data = _request(f"/tv/{tid}")
    lang = str((data or {}).get("original_language") or "").strip().lower() or None
    _cache_set(cache_key, lang or "")
    return lang


def fetch_movie_images(tmdb_id: int, *, sought_languages: list[str] | None = None, preferred_language: str = "en") -> dict[str, Any]:
    tid = int(tmdb_id)
    langs = list(sought_languages) if sought_languages is not None else [preferred_language]
    include = include_image_language_param(langs)
    params = {"include_image_language": include}
    cache_key = _cache_key(f"/movie/{tid}/images", params)
    cached = _cache_get(cache_key, _IMAGES_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    data = _request(f"/movie/{tid}/images", params)
    _cache_set(cache_key, data)
    return data


def fetch_tv_images(tmdb_id: int, *, sought_languages: list[str] | None = None, preferred_language: str = "en") -> dict[str, Any]:
    tid = int(tmdb_id)
    langs = list(sought_languages) if sought_languages is not None else [preferred_language]
    include = include_image_language_param(langs)
    params = {"include_image_language": include}
    cache_key = _cache_key(f"/tv/{tid}/images", params)
    cached = _cache_get(cache_key, _IMAGES_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    data = _request(f"/tv/{tid}/images", params)
    _cache_set(cache_key, data)
    return data


def fetch_tv_season_images(
    tmdb_id: int,
    season_number: int,
    *,
    sought_languages: list[str] | None = None,
    preferred_language: str = "en",
) -> dict[str, Any]:
    tid = int(tmdb_id)
    sn = int(season_number)
    langs = list(sought_languages) if sought_languages is not None else [preferred_language]
    include = include_image_language_param(langs)
    params = {"include_image_language": include}
    cache_key = _cache_key(f"/tv/{tid}/season/{sn}/images", params)
    cached = _cache_get(cache_key, _IMAGES_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    data = _request(f"/tv/{tid}/season/{sn}/images", params)
    _cache_set(cache_key, data)
    return data
