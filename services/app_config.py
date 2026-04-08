from __future__ import annotations

import json
from collections import OrderedDict
from datetime import datetime, timezone
from urllib.parse import urlparse
from typing import Any

from sqlalchemy import func

from core.config import settings
from services.postgres.db import get_session
from services.postgres.models import AppConfig


SETUP_COMPLETED_KEY = "APP_SETUP_COMPLETED_AT"


SETTINGS_SCHEMA: "OrderedDict[str, dict[str, Any]]" = OrderedDict(
    [
        (
            "ENABLE_PLEX",
            {
                "section": "Integrations",
                "label": "Enable Plex",
                "description": "Enable Plex integration for metadata updates and playback/import workflows. If disabled, Plex URL/token/section IDs can stay blank.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "PLEX_URL",
            {
                "section": "Integrations",
                "label": "Plex URL",
                "description": "Base Plex URL, for example http://plex.local:32400. Needed only when Plex is enabled.",
                "type": "url",
                "required": False,
                "restart_required": False,
            },
        ),
        (
            "PLEX_TOKEN",
            {
                "section": "Integrations",
                "label": "Plex Token",
                "description": "Plex authentication token used for API requests. Required only when Plex is enabled.",
                "type": "string",
                "required": False,
                "secret": True,
                "restart_required": False,
            },
        ),
        (
            "PLEX_MOVIE_SECTION_ID",
            {
                "section": "Integrations",
                "label": "Plex Movie Section ID",
                "description": "Numeric Plex library section ID for movie refresh calls. Required when Plex is enabled.",
                "type": "int",
                "required": False,
                "min": 1,
                "restart_required": False,
            },
        ),
        (
            "PLEX_TV_SECTION_ID",
            {
                "section": "Integrations",
                "label": "Plex TV Section ID",
                "description": "Numeric Plex library section ID for TV refresh calls. Required when Plex is enabled.",
                "type": "int",
                "required": False,
                "min": 1,
                "restart_required": False,
            },
        ),
        (
            "ENABLE_JELLYFIN",
            {
                "section": "Integrations",
                "label": "Enable Jellyfin",
                "description": "Enable Jellyfin integration for metadata refresh and playback-driven actions. If disabled, Jellyfin fields can stay blank.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "JELLYFIN_URL",
            {
                "section": "Integrations",
                "label": "Jellyfin URL",
                "description": "Base Jellyfin URL, for example http://jellyfin.local:8096. Needed only when Jellyfin is enabled.",
                "type": "url",
                "required": False,
                "restart_required": False,
            },
        ),
        (
            "JELLYFIN_TOKEN",
            {
                "section": "Integrations",
                "label": "Jellyfin Token",
                "description": "Jellyfin API token used for authenticated requests. Required only when Jellyfin is enabled.",
                "type": "string",
                "required": False,
                "secret": True,
                "restart_required": False,
            },
        ),
        (
            "ENABLE_EMBY",
            {
                "section": "Integrations",
                "label": "Enable Emby",
                "description": "Enable Emby integration for metadata refresh and playback-driven actions. If disabled, Emby fields can stay blank.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "EMBY_URL",
            {
                "section": "Integrations",
                "label": "Emby URL",
                "description": "Base Emby URL, for example http://emby.local:8096. Needed only when Emby is enabled.",
                "type": "url",
                "required": False,
                "restart_required": False,
            },
        ),
        (
            "EMBY_TOKEN",
            {
                "section": "Integrations",
                "label": "Emby Token",
                "description": "Emby API token used for authenticated requests. Required only when Emby is enabled.",
                "type": "string",
                "required": False,
                "secret": True,
                "restart_required": False,
            },
        ),
        (
            "RADARR_URL",
            {
                "section": "Integrations",
                "label": "Radarr URL",
                "description": "Standard Radarr base URL, for example http://radarr.local:7878/api/v3 or http://radarr.local:7878 (both accepted). Leave blank if Radarr is not used.",
                "type": "url",
                "required": False,
                "restart_required": False,
            },
        ),
        (
            "RADARR_API_KEY",
            {
                "section": "Integrations",
                "label": "Radarr API Key",
                "description": "API key used to authenticate standard Radarr requests. Required when Radarr URL is configured.",
                "type": "string",
                "required": False,
                "secret": True,
                "restart_required": False,
            },
        ),
        (
            "RADARR_4K_URL",
            {
                "section": "Integrations",
                "label": "Radarr 4K URL",
                "description": "Optional second Radarr instance URL for 4K media workflows. Leave blank to disable 4K Radarr support.",
                "type": "url",
                "required": False,
                "restart_required": False,
            },
        ),
        (
            "RADARR_4K_API_KEY",
            {
                "section": "Integrations",
                "label": "Radarr 4K API Key",
                "description": "API key for the optional 4K Radarr instance. Required only when Radarr 4K URL is configured.",
                "type": "string",
                "required": False,
                "secret": True,
                "restart_required": False,
            },
        ),
        (
            "SONARR_URL",
            {
                "section": "Integrations",
                "label": "Sonarr URL",
                "description": "Standard Sonarr base URL, for example http://sonarr.local:8989/api/v3 or http://sonarr.local:8989 (both accepted). Leave blank if Sonarr is not used.",
                "type": "url",
                "required": False,
                "restart_required": False,
            },
        ),
        (
            "SONARR_API_KEY",
            {
                "section": "Integrations",
                "label": "Sonarr API Key",
                "description": "API key used to authenticate standard Sonarr requests. Required when Sonarr URL is configured.",
                "type": "string",
                "required": False,
                "secret": True,
                "restart_required": False,
            },
        ),
        (
            "SONARR_4K_URL",
            {
                "section": "Integrations",
                "label": "Sonarr 4K URL",
                "description": "Optional second Sonarr instance URL for 4K media workflows. Leave blank to disable 4K Sonarr support.",
                "type": "url",
                "required": False,
                "restart_required": False,
            },
        ),
        (
            "SONARR_4K_API_KEY",
            {
                "section": "Integrations",
                "label": "Sonarr 4K API Key",
                "description": "API key for the optional 4K Sonarr instance. Required only when Sonarr 4K URL is configured.",
                "type": "string",
                "required": False,
                "secret": True,
                "restart_required": False,
            },
        ),
        (
            "ARR_INSTANCES_JSON",
            {
                "section": "Integrations",
                "label": "ARR Instances JSON (Advanced)",
                "description": "Optional JSON array for many named ARR instances. When set, this overrides fixed std/4K ARR URL/API-key pairs.",
                "type": "string",
                "required": False,
                "restart_required": False,
            },
        ),
        (
            "LIBRARY_ROOT",
            {
                "section": "Paths",
                "label": "Library Root",
                "description": "Optional shared base path for derived folders: movies, tv, movies-4k, and tv-4k. Use this for simple setups; explicit folder fields below can override any derived path.",
                "type": "path",
                "required": False,
                "restart_required": False,
            },
        ),
        (
            "ENABLE_STANDARD_PROFILE",
            {
                "section": "Paths",
                "label": "Use Standard Profile",
                "description": "Generate and use standard (non-4K) library folders from Library Root when available.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "ENABLE_4K_PROFILE",
            {
                "section": "Paths",
                "label": "Use 4K Profile",
                "description": "Generate and use 4K library folders from Library Root. Disable if your setup does not separate 4K media.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "ENABLE_ANIME_PROFILE",
            {
                "section": "Paths",
                "label": "Use Anime Profile",
                "description": "Generate and use an anime TV folder from Library Root for dedicated anime library targeting.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "MOVIE_LIBRARY_FOLDER",
            {
                "section": "Paths",
                "label": "Movie Library Folder",
                "description": "Optional explicit movies folder for placeholders. If blank and Library Root is set, Placeholdarr derives this automatically.",
                "type": "path",
                "required": False,
                "restart_required": False,
            },
        ),
        (
            "TV_LIBRARY_FOLDER",
            {
                "section": "Paths",
                "label": "TV Library Folder",
                "description": "Optional explicit TV folder for placeholders. If blank and Library Root is set, Placeholdarr derives this automatically.",
                "type": "path",
                "required": False,
                "restart_required": False,
            },
        ),
        (
            "MOVIE_LIBRARY_4K_FOLDER",
            {
                "section": "Paths",
                "label": "Movie Library 4K Folder",
                "description": "Optional explicit 4K movies folder. If blank and Library Root is set, Placeholdarr derives a movies-4k path.",
                "type": "path",
                "required": False,
                "restart_required": False,
            },
        ),
        (
            "TV_LIBRARY_4K_FOLDER",
            {
                "section": "Paths",
                "label": "TV Library 4K Folder",
                "description": "Optional explicit 4K TV folder. If blank and Library Root is set, Placeholdarr derives a tv-4k path.",
                "type": "path",
                "required": False,
                "restart_required": False,
            },
        ),
        (
            "ANIME_LIBRARY_FOLDER",
            {
                "section": "Paths",
                "label": "Anime Library Folder",
                "description": "Optional explicit anime TV folder. If blank and Library Root is set with Anime profile enabled, Placeholdarr derives an anime path.",
                "type": "path",
                "required": False,
                "restart_required": False,
            },
        ),
        (
            "DUMMY_FILE_PATH",
            {
                "section": "Paths",
                "label": "Dummy File Path",
                "description": "Path to the primary dummy media file used when creating placeholders. Defaults to APPDATA_PATH/dummy.mp4 in onboarding.",
                "type": "path",
                "required": False,
                "restart_required": True,
            },
        ),
        (
            "COMING_SOON_DUMMY_FILE_PATH",
            {
                "section": "Paths",
                "label": "Coming Soon Dummy File Path",
                "description": "Optional alternate dummy file used only for Coming Soon placeholders. Defaults to APPDATA_PATH/coming_soon_dummy.mp4 in onboarding.",
                "type": "path",
                "required": False,
                "restart_required": True,
            },
        ),
        (
            "STARTUP_SYNC_MODE",
            {
                "section": "Library sync",
                "label": "Startup ARR sync mode",
                "description": (
                    "Runs when the Placeholdarr process starts, after onboarding is finished—not when you click Save in the wizard. "
                    "Auto checks each configured ARR instance in the database: if a full library reconcile has never completed for that instance, "
                    "the next startup runs a full sync once; later startups use a lite history/delta catch-up. "
                    "Finishing onboarding without restarting does not run this pipeline; restart the app (or recreate the container) "
                    "so startup sync runs with your saved settings and ArrState."
                ),
                "type": "choice",
                "restart_required": True,
                "options": [
                    {"value": "auto", "label": "Auto — first qualifying startup: full once per ARR, then lite"},
                    {"value": "full", "label": "Full — always full ARR sync on every startup"},
                    {"value": "lite", "label": "Lite — history/delta catch-up only"},
                    {"value": "off", "label": "Off — skip ARR startup sync"},
                ],
            },
        ),
        (
            "STARTUP_ARR_CHECK_MODE",
            {
                "section": "Library sync",
                "label": "Startup ARR check mode",
                "description": "Controls ARR preflight checks during startup: off skips checks, config validates configured values, live performs live API reachability/auth checks.",
                "type": "choice",
                "restart_required": True,
                "options": [
                    {"value": "off", "label": "Off — skip ARR preflight checks"},
                    {"value": "config", "label": "Config — validate configured URL/API-key pairs"},
                    {"value": "live", "label": "Live — call ARR APIs to verify reachability/auth"},
                ],
            },
        ),
        (
            "FULL_SYNC_INTERVAL_HOURS",
            {
                "section": "Library sync",
                "label": "Recurring full sync interval (hours)",
                "description": "How often to schedule a full ARR/database reconciliation. Set to 0 to disable recurring full sync jobs.",
                "type": "int",
                "min": 0,
                "restart_required": True,
            },
        ),
        (
            "CALENDAR_LOOKAHEAD_DAYS",
            {
                "section": "Calendar",
                "label": "Calendar lookahead days (Coming Soon window)",
                "description": (
                    "How far ahead Placeholdarr creates and keeps Coming Soon placeholders for releases that are not yet available in your library. "
                    "Set to 0 to disable Coming Soon placeholders entirely. Use a positive number for a day cap (e.g. 30). Use -1 for unlimited lookahead within your release-date rules."
                ),
                "type": "int",
                "min": -1,
                "restart_required": False,
            },
        ),
        (
            "CALENDAR_SYNC_INTERVAL_HOURS",
            {
                "section": "Calendar",
                "label": "Calendar sync interval (hours)",
                "description": "How often the calendar/date refresh job runs. Set to 0 to disable this scheduler.",
                "type": "int",
                "min": 0,
                "restart_required": True,
            },
        ),
        (
            "CALENDAR_LOOKAHEAD_DUMMY_MODE",
            {
                "section": "Calendar",
                "label": "Dummy .mp4 for Coming Soon placeholders",
                "description": (
                    "Coming Soon placeholders need a small video file to stand in for a real rip. Under Paths you set full paths—often something like dummy.mp4 (standard) "
                    "and optionally coming_soon_dummy.mp4 (alternate). Here you choose which Paths entry feeds those placeholders: always the standard file, "
                    "or the Coming Soon path when you have configured one."
                ),
                "type": "choice",
                "restart_required": False,
                "options": [
                    {"value": "primary", "label": "Standard dummy (e.g. dummy.mp4 — Dummy File Path)"},
                    {"value": "coming_soon", "label": "Coming Soon dummy (e.g. coming_soon_dummy.mp4 — when that path is set)"},
                ],
            },
        ),
        (
            "PREFERRED_MOVIE_DATE_TYPE",
            {
                "section": "Calendar",
                "label": "Movie release type for Coming Soon placeholders",
                "description": (
                    "Pick one release-date field from Radarr (theatrical, digital, or physical). Placeholdarr uses only that date for movies: "
                    "once it falls inside your calendar lookahead window, it can create Coming Soon placeholders; outside the window it stays in Request (or similar) until the date moves in range. "
                    "Other release types are not used as a fallback—only the type you choose here."
                ),
                "type": "choice",
                "restart_required": False,
                "options": [
                    {"value": "inCinemas", "label": "Theatrical / in cinemas"},
                    {"value": "digitalRelease", "label": "Digital release"},
                    {"value": "physicalRelease", "label": "Physical / home release"},
                ],
            },
        ),
        (
            "CALENDAR_PLACEHOLDER_MODE",
            {
                "section": "Calendar",
                "label": "TV placeholder granularity",
                "description": "Episode: add placeholders as each episode enters the window. Season: add all known episodes in a season when any episode enters the window.",
                "type": "choice",
                "restart_required": False,
                "options": [
                    {"value": "episode", "label": "Episode — per episode as it enters lookahead"},
                    {"value": "season", "label": "Season — whole season when one episode qualifies"},
                ],
            },
        ),
        (
            "ENABLE_COMING_SOON_COUNTDOWN",
            {
                "section": "Calendar",
                "label": "Enable Coming Soon countdown text",
                "description": "Show countdown wording in Coming Soon status metadata (for example, \"in 12 days\").",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "EPISODES_LOOKAHEAD",
            {
                "section": "Playback",
                "label": "Playback search episode lookahead",
                "description": "How many upcoming episodes Placeholdarr may consider when playback triggers an ARR search for a series.",
                "type": "int",
                "min": 1,
                "restart_required": False,
            },
        ),
        (
            "PLAYBACK_COOLDOWN",
            {
                "section": "Playback",
                "label": "Playback cooldown (seconds)",
                "description": "Deduplication window for repeated playback events on the same title. Set to 0 to disable.",
                "type": "int",
                "min": 0,
                "restart_required": False,
            },
        ),
        (
            "TV_PLAY_MODE",
            {
                "section": "Playback",
                "label": "TV playback search granularity",
                "description": "Choose how TV playback searches are targeted: current episode, current season, or whole series.",
                "type": "choice",
                "restart_required": False,
                "options": [
                    {"value": "episode", "label": "Episode"},
                    {"value": "season", "label": "Season"},
                    {"value": "series", "label": "Series"},
                ],
            },
        ),
        (
            "MAX_MONITOR_TIME",
            {
                "section": "Playback",
                "label": "Max monitor time (seconds)",
                "description": "Maximum time a playback-triggered monitoring window remains active before cleanup.",
                "type": "int",
                "min": 1,
                "restart_required": False,
            },
        ),
        (
            "AVAILABLE_CLEANUP_DELAY",
            {
                "section": "Playback",
                "label": "Available cleanup delay (seconds)",
                "description": "Delay before cleanup actions after content becomes available.",
                "type": "int",
                "min": 0,
                "restart_required": False,
            },
        ),
        (
            "ENABLE_PLAYBACK_EVENT_HANDLERS",
            {
                "section": "Playback",
                "label": "Enable playback event handlers",
                "description": "Enable playback-triggered ARR search and monitoring handlers.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "PLAYBACK_SEARCH_PREFERENCE",
            {
                "section": "Playback",
                "label": "Playback search preference",
                "description": "Preferred quality target when playback events trigger searches.",
                "type": "choice",
                "restart_required": False,
                "options": [
                    {"value": "standard", "label": "Standard"},
                    {"value": "4k", "label": "4K"},
                    {"value": "both", "label": "Both"},
                ],
            },
        ),
        (
            "MOVIE_INSTANCE_RANKING",
            {
                "section": "Playback",
                "label": "Movie instance ranking",
                "description": "Internal JSON ranking used by the UI to order Radarr playback routing.",
                "type": "string",
                "restart_required": False,
            },
        ),
        (
            "MOVIE_PLAYBACK_SEARCH_ALL_INSTANCES",
            {
                "section": "Playback",
                "label": "Movie playback: search all instances",
                "description": "If enabled, movie playback searches all eligible Radarr instances and ignores rank order. Only instances where the item already exists in ARR context are searched; Placeholdarr does not add missing items to ARR.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "TV_INSTANCE_RANKING",
            {
                "section": "Playback",
                "label": "TV instance ranking",
                "description": "Internal JSON ranking used by the UI to order Sonarr playback routing.",
                "type": "string",
                "restart_required": False,
            },
        ),
        (
            "TV_PLAYBACK_SEARCH_ALL_INSTANCES",
            {
                "section": "Playback",
                "label": "TV playback: search all instances",
                "description": "If enabled, TV playback searches all eligible Sonarr instances and ignores rank order. Only instances where the series already exists in ARR context are searched; Placeholdarr does not add missing items to ARR.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "TV_PLAYBACK_INSTANCE_MODE",
            {
                "section": "Playback",
                "label": "TV playback instance mode",
                "description": "Applies to real-file TV playback events. Match uses the Sonarr instance tied to the playing file, Preference uses your ranking order, Both tries match first then ranking fallback.",
                "type": "choice",
                "restart_required": False,
                "options": [
                    {"value": "match", "label": "Match file instance only"},
                    {"value": "preference", "label": "Use ranking order only"},
                    {"value": "both", "label": "Match first, then ranking fallback"},
                ],
            },
        ),
        (
            "ENABLE_PLAYBACK_FALLBACK_SEARCH",
            {
                "section": "Playback",
                "label": "Enable playback fallback search",
                "description": "Enable fallback search logic when initial playback-triggered requests do not resolve. This is most useful when Search All is disabled and a primary/ranked path is being tried first.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "PLAYBACK_FALLBACK_TIMEOUT_MINUTES",
            {
                "section": "Playback",
                "label": "Playback fallback timeout (minutes)",
                "description": "How long to wait before triggering playback fallback search behavior.",
                "type": "int",
                "min": 1,
                "restart_required": False,
            },
        ),
        (
            "PLACEHOLDER_STRATEGY",
            {
                "section": "Advanced",
                "label": "Placeholder file strategy",
                "description": "Use hardlink or copy when creating placeholder media files.",
                "type": "choice",
                "restart_required": True,
                "options": [
                    {"value": "hardlink", "label": "Hardlink"},
                    {"value": "copy", "label": "Copy"},
                ],
            },
        ),
        (
            "PLACEHOLDER_CREATE_NFO",
            {
                "section": "Advanced",
                "label": "Create placeholder NFO files",
                "description": "Create companion NFO files for placeholders when supported by the media stack.",
                "type": "bool",
                "restart_required": True,
            },
        ),
        (
            "PLACEHOLDER_STATUS_UPDATES",
            {
                "section": "Advanced",
                "label": "Placeholder status updates",
                "description": "Controls how aggressively placeholder statuses are projected (OFF, REQUEST, or ALL).",
                "type": "choice",
                "restart_required": True,
                "options": [
                    {"value": "OFF", "label": "Off"},
                    {"value": "REQUEST", "label": "Request only"},
                    {"value": "ALL", "label": "All"},
                ],
            },
        ),
        (
            "PLACEHOLDER_STATUS_PROJECTION_MODE",
            {
                "section": "Advanced",
                "label": "Placeholder status projection mode",
                "description": "Choose whether status appears in summary text, title text, both, or is disabled.",
                "type": "choice",
                "restart_required": True,
                "options": [
                    {"value": "summary", "label": "Summary"},
                    {"value": "title", "label": "Title"},
                    {"value": "both", "label": "Both"},
                    {"value": "off", "label": "Off"},
                ],
            },
        ),
        (
            "INCLUDE_SPECIALS",
            {
                "section": "Advanced",
                "label": "Include specials (season 0)",
                "description": "Include specials when creating and reconciling episode placeholder flows.",
                "type": "bool",
                "restart_required": True,
            },
        ),
        (
            "CHECK_INTERVAL",
            {
                "section": "Advanced",
                "label": "Queue check interval (seconds)",
                "description": "Background queue polling cadence. Lower values react faster but increase API traffic.",
                "type": "int",
                "min": 1,
                "restart_required": True,
            },
        ),
        (
            "WORKER_COUNT",
            {
                "section": "Advanced",
                "label": "Worker threads",
                "description": "Worker threads for asynchronous jobs. Increase cautiously for your host.",
                "type": "int",
                "min": 1,
                "restart_required": True,
            },
        ),
    ]
)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError("must be a boolean")


def _coerce_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("must be an integer")
    return int(value)


def _is_blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _coerce_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("must be a valid http(s) URL")
    return text.rstrip("/")


def _coerce_path(value: Any) -> str:
    return str(value or "").strip()


def _validate_value(key: str, raw_value: Any) -> Any:
    meta = SETTINGS_SCHEMA[key]
    value_type = meta["type"]
    if value_type == "bool":
        value = _coerce_bool(raw_value)
    elif value_type == "int":
        value = _coerce_int(raw_value)
        if "min" in meta and value < int(meta["min"]):
            raise ValueError(f"must be >= {meta['min']}")
    elif value_type == "url":
        value = _coerce_url(raw_value)
    elif value_type == "path":
        value = _coerce_path(raw_value)
    elif value_type == "choice":
        value = str(raw_value or "").strip()
        allowed = [str(o["value"]) for o in meta.get("options", [])]
        if not allowed:
            raise ValueError("choice field missing options")
        if value not in allowed:
            raise ValueError(f"must be one of: {', '.join(allowed)}")
    else:
        value = str(raw_value or "").strip()
    if bool(meta.get("required", False)) and _is_blank(value):
        raise ValueError("is required")
    return value


def _get_row(session, key: str) -> AppConfig | None:
    return session.query(AppConfig).filter(AppConfig.key == key).first()


def _set_runtime_value(key: str, value: Any) -> None:
    try:
        setattr(settings, key, value)
    except Exception:
        pass


def apply_persisted_settings(session=None) -> dict[str, Any]:
    owns_session = session is None
    session = session or get_session()
    applied: list[str] = []
    try:
        rows = session.query(AppConfig).filter(AppConfig.key.in_(tuple(SETTINGS_SCHEMA.keys()))).all()
        for row in rows:
            if row.key not in SETTINGS_SCHEMA:
                continue
            _set_runtime_value(row.key, row.value)
            applied.append(row.key)
        return {"applied": applied, "count": len(applied)}
    finally:
        if owns_session:
            session.close()


def get_onboarding_status(session=None) -> dict[str, Any]:
    owns_session = session is None
    session = session or get_session()
    try:
        setup_row = _get_row(session, SETUP_COMPLETED_KEY)
        configured_count = session.query(func.count(AppConfig.id)).filter(
            AppConfig.key.in_(tuple(SETTINGS_SCHEMA.keys()))
        ).scalar() or 0
        return {
            "setup_complete": bool(setup_row and setup_row.value),
            "setup_completed_at": setup_row.value if setup_row else None,
            "configured_settings": configured_count,
            "available_settings": len(SETTINGS_SCHEMA),
        }
    finally:
        if owns_session:
            session.close()


def get_settings_payload(session=None) -> dict[str, Any]:
    owns_session = session is None
    session = session or get_session()
    try:
        grouped: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
        for key, meta in SETTINGS_SCHEMA.items():
            grouped.setdefault(meta["section"], [])
            row = _get_row(session, key)
            effective_value = getattr(settings, key, row.value if row else None)
            if _is_blank(effective_value):
                appdata = str(getattr(settings, "APPDATA_PATH", "/config") or "/config").strip() or "/config"
                appdata = appdata.rstrip("/")
                if key == "DUMMY_FILE_PATH":
                    effective_value = f"{appdata}/dummy.mp4"
                elif key == "COMING_SOON_DUMMY_FILE_PATH":
                    effective_value = f"{appdata}/coming_soon_dummy.mp4"
            entry: dict[str, Any] = {
                "key": key,
                "section": meta["section"],
                "label": meta["label"],
                "description": meta["description"],
                "type": meta["type"],
                "required": bool(meta.get("required", False)),
                "secret": bool(meta.get("secret", False)),
                "restart_required": bool(meta.get("restart_required", False)),
                "value": "" if bool(meta.get("secret", False)) else effective_value,
                "saved_value": None if bool(meta.get("secret", False)) else (row.value if row else None),
                "has_saved_value": bool((row and row.value not in (None, ""))),
            }
            if meta["type"] == "choice":
                entry["options"] = list(meta.get("options") or [])
            grouped[meta["section"]].append(entry)
        return {
            "status": get_onboarding_status(session=session),
            "sections": [{"name": name, "fields": fields} for name, fields in grouped.items()],
        }
    finally:
        if owns_session:
            session.close()


def save_settings(values: dict[str, Any], session=None, partial: bool = False) -> dict[str, Any]:
    owns_session = session is None
    session = session or get_session()
    errors: dict[str, str] = {}
    validated: dict[str, Any] = {}
    try:
        for key, raw_value in values.items():
            if key not in SETTINGS_SCHEMA:
                errors[key] = "unknown setting"
                continue
            try:
                meta = SETTINGS_SCHEMA[key]
                existing_row = _get_row(session, key)
                if bool(meta.get("secret", False)) and _is_blank(raw_value) and existing_row:
                    validated[key] = existing_row.value
                elif bool(meta.get("secret", False)) and _is_blank(raw_value):
                    runtime_value = getattr(settings, key, None)
                    if runtime_value not in (None, ""):
                        validated[key] = runtime_value
                    else:
                        validated[key] = _validate_value(key, raw_value)
                elif key in {"PLEX_MOVIE_SECTION_ID", "PLEX_TV_SECTION_ID"} and _is_blank(raw_value):
                    validated[key] = None
                else:
                    validated[key] = _validate_value(key, raw_value)
            except Exception as exc:
                errors[key] = str(exc)

        enable_plex = bool(validated.get("ENABLE_PLEX", getattr(settings, "ENABLE_PLEX", False)))
        if enable_plex:
            plex_required = {
                "PLEX_URL": "is required when Plex is enabled",
                "PLEX_TOKEN": "is required when Plex is enabled",
                "PLEX_MOVIE_SECTION_ID": "is required when Plex is enabled",
                "PLEX_TV_SECTION_ID": "is required when Plex is enabled",
            }
            for required_key, message in plex_required.items():
                value = validated.get(required_key, getattr(settings, required_key, None))
                if _is_blank(value):
                    errors[required_key] = message

            for section_key in ("PLEX_MOVIE_SECTION_ID", "PLEX_TV_SECTION_ID"):
                value = validated.get(section_key, getattr(settings, section_key, None))
                if _is_blank(value):
                    continue
                try:
                    if int(value) <= 0:
                        errors[section_key] = "must be a positive integer"
                except Exception:
                    errors[section_key] = "must be a positive integer"

        arr_instances_json = str(validated.get("ARR_INSTANCES_JSON", getattr(settings, "ARR_INSTANCES_JSON", "")) or "").strip()
        if arr_instances_json:
            try:
                payload = json.loads(arr_instances_json)
                if not isinstance(payload, list):
                    raise ValueError("must be a JSON array")
                for index, item in enumerate(payload):
                    if not isinstance(item, dict):
                        raise ValueError(f"item {index + 1} must be an object")
                    arr_type = str(item.get("arr_type") or item.get("type") or "").strip().lower()
                    if arr_type not in {"radarr", "sonarr"}:
                        raise ValueError(f"item {index + 1} requires arr_type of 'radarr' or 'sonarr'")
                    instance_key = str(item.get("instance_key") or item.get("key") or item.get("name") or "").strip()
                    if not instance_key:
                        raise ValueError(f"item {index + 1} requires instance_key (or key/name)")
                    url = str(item.get("url") or "").strip()
                    api_key = str(item.get("api_key") or item.get("apikey") or "").strip()
                    if not url or not api_key:
                        raise ValueError(f"item {index + 1} requires url and api_key")
            except Exception as exc:
                errors["ARR_INSTANCES_JSON"] = str(exc)

        if errors:
            return {"ok": False, "errors": errors}

        restart_required_keys: list[str] = []
        saved_keys: list[str] = []
        for key, value in validated.items():
            meta = SETTINGS_SCHEMA[key]
            row = _get_row(session, key)
            if not row:
                row = AppConfig(
                    key=key,
                    value=value,
                    value_type=meta["type"],
                    restart_required=bool(meta.get("restart_required", False)),
                    description=meta["description"],
                )
                session.add(row)
            else:
                row.value = value
                row.value_type = meta["type"]
                row.restart_required = bool(meta.get("restart_required", False))
                row.description = meta["description"]
                session.add(row)
            _set_runtime_value(key, value)
            saved_keys.append(key)
            if bool(meta.get("restart_required", False)):
                restart_required_keys.append(key)

        # Only mark onboarding as completed when this is not a partial save.
        if not partial:
            setup_row = _get_row(session, SETUP_COMPLETED_KEY)
            setup_value = datetime.now(timezone.utc).isoformat()
            if not setup_row:
                setup_row = AppConfig(
                    key=SETUP_COMPLETED_KEY,
                    value=setup_value,
                    value_type="string",
                    restart_required=False,
                    description="Marks completion of first-run settings setup.",
                )
                session.add(setup_row)
            else:
                setup_row.value = setup_value
                session.add(setup_row)

        session.commit()
        return {
            "ok": True,
            "saved_keys": saved_keys,
            "restart_required_keys": restart_required_keys,
            "status": get_onboarding_status(session=session),
        }
    except Exception as exc:
        session.rollback()
        return {"ok": False, "errors": {"__all__": str(exc)}}
    finally:
        if owns_session:
            session.close()


def reset_onboarding(session=None) -> dict[str, Any]:
    """Clear persisted onboarding/settings state so setup can run fresh."""
    owns_session = session is None
    session = session or get_session()
    try:
        target_keys = set(SETTINGS_SCHEMA.keys())
        target_keys.add(SETUP_COMPLETED_KEY)
        deleted = session.query(AppConfig).filter(AppConfig.key.in_(tuple(target_keys))).delete(synchronize_session=False)
        session.commit()
        return {
            "ok": True,
            "deleted_keys": int(deleted or 0),
            "status": get_onboarding_status(session=session),
        }
    except Exception as exc:
        session.rollback()
        return {"ok": False, "errors": {"__all__": str(exc)}}
    finally:
        if owns_session:
            session.close()