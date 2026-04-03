import os
from pathlib import Path
from typing import Literal, Optional
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
    WORKER_COUNT: int = os.getenv("WORKER_COUNT", 4)
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
    RADARR_URL: str
    RADARR_API_KEY: str
    SONARR_URL: str
    SONARR_API_KEY: str

    # 4K Services (optional)
    RADARR_4K_URL: str = ""
    RADARR_4K_API_KEY: str = ""
    SONARR_4K_URL: str = ""
    SONARR_4K_API_KEY: str = ""

    # Webhook/sync instance keys (customizable labels used in webhook query params and DB instance_key columns)
    RADARR_STD_INSTANCE_KEY: str = os.getenv("RADARR_STD_INSTANCE_KEY", "radarr_std").split('#')[0].strip().lower()
    RADARR_4K_INSTANCE_KEY: str = os.getenv("RADARR_4K_INSTANCE_KEY", "radarr_4k").split('#')[0].strip().lower()
    SONARR_STD_INSTANCE_KEY: str = os.getenv("SONARR_STD_INSTANCE_KEY", "sonarr_std").split('#')[0].strip().lower()
    SONARR_4K_INSTANCE_KEY: str = os.getenv("SONARR_4K_INSTANCE_KEY", "sonarr_4k").split('#')[0].strip().lower()
    TAUTULLI_INSTANCE_KEY: str = os.getenv("TAUTULLI_INSTANCE_KEY", "tautulli").split('#')[0].strip().lower()
    JELLYFIN_INSTANCE_KEY: str = os.getenv("JELLYFIN_INSTANCE_KEY", "jellyfin").split('#')[0].strip().lower()
    EMBY_INSTANCE_KEY: str = os.getenv("EMBY_INSTANCE_KEY", "emby").split('#')[0].strip().lower()

    # Startup sync mode:
    # - auto: first successful ARR startup run is full, then lite on later startups
    # - full: always run full ARR startup sync
    # - lite: run ARR history delta catch-up only
    # - off: skip ARR startup sync
    STARTUP_SYNC_MODE: Literal["auto", "full", "lite", "off"] = os.getenv("STARTUP_SYNC_MODE", "auto").split('#')[0].strip().lower()
    # ARR startup check mode:
    # - off: skip ARR preflight checks
    # - config: verify configured URL/API-key pairs only
    # - live: make live ARR API calls to verify reachability/auth
    STARTUP_ARR_CHECK_MODE: Literal["off", "config", "live"] = os.getenv("STARTUP_ARR_CHECK_MODE", "config").split('#')[0].strip().lower()
    FULL_SYNC_INTERVAL_HOURS: int = int(os.getenv("FULL_SYNC_INTERVAL_HOURS", "0").split('#')[0].strip())

    # Library Paths
    LIBRARY_ROOT: str = os.getenv("LIBRARY_ROOT", "").split('#')[0].strip()
    MOVIE_LIBRARY_FOLDER: str = os.getenv("MOVIE_LIBRARY_FOLDER", "").split('#')[0].strip()
    TV_LIBRARY_FOLDER: str = os.getenv("TV_LIBRARY_FOLDER", "").split('#')[0].strip()
    MOVIE_LIBRARY_4K_FOLDER: str = os.getenv("MOVIE_LIBRARY_4K_FOLDER", "").split('#')[0].strip()
    TV_LIBRARY_4K_FOLDER: str = os.getenv("TV_LIBRARY_4K_FOLDER", "").split('#')[0].strip()

    # Application
    PLAYBACK_COOLDOWN: int = int(os.environ.get("PLAYBACK_COOLDOWN", "30").split('#')[0].strip())
    MAX_MONITOR_TIME: int = int(os.getenv("MAX_MONITOR_TIME", "60").split('#')[0].strip())
    CHECK_INTERVAL: int = int(os.getenv("CHECK_INTERVAL", "10").split('#')[0].strip())
    AVAILABLE_CLEANUP_DELAY: int = int(os.getenv("AVAILABLE_CLEANUP_DELAY", "10").split('#')[0].strip())

    # Dummy file management
    DUMMY_FILE_PATH: str
    COMING_SOON_DUMMY_FILE_PATH: str = ""  # Optional
    PLACEHOLDER_STRATEGY: Literal["hardlink", "copy"] = "hardlink"
    PLACEHOLDER_CREATE_NFO: bool = os.getenv("PLACEHOLDER_CREATE_NFO", "true").split('#')[0].strip().lower() == "true"
    PLACEHOLDER_STATUS_UPDATES: str = os.getenv("PLACEHOLDER_STATUS_UPDATES", os.getenv("TITLE_UPDATES", "ALL")).split('#')[0].strip().upper()
    PLACEHOLDER_STATUS_PROJECTION_MODE: Literal["summary", "title", "both", "off"] = os.getenv("PLACEHOLDER_STATUS_PROJECTION_MODE", "summary").split('#')[0].strip().lower()
    PLACEHOLDER_FILE_MODE: str = os.getenv("PLACEHOLDER_FILE_MODE", "666").split('#')[0].strip()
    PLACEHOLDER_DIR_MODE: str = os.getenv("PLACEHOLDER_DIR_MODE", "777").split('#')[0].strip()
    FORCE_PRIME_ON_STARTUP: bool = os.getenv("FORCE_PRIME_ON_STARTUP", "false").split('#')[0].strip().lower() == "true"
    PRIMER_SERIES_COUNT: int = int(os.getenv("PRIMER_SERIES_COUNT", "3").split('#')[0].strip())
    PRIMER_EPISODES_PER_SERIES: int = int(os.getenv("PRIMER_EPISODES_PER_SERIES", "3").split('#')[0].strip())
    PRIMER_REFRESH_WAIT_SECONDS: int = int(os.getenv("PRIMER_REFRESH_WAIT_SECONDS", "60").split('#')[0].strip())

    # Play mode settings
    TV_PLAY_MODE: Literal["episode", "season", "series"] = "episode"
    EPISODES_LOOKAHEAD: int = int(os.getenv("EPISODES_LOOKAHEAD", "5").split('#')[0].strip())
    PLAYBACK_SEARCH_PREFERENCE: Literal["standard", "4k", "both"] = os.getenv("PLAYBACK_SEARCH_PREFERENCE", "both").split('#')[0].strip().lower()
    TV_PLAYBACK_INSTANCE_MODE: Literal["match", "preference", "both"] = os.getenv("TV_PLAYBACK_INSTANCE_MODE", "match").split('#')[0].strip().lower()
    PLAYBACK_FALLBACK_TIMEOUT_MINUTES: int = int(os.getenv("PLAYBACK_FALLBACK_TIMEOUT_MINUTES", "30").split('#')[0].strip())
    # Backward-compat alias for legacy modules still reading TITLE_UPDATES.
    TITLE_UPDATES: str = os.getenv("PLACEHOLDER_STATUS_UPDATES", os.getenv("TITLE_UPDATES", "ALL")).split('#')[0].strip().upper()
    AVAILABLE_CLEANUP_DELAY: int = int(os.getenv("AVAILABLE_CLEANUP_DELAY", "10"))

    # Migration settings
    MIGRATION: bool = False
      
    # Calendar-based status update settings
    # CALENDAR_LOOKAHEAD_DAYS: how many days into the future to create/show "Coming Soon" placeholders
    #   - Positive integer (e.g., 30): strict horizon for selected release type; items beyond are REQUEST
    #   - 0 (zero): disabled/off for future placeholder lookahead; future placeholders are reconciled out
    #   - -1 (negative): infinite lookahead; future items can remain Coming Soon
    # Default: 30 days
    CALENDAR_LOOKAHEAD_DAYS: int = int(os.getenv("CALENDAR_LOOKAHEAD_DAYS", "30").split('#')[0].strip())
    # Calendar scheduler cadence (independent from full sync).
    # <= 0 disables independent calendar scheduler.
    CALENDAR_SYNC_INTERVAL_HOURS: int = int(os.getenv("CALENDAR_SYNC_INTERVAL_HOURS", "12").split('#')[0].strip())
    ENABLE_COMING_SOON_PLACEHOLDERS: bool = os.getenv("ENABLE_COMING_SOON_PLACEHOLDERS", "true").split('#')[0].strip().lower() == "true"
    PREFERRED_MOVIE_DATE_TYPE: str = os.getenv("PREFERRED_MOVIE_DATE_TYPE", "inCinemas").split('#')[0].strip()
    ENABLE_COMING_SOON_COUNTDOWN: bool = os.getenv("ENABLE_COMING_SOON_COUNTDOWN", "true").split('#')[0].strip().lower() == "true"
    CALENDAR_PLACEHOLDER_MODE: str = os.getenv("CALENDAR_PLACEHOLDER_MODE", "episode").split('#')[0].strip().lower()

    # Include specials (season 0) when creating episode subflows
    INCLUDE_SPECIALS: bool = os.getenv("INCLUDE_SPECIALS", "false").split('#')[0].strip().lower() == "true"

    # Postgres
    DB_HOST: str
    DB_PORT: int
    DB_USER: str
    DB_PASS: str
    DB_NAME: str

    PLACEHOLDARR_HOST: str = os.getenv("PLACEHOLDARR_HOST", "0.0.0.0")

    ENABLE_PLEX: bool = os.getenv("ENABLE_PLEX", "true").split('#')[0].strip().lower() == "true"
    ENABLE_JELLYFIN: bool = os.getenv("ENABLE_JELLYFIN", "true").split('#')[0].strip().lower() == "true"
    ENABLE_EMBY: bool = os.getenv("ENABLE_EMBY", "false").split('#')[0].strip().lower() == "true"

    # Job queue / batching
    BATCH_SERIES_SUBFLOWS: bool = os.getenv("BATCH_SERIES_SUBFLOWS", "true").strip().lower() == "true"
    JOB_DEBOUNCE_SECONDS: int = int(os.getenv("JOB_DEBOUNCE_SECONDS", "3"))
    ENABLE_IMPORT_EVENT_HANDLERS: bool = os.getenv("ENABLE_IMPORT_EVENT_HANDLERS", "false").split('#')[0].strip().lower() == "true"
    ENABLE_DELETE_EVENT_HANDLERS: bool = os.getenv("ENABLE_DELETE_EVENT_HANDLERS", "false").split('#')[0].strip().lower() == "true"
    ENABLE_PLAYBACK_EVENT_HANDLERS: bool = os.getenv("ENABLE_PLAYBACK_EVENT_HANDLERS", "false").split('#')[0].strip().lower() == "true"
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
            raise ValueError("DUMMY_FILE_PATH is required")
        path = Path(v)
        if not path.exists():
            raise ValueError(f"Path does not exist: {v}")
        if not path.is_file():
            raise ValueError(f"DUMMY_FILE_PATH must be a file: {v}")
        if path.stat().st_size == 0:
            raise ValueError(f"Dummy file exists but is empty: {v}")
        return str(path.absolute())

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
            raise ValueError("ENABLE_PLEX is true but PLEX_URL or PLEX_TOKEN is missing.")
        if enable_jellyfin and not jellyfin_configured:
            raise ValueError("ENABLE_JELLYFIN is true but JELLYFIN_URL or JELLYFIN_TOKEN is missing.")
        if enable_emby and not emby_configured:
            raise ValueError("ENABLE_EMBY is true but EMBY_URL or EMBY_TOKEN is missing.")
        if not (enable_plex or enable_jellyfin or enable_emby):
            raise ValueError("At least one of ENABLE_PLEX, ENABLE_JELLYFIN, or ENABLE_EMBY must be true.")
        return values

    @root_validator(skip_on_failure=True)
    def configure_library_paths(cls, values):
        library_root = str(values.get('LIBRARY_ROOT') or '').strip()

        movie_folder = str(values.get('MOVIE_LIBRARY_FOLDER') or '').strip()
        tv_folder = str(values.get('TV_LIBRARY_FOLDER') or '').strip()
        movie_4k_folder = str(values.get('MOVIE_LIBRARY_4K_FOLDER') or '').strip()
        tv_4k_folder = str(values.get('TV_LIBRARY_4K_FOLDER') or '').strip()

        if library_root:
            if not movie_folder:
                movie_folder = os.path.join(library_root, 'movies')
            if not tv_folder:
                tv_folder = os.path.join(library_root, 'tv')

        # 4K defaults to the main library root. Users can override either path explicitly.
        has_4k_service = bool(values.get('RADARR_4K_URL') or values.get('SONARR_4K_URL'))
        if has_4k_service and library_root:
            if not movie_4k_folder:
                movie_4k_folder = os.path.join(library_root, 'movies-4k')
            if not tv_4k_folder:
                tv_4k_folder = os.path.join(library_root, 'tv-4k')

        if not movie_folder or not tv_folder:
            raise ValueError(
                'Set MOVIE_LIBRARY_FOLDER/TV_LIBRARY_FOLDER directly or set LIBRARY_ROOT so folders can be derived.'
            )

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
    def radarr_instance_keys(self) -> tuple[str, str]:
        return (self.RADARR_STD_INSTANCE_KEY, self.RADARR_4K_INSTANCE_KEY)

    @property
    def sonarr_instance_keys(self) -> tuple[str, str]:
        return (self.SONARR_STD_INSTANCE_KEY, self.SONARR_4K_INSTANCE_KEY)

    @property
    def playback_source_instance_keys(self) -> tuple[str, str, str]:
        return (self.TAUTULLI_INSTANCE_KEY, self.JELLYFIN_INSTANCE_KEY, self.EMBY_INSTANCE_KEY)

    @property
    def allowed_webhook_instance_keys(self) -> tuple[str, ...]:
        ordered = [
            self.RADARR_STD_INSTANCE_KEY,
            self.RADARR_4K_INSTANCE_KEY,
            self.SONARR_STD_INSTANCE_KEY,
            self.SONARR_4K_INSTANCE_KEY,
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
        return {
            self.RADARR_STD_INSTANCE_KEY: False,
            self.RADARR_4K_INSTANCE_KEY: True,
            self.SONARR_STD_INSTANCE_KEY: False,
            self.SONARR_4K_INSTANCE_KEY: True,
        }

    class Config:
        env_file = str(dotenv_path)
        env_file_encoding = 'utf-8'
        extra = "ignore"  # Ignore extra values not defined in the model
        case_sensitive = True

settings = Settings()
