"""Dashboard routes: lightweight UI showing stats, activity, and live logs."""

import ast
import asyncio
import calendar as _calendar
from collections import defaultdict
import json
import os
import glob as _glob
import re
import threading
import unicodedata
from datetime import date, datetime, timezone, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from sqlalchemy import and_, case, func, or_, text
from sqlalchemy.orm import joinedload

from core.config import parse_configured_arr_instances_json, settings
from core.logger import logger
from services.app_config import (
    get_onboarding_status,
    get_settings_payload,
    reset_onboarding,
    resolve_integration_test_credential,
    save_settings,
)
from services.activity_markers import EVENT_CALENDAR_DATE_REFRESH, EVENT_STARTUP_SOURCE_OF_TRUTH
from services.activity_snapshot import get_queue_download_activity_row, get_queue_download_snapshot
from services.dashboard_stats_snapshot_hooks import build_dashboard_stats_payload
from services.integrations import test_integration_connection
from services.postgres.db import get_session
from services.postgres.models import (
    AppConfig,
    DashboardStatsSnapshot,
    Movie,
    Series,
    Season,
    Episode,
    Placeholder,
    PlaceholderActivityHistory,
    SystemActivityHistory,
    Job,
    EventLog,
)
from services.library_catalog_version import get_library_versions, library_etag_for_shelf
from services.library_poster_paths import library_poster_cache_token, load_library_poster_path
from services.library_future_semantics import movie_row_is_future_outside_lookahead
from services.series_episode_stats import series_episode_counts_map, series_stats_dict_from_row
from services.source_of_truth.status_intent import StatusSource
from services.source_of_truth.calendar_phase import _compute_calendar_decision, _release_type_label

router = APIRouter()


def _launch_post_onboarding_startup_sync() -> None:
    """First-run sync after onboarding completes.

    Phase 4: enqueue ``startup_sync_runner`` Job when ``USE_JOB_DRIVEN_STARTUP_SYNC``
    is enabled; otherwise preserve the legacy daemon-thread behaviour.

    When job-driven mode is on, workers are not started during lifespan if onboarding
    was still incomplete at boot — start runtime services *before* enqueueing so a
    worker can claim the job (the handler also calls ``start_runtime_background_services``
    idempotently).
    """
    try:
        from services.source_of_truth.startup_sync_job import (
            enqueue_startup_sync_runner_job,
            use_job_driven_startup_sync,
        )

        if use_job_driven_startup_sync():
            try:
                from main import start_runtime_background_services

                start_runtime_background_services(reason="post_onboarding_before_runner_job")
            except Exception as exc:
                logger.error(
                    f"Failed to start runtime services before post-onboarding job: {exc}",
                    extra={"emoji_type": "error"},
                )
            session = get_session()
            try:
                enqueue_startup_sync_runner_job(session, reason="post_onboarding")
                session.commit()
                logger.info(
                    "Enqueued post-onboarding startup_sync_runner job",
                    extra={"emoji_type": "gear"},
                )
                return
            except Exception as exc:
                session.rollback()
                logger.warning(
                    f"Failed to enqueue post-onboarding startup job; falling back to thread: {exc}",
                    extra={"emoji_type": "warning"},
                )
            finally:
                session.close()
    except Exception:
        pass

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
    """Run per-instance ``run_full_sync`` after ARR endpoints change.

    Phase 4: enqueue ``startup_sync_runner`` with reason ``arr_endpoint_changed``
    when job-driven startup sync is enabled.

    Same as post-onboarding: ensure workers/schedulers are up before enqueueing when
    startup deferred them at boot (see ``_launch_post_onboarding_startup_sync``).
    """
    try:
        from services.source_of_truth.startup_sync_job import (
            enqueue_startup_sync_runner_job,
            use_job_driven_startup_sync,
        )

        if use_job_driven_startup_sync():
            try:
                from main import start_runtime_background_services

                start_runtime_background_services(reason="arr_change_before_runner_job")
            except Exception as exc:
                logger.error(
                    f"Failed to start runtime services before ARR-change startup job: {exc}",
                    extra={"emoji_type": "error"},
                )
            session = get_session()
            try:
                enqueue_startup_sync_runner_job(session, reason="arr_endpoint_changed")
                session.commit()
                logger.info(
                    f"Enqueued ARR-change startup_sync_runner job reason={reason}",
                    extra={"emoji_type": "gear"},
                )
                return
            except Exception as exc:
                session.rollback()
                logger.warning(
                    f"Failed to enqueue ARR-change startup job; falling back to thread: {exc}",
                    extra={"emoji_type": "warning"},
                )
            finally:
                session.close()
    except Exception:
        pass

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

_HTML_NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _dashboard_dist_index_path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "frontend", "dist", "index.html")


def frontend_build_id() -> str:
    """Vite hashed bundle path from dist/index.html, e.g. ``/assets/index-abc123.js``."""
    index_path = _dashboard_dist_index_path()
    try:
        with open(index_path, encoding="utf-8") as handle:
            html = handle.read()
    except OSError:
        return ""
    match = re.search(r"/assets/index-[^\"']+\.js", html)
    return match.group(0) if match else ""


def _dashboard_not_built_response() -> PlainTextResponse:
    return PlainTextResponse(
        "dashboard-next is not built yet. Run: cd frontend && npm install && npm run build",
        status_code=503,
    )


_SETUP_PREVIEW_SNIPPET = "<script>window.__PLACEHOLDARR_SETUP_PREVIEW__=!0</script>"


def _serve_dashboard_index(inject_setup_preview: bool = False) -> FileResponse | PlainTextResponse | HTMLResponse:
    index_path = _dashboard_dist_index_path()
    if not os.path.isfile(index_path):
        return _dashboard_not_built_response()
    if not inject_setup_preview:
        return FileResponse(index_path, media_type="text/html", headers=_HTML_NO_STORE_HEADERS)
    try:
        with open(index_path, encoding="utf-8") as handle:
            html = handle.read()
    except OSError:
        return FileResponse(index_path, media_type="text/html", headers=_HTML_NO_STORE_HEADERS)
    if "<head>" in html:
        html = html.replace("<head>", "<head>" + _SETUP_PREVIEW_SNIPPET, 1)
    else:
        html = _SETUP_PREVIEW_SNIPPET + html
    return HTMLResponse(content=html, media_type="text/html", headers=_HTML_NO_STORE_HEADERS)


@router.get("/", response_class=HTMLResponse)
async def dashboard_page():
    """Serve the React + TypeScript dashboard at the app root."""
    return _serve_dashboard_index()


@router.get("/activity", response_class=HTMLResponse)
async def dashboard_activity_page():
    return _serve_dashboard_index()


@router.get("/activity/{path:path}", response_class=HTMLResponse)
async def dashboard_activity_nested(path: str):
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


@router.get("/collections", response_class=HTMLResponse)
async def dashboard_collections_page():
    return _serve_dashboard_index()


@router.get("/collections/{path:path}", response_class=HTMLResponse)
async def dashboard_collections_nested(path: str):
    return _serve_dashboard_index()


@router.get("/errors")
async def dashboard_errors_page():
    return RedirectResponse(url="/logs", status_code=307)


@router.get("/logs", response_class=HTMLResponse)
async def dashboard_logs_page():
    return _serve_dashboard_index()


@router.get("/settings", response_class=HTMLResponse)
async def dashboard_settings_page():
    return _serve_dashboard_index()


@router.get("/settings/{path:path}", response_class=HTMLResponse)
async def dashboard_settings_nested(path: str):
    """Deep links under /settings (subsection slugs) must serve the SPA, same as /setup/*."""
    return _serve_dashboard_index()


@router.get("/setup", response_class=HTMLResponse)
async def dashboard_setup_page(request: Request):
    """Serve SPA for onboarding wizard (client-side /setup route)."""
    inject = request.query_params.get("preview") == "1"
    return _serve_dashboard_index(inject_setup_preview=inject)


@router.get("/setup/{path:path}", response_class=HTMLResponse)
async def dashboard_setup_nested(path: str):
    """Deep links under /setup still load the SPA shell."""
    inject = path == "preview" or path.startswith("preview/")
    return _serve_dashboard_index(inject_setup_preview=inject)


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


_OVERLAY_EXAMPLE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".json": "application/json",
}

_FAVICON_FILES = {
    "favicon.ico": "image/x-icon",
    "favicon-16x16.png": "image/png",
    "favicon-32x32.png": "image/png",
    "apple-touch-icon.png": "image/png",
}


def _dashboard_dist_root_file(filename: str, media_types: dict[str, str]) -> FileResponse | JSONResponse:
    """Serve a single file from frontend/dist (Vite public/ copies)."""
    if filename not in media_types:
        return JSONResponse({"ok": False, "message": "not found"}, status_code=404)
    dist_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
    file_path = os.path.join(dist_dir, filename)
    if not os.path.isfile(file_path):
        return JSONResponse({"ok": False, "message": "not found"}, status_code=404)
    return FileResponse(file_path, media_type=media_types[filename])


@router.get("/favicon.ico")
async def dashboard_favicon_ico():
    return _dashboard_dist_root_file("favicon.ico", _FAVICON_FILES)


@router.get("/favicon-16x16.png")
async def dashboard_favicon_16():
    return _dashboard_dist_root_file("favicon-16x16.png", _FAVICON_FILES)


@router.get("/favicon-32x32.png")
async def dashboard_favicon_32():
    return _dashboard_dist_root_file("favicon-32x32.png", _FAVICON_FILES)


@router.get("/apple-touch-icon.png")
async def dashboard_apple_touch_icon():
    return _dashboard_dist_root_file("apple-touch-icon.png", _FAVICON_FILES)


@router.get("/overlay-examples/{asset_path:path}")
async def dashboard_overlay_examples(asset_path: str):
    """Serve static poster overlay preview images from the Vite public folder (copied to dist on build)."""
    dist_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist", "overlay-examples")
    safe_path = os.path.normpath(asset_path).lstrip("/")
    if safe_path.startswith(".."):
        return JSONResponse({"ok": False, "message": "invalid asset path"}, status_code=400)

    file_path = os.path.join(dist_dir, safe_path)
    if not os.path.isfile(file_path):
        return JSONResponse({"ok": False, "message": "asset not found"}, status_code=404)
    _, ext = os.path.splitext(safe_path.lower())
    media_type = _OVERLAY_EXAMPLE_MEDIA_TYPES.get(ext, "application/octet-stream")
    return FileResponse(file_path, media_type=media_type)


# ---------------------------------------------------------------------------
# JSON API endpoints
# ---------------------------------------------------------------------------


@router.get("/api/health")
async def api_health():
    """Liveness probe: no database access. Use from the browser host to verify TCP/process."""
    from core.version import APP_VERSION

    return JSONResponse(
        {
            "ok": True,
            "app_version": APP_VERSION,
            "frontend_build": frontend_build_id(),
        }
    )


@router.get("/api/ready")
async def api_ready():
    """Readiness probe: includes whether the startup source-of-truth sync has released the worker gate."""
    from services.startup_gate import startup_sync_complete

    return JSONResponse(
        {
            "ok": True,
            "startup_sync_complete": startup_sync_complete.is_set(),
        }
    )


@router.get("/api/diagnostics/db")
async def api_diagnostics_db():
    """Operational snapshot of database health for incident triage.

    Phase 2 of the holistic NOTIFY audit: surfaces SQLAlchemy pool counters,
    Postgres lock blockers + longest-running transaction, and shared
    notifier health. Designed to be cheap (<100ms) so it's safe to poll
    from monitoring or a debug page during stalls.
    """
    from services.postgres.db import pool_stats

    out: dict[str, Any] = {"ok": True}

    out["pool"] = pool_stats()

    pg: dict[str, Any] = {}
    session = get_session()
    try:
        try:
            blockers_rows = session.execute(
                text(
                    """
                    SELECT
                        blocked.pid AS blocked_pid,
                        blocking.pid AS blocking_pid,
                        blocked.usename AS blocked_user,
                        blocking.usename AS blocking_user,
                        blocked.state AS blocked_state,
                        blocking.state AS blocking_state,
                        EXTRACT(EPOCH FROM (now() - blocked.query_start))::int AS blocked_query_age_s,
                        EXTRACT(EPOCH FROM (now() - blocking.query_start))::int AS blocking_query_age_s,
                        LEFT(COALESCE(blocked.query, ''), 200) AS blocked_query,
                        LEFT(COALESCE(blocking.query, ''), 200) AS blocking_query
                    FROM pg_stat_activity AS blocked
                    JOIN pg_stat_activity AS blocking
                      ON blocking.pid = ANY(pg_blocking_pids(blocked.pid))
                    WHERE blocked.wait_event_type = 'Lock'
                    LIMIT 20
                    """
                )
            ).fetchall()
            pg["blockers"] = [dict(r._mapping) for r in blockers_rows]
        except Exception as exc:
            pg["blockers_error"] = str(exc)

        try:
            longest = session.execute(
                text(
                    """
                    SELECT
                        pid,
                        usename,
                        state,
                        EXTRACT(EPOCH FROM (now() - xact_start))::int AS xact_age_s,
                        EXTRACT(EPOCH FROM (now() - query_start))::int AS query_age_s,
                        LEFT(COALESCE(query, ''), 200) AS query
                    FROM pg_stat_activity
                    WHERE state IS NOT NULL
                      AND state <> 'idle'
                      AND xact_start IS NOT NULL
                    ORDER BY xact_start ASC NULLS LAST
                    LIMIT 5
                    """
                )
            ).fetchall()
            pg["longest_running"] = [dict(r._mapping) for r in longest]
        except Exception as exc:
            pg["longest_running_error"] = str(exc)
    finally:
        try:
            session.close()
        except Exception:
            pass

    out["postgres"] = pg

    notifier_info: dict[str, Any] = {}
    try:
        from services.postgres.notifier import get_shared_notifier_health

        notifier_info = get_shared_notifier_health() or {}
    except Exception as exc:
        notifier_info["error"] = str(exc)
    out["notifier"] = notifier_info

    out["timestamp"] = datetime.now(timezone.utc).isoformat()
    return JSONResponse(out)


