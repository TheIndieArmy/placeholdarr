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
    SCHEDULED_TIME_FAILED: Optional[str] = None  # Add this line to avoid AttributeError

    # Plex
    PLEX_URL: Optional[str] = None
    PLEX_TOKEN: Optional[str] = None
    PLEX_MOVIE_SECTION_ID: Optional[int] = None
    PLEX_TV_SECTION_ID: Optional[int] = None
    
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

    # Services
    RADARR_URL: str = ""
    RADARR_API_KEY: str = ""
    SONARR_URL: str = ""
    SONARR_API_KEY: str = ""

    # 4K Services (optional)
    RADARR_4K_URL: str = ""
    RADARR_4K_API_KEY: str = ""
    SONARR_4K_URL: str = ""
    SONARR_4K_API_KEY: str = ""

    # ARR instance configuration: now fully dynamic from user-configured ARR server names in onboarding.
    # (Removed static RADARR_STD_INSTANCE_KEY, RADARR_4K_INSTANCE_KEY, SONARR_STD_INSTANCE_KEY, SONARR_4K_INSTANCE_KEY)
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
    LIBRARY_ORGANIZATION_MODE: Literal["single", "separate"] = "separate"
    ENABLE_STANDARD_PROFILE: bool = True
    ENABLE_4K_PROFILE: bool = True
    ENABLE_ANIME_PROFILE: bool = False
    MOVIE_LIBRARY_FOLDER: str = ""
    TV_LIBRARY_FOLDER: str = ""
    MOVIE_LIBRARY_4K_FOLDER: str = ""
    TV_LIBRARY_4K_FOLDER: str = ""
    ANIME_LIBRARY_FOLDER: str = ""

    # Application
    PLAYBACK_COOLDOWN: int = 30
    MAX_MONITOR_TIME: int = 60
    CHECK_INTERVAL: int = 10
    AVAILABLE_CLEANUP_DELAY: int = 10

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
    AVAILABLE_CLEANUP_DELAY: int = 10

    # Migration settings
    MIGRATION: bool = False
      
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
    CALENDAR_PLACEHOLDER_MODE: str = "episode"
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
    BATCH_SERIES_SUBFLOWS: bool = os.getenv("BATCH_SERIES_SUBFLOWS", "true").strip().lower() == "true"
    JOB_DEBOUNCE_SECONDS: int = int(os.getenv("JOB_DEBOUNCE_SECONDS", "3"))
    ENABLE_IMPORT_EVENT_HANDLERS: bool = os.getenv("ENABLE_IMPORT_EVENT_HANDLERS", "true").split('#')[0].strip().lower() == "true"
    ENABLE_DELETE_EVENT_HANDLERS: bool = os.getenv("ENABLE_DELETE_EVENT_HANDLERS", "true").split('#')[0].strip().lower() == "true"
    ENABLE_PLAYBACK_EVENT_HANDLERS: bool = True
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
    
    @validator('PLEX_URL', 'RADARR_URL', 'SONARR_URL', 'JELLYFIN_URL', 'EMBY_URL', pre=True)
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
        use_standard = bool(values.get('ENABLE_STANDARD_PROFILE', True))
        use_4k = bool(values.get('ENABLE_4K_PROFILE', True))
        use_anime = bool(values.get('ENABLE_ANIME_PROFILE', False))

        movie_folder = str(values.get('MOVIE_LIBRARY_FOLDER') or '').strip()
        tv_folder = str(values.get('TV_LIBRARY_FOLDER') or '').strip()
        movie_4k_folder = str(values.get('MOVIE_LIBRARY_4K_FOLDER') or '').strip()
        tv_4k_folder = str(values.get('TV_LIBRARY_4K_FOLDER') or '').strip()
        anime_folder = str(values.get('ANIME_LIBRARY_FOLDER') or '').strip()

        if library_root:
            if use_standard and not movie_folder:
                movie_folder = os.path.join(library_root, 'movies')
            if use_standard and not tv_folder:
                tv_folder = os.path.join(library_root, 'tv')

        # 4K defaults to the main library root. Users can override either path explicitly.
        has_4k_service = bool(values.get('RADARR_4K_URL') or values.get('SONARR_4K_URL'))
        if use_4k and has_4k_service and library_root:
            if not movie_4k_folder:
                movie_4k_folder = os.path.join(library_root, 'movies-4k')
            if not tv_4k_folder:
                tv_4k_folder = os.path.join(library_root, 'tv-4k')

        if use_anime and library_root and not anime_folder:
            anime_folder = os.path.join(library_root, 'anime')

        if not use_standard:
            movie_folder = ''
            tv_folder = ''
        if not use_4k:
            movie_4k_folder = ''
            tv_4k_folder = ''
        if not use_anime:
            anime_folder = ''

        dir_mode = _parse_octal_mode(values.get('PLACEHOLDER_DIR_MODE', '777'), 0o777)
        folder_values = {
            'MOVIE_LIBRARY_FOLDER': movie_folder,
            'TV_LIBRARY_FOLDER': tv_folder,
            'MOVIE_LIBRARY_4K_FOLDER': movie_4k_folder,
            'TV_LIBRARY_4K_FOLDER': tv_4k_folder,
            'ANIME_LIBRARY_FOLDER': anime_folder,
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
        return bool(self.RADARR_4K_URL and self.MOVIE_LIBRARY_4K_FOLDER) or bool(self.SONARR_4K_URL and self.TV_LIBRARY_4K_FOLDER)

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
                        parsed_instances.append(
                            {
                                "instance_key": instance_key,
                                "arr_type": arr_type,
                                "url": url,
                                "api_key": api_key,
                                "label": str(item.get("label") or instance_key).strip() or instance_key,
                                "is_4k": bool(item.get("is_4k", False)),
                            }
                        )
            except Exception as exc:
                logger.warning(f"Failed to parse ARR_INSTANCES_JSON; falling back to legacy settings: {exc}")

        if parsed_instances:
            deduped: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in parsed_instances:
                key = str(item.get("instance_key") or "").strip().lower()
                if not key or key in seen:
                    continue
                deduped.append(item)
                seen.add(key)
            if deduped:
                return deduped

        instances: list[dict[str, Any]] = []
        if self.RADARR_URL and self.RADARR_API_KEY:
            instances.append(
                {
                    "instance_key": "radarr_std",
                    "arr_type": "radarr",
                    "url": self.RADARR_URL,
                    "api_key": self.RADARR_API_KEY,
                    "label": "radarr_std",
                    "is_4k": False,
                }
            )
        if self.RADARR_4K_URL and self.RADARR_4K_API_KEY:
            instances.append(
                {
                    "instance_key": "radarr_4k",
                    "arr_type": "radarr",
                    "url": self.RADARR_4K_URL,
                    "api_key": self.RADARR_4K_API_KEY,
                    "label": "radarr_4k",
                    "is_4k": True,
                }
            )
        if self.SONARR_URL and self.SONARR_API_KEY:
            instances.append(
                {
                    "instance_key": "sonarr_std",
                    "arr_type": "sonarr",
                    "url": self.SONARR_URL,
                    "api_key": self.SONARR_API_KEY,
                    "label": "sonarr_std",
                    "is_4k": False,
                }
            )
        if self.SONARR_4K_URL and self.SONARR_4K_API_KEY:
            instances.append(
                {
                    "instance_key": "sonarr_4k",
                    "arr_type": "sonarr",
                    "url": self.SONARR_4K_URL,
                    "api_key": self.SONARR_4K_API_KEY,
                    "label": "sonarr_4k",
                    "is_4k": True,
                }
            )
        return instances

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

    class Config:
        env_file = str(dotenv_path)
        env_file_encoding = 'utf-8'
        extra = "ignore"  # Ignore extra values not defined in the model
        case_sensitive = True

settings = Settings()
