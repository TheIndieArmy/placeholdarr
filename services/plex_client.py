import os, urllib.parse, requests
from urllib.parse import quote
from plexapi.server import PlexServer
from core.config import settings
from core.logger import logger

def build_plex_url(path: str) -> str:
    if not getattr(settings, "plex_enabled", False) or not settings.PLEX_URL:
        return ""
    """Build a complete Plex URL with proper path handling."""
    # Remove any leading/trailing slashes from both base and path
    base = settings.PLEX_URL.rstrip('/')
    clean_path = path.strip('/')
    
    # Ensure clean URL construction
    url = f"{base}/{clean_path}"
    logger.debug(f"Built Plex URL: {url}", extra={'emoji_type': 'debug'})
    return url

def refresh_plex_item(item_path, media_type=None):
    if not getattr(settings, "plex_enabled", False) or not settings.PLEX_URL or not settings.PLEX_TOKEN:
        return False
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
    if not getattr(settings, "plex_enabled", False) or not settings.PLEX_URL or not settings.PLEX_TOKEN:
        return None
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
    if not getattr(settings, "plex_enabled", False) or not settings.PLEX_URL or not settings.PLEX_TOKEN:
        return None
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

def _prepend_status_to_summary(summary, status):
    """Prepend status to summary, replacing any previous status marker."""
    import re
    if not summary:
        summary = ""
    # Remove any existing status marker at the start
    summary = re.sub(r"^\[.*?\]\s*", "", summary)
    if status:
        return f"[{status}] {summary}".strip()
    else:
        return summary.strip()

def update_plex_title_status(media_type, media_id, title, status=None, year=None, season=None, episode=None):
    if not getattr(settings, "plex_enabled", False) or not settings.PLEX_URL or not settings.PLEX_TOKEN:
        return False
    """
    Update Plex summary with status or remove status markers.
    Uses ID-based matching to find the item.
    """
    try:
        if not plex:
            logger.error("Plex server not available", extra={'emoji_type': 'error'})
            return False

        if media_type == 'tv':
            show = find_show_by_id(media_id, title)
            if not show:
                logger.error(f"Could not find show with TVDB ID {media_id} for summary update", extra={'emoji_type': 'error'})
                return False
            # Update episode summary
            if season is not None and episode is not None:
                try:
                    episode_obj = show.episode(season=season, episode=episode)
                except Exception as e:
                    logger.warning(f"Episode S{season}E{episode} not found for '{show.title}' in Plex. Skipping summary update. ({e})", extra={'emoji_type': 'skip'})
                    return False
                if not episode_obj:
                    logger.warning(f"Episode S{season}E{episode} not found for '{show.title}' in Plex. Skipping summary update.", extra={'emoji_type': 'skip'})
                    return False
                current_summary = getattr(episode_obj, 'summary', '') or ''
                new_summary = _prepend_status_to_summary(current_summary, status)
                episode_obj.editSummary(new_summary)
                episode_obj.reload()
                logger.info(f"Updated episode summary for '{show.title}' S{season}E{episode} to: {new_summary}", extra={'emoji_type': 'update'})
                return True
            # Update season summary
            elif season is not None and episode is None:
                try:
                    # Debug: log all available seasons
                    all_seasons = list(show.seasons())
                    logger.debug(f"Available seasons for '{show.title}': {[getattr(s, 'index', None) for s in all_seasons]} | Titles: {[getattr(s, 'title', None) for s in all_seasons]}")
                    season_obj = None
                    for s in all_seasons:
                        if hasattr(s, 'index') and s.index == season:
                            season_obj = s
                            break
                        # Try matching by title (e.g., 'Season 1')
                        if hasattr(s, 'title') and s.title.strip().endswith(str(season)):
                            season_obj = s
                            break
                    if not season_obj:
                        raise Exception(f"Unable to find elem: cls=Season, attrs={{'index': {season}}}")
                except Exception as e:
                    logger.warning(f"Season S{season} not found for '{show.title}' in Plex. Skipping summary update. ({e})", extra={'emoji_type': 'skip'})
                    return False
                current_summary = getattr(season_obj, 'summary', '') or ''
                new_summary = _prepend_status_to_summary(current_summary, status)
                season_obj.editSummary(new_summary)
                season_obj.reload()
                logger.info(f"Updated season summary for '{show.title}' S{season} to: {new_summary}", extra={'emoji_type': 'update'})
                return True
            # Update series summary
            elif season is None and episode is None:
                current_summary = getattr(show, 'summary', '') or ''
                new_summary = _prepend_status_to_summary(current_summary, status)
                show.editSummary(new_summary)
                show.reload()
                logger.info(f"Updated series summary for '{show.title}' to: {new_summary}", extra={'emoji_type': 'update'})
                return True
            else:
                return False

        elif media_type == 'movie':
            movie = find_movie_by_id(media_id, title, year)
            if not movie:
                logger.error(f"Could not find movie with TMDB ID {media_id} for summary update", extra={'emoji_type': 'error'})
                return False
            current_summary = getattr(movie, 'summary', '') or ''
            new_summary = _prepend_status_to_summary(current_summary, status)
            movie.editSummary(new_summary)
            movie.reload()
            logger.info(f"Updated movie summary for '{movie.title}' to: {new_summary}", extra={'emoji_type': 'update'})
            return True

        return False

    except Exception as e:
        logger.error(f"Error updating summary status: {e}", extra={'emoji_type': 'error'})
        return False

