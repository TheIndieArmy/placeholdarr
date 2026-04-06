from __future__ import annotations

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
                "description": "Enable Plex integration and playback/import handlers against Plex media libraries.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "PLEX_URL",
            {
                "section": "Integrations",
                "label": "Plex URL",
                "description": "Base Plex URL, for example: http://plex.local:32400",
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
                "description": "Plex authentication token used for API requests.",
                "type": "string",
                "required": False,
                "secret": True,
                "restart_required": False,
            },
        ),
        (
            "ENABLE_JELLYFIN",
            {
                "section": "Integrations",
                "label": "Enable Jellyfin",
                "description": "Enable Jellyfin integration for playback and metadata refresh actions.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "JELLYFIN_URL",
            {
                "section": "Integrations",
                "label": "Jellyfin URL",
                "description": "Base Jellyfin URL, for example: http://jellyfin.local:8096",
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
                "description": "Jellyfin API token used for authenticated requests.",
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
                "description": "Enable Emby integration for playback and metadata refresh actions.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "EMBY_URL",
            {
                "section": "Integrations",
                "label": "Emby URL",
                "description": "Base Emby URL, for example: http://emby.local:8096",
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
                "description": "Emby API token used for authenticated requests.",
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
                "description": "Base Radarr URL, for example: http://radarr.local:7878",
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
                "description": "API key used to authenticate Radarr requests.",
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
                "description": "Optional second Radarr instance URL for 4K media.",
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
                "description": "API key for the optional 4K Radarr instance.",
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
                "description": "Base Sonarr URL, for example: http://sonarr.local:8989",
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
                "description": "API key used to authenticate Sonarr requests.",
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
                "description": "Optional second Sonarr instance URL for 4K media.",
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
                "description": "API key for the optional 4K Sonarr instance.",
                "type": "string",
                "required": False,
                "secret": True,
                "restart_required": False,
            },
        ),
        (
            "LIBRARY_ROOT",
            {
                "section": "Paths",
                "label": "Library Root",
                "description": "Optional root used as the base for derived movie and TV folders.",
                "type": "path",
                "required": False,
                "restart_required": False,
            },
        ),
        (
            "MOVIE_LIBRARY_FOLDER",
            {
                "section": "Paths",
                "label": "Movie Library Folder",
                "description": "Folder path where movie placeholders and library files are managed.",
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
                "description": "Folder path where episode placeholders and library files are managed.",
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
                "description": "Optional dedicated folder path for 4K movie placeholders.",
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
                "description": "Optional dedicated folder path for 4K episode placeholders.",
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
                "description": "Path to the base dummy file used for placeholder materialization.",
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
                "description": "Optional path to a special dummy file for upcoming content placeholders.",
                "type": "path",
                "required": False,
                "restart_required": True,
            },
        ),
        (
            "ENABLE_COMING_SOON_PLACEHOLDERS",
            {
                "section": "Calendar",
                "label": "Enable Coming Soon Placeholders",
                "description": "Allow future placeholders inside the configured lookahead window.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "CALENDAR_LOOKAHEAD_DAYS",
            {
                "section": "Calendar",
                "label": "Calendar Lookahead Days",
                "description": "How far ahead future content is considered placeholder-eligible.",
                "type": "int",
                "min": -1,
                "restart_required": False,
            },
        ),
        (
            "CALENDAR_SYNC_INTERVAL_HOURS",
            {
                "section": "Calendar",
                "label": "Calendar Sync Interval Hours",
                "description": "Scheduler cadence for calendar sync operations.",
                "type": "int",
                "min": 0,
                "restart_required": True,
            },
        ),
        (
            "ENABLE_COMING_SOON_COUNTDOWN",
            {
                "section": "Calendar",
                "label": "Enable Coming Soon Countdown",
                "description": "Enable countdown metadata for coming-soon placeholder surfaces.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "ENABLE_IMPORT_EVENT_HANDLERS",
            {
                "section": "Automation",
                "label": "Enable Import Event Handlers",
                "description": "Process import webhooks and react automatically.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "ENABLE_DELETE_EVENT_HANDLERS",
            {
                "section": "Automation",
                "label": "Enable Delete Event Handlers",
                "description": "Process delete webhooks and recreate placeholders when needed.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "ENABLE_PLAYBACK_EVENT_HANDLERS",
            {
                "section": "Automation",
                "label": "Enable Playback Event Handlers",
                "description": "Trigger playback-driven search and placeholder logic.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "ENABLE_PLAYBACK_FALLBACK_SEARCH",
            {
                "section": "Automation",
                "label": "Enable Playback Fallback Search",
                "description": "If enabled, schedule a delayed fallback-instance search when the preferred instance did not import.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "ENABLE_QUEUE_MONITOR",
            {
                "section": "Automation",
                "label": "Enable Queue Monitor",
                "description": "Continuously poll ARR queues after playback-triggered searches and update placeholder statuses.",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "QUEUE_MONITOR_RETRY_GRACE_SECONDS",
            {
                "section": "Automation",
                "label": "Queue Retry Grace Seconds",
                "description": "How long to wait after an item leaves queue before marking it as an error.",
                "type": "int",
                "min": 30,
                "restart_required": False,
            },
        ),
        (
            "PLAYBACK_COOLDOWN",
            {
                "section": "Playback",
                "label": "Playback Cooldown Seconds",
                "description": "Minimum time between repeat playback actions for the same title.",
                "type": "int",
                "min": 0,
                "restart_required": False,
            },
        ),
        (
            "CHECK_INTERVAL",
            {
                "section": "Advanced",
                "label": "Queue Check Interval Seconds",
                "description": "Background polling cadence for queue monitoring.",
                "type": "int",
                "min": 1,
                "restart_required": True,
            },
        ),
        (
            "WORKER_COUNT",
            {
                "section": "Advanced",
                "label": "Worker Threads",
                "description": "Number of worker threads to launch at startup.",
                "type": "int",
                "min": 1,
                "restart_required": True,
            },
        ),
        (
            "FULL_SYNC_INTERVAL_HOURS",
            {
                "section": "Advanced",
                "label": "Full Sync Interval Hours",
                "description": "How often full sync is scheduled; set to 0 to disable periodic full sync.",
                "type": "int",
                "min": 0,
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
            grouped[meta["section"]].append(
                {
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
            )
        return {
            "status": get_onboarding_status(session=session),
            "sections": [{"name": name, "fields": fields} for name, fields in grouped.items()],
        }
    finally:
        if owns_session:
            session.close()


def save_settings(values: dict[str, Any], session=None) -> dict[str, Any]:
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
                else:
                    validated[key] = _validate_value(key, raw_value)
            except Exception as exc:
                errors[key] = str(exc)

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