"""Plex collection helpers for the Collections rule builder.

Lists sections, resolves Plex items per target section by provider GUID
(TMDB for movies, TVDB/TMDB for shows), and creates/updates collection
membership. Targeting is per-section so recipes can point at any Plex
library (including dedicated placeholder libraries), not just the
configured movie/TV sections.

Ownership: Placeholdarr only mutates collections it owns (``placeholdarr``
label and/or ``Managed by Placeholdarr.`` summary footer), tracked by
ratingKey. Same-title collections in one library share a Plex identity, so
we never create a second copy: rename, or explicitly adopt the existing
shelf (adoption syncs membership to the recipe and may remove non-matching
items).
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Optional

from core.logger import logger
from services.media_servers.plex_lookup import (
    _cached_section_all,
    _extract_guid_numeric,
    _extract_path_numeric,
    get_plex_server,
)


class PlexCollectionsError(Exception):
    """Raised when Plex is unavailable or a collection operation fails."""


class CollectionTitleConflict(PlexCollectionsError):
    """Same-title collection exists in a target library and was not adopted."""

    def __init__(self, message: str, *, conflicts: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.conflicts = list(conflicts or [])


OWNERSHIP_LABEL = "placeholdarr"
OWNERSHIP_SUMMARY = "Managed by Placeholdarr."

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


def membership_mask(
    section_id: int,
    section_type: str,
    provider_key_groups: list[list[str]],
) -> list[bool]:
    """True for each provider-key group that exists in the Plex section."""
    plex = get_plex_server()
    if not plex:
        raise PlexCollectionsError("Plex is not configured or unreachable")
    index = _get_section_index(plex, section_id, section_type)
    present: list[bool] = []
    for group in provider_key_groups:
        present.append(any(index.get(key) is not None for key in group))
    return present


def ownership_labels(recipe_id: int, set_value: str | None = None) -> list[str]:
    """Labels applied to Placeholdarr-managed Plex collections."""
    labels = [OWNERSHIP_LABEL, f"placeholdarr-recipe-{int(recipe_id)}"]
    if set_value:
        slug = _slug_value(set_value)
        if slug:
            labels.append(f"placeholdarr-set-{int(recipe_id)}-{slug}")
    return labels


def _slug_value(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return text[:80]


def collection_label_names(collection) -> set[str]:
    names: set[str] = set()
    try:
        for label in collection.labels or []:
            tag = getattr(label, "tag", None) or getattr(label, "title", None) or str(label)
            if tag:
                names.add(str(tag).strip().lower())
    except Exception:
        pass
    return names


def collection_summary_text(collection) -> str:
    return str(getattr(collection, "summary", "") or "").strip()


def has_ownership_summary(collection) -> bool:
    return OWNERSHIP_SUMMARY.lower() in collection_summary_text(collection).lower()


def has_ownership_label(collection) -> bool:
    return OWNERSHIP_LABEL in collection_label_names(collection)


def is_owned_collection(collection) -> bool:
    """True when Placeholdarr marked this collection (label and/or summary)."""
    return has_ownership_label(collection) or has_ownership_summary(collection)


def is_owned_by_recipe(collection, recipe_id: int) -> bool:
    """True when this collection carries the recipe-specific ownership label."""
    needle = f"placeholdarr-recipe-{int(recipe_id)}"
    return needle in collection_label_names(collection)


def _collection_item_count(collection) -> int:
    try:
        count = getattr(collection, "childCount", None)
        if count is not None:
            return int(count)
    except (TypeError, ValueError):
        pass
    try:
        return len(collection.items())
    except Exception:
        return 0


def find_collections_by_title(section, collection_title: str) -> list[Any]:
    needle = collection_title.strip().lower()
    if not needle:
        return []
    matches: list[Any] = []
    for collection in section.collections():
        if str(getattr(collection, "title", "")).strip().lower() == needle:
            matches.append(collection)
    return matches


def describe_title_conflict(
    collection,
    *,
    section_id: int,
    section_title: str,
    collection_title: str,
    recipe_id: int | None,
) -> dict[str, Any]:
    owned = is_owned_collection(collection)
    owned_by_this = bool(recipe_id is not None and is_owned_by_recipe(collection, int(recipe_id)))
    reason = "ours" if owned_by_this else ("other_recipe" if owned else "unlabeled")
    return {
        "title": collection_title.strip(),
        "section_id": int(section_id),
        "section_title": section_title,
        "rating_key": str(getattr(collection, "ratingKey", "") or "") or None,
        "item_count": _collection_item_count(collection),
        "reason": reason,
    }


def find_title_conflicts(
    section_ids: list[int],
    section_type: str,
    titles: list[str],
    *,
    recipe_id: int | None = None,
    known_keys: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return same-title clashes in any selected library that would create a twin.

    Skips collections already owned by this recipe (label or stored ratingKey).
    """
    plex = get_plex_server()
    if not plex:
        raise PlexCollectionsError("Plex is not configured or unreachable")
    unique_titles = []
    seen_titles: set[str] = set()
    for raw in titles:
        title = str(raw or "").strip()
        key = title.lower()
        if not title or key in seen_titles:
            continue
        seen_titles.add(key)
        unique_titles.append(title)
    if not unique_titles or not section_ids:
        return []

    conflicts: list[dict[str, Any]] = []
    for section_id in section_ids:
        section = _get_section(plex, int(section_id))
        section_title = str(getattr(section, "title", "") or section_id)
        # Only movie/show sections of the recipe type are valid targets.
        sec_type = str(getattr(section, "type", "") or "")
        if section_type == "movie" and sec_type != "movie":
            continue
        if section_type == "show" and sec_type != "show":
            continue
        for title in unique_titles:
            known = lookup_stored_key(known_keys, int(section_id), title) if known_keys else None
            if known:
                # Already bound to a ratingKey for this recipe — not a create conflict.
                continue
            for collection in find_collections_by_title(section, title):
                row = describe_title_conflict(
                    collection,
                    section_id=int(section_id),
                    section_title=section_title,
                    collection_title=title,
                    recipe_id=recipe_id,
                )
                if row["reason"] == "ours":
                    continue
                conflicts.append(row)
    return conflicts


