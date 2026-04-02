"""Dashboard routes: lightweight UI showing stats, activity, errors, and live logs."""

import os
import glob as _glob
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, text

from services.postgres.db import get_session
from services.postgres.models import (
    Movie, Series, Episode, Placeholder, Job, EventLog, ObservationTrailAttempt,
)

router = APIRouter()

# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def dashboard_page():
    html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    with open(html_path, "r") as f:
        return HTMLResponse(content=f.read())


# ---------------------------------------------------------------------------
# JSON API endpoints
# ---------------------------------------------------------------------------

@router.get("/api/stats")
async def stats():
    """Return aggregate metrics for the dashboard header."""
    session = get_session()
    try:
        # Movies
        total_movies = session.query(func.count(Movie.id)).filter(Movie.is_deleted == False).scalar() or 0
        movies_with_placeholder = session.query(func.count(Movie.id)).filter(
            Movie.is_deleted == False, Movie.has_placeholder == True
        ).scalar() or 0
        movies_with_file = session.query(func.count(Movie.id)).filter(
            Movie.is_deleted == False, Movie.has_file == True
        ).scalar() or 0

        # Series
        total_series = session.query(func.count(Series.id)).filter(Series.is_deleted == False).scalar() or 0

        # Episodes
        total_episodes = session.query(func.count(Episode.id)).filter(Episode.is_deleted == False).scalar() or 0
        episodes_with_placeholder = session.query(func.count(Episode.id)).filter(
            Episode.is_deleted == False, Episode.has_placeholder == True
        ).scalar() or 0
        episodes_with_file = session.query(func.count(Episode.id)).filter(
            Episode.is_deleted == False, Episode.has_file == True
        ).scalar() or 0

        # Placeholders on disk
        placeholders_on_disk = session.query(func.count(Placeholder.id)).filter(
            Placeholder.has_placeholder == True
        ).scalar() or 0

        # Jobs
        jobs_pending = session.query(func.count(Job.id)).filter(Job.status.in_(["PENDING", "CLAIMED", "WORKING"])).scalar() or 0
        jobs_failed = session.query(func.count(Job.id)).filter(Job.status == "FAILED").scalar() or 0
        jobs_done = session.query(func.count(Job.id)).filter(Job.status == "DONE").scalar() or 0

        # Last sync (most recent DONE job of type containing 'sync' or 'full')
        last_sync_row = session.query(Job.updated_at).filter(
            Job.status == "DONE",
            Job.job_type.ilike("%sync%"),
        ).order_by(Job.updated_at.desc()).first()
        last_sync = last_sync_row[0].isoformat() if last_sync_row and last_sync_row[0] else None

        return {
            "movies": {"total": total_movies, "placeholders": movies_with_placeholder, "downloaded": movies_with_file},
            "series": {"total": total_series},
            "episodes": {"total": total_episodes, "placeholders": episodes_with_placeholder, "downloaded": episodes_with_file},
            "placeholders_on_disk": placeholders_on_disk,
            "jobs": {"pending": jobs_pending, "failed": jobs_failed, "done": jobs_done},
            "last_sync": last_sync,
        }
    finally:
        session.close()


