"""Detect when Arr lookup resolves a watchlist title to a different primary Arr title.

Plex Discover can list aliases (e.g. Pokémon Horizons) that TVDB/Sonarr fold into a
parent series (Pokémon). Placeholdarr must surface that remap before Accept and use
the Arr identity for library presence.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable

from core.logger import logger


def normalize_title(value: str | None) -> str:
    """Lowercase, strip accents/punctuation, collapse whitespace for title compare."""
    text = str(value or "").strip()
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = re.sub(r"[^a-z0-9]+", " ", without_marks.lower())
    return " ".join(cleaned.split())


def alternate_titles_from_lookup(lookup: dict[str, Any] | None) -> list[str]:
    """Collect alternate title strings from a Radarr/Sonarr lookup or catalog payload."""
    if not isinstance(lookup, dict):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in lookup.get("alternateTitles") or []:
        if isinstance(raw, dict):
            title = str(raw.get("title") or raw.get("AlternateTitle") or "").strip()
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


def titles_match(a: str | None, b: str | None) -> bool:
    na = normalize_title(a)
    nb = normalize_title(b)
    return bool(na) and na == nb


def lookup_is_remap(lookup: dict[str, Any] | None, watchlist_title: str | None) -> bool:
    """True when Arr primary title differs from the watchlist title."""
    if not isinstance(lookup, dict):
        return False
    want = str(watchlist_title or "").strip()
    if not want:
        return False
    arr_title = str(lookup.get("title") or "").strip()
    if not arr_title:
        return False
    if titles_match(arr_title, want):
        return False
    # Match via alternateTitles still counts as a remap (primary title differs).
    return True


def _coerce_id(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def remap_metadata_from_lookup(
    lookup: dict[str, Any] | None,
    *,
    watchlist_title: str | None,
) -> dict[str, Any] | None:
    """Build serializable remap / Arr-identity metadata from an Arr lookup row."""
    if not isinstance(lookup, dict):
        return None
    arr_title = str(lookup.get("title") or "").strip()
    if not arr_title:
        return None
    want = str(watchlist_title or "").strip()
    remapped = lookup_is_remap(lookup, want)
    matched_via_alternate = False
    if remapped and want:
        want_n = normalize_title(want)
        for alt in alternate_titles_from_lookup(lookup):
            if normalize_title(alt) == want_n:
                matched_via_alternate = True
                break
    year = _coerce_id(lookup.get("year"))
    tvdb = _coerce_id(lookup.get("tvdbId") if lookup.get("tvdbId") is not None else lookup.get("tvdb_id"))
    tmdb = _coerce_id(lookup.get("tmdbId") if lookup.get("tmdbId") is not None else lookup.get("tmdb_id"))
    imdb = str(lookup.get("imdbId") or lookup.get("imdb_id") or "").strip() or None
    return {
        "remapped": bool(remapped),
        "matched_via_alternate": bool(matched_via_alternate),
        "watchlist_title": want or None,
        "arr_title": arr_title,
        "arr_year": year,
        "tvdb_id": tvdb,
        "tmdb_id": tmdb,
        "imdb_id": imdb,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }


def get_cached_arr_remap(extra: Any) -> dict[str, Any] | None:
    if not isinstance(extra, dict):
        return None
    raw = extra.get("arr_remap")
    return raw if isinstance(raw, dict) else None


def set_cached_arr_remap(extra: Any, meta: dict[str, Any] | None) -> dict[str, Any]:
    base = dict(extra) if isinstance(extra, dict) else {}
    if meta is None:
        base.pop("arr_remap", None)
    else:
        base["arr_remap"] = meta
    return base


def catalog_ids_from_remap(meta: dict[str, Any] | None) -> tuple[int | None, int | None, str | None]:
    """Return (tmdb_id, tvdb_id, imdb_id) from cached remap metadata."""
    if not isinstance(meta, dict):
        return None, None, None
    return (
        _coerce_id(meta.get("tmdb_id")),
        _coerce_id(meta.get("tvdb_id")),
        str(meta.get("imdb_id") or "").strip() or None,
    )


def payload_title_aliases(payload: Any) -> list[str]:
    """Titles from a stored Arr payload (primary + alternateTitles)."""
    if not isinstance(payload, dict):
        return []
    out: list[str] = []
    primary = str(payload.get("title") or "").strip()
    if primary:
        out.append(primary)
    out.extend(alternate_titles_from_lookup(payload))
    return out


def resolve_lookup_for_watchlist_row(
    row: Any,
    *,
    url: str,
    api_key: str,
) -> dict[str, Any] | None:
    """Live Arr lookup for one watchlist ORM row. Returns the raw lookup dict or None."""
    from services.source_of_truth import arr_api

    media_type = str(getattr(row, "media_type", None) or "").strip().lower()
    title = getattr(row, "title", None)
    year = getattr(row, "year", None)
    tmdb_id = getattr(row, "tmdb_id", None)
    tvdb_id = getattr(row, "tvdb_id", None)
    imdb_id = getattr(row, "imdb_id", None)
    if media_type == "movie":
        return arr_api.lookup_movie(
            url=url,
            api_key=api_key,
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            title=title,
            year=year,
        )
    return arr_api.lookup_series(
        url=url,
        api_key=api_key,
        tvdb_id=tvdb_id,
        tmdb_id=tmdb_id,
        imdb_id=imdb_id,
        title=title,
        year=year,
    )


def ensure_arr_remap_cached(
    session,
    rows: Iterable[Any],
    *,
    force: bool = False,
    max_lookups: int = 40,
) -> int:
    """Resolve and cache Arr remap metadata on queue/new rows. Returns rows updated."""
    from core.config import settings

    row_list = [r for r in rows if r is not None]
    if not row_list:
        return 0

    # Prefer first enabled target per media type.
    from services.watchlist.catalog import effective_targets

    movie_targets = effective_targets("movie")
    show_targets = effective_targets("show")
    movie_inst = None
    show_inst = None
    if movie_targets:
        key = str(movie_targets[0].get("instance_key") or "").strip()
        movie_inst = settings.resolve_arr_instance("radarr", instance_key=key) if key else None
    if show_targets:
        key = str(show_targets[0].get("instance_key") or "").strip()
        show_inst = settings.resolve_arr_instance("sonarr", instance_key=key) if key else None

    updated = 0
    lookups = 0
    for row in row_list:
        if lookups >= max(1, int(max_lookups or 40)):
            break
        if getattr(row, "excluded", False):
            continue
        status = str(getattr(row, "status", "") or "")
        # Resolve for queue (new) and already-accepted rows that still lack identity.
        if status not in {"new", "in_arr", "added", "failed"}:
            continue
        cached = get_cached_arr_remap(getattr(row, "extra", None))
        if cached and not force:
            continue
        media_type = str(getattr(row, "media_type", "") or "").strip().lower()
        inst = movie_inst if media_type == "movie" else show_inst
        if not inst:
            continue
        url = str(inst.get("url") or "")
        api_key = str(inst.get("api_key") or "")
        if not url or not api_key:
            continue
        lookups += 1
        try:
            lookup = resolve_lookup_for_watchlist_row(row, url=url, api_key=api_key)
        except Exception as exc:
            logger.debug(
                f"Watchlist Arr remap lookup failed for {getattr(row, 'title', None)!r}: {exc}",
                extra={"emoji_type": "debug"},
            )
            continue
        meta = remap_metadata_from_lookup(lookup, watchlist_title=getattr(row, "title", None))
        if meta is None:
            meta = {
                "remapped": False,
                "lookup_miss": True,
                "watchlist_title": str(getattr(row, "title", None) or "").strip() or None,
                "resolved_at": datetime.now(timezone.utc).isoformat(),
            }
        row.extra = set_cached_arr_remap(getattr(row, "extra", None), meta)
        updated += 1
    if updated and session is not None:
        try:
            session.flush()
        except Exception:
            pass
    return updated


def watchlist_entries_for_library_item(
    session,
    *,
    media_type: str,
    title: str | None,
    tmdb_id: int | None = None,
    tvdb_id: int | None = None,
    imdb_id: str | None = None,
) -> list[dict[str, Any]]:
    """Watchlist memberships that map to this library Arr title (direct ids or remap)."""
    from services.postgres.models import PlexWatchlistItem, PlexWatchlistMembership, PlexWatchlistUser

    want_media = "movie" if media_type == "movie" else "show"
    tmdb_i = _coerce_id(tmdb_id)
    tvdb_i = _coerce_id(tvdb_id)
    imdb_s = str(imdb_id or "").strip() or None
    lib_title_n = normalize_title(title)

    rows = (
        session.query(PlexWatchlistItem)
        .filter(
            PlexWatchlistItem.media_type == want_media,
            PlexWatchlistItem.excluded.is_(False),
        )
        .all()
    )
    matched: list[PlexWatchlistItem] = []
    for row in rows:
        if _row_maps_to_library(
            row,
            lib_title_n=lib_title_n,
            tmdb_i=tmdb_i,
            tvdb_i=tvdb_i,
            imdb_s=imdb_s,
        ):
            matched.append(row)
    if not matched:
        return []

    users_by_id = {u.plex_user_id: u for u in session.query(PlexWatchlistUser).all()}
    item_ids = [int(r.id) for r in matched]
    mems = (
        session.query(PlexWatchlistMembership)
        .filter(PlexWatchlistMembership.watchlist_item_id.in_(item_ids))
        .all()
    )
    mems_by_item: dict[int, list[PlexWatchlistMembership]] = {i: [] for i in item_ids}
    for mem in mems:
        mems_by_item.setdefault(int(mem.watchlist_item_id), []).append(mem)

    out: list[dict[str, Any]] = []
    for row in matched:
        remap = get_cached_arr_remap(getattr(row, "extra", None))
        alias = None
        if isinstance(remap, dict) and remap.get("remapped"):
            alias = str(row.title or "").strip() or None
        elif lib_title_n and normalize_title(row.title) and normalize_title(row.title) != lib_title_n:
            alias = str(row.title or "").strip() or None
        people: list[dict[str, Any]] = []
        for mem in mems_by_item.get(int(row.id), []):
            uid = str(mem.plex_user_id or "")
            user = users_by_id.get(uid)
            people.append(
                {
                    "plex_user_id": uid,
                    "title": (user.title if user else uid),
                    "username": (user.username if user else None),
                    "thumb_url": (user.thumb_url if user else None),
                    "is_admin": bool(user.is_admin) if user else False,
                }
            )
        people.sort(key=lambda u: (0 if u.get("is_admin") else 1, str(u.get("title") or "").lower()))
        out.append(
            {
                "watchlist_item_id": int(row.id),
                "watchlist_title": str(row.title or "").strip(),
                "watchlist_year": row.year,
                "status": "excluded" if row.excluded else row.status,
                "alias_of_library": alias,
                "remapped": bool(isinstance(remap, dict) and remap.get("remapped")),
                "users": people,
            }
        )
    out.sort(key=lambda e: str(e.get("watchlist_title") or "").lower())
    return out


def _row_maps_to_library(
    row: Any,
    *,
    lib_title_n: str,
    tmdb_i: int | None,
    tvdb_i: int | None,
    imdb_s: str | None,
) -> bool:
    try:
        row_tmdb = int(row.tmdb_id) if row.tmdb_id not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        row_tmdb = None
    try:
        row_tvdb = int(row.tvdb_id) if row.tvdb_id not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        row_tvdb = None
    row_imdb = str(row.imdb_id or "").strip() or None

    if tmdb_i and row_tmdb and tmdb_i == row_tmdb:
        return True
    if tvdb_i and row_tvdb and tvdb_i == row_tvdb:
        return True
    if imdb_s and row_imdb and imdb_s == row_imdb:
        return True

    remap = get_cached_arr_remap(getattr(row, "extra", None))
    if isinstance(remap, dict):
        rtmdb, rtvdb, rimdb = catalog_ids_from_remap(remap)
        if tmdb_i and rtmdb and tmdb_i == rtmdb:
            return True
        if tvdb_i and rtvdb and tvdb_i == rtvdb:
            return True
        if imdb_s and rimdb and imdb_s == rimdb:
            return True
        if lib_title_n and normalize_title(remap.get("arr_title")) == lib_title_n:
            return True

    if lib_title_n and normalize_title(row.title) == lib_title_n:
        return True
    return False
