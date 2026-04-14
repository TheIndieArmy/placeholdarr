"""Dashboard routes: lightweight UI showing stats, activity, errors, and live logs."""

import ast
import calendar as _calendar
from collections import defaultdict
import os
import glob as _glob
import re
import threading
import unicodedata
from datetime import date, datetime, timezone, timedelta
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import and_, case, func, or_, text

from core.config import settings
from core.logger import logger
from services.app_config import get_onboarding_status, get_settings_payload, reset_onboarding, save_settings
from services.activity_snapshot import get_queue_download_activity_row
from services.integrations import test_integration_connection
from services.postgres.db import get_session
from services.postgres.models import (
    Movie, Series, Season, Episode, Placeholder, Job, EventLog,
)
from services.source_of_truth.calendar_phase import _compute_calendar_decision, _release_type_label

router = APIRouter()


def _launch_post_onboarding_startup_sync() -> None:
    def _runner() -> None:
        from services.startup_gate import startup_sync_complete
        try:
            from main import start_runtime_background_services
            from services.source_of_truth.startup import run_startup_source_of_truth

            start_runtime_background_services(reason='post_onboarding_completion')

            logger.info(
                "Launching first-run startup sync after onboarding completion",
                extra={"emoji_type": "gear"},
            )
            result = run_startup_source_of_truth()
            logger.info(
                "Post-onboarding startup sync completed"
                f" mode={result.get('startup_sync_mode')}"
                f" run_ids={result.get('run_ids') or []}",
                extra={"emoji_type": "success"},
            )
        except Exception as exc:
            logger.error(f"Post-onboarding startup sync failed: {exc}", extra={"emoji_type": "error"})
        finally:
            startup_sync_complete.set()

    threading.Thread(target=_runner, name="post-onboarding-startup-sync", daemon=True).start()


def _slugify_title(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    lowered = normalized.lower()
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered)
    lowered = re.sub(r"-{2,}", "-", lowered)
    return lowered.strip("-")


def _arr_base_url(item_type: str, instance_key: str, instance_id: str | None = None) -> str:
    def _normalize_ui_base(url: str) -> str:
        normalized = str(url or "").rstrip("/")
        normalized = re.sub(r"/api(?:/v\d+)?$", "", normalized, flags=re.IGNORECASE)
        return normalized.rstrip("/")

    key = str(instance_key or "").strip().lower()
    target_type = "radarr" if item_type == "movie" else "sonarr"

    for item in (getattr(settings, "configured_arr_instances", []) or []):
        item_key = str(item.get("instance_key") or "").strip().lower()
        item_type_key = str(item.get("arr_type") or "").strip().lower()
        if key and item_key == key and item_type_key == target_type:
            return _normalize_ui_base(str(item.get("url") or ""))

    base_url, _api_key = settings.resolve_arr_endpoint(target_type, instance_id=instance_id, instance_key=instance_key)
    return _normalize_ui_base(base_url)


def _arr_instance_maps() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_key: dict[str, dict[str, Any]] = {}
    by_id: dict[str, dict[str, Any]] = {}
    for item in (getattr(settings, "configured_arr_instances", []) or []):
        key = str(item.get("instance_key") or "").strip().lower()
        instance_id = str(item.get("instance_id") or "").strip().lower()
        entry = {
            "instance_key": key,
            "instance_id": instance_id,
            "label": str(item.get("label") or key or instance_id).strip() or key or instance_id,
            "arr_type": str(item.get("arr_type") or "").strip().lower(),
            "role": str(item.get("role") or "").strip().lower(),
            "url": str(item.get("url") or "").strip(),
            "api_key": str(item.get("api_key") or "").strip(),
        }
        if key:
            by_key[key] = entry
        if instance_id:
            by_id[instance_id] = entry
    return by_key, by_id


def _arr_instance_meta(instance_key: str | None = None, instance_id: str | None = None) -> dict[str, Any]:
    by_key, by_id = _arr_instance_maps()
    identity = str(instance_id or "").strip().lower()
    if identity and identity in by_id:
        return by_id[identity]
    key = str(instance_key or "").strip().lower()
    if key and key in by_key:
        return by_key[key]
    return {
        "instance_key": key,
        "instance_id": identity,
        "label": key or identity,
        "arr_type": "",
        "role": "",
        "url": "",
        "api_key": "",
    }


def _legacy_is_4k(instance_meta: dict[str, Any]) -> bool:
    return str(instance_meta.get("role") or "").strip().lower() != "primary"


def _arr_endpoint_fingerprint() -> dict[str, tuple[str, str]]:
    fingerprint: dict[str, tuple[str, str]] = {}
    for item in (getattr(settings, "configured_arr_instances", []) or []):
        instance_id = str(item.get("instance_id") or "").strip().lower()
        key = str(item.get("instance_key") or "").strip().lower()
        identity = instance_id or key
        if not identity:
            continue
        url = str(item.get("url") or "").strip().rstrip("/")
        api_key = str(item.get("api_key") or "").strip()
        fingerprint[identity] = (url, api_key)
    return fingerprint


def _launch_arr_change_full_sync(reason: str) -> None:
    def _runner() -> None:
        try:
            from services.source_of_truth.sync_runner import run_full_sync

            logger.info(
                f"Launching ARR-change startup sync reason={reason}",
                extra={"emoji_type": "gear"},
            )
            run_count = 0
            for item in (getattr(settings, "configured_arr_instances", []) or []):
                arr_type = str(item.get("arr_type") or "").strip().lower()
                if arr_type not in {"radarr", "sonarr"}:
                    continue
                run_full_sync(
                    dry_run=False,
                    batch_size=50,
                    types=("movie",) if arr_type == "radarr" else ("series",),
                    instance_key=str(item.get("instance_key") or "").strip().lower() or None,
                )
                run_count += 1
            logger.info(
                f"ARR-change full sync completed runs={run_count}",
                extra={"emoji_type": "success"},
            )
        except Exception as exc:
            logger.error(f"ARR-change startup sync failed: {exc}", extra={"emoji_type": "error"})

    threading.Thread(target=_runner, name="arr-change-startup-sync", daemon=True).start()


def _title_slug_from_payload(payload: dict | None, title: str) -> str:
    if isinstance(payload, dict):
        slug = str(payload.get("titleSlug") or "").strip()
        if slug:
            return slug
    return _slugify_title(title)


def _arr_item_link(item_type: str, instance_key: str, instance_id: str | None, title: str, payload: dict | None) -> str | None:
    base_url = _arr_base_url(item_type=item_type, instance_key=instance_key, instance_id=instance_id)
    if not base_url:
        return None
    slug = _title_slug_from_payload(payload=payload, title=title)
    if not slug:
        return None
    route = "movie" if item_type == "movie" else "series"
    return f"{base_url}/{route}/{slug}"


def _parse_calendar_month(month_token: str | None) -> date:
    raw = str(month_token or "").strip()
    if not raw:
        today = datetime.now().date()
        return today.replace(day=1)

    try:
        parsed = datetime.strptime(raw, "%Y-%m").date()
    except ValueError:
        raise ValueError("month must be in YYYY-MM format")
    return parsed.replace(day=1)


def _format_calendar_month(month_start: date) -> str:
    return month_start.strftime("%Y-%m")


def _calendar_nav_month(month_start: date, offset: int) -> date:
    year = month_start.year + ((month_start.month - 1 + offset) // 12)
    month = ((month_start.month - 1 + offset) % 12) + 1
    return date(year, month, 1)


def _calendar_window_mode(lookahead_days: int) -> str:
    if lookahead_days == 0:
        return "disabled"
    if lookahead_days < 0:
        return "infinite"
    return "bounded"


def _date_in_lookahead_window(target_date: date | None, today: date, lookahead_days: int) -> bool:
    if not target_date or lookahead_days == 0:
        return False
    if target_date < today:
        return False
    if lookahead_days < 0:
        return True
    return target_date <= (today + timedelta(days=lookahead_days))


def _calendar_grid(month_start: date) -> tuple[list[list[date]], date, date]:
    month_days = _calendar.Calendar(firstweekday=6).monthdatescalendar(month_start.year, month_start.month)
    return month_days, month_days[0][0], month_days[-1][-1]


def _movie_calendar_release(movie: Movie) -> tuple[date | None, str | None, bool]:
    preferred = str(getattr(settings, "PREFERRED_MOVIE_DATE_TYPE", "inCinemas") or "inCinemas").strip()
    candidates = {
        "inCinemas": getattr(movie, "theater_release_date", None),
        "digitalRelease": getattr(movie, "digital_release_date", None),
        "physicalRelease": getattr(movie, "physical_release_date", None),
    }
    order = [preferred] + [release_type for release_type in ("inCinemas", "digitalRelease", "physicalRelease") if release_type != preferred]
    for release_type in order:
        target_date = candidates.get(release_type)
        if target_date:
            return target_date, release_type, release_type == preferred
    return None, preferred, True


def _episode_calendar_episode_code(season_number: int | None, episode_number: int | None) -> str:
    if season_number is None or episode_number is None:
        return ""
    return f"S{int(season_number):02d}E{int(episode_number):02d}"


def _merge_calendar_episodes_same_day(items: list[dict]) -> list[dict]:
    """One calendar row per series per day when multiple episodes share an air date."""

    movies = [i for i in items if i.get("media_type") == "movie"]
    episodes = [i for i in items if i.get("media_type") == "episode"]
    by_series: dict[int, list[dict]] = defaultdict(list)
    loose: list[dict] = []
    for ep in episodes:
        sid = ep.get("series_id")
        if sid is None:
            loose.append(ep)
        else:
            by_series[int(sid)].append(ep)

    out_episodes: list[dict] = []
    for sid in sorted(by_series.keys()):
        group = by_series[sid]
        group.sort(
            key=lambda x: (
                int(x.get("season_number") if x.get("season_number") is not None else -1),
                int(x.get("episode_number") if x.get("episode_number") is not None else -1),
            )
        )
        if len(group) == 1:
            out_episodes.append(group[0])
            continue
        first = group[0]
        release_date = str(first.get("release_date") or "")
        code = _episode_calendar_episode_code(first.get("season_number"), first.get("episode_number"))
        if not code:
            code = str(first.get("subtitle") or "").split(" (+", 1)[0].strip() or "TV"
        extra = len(group) - 1
        subtitle = f"{code} (+{extra})" if extra else code
        merged = {
            **first,
            "id": f"episode-group-{sid}-{release_date}",
            "item_id": int(first["item_id"]),
            "subtitle": subtitle,
            "group_episode_ids": [int(x["item_id"]) for x in group],
            "group_episode_count": len(group),
        }
        out_episodes.append(merged)

    out_episodes.extend(loose)
    combined = movies + out_episodes
    combined.sort(
        key=lambda item: (
            0 if item.get("media_type") == "movie" else 1,
            str(item.get("title") or "").lower(),
        )
    )
    return combined


def _calendar_lookahead_payload(today: date, lookahead_days: int) -> dict:
    mode = _calendar_window_mode(lookahead_days)
    end_date = None
    label = "Lookahead disabled"
    if mode == "infinite":
        label = "Lookahead covers all future dates"
    elif mode == "bounded":
        end_date = today + timedelta(days=lookahead_days)
        label = f"Lookahead covers today through {end_date.isoformat()}"

    return {
        "days": lookahead_days,
        "mode": mode,
        "start_date": today.isoformat(),
        "end_date": end_date.isoformat() if end_date else None,
        "label": label,
    }


def _iso(value) -> str | None:
    """Return an ISO-format date/datetime string regardless of whether value is
    already a str or a date/datetime object.  Returns None for falsy input."""
    if not value:
        return None
    if isinstance(value, str):
        return value
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)

# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

def _dashboard_dist_index_path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "frontend", "dist", "index.html")


def _dashboard_not_built_response() -> PlainTextResponse:
    return PlainTextResponse(
        "dashboard-next is not built yet. Run: cd frontend && npm install && npm run build",
        status_code=503,
    )


def _serve_dashboard_index() -> FileResponse | PlainTextResponse:
    index_path = _dashboard_dist_index_path()
    if not os.path.isfile(index_path):
        return _dashboard_not_built_response()
    return FileResponse(index_path, media_type="text/html")


@router.get("/", response_class=HTMLResponse)
async def dashboard_page():
    """Serve the React + TypeScript dashboard at the app root."""
    return _serve_dashboard_index()


@router.get("/activity", response_class=HTMLResponse)
async def dashboard_activity_page():
    return _serve_dashboard_index()


@router.get("/library", response_class=HTMLResponse)
async def dashboard_library_page():
    return _serve_dashboard_index()


@router.get("/library/{path:path}", response_class=HTMLResponse)
async def dashboard_library_path_page(path: str):
    return _serve_dashboard_index()


@router.get("/calendar", response_class=HTMLResponse)
async def dashboard_calendar_page():
    return _serve_dashboard_index()


@router.get("/errors", response_class=HTMLResponse)
async def dashboard_errors_page():
    return _serve_dashboard_index()


@router.get("/logs", response_class=HTMLResponse)
async def dashboard_logs_page():
    return _serve_dashboard_index()


@router.get("/settings", response_class=HTMLResponse)
async def dashboard_settings_page():
    return _serve_dashboard_index()


@router.get("/dashboard-next", response_class=HTMLResponse)
async def dashboard_next_page():
    """Back-compat route that now points to root dashboard."""
    return RedirectResponse(url="/", status_code=307)


@router.get("/dashboard-next/{path:path}", response_class=HTMLResponse)
async def dashboard_next_path(path: str):
    """Back-compat deep-link route redirecting to root-based SPA paths."""
    target = f"/{path.lstrip('/')}" if path else "/"
    return RedirectResponse(url=target, status_code=307)


@router.get("/assets/{asset_path:path}")
async def dashboard_next_assets(asset_path: str):
    """Serve frontend build assets for the React dashboard."""
    dist_assets_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist", "assets")
    safe_path = os.path.normpath(asset_path).lstrip("/")
    if safe_path.startswith(".."):
        return JSONResponse({"ok": False, "message": "invalid asset path"}, status_code=400)

    file_path = os.path.join(dist_assets_dir, safe_path)
    if not os.path.isfile(file_path):
        return JSONResponse({"ok": False, "message": "asset not found"}, status_code=404)
    return FileResponse(file_path)


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


def _humanize_job_type(job_type: str) -> str:
    """Convert internal job type names to user-friendly display names."""
    mapping = {
        "full_sync": "Full Library Sync",
        "lite_sync": "Quick Sync",
        "placeholder_sweep": "Placeholder Maintenance",
        "fs_scan": "Filesystem Scan",
        "calendar_date_refresh": "Calendar Update",
        "enrich_series": "Series Enrichment",
        "determination": "Status Analysis",
        "status_reconcile": "Status Reconciliation",
        "materialization": "Placeholder Creation",
        "webhook_event": "Webhook Event",
        "nfo_refresh": "Metadata Refresh",
        "import_grace": "Import Grace Check",
        "playback_fallback": "Playback Fallback",
    }
    normalized = str(job_type or "").strip().lower()
    return mapping.get(normalized, _humanize_snake_case(normalized))


def _humanize_snake_case(value: str | None) -> str:
    token = str(value or "").strip().replace("-", "_")
    if not token:
        return "Unknown"
    return " ".join(part.capitalize() for part in token.split("_") if part)


def _activity_details_for_event(event_type: str | None, payload: Any) -> str | None:
    """One-line context (title / series / episode) for the activity table."""
    if not isinstance(payload, dict):
        return None
    et = str(event_type or "").strip().lower()
    movie = payload.get("movie") if isinstance(payload.get("movie"), dict) else {}
    series = payload.get("series") if isinstance(payload.get("series"), dict) else {}
    episode = payload.get("episode") if isinstance(payload.get("episode"), dict) else {}

    def fmt_movie(m: dict[str, Any]) -> str | None:
        t = str(m.get("title") or m.get("name") or "").strip()
        if not t:
            return None
        y = m.get("year")
        try:
            y_int = int(y) if y is not None else None
        except Exception:
            y_int = None
        return f"{t} ({y_int})" if y_int else t

    def fmt_series(s: dict[str, Any]) -> str | None:
        t = str(s.get("title") or "").strip()
        if not t:
            return None
        y = s.get("year")
        try:
            y_int = int(y) if y is not None else None
        except Exception:
            y_int = None
        return f"{t} ({y_int})" if y_int else t

    def fmt_episode(e: dict[str, Any], s: dict[str, Any]) -> str | None:
        st = fmt_series(s) if s else None
        try:
            sn = int(e.get("seasonNumber", e.get("season_number", 0)) or 0)
        except Exception:
            sn = 0
        try:
            en = int(e.get("episodeNumber", e.get("episode_number", 0)) or 0)
        except Exception:
            en = 0
        et = str(e.get("title") or "").strip() or "Episode"
        seg = f"S{sn:02d}E{en:02d} — {et}"
        return f"{st} • {seg}" if st else seg

    if et in {"movie_imported", "movie_added", "movieadd", "movie_deleted", "movie_file_deleted"}:
        return fmt_movie(movie)
    if et in {"series_add", "series_added", "seriesadd", "series_deleted"}:
        return fmt_series(series)
    if et in {"episode_imported", "episode_file_deleted"}:
        return fmt_episode(episode, series)
    return None


def _humanize_event_type(event_type: str | None) -> str:
    mapping = {
        "series_add": "Series Added",
        "seriesadd": "Series Added",
        "movie_added": "Movie Added",
        "movie_imported": "Movie Imported",
        "episode_imported": "Episode Imported",
        "movie_file_deleted": "Movie File Removed",
        "episode_file_deleted": "Episode File Removed",
        "movie_deleted": "Movie Removed",
        "series_deleted": "Series Removed",
        "playback_start": "Playback Started",
        "playback_stop": "Playback Stopped",
    }
    normalized = str(event_type or "").strip().lower()
    return mapping.get(normalized, _humanize_snake_case(normalized))


def _humanize_placeholder_status(status: str | None) -> str:
    mapping = {
        "created": "Created",
        "active": "Active",
        "activated": "Activated",
        "deleted": "Deleted",
        "missing": "Missing",
        "replaced": "Replaced",
        "obsolete": "Obsolete",
        "pending_observation": "Awaiting Confirmation",
        "resolved": "Resolved",
        "unresolved": "Unresolved",
    }
    normalized = str(status or "").strip().lower()
    if not normalized:
        return "Unknown"
    return mapping.get(normalized, _humanize_snake_case(normalized))


def _humanize_placeholder_reason(reason: str | None, *, action: str | None = None) -> str:
    mapping = {
        "file_acquired": "Real file acquired",
        "no_longer_needed": "No longer needed",
        "manual_removal": "Manually removed",
        "replaced_by_file": "Replaced by file",
        "cleanup": "System cleanup",
        "obsolete": "Marked obsolete",
        "fs_deleted": "File removed from disk",
        "media_server_deleted": "Removed from media server",
        "placeholder_requested": "Placeholder requested",
        "import_grace": "Import grace period",
    }
    normalized = str(reason or "").strip().lower()
    if normalized:
        return mapping.get(normalized, _humanize_snake_case(normalized))
    return "Placeholder removed" if action == "Deleted" else "Placeholder created"


def _is_user_relevant_event(event_type: str) -> bool:
    """Return True if event is user-relevant; hide noisy internal events."""
    hidden_types = {
        "queue_monitor",
        "api_connectivity",
        "heartbeat",
        "health_check",
        "polling",
        "internal_state_update",
        "cache_refresh",
        "db_check",
        "worker_ping",
        "unknown",
        "playback_start",
        "playback_stop",
    }
    return str(event_type or "").strip().lower() not in hidden_types


