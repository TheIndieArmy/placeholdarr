import os
from pathlib import Path
from typing import Literal, Optional
from dotenv import load_dotenv
from pydantic_settings import BaseSettings
from pydantic import validator, root_validator
import urllib.parse
import logging

logger = logging.getLogger(__name__)

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
    # Whether to include specials (from .env)
    INCLUDE_SPECIALS: bool = os.getenv("INCLUDE_SPECIALS", "false").strip().lower() == "true"
    LOG_LEVEL: str = os.getenv("PLACEHOLDARR_LOG_LEVEL", "INFO")
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

    # Sync Settings (grouped by instance)
    RADARR_SYNC_ON_STARTUP: bool = os.getenv("RADARR_SYNC_ON_STARTUP", "false").strip().lower() == "true"
    RADARR_SYNC_CRON: str = os.getenv("RADARR_SYNC_CRON", "")
    RADARR_4K_SYNC_ON_STARTUP: bool = os.getenv("RADARR_4K_SYNC_ON_STARTUP", "false").strip().lower() == "true"
    RADARR_4K_SYNC_CRON: str = os.getenv("RADARR_4K_SYNC_CRON", "")
    SONARR_SYNC_ON_STARTUP: bool = os.getenv("SONARR_SYNC_ON_STARTUP", "false").strip().lower() == "true"
    SONARR_SYNC_CRON: str = os.getenv("SONARR_SYNC_CRON", "")
    SONARR_4K_SYNC_ON_STARTUP: bool = os.getenv("SONARR_4K_SYNC_ON_STARTUP", "false").strip().lower() == "true"
    SONARR_4K_SYNC_CRON: str = os.getenv("SONARR_4K_SYNC_CRON", "")

    # Library Paths
    DUMMY_MOVIE_LIBRARY_FOLDER: str
    DUMMY_TV_LIBRARY_FOLDER: str
    DUMMY_MOVIE_LIBRARY_4K_FOLDER: str = ""
    DUMMY_TV_LIBRARY_4K_FOLDER: str = ""
    
    # Real library paths (optional - used for folder extraction when not using dummy libraries)
    MOVIE_LIBRARY_FOLDER: str = ""
    TV_LIBRARY_FOLDER: str = ""
    MOVIE_LIBRARY_4K_FOLDER: str = ""
    TV_LIBRARY_4K_FOLDER: str = ""

    # Application
    PLAYBACK_COOLDOWN: int = int(os.environ.get("PLAYBACK_COOLDOWN", "30").split('#')[0].strip())
    MAX_MONITOR_TIME: int = int(os.getenv("MAX_MONITOR_TIME", "60").split('#')[0].strip())
    CHECK_INTERVAL: int = int(os.getenv("CHECK_INTERVAL", "10").split('#')[0].strip())
    AVAILABLE_CLEANUP_DELAY: int = int(os.getenv("AVAILABLE_CLEANUP_DELAY", "10").split('#')[0].strip())

    # Dummy file management
    DUMMY_FILE_PATH: str
    COMING_SOON_DUMMY_FILE_PATH: str = ""  # Optional
    PLACEHOLDER_STRATEGY: Literal["hardlink", "copy"] = "hardlink"

    # Play mode settings
    TV_PLAY_MODE: Literal["episode", "season", "series"] = "episode"
    EPISODES_LOOKAHEAD: int = int(os.getenv("EPISODES_LOOKAHEAD", "5").split('#')[0].strip())
    TITLE_UPDATES: str = os.getenv("TITLE_UPDATES", "ALL")  # Options: OFF, REQUEST, ALL
    AVAILABLE_CLEANUP_DELAY: int = int(os.getenv("AVAILABLE_CLEANUP_DELAY", "10"))

    # Migration settings
    MIGRATION: bool = False
    
    # SubFlow reset settings on startup
    # Comma-separated list of SubFlow statuses to reset to PENDING on startup
    # Valid values: QUEUED, FAILED
    # Example: "QUEUED,FAILED" or "FAILED" or "" (empty to disable)
    RESET_SUBFLOWS_ON_STARTUP: str = os.getenv("RESET_SUBFLOWS_ON_STARTUP", "QUEUED,FAILED").split('#')[0].strip()
      
    # Calendar-based status update settings
    CALENDAR_LOOKAHEAD_DAYS: int = int(os.getenv("CALENDAR_LOOKAHEAD_DAYS", "30").split('#')[0].strip())
    CALENDAR_SYNC_INTERVAL_HOURS: int = int(os.getenv("CALENDAR_SYNC_INTERVAL_HOURS", "12").split('#')[0].strip())
    ENABLE_COMING_SOON_PLACEHOLDERS: bool = os.getenv("ENABLE_COMING_SOON_PLACEHOLDERS", "true").split('#')[0].strip().lower() == "true"
    PREFERRED_MOVIE_DATE_TYPE: str = os.getenv("PREFERRED_MOVIE_DATE_TYPE", "inCinemas").split('#')[0].strip()
    ENABLE_COMING_SOON_COUNTDOWN: bool = os.getenv("ENABLE_COMING_SOON_COUNTDOWN", "true").split('#')[0].strip().lower() == "true"
    CALENDAR_PLACEHOLDER_MODE: str = os.getenv("CALENDAR_PLACEHOLDER_MODE", "episode").split('#')[0].strip().lower()

    # Postgres
    DB_HOST: str
    DB_PORT: int
    DB_USER: str
    DB_PASS: str
    DB_NAME: str

    PLACEHOLDARR_HOST: str = os.getenv("PLACEHOLDARR_HOST", "0.0.0.0")

    ENABLE_PLEX: bool = os.getenv("ENABLE_PLEX", "true").split('#')[0].strip().lower() == "true"
    ENABLE_JELLYFIN: bool = os.getenv("ENABLE_JELLYFIN", "true").split('#')[0].strip().lower() == "true"

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
    
    @validator('DUMMY_FILE_PATH', 'COMING_SOON_DUMMY_FILE_PATH', 'DUMMY_MOVIE_LIBRARY_FOLDER', 'DUMMY_TV_LIBRARY_FOLDER')
    def validate_path_exists(cls, v):
        if not v:
            return v
        path = Path(v)
        if not path.exists():
            raise ValueError(f"Path does not exist: {v}")
        # If it's a file, check it's not empty (for DUMMY_FILE_PATH)
        if path.is_file() and path.name == os.path.basename(os.getenv("DUMMY_FILE_PATH", "")):
            if path.stat().st_size == 0:
                raise ValueError(f"Dummy file exists but is empty: {v}")
        return str(path.absolute())
    
    @validator('PLEX_URL', 'RADARR_URL', 'SONARR_URL', 'JELLYFIN_URL', pre=True)
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
        plex_keys = [values.get('PLEX_URL'), values.get('PLEX_TOKEN')]
        jellyfin_keys = [values.get('JELLYFIN_URL'), values.get('JELLYFIN_TOKEN')]
        plex_configured = all(plex_keys)
        jellyfin_configured = all(jellyfin_keys)
        if enable_plex and not plex_configured:
            raise ValueError("ENABLE_PLEX is true but PLEX_URL or PLEX_TOKEN is missing.")
        if enable_jellyfin and not jellyfin_configured:
            raise ValueError("ENABLE_JELLYFIN is true but JELLYFIN_URL or JELLYFIN_TOKEN is missing.")
        if not (enable_plex or enable_jellyfin):
            raise ValueError("At least one of ENABLE_PLEX or ENABLE_JELLYFIN must be true.")
        return values

    @property
    def plex_enabled(self) -> bool:
        return self.ENABLE_PLEX and bool(self.PLEX_URL and self.PLEX_TOKEN)

    @property
    def jellyfin_enabled(self) -> bool:
        return self.ENABLE_JELLYFIN and bool(self.JELLYFIN_URL and self.JELLYFIN_TOKEN)

    @property
    def radarr_4k_port(self) -> int:
        return int(urllib.parse.urlparse(self.RADARR_4K_URL).port) if self.RADARR_4K_URL else None
    
    @property
    def sonarr_4k_port(self) -> int:
        return int(urllib.parse.urlparse(self.SONARR_4K_URL).port) if self.SONARR_4K_URL else None

    @property
    def has_4k_support(self) -> bool:
        return bool(self.RADARR_4K_URL and self.DUMMY_MOVIE_LIBRARY_4K_FOLDER) or bool(self.SONARR_4K_URL and self.DUMMY_TV_LIBRARY_4K_FOLDER)

    @property
    def plex_4k_movie_section_id(self) -> int:
        return self.PLEX_MOVIE_4K_SECTION_ID if hasattr(self, 'PLEX_MOVIE_4K_SECTION_ID') else self.PLEX_MOVIE_SECTION_ID

    @property
    def plex_4k_tv_section_id(self) -> int:
        return self.PLEX_TV_4K_SECTION_ID if hasattr(self, 'PLEX_TV_4K_SECTION_ID') else self.PLEX_TV_SECTION_ID

    @property
    def host(self) -> str:
        return self.PLACEHOLDARR_HOST

    class Config:
        env_file = str(dotenv_path)
        env_file_encoding = 'utf-8'
        extra = "ignore"  # Ignore extra values not defined in the model
        case_sensitive = True

settings = Settings()
