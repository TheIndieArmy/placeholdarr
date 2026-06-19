"""Collections API: recipe CRUD, preview, manual runs, and builder metadata."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

import requests
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from core.config import settings
from core.logger import logger
from services import list_sources, tmdb_client
from services.collections.engine import (
    RecipeValidationError,
    explain_definition_item,
    preview_definition,
    run_recipe,
    validate_active_window,
    validate_definition,
    window_is_active,
)
from services.media_servers import plex_collections
from services.postgres.db import session_scope
from services.postgres.models import CollectionRecipe

router = APIRouter()


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _serialize_recipe(row: CollectionRecipe) -> dict[str, Any]:
    window = row.active_window if isinstance(row.active_window, dict) else None
    return {
        "id": row.id,
        "name": row.name,
        "enabled": bool(row.enabled),
        "plex_section_id": row.plex_section_id,
        "plex_section_type": row.plex_section_type,
        "collection_title": row.collection_title,
        "definition": row.definition or {},
        "run_interval_hours": row.run_interval_hours,
        "active_window": window,
        "window_active": window_is_active(window),
        "last_run_at": _iso(row.last_run_at),
        "last_run_summary": row.last_run_summary,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _refresh_collections_schedule() -> None:
    """Re-apply the collections job tick after schedule overrides change."""
    try:
        from services.source_of_truth.scheduler import refresh_collections_schedule

        refresh_collections_schedule()
    except Exception as exc:
        logger.warning(
            f"Collections: failed to refresh schedule after recipe change: {exc}",
            extra={"emoji_type": "warning"},
        )


class RecipePayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    enabled: bool = True
    plex_section_id: int = Field(..., ge=1)
    plex_section_type: str = Field(..., pattern="^(movie|show)$")
    collection_title: str = Field(..., min_length=1, max_length=200)
    definition: dict[str, Any]
    run_interval_hours: int | None = Field(None, ge=1, le=24 * 14)
    active_window: dict[str, Any] | None = None


class PreviewPayload(BaseModel):
    plex_section_id: int = Field(..., ge=1)
    plex_section_type: str = Field(..., pattern="^(movie|show)$")
    definition: dict[str, Any]


@router.get("/api/collections")
async def list_recipes():
    with session_scope() as session:
        rows = session.query(CollectionRecipe).order_by(CollectionRecipe.id).all()
        payload = [_serialize_recipe(row) for row in rows]
    return {
        "recipes": payload,
        "tmdb_configured": tmdb_client.tmdb_configured(),
        "trakt_configured": list_sources.trakt_configured(),
    }


@router.post("/api/collections")
async def create_recipe(body: RecipePayload):
    try:
        normalized = validate_definition(body.definition)
        window = validate_active_window(body.active_window)
    except RecipeValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    with session_scope() as session:
        row = CollectionRecipe(
            name=body.name.strip(),
            enabled=body.enabled,
            plex_section_id=body.plex_section_id,
            plex_section_type=body.plex_section_type,
            collection_title=body.collection_title.strip(),
            definition=normalized,
            run_interval_hours=body.run_interval_hours,
            active_window=window,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        payload = _serialize_recipe(row)
    _refresh_collections_schedule()
    return {"ok": True, "recipe": payload}


@router.get("/api/collections/plex-sections")
async def plex_sections():
    try:
        sections = plex_collections.list_plex_sections()
    except plex_collections.PlexCollectionsError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"sections": sections}


@router.get("/api/collections/tmdb-meta")
async def tmdb_meta(
    media_type: str = Query("movie", pattern="^(movie|tv)$"),
    region: str = Query("US", min_length=2, max_length=2),
):
    if not tmdb_client.tmdb_configured():
        return {
            "configured": False,
            "genres": [],
            "providers": [],
            "regions": [],
        }
    try:
        return {
            "configured": True,
            "genres": tmdb_client.fetch_genres(media_type),
            "providers": tmdb_client.fetch_watch_providers(media_type, region),
            "regions": tmdb_client.fetch_regions(),
        }
    except tmdb_client.TmdbError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# Quality profile lists rarely change; cache per instance for 10 minutes.
_profile_cache_lock = threading.Lock()
_profile_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_PROFILE_CACHE_TTL_SECONDS = 600.0


def _fetch_instance_quality_profiles(instance: dict[str, Any]) -> list[dict[str, Any]]:
    identity = str(instance.get("instance_id") or instance.get("instance_key") or "")
    now = time.monotonic()
    with _profile_cache_lock:
        hit = _profile_cache.get(identity)
        if hit and (now - hit[0]) <= _PROFILE_CACHE_TTL_SECONDS:
            return hit[1]

    url = str(instance.get("url") or "").rstrip("/")
    api_key = str(instance.get("api_key") or "")
    profiles: list[dict[str, Any]] = []
    if url and api_key:
        try:
            resp = requests.get(
                f"{url}/api/v3/qualityprofile",
                headers={"X-Api-Key": api_key, "Accept": "application/json"},
                timeout=15,
            )
            if resp.status_code == 200:
                for raw in resp.json() or []:
                    if isinstance(raw, dict) and raw.get("id") is not None:
                        profiles.append({"id": int(raw["id"]), "name": str(raw.get("name") or f"Profile {raw['id']}")})
        except (requests.RequestException, ValueError) as exc:
            logger.warning(
                f"Collections: failed to fetch quality profiles from {instance.get('instance_key')}: {exc}",
                extra={"emoji_type": "warning"},
            )

    with _profile_cache_lock:
        _profile_cache[identity] = (now, profiles)
    return profiles


@router.get("/api/collections/builder-meta")
async def builder_meta(media_type: str = Query("movie", pattern="^(movie|show)$")):
    """Catalog-backed builder metadata for filter pickers and ARR-scoped options."""
    arr_type = "radarr" if media_type == "movie" else "sonarr"
    instances = [
        item
        for item in (getattr(settings, "configured_arr_instances", []) or [])
        if str(item.get("arr_type") or "").lower() == arr_type
    ]

    instance_options = [
        {
            "instance_key": item.get("instance_key"),
            "label": item.get("label") or item.get("instance_key"),
            "arr_type": item.get("arr_type"),
        }
        for item in instances
    ]

    profile_options: list[dict[str, Any]] = []
    for item in instances:
        for profile in _fetch_instance_quality_profiles(item):
            profile_options.append(
                {
                    "key": f"{item.get('instance_key')}:{profile['id']}",
                    "name": profile["name"],
                    "instance_key": item.get("instance_key"),
                    "instance_label": item.get("label") or item.get("instance_key"),
                }
            )

    if media_type == "movie":
        lang_sql = text(
            "SELECT DISTINCT radarr_payload_raw->'originalLanguage'->>'name' AS lang "
            "FROM movie WHERE is_deleted = false "
            "AND radarr_payload_raw->'originalLanguage'->>'name' IS NOT NULL ORDER BY lang"
        )
        genre_sql = text(
            "SELECT DISTINCT genre "
            "FROM movie, jsonb_array_elements_text(radarr_genres::jsonb) AS genre "
            "WHERE is_deleted = false AND radarr_genres IS NOT NULL "
            "ORDER BY genre"
        )
    else:
        lang_sql = text(
            "SELECT DISTINCT sonarr_payload_raw->'originalLanguage'->>'name' AS lang "
            "FROM series WHERE is_deleted = false "
            "AND sonarr_payload_raw->'originalLanguage'->>'name' IS NOT NULL ORDER BY lang"
        )
        genre_sql = text(
            "SELECT DISTINCT genre "
            "FROM series, jsonb_array_elements_text(sonarr_genres::jsonb) AS genre "
            "WHERE is_deleted = false AND sonarr_genres IS NOT NULL "
            "ORDER BY genre"
        )
    with session_scope() as session:
        languages = [str(row[0]) for row in session.execute(lang_sql) if row[0]]
        genres = [str(row[0]) for row in session.execute(genre_sql) if row[0]]

    return {
        "instances": instance_options,
        "quality_profiles": profile_options,
        "languages": languages,
        "genres": genres,
    }


@router.post("/api/collections/preview")
async def preview(body: PreviewPayload):
    try:
        validate_definition(body.definition)
    except RecipeValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        result = preview_definition(body.definition, body.plex_section_id, body.plex_section_type)
    except (tmdb_client.TmdbError, list_sources.ListSourceError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return result


class ExplainPayload(BaseModel):
    plex_section_id: int = Field(..., ge=1)
    plex_section_type: str = Field(..., pattern="^(movie|show)$")
    definition: dict[str, Any]
    item: dict[str, Any]


@router.post("/api/collections/explain")
async def explain_item(body: ExplainPayload):
    """Trace one catalog title through a definition and report per-stage verdicts."""
    try:
        return explain_definition_item(body.definition, body.plex_section_id, body.plex_section_type, body.item)
    except RecipeValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (tmdb_client.TmdbError, list_sources.ListSourceError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/api/collections/{recipe_id}")
async def get_recipe(recipe_id: int):
    with session_scope() as session:
        row = session.query(CollectionRecipe).filter(CollectionRecipe.id == recipe_id).first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Collection recipe {recipe_id} not found")
        payload = _serialize_recipe(row)
    return {"recipe": payload}


@router.put("/api/collections/{recipe_id}")
async def update_recipe(recipe_id: int, body: RecipePayload):
    try:
        normalized = validate_definition(body.definition)
        window = validate_active_window(body.active_window)
    except RecipeValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    with session_scope() as session:
        row = session.query(CollectionRecipe).filter(CollectionRecipe.id == recipe_id).first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Collection recipe {recipe_id} not found")
        row.name = body.name.strip()
        row.enabled = body.enabled
        row.plex_section_id = body.plex_section_id
        row.plex_section_type = body.plex_section_type
        row.collection_title = body.collection_title.strip()
        row.definition = normalized
        row.run_interval_hours = body.run_interval_hours
        row.active_window = window
        session.commit()
        session.refresh(row)
        payload = _serialize_recipe(row)
    _refresh_collections_schedule()
    return {"ok": True, "recipe": payload}


class RecipeToggleRequest(BaseModel):
    enabled: bool


@router.post("/api/collections/{recipe_id}/toggle")
async def toggle_recipe(recipe_id: int, body: RecipeToggleRequest):
    with session_scope() as session:
        row = session.query(CollectionRecipe).filter(CollectionRecipe.id == recipe_id).first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Collection recipe {recipe_id} not found")
        row.enabled = body.enabled
        session.commit()
        session.refresh(row)
        payload = _serialize_recipe(row)
    _refresh_collections_schedule()
    return {"ok": True, "recipe": payload}


@router.delete("/api/collections/{recipe_id}")
async def delete_recipe(recipe_id: int):
    with session_scope() as session:
        row = session.query(CollectionRecipe).filter(CollectionRecipe.id == recipe_id).first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Collection recipe {recipe_id} not found")
        session.delete(row)
        session.commit()
    return {"ok": True}


@router.post("/api/collections/{recipe_id}/run")
async def run_recipe_now(recipe_id: int):
    with session_scope() as session:
        row = session.query(CollectionRecipe).filter(CollectionRecipe.id == recipe_id).first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Collection recipe {recipe_id} not found")
        name = str(row.name)

    def _runner():
        try:
            run_recipe(recipe_id)
        except Exception as exc:
            logger.error(
                f"Manual collection recipe run failed id={recipe_id}: {exc}",
                extra={"emoji_type": "error"},
            )

    threading.Thread(target=_runner, name=f"collection-recipe-{recipe_id}", daemon=True).start()
    return {"ok": True, "recipe_id": recipe_id, "message": f"Running collection recipe {name!r}"}