def desired_ownership_summary(existing_summary: str | None = None) -> str:
    """Ensure the visible Plex summary ends with our ownership footer."""
    current = str(existing_summary or "").strip()
    if not current:
        return OWNERSHIP_SUMMARY
    if OWNERSHIP_SUMMARY.lower() in current.lower():
        return current
    return f"{current}\n\n{OWNERSHIP_SUMMARY}"


def ensure_ownership_markers(collection, recipe_id: int, set_value: str | None = None) -> None:
    """Apply hidden labels (API) and a visible summary line (Plex UI)."""
    wanted = ownership_labels(recipe_id, set_value)
    existing = collection_label_names(collection)
    missing = [label for label in wanted if label.lower() not in existing]
    if missing:
        try:
            collection.addLabel(missing)
        except Exception as exc:
            logger.warning(
                f"Collections: could not apply ownership labels on "
                f"{getattr(collection, 'title', '')!r}: {exc}",
                extra={"emoji_type": "warning"},
            )

    target_summary = desired_ownership_summary(collection_summary_text(collection))
    if collection_summary_text(collection) != target_summary:
        try:
            collection.editSummary(target_summary)
        except Exception as exc:
            logger.warning(
                f"Collections: could not set ownership summary on "
                f"{getattr(collection, 'title', '')!r}: {exc}",
                extra={"emoji_type": "warning"},
            )

    try:
        collection.reload()
    except Exception:
        pass


# Back-compat alias used by older call sites / tests.
ensure_ownership_labels = ensure_ownership_markers


def lookup_stored_key(
    plex_collection_keys: Any,
    section_id: int,
    collection_title: str,
) -> str | None:
    if not isinstance(plex_collection_keys, dict):
        return None
    by_section = plex_collection_keys.get(str(section_id))
    if not isinstance(by_section, dict):
        return None
    key = by_section.get(collection_title)
    if key is None:
        # Case-insensitive title fallback.
        needle = collection_title.strip().lower()
        for title, rating_key in by_section.items():
            if str(title).strip().lower() == needle:
                key = rating_key
                break
    text = str(key or "").strip()
    return text or None


def set_stored_key(
    plex_collection_keys: dict[str, Any] | None,
    section_id: int,
    collection_title: str,
    rating_key: str | None,
) -> dict[str, Any]:
    out: dict[str, Any] = dict(plex_collection_keys or {})
    section_key = str(section_id)
    by_section = dict(out.get(section_key) or {}) if isinstance(out.get(section_key), dict) else {}
    if rating_key:
        by_section[collection_title] = str(rating_key)
    else:
        by_section.pop(collection_title, None)
    if by_section:
        out[section_key] = by_section
    else:
        out.pop(section_key, None)
    return out