@router.get("/api/activity")
async def activity(limit: int = Query(50, ge=1, le=200)):
    """Return recent activity: events and completed jobs."""
    session = get_session()
    try:
        # Recent events
        events = session.query(
            EventLog.id,
            EventLog.event_type,
            EventLog.source,
            EventLog.status,
            EventLog.created_at,
            EventLog.error_message,
        ).order_by(EventLog.created_at.desc()).limit(limit).all()

        event_list = [
            {
                "id": e.id,
                "type": "event",
                "event_type": e.event_type,
                "source": e.source,
                "status": e.status,
                "error": e.error_message,
                "time": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ]

        # Recent jobs
        jobs = session.query(
            Job.id,
            Job.job_type,
            Job.status,
            Job.created_at,
            Job.updated_at,
            Job.error_message,
        ).order_by(Job.updated_at.desc()).limit(limit).all()

        job_list = [
            {
                "id": j.id,
                "type": "job",
                "job_type": j.job_type,
                "status": j.status,
                "error": j.error_message,
                "time": j.updated_at.isoformat() if j.updated_at else (j.created_at.isoformat() if j.created_at else None),
            }
            for j in jobs
        ]

        # Merge and sort by time descending
        combined = sorted(event_list + job_list, key=lambda x: x.get("time") or "", reverse=True)
        return combined[:limit]
    finally:
        session.close()


@router.get("/api/errors")
async def errors(limit: int = Query(50, ge=1, le=200)):
    """Return recent errors from jobs, events, and observation trails."""
    session = get_session()
    try:
        items = []

        # Failed jobs
        failed_jobs = session.query(
            Job.id, Job.job_type, Job.error_message, Job.updated_at
        ).filter(Job.status == "FAILED").order_by(Job.updated_at.desc()).limit(limit).all()
        for j in failed_jobs:
            items.append({
                "source": "job",
                "id": j.id,
                "label": j.job_type,
                "error": j.error_message,
                "time": j.updated_at.isoformat() if j.updated_at else None,
            })

        # Failed events
        failed_events = session.query(
            EventLog.id, EventLog.event_type, EventLog.error_message, EventLog.updated_at
        ).filter(EventLog.status == "FAILED").order_by(EventLog.updated_at.desc()).limit(limit).all()
        for e in failed_events:
            items.append({
                "source": "event",
                "id": e.id,
                "label": e.event_type,
                "error": e.error_message,
                "time": e.updated_at.isoformat() if e.updated_at else None,
            })

        # Observation trail errors
        trail_errors = session.query(
            ObservationTrailAttempt.id,
            ObservationTrailAttempt.trail_job_id,
            ObservationTrailAttempt.error_message,
            ObservationTrailAttempt.created_at,
        ).filter(
            ObservationTrailAttempt.error_message.isnot(None)
        ).order_by(ObservationTrailAttempt.created_at.desc()).limit(limit).all()
        for t in trail_errors:
            items.append({
                "source": "observation",
                "id": t.id,
                "label": f"trail job #{t.trail_job_id}",
                "error": t.error_message,
                "time": t.created_at.isoformat() if t.created_at else None,
            })

        # Placeholder lookup failures
        ph_errors = session.query(
            Placeholder.id,
            Placeholder.path,
            Placeholder.media_lookup_error,
            Placeholder.updated_at,
        ).filter(
            Placeholder.media_lookup_error.isnot(None)
        ).order_by(Placeholder.updated_at.desc()).limit(limit).all()
        for p in ph_errors:
            items.append({
                "source": "placeholder",
                "id": p.id,
                "label": os.path.basename(p.path) if p.path else f"placeholder #{p.id}",
                "error": p.media_lookup_error,
                "time": p.updated_at.isoformat() if p.updated_at else None,
            })

        items.sort(key=lambda x: x.get("time") or "", reverse=True)
        return items[:limit]
    finally:
        session.close()


@router.get("/api/logs")
async def logs(
    tail: int = Query(200, ge=1, le=2000),
    level: str = Query("all"),
):
    """Tail the current log file. Optionally filter by level (all, warn, error)."""
    from core.config import settings

    # Resolve log directory the same way logger.py does
    explicit_file = str(getattr(settings, "LOG_FILE", "") or "").strip()
    if explicit_file:
        log_dir = os.path.dirname(explicit_file) or "."
    else:
        explicit_dir = str(getattr(settings, "LOG_DIR", "") or "").strip()
        if explicit_dir:
            log_dir = explicit_dir
        else:
            appdata = str(getattr(settings, "APPDATA_PATH", "/config") or "/config").strip() or "/config"
            log_dir = os.path.join(appdata, "logs")

    # Find the most recent log file
    pattern = os.path.join(log_dir, "placeholdarr-*.log")
    log_files = sorted(_glob.glob(pattern))
    if not log_files:
        return {"lines": [], "file": None}

    log_file = log_files[-1]

    # Read last N lines efficiently
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except FileNotFoundError:
        return {"lines": [], "file": None}

    lines = all_lines[-tail:]

    # Optional level filter
    if level == "warn":
        lines = [l for l in lines if "WARNING" in l or "ERROR" in l or "CRITICAL" in l]
    elif level == "error":
        lines = [l for l in lines if "ERROR" in l or "CRITICAL" in l]

    return {
        "lines": [l.rstrip("\n") for l in lines],
        "file": os.path.basename(log_file),
    }
