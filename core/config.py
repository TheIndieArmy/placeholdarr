import json
import os
from pathlib import Path
from typing import Any, Literal, Optional
try:
    from dotenv import load_dotenv
except Exception:
    # Fallback shim when python-dotenv isn't installed in the runtime.
    # The real project normally depends on python-dotenv; this no-op
    # implementation allows lightweight maintenance scripts to import
    # the config module without requiring the external package.
    def load_dotenv(path=None, **kwargs):
        return False
from pydantic_settings import BaseSettings
from pydantic import validator, root_validator
import logging

logger = logging.getLogger(__name__)


def parse_configured_arr_instances_json(raw: str) -> list[dict[str, Any]]:
    """Parse ``ARR_INSTANCES_JSON`` into the same normalized list as ``Settings.configured_arr_instances``."""
    parsed_instances: list[dict[str, Any]] = []
    raw_str = str(raw or "").strip()

    if raw_str:
        try:
            payload = json.loads(raw_str)
            if isinstance(payload, list):
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    arr_type = str(item.get("arr_type") or item.get("type") or "").strip().lower()
                    if arr_type not in {"radarr", "sonarr"}:
                        continue
                    key_raw = str(item.get("instance_key") or item.get("key") or item.get("name") or "").strip().lower()
                    instance_key = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in key_raw).strip("_-")
                    url = str(item.get("url") or "").strip()
                    api_key = str(item.get("api_key") or item.get("apikey") or "").strip()
                    if not instance_key or not url or not api_key:
                        continue
                    instance_id = str(item.get("instance_id") or item.get("id") or "").strip().lower()
                    if not instance_id:
                        instance_id = f"{arr_type}:{instance_key}"
                    aliases_raw = item.get("instance_key_aliases") if isinstance(item.get("instance_key_aliases"), list) else []
                    instance_key_aliases: list[str] = []
                    for a in aliases_raw:
                        akey = "".join(
                            ch if ch.isalnum() or ch in {"_", "-"} else "_"
                            for ch in str(a or "").strip().lower()
                        ).strip("_-")
                        if akey and akey != instance_key and akey not in instance_key_aliases:
                            instance_key_aliases.append(akey)
                    role_raw = str(item.get("role") or "").strip().lower()
                    role = role_raw if role_raw in {"primary", "secondary", "additional"} else ""
                    try:
                        priority = int(item.get("priority"))
                    except Exception:
                        priority = -1
                    parsed_instances.append(
                        {
                            "instance_id": instance_id,
                            "instance_key": instance_key,
                            "instance_key_aliases": instance_key_aliases,
                            "arr_type": arr_type,
                            "url": url,
                            "api_key": api_key,
                            "label": str(item.get("label") or instance_key).strip() or instance_key,
                            "role": role,
                            "priority": priority,
                        }
                    )
        except Exception as exc:
            logger.warning(f"Failed to parse ARR_INSTANCES_JSON: {exc}", extra={"emoji_type": "error"})

    if parsed_instances:
        deduped: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for item in parsed_instances:
            instance_id = str(item.get("instance_id") or "").strip().lower()
            if not instance_id or instance_id in seen_ids:
                continue
            deduped.append(item)
            seen_ids.add(instance_id)
        if deduped:
            rank_by_type: dict[str, int] = {"radarr": 0, "sonarr": 0}
            normalized: list[dict[str, Any]] = []
            for item in deduped:
                arr_type = str(item.get("arr_type") or "").strip().lower()
                rank = int(rank_by_type.get(arr_type, 0))
                rank_by_type[arr_type] = rank + 1
                row = dict(item)
                role = str(row.get("role") or "").strip().lower()
                if role not in {"primary", "secondary", "additional"}:
                    role = "primary" if rank == 0 else ("secondary" if rank == 1 else "additional")
                row["role"] = role
                if int(row.get("priority", -1)) < 0:
                    row["priority"] = rank
                row["is_4k"] = role != "primary"
                normalized.append(row)
            return sorted(
                normalized,
                key=lambda item: (
                    str(item.get("arr_type") or "").strip().lower(),
                    int(item.get("priority", 0)),
                ),
            )

    return []


def _parse_octal_mode(value: str, default: int) -> int:
    text = str(value or '').strip().lower()
    if not text:
        return default
    if text.startswith('0o'):
        text = text[2:]
    if text.startswith('0') and len(text) > 1:
        text = text[1:]
    try:
        return int(text, 8)
    except Exception:
        return default

# Get the project root directory (where main.py is)
ROOT_DIR = Path(__file__).parent.parent

# Use project root for .env path
dotenv_path = ROOT_DIR / ".env"

if dotenv_path.exists():
    load_dotenv(dotenv_path)
    logger.info(f"Loaded .env from {dotenv_path}")
else:
    logger.info(f"No .env file at {dotenv_path}, using process environment")

