"""API routes for TMDB Discover catalog mode."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.logger import logger
from services.discover.changes import run_movie_changes_sync
from services.discover.mode import is_tmdb_discover_mode
from services.discover.seed import ensure_default_popular_source, run_source
from services.postgres.db import get_session
from services.postgres.models import CatalogSource, TmdbMovie, ArrMovieOverlay
from services.tmdb_client import tmdb_configured, verify_api_key

router = APIRouter(prefix="/api/discover", tags=["discover"])


class CatalogSourceIn(BaseModel):
    name: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    media_type: str = "movie"
    filters_json: Optional[dict[str, Any]] = None
    enabled: bool = True
    run_interval_hours: Optional[int] = None


class CatalogSourcePatch(BaseModel):
    name: Optional[str] = None
    source_type: Optional[str] = None
    filters_json: Optional[dict[str, Any]] = None
    enabled: Optional[bool] = None
    run_interval_hours: Optional[int] = None


def _source_dict(row: CatalogSource) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "source_type": row.source_type,
        "media_type": row.media_type,
        "filters_json": row.filters_json or {},
        "enabled": bool(row.enabled),
        "run_interval_hours": row.run_interval_hours,
        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
        "last_run_stats": row.last_run_stats or {},
    }


@router.get("/status")
def discover_status():
    session = get_session()
    try:
        movie_count = session.query(TmdbMovie).count()
        source_count = session.query(CatalogSource).filter(CatalogSource.media_type == "movie").count()
        needs = session.query(TmdbMovie).filter(TmdbMovie.determination == "needs_placeholder").count()
        return {
            "catalog_mode_discover": is_tmdb_discover_mode(),
            "tmdb_configured": tmdb_configured(),
            "movie_count": movie_count,
            "source_count": source_count,
            "needs_placeholder": needs,
        }
    finally:
        session.close()


@router.post("/verify-tmdb")
def discover_verify_tmdb():
    if not tmdb_configured():
        raise HTTPException(status_code=400, detail="TMDB_API_KEY is not set")
    ok = verify_api_key()
    if not ok:
        raise HTTPException(status_code=400, detail="TMDB API key rejected")
    return {"ok": True}


@router.get("/sources")
def list_sources():
    session = get_session()
    try:
        rows = session.query(CatalogSource).order_by(CatalogSource.id.asc()).all()
        return {"sources": [_source_dict(r) for r in rows]}
    finally:
        session.close()


@router.post("/sources")
def create_source(body: CatalogSourceIn):
    if str(body.media_type or "movie").lower() != "movie":
        raise HTTPException(status_code=400, detail="Phase 1 supports movie sources only")
    session = get_session()
    try:
        row = CatalogSource(
            name=body.name.strip(),
            source_type=body.source_type.strip().lower(),
            media_type="movie",
            filters_json=body.filters_json or {},
            enabled=bool(body.enabled),
            run_interval_hours=body.run_interval_hours,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return {"source": _source_dict(row)}
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@router.patch("/sources/{source_id}")
def patch_source(source_id: int, body: CatalogSourcePatch):
    session = get_session()
    try:
        row = session.get(CatalogSource, source_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Source not found")
        if body.name is not None:
            row.name = body.name.strip()
        if body.source_type is not None:
            row.source_type = body.source_type.strip().lower()
        if body.filters_json is not None:
            row.filters_json = body.filters_json
        if body.enabled is not None:
            row.enabled = bool(body.enabled)
        if body.run_interval_hours is not None:
            row.run_interval_hours = body.run_interval_hours
        session.commit()
        session.refresh(row)
        return {"source": _source_dict(row)}
    except HTTPException:
        raise
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@router.delete("/sources/{source_id}")
def delete_source(source_id: int):
    session = get_session()
    try:
        row = session.get(CatalogSource, source_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Source not found")
        session.delete(row)
        session.commit()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@router.post("/sources/{source_id}/run")
def run_one_source(source_id: int):
    """Queue a per-source Discover run (seed → overlay → determine → materialize) in the background."""
    import threading

    session = get_session()
    try:
        row = session.get(CatalogSource, source_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Source not found")
    finally:
        session.close()

    def _runner() -> None:
        try:
            from services.discover.pipeline import run_discover_pipeline

            result = run_discover_pipeline(source_id=int(source_id))
            logger.info(
                f"Discover source {source_id} run finished "
                f"seed_created={(result.get('seed') or {}).get('created')} "
                f"overlay_changed={(result.get('overlay') or {}).get('changed')} "
                f"mat={result.get('materialization')}",
                extra={"emoji_type": "success"},
            )
        except Exception as exc:
            logger.error(f"Discover source run failed: {exc}", extra={"emoji_type": "error"})

    threading.Thread(target=_runner, name=f"discover-source-{source_id}", daemon=True).start()
    return {"ok": True, "started": True, "source_id": source_id, "message": "Discover source run started"}


@router.post("/run")
def run_pipeline(ensure_default: bool = True):
    """Queue the full Discover pipeline in the background (prefer Tasks → Discover catalog sync)."""
    import threading

    if not is_tmdb_discover_mode():
        raise HTTPException(status_code=400, detail="CATALOG_MODE is not tmdb_discover")
    if not tmdb_configured():
        raise HTTPException(status_code=400, detail="TMDB_API_KEY is not set")

    def _runner() -> None:
        try:
            from services.discover.scheduled import run_discover_sync

            if ensure_default:
                ensure_default_popular_source()
            run_discover_sync(trigger="manual")
        except Exception as exc:
            logger.error(f"Discover pipeline run failed: {exc}", extra={"emoji_type": "error"})

    threading.Thread(target=_runner, name="discover-pipeline-run", daemon=True).start()
    return {"ok": True, "started": True, "message": "Discover catalog sync started"}


@router.post("/changes")
def run_changes(days: int = 1):
    return run_movie_changes_sync(days=days)


@router.get("/movies")
def list_movies(limit: int = 100, offset: int = 0):
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    session = get_session()
    try:
        q = session.query(TmdbMovie).order_by(TmdbMovie.popularity.desc().nullslast(), TmdbMovie.tmdb_id.asc())
        total = q.count()
        rows = q.offset(offset).limit(limit).all()
        overlays = {
            (o.tmdb_id, o.instance_key): o
            for o in session.query(ArrMovieOverlay)
            .filter(ArrMovieOverlay.tmdb_id.in_([r.tmdb_id for r in rows] or [-1]))
            .all()
        }
        items = []
        for r in rows:
            mon = any(o.monitored for (tid, _), o in overlays.items() if tid == r.tmdb_id)
            has_file = any(o.has_file for (tid, _), o in overlays.items() if tid == r.tmdb_id)
            items.append(
                {
                    "tmdb_id": r.tmdb_id,
                    "title": r.title,
                    "year": r.year,
                    "poster_path": r.poster_path,
                    "remote_poster": r.remote_poster,
                    "popularity": r.popularity,
                    "determination": r.determination,
                    "has_placeholder": bool(r.has_placeholder),
                    "placeholder_filepath": r.placeholder_filepath,
                    "monitored": mon,
                    "has_file": has_file,
                }
            )
        return {"total": total, "items": items}
    finally:
        session.close()
