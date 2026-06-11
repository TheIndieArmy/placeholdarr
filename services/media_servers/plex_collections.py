"""Plex collection helpers for the Collections rule builder.

Lists sections, resolves Plex items per target section by provider GUID
(TMDB for movies, TVDB/TMDB for shows), and creates/updates collection
membership. Targeting is per-section so recipes can point at any Plex
library (including dedicated placeholder libraries), not just the
configured movie/TV sections.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from core.logger import logger
from services.media_servers.plex_lookup import (
    _cached_section_all,
    _extract_guid_numeric,
    _extract_path_numeric,
    get_plex_server,
)


class PlexCollectionsError(Exception):
    """Raised when Plex is unavailable or a collection operation fails."""


# Provider-id -> item index per section so one recipe run does a single section listing.
_index_lock = threading.Lock()
_section_index_cache: dict[int, tuple[float, dict[str, dict[str, Any]]]] = {}
_INDEX_TTL_SECONDS = 300.0


def list_plex_sections() -> list[dict[str, Any]]:
    """Return all Plex library sections usable as collection targets."""
    plex = get_plex_server()
    if not plex:
        raise PlexCollectionsError("Plex is not configured or unreachable")
    sections = []
    for section in plex.library.sections():
        section_type = str(getattr(section, "type", "") or "")
        if section_type not in ("movie", "show"):
            continue
        try:
            count = int(getattr(section, "totalSize", 0) or 0)
        except Exception:
            count = 0
        sections.append(
            {
                "id": int(section.key),
                "title": str(section.title),
                "type": section_type,
                "item_count": count,
            }
        )
    return sections


def _get_section(plex, section_id: int):
    try:
        return plex.library.sectionByID(int(section_id))
    except Exception as exc:
        raise PlexCollectionsError(f"Plex section {section_id} not found: {exc}") from exc


def _build_section_index(section, section_type: str) -> dict[str, dict[str, Any]]:
    """Map provider keys ('tmdb:123' / 'tvdb:456') to Plex items for one section."""
    kind = "movie" if section_type == "movie" else "TV show"
    items = _cached_section_all(section, kind)
    index: dict[str, dict[str, Any]] = {}
    providers = ("tmdb",) if section_type == "movie" else ("tvdb", "tmdb")
    for item in items:
        for provider in providers:
            pid = _extract_guid_numeric(item, provider) or _extract_path_numeric(item, provider)
            if pid:
                index.setdefault(f"{provider}:{pid}", item)
    return index


def _get_section_index(plex, section_id: int, section_type: str) -> dict[str, dict[str, Any]]:
    now = time.monotonic()
    with _index_lock:
        hit = _section_index_cache.get(int(section_id))
        if hit and (now - hit[0]) <= _INDEX_TTL_SECONDS:
            return hit[1]
    section = _get_section(plex, section_id)
    index = _build_section_index(section, section_type)
    with _index_lock:
        _section_index_cache[int(section_id)] = (now, index)
    return index


def clear_section_index_cache() -> None:
    with _index_lock:
        _section_index_cache.clear()


def resolve_items_in_section(
    section_id: int,
    section_type: str,
    provider_key_groups: list[list[str]],
) -> tuple[list[Any], list[list[str]]]:
    """Resolve provider keys to Plex items in a section.

    Each group is a list of alternative keys for one logical item
    (e.g. ['tvdb:456', 'tmdb:123']); the first key found wins.
    Returns (resolved Plex items in input order, unresolved key groups).
    """
    plex = get_plex_server()
    if not plex:
        raise PlexCollectionsError("Plex is not configured or unreachable")
    index = _get_section_index(plex, section_id, section_type)
    resolved: list[Any] = []
    missing: list[list[str]] = []
    seen_keys: set[str] = set()
    for group in provider_key_groups:
        item = None
        for key in group:
            item = index.get(key)
            if item is not None:
                break
        if item is None:
            missing.append(group)
            continue
        rating_key = str(getattr(item, "ratingKey", ""))
        if rating_key in seen_keys:
            continue
        seen_keys.add(rating_key)
        resolved.append(item)
    return resolved, missing


def sync_collection(
    section_id: int,
    section_type: str,
    collection_title: str,
    items: list[Any],
) -> dict[str, Any]:
    """Create or update a Plex collection so its membership exactly matches `items`.

    Returns counts: {"added": n, "removed": n, "total": n, "created": bool}.
    """
    plex = get_plex_server()
    if not plex:
        raise PlexCollectionsError("Plex is not configured or unreachable")
    section = _get_section(plex, section_id)

    existing = None
    try:
        for collection in section.collections():
            if str(getattr(collection, "title", "")).strip().lower() == collection_title.strip().lower():
                existing = collection
                break
    except Exception as exc:
        raise PlexCollectionsError(f"Failed to list collections for section {section_id}: {exc}") from exc

    target_keys = {str(getattr(item, "ratingKey", "")) for item in items}

    if existing is None:
        if not items:
            return {"added": 0, "removed": 0, "total": 0, "created": False}
        try:
            section.createCollection(collection_title, items=items)
        except Exception as exc:
            raise PlexCollectionsError(
                f"Failed to create collection {collection_title!r} in section {section_id}: {exc}"
            ) from exc
        logger.info(
            f"Collections: created Plex collection {collection_title!r} "
            f"(section={section_id}, items={len(items)})",
            extra={"emoji_type": "info"},
        )
        return {"added": len(items), "removed": 0, "total": len(items), "created": True}

    try:
        current_items = existing.items()
    except Exception as exc:
        raise PlexCollectionsError(
            f"Failed to read collection {collection_title!r} membership: {exc}"
        ) from exc

    current_keys = {str(getattr(item, "ratingKey", "")) for item in current_items}
    to_add = [item for item in items if str(getattr(item, "ratingKey", "")) not in current_keys]
    to_remove = [item for item in current_items if str(getattr(item, "ratingKey", "")) not in target_keys]

    try:
        if to_add:
            existing.addItems(to_add)
        if to_remove:
            existing.removeItems(to_remove)
    except Exception as exc:
        raise PlexCollectionsError(
            f"Failed to update collection {collection_title!r} membership: {exc}"
        ) from exc

    logger.info(
        f"Collections: synced Plex collection {collection_title!r} "
        f"(section={section_id}, added={len(to_add)}, removed={len(to_remove)}, total={len(target_keys)})",
        extra={"emoji_type": "info"},
    )
    return {
        "added": len(to_add),
        "removed": len(to_remove),
        "total": len(target_keys),
        "created": False,
    }
