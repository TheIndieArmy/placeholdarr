"""Dashboard routes: lightweight UI showing stats, activity, errors, and live logs."""

import os
import glob as _glob
import re
import unicodedata
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import case, func, text

from core.config import settings
from services.app_config import get_onboarding_status, get_settings_payload, save_settings
from services.integrations import test_integration_connection
from services.postgres.db import get_session
from services.postgres.models import (
    Movie, Series, Season, Episode, Placeholder, Job, EventLog, ObservationTrailAttempt,
)

router = APIRouter()


def _slugify_title(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    lowered = normalized.lower()
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered)
    lowered = re.sub(r"-{2,}", "-", lowered)
    return lowered.strip("-")


def _arr_base_url(item_type: str, instance_key: str, is_4k: bool) -> str:
    def _normalize_ui_base(url: str) -> str:
        normalized = str(url or "").rstrip("/")
        normalized = re.sub(r"/api(?:/v\d+)?$", "", normalized, flags=re.IGNORECASE)
        return normalized.rstrip("/")

    key = str(instance_key or "").strip().lower()
    if item_type == "movie":
        if is_4k or key == str(getattr(settings, "RADARR_4K_INSTANCE_KEY", "radarr_4k")).strip().lower():
            return _normalize_ui_base(str(getattr(settings, "RADARR_4K_URL", "") or ""))
        return _normalize_ui_base(str(getattr(settings, "RADARR_URL", "") or ""))
    if is_4k or key == str(getattr(settings, "SONARR_4K_INSTANCE_KEY", "sonarr_4k")).strip().lower():
        return _normalize_ui_base(str(getattr(settings, "SONARR_4K_URL", "") or ""))
    return _normalize_ui_base(str(getattr(settings, "SONARR_URL", "") or ""))


def _title_slug_from_payload(payload: dict | None, title: str) -> str:
    if isinstance(payload, dict):
        slug = str(payload.get("titleSlug") or "").strip()
        if slug:
            return slug
    return _slugify_title(title)