def _is_user_relevant_job(job_type: str, status: str | None = None) -> bool:
    token = str(job_type or "").strip().lower()
    if str(status or "").upper() == "FAILED":
        return True
    noisy = {
        "webhook_event",
        "nfo_refresh",
        "determination",
        "materialization",
        "status_reconcile",
        "calendar_date_refresh",
        "import_grace",
        "playback_fallback",
        "observation_trail",
        "placeholder_observation_trail",
    }
    return token not in noisy


def _humanize_activity_reason(reason: str | None) -> str:
    mapping = {
        "library sync": "Library sync",
        "series added": "Series added",
        "movie added": "Movie added",
        "real file deleted": "Real file deleted",
        "media deleted": "Media deleted",
        "import completed": "Import completed",
        "event materialization": "Event processing",
        "materialization run": "Materialization",
    }
    text = str(reason or "").strip().lower()
    if not text:
        return "Unknown"
    return mapping.get(text, _humanize_snake_case(text))


def _infer_reason_from_recent_events(
    session,
    *,
    when_dt: datetime | None,
    movie: Movie | None,
    episode: Episode | None,
) -> str | None:
    anchor = _to_utc(when_dt)
    if not anchor:
        return None
    window_start = anchor - timedelta(minutes=45)
    window_end = anchor + timedelta(minutes=45)
    candidates = (
        session.query(EventLog.event_type, EventLog.payload, EventLog.created_at)
        .filter(EventLog.created_at >= window_start, EventLog.created_at <= window_end)
        .order_by(EventLog.created_at.desc())
        .limit(80)
        .all()
    )
    movie_arr_id = int(getattr(movie, "radarrid", 0) or 0) if movie else 0
    episode_arr_id = int(getattr(episode, "sonarrid", 0) or 0) if episode else 0
    for event_type, payload, _ in candidates:
        p = payload if isinstance(payload, dict) else {}
        normalized = str(event_type or "").strip().lower()
        if movie_arr_id:
            movie_obj = p.get("movie") if isinstance(p.get("movie"), dict) else {}
            payload_movie_id = movie_obj.get("id") or p.get("movieId") or p.get("movie_id")
            if payload_movie_id and int(payload_movie_id) == movie_arr_id:
                if normalized == "movie_file_deleted":
                    return "Real file deleted"
                if normalized in {"movie_added", "movie_imported"}:
                    return "Movie added"
        if episode_arr_id:
            episodes = p.get("episodes") if isinstance(p.get("episodes"), list) else []
            episode_match = False
            for ep in episodes:
                if not isinstance(ep, dict):
                    continue
                ep_id = ep.get("id") or ep.get("episodeId") or ep.get("episode_id")
                if ep_id and int(ep_id) == episode_arr_id:
                    episode_match = True
                    break
            if episode_match:
                if normalized == "episode_file_deleted":
                    return "Real file deleted"
                if normalized == "episode_imported":
                    return "Import completed"
                if normalized in {"series_added", "series_add", "seriesadd"}:
                    return "Series added"
    return None


def _to_utc(value: datetime | None) -> datetime | None:
    if not value:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _safe_window(start: datetime | None, end: datetime | None) -> tuple[datetime | None, datetime | None]:
    start_utc = _to_utc(start)
    end_utc = _to_utc(end)
    if start_utc and end_utc and end_utc < start_utc:
        return end_utc, start_utc
    return start_utc, end_utc


def _extract_series_ids_from_payload(payload: dict[str, Any]) -> tuple[int | None, int | None]:
    series_id_db = None
    series_id_arr = None
    for key in ("series_id", "seriesId"):
        raw = payload.get(key)
        if raw is not None:
            try:
                series_id_db = int(raw)
                break
            except Exception:
                pass
    series_obj = payload.get("series") if isinstance(payload.get("series"), dict) else {}
    for key in ("id", "series_id", "seriesId"):
        raw = series_obj.get(key)
        if raw is not None:
            try:
                series_id_arr = int(raw)
                break
            except Exception:
                pass
    if series_id_arr is None:
        for key in ("sonarr_series_id", "sonarrSeriesId"):
            raw = payload.get(key)
            if raw is not None:
                try:
                    series_id_arr = int(raw)
                    break
                except Exception:
                    pass
    return series_id_db, series_id_arr


def _extract_placeholder_ids(payload: dict[str, Any]) -> set[int]:
    raw_ids = payload.get("placeholder_ids") if isinstance(payload.get("placeholder_ids"), list) else []
    result: set[int] = set()
    for pid in raw_ids:
        try:
            result.add(int(pid))
        except Exception:
            continue
    return result


def _coerce_step_status(raw_status: str | None) -> str:
    token = str(raw_status or "").strip().upper()
    if token in {"FAILED", "ERROR"}:
        return "failed"
    if token in {"DONE", "SUCCESS", "COMPLETED"}:
        return "done"
    if token in {"CLAIMED", "WORKING", "RUNNING", "PENDING", "IN_PROGRESS"}:
        return "working"
    return "pending"


def _merge_step_status(current: str, incoming: str) -> str:
    if incoming == "failed" or current == "failed":
        return "failed"
    if incoming == "working" and current in {"pending", "done"}:
        return "working"
    if incoming == "done" and current == "pending":
        return "done"
    return current


def _build_series_add_summary_row(session, event_row: dict[str, Any]) -> dict[str, Any]:
    payload = event_row.get("payload") if isinstance(event_row.get("payload"), dict) else {}
    series_payload = payload.get("series") if isinstance(payload.get("series"), dict) else {}
    series_title = str(series_payload.get("title") or "Series").strip() or "Series"
    sonarr_series_id = series_payload.get("id")

    series_db_id = None
    if sonarr_series_id is not None:
        row = session.query(Series.id).filter(Series.sonarrid == sonarr_series_id).order_by(Series.updated_at.desc()).first()
        if row:
            series_db_id = row[0]

    event_time = _to_utc(event_row.get("created_at"))
    window_end = (event_time + timedelta(minutes=45)) if event_time else None

    created_episode_count = 0
    created_placeholder_ids: set[int] = set()
    if series_db_id is not None and event_time and window_end:
        created_rows = (
            session.query(Placeholder.id)
            .filter(
                Placeholder.series_id == int(series_db_id),
                Placeholder.created_at >= event_time,
                Placeholder.created_at <= window_end,
            )
            .all()
        )
        created_placeholder_ids = {int(row[0]) for row in created_rows}
        created_episode_count = len(created_placeholder_ids)

    step_status = {
        "materialization": "pending",
        "observation": "pending",
        "status_update": "pending",
    }
    step_labels = {
        "materialization": "Materialization",
        "observation": "Observation",
        "status_update": "Status update",
    }
    step_job_types = {
        "materialization": {"materialization"},
        "observation": {"placeholder_observation_trail", "observation_trail"},
        "status_update": {"status_reconcile"},
    }

    matched_jobs = 0
    if event_time and window_end:
        related_jobs = (
            session.query(Job.job_type, Job.status, Job.payload, Job.created_at, Job.updated_at)
            .filter(
                or_(
                    and_(Job.created_at >= event_time, Job.created_at <= window_end),
                    and_(Job.updated_at >= event_time, Job.updated_at <= window_end),
                )
            )
            .all()
        )
        for job_type, job_status, job_payload, _, _ in related_jobs:
            payload = job_payload if isinstance(job_payload, dict) else {}
            payload_series_db_id, payload_series_arr_id = _extract_series_ids_from_payload(payload)
            payload_placeholders = _extract_placeholder_ids(payload)
            source = str(payload.get("source") or "").strip().lower()

            matches_series = False
            if series_db_id is not None and payload_series_db_id is not None and payload_series_db_id == int(series_db_id):
                matches_series = True
            if sonarr_series_id is not None and payload_series_arr_id is not None and int(payload_series_arr_id) == int(sonarr_series_id):
                matches_series = True
            if created_placeholder_ids and payload_placeholders and (payload_placeholders & created_placeholder_ids):
                matches_series = True
            if source.startswith("event_series_add"):
                matches_series = True

            if not matches_series:
                continue

            matched_jobs += 1
            normalized_job_type = str(job_type or "").strip().lower()
            normalized_status = _coerce_step_status(job_status)
            for step_key, accepted_job_types in step_job_types.items():
                if normalized_job_type in accepted_job_types:
                    step_status[step_key] = _merge_step_status(step_status[step_key], normalized_status)
                    break

    if created_episode_count > 0 and step_status["materialization"] == "pending":
        step_status["materialization"] = "done"
    if created_episode_count == 0 and step_status["materialization"] == "pending":
        step_status["materialization"] = "skipped"

    status_parts = []
    for step_key in ("materialization", "observation", "status_update"):
        token = step_status[step_key]
        label = step_labels[step_key]
        if token == "done":
            status_parts.append(f"{label}: Complete")
        elif token == "working":
            status_parts.append(f"{label}: Running")
        elif token == "failed":
            status_parts.append(f"{label}: Failed")
        elif token == "skipped":
            status_parts.append(f"{label}: Not needed")
        else:
            status_parts.append(f"{label}: Pending")

    event_status = str(event_row.get("status") or "").strip().upper()
    any_failed = any(state == "failed" for state in step_status.values())
    required_steps = ["observation", "status_update"]
    if created_episode_count > 0:
        required_steps.insert(0, "materialization")
    all_required_done = all(step_status[k] in {"done", "skipped"} for k in required_steps)

    final_status = "WORKING"
    if event_status == "FAILED" or any_failed:
        final_status = "FAILED"
    elif all_required_done:
        final_status = "DONE"
    elif event_status == "DONE" and matched_jobs == 0:
        final_status = "DONE"

    details = f"Placeholder(s) created: {created_episode_count} • " + " • ".join(status_parts)
    return {
        "id": event_row.get("id"),
        "type": "event",
        "event_type": event_row.get("event_type"),
        "display_name": f"Series Added: {series_title}",
        "source": event_row.get("source"),
        "status": final_status,
        "error": event_row.get("error_message"),
        "details": details,
        "time": event_row.get("created_at").isoformat() if event_row.get("created_at") else None,
    }


