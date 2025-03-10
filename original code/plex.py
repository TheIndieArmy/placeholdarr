import os
import time
import requests
import threading
from typing import Optional, Dict, List, Any, Union
from plexapi.server import PlexServer

from core.logger import logger
from core.config import settings

# Initialize Plex server connection
plex = None
try:
    plex = PlexServer(settings.PLEX_URL, settings.PLEX_TOKEN)
except Exception as e:
    logger.error(f"Failed to connect to Plex: {e}", extra={'emoji_type': 'error'})

def refresh_specific_path(section_id: int, folder_path: str) -> bool:
    """
    Refresh a specific path in Plex library section
    
    Args:
        section_id: Plex library section ID
        folder_path: Path to refresh
        
    Returns:
        bool: Success or failure
    """
    try:
        if not plex:
            logger.error("Plex server not initialized", extra={'emoji_type': 'error'})
            return False
            
        section = plex.library.sectionByID(section_id)
        if not section:
            logger.error(f"Section not found: {section_id}", extra={'emoji_type': 'error'})
            return False
            
        # Use partial scan for the specific folder
        section.update(path=folder_path)
        logger.debug(f"Refreshing Plex path: {folder_path}", extra={'emoji_type': 'debug'})
        return True
        
    except Exception as e:
        logger.error(f"Failed to refresh Plex library: {e}", extra={'emoji_type': 'error'})
        return False

def update_title(rating_key: str, title: str, status: str = "") -> bool:
    """
    Update title with status marker
    
    Args:
        rating_key: Plex rating key
        title: Original title
        status: Status marker to add ([Request], etc.)
        
    Returns:
        bool: Success or failure
    """
    try:
        if not plex:
            logger.error("Plex server not initialized", extra={'emoji_type': 'error'})
            return False
            
        # Get the item by rating key
        item = plex.fetchItem(rating_key)
        if not item:
            logger.error(f"Item not found with rating key: {rating_key}", extra={'emoji_type': 'error'})
            return False
            
        # Clean the title first
        clean_title = title.split(' [')[0].strip()
        
        # Prepare new title
        if status:
            new_title = f"{clean_title} {status}"
        else:
            new_title = clean_title
            
        # Update if needed
        if item.title != new_title:
            item.editTitle(new_title)
            logger.debug(f"Updated item title: {new_title}", extra={'emoji_type': 'debug'})
            
        return True
        
    except Exception as e:
        logger.error(f"Failed to update title: {e}", extra={'emoji_type': 'error'})
        return False

def schedule_request_update(rating_key: str, title: str, delay: int = 0, retries: int = 0) -> None:
    """
    Schedule adding [Request] tag to a title
    
    Args:
        rating_key: Plex rating key
        title: Original title
        delay: Delay in seconds
        retries: Number of retries
        
    Returns:
        None
    """
    def _do_update():
        result = update_title(rating_key, title, "[Request]")
        
        if not result and retries > 0:
            # Try again in 5 seconds
            threading.Timer(5.0, _do_update).start()
    
    # Schedule the update
    timer = threading.Timer(delay, _do_update)
    timer.daemon = True
    timer.start()

def find_movie_by_tmdb_id(tmdb_id: int) -> Optional[str]:
    """
    Find a movie in Plex by TMDB ID
    
    Args:
        tmdb_id: TMDB ID of the movie
        
    Returns:
        str: Plex rating key or None if not found
    """
    try:
        if not plex:
            logger.error("Plex server not initialized", extra={'emoji_type': 'error'})
            return None
            
        # Try standard section first
        movie_section = plex.library.sectionByID(settings.PLEX_MOVIE_SECTION_ID)
        
        # Search for movies with TMDb GUID
        tmdb_guid = f"tmdb://{tmdb_id}"
        
        # Search through all movies looking for matching GUID
        for movie in movie_section.all():
            if hasattr(movie, 'guids') and movie.guids:
                for guid in movie.guids:
                    if str(tmdb_id) in guid.id:
                        return str(movie.ratingKey)
        
        # Try 4K section if available
        if hasattr(settings, 'PLEX_MOVIE_4K_SECTION_ID'):
            try:
                movie_section_4k = plex.library.sectionByID(settings.PLEX_MOVIE_4K_SECTION_ID)
                for movie in movie_section_4k.all():
                    if hasattr(movie, 'guids') and movie.guids:
                        for guid in movie.guids:
                            if str(tmdb_id) in guid.id:
                                return str(movie.ratingKey)
            except:
                pass
        
        logger.debug(f"Movie not found with TMDB ID: {tmdb_id}", extra={'emoji_type': 'debug'})
        return None
        
    except Exception as e:
        logger.error(f"Failed to find movie by TMDB ID: {e}", extra={'emoji_type': 'error'})
        return None

def find_episode_by_tvdb_id(tvdb_id: int, season_num: int, episode_num: int) -> Optional[str]:
    """
    Find an episode in Plex by TVDB ID, season and episode numbers
    
    Args:
        tvdb_id: TVDB ID of the series
        season_num: Season number
        episode_num: Episode number
        
    Returns:
        str: Plex rating key or None if not found
    """
    try:
        if not plex:
            logger.error("Plex server not initialized", extra={'emoji_type': 'error'})
            return None
            
        # Try standard section first
        tv_section = plex.library.sectionByID(settings.PLEX_TV_SECTION_ID)
        
        # Search for series with TVDB GUID
        tvdb_guid = f"tvdb://{tvdb_id}"
        
        # Search through all series
        for show in tv_section.all():
            if hasattr(show, 'guids') and show.guids:
                for guid in show.guids:
                    if str(tvdb_id) in guid.id:
                        # Found the show, now find the episode
                        try:
                            episode = show.episode(season=season_num, episode=episode_num)
                            return str(episode.ratingKey)
                        except:
                            pass
        
        # Try 4K section if available
        if hasattr(settings, 'PLEX_TV_4K_SECTION_ID'):
            try:
                tv_section_4k = plex.library.sectionByID(settings.PLEX_TV_4K_SECTION_ID)
                for show in tv_section_4k.all():
                    if hasattr(show, 'guids') and show.guids:
                        for guid in show.guids:
                            if str(tvdb_id) in guid.id:
                                try:
                                    episode = show.episode(season=season_num, episode=episode_num)
                                    return str(episode.ratingKey)
                                except:
                                    pass
            except:
                pass
        
        logger.debug(f"Episode not found: TVDB {tvdb_id}, S{season_num}E{episode_num}", extra={'emoji_type': 'debug'})
        return None
        
    except Exception as e:
        logger.error(f"Failed to find episode: {e}", extra={'emoji_type': 'error'})
        return None