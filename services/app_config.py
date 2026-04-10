from __future__ import annotations

import json
from collections import OrderedDict
from datetime import datetime, timezone
import os
from pathlib import Path
import re
from urllib.parse import urlparse
from typing import Any

from sqlalchemy import func

from core.config import settings
from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import AppConfig


SETUP_COMPLETED_KEY = "APP_SETUP_COMPLETED_AT"


SETTINGS_SCHEMA: "OrderedDict[str, dict[str, Any]]" = OrderedDict(
    [
        (
            "ENABLE_PLEX",
            {
                "section": "Media Integrations",
                "label": "Enable Plex",
                "description": "Enable Plex integration for metadata updates and playback/import workflows. If disabled, Plex URL/token/section IDs can stay blank.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "PLEX_URL",
            {
                "section": "Media Integrations",
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
                "section": "Media Integrations",
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
                "section": "Media Integrations",
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
                "section": "Media Integrations",
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
                "section": "Media Integrations",
                "label": "Enable Jellyfin",
                "description": "Enable Jellyfin integration for metadata refresh and playback-driven actions. If disabled, Jellyfin fields can stay blank.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "JELLYFIN_URL",
            {
                "section": "Media Integrations",
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
                "section": "Media Integrations",
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
                "section": "Media Integrations",
                "label": "Enable Emby",
                "description": "Enable Emby integration for metadata refresh and playback-driven actions. If disabled, Emby fields can stay blank.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "EMBY_URL",
            {
                "section": "Media Integrations",
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
                "section": "Media Integrations",
                "label": "Emby Token",
                "description": "Emby API token used for authenticated requests. Required only when Emby is enabled.",
                "type": "string",
                "required": False,
                "secret": True,
                "restart_required": False,
            },
        ),
        (
            "ARR_INSTANCES_JSON",
            {
                "section": "ARR Integrations",
                "label": "ARR Instances JSON (Advanced)",
                "description": "Optional JSON array for named ARR instances. By default, Placeholdarr supports up to 2 Radarr and 2 Sonarr instances per deployment. Changing an instance URL or API key triggers a full resync; label-only changes do not.",
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
                "description": "Base path where Placeholdarr writes placeholders. Placeholdarr derives `movies` and `tv` folders under this root.",
                "type": "path",
                "required": False,
                "restart_required": False,
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
            "MOVIE_PLACEHOLDER_SEARCH_MODE",
            {
                "section": "ARR Integrations",
                "label": "Movie placeholder search instance",
                "description": "When a movie placeholder plays, which Radarr instance should be searched. Primary = first configured instance. Secondary = second configured instance. Both = search all configured instances.",
                "type": "choice",
                "restart_required": False,
                "options": [
                    {"value": "primary", "label": "Primary instance only"},
                    {"value": "secondary", "label": "Secondary instance only"},
                    {"value": "both", "label": "Both instances"},
                ],
            },
        ),
        (
            "TV_PLACEHOLDER_SEARCH_MODE",
            {
                "section": "ARR Integrations",
                "label": "TV placeholder search instance",
                "description": "When a TV placeholder plays, which Sonarr instance should be searched. Primary = first configured instance. Secondary = second configured instance. Both = search all configured instances.",
                "type": "choice",
                "restart_required": False,
                "options": [
                    {"value": "primary", "label": "Primary instance only"},
                    {"value": "secondary", "label": "Secondary instance only"},
                    {"value": "both", "label": "Both instances"},
                ],
            },
        ),
        (
            "MOVIE_PLAYBACK_INSTANCE_MODE",
            {
                "section": "ARR Integrations",
                "label": "Movie real-file playback mode",
                "description": "Applies when a real movie file is played. Match routes to the instance whose library path contains the file. Primary always searches the first configured instance. Secondary always searches the second configured instance. Both searches all configured instances.",
                "type": "choice",
                "restart_required": False,
                "options": [
                    {"value": "match", "label": "Match by library path (recommended)"},
                    {"value": "primary", "label": "Primary instance only"},
                    {"value": "secondary", "label": "Secondary instance only"},
                    {"value": "both", "label": "Both instances"},
                ],
            },
        ),
        (
            "TV_PLAYBACK_INSTANCE_MODE",
            {
                "section": "ARR Integrations",
                "label": "TV real-file playback mode",
                "description": "Applies when a real TV file is played. Match routes to the instance whose library path contains the file. Primary always searches the first configured instance. Secondary always searches the second configured instance. Both searches all configured instances.",
                "type": "choice",
                "restart_required": False,
                "options": [
                    {"value": "match", "label": "Match by library path (recommended)"},
                    {"value": "primary", "label": "Primary instance only"},
                    {"value": "secondary", "label": "Secondary instance only"},
                    {"value": "both", "label": "Both instances"},
                ],
            },
        ),
        (
            "ENABLE_PLAYBACK_FALLBACK_SEARCH",
            {
                "section": "ARR Integrations",
                "label": "Enable playback fallback search",
                "description": "When instance mode is set to Primary or Secondary, content that is not present in the selected ARR instance falls back immediately, including missing rows and rows marked deleted. This setting controls delayed fallback only after a search was actually attempted first but did not resolve, such as no found releases or a failed download path.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "PLAYBACK_FALLBACK_TIMEOUT_MINUTES",
            {
                "section": "ARR Integrations",
                "label": "Playback fallback timeout (minutes)",
                "description": "Minutes to wait before delayed fallback runs on the other instance after an initial search attempt did not resolve. Content that is not present in the selected ARR instance still falls back immediately, including missing rows and rows marked deleted.",
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


def _normalize_instance_key(value: Any) -> str:
    key_raw = str(value or "").strip().lower()
    normalized = re.sub(r"[^a-z0-9_-]+", "_", key_raw).strip("_-")
    return normalized


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


def _apply_runtime_library_defaults() -> None:
    """Derive internal runtime folders from LIBRARY_ROOT in simplified mode."""
    root = str(getattr(settings, "LIBRARY_ROOT", "") or "").strip()
    if not root:
        return
    movie = os.path.join(root, "movies")
    tv = os.path.join(root, "tv")
    _set_runtime_value("MOVIE_LIBRARY_FOLDER", movie)
    _set_runtime_value("TV_LIBRARY_FOLDER", tv)
    # Keep 4K folders aligned to simplified layout so legacy call sites keep working.
    _set_runtime_value("MOVIE_LIBRARY_4K_FOLDER", movie)
    _set_runtime_value("TV_LIBRARY_4K_FOLDER", tv)


def _parse_octal_mode(raw: Any, default: int = 0o777) -> int:
    text = str(raw or "").strip()
    if not text:
        return default
    try:
        return int(text, 8)
    except Exception:
        return default


def _ensure_library_root_folders(root: str, dir_mode: int) -> list[str]:
    root_value = str(root or "").strip()
    if not root_value:
        return []

    created: list[str] = []
    root_path = Path(root_value)
    if not root_path.exists():
        root_path.mkdir(parents=True, exist_ok=True)
        created.append(str(root_path))
    try:
        os.chmod(root_path, dir_mode)
    except Exception:
        pass

    for folder_name in ("movies", "tv"):
        target = root_path / folder_name
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
            created.append(str(target))
        try:
            os.chmod(target, dir_mode)
        except Exception:
            pass
    return created


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
        _apply_runtime_library_defaults()
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
                pass
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


def save_settings(values: dict[str, Any], session=None, partial: bool = False, context: dict[str, Any] | None = None) -> dict[str, Any]:
    owns_session = session is None
    session = session or get_session()
    errors: dict[str, str] = {}
    validated: dict[str, Any] = {}
    created_paths: list[str] = []
    derived_library_paths: list[str] = []
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

        # Simplified path model: derive runtime folders from LIBRARY_ROOT.
        if "LIBRARY_ROOT" in validated:
            root = str(validated.get("LIBRARY_ROOT") or "").strip()
            if root:
                dir_mode = _parse_octal_mode(validated.get("PLACEHOLDER_DIR_MODE", getattr(settings, "PLACEHOLDER_DIR_MODE", "777")), 0o777)
                movie_path = os.path.join(root, "movies")
                tv_path = os.path.join(root, "tv")
                created_paths = _ensure_library_root_folders(root, dir_mode)
                derived_library_paths = [movie_path, tv_path]
                _set_runtime_value("MOVIE_LIBRARY_FOLDER", movie_path)
                _set_runtime_value("TV_LIBRARY_FOLDER", tv_path)
                _set_runtime_value("MOVIE_LIBRARY_4K_FOLDER", movie_path)
                _set_runtime_value("TV_LIBRARY_4K_FOLDER", tv_path)

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
        arr_limit = max(1, int(getattr(settings, "ARR_MAX_INSTANCES_PER_TYPE", 2) or 2))
        allowed_instance_keys: dict[str, set[str]] = {"radarr": set(), "sonarr": set()}

        if arr_instances_json:
            try:
                payload = json.loads(arr_instances_json)
                if not isinstance(payload, list):
                    raise ValueError("must be a JSON array")
                counts: dict[str, int] = {"radarr": 0, "sonarr": 0}
                seen_keys: set[str] = set()
                for index, item in enumerate(payload):
                    if not isinstance(item, dict):
                        raise ValueError(f"item {index + 1} must be an object")
                    arr_type = str(item.get("arr_type") or item.get("type") or "").strip().lower()
                    if arr_type not in {"radarr", "sonarr"}:
                        raise ValueError(f"item {index + 1} requires arr_type of 'radarr' or 'sonarr'")
                    instance_key = _normalize_instance_key(item.get("instance_key") or item.get("key") or item.get("name") or "")
                    if not instance_key:
                        raise ValueError(f"item {index + 1} requires instance_key (or key/name)")
                    if instance_key in seen_keys:
                        raise ValueError(f"item {index + 1} has duplicate instance_key '{instance_key}'")
                    seen_keys.add(instance_key)
                    url = str(item.get("url") or "").strip()
                    api_key = str(item.get("api_key") or item.get("apikey") or "").strip()
                    if not url or not api_key:
                        raise ValueError(f"item {index + 1} requires url and api_key")
                    counts[arr_type] += 1
                    if counts[arr_type] > arr_limit:
                        raise ValueError(f"{arr_type} supports up to {arr_limit} instances per deployment")
                    allowed_instance_keys[arr_type].add(instance_key)
            except Exception as exc:
                errors["ARR_INSTANCES_JSON"] = str(exc)
        else:
            for item in getattr(settings, "configured_arr_instances", []) or []:
                arr_type = str(item.get("arr_type") or "").strip().lower()
                if arr_type not in {"radarr", "sonarr"}:
                    continue
                instance_key = _normalize_instance_key(item.get("instance_key") or "")
                if instance_key:
                    allowed_instance_keys[arr_type].add(instance_key)

        # Legacy JSON ranking fields removed: MOVIE_INSTANCE_RANKING and TV_INSTANCE_RANKING
        # Rankings are derived from configured ARR instances instead.

        if errors:
            logger.warning(
                f"Settings save rejected: partial={partial} context={context or {}} errors={errors}",
                extra={"emoji_type": "warning"},
            )
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
        logger.info(
            "Settings saved"
            f" partial={partial}"
            f" context={context or {}}"
            f" saved_keys={saved_keys}"
            f" derived_library_paths={derived_library_paths}"
            f" created_paths={created_paths}",
            extra={"emoji_type": "update" if partial else "success"},
        )
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