def _build_sync_placeholder_rows(session, sync_job: dict[str, Any]) -> list[dict[str, Any]]:
    start, end = _safe_window(sync_job.get("created_at"), sync_job.get("updated_at"))
    if not start or not end:
        return []

    created_movies = (
        session.query(Placeholder.id)
        .filter(
            Placeholder.movie_id.isnot(None),
            Placeholder.created_at >= start,
            Placeholder.created_at <= end,
        )
        .count()
    )
    created_episodes = (
        session.query(Placeholder.id)
        .filter(
            Placeholder.episode_id.isnot(None),
            Placeholder.created_at >= start,
            Placeholder.created_at <= end,
        )
        .count()
    )
    deleted_movies = (
        session.query(Placeholder.id)
        .filter(
            Placeholder.movie_id.isnot(None),
            Placeholder.has_placeholder == False,  # noqa: E712
            Placeholder.updated_at >= start,
            Placeholder.updated_at <= end,
        )
        .count()
    )
    deleted_episodes = (
        session.query(Placeholder.id)
        .filter(
            Placeholder.episode_id.isnot(None),
            Placeholder.has_placeholder == False,  # noqa: E712
            Placeholder.updated_at >= start,
            Placeholder.updated_at <= end,
        )
        .count()
    )

    sync_label = _humanize_job_type(str(sync_job.get("job_type") or "sync"))
    result: list[dict[str, Any]] = []
    created_total = created_movies + created_episodes
    if created_total > 0:
        result.append(
            {
                "id": sync_job.get("id"),
                "type": "job",
                "job_type": "sync_placeholder_created",
                "display_name": f"{sync_label}: Placeholders Created",
                "status": sync_job.get("status"),
                "error": sync_job.get("error_message"),
                "details": f"Movies {created_movies} • Episodes {created_episodes} • Total {created_total}",
                "time": end.isoformat(),
            }
        )

    deleted_total = deleted_movies + deleted_episodes
    if deleted_total > 0:
        result.append(
            {
                "id": sync_job.get("id"),
                "type": "job",
                "job_type": "sync_placeholder_deleted",
                "display_name": f"{sync_label}: Placeholders Deleted",
                "status": sync_job.get("status"),
                "error": sync_job.get("error_message"),
                "details": f"Movies {deleted_movies} • Episodes {deleted_episodes} • Total {deleted_total}",
                "time": end.isoformat(),
            }
        )
    return result