@router.get("/api/stats")
async def stats():
    """Return aggregate metrics for the dashboard header."""
    session = get_session()
    try:
        snap = session.get(DashboardStatsSnapshot, 1)
        if snap is not None:
            return {
                "movies": {
                    "total": int(snap.movies_total or 0),
                    "placeholders": int(snap.movies_placeholders or 0),
                    "downloaded": int(snap.movies_downloaded or 0),
                    "future_outside_lookahead": int(snap.movies_future_outside_lookahead or 0),
                },
                "series": {"total": int(snap.series_total or 0)},
                "episodes": {
                    "total": int(snap.episodes_total or 0),
                    "placeholders": int(snap.episodes_placeholders or 0),
                    "downloaded": int(snap.episodes_downloaded or 0),
                    "future_outside_lookahead": int(snap.episodes_future_outside_lookahead or 0),
                },
                "placeholders_on_disk": int(snap.placeholders_on_disk or 0),
                "jobs": {
                    "pending": int(snap.jobs_pending or 0),
                    "failed": int(snap.jobs_failed or 0),
                    "done": int(snap.jobs_done or 0),
                },
                "last_sync": snap.last_sync.isoformat() if snap.last_sync else None,
            }

        # Snapshot missing (first run / before hooks). Return live aggregate as safe fallback.
        return build_dashboard_stats_payload(session)
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
        "placeholder_art_refresh": "Art Refresh",
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
        "not_found": "NO QUALIFYING RELEASE FOUND",
    }
    normalized = str(status or "").strip().lower()
    if not normalized:
        return "Unknown"
    return mapping.get(normalized, _humanize_snake_case(normalized))


def _parse_activity_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _group_calendar_placeholder_status_rows(rows: list[dict], *, max_gap_sec: int = 180) -> list[dict]:
    """Collapse consecutive calendar-driven status rows (same sync window) into one expandable parent."""
    cal_src = StatusSource.CALENDAR_RELEASE_WINDOW.value
    if not rows:
        return rows
    out: list[dict] = []
    i = 0
    parent_seq = 0
    while i < len(rows):
        row = rows[i]
        if row.get("action") == "Status" and str(row.get("_status_source") or "") == cal_src:
            cluster: list[dict] = [row]
            j = i + 1
            while j < len(rows):
                nxt = rows[j]
                if nxt.get("action") != "Status" or str(nxt.get("_status_source") or "") != cal_src:
                    break
                t_last = _parse_activity_dt(cluster[-1].get("time"))
                t_nxt = _parse_activity_dt(nxt.get("time"))
                if t_last is None or t_nxt is None:
                    break
                delta = (t_last - t_nxt).total_seconds()
                if delta >= 0 and delta <= max_gap_sec:
                    cluster.append(nxt)
                    j += 1
                else:
                    break
            if len(cluster) >= 2:
                parent_seq += 1
                newest = cluster[0]
                child_rows: list[dict] = []
                for c in cluster:
                    child_rows.append(
                        {
                            "id": c["id"],
                            "type": "placeholder",
                            "action": c.get("action"),
                            "item_type": c.get("item_type"),
                            "item_title": c.get("item_title"),
                            "series_title": c.get("series_title"),
                            "path": c.get("path") or "",
                            "reason": c.get("reason"),
                            "status": c.get("status"),
                            "time": c.get("time"),
                        }
                    )
                parent_id = -(900_000_000 + parent_seq)
                out.append(
                    {
                        "id": parent_id,
                        "type": "placeholder",
                        "group_kind": "calendar_status_sync",
                        "action": "Status",
                        "item_type": "batch",
                        "item_title": f"Calendar sync — {len(child_rows)} titles",
                        "series_title": None,
                        "path": "",
                        "reason": "Placeholder status updated from the calendar (coming soon, release window, release day).",
                        "status": "Updated",
                        "time": newest.get("time"),
                        "children": child_rows,
                    }
                )
                i = j
            else:
                solo = cluster[0]
                solo.pop("_status_source", None)
                out.append(solo)
                i += 1
        else:
            row.pop("_status_source", None)
            out.append(row)
            i += 1
    return out


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
    et = str(event_type or "").strip().lower()
    if et.startswith("internal_dashboard_"):
        return False
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
        "placeholder_status_changed",
    }
    return et not in hidden_types


def _is_user_relevant_job(job_type: str, status: str | None = None) -> bool:
    token = str(job_type or "").strip().lower()
    if str(status or "").upper() == "FAILED":
        return True
    noisy = {
        "webhook_event",
        "nfo_refresh",
        "placeholder_art_refresh",
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
    raw = str(reason or "").strip()
    if not raw:
        return "Unknown"
    text = raw.lower()
    if text in mapping:
        return mapping[text]
    # Prose reasons (tombstone summaries, etc.) must stay intact — snake_case humanize
    # turns hyphens into spaces and flattens the sentence.
    if " " in raw or any(ch in raw for ch in "()-:/"):
        return raw
    return _humanize_snake_case(text)


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


_LOG_TS_HEAD = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?P<ms>,\d{1,6})?",
)