class Settings(BaseSettings):
    APPDATA_PATH: str = os.getenv("APPDATA_PATH", "/config").split('#')[0].strip()
    LOG_DIR: str = os.getenv("LOG_DIR", "").split('#')[0].strip()
    LOG_FILE: str = os.getenv("LOG_FILE", "").split('#')[0].strip()
    LOG_MAX_RUN_FILES: int = int(os.getenv("LOG_MAX_RUN_FILES", "10").split('#')[0].strip())
    # Stall / liveness ticks (VERBOSE). Interval only; does not promote them to INFO.
    STALL_HEARTBEAT_INTERVAL_SEC: float = float(os.getenv("STALL_HEARTBEAT_INTERVAL_SEC", "10").split('#')[0].strip())
    WORKER_COUNT: int = 4

    # Plex
    PLEX_URL: Optional[str] = None
    PLEX_TOKEN: Optional[str] = None
    PLEX_MOVIE_SECTION_ID: Optional[int] = None
    PLEX_TV_SECTION_ID: Optional[int] = None
    PLEX_MOVIE_4K_SECTION_ID: Optional[int] = None
    PLEX_TV_4K_SECTION_ID: Optional[int] = None
    
    # Jellyfin
    JELLYFIN_URL: Optional[str] = None
    JELLYFIN_TOKEN: Optional[str] = None

    # Emby
    EMBY_URL: Optional[str] = None
    EMBY_TOKEN: Optional[str] = None
    EMBY_FORCE_LIBRARY_REFRESH_ON_ITEM_MISS: bool = os.getenv("EMBY_FORCE_LIBRARY_REFRESH_ON_ITEM_MISS", "true").split('#')[0].strip().lower() == "true"
    EMBY_TARGETED_REFRESH_WAIT_SECONDS: float = float(os.getenv("EMBY_TARGETED_REFRESH_WAIT_SECONDS", "5").split('#')[0].strip())

    JELLYFIN_FORCE_LIBRARY_REFRESH_ON_ITEM_MISS: bool = os.getenv("JELLYFIN_FORCE_LIBRARY_REFRESH_ON_ITEM_MISS", "true").split('#')[0].strip().lower() == "true"
    JELLYFIN_TARGETED_REFRESH_WAIT_SECONDS: float = float(os.getenv("JELLYFIN_TARGETED_REFRESH_WAIT_SECONDS", "5").split('#')[0].strip())

    # ARR instance configuration: fully dynamic from user-configured ARR server names in onboarding.
    ARR_INSTANCES_JSON: str = ""
    ARR_MAX_INSTANCES_PER_TYPE: int = int(os.getenv("ARR_MAX_INSTANCES_PER_TYPE", "2").split('#')[0].strip())
    # Playback webhook source instance keys (retain defaults for backward compat)
    TAUTULLI_INSTANCE_KEY: str = os.getenv("TAUTULLI_INSTANCE_KEY", "tautulli").split('#')[0].strip().lower()
    JELLYFIN_INSTANCE_KEY: str = os.getenv("JELLYFIN_INSTANCE_KEY", "jellyfin").split('#')[0].strip().lower()
    EMBY_INSTANCE_KEY: str = os.getenv("EMBY_INSTANCE_KEY", "emby").split('#')[0].strip().lower()
    # Optional override for the URL displayed in webhook setup instructions.
    # Set when Placeholdarr is reachable from ARR/Tautulli/etc. at a different
    # address than the dashboard origin — e.g. an internal Docker/Kubernetes
    # service name when the dashboard is reached via a public reverse proxy.
    WEBHOOK_BASE_URL: str = os.getenv("WEBHOOK_BASE_URL", "").split('#')[0].strip().rstrip("/")

    # Startup sync mode:
    # - auto: first successful ARR startup run is full, then lite on later startups
    # - full: always run full ARR startup sync
    # - lite: run ARR history delta catch-up only
    # - off: skip ARR startup sync
    STARTUP_SYNC_MODE: Literal["auto", "full", "lite", "off"] = "auto"
    # ARR startup check mode:
    # - off: skip ARR preflight checks
    # - config: verify configured URL/API-key pairs only
    # - live: make live ARR API calls to verify reachability/auth
    STARTUP_ARR_CHECK_MODE: Literal["off", "config", "live"] = "live"
    FULL_SYNC_INTERVAL_HOURS: int = 168
    # Lite sync: ARR catalog diff + calendar date refresh + calendar phase (replaces separate calendar cron when > 0).
    LITE_SYNC_INTERVAL_HOURS: int = 12

    # Collections (rule-based Plex collection builder)
    TMDB_API_KEY: Optional[str] = None
    # Trakt API Client ID (public list access only; no OAuth/account linking).
    TRAKT_CLIENT_ID: Optional[str] = None
    # Optional outbound Tautulli API for Collections "most played" sources.
    TAUTULLI_URL: Optional[str] = None
    TAUTULLI_API_KEY: Optional[str] = None
    # How often to run enabled collection recipes. <= 0 disables the scheduled job.
    COLLECTIONS_SYNC_INTERVAL_HOURS: int = 24

    # Dashboard authentication (see services/auth.py). Env AUTH_MODE overrides DB when set.
    AUTH_MODE: str = os.getenv("AUTH_MODE", "builtin").split("#")[0].strip().lower() or "builtin"
    AUTH_TRUSTED_PROXIES: str = os.getenv("AUTH_TRUSTED_PROXIES", "").split("#")[0].strip()
    AUTH_COOKIE_SECURE: bool = os.getenv("AUTH_COOKIE_SECURE", "false").split("#")[0].strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    # Library Paths
    LIBRARY_ROOT: str = ""
    MOVIE_LIBRARY_FOLDER: str = ""
    TV_LIBRARY_FOLDER: str = ""
    MOVIE_LIBRARY_4K_FOLDER: str = ""
    TV_LIBRARY_4K_FOLDER: str = ""

    # Queue monitor /queue poll cadence (seconds between Radarr/Sonarr queue API polls).
    #
    # Two settings exist for backward compatibility only:
    # - CHECK_INTERVAL: original single knob (still the default when the override is 0).
    # - QUEUE_MONITOR_POLL_INTERVAL_SECONDS: when >0, used *instead of* CHECK_INTERVAL for the queue monitor
    #   only, so installs could tune queue polling without repurposing CHECK_INTERVAL's name for other code.
    #
    # In the current codebase only `services/source_of_truth/queue_monitor_producer.py` reads these; there is
    # no separate "general app" consumer of CHECK_INTERVAL. Prefer setting QUEUE_MONITOR_POLL_INTERVAL_SECONDS
    # in the environment when you want an explicit queue poll interval; leave it 0 to use CHECK_INTERVAL.
    # Both are configured via environment / defaults (not the DB settings UI).
    CHECK_INTERVAL: int = int(os.getenv("CHECK_INTERVAL", "10").split("#")[0].strip() or "10")
    QUEUE_MONITOR_POLL_INTERVAL_SECONDS: int = int(
        os.getenv("QUEUE_MONITOR_POLL_INTERVAL_SECONDS", "0").split('#')[0].strip() or "0"
    )
    # When >0, POST RefreshMonitoredDownloads to Radarr/Sonarr on this interval while queue-like placeholders exist.
    # Decoupled from poll interval so you can stagger (e.g. poll every 5s, nudge ARR every 6s). 0 = disabled.
    QUEUE_MONITOR_REFRESH_MONITORED_DOWNLOADS_INTERVAL_SECONDS: int = int(
        os.getenv("QUEUE_MONITOR_REFRESH_MONITORED_DOWNLOADS_INTERVAL_SECONDS", "6").split("#")[0].strip()
    )
    # Delay before the first ARR refresh after producer start (seconds). Spreads work away from the first poll tick.
    QUEUE_MONITOR_REFRESH_STAGGER_SECONDS: int = int(
        os.getenv("QUEUE_MONITOR_REFRESH_STAGGER_SECONDS", "3").split('#')[0].strip()
    )

    # Dummy file management
    DUMMY_FILE_PATH: str = ""
    COMING_SOON_DUMMY_FILE_PATH: str = ""  # Optional
    PLACEHOLDER_STRATEGY: Literal["hardlink", "copy"] = "hardlink"
    PLACEHOLDER_CREATE_NFO: bool = True  # Always on; retained for env/back-compat only (see validator).
    PLACEHOLDER_STATUS_UPDATES: str = "ALL"
    PLACEHOLDER_STATUS_PROJECTION_MODE: Literal["summary", "title", "both"] = "both"
    PLACEHOLDER_FILE_MODE: str = os.getenv("PLACEHOLDER_FILE_MODE", "666").split('#')[0].strip()
    PLACEHOLDER_DIR_MODE: str = os.getenv("PLACEHOLDER_DIR_MODE", "777").split('#')[0].strip()
    ENABLE_PRIMER: bool = False

    # Play mode settings
    TV_PLAY_MODE: Literal["episode", "season", "series"] = "episode"
    EPISODES_LOOKAHEAD: int = 5
    
    # Playback-related settings (all default off: mark unmonitored + search full target set)
    PLAYBACK_MONITOR_ONLY_NO_SEARCH: bool = False
    PLAYBACK_SUPPRESS_SEARCH_WHEN_ALL_ELIGIBLE_MONITORED: bool = False
    PLAYBACK_SUPPRESS_SEARCH_FOR_FUTURE_EPISODES: bool = False
    ENABLE_PLAYBACK_FALLBACK_SEARCH: bool = True
    PLAYBACK_FALLBACK_TIMEOUT_MINUTES: int = 30
    # When multiple Radarr or Sonarr instances share on-disk paths for the same TMDB/TVDB title:
    # - protect_siblings: keep placeholder files until no sibling instance still needs them (default)
    # - any_instance_has_file: delete on obsolete cleanup when this instance has a real file
    RADARR_SHARED_PLACEHOLDER_CLEANUP: Literal["protect_siblings", "any_instance_has_file"] = (
        os.getenv("RADARR_SHARED_PLACEHOLDER_CLEANUP", "protect_siblings").split("#")[0].strip()
        or "protect_siblings"
    )
    SONARR_SHARED_PLACEHOLDER_CLEANUP: Literal["protect_siblings", "any_instance_has_file"] = (
        os.getenv("SONARR_SHARED_PLACEHOLDER_CLEANUP", "protect_siblings").split("#")[0].strip()
        or "protect_siblings"
    )
    # Composited local poster art for placeholders in Plex/Jellyfin/Emby (off = remote URLs in NFO only).
    PLACEHOLDER_POSTER_OVERLAY_MODE: Literal["off", "grayscale", "top_banner", "corner_logo"] = (
        os.getenv("PLACEHOLDER_POSTER_OVERLAY_MODE", "off").split("#")[0].strip().lower() or "off"
    )

    # Calendar-based status update settings
    # CALENDAR_LOOKAHEAD_DAYS: how many days into the future to create/show "Coming Soon" placeholders
    #   - Positive integer (e.g., 30): strict horizon for selected release type; items beyond are REQUEST
    #   - 0 (zero): disabled/off for future placeholder lookahead; future placeholders are reconciled out
    #   - -1 (negative): infinite lookahead; future items can remain Coming Soon
    # Default: 30 days
    CALENDAR_LOOKAHEAD_DAYS: int = 30
    # Calendar scheduler cadence (independent from full sync).
    # <= 0 disables independent calendar scheduler.
    CALENDAR_SYNC_INTERVAL_HOURS: int = 0
    PREFERRED_MOVIE_DATE_TYPE: str = "inCinemas"
    ENABLE_COMING_SOON_COUNTDOWN: bool = True
    # Deprecated: always treated as "coming_soon" (Coming Soon dummy when set, else standard dummy). Retained for env/back-compat only.
    CALENDAR_LOOKAHEAD_DUMMY_MODE: str = "coming_soon"

    # Include specials (season 0) when creating episode subflows
    INCLUDE_SPECIALS: bool = False
    # When True, skip creating placeholders for Radarr/Sonarr-monitored titles and remove
    # stale placeholders when monitoring is learned during sync (import flow unchanged).
    SKIP_PLACEHOLDERS_WHEN_MONITORED: bool = False
    # TV only: when skip-for-monitored is on, treat the whole show as skip if Sonarr series is monitored.
    SKIP_PLACEHOLDERS_WHEN_SERIES_MONITORED: bool = False

    # Postgres
    DB_HOST: str = os.getenv("DB_HOST", "localhost").split('#')[0].strip()
    DB_PORT: int = int(os.getenv("DB_PORT", "5432").split('#')[0].strip())
    DB_USER: str = os.getenv("DB_USER", "").split('#')[0].strip()
    DB_PASS: str = os.getenv("DB_PASS", "").split('#')[0].strip()
    DB_NAME: str = os.getenv("DB_NAME", "").split('#')[0].strip()

    PLACEHOLDARR_HOST: str = os.getenv("PLACEHOLDARR_HOST", "0.0.0.0")

    ENABLE_PLEX: bool = False
    ENABLE_JELLYFIN: bool = False
    ENABLE_EMBY: bool = False

    # Job queue / batching
    ENABLE_QUEUE_MONITOR: bool = os.getenv("ENABLE_QUEUE_MONITOR", "true").split('#')[0].strip().lower() == "true"
    QUEUE_MONITOR_RETRY_GRACE_SECONDS: int = int(os.getenv("QUEUE_MONITOR_RETRY_GRACE_SECONDS", "300").split('#')[0].strip())
    # Max seconds in SEARCHING with no Arr /queue row before NOT_FOUND (stops ARR refresh nudges).
    QUEUE_MONITOR_SEARCH_TIMEOUT_SECONDS: int = int(
        os.getenv("QUEUE_MONITOR_SEARCH_TIMEOUT_SECONDS", "120").split("#")[0].strip()
    )
    ENABLE_IMPORT_GRACE_ACCELERATED: bool = os.getenv("ENABLE_IMPORT_GRACE_ACCELERATED", "true").split('#')[0].strip().lower() == "true"
    IMPORT_GRACE_STEP_SECONDS: int = int(os.getenv("IMPORT_GRACE_STEP_SECONDS", "60").split('#')[0].strip())
    IMPORT_GRACE_ACCELERATED_STEP_SECONDS: int = int(os.getenv("IMPORT_GRACE_ACCELERATED_STEP_SECONDS", "5").split('#')[0].strip())
    STATUS_JOB_BATCH_SIZE: int = int(os.getenv("STATUS_JOB_BATCH_SIZE", "250").split('#')[0].strip())
    STATUS_JOB_DEBOUNCE_SECONDS: float = float(os.getenv("STATUS_JOB_DEBOUNCE_SECONDS", "0.5").split('#')[0].strip())
    MEDIA_REFRESH_SECTION_FALLBACK_ENABLED: bool = os.getenv("MEDIA_REFRESH_SECTION_FALLBACK_ENABLED", "true").split('#')[0].strip().lower() == "true"
    MATERIALIZATION_OVERLAP_ENABLED: bool = True
    MATERIALIZATION_OVERLAP_MOVIE_CHECKPOINT_COUNT: int = 200
    MATERIALIZATION_OVERLAP_EPISODE_CHECKPOINT_COUNT: int = 400
    MATERIALIZATION_OVERLAP_MAX_STALENESS_SECONDS: int = 120
    MATERIALIZATION_OVERLAP_MIN_CANDIDATES: int = 100
    MATERIALIZATION_OVERLAP_MAX_PENDING_SLICES_PER_SOURCE: int = 1
    MATERIALIZATION_OVERLAP_REFRESH_MIN_INTERVAL_SECONDS: int = 90
    MATERIALIZATION_OVERLAP_REFRESH_LEASE_SECONDS: int = 180
    ENABLE_STATUS_ORCHESTRATOR_CALENDAR: bool = os.getenv("ENABLE_STATUS_ORCHESTRATOR_CALENDAR", "true").split('#')[0].strip().lower() == "true"
    # Calendar phase batch size: how many placeholders to evaluate + commit per
    # chunk. The calendar phase iterates the entire on-disk placeholder set
    # (tens of thousands on large libraries). Without batching, a single
    # transaction stays open for the whole scan and can block unrelated worker
    # job-claim commits (e.g. webhook commits sitting behind a long-running
    # release-window scan). Smaller values keep transactions short and worker
    # claims responsive at the cost of more round-trips; larger values are
    # faster overall but hold locks longer per chunk.
    CALENDAR_PHASE_BATCH_SIZE: int = int(
        os.getenv("CALENDAR_PHASE_BATCH_SIZE", "500").split('#')[0].strip() or "500"
    )
    REFRESH_TRIGGER_SUPPRESSED: bool = False
    # Number of worker threads to start when the app starts (default 4)
    # Use WORKER_COUNT to tune parallelism; workers are always started by the app.

    # Job queue uses Postgres LISTEN/NOTIFY with periodic safety polling (see WORKER_SAFETY_POLL_SECONDS).
    USE_JOB_DRIVEN_REFRESH: bool = os.getenv("USE_JOB_DRIVEN_REFRESH", "true").split('#')[0].strip().lower() == "true"
    USE_JOB_DRIVEN_STARTUP_SYNC: bool = os.getenv("USE_JOB_DRIVEN_STARTUP_SYNC", "true").split('#')[0].strip().lower() == "true"
    # Safety wake when using NOTIFY-driven queue monitor (missed NOTIFY recovery).
    QUEUE_MONITOR_NOTIFY_SAFETY_POLL_SECONDS: int = int(
        os.getenv("QUEUE_MONITOR_NOTIFY_SAFETY_POLL_SECONDS", "300").split('#')[0].strip() or "300"
    )

    # Worker NOTIFY-driven loop tuning. Safety poll is the maximum interval an
    # executor sleeps between drain attempts when no NOTIFY arrives; it acts
    # as a backstop against missed wakes. Phase 3 of the holistic NOTIFY
    # audit tightens these defaults: with Phase 1 short handlers, a stuck
    # claim now self-heals in minutes rather than the previous half-hour.
    # WORKER_SAFETY_POLL_SECONDS: was 60s; tightened to 15s so a missed
    # NOTIFY costs at most ~15s of latency.
    WORKER_SAFETY_POLL_SECONDS: int = int(
        os.getenv("WORKER_SAFETY_POLL_SECONDS", "15").split('#')[0].strip() or "15"
    )
    # WORKER_STALE_CLAIMED_RESET_SECONDS: was 1800s; tightened to 900s
    # because Phase 1 handlers are bounded by JOB_HANDLER_TIMEOUT_SECONDS
    # (default 600s) plus a small headroom. Operators with intentionally
    # long-running batch jobs should override via env.
    WORKER_STALE_CLAIMED_RESET_SECONDS: int = int(
        os.getenv("WORKER_STALE_CLAIMED_RESET_SECONDS", "900").split('#')[0].strip() or "900"
    )
    WORKER_STALE_CLAIMED_REAP_INTERVAL_SECONDS: int = int(
        os.getenv("WORKER_STALE_CLAIMED_REAP_INTERVAL_SECONDS", "300").split('#')[0].strip() or "300"
    )
    # Per-job watchdog (FM-6): logs an ERROR if a handler runs longer than this.
    # The thread is not killed; this is an observability hook so operators see
    # hung handlers before the reaper requeues them.
    JOB_HANDLER_TIMEOUT_SECONDS: int = int(
        os.getenv("JOB_HANDLER_TIMEOUT_SECONDS", "600").split('#')[0].strip() or "600"
    )
    # Postgres ``lock_timeout`` applied to the worker's per-transaction job
    # claim (SELECT FOR UPDATE + UPDATE + COMMIT). If the claim cannot acquire
    # the locks it needs within this many seconds, the transaction errors with
    # SQLSTATE 55P03 and the worker rolls back and retries on the next wake
    # instead of stalling for hours behind a long-running transaction (e.g.
    # calendar phase or sync). Set to 0 to disable the safety net (not
    # recommended). Default: 30s — generous enough to absorb normal contention
    # bursts but short enough that workers self-heal quickly.
    WORKER_CLAIM_LOCK_TIMEOUT_SECONDS: int = int(
        os.getenv("WORKER_CLAIM_LOCK_TIMEOUT_SECONDS", "30").split('#')[0].strip() or "30"
    )
    # Maximum number of jobs a single executor's drain pass processes
    # before yielding back to the wait/clear loop. Caps how long one
    # worker monopolizes the pool when a large burst arrives; other
    # workers and a re-set ``_drain_event`` pick up the rest.
    WORKER_MAX_JOBS_PER_DRAIN: int = int(
        os.getenv("WORKER_MAX_JOBS_PER_DRAIN", "50").split('#')[0].strip() or "50"
    )
    # Master switch for NOTIFY-driven worker wakes. When false, the
    # executor falls back to a tight polling loop using
    # ``WORKER_FALLBACK_POLL_SECONDS`` and skips the LISTEN bridge. Useful
    # as a one-line rollback when NOTIFY misbehaves in production.
    WORKER_NOTIFY_ENABLED: bool = (
        os.getenv("WORKER_NOTIFY_ENABLED", "true").split('#')[0].strip().lower() == "true"
    )
    # Polling interval used when ``WORKER_NOTIFY_ENABLED=false``. Tighter
    # than ``WORKER_SAFETY_POLL_SECONDS`` because polling is the only
    # wake mechanism in fallback mode.
    WORKER_FALLBACK_POLL_SECONDS: int = int(
        os.getenv("WORKER_FALLBACK_POLL_SECONDS", "5").split('#')[0].strip() or "5"
    )
    # Persist ARR/Tautulli webhooks serially (or with a small bound) so a
    # MovieAdded flood from import lists cannot open one DB session per POST.
    WEBHOOK_INGEST_CONCURRENCY: int = int(
        os.getenv("WEBHOOK_INGEST_CONCURRENCY", "1").split('#')[0].strip() or "1"
    )

    # ----------------------------------------------------------------
    # Database connection pool tuning. Phase 2 of the holistic NOTIFY
    # audit makes pool sizing externally configurable rather than the
    # legacy hardcoded 10/20. Operators sizing for many workers should
    # confirm pool_size + max_overflow + (notifier LISTEN) + (other
    # processes) stays under Postgres ``max_connections`` (default 100).
    # ----------------------------------------------------------------
    DB_POOL_SIZE: int = int(
        os.getenv("DB_POOL_SIZE", "20").split('#')[0].strip() or "20"
    )
    DB_POOL_MAX_OVERFLOW: int = int(
        os.getenv("DB_POOL_MAX_OVERFLOW", "20").split('#')[0].strip() or "20"
    )
    DB_POOL_TIMEOUT_SECONDS: int = int(
        os.getenv("DB_POOL_TIMEOUT_SECONDS", "10").split('#')[0].strip() or "10"
    )
    DB_POOL_RECYCLE_SECONDS: int = int(
        os.getenv("DB_POOL_RECYCLE_SECONDS", "1800").split('#')[0].strip() or "1800"
    )
    # Checkout/checkin telemetry: logs long-held pool connections and near-exhaustion snapshots.
    DB_POOL_TELEMETRY_ENABLED: bool = (
        os.getenv("DB_POOL_TELEMETRY_ENABLED", "true").split('#')[0].strip().lower() in ("1", "true", "yes")
    )
    DB_POOL_SLOW_CHECKIN_LOG_SECONDS: float = float(
        os.getenv("DB_POOL_SLOW_CHECKIN_LOG_SECONDS", "15").split('#')[0].strip() or "15"
    )
    DB_POOL_NEAR_FULL_LOG_COOLDOWN_SECONDS: float = float(
        os.getenv("DB_POOL_NEAR_FULL_LOG_COOLDOWN_SECONDS", "30").split('#')[0].strip() or "30"
    )
    DB_POOL_NEAR_FULL_FREE_SLOTS: int = int(
        os.getenv("DB_POOL_NEAR_FULL_FREE_SLOTS", "3").split('#')[0].strip() or "3"
    )
    # Max seconds to wait for init_db's Postgres advisory lock when another process
    # is running migrations (blocking pg_advisory_lock + lock_timeout).
    RUNTIME_SCHEMA_LOCK_WAIT_SECONDS: int = int(
        os.getenv("RUNTIME_SCHEMA_LOCK_WAIT_SECONDS", "120").split('#')[0].strip() or "120"
    )

    # Add a method to clean string values
    @validator('*', pre=True)
    def clean_string_values(cls, v):
        """Clean string values by removing comments and extra whitespace"""
        if isinstance(v, str):
            # Split on # but only if it's not part of a URL
            if '#' in v and not ('http://' in v or 'https://' in v):
                v = v.split('#')[0].strip()
            else:
                v = v.strip()
        return v
    
    @validator('DUMMY_FILE_PATH')
    def validate_dummy_file_path(cls, v):
        if not v:
            return v
        path = Path(v)
        if not path.exists():
            raise ValueError(f"Path does not exist: {v}")
        if not path.is_file():
            raise ValueError(f"DUMMY_FILE_PATH must be a file: {v}")
        if path.stat().st_size == 0:
            raise ValueError(f"Dummy file exists but is empty: {v}")
        return str(path.absolute())

    @validator("CALENDAR_LOOKAHEAD_DUMMY_MODE")
    def validate_calendar_lookahead_dummy_mode(cls, v):
        # UI no longer exposes this; Coming Soon placeholders always prefer the Coming Soon dummy path when configured.
        return "coming_soon"

    @validator("PLACEHOLDER_CREATE_NFO")
    def placeholder_create_nfo_always_on(cls, v):
        # Required for status projection and library building; not user-configurable.
        return True

    @validator("PLACEHOLDER_STATUS_PROJECTION_MODE", pre=True)
    def normalize_placeholder_status_projection_mode(cls, v):
        # "off" was removed from the UI; use Placeholder status updates = Off instead. Legacy values map to both.
        raw = str(v or "both").strip().lower()
        if raw == "off" or raw not in {"summary", "title", "both"}:
            return "both"
        return raw

    @validator('COMING_SOON_DUMMY_FILE_PATH')
    def validate_coming_soon_dummy_file_path(cls, v):
        if not v:
            return v
        path = Path(v)
        if not path.exists():
            raise ValueError(f"Path does not exist: {v}")
        if not path.is_file():
            raise ValueError(f"COMING_SOON_DUMMY_FILE_PATH must be a file: {v}")
        return str(path.absolute())
    
    @validator('PLEX_URL', 'JELLYFIN_URL', 'EMBY_URL', pre=True)
    def validate_url(cls, v):
        if v is None or v == "":
            return v  # Allow missing/blank for optional URLs
        if not v.startswith(('http://', 'https://')):
            raise ValueError(f"Invalid URL: {v}")
        return v.rstrip('/')

    @root_validator(skip_on_failure=True)
    def check_media_providers(cls, values):
        enable_plex = values.get('ENABLE_PLEX', True)
        enable_jellyfin = values.get('ENABLE_JELLYFIN', True)
        enable_emby = values.get('ENABLE_EMBY', False)
        plex_keys = [values.get('PLEX_URL'), values.get('PLEX_TOKEN')]
        jellyfin_keys = [values.get('JELLYFIN_URL'), values.get('JELLYFIN_TOKEN')]
        emby_keys = [values.get('EMBY_URL'), values.get('EMBY_TOKEN')]
        plex_configured = all(plex_keys)
        jellyfin_configured = all(jellyfin_keys)
        emby_configured = all(emby_keys)
        if enable_plex and not plex_configured:
            values['ENABLE_PLEX'] = False
        if enable_jellyfin and not jellyfin_configured:
            values['ENABLE_JELLYFIN'] = False
        if enable_emby and not emby_configured:
            values['ENABLE_EMBY'] = False
        return values

    @root_validator(skip_on_failure=True)
    def configure_library_paths(cls, values):
        library_root = str(values.get('LIBRARY_ROOT') or '').strip()
        # Simplified path model:
        # - one base library root
        # - internal folders derive to `<root>/movies` and `<root>/tv`
        movie_folder = str(values.get('MOVIE_LIBRARY_FOLDER') or '').strip()
        tv_folder = str(values.get('TV_LIBRARY_FOLDER') or '').strip()
        if library_root:
            if not movie_folder:
                movie_folder = os.path.join(library_root, 'movies')
            if not tv_folder:
                tv_folder = os.path.join(library_root, 'tv')

        # Keep legacy 4K/anime attributes aligned for compatibility, but do not
        # create separate path branches in the simplified model.
        movie_4k_folder = movie_folder
        tv_4k_folder = tv_folder
        dir_mode = _parse_octal_mode(values.get('PLACEHOLDER_DIR_MODE', '777'), 0o777)
        folder_values = {
            'MOVIE_LIBRARY_FOLDER': movie_folder,
            'TV_LIBRARY_FOLDER': tv_folder,
            'MOVIE_LIBRARY_4K_FOLDER': movie_4k_folder,
            'TV_LIBRARY_4K_FOLDER': tv_4k_folder,
        }

        for key, raw in folder_values.items():
            raw = str(raw or '').strip()
            if not raw:
                values[key] = ''
                continue

            path = Path(raw)
            if path.exists() and not path.is_dir():
                raise ValueError(f'{key} must be a directory path: {raw}')

            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)
                logger.info(f'Created missing library folder for {key}: {path}')

            # Keep directory permissions aligned with placeholder folder policy.
            try:
                os.chmod(path, dir_mode)
            except Exception:
                pass

            values[key] = str(path.absolute())

        return values

    @property
    def plex_enabled(self) -> bool:
        return self.ENABLE_PLEX and bool(self.PLEX_URL and self.PLEX_TOKEN)

    @property
    def jellyfin_enabled(self) -> bool:
        return self.ENABLE_JELLYFIN and bool(self.JELLYFIN_URL and self.JELLYFIN_TOKEN)

    @property
    def emby_enabled(self) -> bool:
        return self.ENABLE_EMBY and bool(self.EMBY_URL and self.EMBY_TOKEN)

    @property
    def has_4k_support(self) -> bool:
        has_4k_radarr = any(item.get('is_4k') and item.get('arr_type') == 'radarr' for item in self.configured_arr_instances)
        has_4k_sonarr = any(item.get('is_4k') and item.get('arr_type') == 'sonarr' for item in self.configured_arr_instances)
        return (has_4k_radarr and bool(self.MOVIE_LIBRARY_4K_FOLDER)) or (has_4k_sonarr and bool(self.TV_LIBRARY_4K_FOLDER))

    @property
    def plex_4k_movie_section_id(self) -> int:
        return self.PLEX_MOVIE_4K_SECTION_ID if hasattr(self, 'PLEX_MOVIE_4K_SECTION_ID') else self.PLEX_MOVIE_SECTION_ID

    @property
    def plex_4k_tv_section_id(self) -> int:
        return self.PLEX_TV_4K_SECTION_ID if hasattr(self, 'PLEX_TV_4K_SECTION_ID') else self.PLEX_TV_SECTION_ID

    @property
    def host(self) -> str:
        return self.PLACEHOLDARR_HOST

    @property
    def coming_soon_placeholders_enabled(self) -> bool:
        """True when the calendar lookahead window is active (Coming Soon placeholders). Set CALENDAR_LOOKAHEAD_DAYS to 0 to disable."""
        try:
            return int(self.CALENDAR_LOOKAHEAD_DAYS) != 0
        except (TypeError, ValueError):
            return False

    @property
    def configured_arr_instances(self) -> list[dict[str, Any]]:
        return parse_configured_arr_instances_json(str(getattr(self, "ARR_INSTANCES_JSON", "") or "").strip())

    @property
    def radarr_instance_keys(self) -> tuple[str, ...]:
        """Extract all configured Radarr instance keys from ARR_INSTANCES_JSON."""
        return tuple(str(item.get('instance_key', '')).lower() for item in self.configured_arr_instances if str(item.get('arr_type', '')).lower() == 'radarr')

    @property
    def sonarr_instance_keys(self) -> tuple[str, ...]:
        """Extract all configured Sonarr instance keys from ARR_INSTANCES_JSON."""
        return tuple(str(item.get('instance_key', '')).lower() for item in self.configured_arr_instances if str(item.get('arr_type', '')).lower() == 'sonarr')

    @property
    def playback_source_instance_keys(self) -> tuple[str, str, str]:
        return (self.TAUTULLI_INSTANCE_KEY, self.JELLYFIN_INSTANCE_KEY, self.EMBY_INSTANCE_KEY)

    @property
    def allowed_webhook_instance_keys(self) -> tuple[str, ...]:
        ordered: list[str] = []
        for item in self.configured_arr_instances:
            key = str(item.get("instance_key") or "").strip().lower()
            if key:
                ordered.append(key)
            for a in item.get("instance_key_aliases") or []:
                av = str(a or "").strip().lower()
                if av:
                    ordered.append(av)
        ordered.extend(
            [
                self.TAUTULLI_INSTANCE_KEY,
                self.JELLYFIN_INSTANCE_KEY,
                self.EMBY_INSTANCE_KEY,
            ]
        )
        deduped: list[str] = []
        for value in ordered:
            key = str(value or '').strip().lower()
            if key and key not in deduped:
                deduped.append(key)
        return tuple(deduped)

    @property
    def instance_is_4k(self) -> dict[str, bool]:
        mapping: dict[str, bool] = {}
        for item in self.configured_arr_instances:
            key = str(item.get("instance_key") or "").strip().lower()
            if not key:
                continue
            mapping[key] = bool(item.get("is_4k", False))
        return mapping

    @property
    def instance_roles(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for item in self.configured_arr_instances:
            key = str(item.get("instance_key") or "").strip().lower()
            if not key:
                continue
            role = str(item.get("role") or "").strip().lower()
            mapping[key] = role if role in {"primary", "secondary", "additional"} else "primary"
        return mapping

    @property
    def movie_instance_ranking(self) -> list[str]:
        """Get movie instance keys derived from configured ARR instances.

        This no longer reads legacy MOVIE_INSTANCE_RANKING environment values.
        """
        return [str(item.get("instance_key", "")).lower() for item in self.configured_arr_instances if str(item.get("arr_type", "")).lower() == "radarr"]

    @property
    def tv_instance_ranking(self) -> list[str]:
        """Get TV instance keys derived from configured ARR instances.

        This no longer reads legacy TV_INSTANCE_RANKING environment values.
        """
        return [str(item.get("instance_key", "")).lower() for item in self.configured_arr_instances if str(item.get("arr_type", "")).lower() == "sonarr"]

    def arr_instances_for_type(self, arr_type: str) -> list[dict[str, Any]]:
        normalized = str(arr_type or "").strip().lower()
        if normalized not in {"radarr", "sonarr"}:
            return []
        return [
            item
            for item in self.configured_arr_instances
            if str(item.get("arr_type", "")).strip().lower() == normalized
        ]

    def resolve_arr_instance(
        self,
        arr_type: str,
        *,
        instance_id: str | None = None,
        instance_key: str | None = None,
        role: str | None = None,
        is_4k: bool | None = None,
    ) -> dict[str, Any] | None:
        instances = self.arr_instances_for_type(arr_type)
        if not instances:
            return None

        if instance_id:
            target_id = str(instance_id).strip().lower()
            for item in instances:
                if str(item.get("instance_id") or "").strip().lower() == target_id:
                    return item

        if instance_key:
            target = str(instance_key).strip().lower()
            for item in instances:
                if str(item.get("instance_key") or "").strip().lower() == target:
                    return item
                aliases = item.get("instance_key_aliases") if isinstance(item.get("instance_key_aliases"), list) else []
                for a in aliases:
                    if str(a or "").strip().lower() == target:
                        return item

        if role:
            target_role = str(role).strip().lower()
            if target_role in {"primary", "secondary", "additional"}:
                for item in instances:
                    if str(item.get("role") or "").strip().lower() == target_role:
                        return item

        if is_4k is not None:
            for item in instances:
                if bool(item.get("is_4k", False)) == bool(is_4k):
                    return item

        return instances[0]

    def resolve_arr_endpoint(
        self,
        arr_type: str,
        *,
        instance_id: str | None = None,
        instance_key: str | None = None,
        role: str | None = None,
        is_4k: bool | None = None,
    ) -> tuple[str, str]:
        item = self.resolve_arr_instance(
            arr_type,
            instance_id=instance_id,
            instance_key=instance_key,
            role=role,
            is_4k=is_4k,
        )
        if not item:
            return "", ""
        return str(item.get("url") or "").strip(), str(item.get("api_key") or "").strip()

    class Config:
        env_file = str(dotenv_path)
        env_file_encoding = 'utf-8'
        extra = "ignore"  # Ignore extra values not defined in the model
        case_sensitive = True

settings = Settings()
