"""Lightweight validation of collection source URLs / references.

Used by the recipe builder Validate control so users can confirm a pasted
URL before running a full preview/sync.
"""
from __future__ import annotations

from typing import Any

from core.config import settings
from services import list_sources
from services import tmdb_client


TMDB_KIND_LABELS = {
    "person": "Person",
    "company": "Company",
    "keyword": "Keyword",
    "collection": "Collection",
    "list": "List",
}


def validate_source_reference(
    *,
    source_type: str,
    media_type: str,
    reference: str,
    subtype: str | None = None,
) -> dict[str, Any]:
    """Return a validation payload for a source URL/ref.

    Shape:
      ok: bool
      source_type: str
      kind: str | None          # e.g. person, list, chart
      title: str | None         # human label for the resolved resource
      detail: str | None        # extra info (counts, user/slug, etc.)
      suggested_title: str | None
      error: str | None
    """
    media = "show" if media_type == "show" else "movie"
    stype = str(source_type or "").strip().lower()
    ref = str(reference or "").strip()
    sub = str(subtype or "").strip().lower() or None

    # Unified TMDB card with subtype=page uses the same path as tmdb_url.
    if stype == "tmdb" and sub == "page":
        stype = "tmdb_url"
    if stype == "trakt" and sub == "list":
        stype = "trakt_list"

    try:
        if stype in ("tmdb_url", "tmdb_person", "tmdb_company", "tmdb_keyword", "tmdb_collection", "tmdb_list"):
            return _validate_tmdb(stype, ref, media)
        if stype == "mdblist":
            return _validate_mdblist(ref, media)
        if stype == "trakt_list":
            return _validate_trakt_list(ref, media)
        if stype == "anilist":
            return _validate_anilist(ref, media)
        if stype == "stevenlu":
            return _validate_stevenlu(ref, media)
    except (tmdb_client.TmdbError, list_sources.ListSourceError) as exc:
        return _fail(stype, str(exc))
    except Exception as exc:
        return _fail(stype, str(exc))

    return _fail(stype, f"Validate is not available for source type {stype!r}")


def _ok(
    source_type: str,
    *,
    kind: str | None,
    title: str | None,
    detail: str | None = None,
    suggested_title: str | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "source_type": source_type,
        "kind": kind,
        "title": title,
        "detail": detail,
        "suggested_title": suggested_title or title,
        "error": None,
    }


def _fail(source_type: str, error: str) -> dict[str, Any]:
    return {
        "ok": False,
        "source_type": source_type,
        "kind": None,
        "title": None,
        "detail": None,
        "suggested_title": None,
        "error": error,
    }


def _validate_tmdb(source_type: str, reference: str, media_type: str) -> dict[str, Any]:
    if not tmdb_client.tmdb_configured():
        raise tmdb_client.TmdbError("TMDB API key is not configured")
    if not reference:
        raise tmdb_client.TmdbError("Paste a TMDB page URL or id")

    expected = {
        "tmdb_person": "person",
        "tmdb_company": "company",
        "tmdb_keyword": "keyword",
        "tmdb_collection": "collection",
        "tmdb_list": "list",
    }.get(source_type)

    kind = expected or tmdb_client.parse_tmdb_resource_kind(reference)
    if not kind:
        # Bare numeric id without a URL — only valid for legacy typed sources.
        if expected:
            kind = expected
        else:
            raise tmdb_client.TmdbError(
                "Could not tell what kind of TMDB page that is — paste a full page URL"
            )
    if expected and kind != expected:
        raise tmdb_client.TmdbError(f"That TMDB URL is a {kind} page, not a {expected} page")

    resource_id = tmdb_client.parse_tmdb_resource_id(reference, kind)
    data = tmdb_client._request(f"/{kind}/{resource_id}")
    title = (
        str(data.get("name") or data.get("title") or data.get("original_name") or "").strip()
        or f"TMDB {kind} {resource_id}"
    )
    kind_label = TMDB_KIND_LABELS.get(kind, kind.title())
    detail = f"TMDB {kind_label} · id {resource_id}"
    if kind in ("company", "keyword"):
        sort_by = tmdb_client.parse_tmdb_sort_by(reference, media_type)
        detail = f"{detail} · sort {sort_by}"
    return _ok(
        "tmdb_url" if source_type == "tmdb_url" else source_type,
        kind=kind,
        title=title,
        detail=detail,
        suggested_title=title,
    )


