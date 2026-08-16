"""External list sources for the Collections rule builder (beyond TMDB).

Public lists only — no account linking:
- MDBList: public list JSON export, no API key required.
- Trakt: public user lists, requires a Client ID (header auth, no OAuth).
- StevenLu: public popular-movies JSON (or a compatible custom URL).
- AniList: public user anime lists via GraphQL (rate-limited).

Items are normalized to the same candidate shape the engine expects, with
multi-provider ids so catalog matching can fall back to IMDb/TVDB when a
TMDB id is missing (common for MDBList).
"""
from __future__ import annotations

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
        raise ListSourceError("Trakt Client ID is not configured")
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
