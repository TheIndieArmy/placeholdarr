"""Fetch the Plex account Universal Watchlist (owner) via plex.tv APIs.

Uses the configured Plex account token (``PLEX_TOKEN`` or ``PLEX_WATCHLIST_TOKEN``).
Server library URL is not required for watchlist reads.

RSS (Plex Pass) is used only as an optional change hint; the metadata API is the
source of truth for the full list and guids.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import requests

from core.config import settings
from core.logger import logger

_PLEX_PRODUCT = "Placeholdarr"
_PLEX_CLIENT_ID_DEFAULT = "placeholdarr-watchlist"
_WATCHLIST_PAGE_SIZE = 100


def plex_watchlist_token() -> str | None:
    """Prefer dedicated watchlist/account token; fall back to PLEX_TOKEN."""
    for key in ("PLEX_WATCHLIST_TOKEN", "PLEX_TOKEN"):
        raw = getattr(settings, key, None)
        tok = str(raw or "").strip()
        if tok:
            return tok
    return None


def _client_identifier() -> str:
    raw = str(getattr(settings, "PLEX_OAUTH_CLIENT_ID", None) or "").strip()
    return raw or _PLEX_CLIENT_ID_DEFAULT


def _plex_headers(token: str, *, accept_json: bool = True) -> dict[str, str]:
    headers = {
        "X-Plex-Token": token,
        "X-Plex-Product": _PLEX_PRODUCT,
        "X-Plex-Client-Identifier": _client_identifier(),
        "X-Plex-Version": "1.0",
        "X-Plex-Platform": "Linux",
        "X-Plex-Device": "Placeholdarr",
        "X-Plex-Device-Name": "Placeholdarr",
    }
    if accept_json:
        headers["Accept"] = "application/json"
    return headers


def _parse_guid_ids(guids: list[Any] | None, *, top_guid: str | None = None) -> dict[str, Any]:
    """Extract tmdb/tvdb/imdb ids from Plex Guid entries."""
    out: dict[str, Any] = {"tmdb_id": None, "tvdb_id": None, "imdb_id": None, "plex_guid": None}
    blobs: list[str] = []
    if top_guid:
        blobs.append(str(top_guid))
    for g in guids or []:
        if isinstance(g, dict):
            blobs.append(str(g.get("id") or g.get("tag") or ""))
        else:
            blobs.append(str(g or ""))
    for blob in blobs:
        s = blob.strip()
        if not s:
            continue
        if out["plex_guid"] is None and s.startswith("plex://"):
            out["plex_guid"] = s
        low = s.lower()
        m = re.search(r"(?:tmdb|themoviedb)://(?:movie|show|tv)?/?(\d+)", low)
        if m and out["tmdb_id"] is None:
            out["tmdb_id"] = int(m.group(1))
        m = re.search(r"tvdb://(\d+)", low)
        if m and out["tvdb_id"] is None:
            out["tvdb_id"] = int(m.group(1))
        m = re.search(r"imdb://(tt\d+)", low)
        if m and out["imdb_id"] is None:
            out["imdb_id"] = m.group(1)
    return out


def _media_type_from_item(item: dict[str, Any]) -> str | None:
    t = str(item.get("type") or item.get("librarySectionType") or "").strip().lower()
    if t in {"movie", "1"}:
        return "movie"
    if t in {"show", "series", "tv", "2"}:
        return "show"
    # Discover watchlist sometimes uses subtype
    st = str(item.get("subtype") or "").strip().lower()
    if st in {"movie"}:
        return "movie"
    if st in {"show", "series"}:
        return "show"
    return None


def _normalize_watchlist_item(raw: dict[str, Any]) -> dict[str, Any] | None:
    media_type = _media_type_from_item(raw)
    if media_type not in {"movie", "show"}:
        return None
    title = str(raw.get("title") or raw.get("titleSort") or "").strip()
    if not title:
        return None
    year = raw.get("year")
    try:
        year_i = int(year) if year is not None else None
    except (TypeError, ValueError):
        year_i = None
    rating_key = str(raw.get("ratingKey") or raw.get("key") or "").strip() or None
    guids = raw.get("Guid") or raw.get("guid") or []
    if isinstance(guids, str):
        guids = [guids]
    ids = _parse_guid_ids(guids if isinstance(guids, list) else [], top_guid=str(raw.get("guid") or "") or None)
    plex_guid = ids.get("plex_guid") or (str(raw.get("guid") or "").strip() or None)
    return {
        "title": title,
        "year": year_i,
        "media_type": media_type,
        "rating_key": rating_key,
        "plex_guid": plex_guid,
        "tmdb_id": ids.get("tmdb_id"),
        "tvdb_id": ids.get("tvdb_id"),
        "imdb_id": ids.get("imdb_id"),
        "thumb": raw.get("thumb") or raw.get("composite"),
    }


def ensure_watchlist_rss(token: str, *, feed_type: str = "watchlist") -> str | None:
    """Create/return a watchlist RSS URL (Plex Pass). Returns None if unavailable."""
    try:
        resp = requests.post(
            "https://discover.provider.plex.tv/rss",
            params={"X-Plex-Token": token},
            json={"feedType": feed_type},
            headers=_plex_headers(token),
            timeout=20,
        )
        if resp.status_code >= 400:
            logger.debug(
                f"Plex watchlist RSS unavailable status={resp.status_code}",
                extra={"emoji_type": "debug"},
            )
            return None
        data = resp.json() if resp.content else {}
        info = data.get("RSSInfo") if isinstance(data, dict) else None
        if isinstance(info, list) and info:
            url = info[0].get("url") if isinstance(info[0], dict) else None
            return str(url) if url else None
    except Exception as exc:
        logger.debug(f"Plex watchlist RSS error: {exc}", extra={"emoji_type": "debug"})
    return None


def fetch_owner_watchlist(*, token: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    """Fetch the signed-in account's Universal Watchlist items (normalized)."""
    tok = (token or plex_watchlist_token() or "").strip()
    if not tok:
        raise RuntimeError("No Plex token configured for watchlist (set PLEX_TOKEN or PLEX_WATCHLIST_TOKEN)")

    items: list[dict[str, Any]] = []
    start = 0
    cap = max(1, min(int(limit or 500), 2000))
    while start < cap:
        size = min(_WATCHLIST_PAGE_SIZE, cap - start)
        url = (
            "https://discover.provider.plex.tv/library/sections/watchlist/all"
            f"?X-Plex-Container-Start={start}&X-Plex-Container-Size={size}"
        )
        resp = requests.get(url, headers=_plex_headers(tok), timeout=45)
        if resp.status_code == 401:
            raise RuntimeError("Plex rejected the watchlist token (unauthorized)")
        if resp.status_code >= 400:
            # Fallback metadata host
            alt = (
                "https://metadata.provider.plex.tv/library/sections/watchlist/all"
                f"?X-Plex-Container-Start={start}&X-Plex-Container-Size={size}"
            )
            resp = requests.get(alt, headers=_plex_headers(tok), timeout=45)
            if resp.status_code >= 400:
                raise RuntimeError(f"Plex watchlist fetch failed HTTP {resp.status_code}")
        data = resp.json() if resp.content else {}
        media = []
        if isinstance(data, dict):
            container = data.get("MediaContainer") if isinstance(data.get("MediaContainer"), dict) else data
            media = container.get("Metadata") or container.get("Directory") or []
        if not isinstance(media, list) or not media:
            break
        for raw in media:
            if not isinstance(raw, dict):
                continue
            norm = _normalize_watchlist_item(raw)
            if norm:
                items.append(norm)
        if len(media) < size:
            break
        start += size
    return items


def test_watchlist_access(token: str | None = None) -> dict[str, Any]:
    """Lightweight probe for Settings / OAuth completion."""
    tok = (token or plex_watchlist_token() or "").strip()
    if not tok:
        return {"ok": False, "error": "No Plex token configured"}
    try:
        items = fetch_owner_watchlist(token=tok, limit=5)
        rss = ensure_watchlist_rss(tok)
        return {
            "ok": True,
            "sample_count": len(items),
            "rss_available": bool(rss),
            "titles": [i["title"] for i in items[:5]],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
