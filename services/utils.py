import re
import os
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

def resolve_final_folder(media_type, title=None, year=None, media_id=None, season_number=None, folder_path=None, arr_root_folder=None, season_folder_name=None, relative_path=None, payload=None):
    """
    Centralized function to resolve the final folder path for dummy file operations.
    Priority:
    1. If ENV is set, use it as base and append *arrs folder name (from payload).
    2. If ENV is blank, use *arrs root and folder name (from payload).
    Always append season folder name for TV if available.
    """
    import os
    # Extract folder name and season folder name from payload
    arr_folder_name = None
    arr_season_folder = None
    arr_root = None
    # --- PATCH: Always use basename of folder_path for movies if provided ---
    if media_type == "movie" and folder_path:
        arr_folder_name = os.path.basename(folder_path)
        arr_root = os.path.dirname(folder_path)
    elif payload:
        # Get full folder path from payload
        arr_full_path = payload.get('folderPath') or payload.get('path') or folder_path
        if arr_full_path:
            arr_folder_name = os.path.basename(arr_full_path)
            arr_root = os.path.dirname(arr_full_path)
        arr_season_folder = payload.get('seasonFolder') or payload.get('season_folder') or season_folder_name
    # Determine base path
    if media_type == "movie":
        env_base = getattr(settings, "MOVIE_LIBRARY_FOLDER", None)
    else:
        env_base = getattr(settings, "TV_LIBRARY_FOLDER", None)
    base_folder = None
    if env_base and str(env_base).strip():
        # ENV is set: use ENV as base, append *arrs folder name
        if arr_folder_name:
            base_folder = os.path.join(env_base, arr_folder_name)
        else:
            # Fallback: build from title/year/id
            folder_name = sanitize_filename(title) if title else ("Unknown Movie" if media_type == "movie" else "Unknown Series")
            if year:
                folder_name += f" ({year})"
            if media_id:
                folder_name += f" {{tmdb-{media_id}}}" if media_type == "movie" else f" {{tvdb-{media_id}}}"
            base_folder = os.path.join(env_base, folder_name)
    elif arr_root and arr_folder_name:
        # ENV is blank: use *arrs root and folder name
        base_folder = os.path.join(arr_root, arr_folder_name)
    else:
        # Fallback: build from title/year/id
        folder_name = sanitize_filename(title) if title else ("Unknown Movie" if media_type == "movie" else "Unknown Series")
        if year:
            folder_name += f" ({year})"
        if media_id:
            folder_name += f" {{tmdb-{media_id}}}" if media_type == "movie" else f" {{tvdb-{media_id}}}"
        base_folder = folder_path or os.path.join(env_base or "", folder_name)
    # Season folder resolution (for TV)
    if media_type != "movie":
        season_folder = arr_season_folder
        if not season_folder and relative_path:
            parts = os.path.normpath(relative_path).split(os.sep)
            for part in parts:
                if part.lower().startswith("season"):
                    season_folder = part
                    break
        elif not season_folder and season_number is not None:
            season_folder = f"Season {season_number:02d}"
        if season_folder:
            return os.path.join(base_folder, season_folder)
    return base_folder

def is_same_file(file1, file2):
    import os
    import hashlib
    if not os.path.exists(file1) or not os.path.exists(file2):
        return False
    size1 = os.path.getsize(file1)
    size2 = os.path.getsize(file2)
    if size1 != size2:
        return False
    def file_hash(path):
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    hash1 = file_hash(file1)
    hash2 = file_hash(file2)
    return hash1 == hash2