def _validate_mdblist(reference: str, media_type: str) -> dict[str, Any]:
    user, slug = list_sources.parse_mdblist_reference(reference)
    items = list_sources.fetch_mdblist(f"{user}/{slug}", media_type, limit=5)
    # Re-fetch is cached; get a fuller count cheaply from cache of a larger limit.
    all_items = list_sources.fetch_mdblist(f"{user}/{slug}", media_type, limit=500)
    title = f"{user}/{slug}"
    media_label = "movies" if media_type == "movie" else "shows"
    detail = f"MDBList · {len(all_items)} {media_label} matched"
    # Touch items so unused warning is avoided if fetch_mdblist changes.
    _ = items
    return _ok("mdblist", kind="list", title=title, detail=detail, suggested_title=slug.replace("-", " ").title())


def _validate_trakt_list(reference: str, media_type: str) -> dict[str, Any]:
    user, slug = list_sources.parse_trakt_reference(reference)
    client_id = getattr(settings, "TRAKT_CLIENT_ID", None)
    if not client_id:
        raise list_sources.ListSourceError(
            "Trakt Client ID is not configured (Settings). Creating a Trakt API app currently requires Trakt VIP."
        )
    url = f"https://api.trakt.tv/users/{user}/lists/{slug}"
    resp = list_sources._get_with_retry(
        url,
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
        raise list_sources.ListSourceError("Trakt rejected the Client ID (check the Trakt Client ID setting)")
    if resp.status_code == 404:
        raise list_sources.ListSourceError(f"Trakt list not found (or private): {user}/{slug}")
    if resp.status_code != 200:
        raise list_sources.ListSourceError(f"Trakt returned HTTP {resp.status_code} for {user}/{slug}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise list_sources.ListSourceError(f"Trakt returned invalid JSON for {user}/{slug}") from exc
    if not isinstance(data, dict):
        raise list_sources.ListSourceError(f"Unexpected Trakt list payload for {user}/{slug}")
    name = str(data.get("name") or slug).strip() or slug
    item_count = data.get("item_count")
    detail = f"Trakt list · {user}/{slug}"
    if item_count is not None:
        detail = f"{detail} · {item_count} items"
    return _ok("trakt_list", kind="list", title=name, detail=detail, suggested_title=name)


def _validate_anilist(reference: str, media_type: str) -> dict[str, Any]:
    if media_type != "show":
        raise list_sources.ListSourceError("AniList sources are TV-only")
    user, list_name = list_sources.parse_anilist_reference(reference)
    # Probe with a tiny limit — confirms user/list exist.
    items = list_sources.fetch_anilist(reference, media_type, limit=5)
    title = f"{user}" + (f" / {list_name}" if list_name else " anime list")
    detail = f"AniList · {len(items)} titles in sample"
    return _ok(
        "anilist",
        kind="list",
        title=title,
        detail=detail,
        suggested_title=list_name or f"{user} anime",
    )


def _validate_stevenlu(reference: str, media_type: str) -> dict[str, Any]:
    if media_type != "movie":
        raise list_sources.ListSourceError("StevenLu lists are movie-only")
    text = str(reference or "").strip()
    if not text:
        # Default popular list URL.
        text = "https://s3.amazonaws.com/popular-movies/movies.json"
    items = list_sources.fetch_stevenlu(text, media_type, limit=5)
    all_items = list_sources.fetch_stevenlu(text, media_type, limit=500)
    title = "StevenLu popular movies" if "popular-movies" in text else "StevenLu JSON list"
    detail = f"StevenLu · {len(all_items)} movies"
    _ = items
    return _ok("stevenlu", kind="list", title=title, detail=detail, suggested_title=title)
