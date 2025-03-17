import os, urllib.parse
from plexapi.server import PlexServer
from core.config import settings
from core.logger import logger

def build_plex_url(path: str) -> str:
    """Build a complete Plex URL with proper path handling."""
    # Remove any leading/trailing slashes from both base and path
    base = settings.PLEX_URL.rstrip('/')
    clean_path = path.strip('/')
    
    # Ensure clean URL construction
    url = f"{base}/{clean_path}"
    logger.debug(f"Built Plex URL: {url}", extra={'emoji_type': 'debug'})
    return url

try:
    plex = PlexServer(settings.PLEX_URL, settings.PLEX_TOKEN)
    logger.info("Connected to Plex via PlexAPI.", extra={'emoji_type': 'info'})
except Exception as e:
    logger.error(f"Failed to connect to Plex: {e}", extra={'emoji_type': 'error'})
    plex = None