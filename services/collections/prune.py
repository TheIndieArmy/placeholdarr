"""Prune never-played Discover auto-add titles (#201)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.config import settings
from core.logger import logger
from services.collections.discover_defaults import DISCOVER_AUTO_ADD_TAG
from services.postgres.db import session_scope
from services.postgres.models import CollectionAutoAdd, CollectionRecipe, Movie, Series


def _recipe_ids_for_ledger_row(row: CollectionAutoAdd) -> set[int]:
    ids: set[int] = set()
    for raw in row.recipe_ids or []:
        try:
            ids.add(int(raw))
        except (TypeError, ValueError):
            continue
    for raw in row.collection_keys or []:
        text = str(raw or "").strip()
        if not text or text.startswith("preview:"):
            continue
        head = text.split(":", 1)[0]
        try:
            ids.add(int(head))
        except (TypeError, ValueError):
            continue
    return ids


def _prune_prefs_for_recipe(recipe_id: int) -> tuple[bool, int]:
    with session_scope() as session:
        recipe = session.query(CollectionRecipe).filter(CollectionRecipe.id == int(recipe_id)).first()
        if recipe is None or not isinstance(recipe.definition, dict):
            return False, 14
        block = recipe.definition.get("auto_add")
        if not isinstance(block, dict) or not block.get("enabled"):
            return False, 14
        prune = block.get("prune") if isinstance(block.get("prune"), dict) else {}
        enabled = bool(prune.get("enabled"))
        try:
            grace = int(prune.get("grace_days") or 14)
        except (TypeError, ValueError):
            grace = 14
        grace = max(1, min(grace, 365))
        return enabled, grace


def ledger_row_prune_policy(row: CollectionAutoAdd) -> tuple[bool, int]:
    """Whether this ledger row may be pruned and the grace days to apply."""
    recipe_ids = _recipe_ids_for_ledger_row(row)
    if not recipe_ids:
        return False, 14
    enabled_any = False
    grace_max = 14
    for rid in recipe_ids:
        enabled, grace = _prune_prefs_for_recipe(rid)
        if enabled:
            enabled_any = True
            grace_max = max(grace_max, grace)
    return enabled_any, grace_max


def prune_settings() -> dict[str, Any]:
    enabled = bool(getattr(settings, "COLLECTIONS_DISCOVER_PRUNE_ENABLED", False))
    try:
        grace = int(getattr(settings, "COLLECTIONS_DISCOVER_PRUNE_GRACE_DAYS", 14) or 14)
    except (TypeError, ValueError):
        grace = 14
    grace = max(1, min(grace, 365))
    return {"enabled": enabled, "grace_days": grace, "tag": DISCOVER_AUTO_ADD_TAG}


def title_has_real_file(media_type: str, *, tmdb_id: int | None, tvdb_id: int | None, instance_key: str) -> bool:
    with session_scope() as session:
        if media_type == "movie":
            q = session.query(Movie).filter(Movie.is_deleted.is_(False))
            if tmdb_id is not None:
                q = q.filter(Movie.tmdbid == int(tmdb_id))
            else:
                return False
            row = q.first()
            if row is None:
                return False
            path = str(getattr(row, "radarr_filepath", None) or "").strip()
            return bool(path)
        q = session.query(Series).filter(Series.is_deleted.is_(False))
        if tvdb_id is not None:
            q = q.filter(Series.tvdbid == int(tvdb_id))
        else:
            return False
        row = q.first()
        if row is None:
            return False
        return bool(str(getattr(row, "sonarr_filepath", None) or "").strip())


def _arr_has_discover_tag(media_type: str, arr_id: int | None, instance_key: str) -> bool:
    if arr_id is None or arr_id < 1:
        return True
    from services.source_of_truth import arr_api

    arr_type = "radarr" if media_type == "movie" else "sonarr"
    instance = settings.resolve_arr_instance(arr_type, instance_key=instance_key)
    if not instance:
        return False
    url = str(instance.get("url") or "")
    api_key = str(instance.get("api_key") or "")
    return arr_api.arr_title_has_tag(
        media_type=media_type,
        url=url,
        api_key=api_key,
        arr_id=int(arr_id),
        label=DISCOVER_AUTO_ADD_TAG,
    )


def count_prune_candidates(*, dry_run: bool = True) -> dict[str, Any]:
    cfg = prune_settings()
    candidates: list[dict[str, Any]] = []
    with session_scope() as session:
        rows = session.query(CollectionAutoAdd).filter(CollectionAutoAdd.played_at.is_(None)).all()
        for row in rows:
            prune_ok, grace_days = ledger_row_prune_policy(row)
            if not prune_ok:
                continue
            cutoff = datetime.now(timezone.utc) - timedelta(days=grace_days)
            seen = row.last_seen_on_list_at or row.added_at
            if seen is not None and seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            if seen is not None and seen > cutoff:
                continue
            if title_has_real_file(
                row.media_type,
                tmdb_id=row.tmdb_id,
                tvdb_id=row.tvdb_id,
                instance_key=row.instance_key,
            ):
                continue
            if not _arr_has_discover_tag(row.media_type, row.arr_id, row.instance_key):
                continue
            candidates.append(
                {
                    "id": row.id,
                    "title": row.title,
                    "media_type": row.media_type,
                    "instance_key": row.instance_key,
                    "arr_id": row.arr_id,
                    "tmdb_id": row.tmdb_id,
                    "tvdb_id": row.tvdb_id,
                }
            )
    return {
        "ok": True,
        "count": len(candidates),
        "grace_days": cfg["grace_days"],
        "enabled": cfg["enabled"] or len(candidates) > 0,
    }


def run_discover_prune(*, dry_run: bool = False) -> dict[str, Any]:
    cfg = prune_settings()
    preview = count_prune_candidates(dry_run=True)
    if preview["count"] == 0 and not cfg["enabled"]:
        return {**preview, "pruned": 0, "skipped": True}
    if dry_run:
        return {**preview, "pruned": 0, "dry_run": True}
    if preview["count"] == 0:
        return {**preview, "pruned": 0}
    from services.source_of_truth import arr_api

    pruned = 0
    errors = 0
    titles: list[str] = []
    with session_scope() as session:
        rows = session.query(CollectionAutoAdd).filter(CollectionAutoAdd.played_at.is_(None)).all()
        for row in rows:
            prune_ok, grace_days = ledger_row_prune_policy(row)
            if not prune_ok:
                continue
            cutoff = datetime.now(timezone.utc) - timedelta(days=grace_days)
            seen = row.last_seen_on_list_at or row.added_at
            if seen is not None and seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            if seen is not None and seen > cutoff:
                continue
            if title_has_real_file(
                row.media_type,
                tmdb_id=row.tmdb_id,
                tvdb_id=row.tvdb_id,
                instance_key=row.instance_key,
            ):
                continue
            if row.arr_id is None or int(row.arr_id) < 1:
                continue
            if not _arr_has_discover_tag(row.media_type, row.arr_id, row.instance_key):
                continue
            arr_type = "radarr" if row.media_type == "movie" else "sonarr"
            instance = settings.resolve_arr_instance(arr_type, instance_key=row.instance_key)
            if not instance:
                errors += 1
                continue
            ok = arr_api.delete_arr_title(
                media_type=row.media_type,
                url=str(instance.get("url") or ""),
                api_key=str(instance.get("api_key") or ""),
                arr_id=int(row.arr_id),
                delete_files=False,
            )
            if ok:
                session.delete(row)
                pruned += 1
                if row.title:
                    titles.append(str(row.title))
            else:
                errors += 1
        session.commit()
    logger.info(
        f"Collections Discover prune pruned={pruned} errors={errors}",
        extra={"emoji_type": "info"},
    )
    return {**preview, "pruned": pruned, "errors": errors, "titles": titles[:50]}