def _fetch_collection_by_rating_key(plex, rating_key: str):
    try:
        item = plex.fetchItem(int(rating_key))
    except Exception:
        try:
            item = plex.fetchItem(f"/library/metadata/{rating_key}")
        except Exception:
            return None
    # Ensure it is a collection-like object.
    if item is None:
        return None
    type_name = str(getattr(item, "type", "") or "").lower()
    if type_name and type_name not in ("collection",):
        # Some plexapi versions expose Collection without type attr; allow objects with items()+title.
        if not hasattr(item, "items"):
            return None
    return item


def _find_unlabeled_by_title(section, collection_title: str):
    """First same-title collection that is not Placeholdarr-owned."""
    for collection in find_collections_by_title(section, collection_title):
        if not is_owned_collection(collection):
            return collection
    return None


def _find_owned_collection_by_title(section, collection_title: str):
    """Title match only when the collection already has our ownership marker."""
    needle = collection_title.strip().lower()
    for collection in section.collections():
        if str(getattr(collection, "title", "")).strip().lower() != needle:
            continue
        if is_owned_collection(collection):
            return collection
    return None


def resolve_owned_collection(
    plex,
    section,
    *,
    collection_title: str,
    known_rating_key: str | None = None,
):
    """Resolve a Placeholdarr-owned collection; never returns unlabeled same-title."""
    if known_rating_key:
        found = _fetch_collection_by_rating_key(plex, known_rating_key)
        if found is not None:
            return found
    try:
        return _find_owned_collection_by_title(section, collection_title)
    except Exception as exc:
        raise PlexCollectionsError(
            f"Failed to list collections for section {getattr(section, 'key', '?')}: {exc}"
        ) from exc


def _apply_custom_item_order(collection, items: list[Any]) -> None:
    """Force Plex custom sort so membership order matches Placeholdarr arrange order."""
    if not items or collection is None or getattr(collection, "smart", False):
        return
    try:
        if int(getattr(collection, "collectionSort", 0) or 0) != 2:
            collection.sortUpdate(sort="custom")
            collection.reload()
    except Exception as exc:
        logger.warning(
            f"Collections: could not set custom sort on {getattr(collection, 'title', '')!r}: {exc}",
            extra={"emoji_type": "warning"},
        )
        return
    try:
        current = collection.items()
        current_keys = [str(getattr(item, "ratingKey", "")) for item in current]
    except Exception:
        current_keys = []
    desired_keys = [str(getattr(item, "ratingKey", "")) for item in items]
    if current_keys == desired_keys:
        return
    after = None
    for item in items:
        try:
            collection.moveItem(item, after=after)
        except Exception as exc:
            logger.warning(
                f"Collections: could not move {getattr(item, 'title', item)!r} "
                f"in {getattr(collection, 'title', '')!r}: {exc}",
                extra={"emoji_type": "warning"},
            )
        after = item


