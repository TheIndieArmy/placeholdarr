"""Built-in Discover preset for Seerr-style Plex shelves (#201)."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from core.config import settings
from core.logger import logger
from services.collections import collection_sets as sets_mod
from services import tmdb_client
from services.collections.discover_defaults import (
    DISCOVER_PRESET_DEFAULT_LIMIT,
    MAJOR_MOVIE_STUDIOS,
    MAJOR_TV_NETWORKS,
    discover_genre_presets,
    discover_picker_networks,
    discover_picker_studios,
    discover_studio_presets,
)
from services.collections.engine import SORT_OPTIONS, RecipeValidationError, run_recipe, validate_definition
from services.collections.hub_order import apply_hub_row_order, save_hub_order_for_section
from services.collections.recipe_portability import prepare_import_recipe
from services.media_servers import plex_collections
from services.postgres.db import session_scope
from services.postgres.models import CollectionRecipe

_PRESET_PATH = Path(__file__).resolve().parent / "presets" / "discover.json"

VISIBLE_RECOMMENDED = {"library": True, "home": False, "shared": False}

_SHELF_ID_RE = re.compile(r"[^a-z0-9]+")

RELEASE_BASIS_SHELF_IDS = frozenset(
    {
        "movie:recently-released",
        "movie:upcoming-movies",
        "show:recently-aired",
        "show:upcoming-series",
    }
)


def preferred_movie_release_basis() -> str:
    """Map Calendar preferred movie date type to collection release_basis."""
    preferred = str(getattr(settings, "PREFERRED_MOVIE_DATE_TYPE", "inCinemas") or "inCinemas").strip()
    return {
        "inCinemas": "theater",
        "digitalRelease": "digital",
        "physicalRelease": "physical",
    }.get(preferred, "theater")


def default_release_basis_for_shelf(sid: str) -> str | None:
    if sid not in RELEASE_BASIS_SHELF_IDS:
        return None
    if sid.startswith("movie:"):
        return preferred_movie_release_basis()
    if sid == "show:recently-aired":
        return "latest_episode"
    if sid == "show:upcoming-series":
        return "premiered"
    return sets_mod.default_release_basis("show")


def shelf_id_for_recipe(raw: dict[str, Any]) -> str:
    section_type = str(raw.get("plex_section_type") or "").strip().lower()
    title = str(raw.get("collection_title") or raw.get("name") or "shelf").strip()
    slug = _SHELF_ID_RE.sub("-", title.lower()).strip("-") or "shelf"
    return f"{section_type}:{slug}"


def _load_bundle() -> dict[str, Any]:
    if not _PRESET_PATH.is_file():
        raise RecipeValidationError("Discover preset bundle is missing")
    try:
        payload = json.loads(_PRESET_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RecipeValidationError("Discover preset bundle is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RecipeValidationError("Discover preset bundle must be an object")
    recipes = payload.get("recipes")
    if not isinstance(recipes, list) or not recipes:
        raise RecipeValidationError("Discover preset bundle has no recipes")
    return payload


def _patch_set_values(raw: dict[str, Any], set_values: dict[str, list[str]] | None) -> dict[str, Any]:
    """Return a copy of the preset recipe with collection_set values overridden when provided."""
    sid = shelf_id_for_recipe(raw)
    if not set_values or sid not in set_values:
        return raw
    overrides = set_values.get(sid) or []
    definition = raw.get("definition")
    if not isinstance(definition, dict):
        return raw
    set_block = definition.get("collection_set")
    if not isinstance(set_block, dict):
        return raw
    category = str(set_block.get("category") or "").strip().lower()
    if category not in ("studio", "network", "genre"):
        return raw
    if not overrides:
        raise RecipeValidationError(f"Pick at least one value for {raw.get('name') or sid}.")
    out = copy.deepcopy(raw)
    out_def = out["definition"]
    out_set = out_def["collection_set"]
    out_set["selection_mode"] = "include"
    out_set["values"] = list(overrides)
    return out


def _shelf_kind(raw: dict[str, Any]) -> tuple[str, str | None]:
    definition = raw.get("definition") if isinstance(raw.get("definition"), dict) else {}
    if definition.get("mode") == "collection_set":
        set_block = definition.get("collection_set") if isinstance(definition.get("collection_set"), dict) else {}
        return "collection_set", str(set_block.get("category") or "").strip().lower() or None
    return "recipe", None


def _validate_apply_request(
    *,
    shelf_ids: list[str] | None,
    movie_section_id: int | None,
    show_section_id: int | None,
    bundle: dict[str, Any],
) -> set[str]:
    selected = {str(s).strip() for s in (shelf_ids or []) if str(s).strip()}
    if not selected:
        raise RecipeValidationError("Pick at least one collection to create.")
    need_movie = False
    need_show = False
    known: set[str] = set()
    for raw in bundle.get("recipes") or []:
        if not isinstance(raw, dict):
            continue
        sid = shelf_id_for_recipe(raw)
        known.add(sid)
        if sid not in selected:
            continue
        section_type = str(raw.get("plex_section_type") or "").strip().lower()
        if section_type == "movie":
            need_movie = True
        elif section_type == "show":
            need_show = True
    unknown = selected - known
    if unknown:
        raise RecipeValidationError(f"Unknown Discover presets: {', '.join(sorted(unknown))}")
    if need_movie and not movie_section_id:
        raise RecipeValidationError("Pick a movie library for the collections you selected.")
    if need_show and not show_section_id:
        raise RecipeValidationError("Pick a TV library for the collections you selected.")
    return selected


def apply_collection_layout_overlay(
    definition: dict[str, Any],
    *,
    sort: str | None = None,
    limit: int | None = None,
    release_basis: str | None = None,
) -> dict[str, Any]:
    """Override arrange-by and cap for every recipe created by Discover apply."""
    if not sort and limit is None and not release_basis:
        return definition
    out = copy.deepcopy(definition)
    if release_basis:
        out["release_basis"] = str(release_basis).strip().lower()
    if sort:
        out["sort"] = sort
        set_block = out.get("collection_set")
        if isinstance(set_block, dict):
            set_block = dict(set_block)
            set_block["sort"] = sort
            out["collection_set"] = set_block
    if limit is not None:
        out["limit"] = limit
        set_block = out.get("collection_set")
        if isinstance(set_block, dict):
            set_block = dict(set_block)
            set_block["limit"] = limit
            out["collection_set"] = set_block
    return validate_definition(out)


def _auto_add_for_section_type(auto_add: dict[str, Any] | None, section_type: str) -> dict[str, Any] | None:
    if not auto_add or not isinstance(auto_add, dict):
        return None
    if "movie" in auto_add or "show" in auto_add:
        block = auto_add.get(section_type)
        return block if isinstance(block, dict) else None
    return auto_add


def apply_auto_add_overlay(
    definition: dict[str, Any],
    auto_add: dict[str, Any] | None,
    *,
    section_type: str,
) -> dict[str, Any]:
    block = _auto_add_for_section_type(auto_add, section_type)
    if not block or not bool(block.get("enabled")):
        return definition
    out = dict(definition)
    out["auto_add"] = block
    return validate_definition(out)


def apply_discover_preset(
    *,
    movie_section_id: int | None = None,
    show_section_id: int | None = None,
    shelf_ids: list[str] | None = None,
    set_values: dict[str, list[str]] | None = None,
    auto_add: dict[str, Any] | None = None,
    apply_row_order: bool = True,
    shelf_layout: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    bundle = _load_bundle()
    selected = _validate_apply_request(
        shelf_ids=shelf_ids,
        movie_section_id=movie_section_id,
        show_section_id=show_section_id,
        bundle=bundle,
    )
    hub_order = bundle.get("hub_order") if isinstance(bundle.get("hub_order"), dict) else {}
    movie_order = hub_order.get("movie") if isinstance(hub_order.get("movie"), list) else []
    show_order = hub_order.get("show") if isinstance(hub_order.get("show"), list) else []

    created: dict[str, list[int]] = {"movie": [], "show": []}
    recipe_by_name: dict[str, int] = {}

    for raw in bundle.get("recipes") or []:
        if not isinstance(raw, dict):
            continue
        sid = shelf_id_for_recipe(raw)
        if sid not in selected:
            continue
        section_type = str(raw.get("plex_section_type") or "").strip().lower()
        if section_type not in ("movie", "show"):
            continue
        patched_raw = _patch_set_values(raw, set_values)
        section_id = int(movie_section_id) if section_type == "movie" else int(show_section_id)
        section_ids = [section_id]
        prepared = prepare_import_recipe(patched_raw, section_ids=section_ids, section_type=section_type)
        definition = apply_auto_add_overlay(prepared["definition"], auto_add, section_type=section_type)
        layout_entry = (shelf_layout or {}).get(sid) if shelf_layout else None
        layout_sort: str | None = None
        layout_limit: int | None = None
        layout_release_basis: str | None = None
        if isinstance(layout_entry, dict):
            raw_sort = layout_entry.get("sort")
            if raw_sort is not None and str(raw_sort).strip():
                layout_sort = str(raw_sort).strip()
            raw_limit = layout_entry.get("limit")
            if raw_limit is not None:
                try:
                    layout_limit = int(raw_limit)
                except (TypeError, ValueError):
                    layout_limit = None
            raw_basis = layout_entry.get("release_basis")
            if raw_basis is not None and str(raw_basis).strip():
                layout_release_basis = str(raw_basis).strip().lower()
        if layout_sort and layout_sort not in SORT_OPTIONS:
            raise RecipeValidationError(f"Unsupported arrange option for {prepared.get('name')}: {layout_sort}")
        definition = apply_collection_layout_overlay(
            definition,
            sort=layout_sort,
            limit=layout_limit,
            release_basis=layout_release_basis if sid in RELEASE_BASIS_SHELF_IDS else None,
        )
        with session_scope() as session:
            row = CollectionRecipe(
                name=prepared["name"],
                enabled=prepared["enabled"],
                plex_section_id=prepared["plex_section_id"],
                plex_section_ids=prepared["plex_section_ids"],
                plex_section_type=prepared["plex_section_type"],
                collection_title=prepared["collection_title"],
                definition=definition,
                run_interval_hours=prepared.get("run_interval_hours"),
                run_schedule=prepared.get("run_schedule"),
                active_window=prepared.get("active_window"),
            )
            session.add(row)
            session.flush()
            rid = int(row.id)
            session.commit()
        created[section_type].append(rid)
        recipe_by_name[str(prepared["name"])] = rid
        try:
            run_recipe(rid)
        except Exception as exc:
            logger.warning(
                f"Discover preset initial run failed for recipe {prepared['name']!r}: {exc}",
                extra={"emoji_type": "warning"},
            )

    hub_results: dict[str, Any] = {}
    if apply_row_order:
        for section_type, section_id in (
            ("movie", movie_section_id),
            ("show", show_section_id),
        ):
            if not section_id or not created.get(section_type):
                continue
            names = movie_order if section_type == "movie" else show_order
            ids = [recipe_by_name[str(n)] for n in names if str(n) in recipe_by_name]
            if not ids:
                ids = created.get(section_type) or []
            save_hub_order_for_section(int(section_id), ids)
            try:
                hub_results[str(section_id)] = apply_hub_row_order(int(section_id), recipe_ids=ids)
            except Exception as exc:
                hub_results[str(section_id)] = {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "recipe_ids": created,
        "hub": hub_results,
    }


def preset_preview() -> dict[str, Any]:
    bundle = _load_bundle()
    shelves: list[dict[str, Any]] = []
    recipes: list[dict[str, Any]] = []
    for raw in bundle.get("recipes") or []:
        if not isinstance(raw, dict):
            continue
        sid = shelf_id_for_recipe(raw)
        kind, set_category = _shelf_kind(raw)
        selectable_values: list[str] | None = None
        if set_category == "studio":
            selectable_values = discover_picker_studios()
        elif set_category == "network":
            selectable_values = discover_picker_networks()
        definition = raw.get("definition") if isinstance(raw.get("definition"), dict) else {}
        set_block = definition.get("collection_set") if isinstance(definition.get("collection_set"), dict) else {}
        default_sort = definition.get("sort") or set_block.get("sort")
        default_limit = definition.get("limit") or set_block.get("limit")
        default_release_basis = default_release_basis_for_shelf(sid)
        shelf = {
            "shelf_id": sid,
            "name": raw.get("name"),
            "description": raw.get("description"),
            "plex_section_type": raw.get("plex_section_type"),
            "collection_title": raw.get("collection_title"),
            "kind": kind,
            "set_category": set_category,
            "release_basis_configurable": sid in RELEASE_BASIS_SHELF_IDS,
            "default_release_basis": default_release_basis,
            "selectable_values": selectable_values,
            "multi_value_hint":
                "One Plex collection per selected studio."
                if set_category == "studio"
                else (
                    "One Plex collection per selected network."
                    if set_category == "network"
                    else None
                ),
            "default_sort": default_sort,
            "default_limit": default_limit,
        }
        shelves.append(shelf)
        recipes.append(
            {
                "name": raw.get("name"),
                "plex_section_type": raw.get("plex_section_type"),
                "collection_title": raw.get("collection_title"),
            }
        )
    tmdb_genres: dict[str, list[str]] = {"movie": [], "show": []}
    if tmdb_client.tmdb_configured():
        try:
            tmdb_genres["movie"] = [str(g.get("name") or "").strip() for g in tmdb_client.fetch_genres("movie") if g.get("name")]
            tmdb_genres["show"] = [
                str(g.get("name") or "").strip() for g in tmdb_client.fetch_genres("tv") if g.get("name")
            ]
        except tmdb_client.TmdbError:
            pass
    return {
        "shelves": shelves,
        "recipes": recipes,
        "hub_order": bundle.get("hub_order") or {},
        "default_limit": DISCOVER_PRESET_DEFAULT_LIMIT,
        "tmdb_genres": tmdb_genres,
        "tmdb_configured": bool(tmdb_client.tmdb_configured()),
        "genre_presets": {
            "movie": discover_genre_presets("movie"),
            "show": discover_genre_presets("show"),
        },
        "studio_presets": discover_studio_presets(),
        "default_genre_preset": "popular",
        "picker_options": {
            "studios": discover_picker_studios(),
            "networks": discover_picker_networks(),
        },
        "default_studio_values": list(MAJOR_MOVIE_STUDIOS),
        "default_network_values": list(MAJOR_TV_NETWORKS),
        "default_movie_release_basis": preferred_movie_release_basis(),
    }


def seed_studio_network_defaults(category: str) -> list[str]:
    if category == "studio":
        return list(MAJOR_MOVIE_STUDIOS)
    if category == "network":
        return list(MAJOR_TV_NETWORKS)
    return []


def list_target_sections() -> list[dict[str, Any]]:
    return plex_collections.list_plex_sections()


def _find_preset_recipe(shelf_id: str, bundle: dict[str, Any] | None = None) -> dict[str, Any]:
    bundle = bundle or _load_bundle()
    for raw in bundle.get("recipes") or []:
        if not isinstance(raw, dict):
            continue
        if shelf_id_for_recipe(raw) == shelf_id:
            return raw
    raise RecipeValidationError(f"Unknown Discover preset: {shelf_id!r}")


def prepare_discover_shelf_definition(
    shelf_id: str,
    section_id: int,
    *,
    set_values: dict[str, list[str]] | None = None,
    shelf_layout: dict[str, Any] | None = None,
    auto_add: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build the same validated definition apply would persist for one shelf (no DB write)."""
    raw = _find_preset_recipe(shelf_id)
    section_type = str(raw.get("plex_section_type") or "").strip().lower()
    if section_type not in ("movie", "show"):
        raise RecipeValidationError(f"Invalid section type for {shelf_id!r}")
    patched_raw = _patch_set_values(raw, set_values)
    section_ids = [int(section_id)]
    prepared = prepare_import_recipe(patched_raw, section_ids=section_ids, section_type=section_type)
    definition = apply_auto_add_overlay(prepared["definition"], auto_add, section_type=section_type)
    layout_entry = shelf_layout if isinstance(shelf_layout, dict) else None
    layout_sort: str | None = None
    layout_limit: int | None = None
    layout_release_basis: str | None = None
    if layout_entry:
        raw_sort = layout_entry.get("sort")
        if raw_sort is not None and str(raw_sort).strip():
            layout_sort = str(raw_sort).strip()
        raw_limit = layout_entry.get("limit")
        if raw_limit is not None:
            try:
                layout_limit = int(raw_limit)
            except (TypeError, ValueError):
                layout_limit = None
        raw_basis = layout_entry.get("release_basis")
        if raw_basis is not None and str(raw_basis).strip():
            layout_release_basis = str(raw_basis).strip().lower()
    if layout_sort and layout_sort not in SORT_OPTIONS:
        raise RecipeValidationError(f"Unsupported arrange option: {layout_sort!r}")
    definition = apply_collection_layout_overlay(
        definition,
        sort=layout_sort,
        limit=layout_limit,
        release_basis=layout_release_basis if shelf_id in RELEASE_BASIS_SHELF_IDS else None,
    )
    return section_type, definition


def shelf_preview(
    shelf_id: str,
    section_id: int,
    *,
    set_values: dict[str, list[str]] | None = None,
    shelf_layout: dict[str, Any] | None = None,
    auto_add: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from services.collections.engine import preview_definition

    section_type, definition = prepare_discover_shelf_definition(
        shelf_id,
        section_id,
        set_values=set_values,
        shelf_layout=shelf_layout,
        auto_add=auto_add,
    )
    return preview_definition(
        definition,
        section_id,
        section_type,
        extra_section_ids=[section_id],
    )