def batch_update_plex_episode_status(tvdb_id, title, status_map, throttle=0.2):
    """
    Efficiently update episode summaries for a show in Plex in batch.
    Args:
        tvdb_id: TVDB ID of the show
        title: Title of the show (fallback)
        status_map: dict of (season, episode) -> status string (or None to clear)
        throttle: seconds to wait between updates (default 0.2)
    Returns:
        dict of (season, episode) -> True/False for update success
    """
    import time
    results = {}
    show = find_show_by_id(tvdb_id, title)
    if not show:
        logger.error(f"[Batch] Could not find show with TVDB ID {tvdb_id}", extra={'emoji_type': 'error'})
        return results
    # Fetch all episodes once
    try:
        all_eps = list(show.episodes())
        # Build mapping: (season, episode) -> episode_obj
        ep_map = {}
        for ep in all_eps:
            try:
                s = getattr(ep, 'seasonNumber', None) or getattr(ep, 'season', None)
                e = getattr(ep, 'episodeNumber', None) or getattr(ep, 'index', None)
                if s is not None and e is not None:
                    ep_map[(int(s), int(e))] = ep
            except Exception as ex:
                logger.warning(f"[Batch] Error mapping episode: {ex}", extra={'emoji_type': 'skip'})
        # Update only those in status_map
        updated_count = 0
        for (season, episode), status in status_map.items():
            ep_obj = ep_map.get((int(season), int(episode)))
            if not ep_obj:
                logger.warning(f"[Batch] Episode S{season}E{episode} not found for '{show.title}'", extra={'emoji_type': 'skip'})
                results[(season, episode)] = False
                continue
            current_summary = getattr(ep_obj, 'summary', '') or ''
            new_summary = _prepend_status_to_summary(current_summary, status)
            if current_summary.strip() == new_summary.strip():
                logger.debug(f"[Batch] No summary change for S{season}E{episode}", extra={'emoji_type': 'skip'})
                results[(season, episode)] = True
                continue
            try:
                ep_obj.editSummary(new_summary)
                ep_obj.reload()
                logger.debug(f"[Batch] Updated summary for '{show.title}' S{season}E{episode} to: {new_summary}", extra={'emoji_type': 'update'})
                results[(season, episode)] = True
                updated_count += 1
                time.sleep(throttle)
            except Exception as e:
                logger.error(f"[Batch] Failed to update summary for S{season}E{episode}: {e}", extra={'emoji_type': 'error'})
                results[(season, episode)] = False
        if updated_count > 0:
            logger.info(f"[Batch] Updated summaries for {updated_count} episode(s) in '{show.title}'", extra={'emoji_type': 'update'})
    except Exception as e:
        logger.error(f"[Batch] Error fetching episodes for show {tvdb_id}: {e}", extra={'emoji_type': 'error'})
    return results

def test_plex_endpoints():
    """Test key Plex API endpoints needed for operation."""
    try:
        url = f"{settings.PLEX_URL}/library/sections"
        headers = {'X-Plex-Token': settings.PLEX_TOKEN}
        import requests
        resp = requests.get(url, headers=headers, timeout=5)
        resp.raise_for_status()
        logger.info("Plex /library/sections endpoint accessible", extra={'emoji_type': 'success'})
    except Exception as ex:
        logger.error(f"Plex /library/sections endpoint failed: {ex}", extra={'emoji_type': 'error'})

# Run connection test at import time (same pattern as Jellyfin)
if getattr(settings, "plex_enabled", False):
    try:
        plex = PlexServer(settings.PLEX_URL, settings.PLEX_TOKEN)
        logger.info("Connected to Plex server", extra={'emoji_type': 'success'})
        test_plex_endpoints()
    except Exception as e:
        logger.error(f"Failed to connect to Plex server: {e}", extra={'emoji_type': 'error'})
        plex = None
else:
    plex = None