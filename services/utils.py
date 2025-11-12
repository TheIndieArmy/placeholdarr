import os
import re
from pathlib import Path
from core.config import settings
from services.postgres.db import get_session
from services.postgres.movie_repo import MovieRepository
import xml.sax.saxutils as saxutils
import tempfile
import time

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
    """Remove status markers like [Request], [Status], etc. and trailing dash markers"""
    if not title:
        return ""
    
    # Remove markers in brackets like [Request], [Status], etc.
    title = re.sub(r'\[.*?\]\s*', '', title)
    
    # Remove trailing dash markers like " - [Status]" or just trailing " -"
    title = re.sub(r'\s*-\s*(\[.*?\])?\s*$', '', title)
    
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
    if settings.DUMMY_MOVIE_LIBRARY_4K_FOLDER and file_path.startswith(settings.DUMMY_MOVIE_LIBRARY_4K_FOLDER):
        return True
    if settings.DUMMY_TV_LIBRARY_4K_FOLDER and file_path.startswith(settings.DUMMY_TV_LIBRARY_4K_FOLDER):
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
            "library_folder": settings.DUMMY_MOVIE_LIBRARY_4K_FOLDER if is_4k else settings.DUMMY_MOVIE_LIBRARY_FOLDER,
            "section_id": settings.PLEX_MOVIE_SECTION_ID,
            "id_type": "tmdbId",
            "queue_id_field": "movieId",
            "search_type": "movie"  # Added this
        }
    else:  # TV
        return {
            "url": settings.SONARR_4K_URL if is_4k else settings.SONARR_URL,
            "api_key": settings.SONARR_4K_API_KEY if is_4k else settings.SONARR_API_KEY,
            "library_folder": settings.DUMMY_TV_LIBRARY_4K_FOLDER if is_4k else settings.DUMMY_TV_LIBRARY_FOLDER,
            "section_id": settings.PLEX_TV_SECTION_ID,
            "id_type": "tvdbId",
            "queue_id_field": "episodeId",
            "search_type": media_type  # This will be 'episode', 'season', or 'series'
        }

def get_movie_by_id(movie_id, session=None):
    session = session if session else get_session()
    movie_repo = MovieRepository(session)
    movie_repo.get_by_id(movie_id)
    return movie_repo

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
    # --- Always use basename of folder_path for movies if provided ---
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
        env_base = getattr(settings, "DUMMY_MOVIE_LIBRARY_FOLDER", None)
    else:
        env_base = getattr(settings, "DUMMY_TV_LIBRARY_FOLDER", None)
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
            if media_id and str(media_id).lower() != 'none':
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
        if media_id and str(media_id).lower() != 'none':
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

def _status_title_suffix(status: str) -> str:
    """Return bracketed status suffix; empty string if none."""
    if not status:
        return ""
    return f" [{status}]"

def _status_plot_prefix(status: str) -> str:
    """Return bracketed status prefix for plot/overview."""
    if not status:
        return ""
    return f"[{status}] "

def _truncate_text(text: str, max_chars: int) -> str:
    """Truncate text at a word boundary to max_chars, return possibly-trimmed string."""
    if not text or max_chars is None or max_chars <= 0:
        return text
    if len(text) <= max_chars:
        return text
    # Trim and avoid breaking words
    cut = text[:max_chars]
    # Backtrack to whitespace if possible
    idx = cut.rfind(' ')
    if idx > int(max_chars * 0.6):
        cut = cut[:idx]
    return cut.rstrip() + '...'

def _escape(text: str) -> str:
    if text is None:
        return ''
    return saxutils.escape(text)

def render_movie_nfo(movie: dict, status: str = None) -> str:
    """Render a basic movie.nfo XML given a movie dict.

    Expected keys in movie dict: title, year, tmdbid, imdbid, radarr_overview
    """
    max_chars = getattr(settings, 'NFO_OVERVIEW_MAX_CHARS', 5000)
    include_ids = getattr(settings, 'NFO_INCLUDE_UNIQUEIDS', True)

    title = movie.get('title') or ''
    year = movie.get('year') or ''
    overview = movie.get('radarr_overview') or ''

    title_with_status = f"{title}{_status_title_suffix(status)}"
    plot = _status_plot_prefix(status) + _truncate_text(overview, max_chars)

    xml = ["<?xml version=\"1.0\" encoding=\"utf-8\"?>", "<movie>"]
    xml.append(f"  <title>{_escape(title_with_status)}</title>")
    if title and title != title_with_status:
        xml.append(f"  <originaltitle>{_escape(title)}</originaltitle>")
    if year:
        xml.append(f"  <year>{_escape(str(year))}</year>")
    if plot:
        xml.append(f"  <plot>{_escape(plot)}</plot>")
    if include_ids:
        if movie.get('tmdbid'):
            xml.append(f"  <uniqueid type=\"tmdb\">{_escape(str(movie.get('tmdbid')))}</uniqueid>")
        if movie.get('imdbid'):
            xml.append(f"  <uniqueid type=\"imdb\">{_escape(str(movie.get('imdbid')))}</uniqueid>")
    xml.append("</movie>")
    return '\n'.join(xml) + '\n'

