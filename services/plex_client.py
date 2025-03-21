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

def find_show_by_id(tvdb_id, title=None):
    """
    Find a TV show in Plex library using TVDB ID as primary identifier
    Falls back to title matching if ID matching fails
    
    Args:
        tvdb_id: TVDB ID of the show
        title: Title to use as fallback (optional)
    
    Returns:
        Plex show object or None if not found
    """
    try:
        if not plex:
            logger.error("Plex server not available", extra={'emoji_type': 'error'})
            return None
            
        tv_section = plex.library.sectionByID(settings.PLEX_TV_SECTION_ID)
        show = None
        
        # Method 1: Match by TVDB ID in GUID (most reliable)
        all_shows = tv_section.all()
        for s in all_shows:
            for guid in s.guids:
                if f'tvdb://{tvdb_id}' in guid.id:
                    show = s
                    logger.debug(f"Found show by TVDB ID in metadata: '{s.title}'", 
                               extra={'emoji_type': 'debug'})
                    return show
        
        # Method 2: Look for TVDB ID in folder path
        for s in all_shows:
            if hasattr(s, 'locations') and s.locations:
                for location in s.locations:
                    if f"tvdb-{tvdb_id}" in location.lower():
                        show = s
                        logger.debug(f"Found show by TVDB ID in path: '{s.title}'", 
                                   extra={'emoji_type': 'debug'})
                        return show
        
        # Method 3: Fallback to title matching
        if title:
            # Clean title without year for matching
            clean_title = title
            if '(' in title and ')' in title:
                clean_title = title.split('(')[0].strip()
            
            show = tv_section.get(clean_title)
            if show:
                logger.debug(f"Found show by title (fallback): '{show.title}'", 
                           extra={'emoji_type': 'debug'})
                return show
        
        if not show:
            logger.debug(f"Show with TVDB ID {tvdb_id} not found in Plex library", 
                       extra={'emoji_type': 'debug'})
        
        return show
        
    except Exception as e:
        logger.error(f"Error finding show by ID: {e}", extra={'emoji_type': 'error'})
        return None

def find_movie_by_id(tmdb_id, title=None, year=None):
    """
    Find a movie in Plex library using TMDB ID as primary identifier
    Falls back to title matching if ID matching fails
    
    Args:
        tmdb_id: TMDB ID of the movie
        title: Title to use as fallback (optional)
        year: Year to use with title matching (optional)
    
    Returns:
        Plex movie object or None if not found
    """
    try:
        if not plex:
            logger.error("Plex server not available", extra={'emoji_type': 'error'})
            return None
            
        movie_section = plex.library.sectionByID(settings.PLEX_MOVIE_SECTION_ID)
        movie = None
        
        # Method 1: Match by TMDB ID in GUID (most reliable)
        all_movies = movie_section.all()
        for m in all_movies:
            for guid in m.guids:
                if f'tmdb://{tmdb_id}' in guid.id:
                    movie = m
                    logger.debug(f"Found movie by TMDB ID in metadata: '{m.title}'", 
                               extra={'emoji_type': 'debug'})
                    return movie
        
        # Method 2: Look for TMDB ID in folder path
        for m in all_movies:
            if hasattr(m, 'locations') and m.locations:
                for location in m.locations:
                    if f"tmdb-{tmdb_id}" in location.lower():
                        movie = m
                        logger.debug(f"Found movie by TMDB ID in path: '{m.title}'", 
                                   extra={'emoji_type': 'debug'})
                        return movie
        
        # Method 3: Fallback to title+year matching
        if title:
            # Clean title without year for matching
            clean_title = title
            if '(' in title and ')' in title:
                clean_title = title.split('(')[0].strip()
            
            # Try with title and year if available
            if year:
                for m in all_movies:
                    if (m.title.lower() == clean_title.lower() and 
                        hasattr(m, 'year') and m.year == int(year)):
                        movie = m
                        logger.debug(f"Found movie by title and year: '{m.title} ({m.year})'", 
                                   extra={'emoji_type': 'debug'})
                        return movie
            
            # Try with just title
            movie = movie_section.get(clean_title)
            if movie:
                logger.debug(f"Found movie by title (fallback): '{movie.title}'", 
                           extra={'emoji_type': 'debug'})
                return movie
        
        if not movie:
            logger.debug(f"Movie with TMDB ID {tmdb_id} not found in Plex library", 
                       extra={'emoji_type': 'debug'})
        
        return movie
        
    except Exception as e:
        logger.error(f"Error finding movie by ID: {e}", extra={'emoji_type': 'error'})
        return None

def update_plex_title_status(media_type, media_id, title, status=None, year=None, season=None, episode=None):
    """
    Update Plex title with status or remove status markers
    Uses ID-based matching to find the item
    
    Args:
        media_type: 'tv' or 'movie'
        media_id: TVDB ID for TV, TMDB ID for movies
        title: Title for fallback matching
        status: Status to add (None = remove status markers)
        year: Year for movies (optional)
        season: Season number for TV (optional)
        episode: Episode number for TV (optional)
    
    Returns:
        Success boolean
    """
    try:
        if not plex:
            logger.error("Plex server not available", extra={'emoji_type': 'error'})
            return False
            
        if media_type == 'tv' and season is not None and episode is not None:
            # Find TV show by ID
            show = find_show_by_id(media_id, title)
            if not show:
                logger.error(f"Could not find show with TVDB ID {media_id} for title update", 
                           extra={'emoji_type': 'error'})
                return False
            
            # Get episode
            try:
                episode_obj = show.episode(season=season, episode=episode)
            except Exception as e:
                logger.error(f"Error finding episode S{season}E{episode} for '{show.title}': {e}", 
                           extra={'emoji_type': 'error'})
                return False
                
            if not episode_obj:
                logger.error(f"Episode S{season}E{episode} not found for '{show.title}'", 
                           extra={'emoji_type': 'error'})
                return False
            
            # Update title
            from services.utils import strip_status_markers
            current_title = episode_obj.title
            base_title = strip_status_markers(current_title)
            
            if status:
                new_title = f"{base_title} - [{status}]"
            else:
                new_title = base_title
                
            episode_obj.editTitle(new_title)
            episode_obj.reload()
            logger.info(f"Updated episode title for '{show.title}' S{season}E{episode} to: {new_title}",
                      extra={'emoji_type': 'update'})
            return True
            
        elif media_type == 'movie':
            # Find movie by ID
            movie = find_movie_by_id(media_id, title, year)
            if not movie:
                logger.error(f"Could not find movie with TMDB ID {media_id} for title update", 
                           extra={'emoji_type': 'error'})
                return False
            
            # Update title
            from services.utils import strip_status_markers
            current_title = movie.title
            base_title = strip_status_markers(current_title)
            
            if status:
                new_title = f"{base_title} - [{status}]"
            else:
                new_title = base_title
                
            movie.editTitle(new_title)
            movie.reload()
            logger.info(f"Updated movie title for '{movie.title}' to: {new_title}",
                      extra={'emoji_type': 'update'})
            return True
            
        return False
        
    except Exception as e:
        logger.error(f"Error updating title status: {e}", extra={'emoji_type': 'error'})
        return False

try:
    plex = PlexServer(settings.PLEX_URL, settings.PLEX_TOKEN)
    logger.info("Connected to Plex via PlexAPI.", extra={'emoji_type': 'info'})
except Exception as e:
    logger.error(f"Failed to connect to Plex: {e}", extra={'emoji_type': 'error'})
    plex = None