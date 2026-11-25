"""Plex library Recommended hub order and visibility (#201 Ordering).

Collections sync never calls these helpers. Apply is explicit from Ordering UI
or a one-shot Discover preset seed.
"""

from __future__ import annotations

import json
from typing import Any

from core.config import settings
from core.logger import logger
from services.collections import collection_sets as sets_mod
from services.collections.engine import recipe_section_ids
from services.media_servers import plex_collections
from services.media_servers.plex_lookup import get_plex_server
from services.postgres.db import session_scope
from services.postgres.models import CollectionRecipe


class HubOrderError(Exception):
    """Raised when hub list/apply fails (Plex Pass / connectivity)."""


def _hub_order_map() -> dict[str, list[str]]:
    raw = str(getattr(settings, "COLLECTIONS_DISCOVER_HUB_ORDER_JSON", "") or "{}").strip()
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, value in parsed.items():
        if isinstance(value, list):
            out[str(key)] = [str(t).strip() for t in value if str(t or "").strip()]
    return out


def save_hub_order_for_section(section_id: int, recipe_ids: list[int]) -> None:
    mapping = _hub_order_map()
    mapping[str(int(section_id))] = [str(r) for r in recipe_ids]
    settings.COLLECTIONS_DISCOVER_HUB_ORDER_JSON = json.dumps(mapping)
    try:
        from services.app_config import save_settings

        save_settings({"COLLECTIONS_DISCOVER_HUB_ORDER_JSON": settings.COLLECTIONS_DISCOVER_HUB_ORDER_JSON})
    except Exception as exc:
        logger.warning(
            f"Collections: could not persist hub order for section {section_id}: {exc}",
            extra={"emoji_type": "warning"},
        )


def collection_titles_for_recipe(recipe: CollectionRecipe, *, section_id: int) -> list[str]:
    definition = recipe.definition if isinstance(recipe.definition, dict) else {}
    if sets_mod.is_collection_set_definition(definition):
        set_cfg = definition.get("collection_set") or {}
        if isinstance(set_cfg, dict):
            managed = set_cfg.get("managed_by_section") or {}
            titles = managed.get(str(section_id)) if isinstance(managed, dict) else None
            if isinstance(titles, list) and titles:
                return [str(t) for t in titles if str(t or "").strip()]
        children = sets_mod.expand_collection_set(set_cfg, str(recipe.plex_section_type))
        return [str(c.get("title") or "").strip() for c in children if str(c.get("title") or "").strip()]
    title = str(recipe.collection_title or recipe.name or "").strip()
    return [title] if title else []


def _section_or_raise(section_id: int):
    plex = get_plex_server()
    if not plex:
        raise HubOrderError("Plex is not configured or unreachable")
    try:
        return plex, plex.library.sectionByID(int(section_id))
    except Exception as exc:
        raise HubOrderError(f"Unknown Plex section {section_id}: {exc}") from exc


def _rating_key_from_hub_identifier(identifier: str | None) -> str | None:
    text = str(identifier or "").strip()
    if not text:
        return None
    # custom.collection.{sectionId}.{ratingKey}
    parts = text.split(".")
    if len(parts) >= 4 and parts[0] == "custom" and parts[1] == "collection":
        return parts[-1] or None
    if text.isdigit():
        return text
    return None


def _placeholdarr_ownership_index(section_id: int) -> dict[str, dict[str, Any]]:
    """Map lowercased title and ratingKey → recipe ownership hints."""
    by_key: dict[str, dict[str, Any]] = {}
    with session_scope() as session:
        for row in session.query(CollectionRecipe).order_by(CollectionRecipe.id).all():
            sids = recipe_section_ids(row)
            if int(section_id) not in sids:
                continue
            keys_map = row.plex_collection_keys if isinstance(row.plex_collection_keys, dict) else {}
            for title in collection_titles_for_recipe(row, section_id=int(section_id)):
                hint = {
                    "placeholdarr_owned": True,
                    "recipe_id": int(row.id),
                    "recipe_name": str(row.name or ""),
                }
                by_key[f"title:{title.strip().lower()}"] = hint
            stored = plex_collections.lookup_stored_key(keys_map, int(section_id), str(row.collection_title or ""))
            if stored:
                by_key[f"rk:{stored}"] = {
                    "placeholdarr_owned": True,
                    "recipe_id": int(row.id),
                    "recipe_name": str(row.name or ""),
                }
            # Set recipes store keys under child titles via set_stored_key; scan values.
            for value in keys_map.values():
                if isinstance(value, dict):
                    for rk in value.values():
                        if rk:
                            by_key[f"rk:{rk}"] = {
                                "placeholdarr_owned": True,
                                "recipe_id": int(row.id),
                                "recipe_name": str(row.name or ""),
                            }
                elif value:
                    by_key[f"rk:{value}"] = {
                        "placeholdarr_owned": True,
                        "recipe_id": int(row.id),
                        "recipe_name": str(row.name or ""),
                    }
    return by_key


