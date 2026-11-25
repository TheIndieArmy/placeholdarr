"""Resolve Collection Set studio / network labels to TMDB ids.

Order of trust:
1. Your library: titles Radarr/Sonarr tagged with this studio or network carry
   TMDB ids, and TMDB details list the exact company / network ids.
2. Studios only: TMDB company search, exact (normalized) name match only.
3. Networks only: a small curated table of well-known ids, exact name match only.

Anything else is unresolved; the set child falls back to library-only.
"""
from __future__ import annotations

import re
import threading
import time
from collections import Counter
from typing import Any, Optional

from sqlalchemy import text

from services import tmdb_client
from services.postgres.db import session_scope

LIBRARY_SAMPLE_SIZE = 6
_RESOLVE_TTL_SECONDS = 6 * 3600

# TMDB has no network name search. Keys are normalized names (see _norm).
CURATED_TMDB_NETWORK_IDS: dict[str, int] = {
    "hbo": 49,
    "netflix": 213,
    "amc": 174,
    "fx": 88,
    "showtime": 67,
    "nbc": 6,
    "cbs": 16,
    "abc": 2,
    "fox": 19,
    "thecw": 71,
    "hulu": 453,
    "appletv+": 2552,
    "appletv": 2552,
    "primevideo": 1024,
    "amazon": 1024,
    "disney+": 2739,
    "bbcone": 4,
    "bbctwo": 332,
}

_lock = threading.Lock()
_resolved: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}


def _norm(name: str) -> str:
    text_val = str(name or "").casefold().replace("&", "and")
    return re.sub(r"[^a-z0-9+]", "", text_val)


def _cached(key: str) -> tuple[bool, Optional[dict[str, Any]]]:
    with _lock:
        hit = _resolved.get(key)
    if not hit or (time.monotonic() - hit[0]) > _RESOLVE_TTL_SECONDS:
        return False, None
    return True, hit[1]


def _store(key: str, value: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    with _lock:
        _resolved[key] = (time.monotonic(), value)
    return value


def clear_cache() -> None:
    with _lock:
        _resolved.clear()


def _library_tmdb_ids(category: str, label: str) -> list[int]:
    if category == "studio":
        sql = text(
            "SELECT DISTINCT tmdbid FROM movie "
            "WHERE is_deleted = false AND tmdbid IS NOT NULL "
            "AND LOWER(TRIM(radarr_studio)) = LOWER(:label) "
            "LIMIT :n"
        )
    else:
        sql = text(
            "SELECT DISTINCT sonarr_tmdbid FROM series "
            "WHERE is_deleted = false AND sonarr_tmdbid IS NOT NULL "
            "AND LOWER(TRIM(sonarr_network)) = LOWER(:label) "
            "LIMIT :n"
        )
    with session_scope() as session:
        return [
            int(row[0])
            for row in session.execute(sql, {"label": label.strip(), "n": LIBRARY_SAMPLE_SIZE})
            if row[0]
        ]


def _from_library(category: str, label: str) -> Optional[dict[str, Any]]:
    """Exact name match on a sampled title's companies/networks, else an id shared by every sample."""
    try:
        sample_ids = _library_tmdb_ids(category, label)
    except Exception:
        return None
    if not sample_ids:
        return None
    want = _norm(label)
    fetch = tmdb_client.fetch_movie_companies if category == "studio" else tmdb_client.fetch_tv_networks
    counts: Counter[int] = Counter()
    names: dict[int, str] = {}
    fetched = 0
    for tmdb_id in sample_ids:
        try:
            entries = fetch(tmdb_id)
        except tmdb_client.TmdbError:
            continue
        fetched += 1
        for entry in entries:
            if _norm(entry["name"]) == want:
                return {"id": entry["id"], "name": entry["name"], "via": "library"}
            counts[entry["id"]] += 1
            names[entry["id"]] = entry["name"]
    if fetched >= 2:
        shared = [eid for eid, n in counts.items() if n == fetched]
        if len(shared) == 1:
            eid = shared[0]
            return {"id": eid, "name": names[eid], "via": "library"}
    return None


def _studio_from_search(label: str) -> Optional[dict[str, Any]]:
    want = _norm(label)
    try:
        rows = tmdb_client.search_companies(label)
    except tmdb_client.TmdbError:
        return None
    for row in rows:
        if _norm(row["name"]) == want:
            return {"id": row["id"], "name": row["name"], "via": "search"}
    return None


def _network_from_curated(label: str) -> Optional[dict[str, Any]]:
    network_id = CURATED_TMDB_NETWORK_IDS.get(_norm(label))
    if network_id is None:
        return None
    return {"id": network_id, "name": label, "via": "curated"}


def resolve(category: str, label: str) -> Optional[dict[str, Any]]:
    """Return ``{id, name, via}`` for a studio or network label, or None when unresolved."""
    if category not in ("studio", "network"):
        return None
    clean = str(label or "").strip()
    if not clean or not tmdb_client.tmdb_configured():
        return None
    key = f"{category}:{_norm(clean)}"
    hit, value = _cached(key)
    if hit:
        return value
    found = _from_library(category, clean)
    if found is None:
        found = _studio_from_search(clean) if category == "studio" else _network_from_curated(clean)
    return _store(key, found)


def resolve_company_id(label: str) -> Optional[int]:
    found = resolve("studio", label)
    return int(found["id"]) if found else None


def resolve_network_id(label: str) -> Optional[int]:
    found = resolve("network", label)
    return int(found["id"]) if found else None
