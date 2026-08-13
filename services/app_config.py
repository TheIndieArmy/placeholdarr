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

# Settings that rewrite existing placeholder NFO text when changed.
NFO_BACKFILL_SETTING_KEYS = frozenset(
    {
        "PLACEHOLDER_STATUS_UPDATES",
        "PLACEHOLDER_STATUS_PROJECTION_MODE",
    }
)

# Settings that re-download / re-composite on-disk poster art (decoupled from NFO).
ART_BACKFILL_SETTING_KEYS = frozenset(
    {
        "PLACEHOLDER_POSTER_OVERLAY_MODE",
    }
)

# Keys removed from SETTINGS_SCHEMA but still accepted on save (no-op) for older clients / partial payloads.
REMOVED_SETTINGS_KEYS_IGNORED_ON_SAVE = frozenset(
    {
        "PLACEHOLDER_CREATE_NFO",
        "PLACEHOLDER_STATUS_PROJECT_TITLE",
        "PLACEHOLDER_STATUS_PROJECT_SUMMARY",
        "CHECK_INTERVAL",
        "QUEUE_MONITOR_POLL_INTERVAL_SECONDS",
        "QUEUE_MONITOR_REFRESH_MONITORED_DOWNLOADS_INTERVAL_SECONDS",
        "QUEUE_MONITOR_REFRESH_STAGGER_SECONDS",
        "LOG_LEVEL",
        "MULTI_INSTANCE_SHARED_PLACEHOLDER_CLEANUP",
    }
)


