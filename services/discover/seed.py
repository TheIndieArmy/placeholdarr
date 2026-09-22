"""Fetch TMDB sources into tmdb_movie + membership tables."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import CatalogSource, TmdbMovie, TmdbMovieSource
from services.tmdb_client import (
    fetch_discover,
    fetch_list,
    fetch_popular,
    fetch_trending,
    fetch_upcoming,
    tmdb_poster_cdn_url,
)


def _year_from_date(date_str: Any) -> int | None:
    text = str(date_str or "").strip()
    if len(text) >= 4 and text[:4].isdigit():
        try:
            return int(text[:4])
        except ValueError:
            return None
    return None


def _upsert_movie(session, item: dict[str, Any]) -> TmdbMovie:
    tmdb_id = int(item["tmdb_id"])
    row = session.get(TmdbMovie, tmdb_id)
    poster_path = item.get("poster_path")
    remote = tmdb_poster_cdn_url(poster_path) if poster_path else None
    year = item.get("year") or _year_from_date(item.get("date") or item.get("release_date"))
    if row is None:
        row = TmdbMovie(tmdb_id=tmdb_id, title=str(item.get("title") or f"TMDB {tmdb_id}"))
        session.add(row)
    row.title = str(item.get("title") or row.title or f"TMDB {tmdb_id}")
    row.year = year if year is not None else row.year
    row.overview = item.get("overview") or row.overview
    row.poster_path = poster_path or row.poster_path
    row.remote_poster = remote or row.remote_poster
    row.popularity = item.get("popularity") if item.get("popularity") is not None else row.popularity
    row.vote_average = item.get("vote_average") if item.get("vote_average") is not None else row.vote_average
    row.vote_count = item.get("vote_count") if item.get("vote_count") is not None else row.vote_count
    row.genre_ids = item.get("genre_ids") or row.genre_ids
    row.original_language = item.get("original_language") or row.original_language
    row.release_date = str(item.get("date") or item.get("release_date") or row.release_date or "") or None
    row.updated_at = datetime.now(timezone.utc)
    return row


def _link_source(session, tmdb_id: int, source_id: int) -> None:
    existing = (
        session.query(TmdbMovieSource)
        .filter(TmdbMovieSource.tmdb_id == tmdb_id, TmdbMovieSource.source_id == source_id)
        .first()
    )
    if existing is None:
        session.add(TmdbMovieSource(tmdb_id=tmdb_id, source_id=source_id))


def fetch_items_for_source(source: CatalogSource) -> list[dict[str, Any]]:
    filters = source.filters_json if isinstance(source.filters_json, dict) else {}
    limit = int(filters.get("limit") or 200)
    limit = max(1, min(limit, 10000))
    st = str(source.source_type or "").strip().lower()
    media = str(source.media_type or "movie").strip().lower() or "movie"
    if media != "movie":
        raise ValueError("Phase 1 discover seed supports movies only")

    if st in ("tmdb_trending", "trending"):
        window = str(filters.get("window") or "week")
        return fetch_trending("movie", window=window, limit=limit)
    if st in ("tmdb_popular", "popular"):
        return fetch_popular("movie", limit=limit)
    if st in ("tmdb_upcoming", "upcoming"):
        return fetch_upcoming("movie", limit=limit)
    if st in ("tmdb_list", "list"):
        list_id = filters.get("list_id") or filters.get("id")
        if not list_id:
            raise ValueError("list source requires filters.list_id")
        return fetch_list(list_id, "movie", limit=limit)
    # default: discover
    genre_ids = filters.get("genre_ids") or filters.get("with_genres")
    if isinstance(genre_ids, str):
        genre_ids = [int(x) for x in genre_ids.split(",") if str(x).strip().isdigit()]
    elif isinstance(genre_ids, list):
        genre_ids = [int(x) for x in genre_ids if str(x).strip().lstrip("-").isdigit()]
    else:
        genre_ids = None
    return fetch_discover(
        "movie",
        genre_ids=genre_ids,
        year_from=filters.get("year_from"),
        year_to=filters.get("year_to"),
        provider_ids=filters.get("provider_ids"),
        watch_region=filters.get("watch_region"),
        min_vote_average=filters.get("min_vote_average"),
        sort_by=str(filters.get("sort_by") or "popularity.desc"),
        limit=limit,
    )


def run_source(source_id: int, *, session=None) -> dict[str, Any]:
    """Seed one catalog source. TMDB HTTP runs without holding a DB transaction open.

    ``session`` is accepted for API compatibility but ignored: load and write each use
    a short-lived session so long TMDB paging cannot leave idle-in-transaction rows.
    """
    del session  # always use short-lived sessions (see docstring)
    from types import SimpleNamespace

    session = get_session()
    try:
        source = session.get(CatalogSource, source_id)
        if source is None:
            raise ValueError(f"catalog_source {source_id} not found")
        source_name = source.name
        source_type = source.source_type
        media_type = source.media_type
        filters_json = dict(source.filters_json or {}) if isinstance(source.filters_json, dict) else {}
    finally:
        session.close()

    source_proxy = SimpleNamespace(
        source_type=source_type,
        media_type=media_type,
        filters_json=filters_json,
    )
    logger.info(
        f"Discover source {source_id} ({source_name}): fetching from TMDB "
        f"(type={source_type} limit={filters_json.get('limit')})",
        extra={"emoji_type": "gear"},
    )
    items = fetch_items_for_source(source_proxy)
    partial = bool(getattr(items, "partial", False))
    failed_at_page = getattr(items, "failed_at_page", None)
    if partial:
        logger.warning(
            f"Discover source {source_id} ({source_name}): partial TMDB fetch "
            f"({len(items)} title(s) before failure"
            f"{f' at page {failed_at_page}' if failed_at_page else ''}); writing what we have",
            extra={"emoji_type": "warning"},
        )
    else:
        logger.info(
            f"Discover source {source_id} ({source_name}): fetched {len(items)} title(s); writing DB",
            extra={"emoji_type": "info"},
        )

    session = get_session()
    try:
        source = session.get(CatalogSource, source_id)
        if source is None:
            raise ValueError(f"catalog_source {source_id} not found after fetch")
        added = 0
        linked = 0
        created_tmdb_ids: list[int] = []
        for item in items:
            if not item.get("tmdb_id"):
                continue
            tid = int(item["tmdb_id"])
            before = session.get(TmdbMovie, tid)
            _upsert_movie(session, item)
            if before is None:
                added += 1
                created_tmdb_ids.append(tid)
            _link_source(session, tid, source.id)
            linked += 1
            if linked and linked % 200 == 0:
                session.flush()
                logger.info(
                    f"Discover source {source_id}: upserted {linked}/{len(items)}...",
                    extra={"emoji_type": "info"},
                )
        db_stats: dict[str, Any] = {
            "fetched": len(items),
            "upserted": linked,
            "created": added,
            "source_type": source.source_type,
        }
        if partial:
            db_stats["partial"] = True
            if failed_at_page is not None:
                db_stats["failed_at_page"] = int(failed_at_page)
            err = getattr(items, "error", None)
            if err:
                db_stats["partial_error"] = str(err)[:240]
        source.last_run_at = datetime.now(timezone.utc)
        source.last_run_stats = db_stats
        source.updated_at = datetime.now(timezone.utc)
        session.commit()
        logger.info(
            f"Discover source {source.id} ({source.name}): fetched={db_stats['fetched']} "
            f"created={db_stats['created']}"
            f"{' (partial)' if partial else ''}",
            extra={"emoji_type": "warning" if partial else "success"},
        )
        return {**db_stats, "created_tmdb_ids": created_tmdb_ids}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def run_all_enabled_sources(*, session=None) -> dict[str, Any]:
    own = session is None
    session = session or get_session()
    try:
        ids = [
            row.id
            for row in session.query(CatalogSource)
            .filter(CatalogSource.enabled.is_(True), CatalogSource.media_type == "movie")
            .order_by(CatalogSource.id.asc())
            .all()
        ]
    finally:
        if own:
            session.close()
            session = None

    totals: dict[str, Any] = {
        "sources": 0,
        "fetched": 0,
        "created": 0,
        "upserted": 0,
        "partial_sources": 0,
        "created_tmdb_ids": [],
        "errors": [],
    }
    created_ids: set[int] = set()
    for sid in ids:
        totals["sources"] += 1
        try:
            stats = run_source(sid)
            totals["fetched"] += int(stats.get("fetched") or 0)
            totals["created"] += int(stats.get("created") or 0)
            totals["upserted"] += int(stats.get("upserted") or 0)
            if stats.get("partial"):
                totals["partial_sources"] += 1
            for tid in stats.get("created_tmdb_ids") or []:
                try:
                    created_ids.add(int(tid))
                except (TypeError, ValueError):
                    continue
        except Exception as exc:
            logger.error(f"Discover source {sid} failed: {exc}", extra={"emoji_type": "error"})
            totals["errors"].append({"source_id": sid, "error": str(exc)})
    totals["created_tmdb_ids"] = sorted(created_ids)
    return totals


def ensure_default_popular_source(*, session=None) -> Optional[int]:
    """Create a default popular-movies source when none exist (onboarding helper)."""
    own = session is None
    session = session or get_session()
    try:
        existing = session.query(CatalogSource).filter(CatalogSource.media_type == "movie").count()
        if existing:
            return None
        src = CatalogSource(
            name="Popular movies",
            source_type="tmdb_popular",
            media_type="movie",
            filters_json={"limit": 200},
            enabled=True,
        )
        session.add(src)
        session.commit()
        session.refresh(src)
        return int(src.id)
    except Exception:
        session.rollback()
        raise
    finally:
        if own:
            session.close()
