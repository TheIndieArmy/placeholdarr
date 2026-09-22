"""TMDB /movie/changes delta hydration for Discover catalog."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import TmdbMovie
from services.tmdb_client import _request, tmdb_poster_cdn_url
from services.discover.seed import _upsert_movie


def _parse_year(date_str: str | None) -> int | None:
    text = str(date_str or "").strip()
    if len(text) >= 4 and text[:4].isdigit():
        return int(text[:4])
    return None


def hydrate_movie_details(tmdb_id: int) -> dict[str, Any] | None:
    data = _request(f"/movie/{int(tmdb_id)}")
    if not isinstance(data, dict) or not data.get("id"):
        return None
    poster_path = data.get("poster_path")
    return {
        "tmdb_id": int(data["id"]),
        "title": data.get("title") or data.get("original_title") or f"TMDB {tmdb_id}",
        "year": _parse_year(data.get("release_date")),
        "date": data.get("release_date"),
        "overview": data.get("overview"),
        "poster_path": poster_path,
        "popularity": data.get("popularity"),
        "vote_average": data.get("vote_average"),
        "vote_count": data.get("vote_count"),
        "genre_ids": [int(g.get("id")) for g in (data.get("genres") or []) if isinstance(g, dict) and g.get("id")],
        "original_language": data.get("original_language"),
    }


def run_movie_changes_sync(*, days: int = 1, max_hydrate: int = 200) -> dict[str, Any]:
    """Pull recent movie changes and upsert known or new catalog rows sparingly."""
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=max(1, min(int(days), 14)))
    stats = {"changed_ids": 0, "hydrated": 0, "errors": 0}
    try:
        data = _request(
            "/movie/changes",
            {"start_date": start.isoformat(), "end_date": end.isoformat()},
        )
    except Exception as exc:
        logger.warning(f"TMDB movie/changes failed: {exc}", extra={"emoji_type": "warning"})
        return {"error": str(exc)}

    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return stats

    ids: list[int] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        raw = item.get("id")
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    stats["changed_ids"] = len(ids)

    session = get_session()
    try:
        existing = {int(r[0]) for r in session.query(TmdbMovie.tmdb_id).all()}
        # Prefer refreshing titles already in catalog; optionally add a capped set of new ids
        refresh_ids = [i for i in ids if i in existing][:max_hydrate]
        new_budget = max(0, max_hydrate - len(refresh_ids))
        new_ids = [i for i in ids if i not in existing][:new_budget]
        for tid in refresh_ids + new_ids:
            try:
                details = hydrate_movie_details(tid)
                if not details:
                    continue
                _upsert_movie(session, details)
                stats["hydrated"] += 1
            except Exception as exc:
                stats["errors"] += 1
                logger.debug(f"hydrate tmdb={tid} failed: {exc}", extra={"emoji_type": "debug"})
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return stats
