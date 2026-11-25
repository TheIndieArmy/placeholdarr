"""Auto-add Missing-from-ARR titles during Collections recipe runs (#201)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from core.config import settings
from core.logger import logger
from services.postgres.db import session_scope
from services.postgres.models import CollectionAutoAdd


def _identity_key(media_type: str, item: dict[str, Any]) -> tuple[str, int | None, int | None, str]:
    tmdb = item.get("tmdb_id")
    tvdb = item.get("tvdb_id")
    imdb = str(item.get("imdb_id") or "").strip()
    try:
        tmdb_i = int(tmdb) if tmdb is not None else None
    except (TypeError, ValueError):
        tmdb_i = None
    try:
        tvdb_i = int(tvdb) if tvdb is not None else None
    except (TypeError, ValueError):
        tvdb_i = None
    return media_type, tmdb_i, tvdb_i, imdb


def _row_signature(media_type: str, tmdb_id: int | None, tvdb_id: int | None) -> tuple[int | None, int | None]:
    if media_type == "movie":
        return (tmdb_id, None)
    return (tmdb_id, tvdb_id)


def held_placeholders(*, collection_key: str, media_type: str) -> set[tuple[int | None, int | None]]:
    """Identities auto-added for this collection that still have no real media file."""
    from services.collections.prune import title_has_real_file

    key = str(collection_key or "").strip()
    if not key:
        return set()
    out: set[tuple[int | None, int | None]] = set()
    with session_scope() as session:
        rows = session.query(CollectionAutoAdd).filter(CollectionAutoAdd.media_type == media_type).all()
        for row in rows:
            keys = row.collection_keys if isinstance(row.collection_keys, list) else []
            if key not in [str(k) for k in keys]:
                continue
            if title_has_real_file(
                row.media_type,
                tmdb_id=row.tmdb_id,
                tvdb_id=row.tvdb_id,
                instance_key=row.instance_key,
            ):
                continue
            out.add(_row_signature(media_type, row.tmdb_id, row.tvdb_id))
    return out


def _append_collection_key(row: CollectionAutoAdd, collection_key: str | None) -> None:
    if not collection_key:
        return
    keys = list(row.collection_keys or [])
    ck = str(collection_key).strip()
    if ck and ck not in keys:
        keys.append(ck)
    row.collection_keys = keys


def touch_ledger_seen(
    *,
    recipe_id: int,
    media_type: str,
    missing_items: list[dict[str, Any]],
    collection_key: str | None = None,
) -> None:
    """Refresh last_seen for titles still on the missing list."""
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        for item in missing_items:
            if not isinstance(item, dict):
                continue
            media_type, tmdb_i, tvdb_i, imdb = _identity_key(media_type, item)
            instance_key = str(item.get("instance_key") or item.get("target_instance_key") or "").strip()
            row = (
                session.query(CollectionAutoAdd)
                .filter(
                    CollectionAutoAdd.media_type == media_type,
                    CollectionAutoAdd.tmdb_id == tmdb_i,
                    CollectionAutoAdd.tvdb_id == tvdb_i,
                    CollectionAutoAdd.instance_key == instance_key,
                )
                .first()
            )
            if row is None:
                continue
            row.last_seen_on_list_at = now
            ids = list(row.recipe_ids or [])
            if recipe_id not in ids:
                ids.append(recipe_id)
            row.recipe_ids = ids
            _append_collection_key(row, collection_key)
            session.add(row)


def run_recipe_auto_add(
    *,
    recipe_id: int,
    section_type: str,
    auto_add: dict[str, Any],
    missing_items: list[dict[str, Any]],
    collection_key: str | None = None,
) -> dict[str, Any]:
    """Add missing titles up to missing_limit for one collection key."""
    from services.source_of_truth import arr_api

    if not auto_add or not missing_items:
        return {"ok": True, "skipped": True, "added": 0}

    media_type = "movie" if section_type == "movie" else "show"
    cap = int(auto_add.get("missing_limit") or auto_add.get("max_per_run") or 5)
    cap = max(1, min(cap, 50))
    keys = list(auto_add.get("instance_keys") or [])
    tag_labels = [str(t).strip() for t in (auto_add.get("tags") or []) if str(t).strip()]
    instance_options = auto_add.get("instance_options") if isinstance(auto_add.get("instance_options"), dict) else {}
    default_qp = int(auto_add.get("quality_profile_id") or 0)
    default_root = str(auto_add.get("root_folder_path") or "").strip()
    monitored = bool(auto_add.get("monitored", True))
    search = bool(auto_add.get("search", False))

    pending = list(missing_items)[:cap]
    added = 0
    errors = 0
    now = datetime.now(timezone.utc)

    for instance_key in keys:
        arr_type = "radarr" if media_type == "movie" else "sonarr"
        instance = settings.resolve_arr_instance(arr_type, instance_key=instance_key)
        if not instance:
            errors += 1
            continue
        opt = instance_options.get(instance_key) if isinstance(instance_options, dict) else None
        if isinstance(opt, dict):
            quality_profile_id = int(opt.get("quality_profile_id") or 0)
            root_folder = str(opt.get("root_folder_path") or "").strip()
        else:
            quality_profile_id = default_qp
            root_folder = default_root
        if quality_profile_id < 1 or not root_folder:
            errors += 1
            continue
        url = str(instance.get("url") or "")
        api_key = str(instance.get("api_key") or "")
        tag_ids: list[int] = []
        for label in tag_labels:
            tag_id = arr_api.ensure_arr_tag(url=url, api_key=api_key, label=label)
            if tag_id is not None:
                tag_ids.append(int(tag_id))
        objects = [
            SimpleNamespace(
                title=str(it.get("title") or "Untitled"),
                year=it.get("year"),
                tmdb_id=it.get("tmdb_id"),
                tvdb_id=it.get("tvdb_id"),
                imdb_id=it.get("imdb_id"),
            )
            for it in pending
            if isinstance(it, dict)
        ]
        results = arr_api.add_missing_titles(
            media_type=media_type,
            url=url,
            api_key=api_key,
            items=objects,
            quality_profile_id=quality_profile_id,
            root_folder_path=root_folder,
            monitored=monitored,
            search=search,
            tag_ids=tag_ids,
            instance_key=instance_key,
        )
        with session_scope() as session:
            for item, result in zip(objects, results):
                if str(result.get("status")) != "ok":
                    if str(result.get("status")) == "error":
                        errors += 1
                    continue
                added += 1
                tmdb_i = getattr(item, "tmdb_id", None)
                tvdb_i = getattr(item, "tvdb_id", None)
                try:
                    tmdb_i = int(tmdb_i) if tmdb_i is not None else None
                except (TypeError, ValueError):
                    tmdb_i = None
                try:
                    tvdb_i = int(tvdb_i) if tvdb_i is not None else None
                except (TypeError, ValueError):
                    tvdb_i = None
                row = (
                    session.query(CollectionAutoAdd)
                    .filter(
                        CollectionAutoAdd.media_type == media_type,
                        CollectionAutoAdd.tmdb_id == tmdb_i,
                        CollectionAutoAdd.tvdb_id == tvdb_i,
                        CollectionAutoAdd.instance_key == instance_key,
                    )
                    .first()
                )
                arr_id = result.get("arr_id")
                try:
                    arr_id = int(arr_id) if arr_id is not None else None
                except (TypeError, ValueError):
                    arr_id = None
                if row is None:
                    row = CollectionAutoAdd(
                        media_type=media_type,
                        tmdb_id=tmdb_i,
                        tvdb_id=tvdb_i,
                        imdb_id=str(getattr(item, "imdb_id", None) or "") or None,
                        instance_key=instance_key,
                        title=str(getattr(item, "title", None) or ""),
                        added_at=now,
                        collection_keys=[collection_key] if collection_key else [],
                    )
                if arr_id is not None:
                    row.arr_id = arr_id
                row.last_seen_on_list_at = now
                recipe_ids = list(row.recipe_ids or [])
                if recipe_id not in recipe_ids:
                    recipe_ids.append(recipe_id)
                row.recipe_ids = recipe_ids
                _append_collection_key(row, collection_key)
                session.add(row)

    logger.info(
        f"Collections auto-add recipe_id={recipe_id} collection_key={collection_key!r} "
        f"added={added} errors={errors} cap={cap}",
        extra={"emoji_type": "info"},
    )
    return {"ok": errors == 0, "added": added, "errors": errors, "attempted": len(pending)}


def mark_played_for_catalog_match(
    *,
    media_type: str,
    tmdb_id: int | None = None,
    tvdb_id: int | None = None,
    imdb_id: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        q = session.query(CollectionAutoAdd).filter(CollectionAutoAdd.media_type == media_type)
        if tmdb_id is not None:
            q = q.filter(CollectionAutoAdd.tmdb_id == int(tmdb_id))
        elif tvdb_id is not None:
            q = q.filter(CollectionAutoAdd.tvdb_id == int(tvdb_id))
        elif imdb_id:
            q = q.filter(CollectionAutoAdd.imdb_id == str(imdb_id).strip())
        else:
            return
        for row in q.all():
            row.played_at = now
            session.add(row)