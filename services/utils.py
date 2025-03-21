import os  # <-- required for get_series_folder
import re
from pathlib import Path
from core.config import settings

def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()

def dedup_title(title: str) -> str:
    parts = [p.strip() for p in title.split(' - ')]
    seen, deduped = set(), []
    for part in parts:
        if part not in seen:
            seen.add(part)
            deduped.append(part)
    return " - ".join(deduped)

def extract_episode_title(raw_title: str) -> str:
    clean = raw_title.split('[')[0].strip()
    parts = clean.split(" - ")
    if len(parts) >= 3 and parts[0] == parts[1]:
        return parts[2].strip()
    elif len(parts) >= 2:
        return parts[1].strip()
    return clean

def strip_movie_status(title: str) -> str:
    pattern = re.compile(r"\s*-\s*(Searching|Not Found - Search Timeout|Downloading\s+\d+%)(\s*-\s*)?$", re.IGNORECASE)
    prev = None
    while prev != title:
        prev = title
        title = pattern.sub("", title).strip()
    return title

def strip_status_markers(title: str) -> str:
    """Keep only the base title by removing everything after first dash or bracket"""
    # First split on '[' and take the first part
    title = title.split('[')[0].strip()
    # Then split on '-' and take the first part
    title = title.split('-')[0].strip()
    # Clean up any extra whitespace
    title = re.sub(r'\s+', ' ', title).strip()
    # Remove ellipsis if present
    title = title.replace('...', '')
    return title

def get_series_folder(media_type, target_base_folder, title, year, media_id, season_number=None) -> str:
    clean_title = sanitize_filename(title)
    year_str = f" ({year})" if year else ''
    folder = f"{clean_title}{year_str} {{tmdb-{media_id}}}" if media_type == 'movie' else f"{clean_title}{year_str} {{tvdb-{media_id}}}"
    return os.path.join(target_base_folder, folder)

def get_folder_path(media_type, base_path, title, year=None, media_id=None, season=None):
    """Generate folder path according to Placeholdarr's naming convention"""
    # First sanitize the title
    sanitized_title = sanitize_filename(title)
    
    # Remove any year pattern from the title to prevent duplication
    year_pattern = r'\s*\(\d{4}\)'
    sanitized_title = re.sub(year_pattern, '', sanitized_title).strip()
    
    # Add the year from metadata when available
    year_str = f" ({year})" if year else ""
    
    if media_type == "movie":
        # Movie folder: "{Movie Title} ({Year}) {tmdb-123456}{edition-Dummy}"
        folder_name = f"{sanitized_title}{year_str} {{tmdb-{media_id}}}{{edition-Dummy}}"
        return os.path.join(base_path, folder_name)
    else:
        # Series folder: "{Series Title} ({year}) {tvdb-123456} (dummy)"
        folder_name = f"{sanitized_title}{year_str} {{tvdb-{media_id}}} (dummy)"
        
        # Add season folder if provided
        if season is not None:
            return os.path.join(base_path, folder_name, f"Season {season:02d}")
        else:
            return os.path.join(base_path, folder_name)

def is_4k_request(file_path: str, source_port: int = None) -> bool:
    """
    Determine if this is a 4K request based on:
    1. File path (if it's in a 4K library)
    2. Source port (if it matches a 4K *arr instance)
    """
    if not settings.has_4k_support:
        return False

    # Check if path is in 4K library
    if settings.MOVIE_LIBRARY_4K_FOLDER and file_path.startswith(settings.MOVIE_LIBRARY_4K_FOLDER):
        return True
    if settings.TV_LIBRARY_4K_FOLDER and file_path.startswith(settings.TV_LIBRARY_4K_FOLDER):
        return True
    
    # Check if request came from 4K instance
    if source_port:
        if source_port == settings.radarr_4k_port or source_port == settings.sonarr_4k_port:
            return True
    
    return False

def get_arr_config(media_type: str, is_4k: bool = False) -> dict:
    """Get appropriate *arr configuration based on media type and quality"""
    if media_type == "movie":
        return {
            "url": settings.RADARR_4K_URL if is_4k else settings.RADARR_URL,
            "api_key": settings.RADARR_4K_API_KEY if is_4k else settings.RADARR_API_KEY,
            "library_folder": settings.MOVIE_LIBRARY_4K_FOLDER if is_4k else settings.MOVIE_LIBRARY_FOLDER,
            "section_id": settings.PLEX_MOVIE_SECTION_ID,
            "id_type": "tmdbId",
            "queue_id_field": "movieId",
            "search_type": "movie"  # Added this
        }
    else:  # TV
        return {
            "url": settings.SONARR_4K_URL if is_4k else settings.SONARR_URL,
            "api_key": settings.SONARR_4K_API_KEY if is_4k else settings.SONARR_API_KEY,
            "library_folder": settings.TV_LIBRARY_4K_FOLDER if is_4k else settings.TV_LIBRARY_FOLDER,
            "section_id": settings.PLEX_TV_SECTION_ID,
            "id_type": "tvdbId",
            "queue_id_field": "episodeId",
            "search_type": media_type  # This will be 'episode', 'season', or 'series'
        }