def _serialize_hub(hub, ownership: dict[str, dict[str, Any]]) -> dict[str, Any]:
    identifier = str(getattr(hub, "identifier", "") or "")
    title = str(getattr(hub, "title", "") or "").strip()
    rk = _rating_key_from_hub_identifier(identifier)
    hint = ownership.get(f"rk:{rk}") if rk else None
    if hint is None and title:
        hint = ownership.get(f"title:{title.lower()}")
    owned = bool(hint and hint.get("placeholdarr_owned"))
    # Fall back to label check when we have a live collection later; hubs alone use recipe map.
    return {
        "identifier": identifier,
        "title": title,
        "rating_key": rk,
        "promoted_to_recommended": bool(getattr(hub, "promotedToRecommended", False)),
        "promoted_to_home": bool(getattr(hub, "promotedToOwnHome", False)),
        "promoted_to_shared": bool(getattr(hub, "promotedToSharedHome", False)),
        "deletable": bool(getattr(hub, "deletable", True)),
        "placeholdarr_owned": owned,
        "recipe_id": hint.get("recipe_id") if hint else None,
        "recipe_name": hint.get("recipe_name") if hint else None,
    }


def list_managed_hubs(section_id: int) -> list[dict[str, Any]]:
    _plex, section = _section_or_raise(section_id)
    try:
        hubs = section.managedHubs()
    except Exception as exc:
        raise HubOrderError(
            f"Could not list managed hubs (Plex Pass required): {exc}"
        ) from exc
    ownership = _placeholdarr_ownership_index(int(section_id))
    return [_serialize_hub(hub, ownership) for hub in hubs]


def list_section_collections(section_id: int) -> list[dict[str, Any]]:
    _plex, section = _section_or_raise(section_id)
    ownership = _placeholdarr_ownership_index(int(section_id))
    managed_ids: set[str] = set()
    try:
        for hub in section.managedHubs():
            rk = _rating_key_from_hub_identifier(getattr(hub, "identifier", None))
            if rk:
                managed_ids.add(str(rk))
    except Exception:
        pass

    out: list[dict[str, Any]] = []
    try:
        collections = list(section.collections())
    except Exception as exc:
        raise HubOrderError(f"Could not list collections for section {section_id}: {exc}") from exc

    for collection in collections:
        title = str(getattr(collection, "title", "") or "").strip()
        rk = str(getattr(collection, "ratingKey", "") or "") or None
        if not title or not rk:
            continue
        hint = ownership.get(f"rk:{rk}") or ownership.get(f"title:{title.lower()}")
        owned = plex_collections.is_owned_collection(collection) or bool(
            hint and hint.get("placeholdarr_owned")
        )
        out.append(
            {
                "rating_key": rk,
                "title": title,
                "item_count": plex_collections._collection_item_count(collection),
                "on_hubs": rk in managed_ids,
                "placeholdarr_owned": owned,
                "recipe_id": hint.get("recipe_id") if hint else None,
                "recipe_name": hint.get("recipe_name") if hint else None,
            }
        )
    out.sort(key=lambda row: str(row.get("title") or "").lower())
    return out


