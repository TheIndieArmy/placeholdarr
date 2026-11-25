"""Collection Sets: one config → N catalog-driven Plex collections.

Categories (v1):
- genre, decade, content_rating — catalog metadata shelves
- studio (movies), network (shows) — one shelf per studio or network
- tag — one collection per Radarr/Sonarr tag (browsing buckets)
- release_timing — Upcoming / released this week|month|year|decade (shared release-date basis)
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional

from sqlalchemy import text

from services import list_sources
from services.postgres.db import session_scope

SET_CATEGORIES = (
    "genre",
    "decade",
    "content_rating",
    "studio",
    "network",
    "tag",
    "release_timing",
)
# Back-compat alias for older imports / call sites.
SET_DIMENSIONS = SET_CATEGORIES
SET_SELECTION_MODES = ("all", "include", "exclude")

DEFAULT_TITLE_PATTERNS = {
    "genre": "Genre · {value}",
    "decade": "Decade · {value}",
    "content_rating": "Rated · {value}",
    "studio": "Studio · {value}",
    "network": "Network · {value}",
    "tag": "Tag · {value}",
    "release_timing": "{value}",
}

CATEGORY_LABELS = {
    "genre": "Genre",
    "decade": "Decade",
    "content_rating": "Content rating",
    "studio": "Studio",
    "network": "Network",
    "tag": "Tag",
    "release_timing": "Release timing",
}
DIMENSION_LABELS = CATEGORY_LABELS

# Preset time windows (not discovered from the catalog).
RELEASE_TIMING_PRESETS = (
    ("upcoming", "Upcoming"),
    ("this_week", "Released this week"),
    ("this_month", "Released this month"),
    ("this_year", "Released this year"),
    ("this_decade", "Released this decade"),
)

MOVIE_RELEASE_BASES = ("theater", "digital", "physical")
SHOW_RELEASE_BASES = ("premiered", "latest_episode", "latest_season")
RELEASE_TIMING_DAYS = {
    "this_week": 7,
    "this_month": 30,
    "this_year": 365,
    "this_decade": 3650,
}


class CollectionSetValidationError(ValueError):
    pass


def _read_category(raw: dict[str, Any]) -> str:
    """Canonical field is category; accept legacy dimension / facet keys."""
    return str(raw.get("category") or raw.get("dimension") or raw.get("facet") or "").strip().lower()


def is_collection_set_definition(definition: Any) -> bool:
    if not isinstance(definition, dict):
        return False
    mode = str(definition.get("mode") or "").strip().lower()
    if mode in ("collection_set", "set"):
        return True
    block = definition.get("collection_set")
    return isinstance(block, dict) and bool(_read_category(block))


def default_title_pattern(category: str) -> str:
    return DEFAULT_TITLE_PATTERNS.get(category, "{value}")


def format_collection_title(pattern: str, *, category: str, value: str, dimension: str | None = None) -> str:
    cat = category or (dimension or "")
    cat_label = CATEGORY_LABELS.get(cat, cat)
    text_pat = (pattern or default_title_pattern(cat)).strip() or default_title_pattern(cat)
    return (
        text_pat.replace("{value}", value)
        .replace("{category}", cat_label)
        .replace("{dimension}", cat_label)  # legacy title-pattern token
        .strip()
    )


def decade_label(year: int) -> str:
    start = (int(year) // 10) * 10
    return f"{start}s"


def decade_bounds(label: str) -> tuple[int, int]:
    """Parse '1990s' → (1990, 1999)."""
    raw = str(label or "").strip().lower().rstrip("s")
    try:
        start = int(raw)
    except ValueError as exc:
        raise CollectionSetValidationError(f"invalid decade value: {label!r}") from exc
    if start < 1800 or start > 2100:
        raise CollectionSetValidationError(f"decade out of range: {label!r}")
    return start, start + 9


def default_release_basis(section_type: str | None) -> str:
    return "premiered" if section_type == "show" else "theater"


def allowed_release_bases(section_type: str | None) -> tuple[str, ...]:
    return SHOW_RELEASE_BASES if section_type == "show" else MOVIE_RELEASE_BASES


def validate_collection_set_block(raw: Any, *, section_type: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CollectionSetValidationError("collection_set config must be an object")

    category = _read_category(raw)
    if category == "certification":
        category = "content_rating"
    if category == "release_lane":
        category = "release_timing"
    if category not in SET_CATEGORIES:
        raise CollectionSetValidationError(
            f"collection_set category must be one of {', '.join(SET_CATEGORIES)}"
        )
    if section_type == "movie" and category == "network":
        raise CollectionSetValidationError("network category applies to TV libraries only")
    if section_type == "show" and category == "studio":
        raise CollectionSetValidationError("studio category applies to movie libraries only")

    selection_mode = str(raw.get("selection_mode") or "all").strip().lower()
    if selection_mode not in SET_SELECTION_MODES:
        raise CollectionSetValidationError(
            f"collection_set selection_mode must be one of {', '.join(SET_SELECTION_MODES)}"
        )

    values_raw = raw.get("values") or []
    if not isinstance(values_raw, list):
        raise CollectionSetValidationError("collection_set values must be a list")
    values: list[str] = []
    seen: set[str] = set()
    preset_ids = {key for key, _ in RELEASE_TIMING_PRESETS}
    preset_labels = {label.lower(): key for key, label in RELEASE_TIMING_PRESETS}
    for entry in values_raw:
        label = str(entry or "").strip()
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        if category == "decade":
            label = decade_label(decade_bounds(label)[0])
        elif category == "release_timing":
            if key in preset_ids:
                label = key
            elif key in preset_labels:
                label = preset_labels[key]
            else:
                raise CollectionSetValidationError(f"unknown release timing preset: {entry!r}")
        values.append(label)
    if selection_mode == "include" and not values:
        raise CollectionSetValidationError("include mode requires at least one value")
    if selection_mode == "exclude" and not values:
        raise CollectionSetValidationError("exclude mode requires at least one value to skip")

    title_pattern = str(raw.get("title_pattern") or default_title_pattern(category)).strip()
    if "{value}" not in title_pattern:
        raise CollectionSetValidationError("title_pattern must include {value}")

    sort = raw.get("sort")
    if sort is not None and str(sort).strip():
        sort = str(sort).strip()
    else:
        sort = "title"

    include_library = raw.get("include_library")
    if include_library is None:
        include_library = True
    else:
        include_library = bool(include_library)

    limit = raw.get("limit")
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise CollectionSetValidationError("collection_set limit must be an integer") from exc
        if include_library and limit < 1:
            raise CollectionSetValidationError("collection_set limit must be >= 1 when library titles are enabled")

    min_items = raw.get("min_items", 1)
    try:
        min_items = int(min_items)
    except (TypeError, ValueError) as exc:
        raise CollectionSetValidationError("min_items must be an integer") from exc
    if min_items < 0:
        raise CollectionSetValidationError("min_items must be >= 0")

    instance_key = str(raw.get("instance_key") or "").strip()
    if category == "tag" and not instance_key:
        raise CollectionSetValidationError("tag category requires an instance_key")

    release_basis = str(raw.get("release_basis") or "").strip().lower() or None
    if category == "release_timing":
        allowed = allowed_release_bases(section_type)
        if not release_basis:
            release_basis = default_release_basis(section_type)
        if release_basis not in allowed:
            raise CollectionSetValidationError(
                f"release_basis must be one of {', '.join(allowed)} for this library type"
            )
    else:
        release_basis = None

    managed = raw.get("managed_by_section")
    managed_by_section: dict[str, list[str]] = {}
    if isinstance(managed, dict):
        for sid, titles in managed.items():
            if not isinstance(titles, list):
                continue
            cleaned = [str(t).strip() for t in titles if str(t or "").strip()]
            if cleaned:
                managed_by_section[str(sid)] = cleaned

    return {
        "category": category,
        "selection_mode": selection_mode,
        "values": values,
        "title_pattern": title_pattern,
        "sort": sort,
        "include_library": include_library,
        "limit": limit,
        "min_items": min_items,
        "instance_key": instance_key or None,
        "release_basis": release_basis,
        "managed_by_section": managed_by_section,
    }


def discover_category_values(
    section_type: str,
    category: str,
    *,
    instance_key: str | None = None,
) -> list[dict[str, str]]:
    """Return [{id, label}] for picker / expand. id is what we store in values."""
    if section_type not in ("movie", "show"):
        raise CollectionSetValidationError(f"unknown section type: {section_type!r}")

    if category == "genre":
        if section_type == "movie":
            sql = text(
                "SELECT DISTINCT genre "
                "FROM movie, jsonb_array_elements_text(radarr_genres::jsonb) AS genre "
                "WHERE is_deleted = false AND radarr_genres IS NOT NULL "
                "ORDER BY genre"
            )
        else:
            sql = text(
                "SELECT DISTINCT genre "
                "FROM series, jsonb_array_elements_text(sonarr_genres::jsonb) AS genre "
                "WHERE is_deleted = false AND sonarr_genres IS NOT NULL "
                "ORDER BY genre"
            )
        with session_scope() as session:
            catalog = [str(row[0]) for row in session.execute(sql) if row[0]]
        from services.collections.discover_defaults import merge_picker_values, picker_genre_names

        merged = merge_picker_values(catalog, picker_genre_names(section_type))
        return [{"id": name, "label": name} for name in merged]

    if category == "content_rating":
        if section_type == "movie":
            sql = text(
                "SELECT DISTINCT radarr_certification AS cert "
                "FROM movie WHERE is_deleted = false "
                "AND radarr_certification IS NOT NULL AND TRIM(radarr_certification) <> '' "
                "ORDER BY cert"
            )
        else:
            sql = text(
                "SELECT DISTINCT sonarr_certification AS cert "
                "FROM series WHERE is_deleted = false "
                "AND sonarr_certification IS NOT NULL AND TRIM(sonarr_certification) <> '' "
                "ORDER BY cert"
            )
        with session_scope() as session:
            out = []
            for row in session.execute(sql):
                if not row[0] or not str(row[0]).strip():
                    continue
                label = str(row[0]).strip()
                out.append({"id": label, "label": label})
            return out

    if category == "decade":
        if section_type == "movie":
            sql = text(
                "SELECT DISTINCT year FROM movie "
                "WHERE is_deleted = false AND year IS NOT NULL AND year >= 1800 AND year <= 2100 "
                "ORDER BY year"
            )
        else:
            sql = text(
                "SELECT DISTINCT year FROM series "
                "WHERE is_deleted = false AND year IS NOT NULL AND year >= 1800 AND year <= 2100 "
                "ORDER BY year"
            )
        with session_scope() as session:
            years = [int(row[0]) for row in session.execute(sql) if row[0] is not None]
        labels = sorted({decade_label(y) for y in years}, key=lambda s: int(s.rstrip("s")))
        return [{"id": lab, "label": lab} for lab in labels]

    if category == "tag":
        if not instance_key:
            raise CollectionSetValidationError("tag category requires an instance_key")
        arr_type = "radarr" if section_type == "movie" else "sonarr"
        try:
            tags = list_sources.fetch_arr_tags(instance_key, arr_type)
        except list_sources.ListSourceError as exc:
            raise CollectionSetValidationError(str(exc)) from exc
        return [{"id": str(t["id"]), "label": str(t["label"])} for t in tags]

    if category == "studio":
        sql = text(
            "SELECT DISTINCT radarr_studio AS val "
            "FROM movie WHERE is_deleted = false "
            "AND radarr_studio IS NOT NULL AND TRIM(radarr_studio) <> '' "
            "ORDER BY val"
        )
        with session_scope() as session:
            catalog = [str(row[0]).strip() for row in session.execute(sql) if row[0] and str(row[0]).strip()]
        from services.collections.discover_defaults import discover_picker_studios, merge_picker_values

        merged = merge_picker_values(catalog, discover_picker_studios())
        return [{"id": name, "label": name} for name in merged]

    if category == "network":
        sql = text(
            "SELECT DISTINCT sonarr_network AS val "
            "FROM series WHERE is_deleted = false "
            "AND sonarr_network IS NOT NULL AND TRIM(sonarr_network) <> '' "
            "ORDER BY val"
        )
        with session_scope() as session:
            catalog = [str(row[0]).strip() for row in session.execute(sql) if row[0] and str(row[0]).strip()]
        from services.collections.discover_defaults import discover_picker_networks, merge_picker_values

        merged = merge_picker_values(catalog, discover_picker_networks())
        return [{"id": name, "label": name} for name in merged]

    if category == "release_timing":
        return [{"id": key, "label": label} for key, label in RELEASE_TIMING_PRESETS]

    raise CollectionSetValidationError(f"unknown collection_set category: {category!r}")


# Back-compat alias.
discover_dimension_values = discover_category_values


def select_active_values(
    discovered: list[dict[str, str]],
    *,
    selection_mode: str,
    values: list[str],
) -> list[dict[str, str]]:
    by_id = {d["id"].lower(): d for d in discovered}
    by_label = {d["label"].lower(): d for d in discovered}
    selected_keys = {v.lower() for v in values}

    if selection_mode == "all":
        return list(discovered)

    if selection_mode == "include":
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for key in selected_keys:
            hit = by_id.get(key) or by_label.get(key)
            if hit and hit["id"].lower() not in seen:
                out.append(hit)
                seen.add(hit["id"].lower())
            elif key not in seen:
                raw = next((v for v in values if v.lower() == key), key)
                out.append({"id": raw, "label": raw})
                seen.add(key)
        return sorted(out, key=lambda d: d["label"].lower())

    return [
        d
        for d in discovered
        if d["id"].lower() not in selected_keys and d["label"].lower() not in selected_keys
    ]


def _release_timing_filter(value_id: str, release_basis: str) -> dict[str, Any]:
    if value_id == "upcoming":
        return {"field": "release_window", "op": "upcoming", "basis": release_basis}
    days = RELEASE_TIMING_DAYS.get(value_id)
    if days is None:
        raise CollectionSetValidationError(f"unknown release timing preset: {value_id!r}")
    return {"field": "release_window", "op": "within_past", "value": days, "basis": release_basis}


def _tmdb_sort_for_set_sort(sort: str, section_type: str) -> str:
    key = str(sort or "title").strip().lower()
    if key in ("release_date", "recent"):
        return "primary_release_date.desc" if section_type == "movie" else "first_air_date.desc"
    if key == "rating":
        return "vote_average.desc"
    if key == "title":
        return "original_title.asc" if section_type == "movie" else "name.asc"
    return "popularity.desc"


def _tmdb_source_limit(limit: int | None, missing_limit: int = 0) -> int:
    library = int(limit or 20)
    extra = max(0, int(missing_limit or 0))
    return max(40, min((library + extra) * 2, 200))


def _tmdb_fetch_sort(sort: str, section_type: str, category: str) -> str:
    """TMDB pool order (Arrange only sorts the Plex shelf)."""
    if category == "release_timing":
        return _tmdb_sort_for_set_sort(sort, section_type)
    return "popularity.desc"


def _tmdb_child_definition(
    sources: list[dict[str, Any]],
    *,
    limit: int | None,
    sort: str,
    release_basis: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "sources": sources,
        "filters": [],
        "limit": limit,
        "sort": sort or "title",
        "sort_provider": None,
        "pins": {"include": [], "exclude": []},
    }
    if release_basis:
        out["release_basis"] = release_basis
    return out


def _pull_new_tmdb_definition(
    category: str,
    value_id: str,
    value_label: str,
    *,
    section_type: str,
    limit: int | None,
    sort: str,
    release_basis: str | None,
    missing_limit: int = 0,
) -> dict[str, Any] | None:
    """Mirror hand-built recipes: TMDB source + set sort/limit (auto-add fills Missing from ARR)."""
    from services.collections.discover_defaults import tmdb_genre_id_for_label
    from services.collections.tmdb_ids import resolve_company_id, resolve_network_id

    sort_by = _tmdb_fetch_sort(sort, section_type, category)
    src_limit = _tmdb_source_limit(limit, missing_limit)

    if category == "genre":
        genre_id = tmdb_genre_id_for_label(section_type, value_label)
        if genre_id is None:
            return None
        return _tmdb_child_definition(
            [
                {
                    "type": "tmdb_discover",
                    "genre_ids": [genre_id],
                    "limit": src_limit,
                    "sort_by": sort_by,
                }
            ],
            limit=limit,
            sort=sort,
        )

    if category == "studio":
        company_id = resolve_company_id(value_label)
        if company_id is None:
            return None
        return _tmdb_child_definition(
            [
                {
                    "type": "tmdb_company",
                    "tmdb_ref": str(company_id),
                    "limit": src_limit,
                    "sort_by": sort_by,
                }
            ],
            limit=limit,
            sort=sort,
        )

    if category == "network":
        if section_type == "movie":
            return None
        network_id = resolve_network_id(value_label)
        if network_id is None:
            return None
        return _tmdb_child_definition(
            [
                {
                    "type": "tmdb_discover",
                    "network_ids": [network_id],
                    "limit": src_limit,
                    "sort_by": sort_by,
                }
            ],
            limit=limit,
            sort=sort,
        )

    if category == "decade":
        start, end = decade_bounds(value_id)
        return _tmdb_child_definition(
            [
                {
                    "type": "tmdb_discover",
                    "year_from": start,
                    "year_to": end,
                    "limit": src_limit,
                    "sort_by": sort_by,
                }
            ],
            limit=limit,
            sort=sort,
        )

    if category == "content_rating":
        cert = str(value_label or "").strip()
        if not cert:
            return None
        if section_type != "movie":
            return None
        return _tmdb_child_definition(
            [
                {
                    "type": "tmdb_discover",
                    "certification": cert,
                    "certification_country": "US",
                    "limit": src_limit,
                    "sort_by": sort_by,
                }
            ],
            limit=limit,
            sort=sort,
        )

    if category == "release_timing":
        basis = release_basis or default_release_basis(section_type)
        if value_id == "upcoming":
            return _tmdb_child_definition(
                [{"type": "tmdb_upcoming", "limit": src_limit}],
                limit=limit,
                sort=sort or "release_date",
                release_basis=basis,
            )
        days = RELEASE_TIMING_DAYS.get(value_id)
        if days is None:
            return None
        today = date.today()
        start = (today - timedelta(days=days)).isoformat()
        end = today.isoformat()
        src: dict[str, Any] = {
            "type": "tmdb_discover",
            "limit": src_limit,
            "sort_by": sort_by,
        }
        if section_type == "movie":
            src["primary_release_date_gte"] = start
            src["primary_release_date_lte"] = end
        else:
            src["first_air_date_gte"] = start
            src["first_air_date_lte"] = end
        return _tmdb_child_definition(
            [src],
            limit=limit,
            sort=sort,
            release_basis=basis,
        )

    return None


def category_filter_or_source(
    category: str,
    value_id: str,
    value_label: str,
    *,
    section_type: str,
    instance_key: str | None,
    limit: int | None,
    sort: str,
    release_basis: str | None = None,
    pull_new: bool = False,
    missing_limit: int = 0,
) -> dict[str, Any]:
    """Build a child recipe definition for one category value."""
    if category == "tag":
        try:
            tag_id = int(value_id)
        except (TypeError, ValueError) as exc:
            raise CollectionSetValidationError(f"invalid tag id: {value_id!r}") from exc
        if not instance_key:
            raise CollectionSetValidationError("tag category requires an instance_key")
        return {
            "sources": [
                {
                    "type": "arr_tag",
                    "instance_key": instance_key,
                    "tag_id": tag_id,
                    "limit": limit or 500,
                }
            ],
            "filters": [],
            "limit": limit,
            "sort": sort or "title",
            "sort_provider": None,
            "pins": {"include": [], "exclude": []},
        }

    if pull_new and category != "tag":
        tmdb_def = _pull_new_tmdb_definition(
            category,
            value_id,
            value_label,
            section_type=section_type,
            limit=limit,
            sort=sort,
            release_basis=release_basis,
            missing_limit=missing_limit,
        )
        if tmdb_def is not None:
            return tmdb_def

    if category == "genre":
        rule = {"field": "genre", "op": "includes_any", "values": [value_label]}
    elif category == "studio":
        rule = {"field": "studio_network", "op": "contains", "value": value_label}
    elif category == "network":
        rule = {"field": "studio_network", "op": "contains", "value": value_label}
    elif category == "content_rating":
        rule = {"field": "certification", "op": "in", "values": [value_label]}
    elif category == "decade":
        start, end = decade_bounds(value_id)
        rule = {"field": "year", "op": "between", "value": start, "value_to": end, "basis": "premiered"}
    elif category == "release_timing":
        basis = release_basis or default_release_basis(section_type)
        rule = _release_timing_filter(value_id, basis)
    else:
        raise CollectionSetValidationError(f"unknown collection_set category: {category!r}")

    return {
        "sources": [{"type": "catalog"}],
        "filters": [rule],
        "limit": limit,
        "sort": sort or "title",
        "sort_provider": None,
        "pins": {"include": [], "exclude": []},
    }


# Back-compat alias.
dimension_filter_or_source = category_filter_or_source


def expand_collection_set(
    config: dict[str, Any],
    section_type: str,
    *,
    auto_add: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return [{value_id, value_label, title, definition}, ...]."""
    pull_new = bool(auto_add and auto_add.get("enabled"))
    missing_limit = 0
    if pull_new and isinstance(auto_add, dict):
        try:
            missing_limit = int(auto_add.get("missing_limit") or auto_add.get("max_per_run") or 10)
        except (TypeError, ValueError):
            missing_limit = 10
    category = str(config.get("category") or config.get("dimension") or "")
    discovered = discover_category_values(
        section_type,
        category,
        instance_key=config.get("instance_key"),
    )
    active = select_active_values(
        discovered,
        selection_mode=config["selection_mode"],
        values=config.get("values") or [],
    )
    pattern = config.get("title_pattern") or default_title_pattern(category)
    out: list[dict[str, Any]] = []
    for item in active:
        title = format_collection_title(pattern, category=category, value=item["label"])
        definition = category_filter_or_source(
            category,
            item["id"],
            item["label"],
            section_type=section_type,
            instance_key=config.get("instance_key"),
            limit=config.get("limit"),
            sort=config.get("sort") or "title",
            release_basis=config.get("release_basis"),
            pull_new=pull_new,
            missing_limit=missing_limit,
        )
        definition["include_library"] = bool(config.get("include_library", True))
        out.append(
            {
                "value_id": item["id"],
                "value_label": item["label"],
                "value": item["label"],
                "title": title,
                "definition": definition,
                "source": _child_source(definition, pull_new=pull_new, category=category),
            }
        )
    return out


def _child_source(definition: dict[str, Any], *, pull_new: bool, category: str) -> str:
    """``tmdb``, ``library``, ``library_fallback`` (TMDB wanted but value unresolved), or ``arr_tag``."""
    types = {str(s.get("type") or "") for s in definition.get("sources") or []}
    if types == {"arr_tag"}:
        return "arr_tag"
    if types and types <= {"catalog"}:
        return "library_fallback" if pull_new and category != "tag" else "library"
    return "tmdb"


def previous_managed_titles(
    config: dict[str, Any],
    previous_summary: Optional[dict[str, Any]],
    section_id: int,
) -> list[str]:
    managed = config.get("managed_by_section") or {}
    from_def = managed.get(str(section_id)) if isinstance(managed, dict) else None
    if isinstance(from_def, list) and from_def:
        return [str(t) for t in from_def if str(t or "").strip()]
    if isinstance(previous_summary, dict):
        block = previous_summary.get("collection_set") or {}
        by_section = block.get("managed_titles") if isinstance(block, dict) else None
        if isinstance(by_section, dict):
            titles = by_section.get(str(section_id)) or []
            if isinstance(titles, list):
                return [str(t) for t in titles if str(t or "").strip()]
    return []