def _extract_log_timestamp(line: str) -> datetime | None:
    token = str(line or "")[:23]
    try:
        return datetime.strptime(token, "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _runtime_log_dir() -> str:
    explicit_file = str(getattr(settings, "LOG_FILE", "") or "").strip()
    if explicit_file:
        return os.path.dirname(explicit_file) or "."
    explicit_dir = str(getattr(settings, "LOG_DIR", "") or "").strip()
    if explicit_dir:
        return explicit_dir
    appdata = str(getattr(settings, "APPDATA_PATH", "/config") or "/config").strip() or "/config"
    return os.path.join(appdata, "logs")


def _latest_runtime_log_file() -> str | None:
    pattern = os.path.join(_runtime_log_dir(), "placeholdarr-*.log")
    log_files = _glob.glob(pattern)
    if not log_files:
        return None
    by_mtime = sorted(log_files, key=lambda p: os.path.getmtime(p), reverse=True)
    for path in by_mtime:
        try:
            if os.path.getsize(path) >= 2048:
                return path
        except Exception:
            continue
    return by_mtime[0]


def _parse_metrics_dict(fragment: str) -> dict[str, Any]:
    try:
        parsed = ast.literal_eval(fragment)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {}


def _latest_startup_sync_anchor(lines: list[str]) -> tuple[int | None, str]:
    mode_idx: int | None = None
    mode = "unknown"
    re_mode = re.compile(r"Startup sync mode selected:\s*(\w+)", re.IGNORECASE)
    for idx, raw in enumerate(lines):
        match = re_mode.search(str(raw or ""))
        if not match:
            continue
        mode_idx = idx
        mode = str(match.group(1) or "").strip().lower() or "unknown"
    return mode_idx, mode


def _build_startup_sync_progress_row() -> dict[str, Any] | None:
    log_file = _latest_runtime_log_file()
    if not log_file:
        return None

    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except Exception:
        return None

    if not lines:
        return None

    anchor_idx, startup_mode = _latest_startup_sync_anchor(lines)
    if anchor_idx is None:
        return None
    scoped_lines = lines[anchor_idx:]

    series_total = 0
    series_processed = 0
    episodes_discovered = 0
    movie_started = False
    series_started = False
    determination: dict[str, Any] = {}
    materialization: dict[str, Any] = {}
    last_observation: dict[str, Any] = {}
    observation_summary: dict[str, Any] = {}
    last_time: datetime | None = None

    re_series_fetch = re.compile(r"Series fullsync: fetched\s+(\d+)\s+series", re.IGNORECASE)
    re_series_progress = re.compile(r"Series fullsync progress:\s*(\d+)/(\d+)\s+series processed\s*\((\d+)\s+episodes", re.IGNORECASE)
    re_determination = re.compile(r"Determination phase complete:\s*(\{.*\})")
    re_materialization = re.compile(r"Materialization phase complete:\s*(\{.*\})")
    re_observation_poll = re.compile(
        r"Observation poll pass #(\d+):\s*start_unresolved=(\d+)\s+remaining_unresolved=(\d+)\s+"
        r"resolved_delta=(\d+)\s+progress_delta=(\d+).*?awaiting_plex_scan=(\d+).*?status_updates_plex=(\d+)",
        re.IGNORECASE,
    )
    re_observation_summary = re.compile(r"Observation summary:\s*ready=(\d+)/(\d+),\s*still_waiting=(\d+),\s*reason=([a-z_]+)", re.IGNORECASE)
    re_movie_refresh = re.compile(r"Full sync movie phase started; scheduled initial library refresh in 5 seconds", re.IGNORECASE)
    re_tv_refresh = re.compile(r"Full sync episode phase started; scheduled initial library refresh in 5 seconds", re.IGNORECASE)
    
    movie_refresh_triggered = False
    tv_refresh_triggered = False
    run_timestamp = ""
    lite_movies_requested = 0
    lite_movies_seen = 0
    lite_series_requested = 0
    lite_series_seen = 0
    lite_episodes_seen = 0
    startup_completed = False

    re_lite_movie = re.compile(r"Startup lite targeted movie sync .*: (\{.*\})", re.IGNORECASE)
    re_lite_series = re.compile(r"Startup lite targeted series sync .*: (\{.*\})", re.IGNORECASE)
    re_startup_completed = re.compile(r"Startup source-of-truth completed .*", re.IGNORECASE)

    for raw in scoped_lines:
        line = str(raw or "")
        ts = _extract_log_timestamp(line)
        if ts is not None:
            last_time = ts
            if "Startup sync mode selected:" in line:
                run_timestamp = ts.strftime("%Y%m%d%H%M%S")

        if "Starting startup movie fullsync" in line:
            movie_started = True
        if "Starting startup series fullsync" in line:
            series_started = True

        match = re_series_fetch.search(line)
        if match:
            series_total = max(series_total, int(match.group(1)))

        match = re_series_progress.search(line)
        if match:
            series_processed = max(series_processed, int(match.group(1)))
            series_total = max(series_total, int(match.group(2)))
            episodes_discovered = max(episodes_discovered, int(match.group(3)))

        match = re_determination.search(line)
        if match:
            determination = _parse_metrics_dict(match.group(1))

        match = re_materialization.search(line)
        if match:
            materialization = _parse_metrics_dict(match.group(1))

        match = re_observation_poll.search(line)
        if match:
            last_observation = {
                "pass": int(match.group(1)),
                "start_unresolved": int(match.group(2)),
                "remaining_unresolved": int(match.group(3)),
                "resolved_delta": int(match.group(4)),
                "progress_delta": int(match.group(5)),
                "awaiting_plex_scan": int(match.group(6)),
                "status_updates_plex": int(match.group(7)),
            }

        match = re_observation_summary.search(line)
        if match:
            observation_summary = {
                "ready": int(match.group(1)),
                "expected": int(match.group(2)),
                "still_waiting": int(match.group(3)),
                "reason": str(match.group(4) or ""),
            }

        if re_movie_refresh.search(line):
            movie_refresh_triggered = True
        if re_tv_refresh.search(line):
            tv_refresh_triggered = True
        match = re_lite_movie.search(line)
        if match:
            lite_metrics = _parse_metrics_dict(match.group(1))
            lite_movies_requested += int(lite_metrics.get("movies_requested", 0) or 0)
            lite_movies_seen += int(lite_metrics.get("movies_seen", 0) or 0)
        match = re_lite_series.search(line)
        if match:
            lite_metrics = _parse_metrics_dict(match.group(1))
            lite_series_requested += int(lite_metrics.get("series_requested", 0) or 0)
            lite_series_seen += int(lite_metrics.get("series_seen", 0) or 0)
            lite_episodes_seen += int(lite_metrics.get("episodes_seen", 0) or 0)
        if re_startup_completed.search(line):
            startup_completed = True

    if not any(
        [
            movie_started,
            series_started,
            determination,
            materialization,
            last_observation,
            observation_summary,
            lite_movies_seen,
            lite_series_seen,
            startup_completed,
        ]
    ):
        return None

    observation_running = startup_mode == "full" and bool(last_observation) and int(last_observation.get("remaining_unresolved", 0) or 0) > 0
    if observation_summary:
        reason = str(observation_summary.get("reason") or "").lower()
        if reason == "all_resolved" and int(observation_summary.get("still_waiting", 0) or 0) <= 0:
            observation_running = False

    overall_status = "WORKING" if observation_running else "DONE"
    if int(materialization.get("errors", 0) or 0) > 0:
        overall_status = "FAILED"
    if startup_completed and overall_status == "WORKING":
        overall_status = "DONE"

    materialization_known = bool(materialization)
    movie_refresh_effective = movie_refresh_triggered or bool(
        materialization.get("movie_refresh_triggered") if materialization else False
    )
    tv_refresh_effective = tv_refresh_triggered or bool(
        materialization.get("tv_refresh_triggered") if materialization else False
    )

    def _lite_library_refresh_pending_label() -> str:
        if not materialization_known:
            return "Checking…"
        return "Not required"

    sections: list[dict[str, Any]] = []
    if startup_mode == "lite":
        discovery_status = "done" if (lite_movies_seen > 0 or lite_series_seen > 0 or startup_completed) else "pending"
    else:
        discovery_status = "done" if (series_total > 0 or movie_started) else "pending"
    sections.append(
        {
            "name": "Discovery",
            "status": discovery_status,
            "metrics": [
                {
                    "label": "Movies discovered",
                    "value": int(lite_movies_seen if startup_mode == "lite" else int(determination.get("movies_total", 0) or 0)),
                },
                {
                    "label": "Series discovered",
                    "value": int(lite_series_seen if startup_mode == "lite" else int(series_total or 0)),
                },
                {
                    "label": "Episodes discovered",
                    "value": int(
                        lite_episodes_seen
                        if startup_mode == "lite"
                        else max(episodes_discovered, int(determination.get("episodes_total", 0) or 0))
                    ),
                },
                {
                    "label": "Series progress",
                    "value": (
                        f"{int(lite_series_seen)}/{int(lite_series_requested or lite_series_seen)}"
                        if startup_mode == "lite"
                        else (f"{int(series_processed)}/{int(series_total)}" if series_total else str(int(series_processed)))
                    ),
                },
            ],
        }
    )

    determination_status = "done" if bool(determination) else "pending"
    sections.append(
        {
            "name": "Determination",
            "status": determination_status,
            "metrics": [
                {"label": "Needs placeholder", "value": int(determination.get("needs_placeholder", 0) or 0)},
                {"label": "Already had placeholder", "value": int(determination.get("placeholder_exists", 0) or 0)},
                {"label": "Not needed", "value": int(determination.get("not_needed", 0) or 0)},
            ],
        }
    )

    materialization_status = "done" if bool(materialization) and overall_status != "WORKING" else ("working" if bool(materialization) else "pending")
    if int(materialization.get("errors", 0) or 0) > 0:
        materialization_status = "failed"
    sections.append(
        {
            "name": "Materialization",
            "status": materialization_status,
            "metrics": [
                {"label": "Created", "value": int(materialization.get("created", 0) or 0)},
                {"label": "Files created", "value": int(materialization.get("files_created", 0) or 0)},
                {"label": "NFO written", "value": int(materialization.get("nfo_written", 0) or 0)},
                {
                    "label": "Movie Library Refresh",
                    "value": (
                        "✅"
                        if movie_refresh_effective
                        else (_lite_library_refresh_pending_label() if startup_mode == "lite" else "Pending")
                    ),
                },
                {
                    "label": "TV Library Refresh",
                    "value": (
                        "✅"
                        if tv_refresh_effective
                        else (_lite_library_refresh_pending_label() if startup_mode == "lite" else "Pending")
                    ),
                },
            ],
        }
    )


    movie_refresh_summary = (
        "✅"
        if movie_refresh_effective
        else (_lite_library_refresh_pending_label() if startup_mode == "lite" else "Pending")
    )
    tv_refresh_summary = (
        "✅"
        if tv_refresh_effective
        else (_lite_library_refresh_pending_label() if startup_mode == "lite" else "Pending")
    )
    summary_details = (
        f"Materialization created {int(materialization.get('created', 0) or 0)} placeholders • "
        f"Movies: {movie_refresh_summary} • "
        f"TV: {tv_refresh_summary}"
    )

    row_id_prefix = "lite-sync-progress" if startup_mode == "lite" else "full-sync-progress"
    row_id = f"{row_id_prefix}-{run_timestamp}" if run_timestamp else row_id_prefix

    log_mtime_dt: datetime | None = None
    try:
        log_mtime_dt = datetime.fromtimestamp(os.path.getmtime(log_file), tz=timezone.utc)
    except Exception:
        log_mtime_dt = None

    display_times = [t for t in (last_time, log_mtime_dt) if isinstance(t, datetime)]
    display_time = max(display_times) if display_times else log_mtime_dt

    return {
        "id": row_id,
        "type": "job",
        "job_type": "lite_sync_progress" if startup_mode == "lite" else "full_sync_progress",
        "display_name": "Lite Sync Progress" if startup_mode == "lite" else "Full Sync Progress",
        "status": overall_status,
        "details": summary_details,
        "time": (display_time.isoformat() if display_time else None),
        "progress": {
            "running": bool(observation_running),
            "sections": sections,
            "log_file": os.path.basename(log_file),
        },
    }


def _build_calendar_sync_row() -> dict[str, Any] | None:
    log_file = _latest_runtime_log_file()
    if not log_file:
        return None
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except Exception:
        return None

    pattern = re.compile(r"Calendar date refresh complete:\s*(\{.*\})")
    latest: dict[str, Any] | None = None
    latest_ts: datetime | None = None
    for raw in lines:
        line = str(raw or "")
        match = pattern.search(line)
        if not match:
            continue
        parsed = _parse_metrics_dict(match.group(1))
        if not parsed:
            continue
        latest = parsed
        latest_ts = _extract_log_timestamp(line)
    if not latest:
        return None

    status = "FAILED" if int(latest.get("errors", 0) or 0) > 0 else "DONE"
    return {
        "id": f"calendar-sync-{latest_ts.isoformat() if latest_ts else 'latest'}",
        "type": "job",
        "job_type": "calendar_sync_progress",
        "display_name": "Calendar Sync",
        "status": status,
        "details": (
            f"Window {latest.get('start_date', '--')} to {latest.get('end_date', '--')} • "
            f"Movies updated {int(latest.get('movie_rows_updated', 0) or 0)} • "
            f"Episodes updated {int(latest.get('episode_rows_updated', 0) or 0)}"
        ),
        "time": latest_ts.isoformat() if latest_ts else None,
        "progress": {
            "running": False,
            "sections": [
                {
                    "name": "Calendar Window",
                    "status": "done" if status != "FAILED" else "failed",
                    "metrics": [
                        {"label": "Lookahead start", "value": str(latest.get("start_date") or "--")},
                        {"label": "Lookahead end", "value": str(latest.get("end_date") or "--")},
                    ],
                },
                {
                    "name": "Date Refresh Stats",
                    "status": "done" if status != "FAILED" else "failed",
                    "metrics": [
                        {"label": "Movie rows seen", "value": int(latest.get("movie_rows_seen", 0) or 0)},
                        {"label": "Movie rows updated", "value": int(latest.get("movie_rows_updated", 0) or 0)},
                        {"label": "Episode rows seen", "value": int(latest.get("episode_rows_seen", 0) or 0)},
                        {"label": "Episode rows updated", "value": int(latest.get("episode_rows_updated", 0) or 0)},
                        {"label": "Errors", "value": int(latest.get("errors", 0) or 0)},
                    ],
                },
            ],
            "log_file": os.path.basename(log_file),
        },
    }


@router.get("/api/activity")
async def activity(limit: int = Query(50, ge=1, le=200)):
    """Return recent user-relevant activity: syncs, errors, and meaningful jobs."""
    session = get_session()
    try:
        # Recent events (filtered for user relevance)
        events = session.query(
            EventLog.id,
            EventLog.event_type,
            EventLog.source,
            EventLog.status,
            EventLog.created_at,
            EventLog.error_message,
            EventLog.payload,
        ).order_by(EventLog.created_at.desc()).limit(limit * 2).all()  # Over-fetch to account for filtering
        event_list = []
        grouped_event_types = {
            "movie_added",
            "movie_imported",
            "episode_imported",
            "movie_file_deleted",
            "episode_file_deleted",
        }
        grouped_counters: dict[str, dict[str, Any]] = {}
        for e in events:
            event_type = str(e.event_type or "").strip().lower()
            if not _is_user_relevant_event(event_type):
                continue
            if event_type in grouped_event_types:
                entry = grouped_counters.setdefault(
                    event_type,
                    {
                        "count": 0,
                        "latest": e.created_at,
                        "failed": 0,
                    },
                )
                entry["count"] = int(entry.get("count") or 0) + 1
                if str(e.status or "").upper() == "FAILED":
                    entry["failed"] = int(entry.get("failed") or 0) + 1
                latest = entry.get("latest")
                if e.created_at and (not latest or e.created_at > latest):
                    entry["latest"] = e.created_at
                continue
            row_data = {
                "id": e.id,
                "event_type": e.event_type,
                "source": e.source,
                "status": e.status,
                "created_at": e.created_at,
                "error_message": e.error_message,
                "payload": e.payload,
            }
            if event_type in {"series_added", "series_add", "seriesadd"}:
                event_list.append(_build_series_add_summary_row(session, row_data))
                continue
            status_text = str(e.status or "")
            display_name = _humanize_event_type(e.event_type)
            if status_text.upper() == "FAILED":
                display_name = f"[Failed] {display_name}"
            payload_dict = e.payload if isinstance(e.payload, dict) else {}
            detail_line = _activity_details_for_event(e.event_type, payload_dict)
            event_list.append(
                {
                    "id": e.id,
                    "type": "event",
                    "event_type": e.event_type,
                    "display_name": display_name,
                    "source": e.source,
                    "status": e.status,
                    "error": e.error_message,
                    "details": detail_line,
                    "time": e.created_at.isoformat() if e.created_at else None,
                }
            )

        for event_type, info in grouped_counters.items():
            count = int(info.get("count") or 0)
            failed = int(info.get("failed") or 0)
            latest = info.get("latest")
            display_name = _humanize_event_type(event_type)
            if failed > 0:
                display_name = f"[Failed] {display_name}"
            sample_payload = None
            for e in events:
                if str(e.event_type or "").strip().lower() != event_type:
                    continue
                if isinstance(e.payload, dict):
                    sample_payload = e.payload
                    break
            grouped_detail = _activity_details_for_event(event_type, sample_payload) if sample_payload else None
            group_details = f"Grouped {count} similar events • Failures {failed}"
            if grouped_detail:
                group_details = f"{grouped_detail} • {group_details}"
            event_list.append(
                {
                    "id": f"group:{event_type}",
                    "type": "event",
                    "event_type": event_type,
                    "display_name": f"{display_name} (x{count})",
                    "status": "FAILED" if failed > 0 else "DONE",
                    "details": group_details,
                    "time": latest.isoformat() if latest else None,
                }
            )

        # Always include a few recent series-added summaries, even if they are older
        # than the primary event window.
        series_events = (
            session.query(
                EventLog.id,
                EventLog.event_type,
                EventLog.source,
                EventLog.status,
                EventLog.created_at,
                EventLog.error_message,
                EventLog.payload,
            )
            .filter(EventLog.event_type.in_(["series_added", "series_add", "seriesadd"]))
            .order_by(EventLog.created_at.desc())
            .limit(10)
            .all()
        )
        for e in series_events:
            event_list.append(
                _build_series_add_summary_row(
                    session,
                    {
                        "id": e.id,
                        "event_type": e.event_type,
                        "source": e.source,
                        "status": e.status,
                        "created_at": e.created_at,
                        "error_message": e.error_message,
                        "payload": e.payload,
                    },
                )
            )

        # Recent jobs (all user-relevant by default)
        jobs = session.query(
            Job.id,
            Job.job_type,
            Job.status,
            Job.created_at,
            Job.updated_at,
            Job.error_message,
        ).order_by(Job.updated_at.desc()).limit(limit).all()

        job_list = []
        for j in jobs:
            normalized_job_type = str(j.job_type or "").strip().lower()
            if normalized_job_type in {"full_sync", "lite_sync"}:
                job_list.extend(
                    _build_sync_placeholder_rows(
                        session,
                        {
                            "id": j.id,
                            "job_type": j.job_type,
                            "status": j.status,
                            "created_at": j.created_at,
                            "updated_at": j.updated_at,
                            "error_message": j.error_message,
                        },
                    )
                )
            if not _is_user_relevant_job(normalized_job_type, j.status):
                continue
            display_name = _humanize_job_type(j.job_type)
            if str(j.status or "").upper() == "FAILED":
                display_name = f"[Failed] {display_name}"
            job_list.append(
                {
                    "id": j.id,
                    "type": "job",
                    "job_type": j.job_type,
                    "display_name": display_name,
                    "status": j.status,
                    "error": j.error_message,
                    "time": j.updated_at.isoformat() if j.updated_at else (j.created_at.isoformat() if j.created_at else None),
                }
            )

        # Ensure recent sync summaries are present even when sync jobs are outside
        # the latest generic job window.
        sync_jobs = (
            session.query(
                Job.id,
                Job.job_type,
                Job.status,
                Job.created_at,
                Job.updated_at,
                Job.error_message,
            )
            .filter(Job.job_type.in_(["full_sync", "lite_sync"]))
            .order_by(Job.updated_at.desc())
            .limit(10)
            .all()
        )
        for j in sync_jobs:
            job_list.extend(
                _build_sync_placeholder_rows(
                    session,
                    {
                        "id": j.id,
                        "job_type": j.job_type,
                        "status": j.status,
                        "created_at": j.created_at,
                        "updated_at": j.updated_at,
                        "error_message": j.error_message,
                    },
                )
            )

        sync_progress_row = _build_startup_sync_progress_row()
        calendar_sync_row = _build_calendar_sync_row()
        queue_activity_row = get_queue_download_activity_row()
        extra_rows = [r for r in (sync_progress_row, calendar_sync_row, queue_activity_row) if r]

        # Merge and sort by time descending: show recent + high-priority (failures)
        combined = sorted(event_list + job_list + extra_rows, key=lambda x: x.get("time") or "", reverse=True)
        unique = []
        seen_keys = set()
        for row in combined:
            key = (row.get("type"), row.get("id"), row.get("display_name"), row.get("time"))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            unique.append(row)
        
        # Prioritize failures: put any FAILED status near the top
        failed = [x for x in unique if x.get("status") == "FAILED"]
        rest = [x for x in unique if x.get("status") != "FAILED"]
        prioritized = failed + rest

        top = prioritized[:limit]
        top_keys = {(row.get("type"), row.get("id"), row.get("display_name"), row.get("time")) for row in top}
        grouped_candidates = [
            row
            for row in prioritized
            if (
                str(row.get("job_type") or "").startswith("sync_placeholder_")
                or str(row.get("display_name") or "").startswith("Series Added:")
            )
        ]
        missing_grouped = [
            row
            for row in grouped_candidates
            if (row.get("type"), row.get("id"), row.get("display_name"), row.get("time")) not in top_keys
        ]
        if missing_grouped and top:
            inject = missing_grouped[: min(3, len(top))]
            top = top[: max(0, len(top) - len(inject))] + inject
            top = sorted(top, key=lambda x: x.get("time") or "", reverse=True)

        top = top[:limit]

        return top
    finally:
        session.close()


@router.get("/api/activity/placeholders")
async def activity_placeholders(limit: int = Query(50, ge=1, le=200)):
    """Return placeholder timeline (created/deleted) with humanized context."""
    session = get_session()
    try:
        # Pull a larger window, then derive up to two timeline events per row.
        placeholders = session.query(Placeholder)\
         .order_by(Placeholder.updated_at.desc())\
         .limit(limit * 4)\
         .all()

        activity_list = []
        for ph in placeholders:
            # Resolve title context once for both create/delete events.
            movie = session.query(Movie).filter(Movie.id == ph.movie_id).first() if ph.movie_id else None
            episode = session.query(Episode).filter(Episode.id == ph.episode_id).first() if ph.episode_id else None
            series = session.query(Series).filter(Series.id == ph.series_id).first() if ph.series_id else None
            
            # If episode exists but series wasn't directly set, try to get it from episode
            if episode and not series and hasattr(episode, 'series_id') and episode.series_id:
                series = session.query(Series).filter(Series.id == episode.series_id).first()
            
            item_type = "movie" if ph.movie_id else "episode"
            if item_type == "movie":
                item_title = movie.title if movie else "Unknown Movie"
                series_title = None
            else:
                if episode:
                    season_num = getattr(episode, 'season_number', 0) or 0
                    ep_num = getattr(episode, 'episode_number', 0) or 0
                    ep_title = getattr(episode, 'title', 'Unknown') or 'Unknown'
                    item_title = f"S{season_num:02d}E{ep_num:02d} - {ep_title}"
                else:
                    item_title = "Unknown Episode"
                series_title = series.title if series else None

            created_at = ph.created_at
            updated_at = ph.updated_at or ph.created_at
            lifecycle = str(ph.lifecycle_status or "").strip().lower()
            file_exists_now = bool(getattr(ph, "path", None) and os.path.exists(ph.path))
            deleted_like = (not bool(ph.has_placeholder)) and (not file_exists_now) and lifecycle in {"deleted", "missing", "obsolete", "replaced", ""}

            extra_meta = ph.extra if isinstance(ph.extra, dict) else {}
            inferred_reason = _infer_reason_from_recent_events(
                session,
                when_dt=updated_at or created_at,
                movie=movie,
                episode=episode,
            )
            created_reason = _humanize_activity_reason(extra_meta.get("create_reason") or inferred_reason)
            deleted_reason = _humanize_activity_reason(extra_meta.get("delete_reason") or inferred_reason)

            if created_at:
                if created_reason == "Unknown":
                    created_reason = "Library materialization"
                activity_list.append({
                    "id": ph.id,
                    "type": "placeholder",
                    "action": "Created",
                    "item_type": item_type,
                    "item_title": item_title,
                    "series_title": series_title,
                    "path": ph.path,
                    "reason": created_reason,
                    "status": _humanize_placeholder_status(ph.lifecycle_status),
                    "time": created_at.isoformat(),
                })

            if deleted_like and updated_at:
                if deleted_reason == "Unknown":
                    if lifecycle in {"missing", "deleted", "obsolete", "replaced"}:
                        deleted_reason = "Library cleanup"
                    else:
                        deleted_reason = _humanize_placeholder_reason(lifecycle or "deleted", action="Deleted")
                activity_list.append({
                    "id": ph.id,
                    "type": "placeholder",
                    "action": "Deleted",
                    "item_type": item_type,
                    "item_title": item_title,
                    "series_title": series_title,
                    "path": ph.path,
                    "reason": deleted_reason,
                    "status": _humanize_placeholder_status(ph.lifecycle_status),
                    "time": updated_at.isoformat(),
                })

        # If a single-row lifecycle never changed, avoid duplicate created/deleted timestamps.
        deduped = []
        seen = set()
        for row in sorted(activity_list, key=lambda x: x.get("time") or "", reverse=True):
            key = (row.get("id"), row.get("action"), row.get("time"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)

        return deduped[:limit]
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

        # Observation trail errors removed as system is deprecated.
        pass

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
                "episode_future": int(row.episode_future or 0),
                "episode_missing": int(row.episode_missing or 0),
            }
            for row in session.query(
                Season.series_id.label("series_id"),
                func.count(Episode.id).label("episode_total"),
                func.sum(case((Episode.has_file == True, 1), else_=0)).label("episode_files"),
                func.sum(case((Episode.has_placeholder == True, 1), else_=0)).label("episode_placeholders"),
                func.sum(
                    case(
                        (
                            and_(
                                func.coalesce(Episode.has_file, False) == False,
                                func.coalesce(Episode.has_placeholder, False) == False,
                                Episode.determination == "not_needed",
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("episode_future"),
                func.sum(
                    case(
                        (
                            and_(
                                func.coalesce(Episode.has_file, False) == False,
                                func.coalesce(Episode.has_placeholder, False) == False,
                                or_(Episode.determination.is_(None), Episode.determination != "not_needed"),
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("episode_missing"),
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
            instance_meta = _arr_instance_meta(movie.instance_key, getattr(movie, "instance_id", None))
            movie_unresolved = (not bool(movie.has_file)) and (not bool(movie.has_placeholder))
            movie_is_future = movie_unresolved and movie.determination == "not_needed"
            movie_has_missing = movie_unresolved and not movie_is_future
            arr_link = _arr_item_link(
                item_type="movie",
                instance_key=movie.instance_key,
                instance_id=getattr(movie, "instance_id", None),
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
                    "is_4k": _legacy_is_4k(instance_meta),
                    "instance_key": movie.instance_key,
                    "instance_id": (getattr(movie, "instance_id", None) or instance_meta.get("instance_id") or None),
                    "instance_label": instance_meta.get("label") or movie.instance_key,
                    "arr_link": arr_link,
                    "determination": movie.determination,
                    "status": movie.status,
                    "has_file": bool(movie.has_file),
                    "has_placeholder": bool(movie.has_placeholder),
                    "is_future": movie_is_future,
                    "has_missing": movie_has_missing,
                    "overview": movie.radarr_overview,
                    "stats": {
                        "downloaded": 1 if movie.has_file else 0,
                        "placeholders": 1 if movie.has_placeholder else 0,
                        "future": 1 if movie_is_future else 0,
                        "missing": 1 if movie_has_missing else 0,
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
            instance_meta = _arr_instance_meta(series.instance_key, getattr(series, "instance_id", None))
            counts = series_episode_counts.get(
                series.id,
                {
                    "episode_total": 0,
                    "episode_files": 0,
                    "episode_placeholders": 0,
                    "episode_future": 0,
                    "episode_missing": 0,
                },
            )
            series_unresolved = max(
                int(counts["episode_total"]) - int(counts["episode_files"]) - int(counts["episode_placeholders"]),
                0,
            )
            series_is_future = series_unresolved > 0 and int(counts["episode_missing"]) == 0 and int(counts["episode_future"]) > 0
            series_has_missing = int(counts["episode_missing"]) > 0
            arr_link = _arr_item_link(
                item_type="series",
                instance_key=series.instance_key,
                instance_id=getattr(series, "instance_id", None),
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
                    "is_4k": _legacy_is_4k(instance_meta),
                    "instance_key": series.instance_key,
                    "instance_id": (getattr(series, "instance_id", None) or instance_meta.get("instance_id") or None),
                    "instance_label": instance_meta.get("label") or series.instance_key,
                    "arr_link": arr_link,
                    "determination": None,
                    "status": series.status,
                    "has_file": bool(series.has_files),
                    "has_placeholder": counts["episode_placeholders"] > 0,
                    "is_future": series_is_future,
                    "has_missing": series_has_missing,
                    "overview": series.sonarr_series_overview,
                    "stats": counts,
                }
            )

        items.sort(
            key=lambda item: (
                0 if item.get("has_placeholder") else 1,
                str(item.get("title") or "").lower(),
            )
        )

        return {"items": items[:limit], "count": min(len(items), limit)}
    finally:
        session.close()


@router.get("/api/detail/movie/{movie_id}")
async def movie_detail(movie_id: int):
    """Return full detail for a single movie."""
    session = get_session()
    try:
        movie = session.query(Movie).filter(Movie.id == movie_id, Movie.is_deleted == False).first()
        if not movie:
            return JSONResponse({"ok": False, "message": "Movie not found"}, status_code=404)
        arr_link = _arr_item_link(
            item_type="movie",
            instance_key=movie.instance_key,
            instance_id=getattr(movie, "instance_id", None),
            title=movie.title,
            payload=movie.radarr_payload_raw if isinstance(movie.radarr_payload_raw, dict) else None,
        )
        instance_meta = _arr_instance_meta(movie.instance_key, getattr(movie, "instance_id", None))
        return {
            "ok": True,
            "type": "movie",
            "id": movie.id,
            "title": movie.title,
            "year": movie.year,
            "overview": movie.radarr_overview,
            "poster_url": movie.remote_poster,
            "backdrop_url": movie.remote_fanart,
            "runtime": movie.radarr_runtime,
            "certification": movie.radarr_certification,
            "genres": movie.radarr_genres or [],
            "studio": movie.radarr_studio,
            "ratings": movie.radarr_ratings or {},
            "collection": movie.radarr_collection,
            "is_4k": _legacy_is_4k(instance_meta),
            "instance_key": movie.instance_key,
            "instance_id": (getattr(movie, "instance_id", None) or instance_meta.get("instance_id") or None),
            "instance_label": instance_meta.get("label") or movie.instance_key,
            "arr_link": arr_link,
            "imdbid": movie.imdbid,
            "tmdbid": movie.tmdbid,
            "status": movie.status,
            "determination": movie.determination,
            "has_file": bool(movie.has_file),
            "has_placeholder": bool(movie.has_placeholder),
            "placeholder_filepath": movie.placeholder_filepath,
            "radarr_quality": movie.radarr_quality,
            "radarr_monitored": bool(movie.radarr_monitored),
            "radarr_release_status": movie.radarr_release_status,
            "theater_release_date": _iso(movie.theater_release_date),
            "digital_release_date": _iso(movie.digital_release_date),
            "physical_release_date": _iso(movie.physical_release_date),
            "last_search": _iso(movie.last_search),
            "updated_at": _iso(movie.updated_at),
            "created_at": _iso(movie.created_at),
        }
    finally:
        session.close()


@router.get("/api/detail/series/{series_id}")
async def series_detail(series_id: int):
    """Return full detail for a series, including all seasons and their episodes."""
    session = get_session()
    try:
        series = session.query(Series).filter(Series.id == series_id, Series.is_deleted == False).first()
        if not series:
            return JSONResponse({"ok": False, "message": "Series not found"}, status_code=404)
        arr_link = _arr_item_link(
            item_type="series",
            instance_key=series.instance_key,
            instance_id=getattr(series, "instance_id", None),
            title=series.title,
            payload=series.sonarr_payload_raw if isinstance(series.sonarr_payload_raw, dict) else None,
        )
        instance_meta = _arr_instance_meta(series.instance_key, getattr(series, "instance_id", None))
        seasons_raw = (
            session.query(Season)
            .filter(Season.series_id == series_id, Season.is_deleted == False)
            .order_by(Season.season_number.asc())
            .all()
        )
        seasons_out = []
        for season in seasons_raw:
            episodes_raw = (
                session.query(Episode)
                .filter(Episode.season_id == season.id, Episode.is_deleted == False)
                .order_by(Episode.episode_number.asc())
                .all()
            )
            ep_total = len(episodes_raw)
            ep_files = sum(1 for e in episodes_raw if e.has_file)
            ep_placeholders = sum(1 for e in episodes_raw if e.has_placeholder)
            episodes_out = [
                {
                    "id": ep.id,
                    "episode_number": ep.episode_number,
                    "title": ep.title,
                    "air_date": _iso(ep.air_date),
                    "overview": ep.sonarr_episode_overview,
                    "still_url": ep.sonarr_episode_still,
                    "has_file": bool(ep.has_file),
                    "has_placeholder": bool(ep.has_placeholder),
                    "determination": ep.determination,
                    "status": ep.status,
                    "sonarr_quality": ep.sonarr_quality,
                    "sonarr_monitored": bool(ep.sonarr_monitored),
                    "placeholder_filepath": ep.placeholder_filepath,
                }
                for ep in episodes_raw
            ]
            seasons_out.append({
                "id": season.id,
                "season_number": season.season_number,
                "title": season.title,
                "overview": season.sonarr_season_overview,
                "has_files": bool(season.has_files),
                "episode_total": ep_total,
                "episode_files": ep_files,
                "episode_placeholders": ep_placeholders,
                "episodes": episodes_out,
            })
        return {
            "ok": True,
            "type": "series",
            "id": series.id,
            "title": series.title,
            "year": series.year,
            "overview": series.sonarr_series_overview,
            "poster_url": series.remote_poster,
            "backdrop_url": series.remote_fanart or series.remote_banner,
            "runtime": series.sonarr_runtime,
            "certification": series.sonarr_certification,
            "genres": series.sonarr_genres or [],
            "network": series.sonarr_network,
            "ratings": series.sonarr_ratings or {},
            "is_4k": _legacy_is_4k(instance_meta),
            "instance_key": series.instance_key,
            "instance_id": (getattr(series, "instance_id", None) or instance_meta.get("instance_id") or None),
            "instance_label": instance_meta.get("label") or series.instance_key,
            "arr_link": arr_link,
            "imdbid": series.imdbid,
            "tvdbid": series.tvdbid,
            "status": series.status,
            "sonarr_status": series.sonarr_status,
            "sonarr_monitored": bool(series.sonarr_monitored),
            "first_aired": _iso(series.sonarr_first_aired),
            "updated_at": _iso(series.updated_at),
            "created_at": _iso(series.created_at),
            "seasons": seasons_out,
        }
    finally:
        session.close()


@router.get("/api/calendar")
async def calendar_view(month: str = Query("")):
    try:
        month_start = _parse_calendar_month(month)
    except ValueError as exc:
        return JSONResponse(content={"ok": False, "message": str(exc)}, status_code=400)

    today = datetime.now().date()
    lookahead_days = int(getattr(settings, "CALENDAR_LOOKAHEAD_DAYS", 30) or 30)
    countdown_enabled = bool(getattr(settings, "ENABLE_COMING_SOON_COUNTDOWN", True))
    placeholders_enabled = bool(settings.coming_soon_placeholders_enabled)

    month_grid, visible_start, visible_end = _calendar_grid(month_start)
    items_by_date: dict[str, list[dict]] = {}
    movie_count = 0
    episode_count = 0
    in_window_count = 0

    session = get_session()
    try:
        movies = (
            session.query(Movie)
            .filter(
                Movie.is_deleted == False,
                or_(
                    Movie.theater_release_date.between(visible_start, visible_end),
                    Movie.digital_release_date.between(visible_start, visible_end),
                    Movie.physical_release_date.between(visible_start, visible_end),
                ),
            )
            .order_by(Movie.title.asc(), Movie.year.asc())
            .all()
        )

        for movie in movies:
            release_date, release_type, is_preferred = _movie_calendar_release(movie)
            if not release_date or release_date < visible_start or release_date > visible_end:
                continue

            in_window = _date_in_lookahead_window(release_date, today, lookahead_days)
            decision = _compute_calendar_decision(
                target_date=release_date,
                has_file=bool(movie.has_file),
                media_type="movie",
                lookahead_days=lookahead_days,
                countdown_enabled=countdown_enabled,
                placeholders_enabled=placeholders_enabled,
                now_date=today,
                release_type=release_type,
                release_type_preferred=is_preferred,
            )
            arr_link = _arr_item_link(
                item_type="movie",
                instance_key=movie.instance_key,
                instance_id=getattr(movie, "instance_id", None),
                title=movie.title,
                payload=movie.radarr_payload_raw if isinstance(movie.radarr_payload_raw, dict) else None,
            )
            movie_instance_meta = _arr_instance_meta(movie.instance_key, getattr(movie, "instance_id", None))
            items_by_date.setdefault(release_date.isoformat(), []).append(
                {
                    "id": f"movie-{movie.id}",
                    "item_id": movie.id,
                    "media_type": "movie",
                    "title": movie.title,
                    "subtitle": f"{movie.year}" if movie.year else "Movie",
                    "release_date": release_date.isoformat(),
                    "in_lookahead_window": in_window,
                    "days_until": decision.days_until,
                    "status": decision.status,
                    "reason": decision.reason,
                    "has_file": bool(movie.has_file),
                    "has_placeholder": bool(movie.has_placeholder),
                    "is_4k": _legacy_is_4k(movie_instance_meta),
                    "instance_key": movie.instance_key,
                    "arr_link": arr_link,
                    "release_type": release_type,
                    "release_type_label": _release_type_label(release_type),
                    "release_type_preferred": is_preferred,
                }
            )
            if release_date.month == month_start.month and release_date.year == month_start.year:
                movie_count += 1
                if in_window:
                    in_window_count += 1

        episodes = (
            session.query(
                Episode.id.label("episode_id"),
                Episode.title.label("episode_title"),
                Episode.air_date.label("air_date"),
                Episode.has_file.label("has_file"),
                Episode.has_placeholder.label("has_placeholder"),
                Episode.episode_number.label("episode_number"),
                Season.season_number.label("season_number"),
                Series.id.label("series_id"),
                Series.title.label("series_title"),
                Series.instance_key.label("instance_key"),
                Series.instance_id.label("instance_id"),
                Series.sonarr_payload_raw.label("sonarr_payload_raw"),
            )
            .join(Season, Season.id == Episode.season_id)
            .join(Series, Series.id == Season.series_id)
            .filter(
                Episode.is_deleted == False,
                Series.is_deleted == False,
                Episode.air_date.between(visible_start, visible_end),
            )
            .order_by(Episode.air_date.asc(), Series.title.asc(), Season.season_number.asc(), Episode.episode_number.asc())
            .all()
        )

        for episode in episodes:
            air_date = episode.air_date
            if not air_date:
                continue

            in_window = _date_in_lookahead_window(air_date, today, lookahead_days)
            decision = _compute_calendar_decision(
                target_date=air_date,
                has_file=bool(episode.has_file),
                media_type="episode",
                lookahead_days=lookahead_days,
                countdown_enabled=countdown_enabled,
                placeholders_enabled=placeholders_enabled,
                now_date=today,
            )
            arr_link = _arr_item_link(
                item_type="series",
                instance_key=episode.instance_key,
                instance_id=episode.instance_id,
                title=episode.series_title,
                payload=episode.sonarr_payload_raw if isinstance(episode.sonarr_payload_raw, dict) else None,
            )
            episode_instance_meta = _arr_instance_meta(episode.instance_key, episode.instance_id)
            ep_code = _episode_calendar_episode_code(episode.season_number, episode.episode_number)
            items_by_date.setdefault(air_date.isoformat(), []).append(
                {
                    "id": f"episode-{episode.episode_id}",
                    "item_id": episode.episode_id,
                    "series_id": episode.series_id,
                    "media_type": "episode",
                    "title": episode.series_title or episode.episode_title,
                    "subtitle": ep_code or None,
                    "season_number": int(episode.season_number) if episode.season_number is not None else None,
                    "episode_number": int(episode.episode_number) if episode.episode_number is not None else None,
                    "release_date": air_date.isoformat(),
                    "in_lookahead_window": in_window,
                    "days_until": decision.days_until,
                    "status": decision.status,
                    "reason": decision.reason,
                    "has_file": bool(episode.has_file),
                    "has_placeholder": bool(episode.has_placeholder),
                    "is_4k": _legacy_is_4k(episode_instance_meta),
                    "instance_key": episode.instance_key,
                    "arr_link": arr_link,
                }
            )
            if air_date.month == month_start.month and air_date.year == month_start.year:
                episode_count += 1
                if in_window:
                    in_window_count += 1

        for iso_key, day_items in list(items_by_date.items()):
            items_by_date[iso_key] = _merge_calendar_episodes_same_day(day_items)

        weeks = []
        for week in month_grid:
            week_days = []
            for day in week:
                iso_date = day.isoformat()
                week_days.append(
                    {
                        "iso_date": iso_date,
                        "day_number": day.day,
                        "is_current_month": day.month == month_start.month,
                        "is_today": day == today,
                        "in_lookahead_window": _date_in_lookahead_window(day, today, lookahead_days),
                        "item_count": len(items_by_date.get(iso_date, [])),
                        "items": items_by_date.get(iso_date, []),
                    }
                )
            weeks.append(week_days)

        return {
            "ok": True,
            "month": _format_calendar_month(month_start),
            "month_label": month_start.strftime("%B %Y"),
            "today_month": _format_calendar_month(today.replace(day=1)),
            "previous_month": _format_calendar_month(_calendar_nav_month(month_start, -1)),
            "next_month": _format_calendar_month(_calendar_nav_month(month_start, 1)),
            "weekday_labels": ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
            "lookahead": _calendar_lookahead_payload(today, lookahead_days),
            "legend": {
                "movie_release_types": [
                    {"key": "inCinemas", "label": _release_type_label("inCinemas")},
                    {"key": "digitalRelease", "label": _release_type_label("digitalRelease")},
                    {"key": "physicalRelease", "label": _release_type_label("physicalRelease")},
                ],
                "media_types": [
                    {"key": "movie", "label": "Movie", "icon": "🎬"},
                    {"key": "episode", "label": "TV", "icon": "📺"},
                ],
            },
            "summary": {
                "movie_count": movie_count,
                "episode_count": episode_count,
                "total_count": movie_count + episode_count,
                "in_window_count": in_window_count,
            },
            "weeks": weeks,
        }
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
    partial = bool(payload.get("partial", False)) if isinstance(payload, dict) else False
    context = payload.get("context") if isinstance(payload, dict) else None
    if not isinstance(values, dict):
        return JSONResponse(content={"ok": False, "errors": {"values": "expected an object"}}, status_code=400)

    logger.debug(
        f"Settings save request received: partial={partial} context={context or {}} keys={sorted(values.keys())}",
        extra={"emoji_type": "processing"},
    )
    before_arr_fingerprint = _arr_endpoint_fingerprint()
    was_setup_complete = bool(get_onboarding_status().get("setup_complete"))
    result = save_settings(values, partial=partial, context=context if isinstance(context, dict) else None)
    after_arr_fingerprint = _arr_endpoint_fingerprint()
    arr_endpoints_changed = before_arr_fingerprint != after_arr_fingerprint
    if result.get("ok") and not partial and not was_setup_complete and bool((result.get("status") or {}).get("setup_complete")):
        _launch_post_onboarding_startup_sync()
    elif result.get("ok") and not partial and arr_endpoints_changed:
        _launch_arr_change_full_sync(reason="arr_endpoint_changed")
    status_code = 200 if result.get("ok") else 400
    return JSONResponse(content=result, status_code=status_code)


@router.post("/api/settings/reset")
async def settings_reset():
    result = reset_onboarding()
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
    """Tail the current log file. Optionally filter by level threshold (all/debug/info/warn/error/critical)."""
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

    # Find the latest non-trivial log file.
    # Filename sort can pick the wrong file when multiple runs exist, and
    # very small run files can be from short utility imports.
    pattern = os.path.join(log_dir, "placeholdarr-*.log")
    log_files = _glob.glob(pattern)
    if not log_files:
        return {"lines": [], "file": None}

    by_mtime = sorted(log_files, key=lambda p: os.path.getmtime(p), reverse=True)
    log_file = by_mtime[0]
    for candidate in by_mtime:
        try:
            if os.path.getsize(candidate) >= 2048:
                log_file = candidate
                break
        except Exception:
            continue

    # Read last N lines efficiently
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except FileNotFoundError:
        return {"lines": [], "file": None}

    # Optional level filter (threshold semantics) — applied to ALL lines before
    # tailing so that changing the level shows the last N *matching* lines from
    # the full history, not the last N raw lines filtered down to almost nothing.
    normalized_level = str(level or "all").strip().lower()
    thresholds = {
        "debug": 10,
        "info": 20,
        "warn": 30,
        "error": 40,
        "critical": 50,
    }

    def _line_level_value(line: str):
        upper = line.upper()
        if "CRITICAL" in upper:
            return 50
        if "ERROR" in upper:
            return 40
        if "WARNING" in upper or " WARN " in upper:
            return 30
        if "INFO" in upper:
            return 20
        if "DEBUG" in upper:
            return 10
        return None

    threshold = thresholds.get(normalized_level)
    if threshold is not None:
        lines = [l for l in all_lines if (_line_level_value(l) or -1) >= threshold]
    else:
        lines = all_lines

    lines = lines[-tail:]

    return {
        "lines": [l.rstrip("\n") for l in lines],
        "file": os.path.basename(log_file),
    }