def _arr_item_link(item_type: str, instance_key: str, is_4k: bool, title: str, payload: dict | None) -> str | None:
    base_url = _arr_base_url(item_type=item_type, instance_key=instance_key, is_4k=is_4k)
    if not base_url:
        return None
    slug = _title_slug_from_payload(payload=payload, title=title)
    if not slug:
        return None
    route = "movie" if item_type == "movie" else "series"
    return f"{base_url}/{route}/{slug}"

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
        movies_future_outside_lookahead = session.query(func.count(Movie.id)).filter(
            Movie.is_deleted == False,
            func.coalesce(Movie.has_file, False) == False,
            func.coalesce(Movie.has_placeholder, False) == False,
            Movie.determination == "not_needed",
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
        episodes_future_outside_lookahead = session.query(func.count(Episode.id)).filter(
            Episode.is_deleted == False,
            func.coalesce(Episode.has_file, False) == False,
            func.coalesce(Episode.has_placeholder, False) == False,
            Episode.determination == "not_needed",
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
            "movies": {
                "total": total_movies,
                "placeholders": movies_with_placeholder,
                "downloaded": movies_with_file,
                "future_outside_lookahead": movies_future_outside_lookahead,
            },
            "series": {"total": total_series},
            "episodes": {
                "total": total_episodes,
                "placeholders": episodes_with_placeholder,
                "downloaded": episodes_with_file,
                "future_outside_lookahead": episodes_future_outside_lookahead,
            },
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


@router.get("/api/library")
async def library(limit: int = Query(300, ge=1, le=1000)):
    """Return mixed movie/series library rows with poster and placeholder stats."""
    session = get_session()
    try:
        series_episode_counts = {
            row.series_id: {
                "episode_total": int(row.episode_total or 0),
                "episode_files": int(row.episode_files or 0),
                "episode_placeholders": int(row.episode_placeholders or 0),
            }
            for row in session.query(
                Season.series_id.label("series_id"),
                func.count(Episode.id).label("episode_total"),
                func.sum(case((Episode.has_file == True, 1), else_=0)).label("episode_files"),
                func.sum(case((Episode.has_placeholder == True, 1), else_=0)).label("episode_placeholders"),
            )
            .join(Episode, Episode.season_id == Season.id)
            .group_by(Season.series_id)
            .all()
        }

        items: list[dict] = []

        movies = (
            session.query(Movie)
            .filter(Movie.is_deleted == False)
            .order_by(Movie.updated_at.desc(), Movie.title.asc())
            .limit(limit)
            .all()
        )
        for movie in movies:
            arr_link = _arr_item_link(
                item_type="movie",
                instance_key=movie.instance_key,
                is_4k=bool(movie.is_4k),
                title=movie.title,
                payload=movie.radarr_payload_raw if isinstance(movie.radarr_payload_raw, dict) else None,
            )
            items.append(
                {
                    "id": f"movie-{movie.id}",
                    "item_id": movie.id,
                    "type": "movie",
                    "title": movie.title,
                    "year": movie.year,
                    "poster_url": movie.remote_poster,
                    "backdrop_url": movie.remote_fanart,
                    "is_4k": bool(movie.is_4k),
                    "instance_key": movie.instance_key,
                    "arr_link": arr_link,
                    "determination": movie.determination,
                    "status": movie.status,
                    "has_file": bool(movie.has_file),
                    "has_placeholder": bool(movie.has_placeholder),
                    "overview": movie.radarr_overview,
                    "stats": {
                        "downloaded": 1 if movie.has_file else 0,
                        "placeholders": 1 if movie.has_placeholder else 0,
                    },
                }
            )

        series_rows = (
            session.query(Series)
            .filter(Series.is_deleted == False)
            .order_by(Series.updated_at.desc(), Series.title.asc())
            .limit(limit)
            .all()
        )
        for series in series_rows:
            counts = series_episode_counts.get(
                series.id,
                {"episode_total": 0, "episode_files": 0, "episode_placeholders": 0},
            )
            arr_link = _arr_item_link(
                item_type="series",
                instance_key=series.instance_key,
                is_4k=bool(series.is_4k),
                title=series.title,
                payload=series.sonarr_payload_raw if isinstance(series.sonarr_payload_raw, dict) else None,
            )
            items.append(
                {
                    "id": f"series-{series.id}",
                    "item_id": series.id,
                    "type": "series",
                    "title": series.title,
                    "year": series.year,
                    "poster_url": series.remote_poster,
                    "backdrop_url": series.remote_fanart or series.remote_banner,
                    "is_4k": bool(series.is_4k),
                    "instance_key": series.instance_key,
                    "arr_link": arr_link,
                    "determination": None,
                    "status": series.status,
                    "has_file": bool(series.has_files),
                    "has_placeholder": counts["episode_placeholders"] > 0,
                    "overview": series.sonarr_series_overview,
                    "stats": counts,
                }
            )

        items.sort(
            key=lambda item: (
                0 if item.get("has_placeholder") else 1,
                0 if item.get("type") == "movie" else 1,
                str(item.get("title") or "").lower(),
            )
        )

        return {"items": items[:limit], "count": min(len(items), limit)}
    finally:
        session.close()


@router.get("/api/settings/status")
async def settings_status():
    return JSONResponse(content=get_onboarding_status())


@router.get("/api/settings/current")
async def settings_current():
    return JSONResponse(content=get_settings_payload())


@router.post("/api/settings/save")
async def settings_save(request: Request):
    payload = await request.json()
    values = payload.get("values") if isinstance(payload, dict) else None
    if not isinstance(values, dict):
        return JSONResponse(content={"ok": False, "errors": {"values": "expected an object"}}, status_code=400)

    result = save_settings(values)
    status_code = 200 if result.get("ok") else 400
    return JSONResponse(content=result, status_code=status_code)


@router.post("/api/integrations/test")
async def integrations_test(request: Request):
    payload = await request.json()
    if not isinstance(payload, dict):
        return JSONResponse(content={"ok": False, "message": "expected request object"}, status_code=400)

    service = str(payload.get("service") or "").strip().lower()
    url = str(payload.get("url") or "").strip()
    credential = str(payload.get("credential") or "").strip()

    if service not in {"plex", "jellyfin", "emby", "radarr", "sonarr"}:
        return JSONResponse(content={"ok": False, "message": "unsupported service"}, status_code=400)
    if not url:
        return JSONResponse(content={"ok": False, "message": "url is required"}, status_code=400)
    if not credential:
        return JSONResponse(content={"ok": False, "message": "credential is required"}, status_code=400)

    result = test_integration_connection(service=service, url=url, token_or_key=credential)
    status_code = 200 if result.get("ok") else 400
    return JSONResponse(content=result, status_code=status_code)


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
