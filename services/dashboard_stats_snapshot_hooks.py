"""Materialized counters for `/api/stats` (singleton row in dashboard_stats_snapshot)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import event, func
from sqlalchemy.orm import Session, object_session

from core.logger import logger
from services.postgres.models import (
    DashboardStatsSnapshot,
    Episode,
    Job,
    Movie,
    Placeholder,
    Season,
    Series,
)
from services.library_future_semantics import sql_episode_future_outside_lookahead, sql_movie_future_outside_lookahead

_hooks_registered = False
_DIRTY_KEY = "dashboard_stats_snapshot_dirty"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def build_dashboard_stats_payload(session: Session, *, include_internal: bool = False) -> dict[str, Any]:
    """Compute the `/api/stats` payload from live tables."""
    total_movies = session.query(func.count(Movie.id)).filter(Movie.is_deleted == False).scalar() or 0
    movies_with_placeholder = session.query(func.count(Movie.id)).filter(
        Movie.is_deleted == False, Movie.has_placeholder == True
    ).scalar() or 0
    movies_with_file = session.query(func.count(Movie.id)).filter(
        Movie.is_deleted == False, Movie.has_file == True
    ).scalar() or 0
    movies_future_outside_lookahead = session.query(func.count(Movie.id)).filter(
        Movie.is_deleted == False,
        sql_movie_future_outside_lookahead(Movie),
    ).scalar() or 0

    total_series = session.query(func.count(Series.id)).filter(Series.is_deleted == False).scalar() or 0

    total_episodes = session.query(func.count(Episode.id)).filter(Episode.is_deleted == False).scalar() or 0
    episodes_with_placeholder = session.query(func.count(Episode.id)).filter(
        Episode.is_deleted == False, Episode.has_placeholder == True
    ).scalar() or 0
    episodes_with_file = session.query(func.count(Episode.id)).filter(
        Episode.is_deleted == False, Episode.has_file == True
    ).scalar() or 0
    episodes_future_outside_lookahead = (
        session.query(func.count(Episode.id))
        .select_from(Episode)
        .join(Season, Episode.season_id == Season.id)
        .filter(
            Episode.is_deleted == False,
            sql_episode_future_outside_lookahead(Episode, Season),
        )
        .scalar()
        or 0
    )

    placeholders_on_disk = session.query(func.count(Placeholder.id)).filter(
        Placeholder.has_placeholder == True
    ).scalar() or 0

    jobs_pending = session.query(func.count(Job.id)).filter(Job.status.in_(["PENDING", "CLAIMED", "WORKING"])).scalar() or 0
    jobs_failed = session.query(func.count(Job.id)).filter(Job.status == "FAILED").scalar() or 0
    jobs_done = session.query(func.count(Job.id)).filter(Job.status == "DONE").scalar() or 0

    last_sync_row = session.query(Job.updated_at).filter(
        Job.status == "DONE",
        Job.job_type.ilike("%sync%"),
    ).order_by(Job.updated_at.desc()).first()
    last_sync = last_sync_row[0] if last_sync_row and last_sync_row[0] else None

    payload: dict[str, Any] = {
        "movies": {
            "total": int(total_movies),
            "placeholders": int(movies_with_placeholder),
            "downloaded": int(movies_with_file),
            "future_outside_lookahead": int(movies_future_outside_lookahead),
        },
        "series": {"total": int(total_series)},
        "episodes": {
            "total": int(total_episodes),
            "placeholders": int(episodes_with_placeholder),
            "downloaded": int(episodes_with_file),
            "future_outside_lookahead": int(episodes_future_outside_lookahead),
        },
        "placeholders_on_disk": int(placeholders_on_disk),
        "jobs": {"pending": int(jobs_pending), "failed": int(jobs_failed), "done": int(jobs_done)},
        "last_sync": last_sync.isoformat() if last_sync else None,
    }
    if include_internal:
        payload["_last_sync_dt"] = last_sync
    return payload


def _refresh_dashboard_stats_snapshot(session: Session) -> None:
    payload = build_dashboard_stats_payload(session, include_internal=True)
    now = _utc_now()

    row = session.get(DashboardStatsSnapshot, 1)
    if row is None:
        row = DashboardStatsSnapshot(id=1)
        session.add(row)

    row.movies_total = int(payload["movies"]["total"])
    row.movies_placeholders = int(payload["movies"]["placeholders"])
    row.movies_downloaded = int(payload["movies"]["downloaded"])
    row.movies_future_outside_lookahead = int(payload["movies"]["future_outside_lookahead"])
    row.series_total = int(payload["series"]["total"])
    row.episodes_total = int(payload["episodes"]["total"])
    row.episodes_placeholders = int(payload["episodes"]["placeholders"])
    row.episodes_downloaded = int(payload["episodes"]["downloaded"])
    row.episodes_future_outside_lookahead = int(payload["episodes"]["future_outside_lookahead"])
    row.placeholders_on_disk = int(payload["placeholders_on_disk"])
    row.jobs_pending = int(payload["jobs"]["pending"])
    row.jobs_failed = int(payload["jobs"]["failed"])
    row.jobs_done = int(payload["jobs"]["done"])
    row.last_sync = payload.get("_last_sync_dt")
    row.computed_at = now
    row.updated_at = now


def _mark_dirty(_mapper, _connection, target) -> None:
    session = object_session(target)
    if session is None:
        return
    session.info[_DIRTY_KEY] = True


def _after_flush_postexec(session: Session, _flush_context) -> None:
    if not session.info.pop(_DIRTY_KEY, False):
        return
    try:
        _refresh_dashboard_stats_snapshot(session)
    except Exception as exc:
        logger.warning("dashboard_stats_snapshot refresh skipped: %s", exc, extra={"emoji_type": "warning"})


def register_dashboard_stats_snapshot_hooks() -> None:
    global _hooks_registered
    if _hooks_registered:
        return
    _hooks_registered = True

    tracked_models = (Movie, Series, Episode, Placeholder, Job)
    for model in tracked_models:
        event.listen(model, "after_insert", _mark_dirty)
        event.listen(model, "after_update", _mark_dirty)
        event.listen(model, "after_delete", _mark_dirty)

    event.listen(Session, "after_flush_postexec", _after_flush_postexec)
    logger.info("Registered dashboard_stats_snapshot ORM hooks", extra={"emoji_type": "success"})
