"""Plex Watchlist API: list, sync, Arr add, exclude."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.logger import logger
from services.watchlist import sync as watchlist_sync

router = APIRouter(tags=["watchlist"])


class WatchlistAddTarget(BaseModel):
    instance_key: str
    quality_profile_id: int | None = None
    root_folder_path: str | None = None


class WatchlistAddRequest(BaseModel):
    item_ids: list[int] = Field(default_factory=list)
    search: bool | None = None
    monitored: bool = True
    tags: list[str] | None = None
    tv_scope: str | None = None
    # Preferred: fan-out list (modal override). None uses Settings targets; [] means none selected.
    targets: list[WatchlistAddTarget] | None = None
    # Legacy single-target shim.
    instance_key: str | None = None
    quality_profile_id: int | None = None
    root_folder_path: str | None = None


class WatchlistExcludeRequest(BaseModel):
    item_ids: list[int] = Field(default_factory=list)
    excluded: bool = True


@router.get("/api/watchlist")
async def watchlist_list(
    status: str | None = None,
    limit: int = 500,
    user_ids: str | None = None,
):
    """List watchlist titles. ``user_ids`` is a comma-separated plex_user_id filter (OR)."""
    filter_ids = [p.strip() for p in str(user_ids or "").split(",") if p.strip()]
    summary = watchlist_sync.watchlist_summary()
    items = watchlist_sync.list_watchlist_items(
        status=status,
        limit=limit,
        plex_user_ids=filter_ids or None,
    )
    users = watchlist_sync.list_watchlist_users()
    return {"summary": summary, "items": items, "users": users}


@router.get("/api/watchlist/users")
async def watchlist_users():
    return {"users": watchlist_sync.list_watchlist_users()}


@router.get("/api/watchlist/summary")
async def watchlist_summary_only():
    return watchlist_sync.watchlist_summary()


@router.post("/api/watchlist/sync")
async def watchlist_sync_now():
    """Manual refresh of the owner Plex Watchlist (records a task run)."""
    import threading

    def _runner():
        try:
            summary = watchlist_sync.run_watchlist_sync(trigger="manual")
            try:
                from services.source_of_truth.scheduler import reschedule_task_after_completion

                reschedule_task_after_completion("watchlist_sync")
            except Exception:
                pass
            if not summary.get("ok") and summary.get("error"):
                logger.warning(
                    f"Manual watchlist sync finished with error: {summary.get('error')}",
                    extra={"emoji_type": "warning"},
                )
        except Exception as exc:
            logger.error(f"Manual watchlist sync failed: {exc}", extra={"emoji_type": "error"})

    threading.Thread(target=_runner, name="manual-watchlist-sync", daemon=True).start()
    return {"ok": True, "message": "Watchlist sync started"}


@router.post("/api/watchlist/add")
async def watchlist_add(body: WatchlistAddRequest):
    ids = [int(i) for i in (body.item_ids or []) if i is not None]
    if not ids:
        raise HTTPException(status_code=400, detail="item_ids required")
    mode = watchlist_sync.watchlist_action_mode()
    search = bool(body.search) if body.search is not None else mode == "add_and_search"
    try:
        target_payload = None
        if body.targets is not None:
            target_payload = [t.model_dump() for t in body.targets]
        result = watchlist_sync.add_watchlist_items_to_arr(
            ids,
            search=search,
            monitored=bool(body.monitored),
            tags=body.tags,
            tv_scope=body.tv_scope,
            targets=target_payload,
            instance_key=body.instance_key,
            quality_profile_id=body.quality_profile_id,
            root_folder_path=body.root_folder_path,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, **result}


@router.post("/api/watchlist/exclude")
async def watchlist_exclude(body: WatchlistExcludeRequest):
    ids = [int(i) for i in (body.item_ids or []) if i is not None]
    if not ids:
        raise HTTPException(status_code=400, detail="item_ids required")
    count = watchlist_sync.set_excluded(ids, bool(body.excluded))
    return {"ok": True, "updated": count, "excluded": bool(body.excluded)}