def sync_collection(
    section_id: int,
    section_type: str,
    collection_title: str,
    items: list[Any],
    *,
    recipe_id: int,
    set_value: str | None = None,
    known_rating_key: str | None = None,
    adopt_unlabeled: bool = False,
) -> dict[str, Any]:
    """Create or update a Placeholdarr-owned Plex collection.

    Never creates a same-title twin in a library. Unlabeled same-title shelves
    are adopted only when ``adopt_unlabeled`` is true (membership then follows
    the recipe and non-matching items are removed).
    """
    plex = get_plex_server()
    if not plex:
        raise PlexCollectionsError("Plex is not configured or unreachable")
    section = _get_section(plex, section_id)
    section_title = str(getattr(section, "title", "") or section_id)

    existing = resolve_owned_collection(
        plex,
        section,
        collection_title=collection_title,
        known_rating_key=known_rating_key,
    )
    adopted = False

    if existing is None:
        unlabeled = _find_unlabeled_by_title(section, collection_title)
        if unlabeled is not None:
            if not adopt_unlabeled:
                conflict = describe_title_conflict(
                    unlabeled,
                    section_id=int(section_id),
                    section_title=section_title,
                    collection_title=collection_title,
                    recipe_id=recipe_id,
                )
                raise CollectionTitleConflict(
                    f"Plex already has a collection named {collection_title!r} in "
                    f"{section_title!r}. Rename this recipe or adopt the existing collection.",
                    conflicts=[conflict],
                )
            existing = unlabeled
            adopted = True
        else:
            # Another Placeholdarr recipe may already own this title.
            for other in find_collections_by_title(section, collection_title):
                if is_owned_collection(other) and not is_owned_by_recipe(other, recipe_id):
                    conflict = describe_title_conflict(
                        other,
                        section_id=int(section_id),
                        section_title=section_title,
                        collection_title=collection_title,
                        recipe_id=recipe_id,
                    )
                    raise CollectionTitleConflict(
                        f"Collection {collection_title!r} in {section_title!r} is already "
                        f"managed by another Placeholdarr recipe. Rename this recipe.",
                        conflicts=[conflict],
                    )

    target_keys = {str(getattr(item, "ratingKey", "")) for item in items}

    if existing is None:
        if not items:
            return {
                "added": 0,
                "removed": 0,
                "total": 0,
                "created": False,
                "adopted": False,
                "rating_key": None,
                "skipped_unlabeled": False,
            }
        try:
            created = section.createCollection(collection_title, items=items)
        except Exception as exc:
            raise PlexCollectionsError(
                f"Failed to create collection {collection_title!r} in section {section_id}: {exc}"
            ) from exc
        ensure_ownership_markers(created, recipe_id, set_value)
        _apply_custom_item_order(created, items)
        rating_key = str(getattr(created, "ratingKey", "") or "") or None
        logger.info(
            f"Collections: created Plex collection {collection_title!r} "
            f"(section={section_id}, items={len(items)}, recipe={recipe_id})",
            extra={"emoji_type": "info"},
        )
        return {
            "added": len(items),
            "removed": 0,
            "total": len(items),
            "created": True,
            "adopted": False,
            "rating_key": rating_key,
            "skipped_unlabeled": False,
        }

    ensure_ownership_markers(existing, recipe_id, set_value)

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
        if to_add or to_remove:
            existing.reload()
        _apply_custom_item_order(existing, items)
    except Exception as exc:
        raise PlexCollectionsError(
            f"Failed to update collection {collection_title!r} membership: {exc}"
        ) from exc

    rating_key = str(getattr(existing, "ratingKey", "") or "") or None
    logger.info(
        f"Collections: {'adopted' if adopted else 'synced'} Plex collection {collection_title!r} "
        f"(section={section_id}, added={len(to_add)}, removed={len(to_remove)}, "
        f"total={len(target_keys)}, recipe={recipe_id})",
        extra={"emoji_type": "info"},
    )
    return {
        "added": len(to_add),
        "removed": len(to_remove),
        "total": len(target_keys),
        "created": False,
        "adopted": adopted,
        "rating_key": rating_key,
        "skipped_unlabeled": False,
    }


def delete_collection(
    section_id: int,
    section_type: str,
    collection_title: str,
    *,
    recipe_id: int,
    set_value: str | None = None,
    known_rating_key: str | None = None,
) -> dict[str, Any]:
    """Delete a Placeholdarr-owned Plex collection if it exists.

    Unlabeled same-title collections are left alone.
    """
    plex = get_plex_server()
    if not plex:
        raise PlexCollectionsError("Plex is not configured or unreachable")
    section = _get_section(plex, section_id)
    existing = resolve_owned_collection(
        plex,
        section,
        collection_title=collection_title,
        known_rating_key=known_rating_key,
    )
    if existing is None:
        return {
            "deleted": False,
            "title": collection_title,
            "rating_key": None,
            "skipped_unlabeled": True,
        }
    # Ensure labels before delete is not required; we already verified ownership.
    try:
        existing.delete()
    except Exception as exc:
        raise PlexCollectionsError(
            f"Failed to delete collection {collection_title!r} in section {section_id}: {exc}"
        ) from exc
    logger.info(
        f"Collections: deleted Plex collection {collection_title!r} "
        f"(section={section_id}, recipe={recipe_id})",
        extra={"emoji_type": "info"},
    )
    return {
        "deleted": True,
        "title": collection_title,
        "rating_key": None,
        "skipped_unlabeled": False,
    }