def get_hubs_snapshot(section_id: int) -> dict[str, Any]:
    hubs = list_managed_hubs(section_id)
    collections = list_section_collections(section_id)
    promote = [c for c in collections if not c.get("on_hubs")]
    return {
        "plex_section_id": int(section_id),
        "hubs": hubs,
        "collections": collections,
        "promote_candidates": promote,
    }


def _find_hub_by_identifier(section, identifier: str):
    needle = str(identifier or "").strip()
    if not needle:
        return None
    for hub in section.managedHubs():
        if str(getattr(hub, "identifier", "") or "") == needle:
            return hub
    return None


def _collection_by_rating_key(plex, rating_key: str):
    try:
        return plex.fetchItem(int(rating_key))
    except Exception:
        try:
            return plex.fetchItem(f"/library/metadata/{rating_key}")
        except Exception:
            return None


def set_hub_visibility(
    section_id: int,
    *,
    identifier: str | None = None,
    rating_key: str | None = None,
    library: bool | None = None,
    home: bool | None = None,
    shared: bool | None = None,
) -> dict[str, Any]:
    plex, section = _section_or_raise(section_id)
    hub = _find_hub_by_identifier(section, identifier) if identifier else None
    if hub is None and rating_key:
        # Promote via collection.visibility() when not yet managed.
        collection = _collection_by_rating_key(plex, str(rating_key))
        if collection is None:
            raise HubOrderError(f"Collection ratingKey {rating_key} not found")
        try:
            hub = collection.visibility()
        except Exception as exc:
            raise HubOrderError(f"Could not load hub visibility: {exc}") from exc
    if hub is None:
        raise HubOrderError("Hub identifier or rating_key is required")
    try:
        hub.updateVisibility(
            recommended=library if library is not None else None,
            home=home if home is not None else None,
            shared=shared if shared is not None else None,
        )
        hub.reload()
    except Exception as exc:
        raise HubOrderError(f"Failed to update hub visibility: {exc}") from exc
    ownership = _placeholdarr_ownership_index(int(section_id))
    return _serialize_hub(hub, ownership)


def remove_managed_hub(section_id: int, identifier: str) -> dict[str, Any]:
    _plex, section = _section_or_raise(section_id)
    hub = _find_hub_by_identifier(section, identifier)
    if hub is None:
        raise HubOrderError(f"Managed hub not found: {identifier}")
    if not bool(getattr(hub, "deletable", True)):
        raise HubOrderError(f"Hub {getattr(hub, 'title', identifier)!r} cannot be removed from recommendations")
    try:
        hub.remove()
    except Exception as exc:
        raise HubOrderError(f"Failed to remove hub: {exc}") from exc
    return {"ok": True, "removed": identifier}


def reorder_managed_hubs(section_id: int, ordered_identifiers: list[str]) -> dict[str, Any]:
    _plex, section = _section_or_raise(section_id)
    try:
        managed = list(section.managedHubs())
    except Exception as exc:
        raise HubOrderError(f"Could not list managed hubs: {exc}") from exc
    by_id = {str(getattr(h, "identifier", "") or ""): h for h in managed}
    prev = None
    moved = 0
    missing: list[str] = []
    for identifier in ordered_identifiers:
        key = str(identifier or "").strip()
        hub = by_id.get(key)
        if hub is None:
            missing.append(key)
            continue
        try:
            if not getattr(hub, "_promoted", False):
                hub.promoteRecommended().reload()
            if prev is not None and hub.identifier != prev.identifier:
                hub.move(after=prev)
                hub.reload()
            prev = hub
            moved += 1
        except Exception as exc:
            logger.warning(
                f"Collections: hub move failed for {key!r} in section {section_id}: {exc}",
                extra={"emoji_type": "warning"},
            )
    return {
        "ok": True,
        "moved": moved,
        "missing_identifiers": missing,
        "requested": len(ordered_identifiers),
    }