def _extract_log_timestamp(line: str) -> datetime | None:
    """Parse leading logging ``asctime`` (with optional millisecond comma group)."""
    raw = str(line or "").lstrip("\ufeff")
    m = _LOG_TS_HEAD.match(raw)
    if not m:
        return None
    base = m.group("ts")
    ms = m.group("ms")
    if ms:
        frac = ms[1:].ljust(6, "0")[:6]
        try:
            return datetime.strptime(f"{base},{frac}", "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc)
        except Exception:
            pass
    try:
        return datetime.strptime(base, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _parse_iso_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _fetch_latest_startup_activity_payload(session) -> dict[str, Any] | None:
    row = (
        session.query(EventLog.payload)
        .filter(EventLog.event_type == EVENT_STARTUP_SOURCE_OF_TRUTH)
        .order_by(EventLog.id.desc())
        .limit(1)
        .first()
    )
    if not row or not isinstance(row[0], dict):
        return None
    return row[0]


def _find_last_startup_mode_line_index(lines: list[str], mode: str) -> int | None:
    token = f"Startup sync mode selected: {str(mode or '').strip().lower()}"
    for idx in range(len(lines) - 1, -1, -1):
        if token in str(lines[idx] or "").lower():
            return idx
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
    """Newest ``placeholdarr-*.log`` by modification time that has any content.

    Do not require a minimum size: right after restart the active log is often small
    while older rotated files are large; skipping the new file made lite/calendar
    activity rows parse stale lines and show incorrect \"hours ago\" times.
    """
    pattern = os.path.join(_runtime_log_dir(), "placeholdarr-*.log")
    log_files = _glob.glob(pattern)
    if not log_files:
        return None
    by_mtime = sorted(log_files, key=lambda p: os.path.getmtime(p), reverse=True)
    for path in by_mtime:
        try:
            if os.path.getsize(path) > 0:
                return path
        except OSError:
            continue
    return by_mtime[0]


_LOG_LEVEL_THRESHOLDS = {
    "debug": 10,
    "info": 20,
    "warn": 30,
    "error": 40,
    "critical": 50,
}


def _log_line_level_value(line: str) -> int | None:
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


def _tail_file_lines(path: str, *, max_lines: int) -> list[str]:
    """Return the last ``max_lines`` lines without reading the whole file."""
    block = 65536
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            if end == 0:
                return []
            data = b""
            pos = end
            while pos > 0 and data.count(b"\n") <= max_lines:
                read_len = min(block, pos)
                pos -= read_len
                fh.seek(pos)
                data = fh.read(read_len) + data
    except OSError:
        return []

    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-max_lines:] if len(lines) > max_lines else lines


def _tail_log_lines_filtered(path: str, *, tail: int, threshold: int) -> list[str]:
    """Last ``tail`` lines at or above ``threshold``, scanning backward from EOF."""
    block = 65536
    collected: list[str] = []
    carry = b""
    try:
        pos = os.path.getsize(path)
    except OSError:
        return []
    if pos == 0:
        return []

    try:
        with open(path, "rb") as fh:
            while pos > 0 and len(collected) < tail:
                read_len = min(block, pos)
                pos -= read_len
                fh.seek(pos)
                chunk = fh.read(read_len) + carry
                parts = chunk.split(b"\n")
                carry = parts[0]
                for line_bytes in reversed(parts[1:]):
                    if len(collected) >= tail:
                        break
                    line = line_bytes.decode("utf-8", errors="replace")
                    if (_log_line_level_value(line) or -1) >= threshold:
                        collected.append(line)
            if len(collected) < tail and carry:
                line = carry.decode("utf-8", errors="replace")
                if (_log_line_level_value(line) or -1) >= threshold:
                    collected.append(line)
    except OSError:
        return []

    collected.reverse()
    return collected[-tail:]


def _read_runtime_log_tail(path: str, *, tail: int, level: str) -> list[str]:
    normalized_level = str(level or "all").strip().lower()
    threshold = _LOG_LEVEL_THRESHOLDS.get(normalized_level)
    if threshold is None:
        lines = _tail_file_lines(path, max_lines=tail)
    else:
        lines = _tail_log_lines_filtered(path, tail=tail, threshold=threshold)
    return [line.rstrip("\n") for line in lines]


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


def _startup_eventlog_supplements_log_parse(
    marker: dict[str, Any] | None,
    anchor_ts: datetime | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """When log lines are missing/truncated, use EventLog payload from the same run (completed_at after anchor)."""
    if not isinstance(marker, dict) or anchor_ts is None:
        return {}, {}
    m_completed = _parse_iso_datetime(marker.get("completed_at"))
    m_started = _parse_iso_datetime(marker.get("started_at"))
    if m_completed is None or m_started is None:
        return {}, {}
    if m_completed < anchor_ts - timedelta(seconds=5):
        return {}, {}
    if m_started < anchor_ts - timedelta(minutes=5) or m_started > anchor_ts + timedelta(minutes=30):
        return {}, {}
    det: dict[str, Any] = {}
    mat: dict[str, Any] = {}
    raw_det = marker.get("determination")
    if isinstance(raw_det, dict) and raw_det:
        det = dict(raw_det)
    raw_mat = marker.get("materialization")
    if isinstance(raw_mat, dict) and raw_mat:
        mat = dict(raw_mat)
    return det, mat


def _build_startup_sync_progress_row(session) -> dict[str, Any] | None:
    """Build startup / lite sync activity row.

    **When** time prefers, in order:
    1. Latest ``internal_dashboard_startup_source_of_truth`` EventLog ``payload.started_at`` (DB).
    2. Timestamp on the ``Startup sync mode selected:`` line (legacy log fallback).

    Section metrics are parsed from log lines after the anchor; when determination/materialization
    lines are missing, the latest matching ``EventLog`` payload (written at startup completion) fills gaps.
    """
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

    marker = _fetch_latest_startup_activity_payload(session)
    display_time: datetime | None = _parse_iso_datetime(marker.get("started_at")) if marker else None

    if marker:
        startup_mode = str(marker.get("mode") or "unknown").strip().lower() or "unknown"
        anchor_idx = _find_last_startup_mode_line_index(lines, startup_mode)
        if anchor_idx is None:
            anchor_idx = max(0, len(lines) - 8000)
    else:
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
    re_determination = re.compile(
        r"(?:Determination phase complete:|Determination · full_scan · complete:|Determination · scoped · complete:|Scoped determination complete:)\s*(\{.*\})"
    )
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
    lite_movies_seen = 0
    lite_series_seen = 0
    lite_episodes_seen = 0
    startup_completed = False

    re_lite_movie_human = re.compile(
        r"Startup lite .* movies:\s*(\d+)\s+synced from Radarr",
        re.IGNORECASE,
    )
    re_lite_series_human = re.compile(
        r"Startup lite .* TV shows:\s*(\d+)\s+series and\s*(\d+)\s+episodes updated from Sonarr",
        re.IGNORECASE,
    )
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
        match = re_lite_movie_human.search(line)
        if match:
            lite_movies_seen += int(match.group(1) or 0)
        match = re_lite_series_human.search(line)
        if match:
            lite_series_seen += int(match.group(1) or 0)
            lite_episodes_seen += int(match.group(2) or 0)
        if re_startup_completed.search(line):
            startup_completed = True

    anchor_ts: datetime | None = None
    if anchor_idx is not None and 0 <= anchor_idx < len(lines):
        anchor_ts = _extract_log_timestamp(lines[anchor_idx])

    marker_det, marker_mat = _startup_eventlog_supplements_log_parse(marker, anchor_ts)
    if not determination and marker_det:
        determination = marker_det
    if not materialization and marker_mat:
        materialization = marker_mat

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
                        str(int(lite_series_seen))
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
                {"label": "Placeholder on disk OK", "value": int(determination.get("placeholder_exists", 0) or 0)},
                {"label": "No placeholder (file, deleted, or rules)", "value": int(determination.get("not_needed", 0) or 0)},
                {"label": "Remove stale placeholder", "value": int(determination.get("obsolete_placeholder", 0) or 0)},
                {
                    "label": "Path corrected",
                    "value": int(determination.get("path_drift_movies", 0) or 0)
                    + int(determination.get("path_drift_episodes", 0) or 0),
                },
                {
                    "label": "Rows updated (DB)",
                    "value": int(determination.get("movies_changed", 0) or 0) + int(determination.get("episodes_changed", 0) or 0),
                },
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
                {"label": "Deleted", "value": int(materialization.get("deleted", 0) or 0)},
                {"label": "Files created", "value": int(materialization.get("files_created", 0) or 0)},
                {"label": "Files deleted", "value": int(materialization.get("files_deleted", 0) or 0)},
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
    _c = int(materialization.get("created", 0) or 0)
    _d = int(materialization.get("deleted", 0) or 0)
    _fd = int(materialization.get("files_deleted", 0) or 0)
    if _d or _fd:
        mat_frag = f"created {_c} • removed {_d} (files deleted {_fd})"
    else:
        mat_frag = f"created {_c}"
    summary_details = f"Materialization {mat_frag} • Movies: {movie_refresh_summary} • TV: {tv_refresh_summary}"

    row_id_prefix = "lite-sync-progress" if startup_mode == "lite" else "full-sync-progress"
    row_id = f"{row_id_prefix}-{run_timestamp}" if run_timestamp else row_id_prefix

    if display_time is None and anchor_idx is not None and anchor_idx < len(lines):
        display_time = _extract_log_timestamp(lines[anchor_idx])
    if display_time is None and anchor_idx is not None:
        for scan_idx in range(anchor_idx, min(len(lines), anchor_idx + 400)):
            t = _extract_log_timestamp(lines[scan_idx])
            if t is not None:
                display_time = t
                break

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


def _activity_marker_row_for_startup_event(ev: EventLog) -> dict[str, Any]:
    payload = ev.payload if isinstance(ev.payload, dict) else {}
    mode = str(payload.get("mode") or "").strip().lower()
    started_at = _parse_iso_datetime(payload.get("started_at"))
    completed_at = _parse_iso_datetime(payload.get("completed_at"))
    is_lite = mode == "lite"
    return {
        "id": f"marker-startup-{ev.id}",
        "type": "job",
        "job_type": "lite_sync_progress" if is_lite else "full_sync_progress",
        "display_name": "Lite Sync Progress" if is_lite else "Full Sync Progress",
        "status": "DONE",
        "details": f"Startup source-of-truth run • Mode {mode or 'auto'}",
        "time": (started_at.isoformat() if started_at else (ev.created_at.isoformat() if ev.created_at else None)),
        "progress": {
            "running": False,
            "sections": [
                {
                    "name": "Run",
                    "status": "done",
                    "metrics": [
                        {"label": "Mode", "value": mode or "--"},
                        {"label": "Started", "value": started_at.isoformat() if started_at else "--"},
                        {"label": "Completed", "value": completed_at.isoformat() if completed_at else "--"},
                    ],
                }
            ],
        },
    }


def _activity_marker_row_for_calendar_event(ev: EventLog) -> dict[str, Any]:
    payload = ev.payload if isinstance(ev.payload, dict) else {}
    started_at = _parse_iso_datetime(payload.get("started_at"))
    completed_at = _parse_iso_datetime(payload.get("completed_at"))
    errors = int(payload.get("errors", 0) or 0)
    status = "FAILED" if errors > 0 else "DONE"
    sort_time = completed_at or started_at or ev.created_at
    return {
        "id": f"marker-calendar-{ev.id}",
        "type": "job",
        "job_type": "calendar_sync_progress",
        "display_name": "Calendar Sync",
        "status": status,
        "details": (
            f"Window {payload.get('start_date', '--')} to {payload.get('end_date', '--')} • "
            f"Movies updated {int(payload.get('movie_rows_updated', 0) or 0)} • "
            f"Episodes updated {int(payload.get('episode_rows_updated', 0) or 0)}"
        ),
        "time": (sort_time.isoformat() if sort_time else None),
        "progress": {
            "running": False,
            "sections": [
                {
                    "name": "Calendar Window",
                    "status": "done" if status != "FAILED" else "failed",
                    "metrics": [
                        {"label": "Lookahead start", "value": str(payload.get("start_date") or "--")},
                        {"label": "Lookahead end", "value": str(payload.get("end_date") or "--")},
                        {"label": "Completed", "value": completed_at.isoformat() if completed_at else "--"},
                    ],
                },
                {
                    "name": "Date Refresh Stats",
                    "status": "done" if status != "FAILED" else "failed",
                    "metrics": [
                        {"label": "Movie rows seen", "value": int(payload.get("movie_rows_seen", 0) or 0)},
                        {"label": "Movie rows updated", "value": int(payload.get("movie_rows_updated", 0) or 0)},
                        {"label": "Episode rows seen", "value": int(payload.get("episode_rows_seen", 0) or 0)},
                        {"label": "Episode rows updated", "value": int(payload.get("episode_rows_updated", 0) or 0)},
                        {"label": "Errors", "value": errors},
                    ],
                },
            ],
        },
    }


def _normalize_event_status_for_activity_feed(raw: Any) -> str:
    """Same rules as ``_event_log_row_status_for_activity_feed`` but for plain strings (historical snapshots)."""
    u = str(raw or "DONE").strip().upper()
    if u in ("PENDING", "QUEUED", "CLAIMED", "PROCESSING"):
        return "DONE"
    if u == "FAILED":
        return "FAILED"
    return str(raw or "DONE")


def _event_log_row_status_for_activity_feed(e: EventLog) -> str:
    """Map raw EventLog worker states to activity-feed status.

    Webhooks are inserted as ``PENDING`` then updated by the worker; snapshots are
    taken on insert, so rows would otherwise show ``pending`` forever while grouped
    headers show ``DONE``. Treat accepted-but-not-finished as done for this feed.
    """
    return _normalize_event_status_for_activity_feed(e.status)


def build_activity_snapshots_for_event_log(session, e: EventLog) -> list[dict[str, Any]]:
    """Build 0+ API activity rows for a single EventLog row (used by system_activity_history hooks)."""
    event_type = str(e.event_type or "").strip().lower()
    if event_type == EVENT_STARTUP_SOURCE_OF_TRUTH:
        # Startup sync progress is now persisted phase-by-phase by startup runner snapshots.
        return []
    if event_type == EVENT_CALENDAR_DATE_REFRESH:
        return [_activity_marker_row_for_calendar_event(e)]
    if not _is_user_relevant_event(event_type):
        return []

    grouped_event_types = {
        "movie_added",
        "movie_imported",
        "episode_imported",
        "movie_file_deleted",
        "episode_file_deleted",
    }
    if event_type in grouped_event_types:
        payload_dict = e.payload if isinstance(e.payload, dict) else {}
        return [
            {
                "id": e.id,
                "type": "event",
                "event_type": e.event_type,
                "_regroup": True,
                "display_name": _humanize_event_type(e.event_type),
                "source": e.source,
                "status": _event_log_row_status_for_activity_feed(e),
                "error": e.error_message,
                "details": _activity_details_for_event(e.event_type, payload_dict),
                "time": e.created_at.isoformat() if e.created_at else None,
                "_payload": payload_dict,
            }
        ]

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
        return [_build_series_add_summary_row(session, row_data)]

    status_text = str(e.status or "")
    display_name = _humanize_event_type(e.event_type)
    if status_text.upper() == "FAILED":
        display_name = f"[Failed] {display_name}"
    payload_dict = e.payload if isinstance(e.payload, dict) else {}
    detail_line = _activity_details_for_event(e.event_type, payload_dict)
    return [
        {
            "id": e.id,
            "type": "event",
            "event_type": e.event_type,
            "display_name": display_name,
            "source": e.source,
            "status": _event_log_row_status_for_activity_feed(e),
            "error": e.error_message,
            "details": detail_line,
            "time": e.created_at.isoformat() if e.created_at else None,
        }
    ]


def build_activity_snapshots_for_job(session, j: Job) -> list[dict[str, Any]]:
    """Build API activity rows for a Job row (expand sync placeholder stats + main job line)."""
    out: list[dict[str, Any]] = []
    normalized_job_type = str(j.job_type or "").strip().lower()
    job_dict = {
        "id": j.id,
        "job_type": j.job_type,
        "status": j.status,
        "created_at": j.created_at,
        "updated_at": j.updated_at,
        "error_message": j.error_message,
    }
    if normalized_job_type in {"full_sync", "lite_sync"}:
        out.extend(_build_sync_placeholder_rows(session, job_dict))
    if not _is_user_relevant_job(normalized_job_type, j.status):
        return out
    display_name = _humanize_job_type(j.job_type)
    if str(j.status or "").upper() == "FAILED":
        display_name = f"[Failed] {display_name}"
    out.append(
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
    return out


# Events closer than this (same type) roll into one "(xN)" row — restores the implicit window the feed had
# when it queried only the latest ``limit * 2`` EventLog rows before ``system_activity_history`` materialization.
_REGROUP_BURST_MAX_GAP_SEC = 300


def _merge_regrouped_event_rows_from_flat(flat_event_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild grouped (xN) event rows from per-event snapshots stored in system_activity_history.

    Historically, `/api/activity` read **recent** EventLog rows only (~``limit * 2``), so regrouping naturally
    reflected short bursts. Materialized snapshots can flatten **many** days of events; without clustering,
    every ``movie_added`` in the loaded history collapsed into a single misleading ``(xN)``. We cluster by
    wall-clock gap (same idea as calendar status grouping / ``max_gap_sec``).
    """
    grouped_event_types = {
        "movie_added",
        "movie_imported",
        "episode_imported",
        "movie_file_deleted",
        "episode_file_deleted",
    }
    bucket: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for r in flat_event_rows:
        et = str(r.get("event_type") or "").strip().lower()
        if r.get("_regroup") and et in grouped_event_types:
            payload_dict = r.get("_payload") if isinstance(r.get("_payload"), dict) else {}
            bucket.append(
                {
                    "id": r.get("id"),
                    "event_type": r.get("event_type"),
                    "source": r.get("source"),
                    "status": r.get("status"),
                    "error": r.get("error"),
                    "time": r.get("time"),
                    "_payload": payload_dict,
                }
            )
        else:
            rest.append({k: v for k, v in r.items() if not str(k).startswith("_")})

    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in bucket:
        et = str(r.get("event_type") or "").strip().lower()
        by_type[et].append(r)

    grouped_out: list[dict[str, Any]] = []
    epoch_min = datetime.min.replace(tzinfo=timezone.utc)

    for event_type, rows in by_type.items():
        seen_ids: set[Any] = set()
        uniq_rows: list[dict[str, Any]] = []
        for r in rows:
            rid = r.get("id")
            if rid is not None:
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)
            uniq_rows.append(r)

        timed: list[tuple[dict[str, Any], datetime | None]] = [
            (r, _parse_iso_datetime(r.get("time"))) for r in uniq_rows
        ]
        timed.sort(key=lambda x: x[1] or epoch_min)

        clusters: list[list[tuple[dict[str, Any], datetime | None]]] = []
        cur: list[tuple[dict[str, Any], datetime | None]] = []
        for item in timed:
            _r, ts = item
            if not cur:
                cur = [item]
                continue
            _pr, prev_ts = cur[-1]
            burst = (
                ts is not None
                and prev_ts is not None
                and (ts - prev_ts).total_seconds() <= float(_REGROUP_BURST_MAX_GAP_SEC)
            )
            if burst:
                cur.append(item)
            else:
                clusters.append(cur)
                cur = [item]
        if cur:
            clusters.append(cur)

        for ci, cluster in enumerate(clusters):
            count = len(cluster)
            failed = 0
            latest: datetime | None = None
            grouped_items: list[dict[str, Any]] = []
            for r, created_at in cluster:
                if _normalize_event_status_for_activity_feed(r.get("status")).upper() == "FAILED":
                    failed += 1
                if created_at and (latest is None or created_at > latest):
                    latest = created_at
                payload_dict = r.get("_payload") if isinstance(r.get("_payload"), dict) else {}
                if len(grouped_items) < 25:
                    grouped_items.append(
                        {
                            "id": r.get("id"),
                            "display_name": _humanize_event_type(r.get("event_type")),
                            "status": _normalize_event_status_for_activity_feed(r.get("status")),
                            "source": r.get("source"),
                            "error": r.get("error"),
                            "details": _activity_details_for_event(r.get("event_type"), payload_dict),
                            "time": r.get("time"),
                        }
                    )

            display_name = _humanize_event_type(event_type)
            if failed > 0:
                display_name = f"[Failed] {display_name}"
            group_details = f"Grouped {count} similar events • Failures {failed}"
            anchor_ts = cluster[-1][1] or cluster[0][1]
            group_id = (
                f"group:{event_type}:{int(anchor_ts.timestamp())}:{ci}"
                if isinstance(anchor_ts, datetime)
                else f"group:{event_type}:{ci}"
            )
            grouped_out.append(
                {
                    "id": group_id,
                    "type": "event",
                    "event_type": event_type,
                    "display_name": f"{display_name} (x{count})",
                    "status": "FAILED" if failed > 0 else "DONE",
                    "details": group_details,
                    "time": latest.isoformat() if isinstance(latest, datetime) else None,
                    "progress": {
                        "grouped_events": grouped_items if isinstance(grouped_items, list) else [],
                    },
                }
            )

    return rest + grouped_out


def _activity_feed_from_history(
    session,
    *,
    limit: int,
    before_time: datetime | None = None,
    before_id: int | None = None,
) -> tuple[list[dict[str, Any]], bool, str | None, int | None]:
    """Return merged activity rows from system_activity_history + live queue/sync rows.

    Returns ``(rows, has_more, next_before_time, next_before_id)``. Empty table = fresh install.
    """
    fetch_n = max(limit * 8, limit)
    try:
        q = session.query(SystemActivityHistory)
        if before_time is not None:
            if before_id is not None:
                q = q.filter(
                    or_(
                        SystemActivityHistory.occurred_at < before_time,
                        and_(
                            SystemActivityHistory.occurred_at == before_time,
                            SystemActivityHistory.id < before_id,
                        ),
                    )
                )
            else:
                q = q.filter(SystemActivityHistory.occurred_at < before_time)
        elif before_id is not None:
            q = q.filter(SystemActivityHistory.id < before_id)

        hist = (
            q.order_by(
                SystemActivityHistory.occurred_at.desc(),
                SystemActivityHistory.id.desc(),
            )
            .limit(fetch_n + 1)
            .all()
        )
    except Exception as exc:
        logger.warning("system_activity_history read failed: %s", exc, extra={"emoji_type": "warning"})
        hist = []

    has_more_hist = len(hist) > fetch_n
    hist = hist[:fetch_n]
    oldest_hist_time: str | None = None
    oldest_hist_id: int | None = None
    if hist:
        oldest = hist[-1]
        oldest_hist_time = oldest.occurred_at.isoformat() if oldest.occurred_at else None
        oldest_hist_id = int(oldest.id) if oldest.id is not None else None

    flat: list[dict[str, Any]] = []
    for h in hist:
        snap = h.snapshot if isinstance(h.snapshot, dict) else {}
        rows = snap.get("rows")
        if isinstance(rows, list):
            for r in rows:
                if isinstance(r, dict):
                    flat.append(r)

    flat.sort(key=lambda x: x.get("time") or "", reverse=True)
    raw_events = [r for r in flat if r.get("type") == "event"]
    raw_jobs = [r for r in flat if r.get("type") == "job"]
    merged_events = _merge_regrouped_event_rows_from_flat(raw_events)
    combined = merged_events + raw_jobs

    # Live queue row only on the newest page (no before cursor).
    if before_time is None and before_id is None:
        queue_activity_row = get_queue_download_activity_row()
        extra = [r for r in (queue_activity_row,) if r]
        combined = combined + extra

    # Calendar sync appears only via ``system_activity_history`` snapshots from
    # ``EVENT_CALENDAR_DATE_REFRESH`` EventLog rows (``_activity_marker_row_for_calendar_event``).
    # Do not append a second synthetic row from log parsing — it duplicated the same run.

    # ``startup_sync_progress`` persists multiple phase snapshots for the same run id.
    # Keep only the latest snapshot per startup sync row id so the activity list
    # doesn't show both "WORKING" and "DONE" cards for one run.
    collapsed_startup: dict[tuple[str, str], dict[str, Any]] = {}
    passthrough: list[dict[str, Any]] = []
    for row in combined:
        if str(row.get("type") or "") != "job":
            passthrough.append(row)
            continue
        jt = str(row.get("job_type") or "")
        if jt not in {"lite_sync_progress", "full_sync_progress"}:
            passthrough.append(row)
            continue
        row_id = str(row.get("id") or "")
        if not row_id:
            passthrough.append(row)
            continue
        key = (jt, row_id)
        existing = collapsed_startup.get(key)
        if existing is None:
            collapsed_startup[key] = row
            continue
        row_time = str(row.get("time") or "")
        existing_time = str(existing.get("time") or "")
        if row_time > existing_time:
            collapsed_startup[key] = row
            continue
        if row_time == existing_time:
            row_terminal = str(row.get("status") or "").upper() in {"DONE", "FAILED"}
            existing_terminal = str(existing.get("status") or "").upper() in {"DONE", "FAILED"}
            if row_terminal and not existing_terminal:
                collapsed_startup[key] = row
    combined = passthrough + list(collapsed_startup.values())
    combined.sort(key=lambda x: x.get("time") or "", reverse=True)

    unique: list[dict[str, Any]] = []
    seen_keys: set[tuple[Any, ...]] = set()
    for row in combined:
        key = (row.get("type"), row.get("id"), row.get("display_name"), row.get("time"))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique.append(row)

    # Chronological "recent operations": do not prepend every FAILED row ahead of all successes.
    # Large FAILED backlogs (e.g. old nfo_refresh attempts) otherwise consume the entire limit and
    # hide successful lite sync, calendar refresh, and webhooks from the same session.
    unique.sort(key=lambda x: x.get("time") or "", reverse=True)

    top = unique[:limit]
    if before_time is None and before_id is None:
        top_keys = {(row.get("type"), row.get("id"), row.get("display_name"), row.get("time")) for row in top}
        grouped_candidates = [
            row
            for row in unique
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

    page = top[:limit]
    has_more = has_more_hist or len(unique) > limit
    return page, bool(has_more and oldest_hist_time is not None), oldest_hist_time, oldest_hist_id


_OPERATIONS_EXCLUDED_JOB_TYPES = frozenset(
    {
        "full_sync_progress",
        "lite_sync_progress",
        "calendar_sync_progress",
        "full_sync",
        "lite_sync",
        "calendar_date_refresh",
    }
)


def _activity_operations_feed(
    session,
    *,
    limit: int,
    before_time: datetime | None = None,
    before_id: int | None = None,
) -> dict[str, Any]:
    """Live/event feed: exclude scheduled maintenance progress and sync job rows."""
    rows, has_more, next_time, next_id = _activity_feed_from_history(
        session,
        limit=max(limit * 2, limit),
        before_time=before_time,
        before_id=before_id,
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        jt = str(row.get("job_type") or "").strip().lower()
        if jt in _OPERATIONS_EXCLUDED_JOB_TYPES:
            continue
        out.append(row)
    page = out[:limit]
    return {
        "items": page,
        "has_more": bool(has_more and len(page) > 0),
        "next_before_time": next_time if has_more else None,
        "next_before_id": next_id if has_more else None,
    }


@router.get("/api/activity/active-searches")
async def activity_active_searches():
    """Titles Placeholdarr is monitoring after a playback/search (not the full ARR queue)."""
    snap = get_queue_download_snapshot()
    if not snap:
        return {
            "active": False,
            "items": [],
            "details": "No titles being monitored",
            "started_at": None,
            "updated_at": None,
        }
    items = snap.get("items") if isinstance(snap.get("items"), list) else []
    n = len(items)
    if n == 0:
        details = (
            "Monitoring Radarr/Sonarr — queue is empty; indexer search may still be running "
            "or nothing matched yet"
        )
    else:
        details = f"{n} title(s) — monitoring search, queue, and import until the real file is in the library"
    return {
        "active": True,
        "items": items,
        "details": details,
        "started_at": snap.get("started_at"),
        "updated_at": snap.get("updated_at"),
    }


@router.get("/api/activity/operations")
async def activity_operations(
    limit: int = Query(50, ge=1, le=200),
    before_time: Optional[str] = Query(None),
    before_id: Optional[int] = Query(None, ge=1),
):
    """Return live operations feed (webhooks, imports, queue monitor — not scheduled sync runs)."""
    session = get_session()
    try:
        bt = _parse_activity_dt(before_time) if before_time else None
        return _activity_operations_feed(session, limit=limit, before_time=bt, before_id=before_id)
    finally:
        session.close()


@router.get("/api/activity")
async def activity(
    limit: int = Query(50, ge=1, le=200),
    before_time: Optional[str] = Query(None),
    before_id: Optional[int] = Query(None, ge=1),
):
    """Alias for operations feed (backward compatibility)."""
    session = get_session()
    try:
        bt = _parse_activity_dt(before_time) if before_time else None
        return _activity_operations_feed(session, limit=limit, before_time=bt, before_id=before_id)
    finally:
        session.close()


def _resolve_history_item_titles(
    session, h: PlaceholderActivityHistory
) -> tuple[str, str | None, int | None]:
    """Fill item_title / series_title / series_id from history row or FK joins (hooks often leave titles empty)."""
    stored_title = (h.item_title or "").strip()
    stored_series_id = int(h.series_id) if h.series_id is not None else None
    if stored_title:
        return stored_title, h.series_title, stored_series_id
    item_type = str(h.item_type or "episode").strip().lower()
    if item_type == "movie":
        movie = session.query(Movie).filter(Movie.id == h.movie_id).first() if h.movie_id else None
        return (movie.title if movie else "Unknown Movie"), None, None
    if item_type == "series":
        series = session.query(Series).filter(Series.id == h.series_id).first() if h.series_id else None
        title = series.title if series and getattr(series, "title", None) else "Unknown Series"
        return title, title, stored_series_id or (int(series.id) if series else None)
    episode = (
        session.query(Episode)
        .options(joinedload(Episode.season).joinedload(Season.series))
        .filter(Episode.id == h.episode_id)
        .first()
        if h.episode_id
        else None
    )
    series = session.query(Series).filter(Series.id == h.series_id).first() if h.series_id else None
    if episode and not series:
        try:
            series = episode.season.series if getattr(episode, "season", None) else None
        except Exception:
            series = None
    if episode:
        sn = getattr(episode, "season_number", None)
        if sn is None and getattr(h, "season_number", None) is not None:
            sn = h.season_number
        sn = int(sn or 0)
        ep_num = int(getattr(episode, "episode_number", 0) or 0)
        ep_title = getattr(episode, "title", "Unknown") or "Unknown"
        item_title = f"S{sn:02d}E{ep_num:02d} - {ep_title}"
    else:
        item_title = "Unknown Episode"
    series_title = series.title if series else None
    resolved_series_id = stored_series_id
    if resolved_series_id is None and series is not None and getattr(series, "id", None) is not None:
        resolved_series_id = int(series.id)
    return item_title, series_title, resolved_series_id


def _activity_dict_from_history_row(session, h: PlaceholderActivityHistory) -> dict[str, Any]:
    item_title, series_title, resolved_series_id = _resolve_history_item_titles(session, h)
    action = str(h.action or "").strip()
    action_cap = action if action in ("Created", "Deleted", "Status") else ("Status" if not action else action.title())
    path_val = str(h.path or "") if h.path is not None else ""
    raw_reason = str(h.reason or "")
    if action_cap == "Created":
        display_reason = _humanize_activity_reason(raw_reason)
        if display_reason == "Unknown":
            display_reason = "Library materialization"
    elif action_cap == "Deleted":
        display_reason = _humanize_activity_reason(raw_reason)
        if display_reason == "Unknown":
            lc = str(h.status_label or "").strip().lower()
            if lc in {"missing", "deleted", "obsolete", "replaced"}:
                display_reason = "Library cleanup"
            else:
                display_reason = _humanize_placeholder_reason(lc or "deleted", action="Deleted")
    else:
        display_reason = raw_reason
    status_src = str(h.source or "").strip()
    new_status_for_human = ""
    if action_cap == "Status" and isinstance(h.extra_snapshot, dict):
        new_status_for_human = str(h.extra_snapshot.get("new_status") or "").strip()
    status_display = _humanize_placeholder_status(
        new_status_for_human or h.status_label or None,
    )
    occurred = h.occurred_at.isoformat() if h.occurred_at else None
    out: dict[str, Any] = {
        "id": h.id,
        "type": "placeholder",
        "action": action_cap,
        "item_type": (
            "movie"
            if str(h.item_type or "").lower() == "movie"
            else "series"
            if str(h.item_type or "").lower() == "series"
            else "episode"
        ),
        "item_title": item_title,
        "series_title": series_title,
        "path": path_val,
        "reason": display_reason,
        "status": status_display,
        "time": occurred,
    }
    extra_snapshot = h.extra_snapshot if isinstance(h.extra_snapshot, dict) else {}
    if resolved_series_id is not None:
        out["_series_id"] = int(resolved_series_id)
    out["_history_id"] = int(h.id) if h.id is not None else None
    if action_cap == "Deleted":
        out["_bulk_series_delete"] = bool(extra_snapshot.get("bulk_series_delete"))
        out["_bulk_delete_series_id"] = extra_snapshot.get("bulk_delete_series_id")
        out["_bulk_delete_series_title"] = str(extra_snapshot.get("bulk_delete_series_title") or "").strip() or None
    if action_cap == "Status":
        out["_status_source"] = status_src
    return out


def _is_series_create_row(row: dict) -> bool:
    """Episode (or series) Created row with a resolvable series id — any reason (sync, SeriesAdd, etc.)."""
    if str(row.get("action") or "").strip() != "Created":
        return False
    if row.get("_series_id") is None:
        return False
    item_type = str(row.get("item_type") or "").strip().lower()
    return item_type in {"episode", "series", "batch"} or item_type == ""


def _series_create_batch_labels(cluster: list[dict], series_title: str) -> tuple[str, str]:
    """Pick parent title (series name only) and reason from the dominant create reason."""
    reasons = [str(c.get("reason") or "").strip() for c in cluster if str(c.get("reason") or "").strip()]
    dominant = ""
    if reasons:
        counts: dict[str, int] = {}
        for r in reasons:
            counts[r] = counts.get(r, 0) + 1
        dominant = max(counts.items(), key=lambda kv: kv[1])[0]
    n = len(cluster)
    title = str(series_title or "Series").strip() or "Series"
    if dominant.lower() == "series added":
        reason = f"{n} episode placeholders after Sonarr SeriesAdd for {title}."
    elif dominant:
        reason = f"{n} episode placeholders for {title} ({dominant})."
    else:
        reason = f"{n} episode placeholders created for {title}."
    return title, reason


def _group_series_placeholder_creates(rows: list[dict], *, max_gap_sec: int = 300) -> list[dict]:
    """Collapse bursty per-episode Created rows for the same series into an expandable parent.

    Clusters by series_id + time gap only (any create reason: SeriesAdd, lite/full sync, etc.).
    Collection-named batches still need a create-time provenance stamp (deferred).
    """
    if not rows:
        return rows
    out: list[dict] = []
    i = 0
    parent_seq = 0
    while i < len(rows):
        row = rows[i]
        if not _is_series_create_row(row):
            out.append(row)
            i += 1
            continue

        series_id = row.get("_series_id")
        series_title = row.get("series_title") or row.get("item_title") or "Series"
        cluster: list[dict] = [row]
        j = i + 1
        while j < len(rows):
            nxt = rows[j]
            if not _is_series_create_row(nxt):
                break
            if nxt.get("_series_id") != series_id:
                break
            t_last = _parse_activity_dt(cluster[-1].get("time"))
            t_nxt = _parse_activity_dt(nxt.get("time"))
            if t_last is None or t_nxt is None:
                break
            delta = (t_last - t_nxt).total_seconds()
            if delta >= 0 and delta <= max_gap_sec:
                cluster.append(nxt)
                j += 1
            else:
                break

        if len(cluster) < 2:
            solo = dict(cluster[0])
            solo.pop("_series_id", None)
            solo.pop("_history_id", None)
            out.append(solo)
            i += 1
            continue

        parent_seq += 1
        newest = cluster[0]
        child_rows: list[dict] = []
        for c in cluster:
            child_rows.append(
                {
                    "id": c["id"],
                    "type": "placeholder",
                    "action": c.get("action"),
                    "item_type": c.get("item_type"),
                    "item_title": c.get("item_title"),
                    "series_title": c.get("series_title"),
                    "path": c.get("path") or "",
                    "reason": c.get("reason"),
                    "status": c.get("status"),
                    "time": c.get("time"),
                }
            )
        item_title, reason = _series_create_batch_labels(cluster, str(series_title or "Series"))
        parent_id = -(910_000_000 + parent_seq)
        out.append(
            {
                "id": parent_id,
                "type": "placeholder",
                "group_kind": "series_create_batch",
                "action": "Created",
                "item_type": "batch",
                "item_title": item_title,
                "series_title": str(series_title or ""),
                "path": "",
                "reason": reason,
                "status": "Created",
                "time": newest.get("time"),
                "children": child_rows,
                "_history_id": newest.get("_history_id"),
                "_cursor_time": cluster[-1].get("time"),
                "_cursor_id": cluster[-1].get("_history_id") or cluster[-1].get("id"),
            }
        )
        i = j
    return out


def _strip_placeholder_activity_internal_keys(row: dict) -> dict:
    cleaned = dict(row)
    for key in (
        "_status_source",
        "_series_id",
        "_history_id",
        "_bulk_series_delete",
        "_bulk_delete_series_id",
        "_bulk_delete_series_title",
        "_cursor_time",
        "_cursor_id",
    ):
        cleaned.pop(key, None)
    return cleaned


def _oldest_placeholder_cursor(items: list[dict]) -> tuple[str | None, int | None]:
    """Return (time, history_id) for the oldest leaf in a page of grouped rows."""
    best_time: str | None = None
    best_id: int | None = None
    best_dt: datetime | None = None

    def consider(time_s: Any, hist_id: Any) -> None:
        nonlocal best_time, best_id, best_dt
        dt = _parse_activity_dt(str(time_s) if time_s else None)
        if dt is None:
            return
        try:
            hid = int(hist_id) if hist_id is not None else None
        except (TypeError, ValueError):
            hid = None
        if best_dt is None or dt < best_dt or (dt == best_dt and hid is not None and (best_id is None or hid < best_id)):
            best_dt = dt
            best_time = str(time_s) if time_s else None
            best_id = hid

    for row in items:
        children = row.get("children")
        if isinstance(children, list) and children:
            if row.get("_cursor_time") is not None:
                consider(row.get("_cursor_time"), row.get("_cursor_id"))
            for child in children:
                if isinstance(child, dict):
                    consider(child.get("time"), child.get("id"))
            continue
        consider(row.get("time"), row.get("_history_id") or row.get("id"))
    return best_time, best_id


def _mark_series_bulk_delete_row(row: dict, *, series_title: str | None = None) -> dict:
    """Normalize series tombstone delete rows for UI (series name once, group_kind)."""
    out = dict(row)
    title = (
        str(series_title or "").strip()
        or str(out.get("_bulk_delete_series_title") or "").strip()
        or str(out.get("series_title") or "").strip()
        or str(out.get("item_title") or "").strip()
        or "Series"
    )
    out["group_kind"] = "series_bulk_delete"
    out["item_type"] = "series"
    out["item_title"] = title
    out["series_title"] = title
    return out


def _group_series_tombstone_placeholder_deletes(rows: list[dict], *, max_gap_sec: int = 90) -> list[dict]:
    """Collapse bursty per-episode delete rows from a series tombstone into one UI row."""
    if not rows:
        return rows
    out: list[dict] = []
    i = 0
    while i < len(rows):
        row = rows[i]
        if not bool(row.get("_bulk_series_delete")) or str(row.get("action") or "").strip() != "Deleted":
            out.append(row)
            i += 1
            continue

        base_ts = _parse_iso_datetime(row.get("time"))
        series_id = row.get("_bulk_delete_series_id")
        series_title = row.get("_bulk_delete_series_title") or row.get("series_title") or row.get("item_title")
        group = [row]
        j = i + 1
        while j < len(rows):
            nxt = rows[j]
            if not bool(nxt.get("_bulk_series_delete")) or str(nxt.get("action") or "").strip() != "Deleted":
                break
            if nxt.get("_bulk_delete_series_id") != series_id:
                break
            nxt_ts = _parse_iso_datetime(nxt.get("time"))
            if base_ts is None or nxt_ts is None:
                break
            if abs((base_ts - nxt_ts).total_seconds()) > max_gap_sec:
                break
            group.append(nxt)
            j += 1

        if len(group) <= 1:
            # Aggregate history row from materializer (count already in reason), or a lone episode delete.
            out.append(_mark_series_bulk_delete_row(row, series_title=str(series_title or "") or None))
            i += 1
            continue

        lead = _mark_series_bulk_delete_row(group[0], series_title=str(series_title or "") or None)
        removed_n = len(group)
        lead["id"] = f"series-bulk-delete-{series_id}-{lead.get('time')}"
        lead["reason"] = f"Series removed: tombstone cleanup, {removed_n} placeholders deleted"
        lead["path"] = ""
        lead["children"] = [
            {
                "id": c["id"],
                "type": "placeholder",
                "action": c.get("action"),
                "item_type": c.get("item_type"),
                "item_title": c.get("item_title"),
                "series_title": c.get("series_title"),
                "path": c.get("path") or "",
                "reason": c.get("reason"),
                "status": c.get("status"),
                "time": c.get("time"),
            }
            for c in group
        ]
        out.append(lead)
        i = j
    return out


def _placeholder_activity_page(
    session,
    *,
    limit: int,
    before_time: datetime | None = None,
    before_id: int | None = None,
) -> dict[str, Any]:
    """Paged placeholder timeline with server-side batch grouping."""
    q = session.query(PlaceholderActivityHistory)
    if before_time is not None:
        if before_id is not None:
            q = q.filter(
                or_(
                    PlaceholderActivityHistory.occurred_at < before_time,
                    and_(
                        PlaceholderActivityHistory.occurred_at == before_time,
                        PlaceholderActivityHistory.id < before_id,
                    ),
                )
            )
        else:
            q = q.filter(PlaceholderActivityHistory.occurred_at < before_time)
    elif before_id is not None:
        q = q.filter(PlaceholderActivityHistory.id < before_id)

    fetch_n = max(limit * 4, limit)
    rows = (
        q.order_by(
            PlaceholderActivityHistory.occurred_at.desc(),
            PlaceholderActivityHistory.id.desc(),
        )
        .limit(fetch_n + 1)
        .all()
    )
    has_more_raw = len(rows) > fetch_n
    rows = rows[:fetch_n]

    activity_list = [_activity_dict_from_history_row(session, h) for h in rows]
    deduped: list[dict] = []
    seen: set[tuple] = set()
    for row in sorted(activity_list, key=lambda x: (x.get("time") or "", x.get("id") or 0), reverse=True):
        key = (row.get("id"), row.get("action"), row.get("time"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    grouped = _group_calendar_placeholder_status_rows(deduped)
    grouped = _group_series_tombstone_placeholder_deletes(grouped)
    grouped = _group_series_placeholder_creates(grouped)

    page = grouped[:limit]
    next_time, next_id = _oldest_placeholder_cursor(page)
    has_more = len(grouped) > limit or has_more_raw
    items = [_strip_placeholder_activity_internal_keys(r) for r in page]
    return {
        "items": items,
        "has_more": bool(has_more and next_time is not None),
        "next_before_time": next_time,
        "next_before_id": next_id,
    }


@router.get("/api/activity/placeholders")
async def activity_placeholders(
    limit: int = Query(50, ge=1, le=200),
    before_time: Optional[str] = Query(None, description="ISO timestamp cursor (exclusive upper bound)"),
    before_id: Optional[int] = Query(None, ge=1, description="History id tie-break with before_time"),
):
    """Return placeholder timeline from append-only history (fast read; hooks populate the table).

    Response is a page envelope: ``{ items, has_more, next_before_time, next_before_id }``.
    """
    session = get_session()
    try:
        bt = _parse_activity_dt(before_time) if before_time else None
        return _placeholder_activity_page(
            session,
            limit=limit,
            before_time=bt,
            before_id=before_id,
        )
    finally:
        session.close()


def _arr_secondary_instance(instance_id: str | None, instance_key: str | None) -> bool:
    iid = str(instance_id or "").lower()
    key = str(instance_key or "").lower()
    return "secondary" in iid or iid.endswith(":secondary") or key.endswith("_secondary") or "secondary" in key


def _movie_merge_priority(movie: Movie) -> tuple[int, int, int]:
    """Lower tuple = preferred canonical row (primary instance, then rows with media)."""
    secondary = 1 if _arr_secondary_instance(getattr(movie, "instance_id", None), movie.instance_key) else 0
    has_media = 0 if (bool(movie.has_file) or bool(movie.has_placeholder)) else 1
    return (secondary, has_media, movie.id)


def _library_earliest_added_from_rows(rows: list[dict]) -> tuple[str | None, int | None]:
    """Earliest Placeholdarr insert (full timestamp) and matching row id for merged shelves."""
    best_at: datetime | None = None
    best_id: int | None = None
    for row in rows:
        dt = _parse_iso_datetime(row.get("created_at"))
        try:
            iid = int(row.get("item_id")) if row.get("item_id") is not None else None
        except (TypeError, ValueError):
            iid = None
        if dt is None:
            continue
        if (
            best_at is None
            or dt < best_at
            or (dt == best_at and iid is not None and (best_id is None or iid < best_id))
        ):
            best_at = dt
            best_id = iid
    if best_at is None:
        return None, best_id
    return _iso(best_at), best_id


def _apply_earliest_added_to_merged_row(merged: dict[str, Any], instance_rows: list[dict], *, id_prefix: str) -> None:
    created_at, item_id = _library_earliest_added_from_rows(instance_rows)
    if created_at:
        merged["created_at"] = created_at
    if item_id is not None:
        merged["item_id"] = item_id
        merged["id"] = f"{id_prefix}-{item_id}"


def _merge_movie_library_rows(entries: list[tuple[Movie, dict]]) -> list[dict]:
    """One grid row per TMDB id; merge stats across Radarr instances."""
    if not entries:
        return []
    buckets: dict[int, list[tuple[Movie, dict]]] = defaultdict(list)
    for movie, row in entries:
        buckets[int(movie.tmdbid)].append((movie, row))
    out: list[dict] = []
    for _tmdbid, group in buckets.items():
        group.sort(key=lambda mr: _movie_merge_priority(mr[0]))
        _, canon_row = group[0]
        if len(group) == 1:
            single = dict(canon_row)
            single["instance_label"] = None
            out.append(single)
            continue
        merged: dict[str, Any] = dict(canon_row)
        merged["has_file"] = any(bool(r["has_file"]) for _, r in group)
        merged["has_placeholder"] = any(bool(r["has_placeholder"]) for _, r in group)
        merged["has_missing"] = any(bool(r["has_missing"]) for _, r in group)
        merged["is_future"] = (not merged["has_missing"]) and any(bool(r["is_future"]) for _, r in group)
        merged["stats"] = {
            "downloaded": 1 if merged["has_file"] else 0,
            "placeholders": 1 if merged["has_placeholder"] else 0,
            "future": 1 if merged["is_future"] else 0,
            "missing": 1 if merged["has_missing"] else 0,
        }
        merged["instance_label"] = None
        _apply_earliest_added_to_merged_row(merged, [r for _, r in group], id_prefix="movie")
        out.append(merged)
    return out


def _series_merge_priority(series: Series) -> tuple[int, int, int]:
    secondary = 1 if _arr_secondary_instance(getattr(series, "instance_id", None), series.instance_key) else 0
    has_media = 0 if bool(series.has_files) else 1
    return (secondary, has_media, series.id)


def _series_counts_empty() -> dict[str, int]:
    return {
        "episode_total": 0,
        "episode_files": 0,
        "episode_placeholders": 0,
        "episode_future": 0,
        "episode_missing": 0,
    }


def _norm_movie_instance_key(value: str | None) -> str:
    key_raw = str(value or "").strip().lower()
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in key_raw).strip("_-")


def _arr_instances_json_effective(session) -> str:
    """Prefer DB-persisted ARR_INSTANCES_JSON so detail routes match Settings after save (runtime env can lag)."""
    row = session.query(AppConfig).filter(AppConfig.key == "ARR_INSTANCES_JSON").first()
    if row and row.value not in (None, ""):
        v = row.value
        if isinstance(v, str):
            s = v.strip()
        elif isinstance(v, (list, dict)):
            s = json.dumps(v)
        else:
            s = str(v).strip()
        if s:
            return s
    return str(getattr(settings, "ARR_INSTANCES_JSON", "") or "").strip()


def _ordered_radarr_instances(session) -> list[dict[str, Any]]:
    """Configured Radarr slots from effective ARR_INSTANCES_JSON (DB first)."""
    raw_str = _arr_instances_json_effective(session)
    inst = parse_configured_arr_instances_json(raw_str)
    rad = [x for x in inst if str(x.get("arr_type") or "").strip().lower() == "radarr"]
    if not rad:
        return []
    role_rank = {"primary": 0, "secondary": 1, "additional": 2}

    def _slot_sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
        pri = item.get("priority")
        try:
            p_int = int(pri) if pri is not None else -1
        except (TypeError, ValueError):
            p_int = -1
        role = str(item.get("role") or "").strip().lower()
        rr = role_rank.get(role, 5)
        ikey = str(item.get("instance_key") or "")
        return (p_int, rr, ikey)

    return sorted(rad, key=_slot_sort_key)


def _ordered_sonarr_instances(session) -> list[dict[str, Any]]:
    """Configured Sonarr slots from effective ARR_INSTANCES_JSON (DB first)."""
    raw_str = _arr_instances_json_effective(session)
    inst = parse_configured_arr_instances_json(raw_str)
    son = [x for x in inst if str(x.get("arr_type") or "").strip().lower() == "sonarr"]
    if not son:
        return []
    role_rank = {"primary": 0, "secondary": 1, "additional": 2}

    def _slot_sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
        pri = item.get("priority")
        try:
            p_int = int(pri) if pri is not None else -1
        except (TypeError, ValueError):
            p_int = -1
        role = str(item.get("role") or "").strip().lower()
        rr = role_rank.get(role, 5)
        ikey = str(item.get("instance_key") or "")
        return (p_int, rr, ikey)

    return sorted(son, key=_slot_sort_key)


def _append_local_rows_missing_from_slot_merge(
    merged: list[dict[str, Any]],
    raw: list[dict[str, Any]],
    id_key: str,
) -> list[dict[str, Any]]:
    """Append raw TMDB/TVDB sibling rows not already emitted from slot padding (key/alias drift vs ARR_INSTANCES_JSON)."""
    covered: set[int] = set()
    for m in merged:
        v = m.get(id_key)
        if isinstance(v, int):
            covered.add(int(v))
    out = list(merged)
    for row in raw:
        oid = row.get(id_key)
        if not isinstance(oid, int):
            continue
        if oid in covered:
            continue
        row_out = {k: v for k, v in row.items() if not str(k).startswith("_")}
        row_out["present"] = True
        out.append(row_out)
        covered.add(oid)
    return out


def _find_raw_hit_for_arr_slot(
    slot: dict[str, Any],
    by_key: dict[str, dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
) -> Optional[dict[str, Any]]:
    key = str(slot.get("instance_key") or "").strip().lower()
    if key and key in by_key:
        return by_key[key]
    for alias in slot.get("instance_key_aliases") or []:
        ak = _norm_movie_instance_key(str(alias))
        if ak and ak in by_key:
            return by_key[ak]
    sid = str(slot.get("instance_id") or "").strip().lower()
    if sid and sid in by_id:
        return by_id[sid]
    return None


def _movie_arr_instance_links_from_db(session, anchor: Movie) -> list[dict[str, Any]]:
    """Deep links to Radarr UI for every local row that shares this movie's TMDB id."""
    rows = (
        session.query(Movie)
        .filter(Movie.is_deleted == False, Movie.tmdbid == anchor.tmdbid)
        .all()
    )
    rows.sort(key=_movie_merge_priority)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in rows:
        link = _arr_item_link(
            item_type="movie",
            instance_key=m.instance_key,
            instance_id=getattr(m, "instance_id", None),
            title=m.title,
            payload=m.radarr_payload_raw if isinstance(m.radarr_payload_raw, dict) else None,
        )
        if not link or link in seen:
            continue
        seen.add(link)
        meta = _arr_instance_meta(m.instance_key, getattr(m, "instance_id", None))
        lbl = str(meta.get("label") or m.instance_key or "").strip() or "Radarr"
        ikey = _norm_movie_instance_key(m.instance_key)
        iid = str(getattr(m, "instance_id", None) or "").strip().lower()
        out.append(
            {
                "label": lbl,
                "url": link,
                "movie_id": m.id,
                "has_file": bool(m.has_file),
                "has_placeholder": bool(m.has_placeholder),
                "_instance_key": ikey,
                "_instance_id": iid,
            }
        )
    return out


def _movie_arr_instance_links(session, anchor: Movie) -> list[dict[str, Any]]:
    """Radarr UI links per configured instance when ARR_INSTANCES_JSON lists Radarr; pads missing titles with ``present: false``."""
    raw = _movie_arr_instance_links_from_db(session, anchor)
    rad_cfg = _ordered_radarr_instances(session)
    if not rad_cfg:
        return [{k: v for k, v in row.items() if not str(k).startswith("_")} for row in raw]

    by_key: dict[str, dict[str, Any]] = {}
    by_id: dict[str, dict[str, Any]] = {}
    for row in raw:
        k = str(row.get("_instance_key") or "").strip().lower()
        if k and k not in by_key:
            by_key[k] = row
        iid = str(row.get("_instance_id") or "").strip().lower()
        if iid and iid not in by_id:
            by_id[iid] = row

    merged: list[dict[str, Any]] = []
    for slot in rad_cfg:
        key = str(slot.get("instance_key") or "").strip().lower()
        hit = _find_raw_hit_for_arr_slot(slot, by_key, by_id)
        if hit:
            row = {k: v for k, v in hit.items() if not str(k).startswith("_")}
            row["present"] = True
            merged.append(row)
        else:
            base = _arr_base_url("movie", key, slot.get("instance_id"))
            merged.append(
                {
                    "label": str(slot.get("label") or key).strip() or "Radarr",
                    "url": base or "",
                    "present": False,
                    "has_file": False,
                    "has_placeholder": False,
                }
            )
    return _append_local_rows_missing_from_slot_merge(merged, raw, "movie_id")


def _episode_stats_for_series(session, series_id: int) -> dict[str, int]:
    """Episode counts for one series (reads materialized columns when available)."""
    series = session.get(Series, int(series_id))
    if series is None or bool(getattr(series, "is_deleted", False)):
        from services.series_episode_stats import series_counts_empty

        return series_counts_empty()
    if getattr(series, "stats_computed_at", None) is None:
        from services.series_episode_stats import compute_series_episode_stats, refresh_series_episode_stats

        refresh_series_episode_stats(session, {int(series_id)})
        session.flush()
    return series_stats_dict_from_row(series)


def _series_arr_instance_links_from_db(session, anchor: Series) -> list[dict[str, Any]]:
    """Deep links to Sonarr UI for every local row that shares this series' TVDB id."""
    rows = (
        session.query(Series)
        .filter(Series.is_deleted == False, Series.tvdbid == anchor.tvdbid)
        .all()
    )
    rows.sort(key=_series_merge_priority)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for s in rows:
        link = _arr_item_link(
            item_type="series",
            instance_key=s.instance_key,
            instance_id=getattr(s, "instance_id", None),
            title=s.title,
            payload=s.sonarr_payload_raw if isinstance(s.sonarr_payload_raw, dict) else None,
        )
        if not link or link in seen:
            continue
        seen.add(link)
        meta = _arr_instance_meta(s.instance_key, getattr(s, "instance_id", None))
        lbl = str(meta.get("label") or s.instance_key or "").strip() or "Sonarr"
        st = _episode_stats_for_series(session, s.id)
        ikey = _norm_movie_instance_key(s.instance_key)
        iid = str(getattr(s, "instance_id", None) or "").strip().lower()
        out.append(
            {
                "label": lbl,
                "url": link,
                "series_id": s.id,
                "episode_files": int(st["episode_files"]),
                "episode_placeholders": int(st["episode_placeholders"]),
                "episode_total": int(st["episode_total"]),
                "_instance_key": ikey,
                "_instance_id": iid,
            }
        )
    return out


def _series_arr_instance_links(session, anchor: Series) -> list[dict[str, Any]]:
    """Sonarr UI links per configured instance when ARR_INSTANCES_JSON lists Sonarr; pads missing shows with ``present: false``."""
    raw = _series_arr_instance_links_from_db(session, anchor)
    son_cfg = _ordered_sonarr_instances(session)
    if not son_cfg:
        return [{k: v for k, v in row.items() if not str(k).startswith("_")} for row in raw]

    by_key: dict[str, dict[str, Any]] = {}
    by_id: dict[str, dict[str, Any]] = {}
    for row in raw:
        k = str(row.get("_instance_key") or "").strip().lower()
        if k and k not in by_key:
            by_key[k] = row
        iid = str(row.get("_instance_id") or "").strip().lower()
        if iid and iid not in by_id:
            by_id[iid] = row

    merged: list[dict[str, Any]] = []
    for slot in son_cfg:
        key = str(slot.get("instance_key") or "").strip().lower()
        hit = _find_raw_hit_for_arr_slot(slot, by_key, by_id)
        if hit:
            row = {k: v for k, v in hit.items() if not str(k).startswith("_")}
            row["present"] = True
            merged.append(row)
        else:
            base = _arr_base_url("series", key, slot.get("instance_id"))
            merged.append(
                {
                    "label": str(slot.get("label") or key).strip() or "Sonarr",
                    "url": base or "",
                    "present": False,
                    "episode_files": 0,
                    "episode_placeholders": 0,
                    "episode_total": 0,
                }
            )
    return _append_local_rows_missing_from_slot_merge(merged, raw, "series_id")


def _merge_series_library_rows(
    entries: list[tuple[Series, dict]],
    series_episode_counts: dict[int, Any],
) -> list[dict]:
    """One grid row per TVDB id; aggregate episode stats across Sonarr instances."""
    if not entries:
        return []
    buckets: dict[int, list[tuple[Series, dict]]] = defaultdict(list)
    for series, row in entries:
        buckets[int(series.tvdbid)].append((series, row))
    out: list[dict] = []
    for _tvdbid, group in buckets.items():
        group.sort(key=lambda sr: _series_merge_priority(sr[0]))
        _, canon_row = group[0]
        if len(group) == 1:
            single = dict(canon_row)
            single["instance_label"] = None
            out.append(single)
            continue
        agg = _series_counts_empty()
        for s, _r in group:
            c = series_episode_counts.get(s.id) or _series_counts_empty()
            for k in agg:
                agg[k] += int(c.get(k) or 0)
        series_unresolved = max(
            int(agg["episode_total"]) - int(agg["episode_files"]) - int(agg["episode_placeholders"]),
            0,
        )
        series_is_future = series_unresolved > 0 and int(agg["episode_missing"]) == 0 and int(agg["episode_future"]) > 0
        series_has_missing = int(agg["episode_missing"]) > 0
        merged: dict[str, Any] = dict(canon_row)
        merged["stats"] = agg
        merged["has_file"] = any(bool(s.has_files) for s, _ in group)
        merged["has_placeholder"] = int(agg["episode_placeholders"]) > 0
        merged["has_missing"] = series_has_missing
        merged["is_future"] = series_is_future
        merged["instance_label"] = None
        _apply_earliest_added_to_merged_row(merged, [r for _, r in group], id_prefix="series")
        out.append(merged)
    return out


_POSTER_JPEG = "poster.jpg"
_LIBRARY_POSTER_CACHE = "public, max-age=604800"


def _movie_poster_jpeg_path(movie: Movie) -> str | None:
    """Absolute path to placeholder ``poster.jpg`` when the title has on-disk placeholder media."""
    from services.placeholder_poster_art import POSTER_JPEG

    if not bool(getattr(movie, "has_placeholder", False)):
        return None
    fp = str(getattr(movie, "placeholder_filepath", "") or "").strip()
    if fp:
        candidate = os.path.join(os.path.dirname(os.path.abspath(fp)), _POSTER_JPEG)
        if os.path.isfile(candidate):
            return candidate
    try:
        from services.placeholders import movie_placeholder_path

        candidate = os.path.join(os.path.dirname(os.path.abspath(movie_placeholder_path(movie))), _POSTER_JPEG)
        if os.path.isfile(candidate):
            return candidate
    except Exception:
        pass
    return None


def _local_poster_file_for_movie(movie: Movie, *, try_catalog_download: bool = False) -> str | None:
    from services.placeholder_poster_art import (
        POSTER_GRID_JPEG,
        resolve_library_grid_poster_path,
        write_library_grid_poster,
    )

    candidate = _movie_poster_jpeg_path(movie)
    if candidate:
        return resolve_library_grid_poster_path(
            candidate,
            meta_key="poster",
            catalog_poster_url=getattr(movie, "remote_poster", None),
        )
    if not try_catalog_download:
        return None
    remote = getattr(movie, "remote_poster", None)
    fp = str(getattr(movie, "placeholder_filepath", "") or "").strip()
    if not remote or not fp:
        return None
    folder = os.path.dirname(os.path.abspath(fp))
    if not folder:
        return None
    grid = os.path.join(folder, POSTER_GRID_JPEG)
    if os.path.isfile(grid):
        return grid
    if write_library_grid_poster(folder, remote) and os.path.isfile(grid):
        return grid
    return None


def _local_poster_file_for_series(series: Series, *, try_catalog_download: bool = False) -> str | None:
    from services.placeholder_poster_art import (
        POSTER_GRID_JPEG,
        resolve_library_grid_poster_path,
        write_library_grid_poster,
    )

    folder = str(getattr(series, "placeholder_folder", "") or "").strip()
    if not folder:
        return None
    folder_abs = os.path.abspath(folder)
    candidate = os.path.join(folder_abs, _POSTER_JPEG)
    if os.path.isfile(candidate):
        return resolve_library_grid_poster_path(
            candidate,
            meta_key="series_poster",
            catalog_poster_url=getattr(series, "remote_poster", None),
        )
    if not try_catalog_download:
        return None
    grid = os.path.join(folder_abs, POSTER_GRID_JPEG)
    if os.path.isfile(grid):
        return grid
    remote = getattr(series, "remote_poster", None)
    if remote and write_library_grid_poster(folder_abs, remote) and os.path.isfile(grid):
        return grid
    return None


def _placeholder_poster_blocks_remote(item_type: str, entity: Movie | Series) -> bool:
    """Block raw TMDB URLs only when composited placeholder poster.jpg exists without a grid file."""
    from services.placeholder_poster_art import _poster_on_disk_is_composited, poster_overlay_mode

    poster_path: str | None
    if item_type == "movie":
        poster_path = _movie_poster_jpeg_path(entity)
    elif item_type == "series":
        folder = str(getattr(entity, "placeholder_folder", "") or "").strip()
        if not folder:
            return False
        poster_path = os.path.join(os.path.abspath(folder), _POSTER_JPEG)
        if not os.path.isfile(poster_path):
            return False
    else:
        return False

    if not poster_path or not os.path.isfile(poster_path):
        return False
    if _poster_on_disk_is_composited(poster_path):
        return True
    return poster_overlay_mode() != "off"


def _library_poster_local_file(
    item_type: str,
    entity: Movie | Series,
    *,
    try_catalog_download: bool = False,
) -> str | None:
    if item_type == "movie":
        return _local_poster_file_for_movie(entity, try_catalog_download=try_catalog_download)
    if item_type == "series":
        return _local_poster_file_for_series(entity, try_catalog_download=try_catalog_download)
    return None


def _library_poster_cache_token(path: str) -> int:
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return 0


def _library_poster_api_url(
    item_type: str,
    item_id: int,
    entity: Movie | Series,
    *,
    try_catalog_download: bool = False,
) -> str | None:
    local_path = _library_poster_local_file(item_type, entity, try_catalog_download=try_catalog_download)
    if not local_path:
        return None
    token = _library_poster_cache_token(local_path)
    return f"/api/library/poster/{item_type}/{int(item_id)}?v={token}"


def _effective_library_poster_url(item_type: str, item_id: int, entity: Movie | Series, remote: str | None) -> str | None:
    """Library grids use cacheable ``/api/library/poster`` (``poster-grid.jpg`` when overlays are on)."""
    local_url = _library_poster_api_url(item_type, item_id, entity)
    if local_url:
        return local_url
    # Catalog-only rows (no poster.jpg yet) may use remote_poster; composited placeholder art must not.
    if _placeholder_poster_blocks_remote(item_type, entity):
        return None
    return remote


def _library_poster_url_for_list(
    item_type: str,
    item_id: int,
    *,
    use_local_poster_api: bool,
    remote: str | None,
    cache_bust: datetime | None = None,
) -> str | None:
    """Fast list-view poster URL — no filesystem probes or HTTP downloads during list builds."""
    if use_local_poster_api:
        token = 0
        if isinstance(cache_bust, datetime):
            try:
                token = int(cache_bust.timestamp())
            except (ValueError, OSError):
                token = 0
        return f"/api/library/poster/{item_type}/{int(item_id)}?v={token}"
    remote_text = str(remote or "").strip()
    return remote_text or None


def _resolve_library_poster_url(
    item_type: str,
    item_id: int,
    entity: Movie | Series,
    remote: str | None,
    *,
    summary: bool,
    use_local_poster_api: bool,
) -> str | None:
    if summary:
        return _library_poster_url_for_list(
            item_type,
            item_id,
            use_local_poster_api=use_local_poster_api,
            remote=remote,
            cache_bust=getattr(entity, "updated_at", None),
        )
    return _effective_library_poster_url(item_type, item_id, entity, remote)


def _build_library_payload(
    client_etag: str,
    limit: int,
    summary: bool,
    media_type: str | None,
) -> tuple[str, dict[str, Any] | None]:
    """Build library JSON off the event loop. Returns ``(etag, None)`` for 304."""
    want_movies = True
    want_series = True
    mt = str(media_type or "").strip().lower()
    if mt in {"movie", "movies"}:
        want_series = False
    elif mt in {"series", "tv"}:
        want_movies = False

    session = get_session()
    try:
        try:
            session.execute(text("SET LOCAL lock_timeout = '30s'"))
        except Exception:
            pass
        etag = library_etag_for_shelf(session, mt if mt else "all")
        if client_etag and client_etag == etag:
            return etag, None

        series_episode_counts: dict[int, dict[str, int]] = {}

        movie_entries: list[tuple[Movie, dict]] = []

        if want_movies:
            movies = (
                session.query(Movie)
                .filter(Movie.is_deleted == False)
                .order_by(Movie.updated_at.desc(), Movie.title.asc())
                .limit(limit)
                .all()
            )
        else:
            movies = []
        for movie in movies:
            instance_meta = _arr_instance_meta(movie.instance_key, getattr(movie, "instance_id", None))
            movie_unresolved = (not bool(movie.has_file)) and (not bool(movie.has_placeholder))
            movie_is_future = movie_unresolved and movie_row_is_future_outside_lookahead(movie)
            movie_has_missing = movie_unresolved and not movie_is_future
            arr_link = _arr_item_link(
                item_type="movie",
                instance_key=movie.instance_key,
                instance_id=getattr(movie, "instance_id", None),
                title=movie.title,
                payload=movie.radarr_payload_raw if isinstance(movie.radarr_payload_raw, dict) else None,
            )
            row = {
                "id": f"movie-{movie.id}",
                "item_id": movie.id,
                "type": "movie",
                "title": movie.title,
                "year": movie.year,
                "tmdb_id": int(movie.tmdbid or 0) or None,
                "tvdb_id": None,
                "imdb_id": movie.imdbid,
                "poster_url": _resolve_library_poster_url(
                    "movie",
                    int(movie.id),
                    movie,
                    movie.remote_poster,
                    summary=summary,
                    use_local_poster_api=bool(movie.has_placeholder),
                ),
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
                "created_at": _iso(getattr(movie, "created_at", None)),
                "updated_at": _iso(getattr(movie, "updated_at", None)),
                "overview": movie.radarr_overview,
                "stats": {
                    "downloaded": 1 if movie.has_file else 0,
                    "placeholders": 1 if movie.has_placeholder else 0,
                    "future": 1 if movie_is_future else 0,
                    "missing": 1 if movie_has_missing else 0,
                },
            }
            if summary:
                row.pop("overview", None)
                row.pop("backdrop_url", None)
            movie_entries.append((movie, row))

        series_entries: list[tuple[Series, dict]] = []

        if want_series:
            series_rows = (
                session.query(Series)
                .filter(Series.is_deleted == False)
                .order_by(Series.updated_at.desc(), Series.title.asc())
                .limit(limit)
                .all()
            )
            series_episode_counts = series_episode_counts_map(session, series_rows)
        else:
            series_rows = []
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
            series_folder = str(getattr(series, "placeholder_folder", "") or "").strip()
            row = {
                "id": f"series-{series.id}",
                "item_id": series.id,
                "type": "series",
                "title": series.title,
                "year": series.year,
                "tmdb_id": int(getattr(series, "sonarr_tmdbid", 0) or 0) or None,
                "tvdb_id": int(series.tvdbid or 0) or None,
                "imdb_id": series.imdbid,
                "network": getattr(series, "sonarr_network", None),
                "poster_url": _resolve_library_poster_url(
                    "series",
                    int(series.id),
                    series,
                    series.remote_poster,
                    summary=summary,
                    use_local_poster_api=bool(series_folder),
                ),
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
                "created_at": _iso(getattr(series, "created_at", None)),
                "updated_at": _iso(getattr(series, "updated_at", None)),
                "overview": series.sonarr_series_overview,
                "stats": counts,
            }
            if summary:
                row.pop("overview", None)
                row.pop("backdrop_url", None)
            series_entries.append((series, row))

        items: list[dict] = []
        items.extend(_merge_movie_library_rows(movie_entries))
        items.extend(_merge_series_library_rows(series_entries, series_episode_counts))

        items.sort(
            key=lambda item: (
                0 if item.get("has_placeholder") else 1,
                str(item.get("title") or "").lower(),
            )
        )

        total = len(items)
        return etag, {
            "items": items[:limit],
            "count": min(total, limit),
            "total": total,
            "version": int(etag) if etag.isdigit() else etag,
        }
    finally:
        session.close()


def _library_poster_file_response(path: str) -> FileResponse:
    cache_token = library_poster_cache_token(path)
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={
            "Cache-Control": _LIBRARY_POSTER_CACHE,
            "ETag": f'"{cache_token}"',
        },
    )


def _resolve_library_poster_response(kind: str, item_id: int):
    """Read-only poster serve: cached path lookup, no catalog downloads on request."""
    normalized = str(kind or "").strip().lower()
    if normalized not in {"movie", "series"}:
        return JSONResponse({"ok": False, "message": "invalid item type"}, status_code=400)

    local_path, remote = load_library_poster_path(normalized, int(item_id))
    if local_path:
        return _library_poster_file_response(local_path)
    if remote:
        return RedirectResponse(remote, status_code=302)
    return JSONResponse({"ok": False, "message": "poster not found"}, status_code=404)


@router.get("/api/library/poster/{item_type}/{item_id}")
async def library_poster(item_type: str, item_id: int):
    """Serve pre-materialized poster-grid.jpg (or poster.jpg). Never downloads art on this hot path."""
    return await asyncio.to_thread(_resolve_library_poster_response, item_type, item_id)


@router.get("/api/library/version")
async def library_version():
    """Lightweight shelf version counters for conditional library polling."""
    session = get_session()
    try:
        return get_library_versions(session)
    finally:
        session.close()


@router.get("/api/library")
async def library(
    request: Request,
    response: Response,
    limit: int = Query(50000, ge=1, le=50000),
    summary: bool = Query(False),
    media_type: str | None = Query(None, description="Optional filter: movie or series"),
):
    """Return mixed movie/series library rows with poster and placeholder stats.

    When ``summary`` is true, omit large text fields (``overview``, ``backdrop_url``) to shrink JSON for grid polling.
    When ``media_type`` is set, only query that shelf (faster load for Movies vs TV pages).
    Supports ``If-None-Match`` with the shelf version for 304 responses.
    """
    client_etag = str(request.headers.get("if-none-match") or "").strip().strip('"')
    etag, payload = await asyncio.to_thread(
        _build_library_payload,
        client_etag,
        limit,
        summary,
        media_type,
    )
    if payload is None:
        return Response(status_code=304)
    response.headers["ETag"] = f'"{etag}"'
    return payload


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
            "arr_instance_links": _movie_arr_instance_links(session, movie) or [],
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
            "arr_instance_links": _series_arr_instance_links(session, series) or [],
        }
    finally:
        session.close()


def _scoped_movie_placeholder_ids(session, movie_id: int) -> list[int]:
    rows = (
        session.query(Placeholder.id)
        .filter(
            Placeholder.movie_id == int(movie_id),
            Placeholder.has_placeholder == True,  # noqa: E712
        )
        .all()
    )
    return [int(r[0]) for r in rows if r and r[0] is not None]


def _scoped_episode_placeholder_ids(session, episode_id: int) -> list[int]:
    rows = (
        session.query(Placeholder.id)
        .filter(
            Placeholder.episode_id == int(episode_id),
            Placeholder.has_placeholder == True,  # noqa: E712
        )
        .all()
    )
    return [int(r[0]) for r in rows if r and r[0] is not None]


def _scoped_series_placeholder_ids(session, series_id: int) -> list[int]:
    rows = (
        session.query(Placeholder.id)
        .join(Episode, Episode.id == Placeholder.episode_id)
        .join(Season, Season.id == Episode.season_id)
        .filter(
            Season.series_id == int(series_id),
            Placeholder.has_placeholder == True,  # noqa: E712
        )
        .all()
    )
    return [int(r[0]) for r in rows if r and r[0] is not None]


@router.post("/api/library/movie/{movie_id}/refresh-placeholder")
async def refresh_movie_placeholder(movie_id: int):
    session = get_session()
    try:
        movie = session.query(Movie).filter(Movie.id == int(movie_id), Movie.is_deleted == False).first()  # noqa: E712
        if not movie:
            return JSONResponse({"ok": False, "message": "Movie not found"}, status_code=404)
    finally:
        session.close()
    from services.source_of_truth.entity_reconcile import enqueue_entity_reconcile

    out = enqueue_entity_reconcile(
        entity_type="movie",
        entity_id=int(movie_id),
        source=f"library_movie:{movie_id}",
    )
    return {
        "ok": bool(out.get("ok", True)),
        "job_id": out.get("job_id"),
        "step_label": out.get("step_label"),
        "reused": bool(out.get("reused")),
    }


@router.post("/api/library/series/{series_id}/refresh-placeholder")
async def refresh_series_placeholder(series_id: int):
    session = get_session()
    try:
        series = session.query(Series).filter(Series.id == int(series_id), Series.is_deleted == False).first()  # noqa: E712
        if not series:
            return JSONResponse({"ok": False, "message": "Series not found"}, status_code=404)
    finally:
        session.close()
    from services.source_of_truth.entity_reconcile import enqueue_entity_reconcile

    out = enqueue_entity_reconcile(
        entity_type="series",
        entity_id=int(series_id),
        source=f"library_series:{series_id}",
    )
    return {
        "ok": bool(out.get("ok", True)),
        "job_id": out.get("job_id"),
        "step_label": out.get("step_label"),
        "reused": bool(out.get("reused")),
    }


@router.post("/api/library/episode/{episode_id}/refresh-placeholder")
async def refresh_episode_placeholder(episode_id: int):
    session = get_session()
    try:
        episode = session.query(Episode).filter(Episode.id == int(episode_id), Episode.is_deleted == False).first()  # noqa: E712
        if not episode:
            return JSONResponse({"ok": False, "message": "Episode not found"}, status_code=404)
    finally:
        session.close()
    from services.source_of_truth.entity_reconcile import enqueue_entity_reconcile

    out = enqueue_entity_reconcile(
        entity_type="episode",
        entity_id=int(episode_id),
        source=f"library_episode:{episode_id}",
    )
    return {
        "ok": bool(out.get("ok", True)),
        "job_id": out.get("job_id"),
        "step_label": out.get("step_label"),
        "reused": bool(out.get("reused")),
    }


@router.get("/api/library/reconcile-jobs/{job_id}")
async def get_entity_reconcile_job(job_id: int):
    session = get_session()
    try:
        from services.postgres.models import Job
        from services.source_of_truth.entity_reconcile import (
            ENTITY_RECONCILE_JOB_TYPE,
            reconcile_job_status_view,
        )

        job = session.query(Job).filter(Job.id == int(job_id)).first()
        if not job or str(job.job_type) != ENTITY_RECONCILE_JOB_TYPE:
            return JSONResponse({"ok": False, "message": "Reconcile job not found"}, status_code=404)
        return reconcile_job_status_view(job)
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
    lookahead_before = int(getattr(settings, "CALENDAR_LOOKAHEAD_DAYS", 30) or 30)
    include_specials_before = bool(getattr(settings, "INCLUDE_SPECIALS", False))
    apply_scope_raw = payload.get("apply_scope") if isinstance(payload, dict) else None
    apply_scope = (
        str(apply_scope_raw).strip().lower()
        if isinstance(apply_scope_raw, str) and str(apply_scope_raw).strip()
        else None
    )
    if apply_scope not in {None, "now", "next_full_sync", "future"}:
        apply_scope = None
    result = save_settings(
        values,
        partial=partial,
        context=context if isinstance(context, dict) else None,
        apply_scope=apply_scope,
    )
    after_arr_fingerprint = _arr_endpoint_fingerprint()
    arr_endpoints_changed = before_arr_fingerprint != after_arr_fingerprint
    if result.get("ok") and not partial and not was_setup_complete and bool((result.get("status") or {}).get("setup_complete")):
        _launch_post_onboarding_startup_sync()
    elif result.get("ok") and not partial and arr_endpoints_changed:
        _launch_arr_change_full_sync(reason="arr_endpoint_changed")
    if result.get("ok"):
        lookahead_after = int(getattr(settings, "CALENDAR_LOOKAHEAD_DAYS", 30) or 30)
        include_specials_after = bool(getattr(settings, "INCLUDE_SPECIALS", False))
        if lookahead_before != lookahead_after or include_specials_before != include_specials_after:
            try:
                from services.series_episode_stats_hooks import schedule_full_series_stats_refresh

                schedule_full_series_stats_refresh()
            except Exception as exc:
                logger.warning(
                    "Could not schedule full series stats refresh after settings change: %s",
                    exc,
                    extra={"emoji_type": "warning"},
                )
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
    credential_key = str(payload.get("credential_key") or "").strip() or None
    instance_id = str(payload.get("instance_id") or "").strip() or None

    if service not in {"plex", "jellyfin", "emby", "radarr", "sonarr"}:
        return JSONResponse(content={"ok": False, "message": "unsupported service"}, status_code=400)
    if not url:
        return JSONResponse(content={"ok": False, "message": "url is required"}, status_code=400)

    resolved, resolve_err = resolve_integration_test_credential(
        service=service,
        url=url,
        credential=credential,
        credential_key=credential_key,
        instance_id=instance_id,
    )
    if resolve_err or not resolved:
        return JSONResponse(
            content={"ok": False, "message": resolve_err or "credential is required"},
            status_code=400,
        )

    result = test_integration_connection(service=service, url=url, token_or_key=resolved)
    status_code = 200 if result.get("ok") else 400
    return JSONResponse(content=result, status_code=status_code)


@router.get("/api/logs")
async def logs(
    tail: int = Query(200, ge=1, le=2000),
    level: str = Query("all"),
    since_id: int | None = Query(None, ge=0),
):
    """Tail logs from the in-process live buffer, or fall back to the log file on cold start."""
    from services.live_log_buffer import LIVE_LOG_BUFFER, live_log_payload

    log_file = _latest_runtime_log_file()
    file_name = os.path.basename(log_file) if log_file else None

    if since_id is not None:
        payload = live_log_payload(tail=tail, level=level, since_id=since_id)
        payload["file"] = file_name
        return payload

    if LIVE_LOG_BUFFER.latest_id() > 0:
        payload = live_log_payload(tail=tail, level=level, since_id=None)
        payload["file"] = file_name
        return payload

    if not log_file:
        return {"lines": [], "file": None, "latest_id": 0, "source": "file"}

    try:
        lines = await asyncio.to_thread(_read_runtime_log_tail, log_file, tail=tail, level=level)
    except FileNotFoundError:
        return {"lines": [], "file": None, "latest_id": 0, "source": "file"}

    return {
        "lines": lines,
        "file": file_name,
        "capture_level": "FULL",
        "latest_id": 0,
        "source": "file",
    }


@router.get("/api/logs/stream")
async def logs_stream(
    request: Request,
    since_id: int = Query(0, ge=0),
    level: str = Query("all"),
):
    """Server-sent events stream of new log lines from the live buffer."""
    from services.live_log_buffer import LIVE_LOG_BUFFER

    async def generate():
        last_id = max(0, int(since_id))
        while True:
            if await request.is_disconnected():
                break
            entries = await asyncio.to_thread(
                lambda cursor=last_id, filter_level=level: LIVE_LOG_BUFFER.get_since(
                    cursor,
                    level=filter_level,
                )
            )
            for entry in entries:
                last_id = entry.id
                yield f"data: {json.dumps({'id': entry.id, 'line': entry.line})}\n\n"
            await asyncio.sleep(0.35)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/events")
async def dashboard_events_stream(request: Request):
    """Server-sent events for dashboard liveness and cheap state deltas."""
    from services.dashboard_events import iter_dashboard_events

    async def generate():
        async for event in iter_dashboard_events():
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
