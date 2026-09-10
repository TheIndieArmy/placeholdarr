"""Batched jobs to resolve preferred-language poster URLs after catalog sync."""

from __future__ import annotations

from typing import Any

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import Job
from services.poster_language import (
    collect_stale_poster_language_ids,
    poster_language_resolve_enabled,
    resolve_movies_by_ids,
    resolve_series_by_ids,
)
from services.tmdb_client import tmdb_configured

POSTER_LANGUAGE_RESOLVE_JOB_TYPE = "poster_language_resolve"
_BATCH_SIZE = 40


def enqueue_poster_language_resolve_stale(*, source: str = "sync") -> dict[str, Any]:
    """Enqueue resolve jobs for catalog rows missing an exact-language localized stamp."""
    if not poster_language_resolve_enabled() or not tmdb_configured():
        return {"ok": True, "enqueued": False, "reason": "skipped", "source": source}

    session = get_session()
    try:
        ids = collect_stale_poster_language_ids(session)
        movie_ids = list(ids.get("movie_ids") or [])
        series_ids = list(ids.get("series_ids") or [])
        if not movie_ids and not series_ids:
            return {
                "ok": True,
                "enqueued": False,
                "movie_count": 0,
                "series_count": 0,
                "jobs_created": 0,
                "source": source,
            }

        from services.source_of_truth.job_priority import default_priority_for

        jobs_created = 0
        for i in range(0, len(movie_ids), _BATCH_SIZE):
            chunk = movie_ids[i : i + _BATCH_SIZE]
            session.add(
                Job(
                    job_type=POSTER_LANGUAGE_RESOLVE_JOB_TYPE,
                    payload={"movie_ids": chunk, "series_ids": [], "source": source},
                    status="PENDING",
                    max_attempts=3,
                    priority=default_priority_for(POSTER_LANGUAGE_RESOLVE_JOB_TYPE),
                )
            )
            jobs_created += 1
        for i in range(0, len(series_ids), _BATCH_SIZE):
            chunk = series_ids[i : i + _BATCH_SIZE]
            session.add(
                Job(
                    job_type=POSTER_LANGUAGE_RESOLVE_JOB_TYPE,
                    payload={"movie_ids": [], "series_ids": chunk, "source": source},
                    status="PENDING",
                    max_attempts=3,
                    priority=default_priority_for(POSTER_LANGUAGE_RESOLVE_JOB_TYPE),
                )
            )
            jobs_created += 1
        session.commit()
        return {
            "ok": True,
            "enqueued": jobs_created > 0,
            "movie_count": len(movie_ids),
            "series_count": len(series_ids),
            "jobs_created": jobs_created,
            "source": source,
        }
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        logger.warning(
            f"poster language resolve enqueue failed: {exc}",
            extra={"emoji_type": "warning"},
        )
        return {"ok": False, "enqueued": False, "reason": str(exc), "source": source}
    finally:
        try:
            session.close()
        except Exception:
            pass


def process_poster_language_resolve_job(session, job: Job) -> dict[str, Any]:
    payload = job.payload if isinstance(job.payload, dict) else {}
    movie_ids = [int(x) for x in (payload.get("movie_ids") or []) if x is not None]
    series_ids = [int(x) for x in (payload.get("series_ids") or []) if x is not None]
    movie_stats = resolve_movies_by_ids(session, movie_ids) if movie_ids else {"resolved": 0, "skipped": 0}
    series_stats = (
        resolve_series_by_ids(session, series_ids, include_seasons=True)
        if series_ids
        else {"resolved": 0, "skipped": 0, "seasons_resolved": 0}
    )
    session.commit()
    return {
        "ok": True,
        "movies": movie_stats,
        "series": series_stats,
        "source": payload.get("source"),
    }