def apply_hubs_draft(
    section_id: int,
    *,
    ordered_identifiers: list[str] | None = None,
    visibility: list[dict[str, Any]] | None = None,
    promote_rating_keys: list[str] | None = None,
    demote_identifiers: list[str] | None = None,
) -> dict[str, Any]:
    """Apply Ordering UI draft: promote, visibility, demote, then reorder."""
    promoted: list[str] = []
    demoted: list[str] = []
    visibility_updated: list[str] = []
    errors: list[str] = []

    for rk in promote_rating_keys or []:
        try:
            row = set_hub_visibility(
                section_id,
                rating_key=str(rk),
                library=True,
                home=False,
                shared=False,
            )
            promoted.append(str(row.get("identifier") or rk))
        except HubOrderError as exc:
            errors.append(str(exc))

    for patch in visibility or []:
        if not isinstance(patch, dict):
            continue
        try:
            row = set_hub_visibility(
                section_id,
                identifier=str(patch.get("identifier") or "") or None,
                rating_key=str(patch.get("rating_key") or "") or None,
                library=patch.get("library"),
                home=patch.get("home"),
                shared=patch.get("shared"),
            )
            visibility_updated.append(str(row.get("identifier") or ""))
        except HubOrderError as exc:
            errors.append(str(exc))

    for identifier in demote_identifiers or []:
        try:
            remove_managed_hub(section_id, str(identifier))
            demoted.append(str(identifier))
        except HubOrderError as exc:
            errors.append(str(exc))

    reorder_result: dict[str, Any] = {"ok": True, "moved": 0, "skipped": True}
    if ordered_identifiers is not None:
        try:
            reorder_result = reorder_managed_hubs(section_id, list(ordered_identifiers))
        except HubOrderError as exc:
            errors.append(str(exc))
            reorder_result = {"ok": False, "error": str(exc)}

    snapshot = get_hubs_snapshot(section_id)
    return {
        "ok": len(errors) == 0 and bool(reorder_result.get("ok", True)),
        "promoted": promoted,
        "demoted": demoted,
        "visibility_updated": [v for v in visibility_updated if v],
        "reorder": reorder_result,
        "errors": errors,
        "hubs": snapshot["hubs"],
        "promote_candidates": snapshot["promote_candidates"],
    }


def apply_hub_row_order(
    section_id: int,
    *,
    recipe_ids: list[int] | None = None,
    collection_titles: list[str] | None = None,
) -> dict[str, Any]:
    """Move managed collection hubs on a library Recommended row (Plex Pass)."""
    plex = get_plex_server()
    if not plex:
        return {"ok": False, "error": "Plex is not configured"}
    try:
        section = plex.library.sectionByID(int(section_id))
    except Exception as exc:
        return {"ok": False, "error": f"Unknown Plex section {section_id}: {exc}"}

    if collection_titles is None:
        titles: list[str] = []
        if recipe_ids:
            with session_scope() as session:
                for rid in recipe_ids:
                    row = session.query(CollectionRecipe).filter(CollectionRecipe.id == int(rid)).first()
                    if row is None:
                        continue
                    sids = recipe_section_ids(row)
                    if int(section_id) not in sids:
                        continue
                    titles.extend(collection_titles_for_recipe(row, section_id=int(section_id)))
        else:
            stored = _hub_order_map().get(str(int(section_id))) or []
            ids: list[int] = []
            for entry in stored:
                try:
                    ids.append(int(entry))
                except (TypeError, ValueError):
                    continue
            return apply_hub_row_order(section_id, recipe_ids=ids)
    else:
        titles = [str(t).strip() for t in collection_titles if str(t or "").strip()]

    if not titles:
        return {"ok": True, "moved": 0, "skipped": True}

    try:
        managed = section.managedHubs()
    except Exception as exc:
        return {"ok": False, "error": f"Could not list managed hubs: {exc}"}

    by_title = {}
    for hub in managed:
        name = str(getattr(hub, "title", "") or "").strip()
        if name:
            by_title[name] = hub

    identifiers: list[str] = []
    missing: list[str] = []
    for title in titles:
        hub = by_title.get(title)
        if hub is None:
            missing.append(title)
            continue
        identifiers.append(str(getattr(hub, "identifier", "") or ""))
    try:
        result = reorder_managed_hubs(section_id, identifiers)
    except HubOrderError as exc:
        return {"ok": False, "error": str(exc), "missing_titles": missing}
    return {
        "ok": True,
        "moved": result.get("moved", 0),
        "missing_titles": missing,
        "requested": len(titles),
    }