SETTINGS_SCHEMA: "OrderedDict[str, dict[str, Any]]" = OrderedDict(
    [
        (
            "AUTH_MODE",
            {
                "section": "Security",
                "label": "Authentication mode",
                "description": (
                    "builtin: Placeholdarr username/password (default). "
                    "forward_auth: trust Remote-User / X-Forwarded-User only from AUTH_TRUSTED_PROXIES. "
                    "disabled: no login checks (unsafe if the port is reachable)."
                ),
                "type": "choice",
                "options": [
                    {"value": "builtin", "label": "Builtin — Placeholdarr username/password (default)"},
                    {
                        "value": "forward_auth",
                        "label": "Forward auth — trust Remote-User / X-Forwarded-User from trusted proxies",
                    },
                    {"value": "disabled", "label": "Disabled — no login checks (unsafe if the port is reachable)"},
                ],
                "required": True,
                "restart_required": False,
            },
        ),
        (
            "AUTH_TRUSTED_PROXIES",
            {
                "section": "Security",
                "label": "Trusted proxy CIDRs",
                "description": (
                    "Comma-separated IPs or CIDRs allowed to assert forward-auth identity headers "
                    "(e.g. 10.0.0.0/8,172.16.0.0/12,192.168.0.0/16). Required for forward_auth."
                ),
                "type": "string",
                "required": False,
                "restart_required": False,
            },
        ),
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
            "TMDB_API_KEY",
            {
                "section": "Media Integrations",
                "label": "TMDB API Key",
                "description": (
                    "TMDB API key (v3 auth) used by the Collections builder to source trending, popular, upcoming, "
                    "and discover lists. Get a free key at themoviedb.org. Optional; only needed for TMDB-based collection sources."
                ),
                "type": "string",
                "required": False,
                "secret": True,
                "restart_required": False,
            },
        ),
        (
            "TRAKT_CLIENT_ID",
            {
                "section": "Media Integrations",
                "label": "Trakt Client ID",
                "description": (
                    "Trakt API application Client ID used by the Collections builder to read public Trakt lists. "
                    "Create a free app at trakt.tv/oauth/applications — only the Client ID is needed (no account linking). "
                    "Optional; only needed for Trakt list collection sources."
                ),
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
                "description": (
                    "Base path where Placeholdarr writes placeholders. Placeholdarr derives `movies` and `tv` folders under this root. "
                    "Use a path separate from Radarr/Sonarr library roots to avoid potential issues with library management."
                ),
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
                # Intro copy is also structured in `frontend/src/App.tsx` (`StartupSyncModeDescription`); update both together.
                "description": (
                    "Controls how Placeholdarr refreshes from Radarr and Sonarr during startup. Full sync will scan arrs services "
                    "and Placeholdarr root folder before proceeding to add/delete placeholder files as needed. Lite sync compares "
                    "each instance's live Radarr/Sonarr catalogs to the database, runs targeted API sync only for changed titles, "
                    "then scoped determination and placeholder materialization for touched rows (skipping a full filesystem scan). "
                    "Auto will run full at startup when needed (for example, after adding a new arr instance), and a lite sync at other times. "
                    "Placeholdarr operations are relatively quick. However, media player libraries still need to scan and update, "
                    "which can take some time for large library changes. "
                    "A full sync will automatically start in the background at the completion of this setup."
                ),
                "type": "choice",
                "restart_required": True,
                "options": [
                    {"value": "auto", "label": "Auto — Full sync when needed; Lite sync all other times"},
                    {"value": "full", "label": "Full — always full ARR sync on every startup"},
                    {"value": "lite", "label": "Lite — catalog diff + targeted sync only"},
                    {"value": "off", "label": "Off — skip ARR startup sync"},
                ],
            },
        ),
        (
            "FULL_SYNC_INTERVAL_HOURS",
            {
                "section": "Library sync",
                "label": "Scheduled full sync interval (hours)",
                "description": "How often to schedule a full ARR/database reconciliation (default 168 = weekly). Set to 0 to disable recurring full sync jobs.",
                "type": "int",
                "min": 0,
                "restart_required": True,
            },
        ),
        (
            "LITE_SYNC_INTERVAL_HOURS",
            {
                "section": "Library sync",
                "label": "Scheduled lite sync interval (hours)",
                "description": (
                    "How often to run lite sync: ARR catalog diff, targeted sync for changes, calendar date refresh, "
                    "Coming Soon status updates, and scoped placeholder work. Default 12 hours. Set to 0 to disable. "
                    "Includes calendar maintenance; a separate calendar interval is only used when lite sync is off."
                ),
                "type": "int",
                "min": 0,
                "restart_required": True,
            },
        ),
        (
            "COLLECTIONS_SYNC_INTERVAL_HOURS",
            {
                "section": "Library sync",
                "label": "Scheduled collections sync interval (hours)",
                "description": (
                    "How often to run enabled collection recipes and sync their results into Plex collections. "
                    "Default 24 hours. Set to 0 to disable the scheduled job (recipes can still be run manually)."
                ),
                "type": "int",
                "min": 0,
                "restart_required": True,
            },
        ),
        (
            "INCLUDE_SPECIALS",
            {
                "section": "Library sync",
                "label": "Include specials (season 0)",
                "description": "Include specials when creating and reconciling episode placeholder flows.",
                "type": "bool",
                "restart_required": True,
            },
        ),
        (
            "SKIP_PLACEHOLDERS_WHEN_MONITORED",
            {
                "section": "Library sync",
                "label": "Skip placeholders for monitored titles",
                "description": (
                    "When enabled, do not create placeholder files for movies or episodes that are monitored in "
                    "Radarr or Sonarr (and have no real file). Existing placeholders are removed on the next ARR sync "
                    "after monitoring is detected. Import still removes placeholders when a real file arrives. "
                    "Manual monitor toggles in Radarr/Sonarr may take until the next full sync to apply."
                ),
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "SKIP_PLACEHOLDERS_WHEN_SERIES_MONITORED",
            {
                "section": "Library sync",
                "label": "TV: skip when series is monitored (Sonarr)",
                "description": (
                    "When the series is monitored in Sonarr, do not create placeholders for any episode in that show—even "
                    "when individual seasons or episodes are unmonitored. Movies are unchanged."
                ),
                "type": "bool",
                "restart_required": False,
                "depends_on": "SKIP_PLACEHOLDERS_WHEN_MONITORED",
                "nested": True,
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
                "label": "Calendar sync interval (hours, legacy)",
                "description": (
                    "Only used when lite sync interval is 0. When lite sync is enabled, calendar date refresh and "
                    "status updates run as part of lite sync instead. Set to 0 to disable."
                ),
                "type": "int",
                "min": 0,
                "restart_required": True,
            },
        ),
        (
            "PREFERRED_MOVIE_DATE_TYPE",
            {
                "section": "Calendar",
                "label": "Movie release date type",
                "description": (
                    "Choose which Radarr release date type will be used to determine when placeholders are created."
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
                "description": "",
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "TV_PLAY_MODE",
            {
                "section": "Lookahead",
                "label": "Search mode",
                "description": "How wide the Sonarr search is from the episode you played.",
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
            "EPISODES_LOOKAHEAD",
            {
                "section": "Lookahead",
                "label": "Lookahead range",
                "description": "In Episode mode, how many upcoming episodes without files to include forward from the played episode.",
                "type": "int",
                "min": 1,
                "restart_required": False,
            },
        ),
        (
            "PLAYBACK_MONITOR_ONLY_NO_SEARCH",
            {
                "section": "Lookahead",
                "label": "Monitor only on playback (no search)",
                "description": (
                    "When enabled, playback marks unmonitored target episodes monitored in Sonarr but never runs "
                    "a search or SEARCHING placeholder updates. While this is on, the two search filters below "
                    "have no effect."
                ),
                "type": "bool",
                "restart_required": False,
            },
        ),
        (
            "PLAYBACK_SUPPRESS_SEARCH_WHEN_ALL_ELIGIBLE_MONITORED",
            {
                "section": "Lookahead",
                "label": "Do not search already-monitored episodes",
                "description": (
                    "When enabled, target episodes already monitored in Sonarr are excluded from playback searches. "
                    "Episodes not yet monitored are marked monitored in Sonarr and searched. When disabled, every "
                    "target episode in your search mode can be searched, including those already monitored."
                ),
                "type": "bool",
                "restart_required": False,
                "disabled_when": "PLAYBACK_MONITOR_ONLY_NO_SEARCH",
            },
        ),
        (
            "PLAYBACK_SUPPRESS_SEARCH_FOR_FUTURE_EPISODES",
            {
                "section": "Lookahead",
                "label": "Do not search future episodes on playback",
                "description": (
                    "When enabled, target episodes that have not aired yet (or unknown air dates treated as future) "
                    "are marked monitored in Sonarr if needed but are not searched. When disabled, future episodes "
                    "in the target list can be searched like any other. Applies to Episode, Season, and Series "
                    "search modes."
                ),
                "type": "bool",
                "restart_required": False,
                "disabled_when": "PLAYBACK_MONITOR_ONLY_NO_SEARCH",
            },
        ),
        (
            "PLACEHOLDER_STATUS_UPDATES",
            {
                "section": "Status Updates",
                "label": "Placeholder status updates",
                "description": (
                    "Choose which placeholder lifecycle states are projected into title/summary metadata. "
                    "Changes can be applied immediately via Placeholder refresh, scheduled for next full sync, "
                    "or left for future transitions only."
                ),
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
                "section": "Status Updates",
                "label": "Project status into",
                "description": (
                    "Choose where bracketed placeholder status appears in media library metadata. "
                    "Changing this can trigger a metadata placeholder refresh (now or next full sync)."
                ),
                "type": "choice",
                "restart_required": True,
                "options": [
                    {"value": "summary", "label": "Summary"},
                    {"value": "title", "label": "Title"},
                    {"value": "both", "label": "Both"},
                ],
            },
        ),
        (
            "PLACEHOLDER_POSTER_OVERLAY_MODE",
            {
                "section": "Status Updates",
                "label": "Placeholder poster overlay",
                "description": (
                    "How placeholder posters appear in Plex, Jellyfin, and Emby. Always writes local poster.jpg, "
                    "seasonNN-poster.jpg at the series root, and episode thumb JPEGs (composited when a style is "
                    "selected, raw download when Off). NFOs do not include art tags; players pick up files from "
                    "disk after a library refresh. Changing this can trigger an art placeholder refresh now or "
                    "at the next full sync."
                ),
                "type": "choice",
                "restart_required": False,
                "options": [
                    {"value": "off", "label": "Off (raw download, no overlay)"},
                    {"value": "grayscale", "label": "Grayscale poster"},
                    {"value": "top_banner", "label": "Top banner — PLACEHOLDER"},
                    {"value": "corner_logo", "label": "Corner badge — Placeholdarr logo"},
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
            "RADARR_SHARED_PLACEHOLDER_CLEANUP",
            {
                "section": "ARR Integrations",
                "label": "Radarr shared placeholder cleanup",
                "description": (
                    "When two Radarr instances track the same movie (same TMDB id), controls shared placeholder "
                    "behavior: keep placeholders until no instance still needs them, or stop creating/recreating "
                    "placeholders on other instances once any instance has a real file (and remove stale on-disk files)."
                ),
                "type": "choice",
                "restart_required": False,
                "options": [
                    {
                        "value": "protect_siblings",
                        "label": "Protect until no instance needs placeholder (recommended)",
                    },
                    {
                        "value": "any_instance_has_file",
                        "label": "Remove when any instance has a real file",
                    },
                ],
            },
        ),
        (
            "SONARR_SHARED_PLACEHOLDER_CLEANUP",
            {
                "section": "ARR Integrations",
                "label": "Sonarr shared placeholder cleanup",
                "description": (
                    "When two Sonarr instances track the same episode (same TVDB id and season/episode), controls "
                    "shared placeholder behavior: keep placeholders until no instance still needs them, or stop "
                    "creating/recreating placeholders on other instances once any instance has a real file."
                ),
                "type": "choice",
                "restart_required": False,
                "options": [
                    {
                        "value": "protect_siblings",
                        "label": "Protect until no instance needs placeholder (recommended)",
                    },
                    {
                        "value": "any_instance_has_file",
                        "label": "Remove when any instance has a real file",
                    },
                ],
            },
        ),
        (
            "WEBHOOK_BASE_URL",
            {
                "section": "Advanced",
                "label": "Webhook base URL",
                "description": (
                    "Optional. The URL that ARR, Tautulli, Jellyfin, and Emby should use to call "
                    "Placeholdarr. When set, this replaces the dashboard origin in the webhook "
                    "setup instructions. Use this when the address those services should reach "
                    "Placeholdarr at is different from the URL you use to view the dashboard — "
                    "for example, an internal Docker/Kubernetes service name when the dashboard "
                    "is reached through a public reverse proxy. Leave blank to use the dashboard's "
                    "own URL. Format: http(s)://host[:port] (no trailing slash)."
                ),
                "type": "url",
                "required": False,
                "restart_required": False,
            },
        ),
        (
            "PLACEHOLDER_STRATEGY",
            {
                "section": "Advanced",
                "label": "Placeholder file strategy",
                "description": (
                    "Use hardlink or copy when creating placeholder media files. Copy can be a better fit for some "
                    "filesystem or path layouts where hardlinks are unreliable or unsupported; if hardlink causes "
                    "issues, switch to copy."
                ),
                "type": "choice",
                "restart_required": True,
                "options": [
                    {"value": "hardlink", "label": "Hardlink"},
                    {"value": "copy", "label": "Copy"},
                ],
            },
        ),
        (
            "QUEUE_MONITOR_SEARCH_TIMEOUT_SECONDS",
            {
                "section": "Advanced",
                "label": "Queue monitor search timeout (seconds)",
                "description": (
                    "The amount of time to wait for content to be added to the Radarr/Sonarr queue after it is "
                    "requested. If content does not reach the queue before this timeout, Placeholdarr assumes no "
                    "qualifying releases were found. Adjust this setting based on how long typical indexer searches "
                    "take in your environment. Default 120 seconds (2 minutes)."
                ),
                "type": "int",
                "min": 60,
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


def _normalized_stored_setting_value(key: str, value: Any) -> str:
    """Stable string compare for whether a setting value actually changed on save."""
    if value is None:
        return ""
    meta = SETTINGS_SCHEMA.get(key) or {}
    value_type = str(meta.get("type") or "").strip().lower()
    if value_type == "bool":
        try:
            return "1" if _coerce_bool(value) else "0"
        except ValueError:
            return str(value).strip().lower()
    if value_type == "int":
        try:
            return str(int(value))
        except (TypeError, ValueError):
            return str(value).strip()
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value).strip()


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


def _arr_instance_id_has_uuid(instance_id: str) -> bool:
    """True when instance_id embeds a UUID (stable webhook id), not the legacy ``radarr:slug`` fallback."""
    text = str(instance_id or "")
    return text.count("-") >= 4


def _stable_default_instance_id(arr_type: str, item: dict[str, Any]) -> str:
    """Default webhook row id: ``radarr_primary``, ``sonarr_secondary``, etc.

    One Placeholdarr deployment uses a single origin; two deployments never share a URL, so these
    predictable ids are safe for local / single-tenant installs. Existing UUID ids are preserved
    by merge logic when already saved.
    """
    r = str(item.get("role") or "").strip().lower()
    if r not in ("primary", "secondary"):
        try:
            r = "primary" if int(item.get("priority", 0) or 0) == 0 else "secondary"
        except Exception:
            r = "primary"
    return f"{arr_type}_{r}"


def _normalize_arr_instance_url(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def _merge_arr_instances_for_stable_webhooks(previous_json: str, incoming_json: str) -> str:
    """Normalize instance_id values and carry forward prior instance_key values as webhook aliases.

    New rows get deterministic ids (``radarr_primary``, …). Rows that already use UUID-based ids keep them.
    Blank ``api_key`` values on matched rows are retained from the previous JSON (secret redaction UX).
    """
    try:
        incoming = json.loads(incoming_json)
        if not isinstance(incoming, list):
            return incoming_json
    except Exception:
        return incoming_json
    try:
        previous = json.loads(previous_json) if str(previous_json or "").strip() else []
        if not isinstance(previous, list):
            previous = []
    except Exception:
        previous = []

    def fp(item: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(item.get("arr_type") or item.get("type") or "").strip().lower(),
            _normalize_arr_instance_url(item.get("url")),
            str(item.get("api_key") or item.get("apikey") or "").strip(),
        )

    def fp_url(item: dict[str, Any]) -> tuple[str, str]:
        return (
            str(item.get("arr_type") or item.get("type") or "").strip().lower(),
            _normalize_arr_instance_url(item.get("url")),
        )

    old_by_id: dict[str, dict[str, Any]] = {}
    old_by_fp: dict[tuple[str, str, str], dict[str, Any]] = {}
    old_by_url: dict[tuple[str, str], dict[str, Any]] = {}
    old_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for o in previous:
        if not isinstance(o, dict):
            continue
        oid = str(o.get("instance_id") or "").strip().lower()
        if oid:
            old_by_id[oid] = o
        try:
            old_by_fp[fp(o)] = o
            old_by_url[fp_url(o)] = o
        except Exception:
            continue
        ok = _normalize_instance_key(o.get("instance_key") or o.get("key") or o.get("name") or "")
        otype = str(o.get("arr_type") or o.get("type") or "").strip().lower()
        if ok and otype:
            old_by_key[(otype, ok)] = o

    alias_cap = 32

    for item in incoming:
        if not isinstance(item, dict):
            continue
        arr_type = str(item.get("arr_type") or item.get("type") or "").strip().lower()
        if arr_type not in {"radarr", "sonarr"}:
            continue
        new_key = _normalize_instance_key(item.get("instance_key") or item.get("key") or item.get("name") or "")
        nid = str(item.get("instance_id") or "").strip().lower()
        matched: dict[str, Any] | None = None
        if nid and nid in old_by_id:
            matched = old_by_id[nid]
        else:
            try:
                matched = old_by_fp.get(fp(item))
            except Exception:
                matched = None
        if matched is None and new_key:
            matched = old_by_key.get((arr_type, new_key))
        if matched is None:
            try:
                matched = old_by_url.get(fp_url(item))
            except Exception:
                matched = None

        # Retain previous API key when the client sends a blank (redacted) value.
        incoming_key = str(item.get("api_key") or item.get("apikey") or "").strip()
        if not incoming_key and matched:
            prev_key = str(matched.get("api_key") or matched.get("apikey") or "").strip()
            if prev_key:
                item["api_key"] = prev_key

        aliases: list[str] = []
        raw_aliases = item.get("instance_key_aliases") if isinstance(item.get("instance_key_aliases"), list) else []
        for a in raw_aliases:
            k = _normalize_instance_key(a)
            if k and k not in aliases:
                aliases.append(k)

        if matched:
            old_key = _normalize_instance_key(matched.get("instance_key") or matched.get("key") or matched.get("name") or "")
            mid = str(matched.get("instance_id") or "").strip().lower()
            if _arr_instance_id_has_uuid(mid):
                item["instance_id"] = mid
            elif _arr_instance_id_has_uuid(nid):
                item["instance_id"] = nid
            else:
                item["instance_id"] = _stable_default_instance_id(arr_type, item)
            old_aliases = matched.get("instance_key_aliases") if isinstance(matched.get("instance_key_aliases"), list) else []
            for a in old_aliases:
                k = _normalize_instance_key(a)
                if k and k not in aliases:
                    aliases.append(k)
            if old_key and old_key != new_key and old_key not in aliases:
                aliases.insert(0, old_key)
        else:
            if _arr_instance_id_has_uuid(nid):
                item["instance_id"] = nid
            elif str(nid or "").strip():
                item["instance_id"] = str(nid).strip().lower()
            else:
                item["instance_id"] = _stable_default_instance_id(arr_type, item)

        aliases = [a for a in aliases if a and a != new_key]
        seen: set[str] = set()
        deduped: list[str] = []
        for a in aliases:
            if a not in seen:
                deduped.append(a)
                seen.add(a)
        item["instance_key_aliases"] = deduped[:alias_cap]
        # Never persist client-only redaction flags.
        item.pop("api_key_saved", None)

    return json.dumps(incoming)


def _redact_arr_instances_json_for_payload(raw: Any) -> tuple[str, bool]:
    """Return redacted ARR_INSTANCES_JSON string and whether any api_key was saved server-side."""
    text = "" if raw is None else (raw if isinstance(raw, str) else json.dumps(raw))
    text = str(text or "").strip()
    if not text:
        return ("[]", False)
    try:
        payload = json.loads(text)
    except Exception:
        return (text, False)
    if not isinstance(payload, list):
        return (text, False)
    any_saved = False
    redacted: list[Any] = []
    for item in payload:
        if not isinstance(item, dict):
            redacted.append(item)
            continue
        row = dict(item)
        key_present = bool(str(row.get("api_key") or row.get("apikey") or "").strip())
        if key_present:
            any_saved = True
        row["api_key"] = ""
        row.pop("apikey", None)
        row["api_key_saved"] = key_present
        redacted.append(row)
    return (json.dumps(redacted), any_saved)


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
        if key == "PLACEHOLDER_STATUS_PROJECTION_MODE" and value.lower() == "off":
            value = "both"
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
        if key == "PLACEHOLDER_STATUS_PROJECTION_MODE":
            raw = str(value or "both").strip().lower()
            if raw == "off" or raw not in {"summary", "title", "both"}:
                value = "both"
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
        _set_runtime_value("PLACEHOLDER_CREATE_NFO", True)
        return {"applied": applied, "count": len(applied)}
    finally:
        if owns_session:
            session.close()


def get_onboarding_status(session=None) -> dict[str, Any]:
    from services.startup_gate import startup_sync_complete

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
            "startup_sync_complete": startup_sync_complete.is_set(),
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
            if key in {"RADARR_SHARED_PLACEHOLDER_CLEANUP", "SONARR_SHARED_PLACEHOLDER_CLEANUP"}:
                if _is_blank(effective_value):
                    legacy_row = _get_row(session, "MULTI_INSTANCE_SHARED_PLACEHOLDER_CLEANUP")
                    if legacy_row and not _is_blank(legacy_row.value):
                        effective_value = legacy_row.value
            if key == "PLACEHOLDER_STATUS_PROJECTION_MODE":
                ev = str(effective_value or "both").strip().lower()
                if ev == "off" or ev not in {"summary", "title", "both"}:
                    effective_value = "both"
            if _is_blank(effective_value):
                pass
            saved_value_out = None if bool(meta.get("secret", False)) else (row.value if row else None)
            if key == "PLACEHOLDER_STATUS_PROJECTION_MODE" and saved_value_out is not None:
                sv = str(saved_value_out).strip().lower()
                if sv == "off" or sv not in {"summary", "title", "both"}:
                    saved_value_out = "both"
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
                "saved_value": saved_value_out,
                "has_saved_value": bool((row and row.value not in (None, ""))),
            }
            if meta["type"] == "choice":
                entry["options"] = list(meta.get("options") or [])
            if meta.get("depends_on"):
                entry["depends_on"] = str(meta["depends_on"])
            if meta.get("disabled_when"):
                entry["disabled_when"] = str(meta["disabled_when"])
            if meta.get("nested"):
                entry["nested"] = True
            if key == "ARR_INSTANCES_JSON":
                redacted_value, any_saved = _redact_arr_instances_json_for_payload(effective_value)
                entry["value"] = redacted_value
                entry["saved_value"] = None
                entry["has_saved_value"] = any_saved or bool((row and row.value not in (None, "")))
            grouped[meta["section"]].append(entry)

        # Not a settings field (no SETTINGS_SCHEMA entry, not user-editable
        # via save/reset) — exposed only so the frontend's webhook-URL
        # builders (which already read this same values map) can display it.
        # See services/auth.py for where it's actually generated/rotated.
        from services.auth import ensure_webhook_api_key

        return {
            "status": get_onboarding_status(session=session),
            "sections": [{"name": name, "fields": fields} for name, fields in grouped.items()],
            "webhook_api_key": ensure_webhook_api_key(session=session),
        }
    finally:
        if owns_session:
            session.close()


def save_settings(
    values: dict[str, Any],
    session=None,
    partial: bool = False,
    context: dict[str, Any] | None = None,
    *,
    apply_scope: str | None = None,
) -> dict[str, Any]:
    owns_session = session is None
    session = session or get_session()
    errors: dict[str, str] = {}
    validated: dict[str, Any] = {}
    created_paths: list[str] = []
    derived_library_paths: list[str] = []
    specials_before: bool | None = None
    specials_after: bool | None = None
    try:
        for key, raw_value in values.items():
            if key not in SETTINGS_SCHEMA:
                if key in REMOVED_SETTINGS_KEYS_IGNORED_ON_SAVE:
                    continue
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

        nfo_backfill_keys_changed: list[str] = []
        for key in NFO_BACKFILL_SETTING_KEYS:
            if key not in validated:
                continue
            prev_row = _get_row(session, key)
            prev_val = "" if not prev_row or prev_row.value is None else str(prev_row.value).strip()
            new_val = str(validated.get(key) or "").strip()
            if prev_val != new_val:
                nfo_backfill_keys_changed.append(key)

        art_backfill_keys_changed: list[str] = []
        for key in ART_BACKFILL_SETTING_KEYS:
            if key not in validated:
                continue
            prev_row = _get_row(session, key)
            prev_val = "" if not prev_row or prev_row.value is None else str(prev_row.value).strip()
            new_val = str(validated.get(key) or "").strip()
            if prev_val != new_val:
                art_backfill_keys_changed.append(key)

        if "ARR_INSTANCES_JSON" in validated:
            prev_row = _get_row(session, "ARR_INSTANCES_JSON")
            prev_raw = str(prev_row.value if prev_row and prev_row.value is not None else "") or ""
            merged = _merge_arr_instances_for_stable_webhooks(prev_raw, str(validated.get("ARR_INSTANCES_JSON") or ""))
            validated["ARR_INSTANCES_JSON"] = merged

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
                seen_instance_ids: set[str] = set()
                reserved_tokens: set[str] = set()
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
                    instance_id = str(item.get("instance_id") or "").strip().lower()
                    if not instance_id:
                        raise ValueError(f"item {index + 1} requires instance_id (stable webhook identity)")
                    if instance_id in seen_instance_ids:
                        raise ValueError(f"item {index + 1} has duplicate instance_id '{instance_id}'")
                    seen_instance_ids.add(instance_id)
                    tokens = [instance_key]
                    for a in item.get("instance_key_aliases") or []:
                        ak = _normalize_instance_key(a)
                        if ak:
                            tokens.append(ak)
                    for t in tokens:
                        if t in reserved_tokens:
                            raise ValueError(
                                f"item {index + 1} instance_key or alias '{t}' conflicts with another instance row"
                            )
                        reserved_tokens.add(t)
                    url = str(item.get("url") or "").strip()
                    api_key = str(item.get("api_key") or item.get("apikey") or "").strip()
                    if not url or not api_key:
                        raise ValueError(f"item {index + 1} requires url and api_key")
                    counts[arr_type] += 1
                    if counts[arr_type] > arr_limit:
                        raise ValueError(f"{arr_type} supports up to {arr_limit} instances per deployment")
                    allowed_instance_keys[arr_type].add(instance_key)
                    for a in item.get("instance_key_aliases") or []:
                        ak = _normalize_instance_key(a)
                        if ak:
                            allowed_instance_keys[arr_type].add(ak)
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
            prev_value = row.value if row else None
            if key == "INCLUDE_SPECIALS":
                if row is not None:
                    specials_before = _coerce_bool(row.value)
                else:
                    specials_before = _coerce_bool(getattr(settings, "INCLUDE_SPECIALS", False))
                specials_after = _coerce_bool(value)
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
            if bool(meta.get("restart_required", False)) and _normalized_stored_setting_value(
                key, prev_value
            ) != _normalized_stored_setting_value(key, value):
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
        if specials_before is not None and specials_after is not None and specials_before != specials_after:
            try:
                from services.source_of_truth.lite_reconcile import mark_specials_backfill_pending

                mark_specials_backfill_pending(enabled=bool(specials_after))
                logger.info(
                    f"Settings change detected: INCLUDE_SPECIALS {specials_before} -> {specials_after}; "
                    f"specials_backfill_pending={bool(specials_after)}",
                    extra={"emoji_type": "info"},
                )
            except Exception as exc:
                logger.warning(
                    f"Failed to persist specials backfill marker after INCLUDE_SPECIALS change: {exc}",
                    extra={"emoji_type": "warning"},
                )
        arr_instance_reconcile: dict[str, Any] | None = None
        if not partial and "ARR_INSTANCES_JSON" in validated:
            try:
                from services.source_of_truth.arr_instance_reconcile import reconcile_after_arr_settings_save

                arr_instance_reconcile = reconcile_after_arr_settings_save(
                    str(validated.get("ARR_INSTANCES_JSON") or "")
                )
            except Exception as exc:
                logger.error(
                    f"ARR instance reconcile after settings save failed: {exc}",
                    extra={"emoji_type": "error"},
                )
        _set_runtime_value("PLACEHOLDER_CREATE_NFO", True)
        backfill_summary: dict[str, Any] | None = None
        art_backfill_summary: dict[str, Any] | None = None
        if (nfo_backfill_keys_changed or art_backfill_keys_changed) and apply_scope:
            from services.source_of_truth.placeholder_refresh import execute_placeholder_refresh_apply_scope

            refresh_out = execute_placeholder_refresh_apply_scope(
                apply_scope=str(apply_scope),
                metadata=bool(nfo_backfill_keys_changed),
                art=bool(art_backfill_keys_changed),
                templates=False,
                source="settings_save",
                task_run_trigger="settings_change" if str(apply_scope) == "now" else None,
            )
            if isinstance(refresh_out.get("nfo_backfill"), dict):
                backfill_summary = dict(refresh_out["nfo_backfill"])
            else:
                backfill_summary = {
                    "ok": bool(refresh_out.get("ok", True)),
                    "scope": str(refresh_out.get("scope") or ""),
                    "enqueued": bool(refresh_out.get("enqueued")),
                }
            if isinstance(refresh_out.get("art_backfill"), dict):
                art_backfill_summary = dict(refresh_out["art_backfill"])
            else:
                art_backfill_summary = {
                    "ok": bool(refresh_out.get("ok", True)),
                    "scope": str(refresh_out.get("scope") or ""),
                    "enqueued": bool(refresh_out.get("enqueued")),
                    "pending": bool(refresh_out.get("pending")),
                }
            logger.info(
                f"Placeholder refresh after settings save scope={apply_scope} "
                f"metadata_keys={nfo_backfill_keys_changed} art_keys={art_backfill_keys_changed}",
                extra={"emoji_type": "processing"},
            )

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
            "arr_instance_reconcile": arr_instance_reconcile,
            "nfo_backfill_keys_changed": nfo_backfill_keys_changed,
            "nfo_backfill": backfill_summary,
            "art_backfill_keys_changed": art_backfill_keys_changed,
            "art_backfill": art_backfill_summary,
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