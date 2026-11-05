"""Skyhook (Servarr TVDB metadata) helpers for confident series title/alias matches.

Sonarr's `/api/v3/series/lookup` often returns empty `alternateTitles`. Skyhook search
includes TVDB `alternativeTitles`, which is how aliases like Physical: Asia → Physical: 100
and Pokémon Horizons → Pokémon are represented upstream.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Optional
from urllib.parse import urlencode

import requests

from core.logger import logger

SKYHOOK_SEARCH_URL = "https://skyhook.sonarr.tv/v1/tvdb/search/en"
SKYHOOK_HTTP_TIMEOUT_SECONDS = 20
_CACHE_TTL_SECONDS = 300
_CACHE_MAX_ENTRIES = 256

_cache_lock = threading.Lock()
_cache: OrderedDict[str, dict] = OrderedDict()


def _cache_get(key: str):
    with _cache_lock:
        rec = _cache.get(key)
        if not rec:
            return None
        if (time.time() - rec["ts"]) > _CACHE_TTL_SECONDS:
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        return rec["value"]


def _cache_set(key: str, value) -> None:
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
        _cache[key] = {"ts": time.time(), "value": value}
        while len(_cache) > _CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)


def search_series(term: str) -> list[dict[str, Any]]:
    """Search Skyhook by title. Returns a list of show dicts (may be empty)."""
    name = str(term or "").strip()
    if not name:
        return []
    cache_key = f"skyhook:search:{name.lower()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return list(cached)

    url = f"{SKYHOOK_SEARCH_URL}?{urlencode({'term': name})}"
    try:
        response = requests.get(url, timeout=SKYHOOK_HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logger.debug(
            f"Skyhook series search failed for {name!r}: {exc}",
            extra={"emoji_type": "debug"},
        )
        return []

    rows = [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []
    _cache_set(cache_key, rows)
    return list(rows)


def alternative_titles_from_skyhook(row: dict[str, Any] | None) -> list[str]:
    """Primary-compatible alternate title strings from a Skyhook show payload."""
    if not isinstance(row, dict):
        return []
    out: list[str] = []
    seen: set[str] = set()
    raw_list = row.get("alternativeTitles") or row.get("alternateTitles") or []
    if not isinstance(raw_list, list):
        return []
    from services.watchlist.remap import normalize_title

    for raw in raw_list:
        if isinstance(raw, dict):
            title = str(raw.get("title") or "").strip()
        else:
            title = str(raw or "").strip()
        if not title:
            continue
        key = normalize_title(title)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(title)
    return out


def skyhook_titles_as_sonarr_alts(row: dict[str, Any] | None) -> list[dict[str, str]]:
    """Shape Skyhook alts like Sonarr `alternateTitles` (`[{title: ...}, ...]`)."""
    return [{"title": title} for title in alternative_titles_from_skyhook(row)]


def match_skyhook_series(
    title: str,
    *,
    year: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Return a Skyhook show when the title matches primary or alternativeTitles.

    Prefers year when multiple exact/alias matches exist and year is known.
    """
    from services.watchlist.remap import normalize_title

    want = normalize_title(title)
    if not want:
        return None
    rows = search_series(title)
    if not rows:
        return None

    want_year: Optional[int] = None
    try:
        parsed = int(year) if year is not None else 0
        want_year = parsed if parsed > 0 else None
    except (TypeError, ValueError):
        want_year = None

    def _year(row: dict) -> Optional[int]:
        for key in ("year", "firstAired"):
            raw = row.get(key)
            if key == "firstAired" and isinstance(raw, str) and len(raw) >= 4:
                raw = raw[:4]
            try:
                parsed = int(raw or 0)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
        return None

    def _primary(row: dict) -> bool:
        return normalize_title(str(row.get("title") or "")) == want

    def _alias(row: dict) -> bool:
        return any(normalize_title(alt) == want for alt in alternative_titles_from_skyhook(row))

    exact = [row for row in rows if _primary(row)]
    if want_year:
        year_exact = [row for row in exact if _year(row) == want_year]
        if year_exact:
            return year_exact[0]
    if exact:
        return exact[0]

    alias = [row for row in rows if _alias(row)]
    if want_year:
        year_alias = [row for row in alias if _year(row) == want_year]
        if year_alias:
            return year_alias[0]
    if alias:
        return alias[0]
    return None
