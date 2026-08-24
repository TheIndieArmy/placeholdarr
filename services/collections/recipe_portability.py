"""Portable Collection recipe export / import.

Export strips runtime fields (ids, last-run stats, Plex ratingKeys) so a
bundle can move between Placeholdarr installs. Import rebinds Plex library
targets and re-validates definitions before create.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional

from core.version import APP_VERSION
from services.collections.engine import (
    RecipeValidationError,
    normalize_section_ids,
    validate_active_window,
    validate_definition,
)

EXPORT_FORMAT = "placeholdarr-collections"
EXPORT_VERSION = 1


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def portable_recipe(row_payload: dict[str, Any]) -> dict[str, Any]:
    """Build an exportable recipe dict from a serialized recipe row."""
    definition = deepcopy(row_payload.get("definition") or {})
    if isinstance(definition.get("collection_set"), dict):
        set_cfg = dict(definition["collection_set"])
        set_cfg.pop("managed_by_section", None)
        definition["collection_set"] = set_cfg

    section_ids = row_payload.get("plex_section_ids") or []
    if not isinstance(section_ids, list):
        section_ids = []
    primary = row_payload.get("plex_section_id")
    if not section_ids and primary is not None:
        section_ids = [primary]

    return {
        "name": str(row_payload.get("name") or "").strip(),
        "enabled": bool(row_payload.get("enabled", True)),
        "plex_section_type": str(row_payload.get("plex_section_type") or "").strip(),
        "collection_title": str(row_payload.get("collection_title") or "").strip(),
        "definition": definition,
        "run_interval_hours": row_payload.get("run_interval_hours"),
        "active_window": row_payload.get("active_window")
        if isinstance(row_payload.get("active_window"), dict)
        else None,
        # Hints only — ignored on import except for UI messaging.
        "exported_plex_section_ids": [int(x) for x in section_ids if str(x).isdigit() or isinstance(x, int)],
    }


def build_export_bundle(recipes: list[dict[str, Any]]) -> dict[str, Any]:
    portable = [portable_recipe(r) for r in recipes]
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exported_at": _iso_now(),
        "app_version": APP_VERSION,
        "recipes": portable,
    }


def parse_import_bundle(payload: Any) -> list[dict[str, Any]]:
    """Accept a full export bundle or a bare recipe / recipe list."""
    if isinstance(payload, list):
        recipes = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("recipes"), list):
            fmt = str(payload.get("format") or "").strip()
            if fmt and fmt != EXPORT_FORMAT:
                raise RecipeValidationError(f"Unsupported collections export format: {fmt!r}")
            version = payload.get("version")
            if version is not None and int(version) != EXPORT_VERSION:
                raise RecipeValidationError(
                    f"Unsupported collections export version: {version!r} (expected {EXPORT_VERSION})"
                )
            recipes = payload["recipes"]
        elif payload.get("name") and payload.get("definition") is not None:
            recipes = [payload]
        else:
            raise RecipeValidationError("Import JSON must include a recipes array")
    else:
        raise RecipeValidationError("Import JSON must be an object or array")

    if not recipes:
        raise RecipeValidationError("Import JSON contains no recipes")
    if not all(isinstance(r, dict) for r in recipes):
        raise RecipeValidationError("Each imported recipe must be an object")
    return recipes


def prepare_import_recipe(
    raw: dict[str, Any],
    *,
    section_ids: list[int],
    section_type: str,
) -> dict[str, Any]:
    """Validate + normalize one imported recipe against chosen Plex libraries."""
    name = str(raw.get("name") or "").strip()
    if not name:
        raise RecipeValidationError("Imported recipe is missing a name")
    title = str(raw.get("collection_title") or "").strip() or name
    declared_type = str(raw.get("plex_section_type") or section_type).strip().lower()
    if declared_type not in ("movie", "show"):
        raise RecipeValidationError(f"Imported recipe {name!r} has invalid plex_section_type")
    if declared_type != section_type:
        raise RecipeValidationError(
            f"Imported recipe {name!r} is {declared_type} but target libraries are {section_type}"
        )

    definition = deepcopy(raw.get("definition") or {})
    if isinstance(definition.get("collection_set"), dict):
        set_cfg = dict(definition["collection_set"])
        set_cfg.pop("managed_by_section", None)
        definition["collection_set"] = set_cfg

    normalized = validate_definition(definition)
    window = validate_active_window(raw.get("active_window"))
    ids = normalize_section_ids(section_ids[0], section_ids)

    interval = raw.get("run_interval_hours")
    if interval is not None:
        try:
            interval = int(interval)
        except (TypeError, ValueError) as exc:
            raise RecipeValidationError(f"Invalid run_interval_hours on {name!r}") from exc
        if interval < 1 or interval > 24 * 14:
            raise RecipeValidationError(f"run_interval_hours out of range on {name!r}")

    return {
        "name": name[:200],
        "enabled": bool(raw.get("enabled", True)),
        "plex_section_id": ids[0],
        "plex_section_ids": ids,
        "plex_section_type": declared_type,
        "collection_title": title[:200],
        "definition": normalized,
        "run_interval_hours": interval,
        "active_window": window,
    }


def resolve_import_sections(
    *,
    recipe_type: str,
    available: list[dict[str, Any]],
    preferred_ids: Optional[list[int]] = None,
) -> list[int]:
    """Pick target section ids for an imported recipe.

    Prefer caller-supplied ids (must match type); otherwise first available
    section of that type.
    """
    typed = [s for s in available if str(s.get("type") or "") == recipe_type]
    if not typed:
        raise RecipeValidationError(f"No Plex {recipe_type} libraries available to import into")

    if preferred_ids:
        wanted = {int(x) for x in preferred_ids}
        matched = [int(s["id"]) for s in typed if int(s["id"]) in wanted]
        if matched:
            return matched
        raise RecipeValidationError(
            f"Selected libraries do not include a {recipe_type} section for import"
        )
    return [int(typed[0]["id"])]
