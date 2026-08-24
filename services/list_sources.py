"""External list sources for the Collections rule builder (beyond TMDB).

Public lists only — no account linking:
- MDBList: public list JSON export, no API key required.
- Trakt: public user lists and charts, requires a Client ID (header auth, no OAuth).
- StevenLu: public popular-movies JSON (or a compatible custom URL).
- AniList: public user anime lists via GraphQL (rate-limited).
- Tautulli: most popular / most watched via API (optional URL + API key).
- Radarr/Sonarr tags: titles carrying a chosen *arr tag.

Items are normalized to the same candidate shape the engine expects, with
multi-provider ids so catalog matching can fall back to IMDb/TVDB when a
TMDB id is missing (common for MDBList).
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Optional

import requests

from core.config import settings

LIST_HTTP_TIMEOUT_SECONDS = 30
_CACHE_TTL_SECONDS = 12 * 3600

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}


class ListSourceError(Exception):
    """Raised when a list source is unconfigured, unreachable, or malformed."""


def trakt_configured() -> bool:
    return bool(getattr(settings, "TRAKT_CLIENT_ID", None))


def tautulli_configured() -> bool:
    url = str(getattr(settings, "TAUTULLI_URL", None) or "").strip()
    key = str(getattr(settings, "TAUTULLI_API_KEY", None) or "").strip()
    return bool(url and key)


def _cache_get(key: str) -> Any | None:
    with _cache_lock:
        hit = _cache.get(key)
    if not hit:
        return None
    stored_at, value = hit
    if (time.monotonic() - stored_at) > _CACHE_TTL_SECONDS:
        return None
    return value


def _cache_set(key: str, value: Any) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)


def _get_with_retry(
    url: str,
    *,
    headers: dict[str, str],
    params: Optional[dict[str, Any]] = None,
    source_name: str,
) -> requests.Response:
    """GET with a single 429 retry honoring Retry-After (capped at 10s)."""
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=LIST_HTTP_TIMEOUT_SECONDS)
        if resp.status_code == 429:
            try:
                delay = min(max(float(resp.headers.get("Retry-After", 2)), 0.5), 10.0)
            except (TypeError, ValueError):
                delay = 2.0
            time.sleep(delay)
            resp = requests.get(url, params=params, headers=headers, timeout=LIST_HTTP_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise ListSourceError(f"{source_name} request failed: {exc}") from exc
    if resp.status_code == 429:
        raise ListSourceError(f"{source_name} rate limit exceeded (429) — try again shortly")
    return resp


def _normalize_candidate(
    *,
    title: str,
    year: Optional[int],
    tmdb_id: Optional[int],
    imdb_id: Optional[str],
    tvdb_id: Optional[int],
    rank: Optional[int],
    date: Optional[str] = None,
    poster_path: Optional[str] = None,
    genre_names: Optional[list[str]] = None,
    original_language: Optional[str] = None,
    vote_average: Optional[float] = None,
    vote_count: Optional[int] = None,
    ratings: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "tmdb_id": int(tmdb_id) if tmdb_id else None,
        "imdb_id": str(imdb_id) if imdb_id else None,
        "tvdb_id": int(tvdb_id) if tvdb_id else None,
        "title": title,
        "year": year,
        "date": date,
        "popularity": float(-(rank or 0)) if rank else None,
        "vote_average": vote_average,
        "vote_count": vote_count,
        "poster_path": poster_path,
        "genre_names": [str(g).strip().lower() for g in (genre_names or []) if g],
        "original_language": original_language,
        "ratings": ratings or {},
    }


# ---------------------------------------------------------------------------
# MDBList
# ---------------------------------------------------------------------------

_MDBLIST_URL_RE = re.compile(r"mdblist\.com/lists/([^/\s]+)/([^/\s?#]+)", re.IGNORECASE)


def parse_mdblist_reference(reference: str) -> tuple[str, str]:
    """Accept a pasted MDBList URL or 'user/slug' and return (user, slug)."""
    text = str(reference or "").strip()
    if not text:
        raise ListSourceError("MDBList source requires a list URL or user/slug")
    match = _MDBLIST_URL_RE.search(text)
    if match:
        return match.group(1), match.group(2)
    parts = [p for p in text.strip("/").split("/") if p]
    if len(parts) == 2:
        return parts[0], parts[1]
    raise ListSourceError(
        f"Could not parse MDBList reference {text!r}; expected a list URL or user/slug"
    )


def fetch_mdblist(reference: str, media_type: str, limit: int = 200) -> list[dict[str, Any]]:
    """Fetch a public MDBList via its JSON export, filtered to the media type."""
    user, slug = parse_mdblist_reference(reference)
    limit = max(1, min(int(limit or 200), 1000))
    cache_key = f"mdblist:{user}/{slug}:{media_type}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"https://mdblist.com/lists/{user}/{slug}/json"
    resp = _get_with_retry(
        url,
        headers={"Accept": "application/json", "User-Agent": "Placeholdarr"},
        source_name="MDBList",
    )
    if resp.status_code == 404:
        raise ListSourceError(f"MDBList list not found: {user}/{slug}")
    if resp.status_code != 200:
        raise ListSourceError(f"MDBList returned HTTP {resp.status_code} for {user}/{slug}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise ListSourceError(
            f"MDBList returned invalid JSON for {user}/{slug} (is the list public?)"
        ) from exc
    if not isinstance(data, list):
        raise ListSourceError(f"Unexpected MDBList payload shape for {user}/{slug}")

    wanted = "movie" if media_type == "movie" else "show"
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in data:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("mediatype") or "") != wanted:
            continue
        imdb_id = raw.get("imdb_id") or None
        tvdb_id = raw.get("tvdbid") or raw.get("tvdb_id") or None
        tmdb_id = raw.get("tmdbid") or raw.get("tmdb_id") or None
        dedupe_key = str(imdb_id or f"tvdb:{tvdb_id}" or f"tmdb:{tmdb_id}")
        if not (imdb_id or tvdb_id or tmdb_id) or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        try:
            year = int(raw.get("release_year") or 0) or None
        except (TypeError, ValueError):
            year = None
        genre_raw = raw.get("genre") or raw.get("genres") or []
        if isinstance(genre_raw, str):
            genre_names = [g.strip() for g in genre_raw.split(",") if g.strip()]
        elif isinstance(genre_raw, list):
            genre_names = [str(g) for g in genre_raw if g]
        else:
            genre_names = []
        ratings: dict[str, Any] = {}
        for entry in raw.get("ratings") or []:
            if not isinstance(entry, dict):
                continue
            source = str(entry.get("source") or entry.get("name") or "").strip().lower()
            if not source:
                continue
            try:
                ratings[source] = {
                    "value": float(entry.get("value") or entry.get("score") or 0),
                    "votes": int(entry.get("votes") or 0),
                }
            except (TypeError, ValueError):
                continue
        items.append(
            _normalize_candidate(
                title=str(raw.get("title") or ""),
                year=year,
                tmdb_id=tmdb_id,
                imdb_id=imdb_id,
                tvdb_id=tvdb_id,
                rank=raw.get("rank"),
                date=str(raw.get("released") or raw.get("release_date") or "") or None,
                poster_path=raw.get("poster") or raw.get("poster_path"),
                genre_names=genre_names,
                original_language=raw.get("language") or raw.get("original_language"),
                vote_average=raw.get("score_average") or raw.get("tmdb_percent") or raw.get("score"),
                ratings=ratings,
            )
        )
        if len(items) >= limit:
            break

    _cache_set(cache_key, items)
    return items


# ---------------------------------------------------------------------------
# Trakt
# ---------------------------------------------------------------------------

_TRAKT_URL_RE = re.compile(r"trakt\.tv/users/([^/\s]+)/lists/([^/\s?#]+)", re.IGNORECASE)


def parse_trakt_reference(reference: str) -> tuple[str, str]:
    """Accept a pasted Trakt list URL or 'user/slug' and return (user, slug)."""
    text = str(reference or "").strip()
    if not text:
        raise ListSourceError("Trakt source requires a list URL or user/slug")
    match = _TRAKT_URL_RE.search(text)
    if match:
        return match.group(1), match.group(2)
    parts = [p for p in text.strip("/").split("/") if p]
    if len(parts) == 2:
        return parts[0], parts[1]
    raise ListSourceError(
        f"Could not parse Trakt reference {text!r}; expected a list URL or user/slug"
    )


def fetch_trakt_list(reference: str, media_type: str, limit: int = 200) -> list[dict[str, Any]]:
    """Fetch a public Trakt user list, filtered to the media type."""
    client_id = getattr(settings, "TRAKT_CLIENT_ID", None)
    if not client_id:
        raise ListSourceError(
            "Trakt Client ID is not configured (Settings). Creating a Trakt API app currently requires Trakt VIP."
        )
    user, slug = parse_trakt_reference(reference)
    limit = max(1, min(int(limit or 200), 1000))
    cache_key = f"trakt:{user}/{slug}:{media_type}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    kind = "movies" if media_type == "movie" else "shows"
    url = f"https://api.trakt.tv/users/{user}/lists/{slug}/items/{kind}"
    resp = _get_with_retry(
        url,
        params={"limit": limit},
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": str(client_id),
            "User-Agent": "Placeholdarr",
        },
        source_name="Trakt",
    )
    if resp.status_code in (401, 403):
        raise ListSourceError("Trakt rejected the Client ID (check the Trakt Client ID setting)")
    if resp.status_code == 404:
        raise ListSourceError(f"Trakt list not found (or private): {user}/{slug}")
    if resp.status_code != 200:
        raise ListSourceError(f"Trakt returned HTTP {resp.status_code} for {user}/{slug}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise ListSourceError(f"Trakt returned invalid JSON for {user}/{slug}") from exc
    if not isinstance(data, list):
        raise ListSourceError(f"Unexpected Trakt payload shape for {user}/{slug}")

    inner_key = "movie" if media_type == "movie" else "show"
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in data:
        if not isinstance(raw, dict):
            continue
        inner = raw.get(inner_key)
        if not isinstance(inner, dict):
            continue
        ids = inner.get("ids") if isinstance(inner.get("ids"), dict) else {}
        tmdb_id = ids.get("tmdb")
        imdb_id = ids.get("imdb")
        tvdb_id = ids.get("tvdb")
        dedupe_key = f"{tmdb_id}:{imdb_id}:{tvdb_id}"
        if not (tmdb_id or imdb_id or tvdb_id) or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        items.append(
            _normalize_candidate(
                title=str(inner.get("title") or ""),
                year=int(inner.get("year") or 0) or None,
                tmdb_id=tmdb_id,
                imdb_id=imdb_id,
                tvdb_id=tvdb_id,
                rank=raw.get("rank"),
            )
        )
        if len(items) >= limit:
            break

    _cache_set(cache_key, items)
    return items


# ---------------------------------------------------------------------------
# StevenLu
# ---------------------------------------------------------------------------

STEVENLU_DEFAULT_URL = "https://s3.amazonaws.com/popular-movies/movies.json"


def fetch_stevenlu(reference: str, media_type: str, limit: int = 200) -> list[dict[str, Any]]:
    """Fetch StevenLu popular-movies JSON (IMDb ids). Movies only."""
    if media_type != "movie":
        raise ListSourceError("StevenLu lists are movie-only")
    url = str(reference or "").strip() or STEVENLU_DEFAULT_URL
    if not url.startswith("http://") and not url.startswith("https://"):
        raise ListSourceError("StevenLu source needs an https JSON URL")
    limit = max(1, min(int(limit or 200), 1000))
    cache_key = f"stevenlu:{url}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    resp = _get_with_retry(
        url,
        headers={"Accept": "application/json", "User-Agent": "Placeholdarr"},
        source_name="StevenLu",
    )
    if resp.status_code != 200:
        raise ListSourceError(f"StevenLu URL returned HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as extra:
        raise ListSourceError("StevenLu URL did not return JSON") from extra
    if not isinstance(data, list):
        raise ListSourceError("Unexpected StevenLu payload (expected a JSON array)")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in data:
        if not isinstance(raw, dict):
            continue
        imdb_id = raw.get("imdb_id") or raw.get("imdb") or raw.get("imdbid")
        tmdb_id = raw.get("tmdb") or raw.get("tmdb_id") or raw.get("tmdbid")
        title = str(raw.get("title") or raw.get("name") or "")
        key = str(imdb_id or tmdb_id or title)
        if not key or key in seen:
            continue
        if not (imdb_id or tmdb_id):
            continue
        seen.add(key)
        year = None
        try:
            year = int(raw.get("year") or 0) or None
        except (TypeError, ValueError):
            year = None
        items.append(
            _normalize_candidate(
                title=title,
                year=year,
                tmdb_id=int(tmdb_id) if tmdb_id else None,
                imdb_id=str(imdb_id) if imdb_id else None,
                tvdb_id=None,
                rank=len(items) + 1,
            )
        )
        if len(items) >= limit:
            break
    _cache_set(cache_key, items)
    return items


# ---------------------------------------------------------------------------
# AniList
# ---------------------------------------------------------------------------

ANILIST_GRAPHQL_URL = "https://graphql.anilist.co"
_ANILIST_MIN_INTERVAL_SECONDS = 2.1
_anilist_lock = threading.Lock()
_anilist_last_at = 0.0

_ANILIST_USER_RE = re.compile(
    r"anilist\.co/user/([^/\s]+)(?:/(?:animelist|mangalist)(?:/([^/\s?#]+))?)?",
    re.IGNORECASE,
)

_ANILIST_QUERY = """
query ($userName: String, $type: MediaType) {
  MediaListCollection(userName: $userName, type: $type) {
    lists {
      name
      isCustomList
      entries {
        media {
          id
          idMal
          format
          type
          title { english romaji native }
          startDate { year }
        }
      }
    }
  }
}
"""


def parse_anilist_reference(reference: str) -> tuple[str, Optional[str]]:
    """Return (username, optional custom list name) from a URL or user/list."""
    text = str(reference or "").strip()
    if not text:
        raise ListSourceError("AniList source requires a user list URL")
    match = _ANILIST_USER_RE.search(text)
    if match:
        return match.group(1), match.group(2)
    parts = [p for p in text.strip("/").split("/") if p]
    if len(parts) == 1:
        return parts[0], None
    if len(parts) == 2:
        return parts[0], parts[1]
    raise ListSourceError("Could not parse AniList reference; expected anilist.co/user/NAME/animelist")


def _anilist_throttle() -> None:
    global _anilist_last_at
    with _anilist_lock:
        wait = _ANILIST_MIN_INTERVAL_SECONDS - (time.monotonic() - _anilist_last_at)
        if wait > 0:
            time.sleep(wait)
        _anilist_last_at = time.monotonic()


def fetch_anilist(reference: str, media_type: str, limit: int = 200) -> list[dict[str, Any]]:
    """Fetch a public AniList user anime list (one GraphQL call, cached)."""
    user, list_name = parse_anilist_reference(reference)
    limit = max(1, min(int(limit or 200), 1000))
    cache_key = f"anilist:{user}:{list_name or '*'}:{media_type}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    wanted_movie = media_type == "movie"
    payload = {
        "query": _ANILIST_QUERY,
        "variables": {"userName": user, "type": "ANIME"},
    }
    headers = {
        "Accept": "application/json",
        "User-Agent": "Placeholdarr",
        "Content-Type": "application/json",
    }
    try:
        _anilist_throttle()
        resp = requests.post(ANILIST_GRAPHQL_URL, json=payload, headers=headers, timeout=LIST_HTTP_TIMEOUT_SECONDS)
        if resp.status_code == 429:
            try:
                delay = min(max(float(resp.headers.get("Retry-After", 60)), 2.0), 90.0)
            except (TypeError, ValueError):
                delay = 60.0
            time.sleep(delay)
            _anilist_throttle()
            resp = requests.post(ANILIST_GRAPHQL_URL, json=payload, headers=headers, timeout=LIST_HTTP_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise ListSourceError(f"AniList request failed: {exc}") from exc
    if resp.status_code == 429:
        raise ListSourceError("AniList rate limit exceeded — try again in a minute")
    if resp.status_code != 200:
        raise ListSourceError(f"AniList returned HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError as extra:
        raise ListSourceError("AniList returned invalid JSON") from extra
    errors = body.get("errors") if isinstance(body, dict) else None
    if errors:
        message = errors[0].get("message") if errors and isinstance(errors[0], dict) else "AniList query failed"
        raise ListSourceError(str(message))
    collection = ((body.get("data") or {}).get("MediaListCollection") or {})
    lists = collection.get("lists") or []
    wanted_slug = (list_name or "").replace("-", " ").strip().lower()
    entries: list[dict[str, Any]] = []
    for lst in lists:
        if not isinstance(lst, dict):
            continue
        name = str(lst.get("name") or "")
        if wanted_slug and name.replace("-", " ").strip().lower() != wanted_slug:
            continue
        for entry in lst.get("entries") or []:
            if isinstance(entry, dict) and isinstance(entry.get("media"), dict):
                entries.append(entry["media"])
        if wanted_slug:
            break
    if wanted_slug and not entries:
        raise ListSourceError(f"AniList list {list_name!r} was not found for user {user}")

    movie_formats = {"MOVIE"}
    tv_formats = {"TV", "TV_SHORT", "OVA", "ONA", "SPECIAL", "MUSIC"}
    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    from services import tmdb_client

    for media in entries:
        anilist_id = media.get("id")
        if not anilist_id or int(anilist_id) in seen:
            continue
        fmt = str(media.get("format") or "")
        if wanted_movie and fmt not in movie_formats:
            continue
        if not wanted_movie and fmt not in tv_formats and fmt:
            continue
        titles = media.get("title") if isinstance(media.get("title"), dict) else {}
        title = str(titles.get("english") or titles.get("romaji") or titles.get("native") or "")
        year = None
        start = media.get("startDate") if isinstance(media.get("startDate"), dict) else {}
        try:
            year = int(start.get("year") or 0) or None
        except (TypeError, ValueError):
            year = None
        tmdb_id = None
        if tmdb_client.tmdb_configured() and title:
            try:
                tmdb_id = tmdb_client.search_title(title, year, "movie" if wanted_movie else "tv")
            except Exception:
                tmdb_id = None
        if not tmdb_id:
            continue
        seen.add(int(anilist_id))
        items.append(
            _normalize_candidate(
                title=title,
                year=year,
                tmdb_id=tmdb_id,
                imdb_id=None,
                tvdb_id=None,
                rank=len(items) + 1,
            )
        )
        if len(items) >= limit:
            break

    _cache_set(cache_key, items)
    return items


# ---------------------------------------------------------------------------
# Trakt charts (trending / popular / watched / played)
# ---------------------------------------------------------------------------

TRAKT_CHART_SUBTYPES = (
    "trending",
    "popular",
    "watched",
    "played",
    "collected",
)
TRAKT_CHART_PERIODS = ("daily", "weekly", "monthly", "yearly", "all")


def fetch_trakt_chart(
    media_type: str,
    subtype: str = "trending",
    *,
    period: str = "weekly",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Fetch a Trakt chart endpoint (not a user list)."""
    client_id = getattr(settings, "TRAKT_CLIENT_ID", None)
    if not client_id:
        raise ListSourceError(
            "Trakt Client ID is not configured (Settings). Creating a Trakt API app currently requires Trakt VIP."
        )
    chart = str(subtype or "trending").strip().lower()
    if chart not in TRAKT_CHART_SUBTYPES:
        raise ListSourceError(f"Unknown Trakt chart subtype: {chart!r}")
    period_key = str(period or "weekly").strip().lower()
    if period_key not in TRAKT_CHART_PERIODS:
        raise ListSourceError(f"Unknown Trakt chart period: {period_key!r}")
    limit = max(1, min(int(limit or 200), 1000))
    kind = "movies" if media_type == "movie" else "shows"
    if chart in ("trending", "popular"):
        path = f"{kind}/{chart}"
    else:
        path = f"{kind}/{chart}/{period_key}"
    cache_key = f"trakt_chart:{path}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"https://api.trakt.tv/{path}"
    resp = _get_with_retry(
        url,
        params={"limit": limit},
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": str(client_id),
            "User-Agent": "Placeholdarr",
        },
        source_name="Trakt",
    )
    if resp.status_code in (401, 403):
        raise ListSourceError("Trakt rejected the Client ID (check the Trakt Client ID setting)")
    if resp.status_code != 200:
        raise ListSourceError(f"Trakt chart returned HTTP {resp.status_code} for {path}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise ListSourceError(f"Trakt chart returned invalid JSON for {path}") from exc
    if not isinstance(data, list):
        raise ListSourceError(f"Unexpected Trakt chart payload for {path}")

    inner_key = "movie" if media_type == "movie" else "show"
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, raw in enumerate(data):
        if not isinstance(raw, dict):
            continue
        # trending/watched wrap the media object; popular is bare media objects
        inner = raw.get(inner_key) if isinstance(raw.get(inner_key), dict) else raw
        if not isinstance(inner, dict):
            continue
        ids = inner.get("ids") if isinstance(inner.get("ids"), dict) else {}
        tmdb_id = ids.get("tmdb")
        imdb_id = ids.get("imdb")
        tvdb_id = ids.get("tvdb")
        dedupe_key = f"{tmdb_id}:{imdb_id}:{tvdb_id}"
        if not (tmdb_id or imdb_id or tvdb_id) or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        items.append(
            _normalize_candidate(
                title=str(inner.get("title") or ""),
                year=int(inner.get("year") or 0) or None,
                tmdb_id=tmdb_id,
                imdb_id=imdb_id,
                tvdb_id=tvdb_id,
                rank=idx + 1,
            )
        )
        if len(items) >= limit:
            break

    _cache_set(cache_key, items)
    return items


# ---------------------------------------------------------------------------
# Tautulli home stats
# ---------------------------------------------------------------------------

TAUTULLI_STAT_SUBTYPES = ("most_popular", "most_watched")


def fetch_tautulli(
    media_type: str,
    subtype: str = "most_popular",
    *,
    days: int = 30,
    minimum_plays: int = 1,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Fetch Tautulli home stats (requires TAUTULLI_URL + TAUTULLI_API_KEY)."""
    base = str(getattr(settings, "TAUTULLI_URL", None) or "").rstrip("/")
    api_key = str(getattr(settings, "TAUTULLI_API_KEY", None) or "").strip()
    if not base or not api_key:
        raise ListSourceError("Tautulli URL and API key are not configured (Settings → Collections)")
    stat = str(subtype or "most_popular").strip().lower()
    if stat not in TAUTULLI_STAT_SUBTYPES:
        raise ListSourceError(f"Unknown Tautulli subtype: {stat!r}")
    days = max(1, min(int(days or 30), 365))
    minimum_plays = max(1, int(minimum_plays or 1))
    limit = max(1, min(int(limit or 200), 500))
    # Tautulli: popular_* = unique users; top_* = play counts
    if media_type == "movie":
        stat_id = "popular_movies" if stat == "most_popular" else "top_movies"
    else:
        # Prefer shows over episodes for collection membership
        stat_id = "popular_tv" if stat == "most_popular" else "top_tv"
    cache_key = f"tautulli:{stat_id}:{days}:{minimum_plays}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"{base}/api/v2"
    resp = _get_with_retry(
        url,
        params={
            "apikey": api_key,
            "cmd": "get_home_stats",
            "stat_id": stat_id,
            "time_range": days,
            "stats_count": limit,
        },
        headers={"Accept": "application/json", "User-Agent": "Placeholdarr"},
        source_name="Tautulli",
    )
    if resp.status_code != 200:
        raise ListSourceError(f"Tautulli returned HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise ListSourceError("Tautulli returned invalid JSON") from exc
    response = body.get("response") if isinstance(body, dict) else None
    if not isinstance(response, dict) or response.get("result") != "success":
        message = (response or {}).get("message") if isinstance(response, dict) else "Tautulli request failed"
        raise ListSourceError(str(message))
    data = response.get("data")
    rows: list[Any] = []
    if isinstance(data, list):
        # Some versions return a list of stat blocks
        for block in data:
            if isinstance(block, dict) and str(block.get("stat_id") or "") == stat_id:
                rows = block.get("rows") or []
                break
        if not rows and data and isinstance(data[0], dict) and "rows" in data[0]:
            rows = data[0].get("rows") or []
    elif isinstance(data, dict):
        rows = data.get("rows") or []
    if not isinstance(rows, list):
        rows = []

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        plays = raw.get("total_plays") or raw.get("users_watched") or raw.get("plays") or 0
        try:
            if int(plays) < minimum_plays:
                continue
        except (TypeError, ValueError):
            pass
        title = str(raw.get("title") or raw.get("grandparent_title") or "").strip()
        year = None
        try:
            year = int(raw.get("year") or 0) or None
        except (TypeError, ValueError):
            year = None
        # Prefer GUIDs when Tautulli includes them
        guids = raw.get("guids") or raw.get("guid") or []
        if isinstance(guids, str):
            guids = [guids]
        tmdb_id = None
        imdb_id = None
        tvdb_id = None
        for g in guids if isinstance(guids, list) else []:
            text = str(g)
            if "tmdb://" in text or "themoviedb://" in text:
                try:
                    tmdb_id = int(re.search(r"(\d+)", text.split("://", 1)[-1]).group(1))  # type: ignore[union-attr]
                except Exception:
                    pass
            elif "imdb://" in text:
                m = re.search(r"(tt\d+)", text)
                if m:
                    imdb_id = m.group(1)
            elif "tvdb://" in text:
                try:
                    tvdb_id = int(re.search(r"(\d+)", text.split("://", 1)[-1]).group(1))  # type: ignore[union-attr]
                except Exception:
                    pass
        rating_key = str(raw.get("rating_key") or raw.get("grandparent_rating_key") or "")
        dedupe = f"{tmdb_id}:{imdb_id}:{tvdb_id}:{rating_key}:{title}:{year}"
        if not title or dedupe in seen:
            continue
        if not (tmdb_id or imdb_id or tvdb_id):
            # Keep title/year for catalog fuzzy match paths that use imdb/tmdb later;
            # engine matching needs an id — skip when nothing available.
            continue
        seen.add(dedupe)
        items.append(
            _normalize_candidate(
                title=title,
                year=year,
                tmdb_id=tmdb_id,
                imdb_id=imdb_id,
                tvdb_id=tvdb_id,
                rank=len(items) + 1,
            )
        )
        if len(items) >= limit:
            break

    _cache_set(cache_key, items)
    return items


# ---------------------------------------------------------------------------
# Radarr / Sonarr tags
# ---------------------------------------------------------------------------


def fetch_arr_tags(instance_key: str, arr_type: str) -> list[dict[str, Any]]:
    """Return [{id, label}] for an *arr instance (for builder UI)."""
    from services.source_of_truth import arr_api

    instances = getattr(settings, "configured_arr_instances", []) or []
    match = None
    for item in instances:
        if str(item.get("arr_type") or "").lower() != arr_type:
            continue
        if str(item.get("instance_key") or "").lower() == str(instance_key or "").lower():
            match = item
            break
    if not match:
        raise ListSourceError(f"No {arr_type} instance configured for key {instance_key!r}")
    url = str(match.get("url") or "").strip()
    api_key = str(match.get("api_key") or "").strip()
    if not url or not api_key:
        raise ListSourceError(f"{arr_type} instance {instance_key!r} is missing URL or API key")
    endpoint = arr_api._build_endpoint(url, "tag")
    data = arr_api._get_json(endpoint, {"apikey": api_key}, timeout=20) or []
    out: list[dict[str, Any]] = []
    if isinstance(data, list):
        for row in data:
            if not isinstance(row, dict) or row.get("id") is None:
                continue
            out.append({"id": int(row["id"]), "label": str(row.get("label") or row["id"])})
    out.sort(key=lambda t: t["label"].lower())
    return out


def fetch_arr_tag_items(
    media_type: str,
    *,
    instance_key: str,
    tag_id: int,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Titles in Radarr/Sonarr that carry the given tag id."""
    from services.source_of_truth import arr_api

    arr_type = "radarr" if media_type == "movie" else "sonarr"
    instances = getattr(settings, "configured_arr_instances", []) or []
    match = None
    for item in instances:
        if str(item.get("arr_type") or "").lower() != arr_type:
            continue
        if str(item.get("instance_key") or "").lower() == str(instance_key or "").lower():
            match = item
            break
    if not match:
        raise ListSourceError(f"No {arr_type} instance configured for key {instance_key!r}")
    url = str(match.get("url") or "").strip()
    api_key = str(match.get("api_key") or "").strip()
    if not url or not api_key:
        raise ListSourceError(f"{arr_type} instance {instance_key!r} is missing URL or API key")
    try:
        wanted_tag = int(tag_id)
    except (TypeError, ValueError) as exc:
        raise ListSourceError("arr_tag source requires a numeric tag_id") from exc
    limit = max(1, min(int(limit or 500), 2000))
    cache_key = f"arr_tag:{arr_type}:{instance_key}:{wanted_tag}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if arr_type == "radarr":
        rows = arr_api.fetch_radarr_movies(url=url, api_key=api_key) or []
    else:
        rows = arr_api.fetch_sonarr_series(url=url, api_key=api_key) or []

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        tags = raw.get("tags") or []
        try:
            tag_ids = {int(t) for t in tags}
        except (TypeError, ValueError):
            continue
        if wanted_tag not in tag_ids:
            continue
        title = str(raw.get("title") or "")
        year = None
        try:
            year = int(raw.get("year") or 0) or None
        except (TypeError, ValueError):
            year = None
        tmdb_id = raw.get("tmdbId") or raw.get("tmdb_id")
        imdb_id = raw.get("imdbId") or raw.get("imdb_id")
        tvdb_id = raw.get("tvdbId") or raw.get("tvdb_id")
        dedupe = f"{tmdb_id}:{imdb_id}:{tvdb_id}"
        if not (tmdb_id or imdb_id or tvdb_id) or dedupe in seen:
            continue
        seen.add(dedupe)
        items.append(
            _normalize_candidate(
                title=title,
                year=year,
                tmdb_id=int(tmdb_id) if tmdb_id else None,
                imdb_id=str(imdb_id) if imdb_id else None,
                tvdb_id=int(tvdb_id) if tvdb_id else None,
                rank=len(items) + 1,
            )
        )
        if len(items) >= limit:
            break

    _cache_set(cache_key, items)
    return items