def render_series_nfo(series: dict, status: str = None) -> str:
    """Render a tvshow nfo. Expected keys: title, year, tvdbid, sonarr_series_overview"""
    max_chars = getattr(settings, 'NFO_OVERVIEW_MAX_CHARS', 5000)
    include_ids = getattr(settings, 'NFO_INCLUDE_UNIQUEIDS', True)

    title = series.get('title') or ''
    year = series.get('year') or ''
    overview = series.get('sonarr_series_overview') or ''

    plot = _status_plot_prefix(status) + _truncate_text(overview, max_chars)
    xml = ["<?xml version=\"1.0\" encoding=\"utf-8\"?>", "<tvshow>"]
    xml.append(f"  <title>{_escape(title)}</title>")
    if year:
        xml.append(f"  <year>{_escape(str(year))}</year>")
    if plot:
        xml.append(f"  <plot>{_escape(plot)}</plot>")
    if include_ids and series.get('tvdbid'):
        xml.append(f"  <uniqueid type=\"thetvdb\">{_escape(str(series.get('tvdbid')))}</uniqueid>")
    xml.append("</tvshow>")
    return '\n'.join(xml) + '\n'

def render_episode_nfo(episode: dict, status: str = None) -> str:
    """Render an episode .nfo. Expected keys: title, season, episode, aired, sonarr_episode_overview"""
    max_chars = getattr(settings, 'NFO_OVERVIEW_MAX_CHARS', 5000)
    include_ids = getattr(settings, 'NFO_INCLUDE_UNIQUEIDS', True)

    title = episode.get('title') or ''
    season = episode.get('season')
    epnum = episode.get('episode')
    aired = episode.get('aired')
    overview = episode.get('sonarr_episode_overview') or ''

    title_with_status = f"{title}{_status_title_suffix(status)}"
    plot = _status_plot_prefix(status) + _truncate_text(overview, max_chars)

    xml = ["<?xml version=\"1.0\" encoding=\"utf-8\"?>", "<episodedetails>"]
    xml.append(f"  <title>{_escape(title_with_status)}</title>")
    if season is not None:
        xml.append(f"  <season>{_escape(str(season))}</season>")
    if epnum is not None:
        xml.append(f"  <episode>{_escape(str(epnum))}</episode>")
    if aired:
        xml.append(f"  <aired>{_escape(str(aired))}</aired>")
    if plot:
        xml.append(f"  <plot>{_escape(plot)}</plot>")
    if include_ids and episode.get('tvdb'):
        xml.append(f"  <uniqueid type=\"thetvdb\">{_escape(str(episode.get('tvdb')))}</uniqueid>")
    xml.append("</episodedetails>")
    return '\n'.join(xml) + '\n'

def write_nfo(nfo_path: str, xml_text: str) -> bool:
    """Atomically write xml_text to nfo_path using a temp file and os.replace.
    Also set file permissions to be open (rw-rw-rw-) so media servers can read/write as needed.
    """
    try:
        dirpath = os.path.dirname(nfo_path)
        if dirpath and not os.path.exists(dirpath):
            os.makedirs(dirpath, exist_ok=True)

        # Write to temp file in same directory for atomic replace
        fd, tmp = tempfile.mkstemp(prefix='.tmp_nfo_', dir=dirpath or '.')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                fh.write(xml_text)
            # Ensure file timestamp changes slightly to help scanners
            os.replace(tmp, nfo_path)
            # Set permissive file permissions so NFOs are readable/writable by media server users
            try:
                os.chmod(nfo_path, 0o666)
            except Exception:
                # Non-fatal: permission change failure should not prevent success
                pass
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass

        # small sleep optionally to avoid tight write loops
        time.sleep(0.01)
        return True
    except Exception as e:
        # Log is not available in this module; raise for caller to handle or return False
        return False

def write_nfo_for_placeholder(placeholder_path: str, meta: dict, media_type: str = 'tv', status: str = None) -> bool:
    """Given the placeholder file path, produce the matching .nfo and write it.

    placeholder_path: full path to the .mp4 placeholder file
    meta: dict of metadata consumed by render_*_nfo
    media_type: 'movie' or 'tv'
    status: optional status string to include
    """
    if not getattr(settings, 'PLACEHOLDARR_WRITE_NFO', True):
        return False

    base = os.path.splitext(placeholder_path)[0]
    nfo_path = f"{base}.nfo"

    try:
        if media_type == 'movie':
            xml = render_movie_nfo(meta, status=status)
        else:
            # For episodes, caller may supply either episode-level meta or series+episode
            xml = render_episode_nfo(meta, status=status)
        ok = write_nfo(nfo_path, xml)
        return ok
    except Exception:
        return False