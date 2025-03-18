import os, urllib.parse, requests
from urllib.parse import quote
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

def refresh_plex_item(item_path, media_type=None):
    """
    Refresh a specific Plex path
    
    Args:
        item_path (str): Path to refresh
        media_type (str, optional): 'movie' or 'tv' to help determine section
    """
    try:
        # Determine section ID from path or media_type
        section_id = None
        
        # First try to determine by path prefix
        if any(item_path.startswith(folder) for folder in [settings.MOVIE_LIBRARY_FOLDER, settings.MOVIE_LIBRARY_4K_FOLDER] if folder):
            section_id = settings.PLEX_MOVIE_SECTION_ID
        elif any(item_path.startswith(folder) for folder in [settings.TV_LIBRARY_FOLDER, settings.TV_LIBRARY_4K_FOLDER] if folder):
            section_id = settings.PLEX_TV_SECTION_ID
        
        # If that fails, use media_type hint or try to guess from path
        if section_id is None:
            if media_type == 'movie' or ('movie' in item_path.lower()):
                section_id = settings.PLEX_MOVIE_SECTION_ID
            elif media_type == 'tv' or 'tv' in item_path.lower() or 'season' in item_path.lower():
                section_id = settings.PLEX_TV_SECTION_ID
            else:
                logger.error(f"Cannot determine section ID for path: {item_path}", extra={'emoji_type': 'error'})
                return False
        
        # Make sure we're refreshing a directory, not a file
        if os.path.isfile(item_path):
            item_path = os.path.dirname(item_path)
            
        url = build_plex_url(f"library/sections/{section_id}/refresh?path={quote(item_path)}")
        logger.debug(f"Refreshing Plex by path: {item_path}", extra={'emoji_type': 'debug'})
        
        # Execute the refresh
        response = requests.get(url, headers={'X-Plex-Token': settings.PLEX_TOKEN})
        response.raise_for_status()
        logger.info(f"Plex refresh initiated successfully for path: {item_path}", extra={'emoji_type': 'refresh'})
        return True
        
    except Exception as e:
        logger.error(f"Failed to refresh Plex: {e}", extra={'emoji_type': 'error'})
        return False

try:
    plex = PlexServer(settings.PLEX_URL, settings.PLEX_TOKEN)
    logger.info("Connected to Plex via PlexAPI.", extra={'emoji_type': 'info'})
except Exception as e:
    logger.error(f"Failed to connect to Plex: {e}", extra={'emoji_type': 'error'})
    plex = None