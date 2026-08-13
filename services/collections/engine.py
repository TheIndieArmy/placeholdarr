"""Rule engine for the Collections builder.

Pipeline per recipe:
  source blocks (TMDB / catalog) -> match to Placeholdarr catalog by TMDB/TVDB id
  -> filter blocks (metadata AND) -> sort/limit -> resolve ratingKeys in the
  target Plex section -> create/update the Plex collection.

`preview_definition()` runs the same pipeline without touching the Plex
collection so the builder UI can show live staged counts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, or_

from core.logger import logger
from services import list_sources, tmdb_client
from services.media_servers import plex_collections
from services.postgres.db import session_scope
from services.postgres.models import CollectionRecipe, Episode, Movie, Season, Series

SOURCE_TYPES = (
    "tmdb_trending",
    "tmdb_popular",
    "tmdb_upcoming",
    "tmdb_discover",
    "tmdb_list",
    "mdblist",
    "trakt_list",
    "catalog",
)
FILTER_FIELDS = (
    "genre",
    "year",
    "certification",
    "studio_network",
    "monitored",
    # "quality" is legacy (matched downloaded-file quality); superseded by quality_profile.
    "quality",
    "quality_profile",
    "original_language",
    "instance",
    "release_window",
    "rating",
)
SORT_OPTIONS = ("popularity", "release_date", "latest_aired", "title")
# release_window basis: TV air-date modes + per-type movie release dates.
RELEASE_WINDOW_BASES = (
    "premiered",
    "latest_episode",
    "latest_season",
    "theater",
    "digital",
    "physical",
)
MOVIE_RELEASE_BASES = ("theater", "digital", "physical")
# Radarr nested rating providers (Sonarr is a single flat value/votes).
MOVIE_RATING_PROVIDERS = ("imdb", "tmdb", "trakt", "metacritic", "rottenTomatoes")
DEFAULT_SOURCE_LIMIT = 100
MAX_COLLECTION_ITEMS = 500


class RecipeValidationError(Exception):
    """Raised when a recipe definition is structurally invalid."""


# ---------------------------------------------------------------------------
# Definition validation
# ---------------------------------------------------------------------------

def _validate_filter_rule(rule: Any) -> dict[str, Any]:
    if not isinstance(rule, dict):
        raise RecipeValidationError("each filter block must be an object")
    if rule.get("field") not in FILTER_FIELDS:
        raise RecipeValidationError(f"unknown filter field: {rule.get('field')!r}")
    if rule.get("field") == "release_window" and rule.get("basis") is not None:
        if rule["basis"] not in RELEASE_WINDOW_BASES:
            raise RecipeValidationError(f"unknown release_window basis: {rule['basis']!r}")
    if rule.get("field") == "rating":
        provider = rule.get("provider")
        if provider is not None and provider not in MOVIE_RATING_PROVIDERS:
            raise RecipeValidationError(f"unknown rating provider: {provider!r}")
        min_votes = rule.get("min_votes")
        if min_votes is not None:
            try:
                if int(min_votes) < 0:
                    raise RecipeValidationError("rating min_votes must be >= 0")
            except (TypeError, ValueError) as exc:
                raise RecipeValidationError("rating min_votes must be an integer") from exc
    return rule


MAX_FILTER_DEPTH = 3


def _normalize_filter_node(node: Any, depth: int) -> Optional[dict[str, Any]]:
    """Validate one tree node (rule or and/or group). Empty groups are dropped."""
    if isinstance(node, dict) and "field" in node:
        return _validate_filter_rule(node)
    if isinstance(node, dict) and node.get("op") in ("and", "or"):
        if depth > MAX_FILTER_DEPTH:
            raise RecipeValidationError(f"filter groups can nest at most {MAX_FILTER_DEPTH} levels deep")
        raw_children = node.get("children")
        if not isinstance(raw_children, list):
            raise RecipeValidationError("each filter group needs a children list")
        children = []
        for child in raw_children:
            normalized = _normalize_filter_node(child, depth + 1)
            if normalized is not None:
                children.append(normalized)
        if not children:
            return None
        return {"op": node["op"], "children": children}
    raise RecipeValidationError("filter nodes must be rules or groups with op 'and'/'or'")


def _normalize_filters(filters: Any) -> dict[str, Any]:
    """Normalize filters to a boolean tree of and/or groups over rules.

    Shapes accepted:
      - legacy flat rule list (implicit AND): [rule, ...]
      - linear OR groups: {"op": "or", "children": [{"op": "and", "children": [rule...]}, ...]}
      - full nesting (Advanced): arbitrary and/or tree, depth-capped at MAX_FILTER_DEPTH.

    Empty groups are dropped; an empty tree means "everything passes".
    """
    if filters is None:
        filters = []
    if isinstance(filters, list):
        rules = [_validate_filter_rule(rule) for rule in filters]
        groups = [{"op": "and", "children": rules}] if rules else []
        return {"op": "or", "children": groups}
    if isinstance(filters, dict):
        if filters.get("op") not in ("and", "or"):
            raise RecipeValidationError("filters root must be a group with op 'and' or 'or'")
        root = _normalize_filter_node(filters, 1)
        return root if root is not None else {"op": str(filters["op"]), "children": []}
    raise RecipeValidationError("filters must be a list of rules or a group object")


def validate_definition(definition: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a recipe definition. Raises RecipeValidationError."""
    if not isinstance(definition, dict):
        raise RecipeValidationError("definition must be an object")

    sources = definition.get("sources") or []
    if not isinstance(sources, list) or not sources:
        raise RecipeValidationError("definition needs at least one source block")
    normalized_sources = []
    for source in sources:
        if not isinstance(source, dict):
            raise RecipeValidationError("each source block must be an object")
        source_type = source.get("type")
        if source_type not in SOURCE_TYPES:
            raise RecipeValidationError(f"unknown source type: {source_type!r}")
        if source_type == "tmdb_list" and not str(source.get("list_id") or "").strip():
            raise RecipeValidationError("tmdb_list source requires a list_id")
        if source_type in ("mdblist", "trakt_list") and not str(source.get("list_ref") or "").strip():
            raise RecipeValidationError(f"{source_type} source requires a list URL or user/slug")
        normalized_sources.append(source)

    filters = _normalize_filters(definition.get("filters"))

    limit = definition.get("limit")
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise RecipeValidationError("limit must be an integer")
        if limit < 1:
            raise RecipeValidationError("limit must be >= 1")
        limit = min(limit, MAX_COLLECTION_ITEMS)

    sort = definition.get("sort")
    if sort is not None and sort not in SORT_OPTIONS:
        raise RecipeValidationError(f"unknown sort option: {sort!r}")

    pins = definition.get("pins") or {}
    if not isinstance(pins, dict):
        raise RecipeValidationError("pins must be an object")
    normalized_pins: dict[str, list[dict[str, Any]]] = {"include": [], "exclude": []}
    for bucket in ("include", "exclude"):
        entries = pins.get(bucket) or []
        if not isinstance(entries, list):
            raise RecipeValidationError(f"pins.{bucket} must be a list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise RecipeValidationError(f"each pins.{bucket} item must be an object")
            if not (entry.get("tmdb_id") or entry.get("tvdb_id") or entry.get("imdb_id")):
                raise RecipeValidationError(f"each pins.{bucket} item needs a tmdb/tvdb/imdb id")
            normalized_pins[bucket].append(entry)

    return {
        "sources": normalized_sources,
        "filters": filters,
        "limit": limit,
        "sort": sort,
        "pins": normalized_pins,
    }


# ---------------------------------------------------------------------------
# Source blocks
# ---------------------------------------------------------------------------

def _media_type_for_section(section_type: str) -> str:
    return "movie" if section_type == "movie" else "tv"


def _fetch_source_items(source: dict[str, Any], media_type: str) -> list[dict[str, Any]]:
    """Fetch candidates for one external source block (cached by the underlying clients)."""
    source_type = source.get("type")
    limit = int(source.get("limit") or DEFAULT_SOURCE_LIMIT)
    if source_type == "tmdb_trending":
        return tmdb_client.fetch_trending(media_type, str(source.get("window") or "week"), limit)
    if source_type == "tmdb_popular":
        return tmdb_client.fetch_popular(media_type, limit)
    if source_type == "tmdb_upcoming":
        return tmdb_client.fetch_upcoming(media_type, limit)
    if source_type == "tmdb_discover":
        return tmdb_client.fetch_discover(
            media_type,
            genre_ids=[int(g) for g in (source.get("genre_ids") or [])],
            year_from=source.get("year_from"),
            year_to=source.get("year_to"),
            provider_ids=[int(p) for p in (source.get("provider_ids") or [])],
            watch_region=source.get("watch_region"),
            min_vote_average=source.get("min_vote_average"),
            limit=limit,
        )
    if source_type == "tmdb_list":
        return tmdb_client.fetch_list(str(source.get("list_id")), media_type, limit)
    if source_type == "mdblist":
        return list_sources.fetch_mdblist(str(source.get("list_ref")), media_type, limit)
    if source_type == "trakt_list":
        return list_sources.fetch_trakt_list(str(source.get("list_ref")), media_type, limit)
    return []


def _gather_source_candidates(sources: list[dict[str, Any]], media_type: str) -> Optional[list[dict[str, Any]]]:
    """Union all external source blocks (TMDB / MDBList / Trakt) into one candidate list.

    Returns None when the catalog itself is the candidate pool (a catalog block
    present, or no external sources at all). Candidates carry tmdb/imdb/tvdb ids
    so catalog matching can fall back when a TMDB id is missing.
    """
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    has_external_source = False
    has_catalog_source = False

    for source in sources:
        if source.get("type") == "catalog":
            has_catalog_source = True
            continue
        has_external_source = True
        items = _fetch_source_items(source, media_type)
        for item in items:
            key = (
                f"tmdb:{item.get('tmdb_id')}"
                if item.get("tmdb_id")
                else f"imdb:{item.get('imdb_id')}"
                if item.get("imdb_id")
                else f"tvdb:{item.get('tvdb_id')}"
            )
            if key in seen:
                continue
            seen.add(key)
            candidates.append(item)

    if not has_external_source:
        return None
    if has_catalog_source:
        # Catalog block alongside external blocks widens the pool to the whole catalog.
        return None
    return candidates


def _candidate_id_sets(candidates: list[dict[str, Any]]) -> dict[str, set]:
    return {
        "tmdb": {int(c["tmdb_id"]) for c in candidates if c.get("tmdb_id")},
        "imdb": {str(c["imdb_id"]) for c in candidates if c.get("imdb_id")},
        "tvdb": {int(c["tvdb_id"]) for c in candidates if c.get("tvdb_id")},
    }


def _candidate_index(candidates: Optional[list[dict[str, Any]]]) -> dict[tuple[str, Any], dict[str, Any]]:
    """Index candidates by every provider id they carry, for popularity lookups."""
    index: dict[tuple[str, Any], dict[str, Any]] = {}
    for c in candidates or []:
        if c.get("tmdb_id"):
            index.setdefault(("tmdb", int(c["tmdb_id"])), c)
        if c.get("imdb_id"):
            index.setdefault(("imdb", str(c["imdb_id"])), c)
        if c.get("tvdb_id"):
            index.setdefault(("tvdb", int(c["tvdb_id"])), c)
    return index


# ---------------------------------------------------------------------------
# Catalog matching
# ---------------------------------------------------------------------------

def _load_catalog_rows(section_type: str, id_sets: Optional[dict[str, set]]) -> list[Any]:
    """Load active catalog rows, optionally restricted to candidate ids (any provider matches)."""
    with session_scope() as session:
        if section_type == "movie":
            query = session.query(Movie).filter(Movie.is_deleted.is_(False))
            if id_sets is not None:
                clauses = []
                if id_sets.get("tmdb"):
                    clauses.append(Movie.tmdbid.in_(id_sets["tmdb"]))
                if id_sets.get("imdb"):
                    clauses.append(Movie.imdbid.in_(id_sets["imdb"]))
                if not clauses:
                    return []
                query = query.filter(or_(*clauses))
            rows = query.all()
        else:
            query = session.query(Series).filter(Series.is_deleted.is_(False))
            if id_sets is not None:
                clauses = []
                if id_sets.get("tmdb"):
                    clauses.append(Series.sonarr_tmdbid.in_(id_sets["tmdb"]))
                if id_sets.get("tvdb"):
                    clauses.append(Series.tvdbid.in_(id_sets["tvdb"]))
                if id_sets.get("imdb"):
                    clauses.append(Series.imdbid.in_(id_sets["imdb"]))
                if not clauses:
                    return []
                query = query.filter(or_(*clauses))
            rows = query.all()
        session.expunge_all()
    return rows


def _dedupe_rows(rows: list[Any], section_type: str) -> list[Any]:
    """Collapse multi-instance rows for the same title into one entry."""
    seen: set[Any] = set()
    deduped = []
    for row in rows:
        key = row.tmdbid if section_type == "movie" else row.tvdbid
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


# ---------------------------------------------------------------------------
# Filter blocks
# ---------------------------------------------------------------------------

def _row_genres(row: Any, section_type: str) -> list[str]:
    raw = row.radarr_genres if section_type == "movie" else row.sonarr_genres
    if not isinstance(raw, list):
        return []
    return [str(g).strip().lower() for g in raw if g]


def _row_certification(row: Any, section_type: str) -> str:
    raw = row.radarr_certification if section_type == "movie" else row.sonarr_certification
    return str(raw or "").strip().upper()


def _row_studio_network(row: Any, section_type: str) -> str:
    raw = row.radarr_studio if section_type == "movie" else row.sonarr_network
    return str(raw or "").strip().lower()


def _row_monitored(row: Any, section_type: str) -> bool:
    return bool(row.radarr_monitored if section_type == "movie" else row.sonarr_monitored)


def _row_quality(row: Any, section_type: str) -> str:
    raw = row.radarr_quality if section_type == "movie" else row.sonarr_quality
    return str(raw or "").strip().lower()


def _row_payload(row: Any, section_type: str) -> dict[str, Any]:
    raw = row.radarr_payload_raw if section_type == "movie" else row.sonarr_payload_raw
    return raw if isinstance(raw, dict) else {}


def _row_original_language(row: Any, section_type: str) -> str:
    lang = _row_payload(row, section_type).get("originalLanguage")
    if isinstance(lang, dict):
        return str(lang.get("name") or "").strip().lower()
    return str(lang or "").strip().lower()


def _row_quality_profile_key(row: Any, section_type: str) -> str:
    """Composite '{instance_key}:{qualityProfileId}' matching builder-meta option keys."""
    profile_id = _row_payload(row, section_type).get("qualityProfileId")
    if profile_id is None:
        return ""
    return f"{str(row.instance_key or '')}:{profile_id}"


def _nested_rating_entry(raw: dict[str, Any], provider: str) -> Optional[tuple[float, int]]:
    nested = raw.get(provider)
    if not isinstance(nested, dict):
        return None
    value = nested.get("value")
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    votes_raw = nested.get("votes")
    try:
        votes = int(votes_raw or 0)
    except (TypeError, ValueError):
        votes = 0
    return float(value), max(0, votes)


def _row_rating_detail(
    row: Any,
    section_type: str,
    provider: Optional[str] = None,
) -> Optional[tuple[float, int]]:
    """Return (value, votes) from ARR ratings JSON.

    Movies (Radarr): ``provider`` selects imdb/tmdb/trakt/metacritic/rottenTomatoes on
    that provider's native scale. When ``provider`` is omitted, keep the legacy
    best-effort fallback (IMDb → TMDB → Metacritic → RT), with RT scaled to ~0–10
    so older recipes that assume a /10 threshold keep working.

    Shows (Sonarr): single flat ``value`` / ``votes`` from Skyhook (typically IMDb
    when mapped); ``provider`` is ignored.
    """
    raw = row.radarr_ratings if section_type == "movie" else row.sonarr_ratings
    if not isinstance(raw, dict):
        return None

    if section_type != "movie":
        direct = raw.get("value")
        if not isinstance(direct, (int, float)) or direct <= 0:
            return None
        try:
            votes = int(raw.get("votes") or 0)
        except (TypeError, ValueError):
            votes = 0
        return float(direct), max(0, votes)

    if provider:
        return _nested_rating_entry(raw, str(provider))

    # Legacy movie path (no provider on the rule).
    for key in ("imdb", "tmdb", "metacritic", "rottenTomatoes", "trakt"):
        entry = _nested_rating_entry(raw, key)
        if entry is None:
            continue
        value, votes = entry
        if key == "rottenTomatoes" and value > 10:
            value = value / 10.0
        elif key == "metacritic" and value > 10:
            value = value / 10.0
        return value, votes
    return None


def _row_rating(row: Any, section_type: str, provider: Optional[str] = None) -> Optional[float]:
    detail = _row_rating_detail(row, section_type, provider)
    return detail[0] if detail else None


def _row_release_date(row: Any, section_type: str) -> Optional[datetime]:
    """Legacy fallback: movies prefer digital → physical → theater; shows use series premiere."""
    if section_type == "movie":
        raw = row.digital_release_date or row.physical_release_date or row.theater_release_date
    else:
        raw = row.sonarr_first_aired
    return _coerce_utc_datetime(raw)


def _row_movie_basis_date(row: Any, basis: str) -> Optional[datetime]:
    if basis in MOVIE_RELEASE_BASES:
        column = {
            "theater": "theater_release_date",
            "digital": "digital_release_date",
            "physical": "physical_release_date",
        }[basis]
        return _coerce_utc_datetime(getattr(row, column, None))
    # premiered / omitted: first available release date (back-compat with flat movie dates).
    return _row_release_date(row, "movie")


def _coerce_utc_datetime(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        return datetime(raw.year, raw.month, raw.day, tzinfo=timezone.utc)
    except Exception:
        return None


def _iter_filter_rules(filters: Any):
    """Yield every leaf rule from a flat rule list or an and/or tree."""
    if isinstance(filters, list):
        for rule in filters:
            if isinstance(rule, dict) and "field" in rule:
                yield rule
        return
    if isinstance(filters, dict):
        if "field" in filters:
            yield filters
            return
        for child in filters.get("children") or []:
            yield from _iter_filter_rules(child)


def _definition_needs_air_dates(normalized: dict[str, Any], section_type: str) -> bool:
    if section_type == "movie":
        return False
    if normalized.get("sort") == "latest_aired":
        return True
    for rule in _iter_filter_rules(normalized["filters"]):
        if rule.get("field") == "release_window" and rule.get("basis") in ("latest_episode", "latest_season"):
            return True
    return False


def _load_air_date_lookup(series_row_ids: list[int]) -> dict[int, dict[str, Optional[datetime]]]:
    """Per catalog Series row: latest aired episode date + premiere date of the newest aired season.

    Closes the "Seerr gap": a 2015 series with a season that aired last week counts
    as recent under latest_episode/latest_season, unlike the premiere-based date.
    """
    if not series_row_ids:
        return {}
    today = datetime.now(timezone.utc).date()
    lookup: dict[int, dict[str, Optional[datetime]]] = {}
    with session_scope() as session:
        latest_episode_rows = (
            session.query(Season.series_id, func.max(Episode.air_date))
            .join(Episode, Episode.season_id == Season.id)
            .filter(
                Season.series_id.in_(series_row_ids),
                Season.is_deleted.is_(False),
                Episode.is_deleted.is_(False),
                Episode.air_date.isnot(None),
                Episode.air_date <= today,
            )
            .group_by(Season.series_id)
            .all()
        )
        for series_id, latest in latest_episode_rows:
            lookup.setdefault(int(series_id), {})["latest_episode"] = _coerce_utc_datetime(latest)

        # Season premieres: MIN(air_date) per season, then the newest premiere that has
        # already aired. Season 0 (specials) excluded so a one-off special doesn't count
        # as a "new season".
        premiere_sub = (
            session.query(
                Season.series_id.label("series_id"),
                func.min(Episode.air_date).label("premiere"),
            )
            .join(Episode, Episode.season_id == Season.id)
            .filter(
                Season.series_id.in_(series_row_ids),
                Season.is_deleted.is_(False),
                Season.season_number > 0,
                Episode.is_deleted.is_(False),
                Episode.air_date.isnot(None),
            )
            .group_by(Season.series_id, Season.id)
            .subquery()
        )
        latest_season_rows = (
            session.query(premiere_sub.c.series_id, func.max(premiere_sub.c.premiere))
            .filter(premiere_sub.c.premiere <= today)
            .group_by(premiere_sub.c.series_id)
            .all()
        )
        for series_id, premiere in latest_season_rows:
            lookup.setdefault(int(series_id), {})["latest_season"] = _coerce_utc_datetime(premiere)
    return lookup


def _row_basis_date(
    row: Any,
    section_type: str,
    basis: str,
    air_dates: Optional[dict[int, dict[str, Optional[datetime]]]],
) -> Optional[datetime]:
    if section_type == "movie":
        return _row_movie_basis_date(row, basis)
    if basis == "premiered":
        return _coerce_utc_datetime(row.sonarr_first_aired)
    if basis not in ("latest_episode", "latest_season"):
        return _row_release_date(row, section_type)
    if air_dates is None:
        return None
    return (air_dates.get(int(row.id)) or {}).get(basis)


def _passes_filter(
    row: Any,
    rule: dict[str, Any],
    section_type: str,
    air_dates: Optional[dict[int, dict[str, Optional[datetime]]]] = None,
) -> bool:
    field = rule.get("field")
    op = str(rule.get("op") or "")

    if field == "genre":
        values = {str(v).strip().lower() for v in (rule.get("values") or []) if v}
        if not values:
            return True
        genres = set(_row_genres(row, section_type))
        if op == "excludes":
            return not (genres & values)
        return bool(genres & values)

    if field == "year":
        year = int(row.year or 0)
        if not year:
            return False
        if op == "gte":
            return year >= int(rule.get("value") or 0)
        if op == "lte":
            return year <= int(rule.get("value") or 9999)
        if op == "between":
            low = int(rule.get("value") or 0)
            high = int(rule.get("value_to") or 9999)
            return low <= year <= high
        return year == int(rule.get("value") or 0)

    if field == "certification":
        values = {str(v).strip().upper() for v in (rule.get("values") or []) if v}
        if not values:
            return True
        cert = _row_certification(row, section_type)
        if op == "not_in":
            return cert not in values
        return cert in values

    if field == "studio_network":
        needle = str(rule.get("value") or "").strip().lower()
        if not needle:
            return True
        haystack = _row_studio_network(row, section_type)
        if op == "not_contains":
            return needle not in haystack
        return needle in haystack

    if field == "monitored":
        expected = bool(rule.get("value", True))
        return _row_monitored(row, section_type) == expected

    if field == "quality":
        needle = str(rule.get("value") or "").strip().lower()
        if not needle:
            return True
        haystack = _row_quality(row, section_type)
        if op == "not_contains":
            return needle not in haystack
        return needle in haystack

    if field == "quality_profile":
        values = {str(v).strip() for v in (rule.get("values") or []) if v}
        if not values:
            return True
        profile_key = _row_quality_profile_key(row, section_type)
        if op == "not_in":
            return profile_key not in values
        return profile_key in values

    if field == "original_language":
        values = {str(v).strip().lower() for v in (rule.get("values") or []) if v}
        if not values:
            return True
        language = _row_original_language(row, section_type)
        if op == "not_in":
            return language not in values
        return language in values

    if field == "instance":
        expected = str(rule.get("value") or "").strip()
        if not expected:
            return True
        return str(row.instance_key or "") == expected

    if field == "release_window":
        basis = str(rule.get("basis") or "premiered")
        release = _row_basis_date(row, section_type, basis, air_dates)
        now = datetime.now(timezone.utc)
        if op in ("has_released", "not_yet_released"):
            if release is None:
                return False
            released = release <= now
            return released if op == "has_released" else not released
        days = int(rule.get("value") or 0)
        if days <= 0:
            return True
        if release is None:
            return False
        if op == "within_next":
            return now <= release <= now + timedelta(days=days)
        return now - timedelta(days=days) <= release <= now

    if field == "rating":
        threshold = float(rule.get("value") or 0)
        provider = rule.get("provider")
        if provider is not None:
            provider = str(provider)
        detail = _row_rating_detail(row, section_type, provider)
        if detail is None:
            return False
        rating, votes = detail
        min_votes = rule.get("min_votes")
        if min_votes is not None:
            try:
                if votes < int(min_votes):
                    return False
            except (TypeError, ValueError):
                return False
        if op == "lte":
            return rating <= threshold
        return rating >= threshold

    return True


def _node_passes(
    row: Any,
    node: dict[str, Any],
    section_type: str,
    air_dates: Optional[dict[int, dict[str, Optional[datetime]]]] = None,
) -> bool:
    """Recursive and/or tree evaluation with short-circuiting; empty groups pass."""
    if "field" in node:
        return _passes_filter(row, node, section_type, air_dates)
    children = node.get("children") or []
    if not children:
        return True
    if node.get("op") == "or":
        return any(_node_passes(row, child, section_type, air_dates) for child in children)
    return all(_node_passes(row, child, section_type, air_dates) for child in children)


def _filters_tree(filters: Any) -> Optional[dict[str, Any]]:
    """Coerce a (possibly legacy) filters value to a tree root, or None when empty."""
    if not filters:
        return None
    if isinstance(filters, list):
        return {"op": "and", "children": [rule for rule in filters if isinstance(rule, dict)]}
    if isinstance(filters, dict) and (filters.get("children") or "field" in filters):
        return filters
    return None


def _apply_filters(
    rows: list[Any],
    filters: Any,
    section_type: str,
    air_dates: Optional[dict[int, dict[str, Optional[datetime]]]] = None,
) -> list[Any]:
    tree = _filters_tree(filters)
    if tree is None:
        return rows
    return [row for row in rows if _node_passes(row, tree, section_type, air_dates)]


# ---------------------------------------------------------------------------
# Sort / limit
# ---------------------------------------------------------------------------

def _sort_rows(
    rows: list[Any],
    sort: Optional[str],
    section_type: str,
    candidate_index: dict[tuple[str, Any], dict[str, Any]],
    air_dates: Optional[dict[int, dict[str, Optional[datetime]]]] = None,
) -> list[Any]:
    if sort == "title":
        return sorted(rows, key=lambda r: str(r.title or "").lower())
    if sort == "release_date":
        epoch = datetime(1900, 1, 1, tzinfo=timezone.utc)
        return sorted(rows, key=lambda r: _row_release_date(r, section_type) or epoch, reverse=True)
    if sort == "latest_aired":
        # Shows: latest aired episode; movies (or shows missing episode data): release date.
        epoch = datetime(1900, 1, 1, tzinfo=timezone.utc)

        def aired_key(row: Any) -> datetime:
            value = _row_basis_date(row, section_type, "latest_episode", air_dates)
            return value or _row_release_date(row, section_type) or epoch

        return sorted(rows, key=aired_key, reverse=True)
    if sort == "popularity" and candidate_index:
        def popularity(row: Any) -> float:
            tmdb_id = row.tmdbid if section_type == "movie" else row.sonarr_tmdbid
            item = candidate_index.get(("tmdb", int(tmdb_id or 0)))
            if item is None and getattr(row, "imdbid", None):
                item = candidate_index.get(("imdb", str(row.imdbid)))
            if item is None and section_type != "movie" and getattr(row, "tvdbid", None):
                item = candidate_index.get(("tvdb", int(row.tvdbid)))
            return float(item.get("popularity") or 0) if item else 0.0
        return sorted(rows, key=popularity, reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Pins (always include / always exclude)
# ---------------------------------------------------------------------------

def _pin_id_sets(entries: list[dict[str, Any]]) -> dict[str, set]:
    return {
        "tmdb": {int(e["tmdb_id"]) for e in entries if e.get("tmdb_id")},
        "imdb": {str(e["imdb_id"]) for e in entries if e.get("imdb_id")},
        "tvdb": {int(e["tvdb_id"]) for e in entries if e.get("tvdb_id")},
    }


def _row_matches_ids(row: Any, section_type: str, id_sets: dict[str, set]) -> bool:
    if section_type == "movie":
        if id_sets["tmdb"] and int(row.tmdbid or 0) in id_sets["tmdb"]:
            return True
    else:
        if id_sets["tmdb"] and int(row.sonarr_tmdbid or 0) in id_sets["tmdb"]:
            return True
        if id_sets["tvdb"] and int(row.tvdbid or 0) in id_sets["tvdb"]:
            return True
    if id_sets["imdb"] and str(getattr(row, "imdbid", "") or "") in id_sets["imdb"]:
        return True
    return False


def _row_identity(row: Any, section_type: str) -> Any:
    return row.tmdbid if section_type == "movie" else row.tvdbid


def _apply_exclude_pins(
    rows: list[Any],
    pins: dict[str, list[dict[str, Any]]],
    section_type: str,
) -> tuple[list[Any], int]:
    """Drop excluded titles regardless of source/filters. Runs before the item limit."""
    exclude_entries = pins.get("exclude") or []
    if not exclude_entries:
        return rows, 0
    exclude_ids = _pin_id_sets(exclude_entries)
    kept = [row for row in rows if not _row_matches_ids(row, section_type, exclude_ids)]
    return kept, len(rows) - len(kept)


def _merge_include_pins(
    rows: list[Any],
    pins: dict[str, list[dict[str, Any]]],
    section_type: str,
) -> list[Any]:
    """Force-add pinned catalog rows (bypassing sources/filters) so they sort with everything else."""
    include_entries = pins.get("include") or []
    if not include_entries:
        return rows
    include_ids = _pin_id_sets(include_entries)
    present = {_row_identity(row, section_type) for row in rows}
    pin_rows = _dedupe_rows(_load_catalog_rows(section_type, include_ids), section_type)
    for row in pin_rows:
        if _row_identity(row, section_type) in present:
            continue
        present.add(_row_identity(row, section_type))
        rows.append(row)
    return rows


def _apply_limit_with_pins(
    rows: list[Any],
    pins: dict[str, list[dict[str, Any]]],
    section_type: str,
    limit: int,
) -> tuple[list[Any], int]:
    """Cap the sorted selection while guaranteeing include-pinned rows survive.

    Pinned rows keep their sorted position and count toward the limit; non-pinned
    rows fill the remaining slots in sort order. Returns (rows, pinned_in_count).
    """
    include_entries = pins.get("include") or []
    if not include_entries:
        return rows[:limit], 0
    include_ids = _pin_id_sets(include_entries)
    pinned_total = sum(1 for row in rows if _row_matches_ids(row, section_type, include_ids))
    nonpinned_budget = max(limit - pinned_total, 0)
    selected: list[Any] = []
    pinned_in = 0
    for row in rows:
        if _row_matches_ids(row, section_type, include_ids):
            selected.append(row)
            pinned_in += 1
        elif nonpinned_budget > 0:
            selected.append(row)
            nonpinned_budget -= 1
    return selected, pinned_in


# ---------------------------------------------------------------------------
# Provider keys for Plex resolution
# ---------------------------------------------------------------------------

def _provider_key_groups(rows: list[Any], section_type: str) -> list[list[str]]:
    groups = []
    for row in rows:
        if section_type == "movie":
            groups.append([f"tmdb:{row.tmdbid}"])
        else:
            keys = []
            if row.tvdbid:
                keys.append(f"tvdb:{row.tvdbid}")
            if row.sonarr_tmdbid:
                keys.append(f"tmdb:{row.sonarr_tmdbid}")
            groups.append(keys or ["tvdb:0"])
    return groups


def _row_summary(row: Any, section_type: str) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "year": row.year,
        "tmdb_id": int(row.tmdbid if section_type == "movie" else (row.sonarr_tmdbid or 0)) or None,
        "tvdb_id": int(getattr(row, "tvdbid", 0) or 0) or None,
        "poster": row.remote_poster,
    }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _evaluate(
    definition: dict[str, Any],
    section_id: int,
    section_type: str,
    *,
    resolve: bool,
) -> dict[str, Any]:
    normalized = validate_definition(definition)
    media_type = _media_type_for_section(section_type)

    candidates = _gather_source_candidates(normalized["sources"], media_type)
    id_sets = _candidate_id_sets(candidates) if candidates is not None else None

    rows = _load_catalog_rows(section_type, id_sets)
    matched_count = len(_dedupe_rows(rows, section_type))

    air_dates = (
        _load_air_date_lookup([int(r.id) for r in rows])
        if _definition_needs_air_dates(normalized, section_type)
        else None
    )

    # Filter before dedupe so instance-scoped rules (instance, quality_profile) can
    # match any instance row for a title; dedupe then keeps the first passing row.
    rows = _apply_filters(rows, normalized["filters"], section_type, air_dates)
    rows = _dedupe_rows(rows, section_type)
    filtered_count = len(rows)

    rows, pinned_out = _apply_exclude_pins(rows, normalized["pins"], section_type)
    rows = _merge_include_pins(rows, normalized["pins"], section_type)

    rows = _sort_rows(rows, normalized["sort"], section_type, _candidate_index(candidates), air_dates)
    limit = normalized["limit"] or MAX_COLLECTION_ITEMS
    rows, pinned_in = _apply_limit_with_pins(rows, normalized["pins"], section_type, limit)

    result: dict[str, Any] = {
        "tmdb_candidates": len(candidates) if candidates is not None else None,
        "matched_in_catalog": matched_count,
        "after_filters": filtered_count,
        "pinned_in": pinned_in,
        "pinned_out": pinned_out,
        "selected": len(rows),
        "rows": rows,
        "resolved_items": None,
        "in_target_library": None,
        "unresolved": None,
        "plex_error": None,
    }

    if resolve:
        try:
            resolved_items, missing = plex_collections.resolve_items_in_section(
                section_id, section_type, _provider_key_groups(rows, section_type)
            )
            result["resolved_items"] = resolved_items
            result["in_target_library"] = len(resolved_items)
            result["unresolved"] = len(missing)
        except plex_collections.PlexCollectionsError as exc:
            result["plex_error"] = str(exc)

    return result


def preview_definition(
    definition: dict[str, Any],
    section_id: int,
    section_type: str,
    *,
    sample_size: int = 48,
) -> dict[str, Any]:
    """Evaluate a definition without writing to Plex. Returns staged counts + sample items."""
    outcome = _evaluate(definition, section_id, section_type, resolve=True)
    sample = [_row_summary(row, section_type) for row in outcome["rows"][:sample_size]]
    return {
        "tmdb_candidates": outcome["tmdb_candidates"],
        "matched_in_catalog": outcome["matched_in_catalog"],
        "after_filters": outcome["after_filters"],
        "pinned_in": outcome["pinned_in"],
        "pinned_out": outcome["pinned_out"],
        "selected": outcome["selected"],
        "in_target_library": outcome["in_target_library"],
        "unresolved": outcome["unresolved"],
        "plex_error": outcome["plex_error"],
        "sample": sample,
    }


def _item_id_sets(item: dict[str, Any]) -> dict[str, set]:
    return {
        "tmdb": {int(item["tmdb_id"])} if item.get("tmdb_id") else set(),
        "imdb": {str(item["imdb_id"])} if item.get("imdb_id") else set(),
        "tvdb": {int(item["tvdb_id"])} if item.get("tvdb_id") else set(),
    }


def _candidate_matches_ids(candidate: dict[str, Any], target: dict[str, set]) -> bool:
    if candidate.get("tmdb_id") and int(candidate["tmdb_id"]) in target["tmdb"]:
        return True
    if candidate.get("imdb_id") and str(candidate["imdb_id"]) in target["imdb"]:
        return True
    if candidate.get("tvdb_id") and int(candidate["tvdb_id"]) in target["tvdb"]:
        return True
    return False


def _verdict_tree(
    row: Any,
    node: dict[str, Any],
    section_type: str,
    air_dates: Optional[dict[int, dict[str, Optional[datetime]]]],
) -> dict[str, Any]:
    """Mirror the filter tree with a pass/fail verdict on every node for one row."""
    if "field" in node:
        passed = _passes_filter(row, node, section_type, air_dates)
        return {
            "kind": "rule",
            "field": node.get("field"),
            "op": node.get("op"),
            "value": node.get("value"),
            "value_to": node.get("value_to"),
            "values": node.get("values"),
            "basis": node.get("basis"),
            "provider": node.get("provider"),
            "min_votes": node.get("min_votes"),
            "status": "pass" if passed else "fail",
        }
    children = [_verdict_tree(row, child, section_type, air_dates) for child in (node.get("children") or [])]
    if not children:
        passed = True
    elif node.get("op") == "or":
        passed = any(child["status"] == "pass" for child in children)
    else:
        passed = all(child["status"] == "pass" for child in children)
    return {"kind": "group", "op": node.get("op"), "status": "pass" if passed else "fail", "children": children}


def _count_passing_rules(verdict: dict[str, Any]) -> int:
    if verdict["kind"] == "rule":
        return 1 if verdict["status"] == "pass" else 0
    return sum(_count_passing_rules(child) for child in verdict["children"])


def _mark_verdict_skipped(verdict: dict[str, Any]) -> None:
    verdict["status"] = "skip"
    for child in verdict.get("children") or []:
        _mark_verdict_skipped(child)


def _neutralize_verdicts(verdict: dict[str, Any]) -> None:
    """Grey out branches that didn't matter: failing children of a passing OR group."""
    if verdict["kind"] != "group":
        return
    if verdict["op"] == "or" and verdict["status"] == "pass":
        for child in verdict["children"]:
            if child["status"] == "fail":
                _mark_verdict_skipped(child)
            else:
                _neutralize_verdicts(child)
        return
    for child in verdict["children"]:
        _neutralize_verdicts(child)


def explain_definition_item(
    definition: dict[str, Any],
    section_id: int,
    section_type: str,
    item: dict[str, Any],
) -> dict[str, Any]:
    """Trace one title through the pipeline and report a per-stage pass/fail verdict.

    Stage statuses: "pass" | "fail" | "skip" (not reached after an earlier failure).
    """
    normalized = validate_definition(definition)
    media_type = _media_type_for_section(section_type)
    target = _item_id_sets(item)
    if not (target["tmdb"] or target["imdb"] or target["tvdb"]):
        raise RecipeValidationError("explain item needs a tmdb/tvdb/imdb id")

    pins = normalized["pins"]
    pinned_include = any(_candidate_matches_ids(p, target) for p in pins.get("include") or [])
    pinned_exclude = any(_candidate_matches_ids(p, target) for p in pins.get("exclude") or [])
    # Mirrors engine order: excludes drop rows, then include-merge re-adds them.
    effective_exclude = pinned_exclude and not pinned_include

    stages: list[dict[str, Any]] = []
    failed = False

    # --- Stage 1: sources -------------------------------------------------
    source_checks: list[dict[str, Any]] = []
    any_source_hit = False
    for source in normalized["sources"]:
        source_type = str(source.get("type"))
        check: dict[str, Any] = {"type": source_type, "list_ref": source.get("list_ref") or source.get("list_id")}
        if source_type == "catalog":
            check["status"] = "pass"
            check["detail"] = "Catalog pool includes every tracked title"
            any_source_hit = True
        else:
            try:
                items = _fetch_source_items(source, media_type)
                hit = any(_candidate_matches_ids(c, target) for c in items)
                check["status"] = "pass" if hit else "fail"
                if hit:
                    any_source_hit = True
            except (tmdb_client.TmdbError, list_sources.ListSourceError) as exc:
                check["status"] = "fail"
                check["detail"] = str(exc)
        source_checks.append(check)
    sources_status = "pass" if any_source_hit else "fail"
    sources_detail = None
    if not any_source_hit and pinned_include:
        sources_status = "pass"
        sources_detail = "Not produced by any source — bypassed by include pin"
    if sources_status == "fail":
        failed = True
    stages.append({"key": "sources", "status": sources_status, "detail": sources_detail, "checks": source_checks})

    # --- Stage 2: catalog match -------------------------------------------
    item_rows: list[Any] = []
    if failed:
        stages.append({"key": "catalog", "status": "skip", "detail": None, "checks": []})
    else:
        item_rows = _load_catalog_rows(section_type, target)
        status = "pass" if item_rows else "fail"
        if not item_rows:
            failed = True
        stages.append(
            {
                "key": "catalog",
                "status": status,
                "detail": None if item_rows else "Not tracked by any Radarr/Sonarr instance",
                "checks": [],
            }
        )

    # --- Stage 3: filters ---------------------------------------------------
    if failed:
        stages.append({"key": "filters", "status": "skip", "detail": None, "checks": []})
    else:
        tree = _filters_tree(normalized["filters"])
        item_air_dates = (
            _load_air_date_lookup([int(r.id) for r in item_rows])
            if _definition_needs_air_dates(normalized, section_type)
            else None
        )
        # A title passes when any single instance row satisfies the tree; report the
        # verdict tree from the row that does best (overall pass first, then most
        # individual rules passed).
        best_verdict: Optional[dict[str, Any]] = None
        best_pass = False
        best_key = (-1, -1)
        for row in item_rows:
            verdict = _verdict_tree(row, tree, section_type, item_air_dates) if tree else None
            row_pass = verdict is None or verdict["status"] == "pass"
            count = _count_passing_rules(verdict) if verdict else 0
            key = (1 if row_pass else 0, count)
            if key > best_key:
                best_key = key
                best_verdict = verdict
                best_pass = row_pass
        filters_pass = bool(item_rows) and best_pass
        if best_verdict is not None:
            _neutralize_verdicts(best_verdict)

        status = "pass" if filters_pass else "fail"
        detail = None
        if not filters_pass and pinned_include:
            status = "pass"
            detail = "Filter failures bypassed by include pin"
        if status == "fail":
            failed = True
        stages.append(
            {
                "key": "filters",
                "status": status,
                "detail": detail,
                "checks": [],
                "tree": best_verdict,
            }
        )

    # --- Stage 4: pins -------------------------------------------------------
    if failed:
        stages.append({"key": "pins", "status": "skip", "detail": None, "checks": []})
    elif effective_exclude:
        failed = True
        stages.append({"key": "pins", "status": "fail", "detail": "Excluded by pin", "checks": []})
    elif pinned_include:
        stages.append({"key": "pins", "status": "pass", "detail": "Force-included by pin", "checks": []})
    else:
        stages.append({"key": "pins", "status": "pass", "detail": "No pins affect this title", "checks": []})

    # --- Stage 5: sort + limit ------------------------------------------------
    if failed:
        stages.append({"key": "limit", "status": "skip", "detail": None, "checks": []})
        in_selection = False
    else:
        candidates = _gather_source_candidates(normalized["sources"], media_type)
        id_sets = _candidate_id_sets(candidates) if candidates is not None else None
        rows = _load_catalog_rows(section_type, id_sets)
        full_air_dates = (
            _load_air_date_lookup([int(r.id) for r in rows])
            if _definition_needs_air_dates(normalized, section_type)
            else None
        )
        rows = _apply_filters(rows, normalized["filters"], section_type, full_air_dates)
        rows = _dedupe_rows(rows, section_type)
        rows, _ = _apply_exclude_pins(rows, pins, section_type)
        rows = _merge_include_pins(rows, pins, section_type)
        rows = _sort_rows(rows, normalized["sort"], section_type, _candidate_index(candidates), full_air_dates)
        limit = normalized["limit"] or MAX_COLLECTION_ITEMS
        selected, _ = _apply_limit_with_pins(rows, pins, section_type, limit)

        rank = next(
            (i + 1 for i, row in enumerate(rows) if _row_matches_ids(row, section_type, target)),
            None,
        )
        in_selection = any(_row_matches_ids(row, section_type, target) for row in selected)
        if in_selection:
            detail = f"Ranked {rank} of {len(rows)} (limit {limit})" if rank else None
            stages.append({"key": "limit", "status": "pass", "detail": detail, "checks": []})
        else:
            failed = True
            detail = (
                f"Cut by the item limit — ranked {rank} of {len(rows)} (limit {limit})"
                if rank
                else "Not present in the evaluated selection"
            )
            stages.append({"key": "limit", "status": "fail", "detail": detail, "checks": []})

    # --- Stage 6: target library ------------------------------------------------
    if failed:
        stages.append({"key": "library", "status": "skip", "detail": None, "checks": []})
        resolved_ok = False
    else:
        keys = [f"tmdb:{i}" for i in target["tmdb"]] + [f"tvdb:{i}" for i in target["tvdb"]]
        try:
            resolved, _missing = plex_collections.resolve_items_in_section(section_id, section_type, [keys])
            resolved_ok = bool(resolved)
        except plex_collections.PlexCollectionsError as exc:
            stages.append({"key": "library", "status": "fail", "detail": str(exc), "checks": []})
            return {"in_collection": False, "stages": stages}
        if resolved_ok:
            stages.append({"key": "library", "status": "pass", "detail": None, "checks": []})
        else:
            stages.append(
                {
                    "key": "library",
                    "status": "fail",
                    "detail": "Not present in the target Plex library",
                    "checks": [],
                }
            )

    return {"in_collection": bool(in_selection and resolved_ok), "stages": stages}


def run_recipe(recipe_id: int) -> dict[str, Any]:
    """Execute one recipe end-to-end and persist its run summary."""
    with session_scope() as session:
        recipe = session.query(CollectionRecipe).filter(CollectionRecipe.id == recipe_id).first()
        if recipe is None:
            raise RecipeValidationError(f"collection recipe {recipe_id} not found")
        definition = dict(recipe.definition or {})
        section_id = int(recipe.plex_section_id)
        section_type = str(recipe.plex_section_type)
        collection_title = str(recipe.collection_title)
        recipe_name = str(recipe.name)

    summary: dict[str, Any]
    try:
        outcome = _evaluate(definition, section_id, section_type, resolve=True)
        if outcome["plex_error"]:
            raise plex_collections.PlexCollectionsError(outcome["plex_error"])
        sync_stats = plex_collections.sync_collection(
            section_id, section_type, collection_title, outcome["resolved_items"] or []
        )
        summary = {
            "status": "ok",
            "tmdb_candidates": outcome["tmdb_candidates"],
            "matched_in_catalog": outcome["matched_in_catalog"],
            "after_filters": outcome["after_filters"],
            "pinned_in": outcome["pinned_in"],
            "pinned_out": outcome["pinned_out"],
            "selected": outcome["selected"],
            "in_target_library": outcome["in_target_library"],
            "unresolved": outcome["unresolved"],
            "synced": sync_stats,
        }
        logger.info(
            f"Collections: recipe {recipe_name!r} synced "
            f"(selected={outcome['selected']}, in_library={outcome['in_target_library']}, "
            f"added={sync_stats['added']}, removed={sync_stats['removed']})",
            extra={"emoji_type": "info"},
        )
    except (
        tmdb_client.TmdbError,
        list_sources.ListSourceError,
        plex_collections.PlexCollectionsError,
        RecipeValidationError,
    ) as exc:
        summary = {"status": "error", "error": str(exc)}
        logger.error(
            f"Collections: recipe {recipe_name!r} failed: {exc}",
            extra={"emoji_type": "error"},
        )

    with session_scope() as session:
        recipe = session.query(CollectionRecipe).filter(CollectionRecipe.id == recipe_id).first()
        if recipe is not None:
            recipe.last_run_at = datetime.now(timezone.utc)
            recipe.last_run_summary = summary
            session.commit()

    return summary


# ---------------------------------------------------------------------------
# Per-recipe schedules + seasonal active windows
# ---------------------------------------------------------------------------

def _parse_month_day(raw: Any) -> Optional[tuple[int, int]]:
    try:
        month_str, day_str = str(raw or "").strip().split("-", 1)
        month, day = int(month_str), int(day_str)
    except (TypeError, ValueError):
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return (month, day)


def validate_active_window(window: Any) -> Optional[dict[str, Any]]:
    """Normalize an active_window payload; raises RecipeValidationError on bad input."""
    if window is None:
        return None
    if not isinstance(window, dict):
        raise RecipeValidationError("active_window must be an object")
    start = _parse_month_day(window.get("start"))
    end = _parse_month_day(window.get("end"))
    if start is None or end is None:
        raise RecipeValidationError("active_window needs start and end as MM-DD")
    when_inactive = str(window.get("when_inactive") or "keep")
    if when_inactive not in ("keep", "clear"):
        raise RecipeValidationError("active_window when_inactive must be 'keep' or 'clear'")
    return {
        "start": f"{start[0]:02d}-{start[1]:02d}",
        "end": f"{end[0]:02d}-{end[1]:02d}",
        "when_inactive": when_inactive,
    }


def window_is_active(window: Optional[dict[str, Any]], when: Optional[datetime] = None) -> bool:
    """True when `when` falls inside the annually recurring window (inclusive).

    Wrap-around windows (start > end, e.g. 12-15 -> 01-06) span the year boundary.
    """
    if not window:
        return True
    start = _parse_month_day(window.get("start"))
    end = _parse_month_day(window.get("end"))
    if start is None or end is None:
        return True
    now = (when or datetime.now(timezone.utc)).date()
    today = (now.month, now.day)
    if start <= end:
        return start <= today <= end
    return today >= start or today <= end


def smallest_enabled_recipe_interval_hours() -> Optional[int]:
    """Smallest per-recipe schedule override among enabled recipes (None if no overrides)."""
    try:
        with session_scope() as session:
            value = (
                session.query(func.min(CollectionRecipe.run_interval_hours))
                .filter(
                    CollectionRecipe.enabled.is_(True),
                    CollectionRecipe.run_interval_hours.isnot(None),
                )
                .scalar()
            )
    except Exception:
        return None
    return int(value) if value else None


def _recipe_is_due(
    last_run_at: Optional[datetime],
    interval_hours: int,
    now: datetime,
) -> bool:
    if last_run_at is None:
        return True
    last = last_run_at if last_run_at.tzinfo else last_run_at.replace(tzinfo=timezone.utc)
    # Small grace so a job ticking at exactly the interval doesn't miss by seconds.
    return now >= last + timedelta(hours=interval_hours) - timedelta(minutes=5)


def _clear_recipe_collection(recipe_id: int) -> dict[str, Any]:
    """Empty the Plex collection for a recipe leaving its active window (when_inactive=clear)."""
    with session_scope() as session:
        recipe = session.query(CollectionRecipe).filter(CollectionRecipe.id == recipe_id).first()
        if recipe is None:
            raise RecipeValidationError(f"recipe {recipe_id} not found")
        section_id = int(recipe.plex_section_id)
        section_type = str(recipe.plex_section_type)
        collection_title = str(recipe.collection_title)
        recipe_name = str(recipe.name)

    try:
        sync_stats = plex_collections.sync_collection(section_id, section_type, collection_title, [])
        summary = {"status": "cleared", "synced": sync_stats, "window_cleared": True}
        logger.info(
            f"Collections: recipe {recipe_name!r} left its active window — collection cleared "
            f"(removed={sync_stats['removed']})",
            extra={"emoji_type": "info"},
        )
    except plex_collections.PlexCollectionsError as exc:
        summary = {"status": "error", "error": str(exc)}
        logger.error(
            f"Collections: failed to clear collection for dormant recipe {recipe_name!r}: {exc}",
            extra={"emoji_type": "error"},
        )

    with session_scope() as session:
        recipe = session.query(CollectionRecipe).filter(CollectionRecipe.id == recipe_id).first()
        if recipe is not None:
            recipe.last_run_at = datetime.now(timezone.utc)
            recipe.last_run_summary = summary
            session.commit()
    return summary


def run_all_enabled_recipes(*, force: bool = False, default_interval_hours: int = 24) -> dict[str, Any]:
    """Run enabled recipes that are due and inside their active window.

    `force=True` (manual task trigger) ignores due-ness but still respects
    seasonal windows; dormant recipes are skipped (cleared once if configured).
    """
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        recipe_rows = [
            {
                "id": row.id,
                "run_interval_hours": row.run_interval_hours,
                "active_window": row.active_window,
                "last_run_at": row.last_run_at,
                "last_run_summary": row.last_run_summary,
            }
            for row in session.query(CollectionRecipe)
            .filter(CollectionRecipe.enabled.is_(True))
            .order_by(CollectionRecipe.id)
            .all()
        ]

    results: dict[str, Any] = {
        "total": len(recipe_rows),
        "ok": 0,
        "failed": 0,
        "skipped": 0,
        "dormant": 0,
        "recipes": {},
    }
    for row in recipe_rows:
        recipe_id = int(row["id"])
        window = row["active_window"] if isinstance(row["active_window"], dict) else None

        if not window_is_active(window, now):
            if (
                window
                and window.get("when_inactive") == "clear"
                and not (row["last_run_summary"] or {}).get("window_cleared")
            ):
                summary = _clear_recipe_collection(recipe_id)
                results["recipes"][str(recipe_id)] = summary
                if summary.get("status") == "error":
                    results["failed"] += 1
                else:
                    results["dormant"] += 1
            else:
                results["recipes"][str(recipe_id)] = {"status": "dormant"}
                results["dormant"] += 1
            continue

        interval = int(row["run_interval_hours"] or default_interval_hours or 24)
        if not force and not _recipe_is_due(row["last_run_at"], interval, now):
            results["recipes"][str(recipe_id)] = {"status": "skipped", "reason": "not due"}
            results["skipped"] += 1
            continue

        try:
            summary = run_recipe(recipe_id)
        except RecipeValidationError as exc:
            summary = {"status": "error", "error": str(exc)}
        results["recipes"][str(recipe_id)] = summary
        if summary.get("status") == "ok":
            results["ok"] += 1
        else:
            results["failed"] += 1
    return results
