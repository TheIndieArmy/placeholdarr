from pathlib import Path
from typing import Literal
from dotenv import load_dotenv
from pydantic_settings import BaseSettings
from pydantic import validator
import urllib.parse

# Get the project root directory (where main.py is)
ROOT_DIR = Path(__file__).parent.parent

# Use project root for .env path
dotenv_path = ROOT_DIR / ".env"

if not dotenv_path.exists():
    raise FileNotFoundError(f".env file does not exist at {dotenv_path}")

# Preload the environment variables
load_dotenv(dotenv_path)

class Settings(BaseSettings):
    LOG_LEVEL: str = "DEBUG"
    
    # Plex
    PLEX_URL: str
    PLEX_TOKEN: str
    PLEX_MOVIE_SECTION_ID: int
    PLEX_TV_SECTION_ID: int

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

    # Library Paths
    MOVIE_LIBRARY_FOLDER: str
    TV_LIBRARY_FOLDER: str
    MOVIE_LIBRARY_4K_FOLDER: str = ""
    TV_LIBRARY_4K_FOLDER: str = ""

    # Application
    MAX_MONITOR_TIME: int = 120
    CHECK_INTERVAL: int = 3
    CHECK_MAX_ATTEMPTS: int = 1000

    # Dummy file management
    DUMMY_FILE_PATH: str
    PLACEHOLDER_STRATEGY: Literal["hardlink", "copy"] = "hardlink"

    # Play mode settings
    TV_PLAY_MODE: Literal["episode", "season", "series"] = "episode"
    
    # Lookahead settings
    EPISODES_LOOKAHEAD: int = 3  # Number of episodes to look ahead for downloading when in episode mode
    INCLUDE_SPECIALS: bool = False  # Whether to include special episodes (season 0) in lookahead
    
    @validator('EPISODES_LOOKAHEAD')
    def validate_episodes_lookahead(cls, v):
        if v < 0:
            raise ValueError("EPISODES_LOOKAHEAD must be a non-negative integer")
        return v
    
    @validator('DUMMY_FILE_PATH', 'MOVIE_LIBRARY_FOLDER', 'TV_LIBRARY_FOLDER')
    def validate_path_exists(cls, v):
        path = Path(v)
        if not path.exists():
            raise ValueError(f"Path does not exist: {v}")
        return str(path.absolute())
    
    @validator('PLEX_URL', 'RADARR_URL', 'SONARR_URL')
    def validate_url(cls, v):
        if not v.startswith(('http://', 'https://')):
            raise ValueError(f"Invalid URL: {v}")
        return v.rstrip('/')

    @property
    def radarr_4k_port(self) -> int:
        return int(urllib.parse.urlparse(self.RADARR_4K_URL).port) if self.RADARR_4K_URL else None
    
    @property
    def sonarr_4k_port(self) -> int:
        return int(urllib.parse.urlparse(self.SONARR_4K_URL).port) if self.SONARR_4K_URL else None

    @property
    def has_4k_support(self) -> bool:
        return bool(self.RADARR_4K_URL and self.MOVIE_LIBRARY_4K_FOLDER) or bool(self.SONARR_4K_URL and self.TV_LIBRARY_4K_FOLDER)

    @property
    def plex_4k_movie_section_id(self) -> int:
        return self.PLEX_MOVIE_4K_SECTION_ID if hasattr(self, 'PLEX_MOVIE_4K_SECTION_ID') else self.PLEX_MOVIE_SECTION_ID

    @property
    def plex_4k_tv_section_id(self) -> int:
        return self.PLEX_TV_4K_SECTION_ID if hasattr(self, 'PLEX_TV_4K_SECTION_ID') else self.PLEX_TV_SECTION_ID

    class Config:
        env_file = str(dotenv_path)
        env_file_encoding = 'utf-8'
        extra = "ignore"  # Ignore extra values not defined in the model
        case_sensitive = True

settings = Settings()
