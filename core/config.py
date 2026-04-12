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
    LOG_LEVEL: str = os.getenv("PLACEHOLDARR_LOG_LEVEL", "INFO")
    APPDATA_PATH: str = os.getenv("APPDATA_PATH", "/config").split('#')[0].strip()
    LOG_DIR: str = os.getenv("LOG_DIR", "").split('#')[0].strip()
    LOG_FILE: str = os.getenv("LOG_FILE", "").split('#')[0].strip()
    LOG_MAX_RUN_FILES: int = int(os.getenv("LOG_MAX_RUN_FILES", "10").split('#')[0].strip())
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
    FULL_SYNC_INTERVAL_HOURS: int = 0

    # Library Paths
    LIBRARY_ROOT: str = ""
    MOVIE_LIBRARY_FOLDER: str = ""
    TV_LIBRARY_FOLDER: str = ""
    MOVIE_LIBRARY_4K_FOLDER: str = ""
    TV_LIBRARY_4K_FOLDER: str = ""

    # Application
    CHECK_INTERVAL: int = 10

    # Dummy file management
    DUMMY_FILE_PATH: str = ""
    COMING_SOON_DUMMY_FILE_PATH: str = ""  # Optional
    PLACEHOLDER_STRATEGY: Literal["hardlink", "copy"] = "hardlink"
    PLACEHOLDER_CREATE_NFO: bool = True
    PLACEHOLDER_STATUS_UPDATES: str = "ALL"
    PLACEHOLDER_STATUS_PROJECTION_MODE: Literal["summary", "title", "both", "off"] = "summary"
    PLACEHOLDER_FILE_MODE: str = os.getenv("PLACEHOLDER_FILE_MODE", "666").split('#')[0].strip()
    PLACEHOLDER_DIR_MODE: str = os.getenv("PLACEHOLDER_DIR_MODE", "777").split('#')[0].strip()
    FORCE_PRIME_ON_STARTUP: bool = os.getenv("FORCE_PRIME_ON_STARTUP", "false").split('#')[0].strip().lower() == "true"
    PRIMER_SERIES_COUNT: int = int(os.getenv("PRIMER_SERIES_COUNT", "3").split('#')[0].strip())
    PRIMER_EPISODES_PER_SERIES: int = int(os.getenv("PRIMER_EPISODES_PER_SERIES", "3").split('#')[0].strip())
    PRIMER_REFRESH_WAIT_SECONDS: int = int(os.getenv("PRIMER_REFRESH_WAIT_SECONDS", "60").split('#')[0].strip())

    # Play mode settings
    TV_PLAY_MODE: Literal["episode", "season", "series"] = "episode"
    EPISODES_LOOKAHEAD: int = 5
    
    # Playback-related settings
    
    ENABLE_PLAYBACK_FALLBACK_SEARCH: bool = True
    PLAYBACK_FALLBACK_TIMEOUT_MINUTES: int = 30

    # Calendar-based status update settings
    # CALENDAR_LOOKAHEAD_DAYS: how many days into the future to create/show "Coming Soon" placeholders
    #   - Positive integer (e.g., 30): strict horizon for selected release type; items beyond are REQUEST
    #   - 0 (zero): disabled/off for future placeholder lookahead; future placeholders are reconciled out
    #   - -1 (negative): infinite lookahead; future items can remain Coming Soon
    # Default: 30 days
    CALENDAR_LOOKAHEAD_DAYS: int = 30
    # Calendar scheduler cadence (independent from full sync).
    # <= 0 disables independent calendar scheduler.
    CALENDAR_SYNC_INTERVAL_HOURS: int = 12
    PREFERRED_MOVIE_DATE_TYPE: str = "inCinemas"
    ENABLE_COMING_SOON_COUNTDOWN: bool = True
    # For calendar "coming soon" placeholders: use primary dummy only, or prefer coming-soon dummy when configured.
    CALENDAR_LOOKAHEAD_DUMMY_MODE: str = "coming_soon"

    # Include specials (season 0) when creating episode subflows
    INCLUDE_SPECIALS: bool = False

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
    ENABLE_IMPORT_GRACE_ACCELERATED: bool = os.getenv("ENABLE_IMPORT_GRACE_ACCELERATED", "true").split('#')[0].strip().lower() == "true"
    IMPORT_GRACE_STEP_SECONDS: int = int(os.getenv("IMPORT_GRACE_STEP_SECONDS", "60").split('#')[0].strip())
    IMPORT_GRACE_ACCELERATED_STEP_SECONDS: int = int(os.getenv("IMPORT_GRACE_ACCELERATED_STEP_SECONDS", "5").split('#')[0].strip())
    STATUS_JOB_BATCH_SIZE: int = int(os.getenv("STATUS_JOB_BATCH_SIZE", "250").split('#')[0].strip())
    STATUS_JOB_DEBOUNCE_SECONDS: float = float(os.getenv("STATUS_JOB_DEBOUNCE_SECONDS", "0.5").split('#')[0].strip())
    PLEX_METADATA_READY_CONFIRM_POLLS: int = int(os.getenv("PLEX_METADATA_READY_CONFIRM_POLLS", "2").split('#')[0].strip())
    OBSERVATION_PASS_CHUNK_SIZE: int = int(os.getenv("OBSERVATION_PASS_CHUNK_SIZE", "150").split('#')[0].strip())
    OBSERVATION_MAX_PASS_SECONDS: int = int(os.getenv("OBSERVATION_MAX_PASS_SECONDS", "45").split('#')[0].strip())
    OBSERVATION_MIN_CHUNKS_PER_PASS: int = int(os.getenv("OBSERVATION_MIN_CHUNKS_PER_PASS", "1").split('#')[0].strip())
    OBSERVATION_SNAPSHOT_CACHE_PASSES: int = int(os.getenv("OBSERVATION_SNAPSHOT_CACHE_PASSES", "2").split('#')[0].strip())
    MEDIA_REFRESH_SECTION_FALLBACK_ENABLED: bool = os.getenv("MEDIA_REFRESH_SECTION_FALLBACK_ENABLED", "true").split('#')[0].strip().lower() == "true"
    OBSERVATION_BULK_STRICT_KEYS_ONLY: bool = os.getenv("OBSERVATION_BULK_STRICT_KEYS_ONLY", "true").split('#')[0].strip().lower() == "true"
    OBSERVATION_STRICT_KEYS_MIN_PLACEHOLDERS: int = int(os.getenv("OBSERVATION_STRICT_KEYS_MIN_PLACEHOLDERS", "100").split('#')[0].strip())
    OBSERVATION_SINGLE_FLIGHT_ENABLED: bool = os.getenv("OBSERVATION_SINGLE_FLIGHT_ENABLED", "true").split('#')[0].strip().lower() == "true"
    OBSERVATION_SINGLE_FLIGHT_WAIT_SECONDS: float = float(os.getenv("OBSERVATION_SINGLE_FLIGHT_WAIT_SECONDS", "15").split('#')[0].strip())
    OBSERVATION_SINGLE_FLIGHT_RETRY_SECONDS: float = float(os.getenv("OBSERVATION_SINGLE_FLIGHT_RETRY_SECONDS", "0.25").split('#')[0].strip())
    OBSERVATION_FLIGHT_STALE_SECONDS: int = int(os.getenv("OBSERVATION_FLIGHT_STALE_SECONDS", "300").split('#')[0].strip())
    HYBRID_OBSERVATION_SLICES_ENABLED: bool = True
    HYBRID_OBSERVATION_INITIAL_DELAY_SECONDS: int = 15
    HYBRID_OBSERVATION_CADENCE_SECONDS: int = 120
    HYBRID_OBSERVATION_MAX_ATTEMPTS: int = 4
    HYBRID_OBSERVATION_MIN_UNRESOLVED: int = 1
    HYBRID_OBSERVATION_TARGET_SLICE_SIZE: int = int(os.getenv("HYBRID_OBSERVATION_TARGET_SLICE_SIZE", "400").split('#')[0].strip())
    HYBRID_OBSERVATION_LOW_WATERMARK: int = int(os.getenv("HYBRID_OBSERVATION_LOW_WATERMARK", "120").split('#')[0].strip())
    HYBRID_OBSERVATION_MID_PASS_REFILL_ENABLED: bool = os.getenv("HYBRID_OBSERVATION_MID_PASS_REFILL_ENABLED", "true").split('#')[0].strip().lower() == "true"
    HYBRID_OBSERVATION_REFILL_NEWEST_RATIO_SCANNING: float = float(os.getenv("HYBRID_OBSERVATION_REFILL_NEWEST_RATIO_SCANNING", "0.7").split('#')[0].strip())
    HYBRID_OBSERVATION_REFILL_NEWEST_RATIO_IDLE: float = float(os.getenv("HYBRID_OBSERVATION_REFILL_NEWEST_RATIO_IDLE", "0.25").split('#')[0].strip())
    HYBRID_OBSERVATION_SINGLE_FLIGHT_RETRY_BASE_SECONDS: int = int(os.getenv("HYBRID_OBSERVATION_SINGLE_FLIGHT_RETRY_BASE_SECONDS", "30").split('#')[0].strip())
    HYBRID_OBSERVATION_SINGLE_FLIGHT_RETRY_MAX_SECONDS: int = int(os.getenv("HYBRID_OBSERVATION_SINGLE_FLIGHT_RETRY_MAX_SECONDS", "180").split('#')[0].strip())
    OBSERVATION_CONTINUATION_TRAIL_CONDITIONAL_ENABLED: bool = os.getenv("OBSERVATION_CONTINUATION_TRAIL_CONDITIONAL_ENABLED", "true").split('#')[0].strip().lower() == "true"
    OBSERVATION_CONTINUATION_TRAIL_MAX_CANDIDATES: int = int(os.getenv("OBSERVATION_CONTINUATION_TRAIL_MAX_CANDIDATES", "150").split('#')[0].strip())
    OBSERVATION_TRAIL_STUBBORN_DEMOTION_AFTER_ATTEMPTS: int = int(os.getenv("OBSERVATION_TRAIL_STUBBORN_DEMOTION_AFTER_ATTEMPTS", "4").split('#')[0].strip())
    OBSERVATION_TRAIL_STUBBORN_DELAY_SECONDS: int = int(os.getenv("OBSERVATION_TRAIL_STUBBORN_DELAY_SECONDS", "900").split('#')[0].strip())
    MATERIALIZATION_OVERLAP_ENABLED: bool = True
    MATERIALIZATION_OVERLAP_MOVIE_CHECKPOINT_COUNT: int = 200
    MATERIALIZATION_OVERLAP_EPISODE_CHECKPOINT_COUNT: int = 400
    MATERIALIZATION_OVERLAP_MAX_STALENESS_SECONDS: int = 120
    MATERIALIZATION_OVERLAP_MIN_CANDIDATES: int = 100
    MATERIALIZATION_OVERLAP_MAX_PENDING_SLICES_PER_SOURCE: int = 1
    MATERIALIZATION_OVERLAP_REFRESH_MIN_INTERVAL_SECONDS: int = 90
    MATERIALIZATION_OVERLAP_REFRESH_LEASE_SECONDS: int = 180
    ENABLE_STATUS_ORCHESTRATOR_CALENDAR: bool = os.getenv("ENABLE_STATUS_ORCHESTRATOR_CALENDAR", "true").split('#')[0].strip().lower() == "true"
    # Number of worker threads to start when the app starts (default 4)
    # Use WORKER_COUNT to tune parallelism; workers are always started by the app.

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

    @validator('CALENDAR_LOOKAHEAD_DUMMY_MODE')
    def validate_calendar_lookahead_dummy_mode(cls, v):
        text = str(v or "coming_soon").strip().lower()
        return "primary" if text == "primary" else "coming_soon"

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
        parsed_instances: list[dict[str, Any]] = []
        raw = str(getattr(self, "ARR_INSTANCES_JSON", "") or "").strip()

        if raw:
            try:
                payload = json.loads(raw)
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
                                "arr_type": arr_type,
                                "url": url,
                                "api_key": api_key,
                                "label": str(item.get("label") or instance_key).strip() or instance_key,
                                "role": role,
                                "priority": priority,
                            }
                        )
            except Exception as exc:
                logger.warning(f"Failed to parse ARR_INSTANCES_JSON; falling back to legacy settings: {exc}")

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
                # Forward model:
                # - instance_id: stable instance identity
                # - role: primary/secondary/additional
                # - priority: explicit per-type fallback order
                # Compatibility output:
                # - is_4k remains derived (primary=False, secondary/additional=True)
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
        ordered = [
            *(str(item.get("instance_key") or "").strip().lower() for item in self.configured_arr_instances),
            self.TAUTULLI_INSTANCE_KEY,
            self.JELLYFIN_INSTANCE_KEY,
            self.EMBY_INSTANCE_KEY,
        ]
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
