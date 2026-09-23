from __future__ import annotations

import json
from collections import OrderedDict
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import uuid
from urllib.parse import urlparse
from typing import Any

from sqlalchemy import func

from core.config import settings
from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import AppConfig


SETUP_COMPLETED_KEY = "APP_SETUP_COMPLETED_AT"

# Choice fields that also accept a concrete Arr instance_key (from the instance name).
_INSTANCE_SEARCH_MODE_KEYS = frozenset(
    {
        "MOVIE_PLACEHOLDER_SEARCH_MODE",
        "TV_PLACEHOLDER_SEARCH_MODE",
        "MOVIE_PLAYBACK_INSTANCE_MODE",
        "TV_PLAYBACK_INSTANCE_MODE",
    }
)
_INSTANCE_SEARCH_MODE_RESERVED = frozenset({"match", "both", "primary", "secondary"})
_INSTANCE_KEY_MODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

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
        "ENABLE_PREFERRED_POSTER_LANGUAGE",
        "PREFERRED_POSTER_LANGUAGE",
        "PREFER_ORIGINAL_POSTER_LANGUAGE",
    }
)

# Settings that relocate placeholder folders on disk (destination map / library root).
DESTINATION_REMATERIALIZE_SETTING_KEYS = frozenset(
    {
        "LIBRARY_ROOT",
        "LIBRARY_DESTINATION_MAP_JSON",
    }
)

_POSTER_LANGUAGE_OPTIONS = [
    {"value": "en", "label": "English (en)"},
    {"value": "de", "label": "German (de)"},
    {"value": "fr", "label": "French (fr)"},
    {"value": "es", "label": "Spanish (es)"},
    {"value": "it", "label": "Italian (it)"},
    {"value": "pt", "label": "Portuguese (pt)"},
    {"value": "ja", "label": "Japanese (ja)"},
    {"value": "ko", "label": "Korean (ko)"},
    {"value": "zh", "label": "Chinese (zh)"},
    {"value": "ru", "label": "Russian (ru)"},
    {"value": "nl", "label": "Dutch (nl)"},
    {"value": "pl", "label": "Polish (pl)"},
    {"value": "sv", "label": "Swedish (sv)"},
    {"value": "da", "label": "Danish (da)"},
    {"value": "no", "label": "Norwegian (no)"},
    {"value": "fi", "label": "Finnish (fi)"},
    {"value": "tr", "label": "Turkish (tr)"},
    {"value": "ar", "label": "Arabic (ar)"},
    {"value": "hi", "label": "Hindi (hi)"},
]

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
        "MOVIE_LIBRARY_4K_FOLDER",
        "TV_LIBRARY_4K_FOLDER",
        "PLEX_MOVIE_4K_SECTION_ID",
        "PLEX_TV_4K_SECTION_ID",
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
            "PLEX_PLAYBACK_NOTIFIER",
            {
                "section": "Media Integrations",
                "label": "Plex playback notifier",
                "description": "How Placeholdarr hears about Plex playback: Tautulli webhook or Tracearr JSON webhook.",
                "type": "choice",
                "options": [
                    {"value": "tautulli", "label": "Tautulli"},
                    {"value": "tracearr", "label": "Tracearr"},
                ],
                "required": True,
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
            "JELLYFIN_PLAYBACK_NOTIFIER",
            {
                "section": "Media Integrations",
                "label": "Jellyfin playback notifier",
                "description": "How Placeholdarr hears about Jellyfin playback: Jellyfin webhook plugin or Tracearr JSON webhook.",
                "type": "choice",
                "options": [
                    {"value": "native", "label": "Jellyfin webhook"},
                    {"value": "tracearr", "label": "Tracearr"},
                ],
                "required": True,
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
            "EMBY_PLAYBACK_NOTIFIER",
            {
                "section": "Media Integrations",
                "label": "Emby playback notifier",
                "description": "How Placeholdarr hears about Emby playback: Emby webhook notifications or Tracearr JSON webhook.",
                "type": "choice",
                "options": [
                    {"value": "native", "label": "Emby webhook"},
                    {"value": "tracearr", "label": "Tracearr"},
                ],
                "required": True,
                "restart_required": False,
            },
        ),
        (
            "TMDB_API_KEY",
            {
                "section": "Optional APIs",
                "label": "TMDB API Key",
                "description": (
                    "TMDB API key (v3 auth) for TMDB poster language, Collections list sources, "
                    "and TMDB Discover catalog mode. Get a free key at themoviedb.org. "
                    "Required when Catalog mode is TMDB Discover."
                ),
                "type": "string",
                "required": False,
                "secret": True,
                "restart_required": False,
            },
        ),
        (
            "CATALOG_MODE",
            {
                "section": "Library sync",
                "label": "Catalog mode",
                "description": (
                    "Arr catalog: sync titles from Radarr/Sonarr (classic Placeholdarr). "
                    "TMDB Discover: seed movie placeholders from TMDB, use Arr only for monitored/hasFile and on-play add/search. "
                    "Both catalogs can live in the same database; switching modes changes which one drives sync and placeholders. "
                    "Restart after changing so schedulers pick up the new mode."
                ),
                "type": "choice",
                "options": [
                    {"value": "arr_catalog", "label": "Arr catalog"},
                    {"value": "tmdb_discover", "label": "TMDB Discover"},
                ],
                "required": True,
                "restart_required": True,
            },
        ),
        (
            "DISCOVER_SKIP_PLACEHOLDER_WHEN_MONITORED",
            {
                "section": "Library sync",
                "label": "Discover: skip placeholder when monitored",
                "description": (
                    "When Catalog mode is TMDB Discover, do not keep a placeholder for titles that are "
                    "already monitored in Radarr (Arr is tracking them)."
                ),
                "type": "choice",
                "options": [
                    {"value": "true", "label": "Yes"},
                    {"value": "false", "label": "No"},
                ],
                "required": False,
                "restart_required": False,
            },
        ),
        (
            "DISCOVER_SKIP_MONITORED_ANY_INSTANCE",
            {
                "section": "Library sync",
                "label": "Discover: monitored on any instance skips",
                "description": (
                    "When skipping monitored titles, treat monitored on any Radarr instance as enough "
                    "to skip the Discover placeholder. Only applies when more than one Radarr is connected."
                ),
                "type": "choice",
                "options": [
                    {"value": "true", "label": "Yes"},
                    {"value": "false", "label": "No"},
                ],
                "required": False,
                "restart_required": False,
            },
        ),
        (
            "DISCOVER_STARTUP_SYNC_MODE",
            {
                "section": "Library sync",
                "label": "Discover startup sync",
                "description": (
                    "What Placeholdarr does on process start when Catalog mode is TMDB Discover. "
                    "On: always run a full Discover catalog sync (seed sources, Arr overlay, placeholders, art). "
                    "Auto: run only when the Discover movie catalog is empty (first boot / after wipe). "
                    "Off: no Discover sync at startup; use Tasks → Discover catalog sync (or a schedule) instead. "
                    "This is separate from Arr Startup ARR sync mode, which does not apply in Discover mode."
                ),
                "type": "choice",
                "restart_required": True,
                "options": [
                    {"value": "on", "label": "On: full Discover sync every startup"},
                    {"value": "auto", "label": "Auto: sync only when the catalog is empty"},
                    {"value": "off", "label": "Off: no startup Discover sync"},
                ],
            },
        ),
        (
            "TRAKT_CLIENT_ID",
            {
                "section": "Optional APIs",
                "label": "Trakt Client ID",
                "description": (
                    "Trakt API Client ID for Collections (public lists and charts). "
                    "Creating a Trakt API application currently requires Trakt VIP "
                    "(trakt.tv/oauth/applications). Paste the Client ID here once you have one. "
                    "Optional; only needed for Trakt collection sources."
                ),
                "type": "string",
                "required": False,
                "secret": True,
                "restart_required": False,
            },
        ),
        (
            "TAUTULLI_URL",
            {
                "section": "Optional APIs",
                "label": "Tautulli URL",
                "description": (
                    "Base URL for outbound Tautulli API calls used by Collections most-popular / most-watched "
                    "sources (e.g. http://tautulli:8181). Separate from playback webhooks. Optional."
                ),
                "type": "string",
                "required": False,
                "restart_required": False,
            },
        ),
        (
            "TAUTULLI_API_KEY",
            {
                "section": "Optional APIs",
                "label": "Tautulli API Key",
                "description": (
                    "Tautulli Settings → Web Interface → API key. Required together with Tautulli URL for "
                    "Collections Tautulli sources."
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
                "description": "Optional JSON array for named ARR instances. By default, Placeholdarr supports up to 4 Radarr and 4 Sonarr instances per deployment (override with ARR_MAX_INSTANCES_PER_TYPE). Changing an instance URL or API key triggers a full resync; label-only changes do not.",
                "type": "string",
                "required": False,
                "restart_required": False,
            },
        ),
        (
            "LIBRARY_ROOT",
            {
                "section": "Paths",
                "label": "Library Root (Arr catalog)",
                "description": (
                    "Arr catalog mode: default Placeholdarr destinations are `movies` and `tv` under this root. "
                    "Unmapped Arr roots use those folders and the default Plex libraries below. "
                    "Use Library destinations only when an Arr root should land in a different folder or Plex library. "
                    "Keep this path separate from Radarr/Sonarr library roots and from Discover Library Root."
                ),
                "type": "path",
                "required": False,
                "restart_required": False,
            },
        ),
        (
            "DISCOVER_LIBRARY_ROOT",
            {
                "section": "Paths",
                "label": "Discover Library Root",
                "description": (
                    "TMDB Discover mode: Placeholdarr writes Discover movie placeholders under "
                    "`movies` on this root (separate from Arr Library Root). "
                    "Point a dedicated Plex/Jellyfin/Emby movies library at that folder. "
                    "Required for Discover when you want isolated disks and faster materialize on a busy Arr tree."
                ),
                "type": "path",
                "required": False,
                "restart_required": False,
            },
        ),
        (
            "PLEX_MOVIE_SECTION_ID",
            {
                "section": "Paths",
                "label": "Default Plex Movies library",
                "description": (
                    "Plex section ID for the Arr Movies destination (Library Root / movies). Required when Plex is "
                    "enabled; ignored for Jellyfin/Emby (they refresh by folder path). Mapped destinations can pick a "
                    "different library."
                ),
                "type": "int",
                "required": False,
                "min": 1,
                "restart_required": False,
            },
        ),
        (
            "PLEX_TV_SECTION_ID",
            {
                "section": "Paths",
                "label": "Default Plex TV library",
                "description": (
                    "Plex section ID for the default TV destination (Library Root / tv). Required when Plex is enabled; "
                    "ignored for Jellyfin/Emby (they refresh by folder path). Mapped destinations can pick a different library."
                ),
                "type": "int",
                "required": False,
                "min": 1,
                "restart_required": False,
            },
        ),
        (
            "DISCOVER_PLEX_MOVIE_SECTION_ID",
            {
                "section": "Paths",
                "label": "Discover Plex Movies library",
                "description": (
                    "Plex section ID for Discover Library Root / movies. "
                    "Use a separate Plex library from Arr Movies when the trees differ. "
                    "Ignored for Jellyfin/Emby (path refresh)."
                ),
                "type": "int",
                "required": False,
                "min": 1,
                "restart_required": False,
            },
        ),
        (
            "LIBRARY_DESTINATION_MAP_JSON",
            {
                "section": "Paths",
                "label": "Library destinations",
                "description": (
                    "Optional map of Arr root folders to Placeholdarr destination folders and Plex libraries. "
                    "Unmapped roots keep the default destinations from Library Root (movies/tv) and the Plex libraries "
                    "above. Jellyfin and Emby do not need library IDs; they refresh by path. "
                    "Changing destinations rematerializes placeholders (Apply now or next full sync)."
                ),
                "type": "string",
                "required": False,
                "restart_required": False,
                "default": "",
            },
        ),
        (
            "STARTUP_SYNC_MODE",
            {
                "section": "Library sync",
                "label": "Startup ARR sync mode",
                # Intro copy is also structured in `frontend/src/App.tsx` (`StartupSyncModeDescription`); update both together.
                "description": (
                    "One input to the single boot sync decision, together with overdue scheduled lite/full tasks. "
                    "At most one sync runs at startup: any full demand wins (overdue full, Full mode, or Auto when a first full is still needed); "
                    "otherwise lite when overdue or when Lite/Auto requests it. "
                    "Off means do not request a sync only because the process started; overdue schedules can still promote a full or lite run. "
                    "Full sync scans *arr catalogs and Placeholdarr roots, then add/delete placeholders as needed. "
                    "Lite sync diffs live catalogs to the database, syncs changed titles, then scoped determination and materialization (no full filesystem scan). "
                    "Placeholdarr work is relatively quick; media players may still take time to rescan large library changes. "
                    "A full sync will automatically start in the background at the completion of this setup."
                ),
                "type": "choice",
                "restart_required": True,
                "options": [
                    {"value": "auto", "label": "Auto: Full when first full is needed; otherwise request lite (overdue full still wins)"},
                    {"value": "full", "label": "Full: always request full on every startup"},
                    {"value": "lite", "label": "Lite: request lite (overdue full still wins)"},
                    {"value": "off", "label": "Off: no startup request; overdue schedules still run"},
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
            "PLACEHOLDER_POLICY_NEVER_TAGS",
            {
                "section": "Library sync",
                "label": "Never placeholder tags",
                "description": (
                    "Movies/series with any of these Arr tags get Never on sync. Default: placeholdarr-never."
                ),
                "type": "string_list",
                "restart_required": False,
                "default": ["placeholdarr-never"],
            },
        ),
        (
            "PLACEHOLDER_POLICY_PINNED_TAGS",
            {
                "section": "Library sync",
                "label": "Pinned placeholder tags",
                "description": (
                    "Movies/series with any of these Arr tags get Pinned on sync (unless a Never tag also matches). "
                    "Default: placeholdarr-pinned."
                ),
                "type": "string_list",
                "restart_required": False,
                "default": ["placeholdarr-pinned"],
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
                "description": "",
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
            "ENABLE_PREFERRED_POSTER_LANGUAGE",
            {
                "section": "Status Updates",
                "label": "Fetch from TMDB",
                "description": "",
                "type": "bool",
                "restart_required": False,
                "default": False,
            },
        ),
        (
            "PREFER_ORIGINAL_POSTER_LANGUAGE",
            {
                "section": "Status Updates",
                "label": "Original language first",
                "description": "",
                "type": "bool",
                "restart_required": False,
                "default": False,
                "depends_on": "ENABLE_PREFERRED_POSTER_LANGUAGE",
                "nested": True,
            },
        ),
        (
            "PREFERRED_POSTER_LANGUAGE",
            {
                "section": "Status Updates",
                "label": "Language",
                "description": "",
                "type": "choice",
                "restart_required": False,
                "default": "en",
                "options": list(_POSTER_LANGUAGE_OPTIONS),
                "depends_on": "ENABLE_PREFERRED_POSTER_LANGUAGE",
                "nested": True,
            },
        ),
        (
            "MOVIE_PLACEHOLDER_SEARCH_MODE",
            {
                "section": "ARR Integrations",
                "label": "Movie placeholder search preference",
                "description": "When a movie placeholder plays, which instance(s) to search. All searches every configured Radarr. You can also force a specific instance by its key (the name you gave it).",
                "type": "choice",
                "restart_required": False,
                "default": "both",
                "options": [
                    {"value": "both", "label": "All instances"},
                    {"value": "match", "label": "Matched by library path (legacy)"},
                    {"value": "primary", "label": "First instance only (legacy)"},
                    {"value": "secondary", "label": "Second instance only (legacy)"},
                ],
            },
        ),
        (
            "MOVIE_PLACEHOLDER_PREFER_PATH_MATCH",
            {
                "section": "ARR Integrations",
                "label": "Movie placeholder prefer path match",
                "description": "When enabled, a unique library destination match overrides the movie placeholder search preference. Shared or unmatched paths still use the preference.",
                "type": "bool",
                "restart_required": False,
                "default": True,
            },
        ),
        (
            "TV_PLACEHOLDER_SEARCH_MODE",
            {
                "section": "ARR Integrations",
                "label": "TV placeholder search preference",
                "description": "When a TV placeholder plays, which instance(s) to search. All searches every configured Sonarr. You can also force a specific instance by its key (the name you gave it).",
                "type": "choice",
                "restart_required": False,
                "default": "both",
                "options": [
                    {"value": "both", "label": "All instances"},
                    {"value": "match", "label": "Matched by library path (legacy)"},
                    {"value": "primary", "label": "First instance only (legacy)"},
                    {"value": "secondary", "label": "Second instance only (legacy)"},
                ],
            },
        ),
        (
            "TV_PLACEHOLDER_PREFER_PATH_MATCH",
            {
                "section": "ARR Integrations",
                "label": "TV placeholder prefer path match",
                "description": "When enabled, a unique library destination match overrides the TV placeholder search preference. Shared or unmatched paths still use the preference.",
                "type": "bool",
                "restart_required": False,
                "default": True,
            },
        ),
        (
            "MOVIE_PLAYBACK_INSTANCE_MODE",
            {
                "section": "ARR Integrations",
                "label": "Movie real-file search preference",
                "description": "When a real movie file is played, which instance(s) to search. All searches every configured Radarr. You can also force a specific instance by its key (the name you gave it).",
                "type": "choice",
                "restart_required": False,
                "default": "both",
                "options": [
                    {"value": "both", "label": "All instances"},
                    {"value": "match", "label": "Matched by library path (legacy)"},
                    {"value": "primary", "label": "First instance only (legacy)"},
                    {"value": "secondary", "label": "Second instance only (legacy)"},
                ],
            },
        ),
        (
            "MOVIE_PLAYBACK_PREFER_PATH_MATCH",
            {
                "section": "ARR Integrations",
                "label": "Movie real-file prefer path match",
                "description": "When enabled, a unique library destination match overrides the movie real-file search preference. Shared or unmatched paths still use the preference.",
                "type": "bool",
                "restart_required": False,
                "default": True,
            },
        ),
        (
            "TV_PLAYBACK_INSTANCE_MODE",
            {
                "section": "ARR Integrations",
                "label": "TV real-file search preference",
                "description": "When a real TV file is played, which instance(s) to search. All searches every configured Sonarr. You can also force a specific instance by its key (the name you gave it).",
                "type": "choice",
                "restart_required": False,
                "default": "both",
                "options": [
                    {"value": "both", "label": "All instances"},
                    {"value": "match", "label": "Matched by library path (legacy)"},
                    {"value": "primary", "label": "First instance only (legacy)"},
                    {"value": "secondary", "label": "Second instance only (legacy)"},
                ],
            },
        ),
        (
            "TV_PLAYBACK_PREFER_PATH_MATCH",
            {
                "section": "ARR Integrations",
                "label": "TV real-file prefer path match",
                "description": "When enabled, a unique library destination match overrides the TV real-file search preference. Shared or unmatched paths still use the preference.",
                "type": "bool",
                "restart_required": False,
                "default": True,
            },
        ),
        (
            "ENABLE_PLAYBACK_FALLBACK_SEARCH",
            {
                "section": "ARR Integrations",
                "label": "Enable playback fallback search",
                "description": "When search is forced to a specific Arr instance, content that is not present there falls back immediately, including missing rows and rows marked deleted. This setting controls delayed fallback only after a search was actually attempted first but did not resolve, such as no found releases or a failed download path.",
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
                "description": "When two Radarr instances share the same Placeholdarr folder, choose when placeholder files are removed from disk. Not used when each instance maps to its own destination.",
                "type": "choice",
                "restart_required": False,
                "default": "protect_siblings",
                "options": [
                    {"value": "protect_siblings", "label": "Remove when all instances have a real file"},
                    {"value": "any_instance_has_file", "label": "Remove when any instance has a real file"},
                ],
            },
        ),
        (
            "SONARR_SHARED_PLACEHOLDER_CLEANUP",
            {
                "section": "ARR Integrations",
                "label": "Sonarr shared placeholder cleanup",
                "description": "When two Sonarr instances share the same Placeholdarr folder, choose when placeholder files are removed from disk. Not used when each instance maps to its own destination.",
                "type": "choice",
                "restart_required": False,
                "default": "protect_siblings",
                "options": [
                    {"value": "protect_siblings", "label": "Remove when all instances have a real file"},
                    {"value": "any_instance_has_file", "label": "Remove when any instance has a real file"},
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
    if value is None:
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
    """True when instance_id is a UUID (stable webhook identity)."""
    text = str(instance_id or "").strip()
    if not text:
        return False
    try:
        uuid.UUID(text)
        return True
    except Exception:
        return False


def _new_arr_instance_id() -> str:
    return str(uuid.uuid4())


def _stable_default_instance_id(arr_type: str, item: dict[str, Any]) -> str:
    """Assign a new stable webhook id for a brand-new Arr slot (UUID)."""
    return _new_arr_instance_id()


def _normalize_arr_instance_url(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


_RESERVED_ARR_INSTANCE_KEYS = frozenset(
    {
        "primary",
        "secondary",
        "additional",
        "standard",
        "4k",
        "match",
        "both",
    }
)


def _merge_arr_instances_for_stable_webhooks(previous_json: str, incoming_json: str) -> str:
    """Preserve stable instance_id values and carry renamed keys as aliases.

    Matched slots always keep their prior ``instance_id`` (UUID or legacy slug).
    Brand-new slots receive a UUID. Blank ``api_key`` values are retained from the
    previous row when the client sent a redacted value.

    URL transplant: when a surviving drawer still sends identity A but its URL
    matches removed peer B, adopt B's ``instance_id`` (and B's key aliases). Do
    not keep A's id on B's server, and do not alias A's key onto B (A must
    tombstone).
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

    from services.source_of_truth.arr_instance_key_rewrite import detect_url_transplants

    transplants = detect_url_transplants(previous_json, incoming_json)
    transplant_by_displaced_id = {
        str(row.get("displaced_instance_id") or "").strip().lower(): row for row in transplants
    }
    displaced_keys = {
        str(row.get("displaced_key") or "").strip().lower()
        for row in transplants
        if str(row.get("displaced_key") or "").strip()
    }

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
        transplant = transplant_by_displaced_id.get(nid) if nid else None
        if transplant:
            adopted_id = str(transplant.get("adopted_instance_id") or "").strip().lower()
            matched = old_by_id.get(adopted_id) if adopted_id else None
        elif nid and nid in old_by_id:
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
            # Identity is immutable once assigned: never rewrite id on rename.
            # URL transplant rebinds to the adopted (URL owner) id above.
            if mid:
                item["instance_id"] = mid
            elif _arr_instance_id_has_uuid(nid):
                item["instance_id"] = nid
            else:
                item["instance_id"] = _new_arr_instance_id()
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
            elif nid:
                # Keep legacy non-UUID ids the UI already shows (webhook continuity).
                item["instance_id"] = nid
            else:
                item["instance_id"] = _new_arr_instance_id()

        aliases = [a for a in aliases if a and a != new_key and a not in displaced_keys]
        seen: set[str] = set()
        deduped: list[str] = []
        for a in aliases:
            if a not in seen:
                deduped.append(a)
                seen.add(a)
        item["instance_key_aliases"] = deduped[:alias_cap]
        # Never persist client-only redaction flags or retired routing fields.
        item.pop("api_key_saved", None)
        item.pop("role", None)
        item.pop("is_4k", None)

    # Safety net: never emit two slots with the same instance_id (e.g. UI restored a
    # cached draft whose id was later adopted by the surviving slot after transplant).
    seen_out_ids: set[str] = set()
    for item in incoming:
        if not isinstance(item, dict):
            continue
        arr_type = str(item.get("arr_type") or item.get("type") or "").strip().lower()
        if arr_type not in {"radarr", "sonarr"}:
            continue
        oid = str(item.get("instance_id") or "").strip().lower()
        if not oid:
            item["instance_id"] = _new_arr_instance_id()
            oid = str(item.get("instance_id") or "").strip().lower()
        if oid in seen_out_ids:
            item["instance_id"] = _new_arr_instance_id()
            item["instance_key_aliases"] = []
            oid = str(item.get("instance_id") or "").strip().lower()
        seen_out_ids.add(oid)

    # Safety net: primary keys and aliases must be unique across rows. Prefer keeping
    # earlier rows' tokens; remint later keys and drop colliding aliases.
    taken_tokens: set[str] = set()
    for item in incoming:
        if not isinstance(item, dict):
            continue
        arr_type = str(item.get("arr_type") or item.get("type") or "").strip().lower()
        if arr_type not in {"radarr", "sonarr"}:
            continue
        key = _normalize_instance_key(item.get("instance_key") or item.get("key") or item.get("name") or "")
        if not key or key in taken_tokens or key in _RESERVED_ARR_INSTANCE_KEYS:
            base = arr_type or "arr"
            n = 1
            candidate = f"{base}{n}"
            while candidate in taken_tokens or candidate in _RESERVED_ARR_INSTANCE_KEYS:
                n += 1
                candidate = f"{base}{n}"
            item["instance_key"] = candidate
            item["instance_key_aliases"] = []
            key = candidate
        taken_tokens.add(key)
        cleaned_aliases: list[str] = []
        for a in item.get("instance_key_aliases") or []:
            ak = _normalize_instance_key(a)
            if not ak or ak == key or ak in taken_tokens or ak in _RESERVED_ARR_INSTANCE_KEYS:
                continue
            cleaned_aliases.append(ak)
            taken_tokens.add(ak)
        item["instance_key_aliases"] = cleaned_aliases

    return json.dumps(incoming)


def _redact_arr_instances_json_for_payload(raw: Any) -> tuple[str, bool]:
    """Normalize ARR_INSTANCES_JSON for the authenticated settings UI.

    API keys are included so the UI can mask them with an explicit Show/Hide
    control (same as other secret settings fields). ``api_key_saved`` remains
    for clients that still treat a blank key as "retain on save".
    """
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
    normalized: list[Any] = []
    for item in payload:
        if not isinstance(item, dict):
            normalized.append(item)
            continue
        row = dict(item)
        key = str(row.get("api_key") or row.get("apikey") or "").strip()
        key_present = bool(key)
        if key_present:
            any_saved = True
        row["api_key"] = key
        row.pop("apikey", None)
        row["api_key_saved"] = key_present
        normalized.append(row)
    return (json.dumps(normalized), any_saved)


def _coerce_string_list(raw_value: Any) -> str:
    """Normalize a string-list setting to a canonical JSON array string for storage."""
    if isinstance(raw_value, list):
        items = raw_value
    elif isinstance(raw_value, str):
        text = raw_value.strip()
        if not text:
            items = []
        else:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    items = parsed
                else:
                    items = [p.strip() for p in text.split(",") if p.strip()]
            except Exception:
                items = [p.strip() for p in text.split(",") if p.strip()]
    elif raw_value is None:
        items = []
    else:
        raise ValueError("must be a JSON array of strings")

    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        label = str(item or "").strip()
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(label)
    return json.dumps(out)


def _parse_string_list_value(raw_value: Any, *, default: list[str] | None = None) -> list[str]:
    """Decode a stored string-list setting for API/UI payloads."""
    fallback = list(default or [])
    if raw_value is None:
        return fallback
    if isinstance(raw_value, list):
        items = raw_value
    elif isinstance(raw_value, str):
        text = raw_value.strip()
        if not text:
            return fallback
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                items = parsed
            else:
                items = [p.strip() for p in text.split(",") if p.strip()]
        except Exception:
            items = [p.strip() for p in text.split(",") if p.strip()]
    else:
        return fallback
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        label = str(item or "").strip()
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(label)
    return out


def _validate_value(key: str, raw_value: Any) -> Any:
    meta = SETTINGS_SCHEMA[key]
    value_type = meta["type"]
    if value_type == "bool":
        value = _coerce_bool(raw_value)
    elif value_type == "int":
        # Optional ints (e.g. Discover Plex section) may arrive as null/"" from the UI.
        if _is_blank(raw_value):
            if bool(meta.get("required", False)):
                raise ValueError("is required")
            value = None
        else:
            value = _coerce_int(raw_value)
            if "min" in meta and value < int(meta["min"]):
                raise ValueError(f"must be >= {meta['min']}")
    elif value_type == "url":
        value = _coerce_url(raw_value)
    elif value_type == "path":
        value = _coerce_path(raw_value)
    elif value_type == "choice":
        if key in {
            "DISCOVER_SKIP_PLACEHOLDER_WHEN_MONITORED",
            "DISCOVER_SKIP_MONITORED_ANY_INSTANCE",
        }:
            # Accept legacy bool / "True" strings from earlier schema.
            try:
                value = "true" if _coerce_bool(raw_value if raw_value not in (None, "") else True) else "false"
            except ValueError:
                value = "true"
        else:
            value = str(raw_value or "").strip()
        if key == "PLACEHOLDER_STATUS_PROJECTION_MODE" and value.lower() == "off":
            value = "both"
        if key in _INSTANCE_SEARCH_MODE_KEYS:
            value = value.lower() or "match"
            if value in _INSTANCE_SEARCH_MODE_RESERVED:
                pass
            elif _INSTANCE_KEY_MODE_RE.fullmatch(value):
                pass
            else:
                raise ValueError(
                    "must be match, both, primary, secondary, or a configured instance key"
                )
        else:
            allowed = [str(o["value"]) for o in meta.get("options", [])]
            if not allowed:
                raise ValueError("choice field missing options")
            if value not in allowed:
                raise ValueError(f"must be one of: {', '.join(allowed)}")
    elif value_type == "string_list":
        value = _coerce_string_list(raw_value)
    else:
        value = str(raw_value or "").strip()
    if bool(meta.get("required", False)) and _is_blank(value):
        raise ValueError("is required")
    return value


def _get_row(session, key: str) -> AppConfig | None:
    return session.query(AppConfig).filter(AppConfig.key == key).first()


_MEDIA_TEST_CREDENTIAL_KEYS = frozenset({"PLEX_TOKEN", "JELLYFIN_TOKEN", "EMBY_TOKEN"})


def _normalize_integration_url(url: str) -> str:
    text = str(url or "").strip().rstrip("/")
    return text.lower()


def resolve_integration_test_credential(
    *,
    service: str,
    url: str,
    credential: str,
    credential_key: str | None = None,
    instance_id: str | None = None,
    session=None,
) -> tuple[str | None, str | None]:
    """Resolve a connection-test credential, falling back to saved secrets when blank.

    Returns ``(credential, error_message)``. When ``error_message`` is set, the
    caller should reject the request.
    """
    provided = str(credential or "").strip()
    if provided:
        return provided, None

    owns_session = session is None
    session = session or get_session()
    try:
        service_key = str(service or "").strip().lower()
        cred_key = str(credential_key or "").strip()
        if cred_key:
            if cred_key not in _MEDIA_TEST_CREDENTIAL_KEYS:
                return None, "unsupported credential_key"
            expected = {
                "PLEX_TOKEN": "plex",
                "JELLYFIN_TOKEN": "jellyfin",
                "EMBY_TOKEN": "emby",
            }.get(cred_key)
            if expected and service_key != expected:
                return None, "credential_key does not match service"
            row = _get_row(session, cred_key)
            saved = str(row.value or "").strip() if row else ""
            if saved:
                return saved, None
            return None, "credential is required (no saved token)"

        if service_key in {"radarr", "sonarr"}:
            row = _get_row(session, "ARR_INSTANCES_JSON")
            raw = str(row.value or "").strip() if row else ""
            if not raw:
                return None, "credential is required (no saved ARR instances)"
            try:
                payload = json.loads(raw)
            except Exception:
                return None, "credential is required (invalid saved ARR instances)"
            if not isinstance(payload, list):
                return None, "credential is required (invalid saved ARR instances)"
            want_id = str(instance_id or "").strip().lower()
            want_url = _normalize_integration_url(url)
            for item in payload:
                if not isinstance(item, dict):
                    continue
                arr_type = str(item.get("arr_type") or item.get("type") or "").strip().lower()
                if arr_type != service_key:
                    continue
                item_id = str(item.get("instance_id") or item.get("id") or "").strip().lower()
                item_url = _normalize_integration_url(str(item.get("url") or ""))
                matched = False
                if want_id and item_id and want_id == item_id:
                    matched = True
                elif want_url and item_url and want_url == item_url:
                    matched = True
                if not matched:
                    continue
                saved = str(item.get("api_key") or item.get("apikey") or "").strip()
                if saved:
                    return saved, None
                return None, "credential is required (no saved API key for this instance)"
            return None, "credential is required (no matching saved ARR instance)"

        return None, "credential is required"
    finally:
        if owns_session:
            session.close()


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
    """Derive default movie/TV destination folders from LIBRARY_ROOT / Discover root."""
    root = str(getattr(settings, "LIBRARY_ROOT", "") or "").strip()
    if root:
        movie = os.path.join(root, "movies")
        tv = os.path.join(root, "tv")
        _set_runtime_value("MOVIE_LIBRARY_FOLDER", movie)
        _set_runtime_value("TV_LIBRARY_FOLDER", tv)
    discover_root = str(getattr(settings, "DISCOVER_LIBRARY_ROOT", "") or "").strip()
    if discover_root:
        _set_runtime_value("DISCOVER_MOVIE_LIBRARY_FOLDER", os.path.join(discover_root, "movies"))


_LEGACY_4K_FOLDER_KEYS = ("MOVIE_LIBRARY_4K_FOLDER", "TV_LIBRARY_4K_FOLDER")
_LEGACY_4K_PLEX_SECTION_KEYS = ("PLEX_MOVIE_4K_SECTION_ID", "PLEX_TV_4K_SECTION_ID")
_RETIRED_ARR_INSTANCE_FIELDS = ("role", "is_4k")


def migrate_legacy_library_4k_folders(session=None) -> dict[str, Any]:
    """Drop removed 4K folder and 4K Plex section settings.

    Idempotent. Default destinations come from LIBRARY_ROOT; extra trees use the
    dest map. Mapped destinations carry their own Plex section IDs.
    """
    owns_session = session is None
    session = session or get_session()
    deleted_keys: list[str] = []
    try:
        target_keys = _LEGACY_4K_FOLDER_KEYS + _LEGACY_4K_PLEX_SECTION_KEYS
        rows = (
            session.query(AppConfig)
            .filter(AppConfig.key.in_(target_keys))
            .all()
        )
        for row in rows:
            deleted_keys.append(str(row.key))
            session.delete(row)
        if deleted_keys:
            session.commit()
            logger.info(
                f"Removed legacy 4K path/section settings: {', '.join(deleted_keys)}",
                extra={"emoji_type": "update"},
            )
        _apply_runtime_library_defaults()
        return {"ok": True, "deleted_keys": deleted_keys}
    except Exception as exc:
        session.rollback()
        logger.warning(
            f"Legacy library 4K settings migration failed: {exc}",
            extra={"emoji_type": "warning"},
        )
        return {"ok": False, "deleted_keys": [], "error": str(exc)}
    finally:
        if owns_session:
            session.close()


def migrate_arr_instances_drop_role_is_4k(session=None) -> dict[str, Any]:
    """Rewrite saved ARR_INSTANCES_JSON without retired role/is_4k routing fields.

    Idempotent. Identity stays instance_key + instance_id; list order / priority
    remains rank. Does not change URLs or API keys.
    """
    owns_session = session is None
    session = session or get_session()
    try:
        row = _get_row(session, "ARR_INSTANCES_JSON")
        if row is None or _is_blank(row.value):
            return {"ok": True, "rewritten": False, "stripped_fields": 0}
        raw = str(row.value or "").strip()
        try:
            payload = json.loads(raw)
        except Exception as exc:
            logger.warning(
                f"ARR instance role/is_4k cleanup skipped (invalid JSON): {exc}",
                extra={"emoji_type": "warning"},
            )
            return {"ok": False, "rewritten": False, "stripped_fields": 0, "error": str(exc)}
        if not isinstance(payload, list):
            return {"ok": True, "rewritten": False, "stripped_fields": 0}

        stripped = 0
        cleaned: list[Any] = []
        changed = False
        for item in payload:
            if not isinstance(item, dict):
                cleaned.append(item)
                continue
            row_out = dict(item)
            for field in _RETIRED_ARR_INSTANCE_FIELDS:
                if field in row_out:
                    row_out.pop(field, None)
                    stripped += 1
                    changed = True
            cleaned.append(row_out)

        if not changed:
            return {"ok": True, "rewritten": False, "stripped_fields": 0}

        new_raw = json.dumps(cleaned)
        row.value = new_raw
        if hasattr(row, "value_type") and not row.value_type:
            row.value_type = "string"
        session.commit()
        _set_runtime_value("ARR_INSTANCES_JSON", new_raw)
        logger.info(
            f"Cleaned ARR_INSTANCES_JSON: removed {stripped} retired role/is_4k field(s)",
            extra={"emoji_type": "update"},
        )
        return {"ok": True, "rewritten": True, "stripped_fields": stripped}
    except Exception as exc:
        session.rollback()
        logger.warning(
            f"ARR instance role/is_4k cleanup failed: {exc}",
            extra={"emoji_type": "warning"},
        )
        return {"ok": False, "rewritten": False, "stripped_fields": 0, "error": str(exc)}
    finally:
        if owns_session:
            session.close()


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
        # Own sessions so delete/rewrite commits do not share the read session below.
        migrate_legacy_library_4k_folders()
        migrate_arr_instances_drop_role_is_4k()
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
            # Settings.ARR_INSTANCES_JSON defaults to "" so getattr never falls back to the DB
            # row. Prefer a persisted value so a restart cannot look like a disconnect.
            if key == "ARR_INSTANCES_JSON" and row is not None and not _is_blank(row.value):
                effective_value = row.value
            if key in {"RADARR_SHARED_PLACEHOLDER_CLEANUP", "SONARR_SHARED_PLACEHOLDER_CLEANUP"}:
                if _is_blank(effective_value):
                    legacy_row = _get_row(session, "MULTI_INSTANCE_SHARED_PLACEHOLDER_CLEANUP")
                    if legacy_row and not _is_blank(legacy_row.value):
                        effective_value = legacy_row.value
            if key == "PLACEHOLDER_STATUS_PROJECTION_MODE":
                ev = str(effective_value or "both").strip().lower()
                if ev == "off" or ev not in {"summary", "title", "both"}:
                    effective_value = "both"
            if meta["type"] == "string_list":
                default_list = meta.get("default")
                defaults = list(default_list) if isinstance(default_list, list) else []
                if _is_blank(effective_value) and not row:
                    effective_value = defaults
                else:
                    effective_value = _parse_string_list_value(effective_value, default=defaults)
            if _is_blank(effective_value):
                pass
            saved_value_out = row.value if row else None
            if key == "PLACEHOLDER_STATUS_PROJECTION_MODE" and saved_value_out is not None:
                sv = str(saved_value_out).strip().lower()
                if sv == "off" or sv not in {"summary", "title", "both"}:
                    saved_value_out = "both"
            if meta["type"] == "string_list" and saved_value_out is not None:
                saved_value_out = _parse_string_list_value(saved_value_out, default=[])
            entry: dict[str, Any] = {
                "key": key,
                "section": meta["section"],
                "label": meta["label"],
                "description": meta["description"],
                "type": meta["type"],
                "required": bool(meta.get("required", False)),
                "secret": bool(meta.get("secret", False)),
                "restart_required": bool(meta.get("restart_required", False)),
                # Authenticated settings UI needs the value so secret fields can show
                # masked input with an explicit Show/Hide control.
                "value": effective_value,
                "saved_value": saved_value_out,
                "has_saved_value": bool((row and row.value not in (None, ""))),
            }
            if meta["type"] == "choice":
                entry["options"] = list(meta.get("options") or [])
            if meta["type"] == "string_list" and isinstance(meta.get("default"), list):
                entry["default"] = list(meta["default"])
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

        from services.auth import ensure_webhook_api_key

        return {
            "status": get_onboarding_status(session=session),
            "sections": [{"name": name, "fields": fields} for name, fields in grouped.items()],
            # Own session: do not commit the settings-read transaction.
            "webhook_api_key": ensure_webhook_api_key(),
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
    arr_transplant_movie_ids: list[int] = []
    arr_transplant_episode_ids: list[int] = []
    arr_instances_previous_json: str | None = None
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
                elif key in {
                    "PLEX_MOVIE_SECTION_ID",
                    "PLEX_TV_SECTION_ID",
                    "DISCOVER_PLEX_MOVIE_SECTION_ID",
                } and _is_blank(raw_value):
                    validated[key] = None
                else:
                    validated[key] = _validate_value(key, raw_value)
            except Exception as exc:
                errors[key] = str(exc)

        # Default destinations: derive movie/tv folders from LIBRARY_ROOT.
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

        if "DISCOVER_LIBRARY_ROOT" in validated:
            discover_root = str(validated.get("DISCOVER_LIBRARY_ROOT") or "").strip()
            if discover_root:
                dir_mode = _parse_octal_mode(
                    validated.get("PLACEHOLDER_DIR_MODE", getattr(settings, "PLACEHOLDER_DIR_MODE", "777")),
                    0o777,
                )
                discover_created = _ensure_library_root_folders(discover_root, dir_mode)
                for path in discover_created:
                    if path not in created_paths:
                        created_paths.append(path)
                discover_movie = os.path.join(discover_root, "movies")
                if discover_movie not in derived_library_paths:
                    derived_library_paths.append(discover_movie)
                _set_runtime_value("DISCOVER_MOVIE_LIBRARY_FOLDER", discover_movie)

        arr_root = str(validated.get("LIBRARY_ROOT", getattr(settings, "LIBRARY_ROOT", "")) or "").strip()
        discover_root_check = str(
            validated.get("DISCOVER_LIBRARY_ROOT", getattr(settings, "DISCOVER_LIBRARY_ROOT", "")) or ""
        ).strip()
        if arr_root and discover_root_check:
            arr_n = os.path.normpath(arr_root).lower()
            disc_n = os.path.normpath(discover_root_check).lower()
            if arr_n == disc_n:
                errors["DISCOVER_LIBRARY_ROOT"] = (
                    "must be a different path from Library Root (Arr catalog); "
                    "Discover and Arr placeholders should not share the same tree"
                )

        # Plex completeness is for full saves or partials that touch Plex enable/credentials.
        # Paths / TMDB / sources steps must not fail because Plex is not configured yet.
        plex_config_keys = {"ENABLE_PLEX", "PLEX_URL", "PLEX_TOKEN"}
        if partial and not plex_config_keys.intersection(validated.keys()):
            enable_plex = False
        else:
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

        destination_rematerialize_keys_changed: list[str] = []
        for key in DESTINATION_REMATERIALIZE_SETTING_KEYS:
            if key not in validated:
                continue
            prev_row = _get_row(session, key)
            prev_val = "" if not prev_row or prev_row.value is None else str(prev_row.value).strip()
            new_val = str(validated.get(key) or "").strip()
            if prev_val != new_val:
                destination_rematerialize_keys_changed.append(key)

        if "LIBRARY_DESTINATION_MAP_JSON" in validated:
            raw_map = str(validated.get("LIBRARY_DESTINATION_MAP_JSON") or "").strip()
            if raw_map:
                try:
                    from services.library_destinations import (
                        parse_library_destination_map,
                        validate_library_destination_map_unique,
                    )

                    # Round-trip normalize so invalid JSON fails loudly at save.
                    import json as _json

                    payload = _json.loads(raw_map)
                    if not isinstance(payload, list):
                        errors["LIBRARY_DESTINATION_MAP_JSON"] = "must be a JSON array"
                    else:
                        normalized_rows = parse_library_destination_map(raw_map)
                        dup_err = validate_library_destination_map_unique(normalized_rows)
                        if dup_err:
                            errors["LIBRARY_DESTINATION_MAP_JSON"] = dup_err
                        else:
                            validated["LIBRARY_DESTINATION_MAP_JSON"] = _json.dumps(
                                normalized_rows,
                                separators=(",", ":"),
                            )
                except Exception as exc:
                    errors["LIBRARY_DESTINATION_MAP_JSON"] = f"invalid JSON: {exc}"

        if "ARR_INSTANCES_JSON" in validated:
            prev_row = _get_row(session, "ARR_INSTANCES_JSON")
            prev_raw = str(prev_row.value if prev_row and prev_row.value is not None else "") or ""
            arr_instances_previous_json = prev_raw
            incoming_raw = str(validated.get("ARR_INSTANCES_JSON") or "")
            merged = _merge_arr_instances_for_stable_webhooks(prev_raw, incoming_raw)
            # Soft guard: empty overwrite of a populated config is almost always a
            # client bug (redacted keys filtered out of form state). Keep the
            # previous value and log loudly. Intentional full clear is rare; users
            # can still clear by disconnecting slots then saving once this path is
            # revisited with an explicit clear flag if needed.
            try:
                prev_list = json.loads(prev_raw) if str(prev_raw or "").strip() else []
            except Exception:
                prev_list = []
            try:
                merged_list = json.loads(merged) if str(merged or "").strip() else []
            except Exception:
                merged_list = []
            if (
                isinstance(prev_list, list)
                and isinstance(merged_list, list)
                and len(prev_list) > 0
                and len(merged_list) == 0
            ):
                logger.error(
                    "Refusing accidental ARR_INSTANCES_JSON wipe (client sent empty list; "
                    f"keeping {len(prev_list)} previous instance(s))",
                    extra={"emoji_type": "error"},
                )
                validated["ARR_INSTANCES_JSON"] = prev_raw
            else:
                validated["ARR_INSTANCES_JSON"] = merged
                try:
                    from services.source_of_truth.arr_instance_key_rewrite import (
                        apply_instance_key_renames,
                        prepare_url_transplants,
                    )

                    dest_for_rewrite = None
                    if "LIBRARY_DESTINATION_MAP_JSON" in validated:
                        dest_for_rewrite = str(validated.get("LIBRARY_DESTINATION_MAP_JSON") or "")
                    transplant_prep = prepare_url_transplants(
                        prev_raw,
                        incoming_raw,
                        destination_map_json=dest_for_rewrite,
                    )
                    arr_transplant_movie_ids = list(transplant_prep.get("movie_ids") or [])
                    arr_transplant_episode_ids = list(transplant_prep.get("episode_ids") or [])
                    rewritten_dest = transplant_prep.get("destination_map_json")
                    if (
                        transplant_prep.get("ok")
                        and rewritten_dest is not None
                        and "LIBRARY_DESTINATION_MAP_JSON" in validated
                        and str(rewritten_dest) != str(validated.get("LIBRARY_DESTINATION_MAP_JSON") or "")
                    ):
                        validated["LIBRARY_DESTINATION_MAP_JSON"] = rewritten_dest
                        dest_for_rewrite = str(rewritten_dest)
                    rewrite = apply_instance_key_renames(
                        prev_raw,
                        merged,
                        destination_map_json=dest_for_rewrite,
                    )
                    rewritten_dest = rewrite.get("destination_map_json")
                    if (
                        rewrite.get("ok")
                        and rewritten_dest is not None
                        and "LIBRARY_DESTINATION_MAP_JSON" in validated
                        and str(rewritten_dest) != str(validated.get("LIBRARY_DESTINATION_MAP_JSON") or "")
                    ):
                        validated["LIBRARY_DESTINATION_MAP_JSON"] = rewritten_dest
                except Exception as rewrite_exc:
                    logger.error(
                        f"ARR instance_key rename rewrite after settings save failed: {rewrite_exc}",
                        extra={"emoji_type": "error"},
                    )

        arr_instances_json = str(validated.get("ARR_INSTANCES_JSON", getattr(settings, "ARR_INSTANCES_JSON", "")) or "").strip()
        arr_limit = max(1, int(getattr(settings, "ARR_MAX_INSTANCES_PER_TYPE", 4) or 4))
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
                    if instance_key in _RESERVED_ARR_INSTANCE_KEYS:
                        raise ValueError(
                            f"item {index + 1} instance_key '{instance_key}' is reserved; "
                            "choose a different name (for example sonarr_4k or sonarr2)"
                        )
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
                            if ak in _RESERVED_ARR_INSTANCE_KEYS:
                                raise ValueError(
                                    f"item {index + 1} alias '{ak}' is reserved; remove or rename the alias"
                                )
                            tokens.append(ak)
                    for t in tokens:
                        if t in reserved_tokens:
                            raise ValueError(
                                f"item {index + 1} instance_key or alias '{t}' conflicts with another instance row"
                            )
                        reserved_tokens.add(t)
                    url = str(item.get("url") or "").strip()
                    api_key = str(item.get("api_key") or item.get("apikey") or "").strip()
                    label = str(item.get("label") or instance_key or f"item {index + 1}").strip()
                    if not url:
                        raise ValueError(f"{label}: url is required")
                    # Blank api_key is allowed after merge when the client sent a redacted
                    # value and no prior secret exists (URL-only shell), or while the user is
                    # still pasting keys. Runtime configured_arr_instances skips unkeyed rows.
                    if not api_key:
                        logger.warning(
                            f"ARR_INSTANCES_JSON: '{label}' has no api_key after merge; "
                            "saving as inactive until a key is provided",
                            extra={"emoji_type": "warning"},
                        )
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
        plex_location_cache_keys = {
            "ENABLE_PLEX",
            "PLEX_URL",
            "PLEX_TOKEN",
            "PLEX_MOVIE_SECTION_ID",
            "PLEX_TV_SECTION_ID",
            "DISCOVER_PLEX_MOVIE_SECTION_ID",
            "LIBRARY_ROOT",
            "DISCOVER_LIBRARY_ROOT",
        }
        if plex_location_cache_keys.intersection(saved_keys):
            try:
                from services.media_servers.plex import clear_plex_section_location_cache

                clear_plex_section_location_cache()
            except Exception:
                pass
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
                    str(validated.get("ARR_INSTANCES_JSON") or ""),
                    extra_movie_ids=arr_transplant_movie_ids,
                    extra_episode_ids=arr_transplant_episode_ids,
                    previous_arr_instances_json=arr_instances_previous_json,
                )
            except Exception as exc:
                logger.error(
                    f"ARR instance reconcile after settings save failed: {exc}",
                    extra={"emoji_type": "error"},
                )
        _set_runtime_value("PLACEHOLDER_CREATE_NFO", True)
        backfill_summary: dict[str, Any] | None = None
        art_backfill_summary: dict[str, Any] | None = None
        if (
            "ENABLE_PREFERRED_POSTER_LANGUAGE" in art_backfill_keys_changed
            or "PREFERRED_POSTER_LANGUAGE" in art_backfill_keys_changed
            or "PREFER_ORIGINAL_POSTER_LANGUAGE" in art_backfill_keys_changed
        ):
            try:
                from services.poster_language import clear_stale_localized_posters

                clear_stale_localized_posters(session)
                session.commit()
            except Exception as exc:
                logger.warning(
                    f"Failed to clear stale localized posters after language change: {exc}",
                    extra={"emoji_type": "warning"},
                )
                try:
                    session.rollback()
                except Exception:
                    pass
        if (nfo_backfill_keys_changed or art_backfill_keys_changed) and apply_scope:
            from services.source_of_truth.placeholder_refresh import execute_placeholder_refresh_apply_scope

            # Poster language must apply to existing placeholders too; never leave them on a stale language.
            language_keys_changed = bool(
                {
                    "ENABLE_PREFERRED_POSTER_LANGUAGE",
                    "PREFERRED_POSTER_LANGUAGE",
                    "PREFER_ORIGINAL_POSTER_LANGUAGE",
                }.intersection(art_backfill_keys_changed)
            )
            effective_scope = str(apply_scope)
            if language_keys_changed and effective_scope == "future":
                effective_scope = "next_full_sync"

            refresh_out = execute_placeholder_refresh_apply_scope(
                apply_scope=effective_scope,
                metadata=bool(nfo_backfill_keys_changed),
                art=bool(art_backfill_keys_changed),
                templates=False,
                source="settings_save",
                task_run_trigger="settings_change" if effective_scope == "now" else None,
            )
            if (
                language_keys_changed
                and effective_scope == "now"
            ):
                try:
                    from services.source_of_truth.poster_language_job import (
                        enqueue_poster_language_resolve_stale,
                    )

                    enqueue_poster_language_resolve_stale(source="settings_save:poster_language")
                except Exception as lang_exc:
                    logger.warning(
                        f"Poster language resolve enqueue after settings save failed: {lang_exc}",
                        extra={"emoji_type": "warning"},
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
                f"Placeholder refresh after settings save scope={effective_scope} "
                f"metadata_keys={nfo_backfill_keys_changed} art_keys={art_backfill_keys_changed}",
                extra={"emoji_type": "processing"},
            )

        destination_rematerialize_summary: dict[str, Any] | None = None
        if destination_rematerialize_keys_changed and apply_scope:
            effective_dest_scope = str(apply_scope)
            if effective_dest_scope == "future":
                effective_dest_scope = "next_full_sync"
            try:
                from services.library_destination_rematerialize import enqueue_destination_rematerialize

                destination_rematerialize_summary = enqueue_destination_rematerialize(
                    source="settings_save:destination_map",
                    apply_now=(effective_dest_scope == "now"),
                )
                logger.info(
                    f"Destination rematerialize after settings save scope={effective_dest_scope} "
                    f"keys={destination_rematerialize_keys_changed}",
                    extra={"emoji_type": "processing"},
                )
            except Exception as dest_exc:
                logger.warning(
                    f"Destination rematerialize after settings save failed: {dest_exc}",
                    extra={"emoji_type": "warning"},
                )
                destination_rematerialize_summary = {"ok": False, "error": str(dest_exc)}

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
            "destination_rematerialize_keys_changed": destination_rematerialize_keys_changed,
            "destination_rematerialize": destination_rematerialize_summary,
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