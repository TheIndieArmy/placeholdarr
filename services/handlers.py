import os, re, threading, time, shutil, requests
from fastapi.responses import JSONResponse
from core.config import settings
from core.logger import logger
from services.plex_client import plex, build_plex_url
from services.integrations import (
    place_dummy_file, delete_dummy_files, schedule_episode_request_update,
    schedule_movie_request_update, check_media_has_file,
    search_in_radarr, search_in_sonarr, trigger_sonarr_search
)
from services.utils import (
    strip_movie_status, sanitize_filename, extract_episode_title, 
    is_4k_request, strip_status_markers
)

def handle_webhook(data: dict, source_port: int = None):
    """Handle webhook with quality awareness"""
    source = data.get("instanceName", "Tautulli")
    logger.debug(f"{source} payload: {data}", extra={'emoji_type': 'debug'})
    
    # Get file path for quality detection
    file_path = (data.get('media', {}).get('file_info', {}).get('path') or 
                 data.get('movie', {}).get('folderPath') or 
                 data.get('file', ''))
    
    # Determine if this is a 4K request
    is_4k = is_4k_request(file_path, source_port)
    logger.debug(f"Quality determination: {'4K' if is_4k else 'Standard'}", extra={'emoji_type': 'debug'})
    
    event_type = (data.get('event') or data.get('eventType') or 'unknown').lower()
    logger.info(f"Received webhook event: {event_type}", extra={'emoji_type': 'webhook'})
    
    # Pass is_4k flag to all handlers
    if event_type == 'seriesadd':
        return handle_seriesadd(data, is_4k)
    elif event_type == 'episodefiledelete':
        return handle_episodefiledelete(data, is_4k)
    elif event_type == 'moviefiledelete':
        return handle_moviefiledelete(data)
    elif event_type == 'moviedelete':
        return handle_movie_delete(data)
    elif event_type in ('movieadd', 'movieadded'):  # adjusted condition to catch both forms
        return handle_movieadd(data)
    elif event_type == 'seriesdelete':
        return handle_seriesdelete(data)
    elif event_type == 'playback.start':
        return handle_playback(data)
    else:
        return handle_arrs_import(data)

def handle_seriesadd(data: dict, is_4k: bool = False):
    # Extract series info and episodes, create dummies and schedule updates.
    series = data.get('series', {})
    episodes = data.get('episodes', [])
    series_title = series.get('title', 'Unknown Series')
    series_year = series.get('year')
    tvdb_id = series.get('tvdbId')
    if not episodes:
        series_id = series.get('id')
        if series_id:
            r = requests.get(f"{settings.SONARR_URL}/episode",
                             params={'seriesId': series_id},
                             headers={'X-Api-Key': settings.SONARR_API_KEY})
            r.raise_for_status()
            episodes = r.json()
        else:
            logger.warning("No series ID provided in seriesadd event.", extra={'emoji_type': 'warning'})
            episodes = []
    unique_folders = set()
    for ep in episodes:
        season_num = ep.get('seasonNumber')
        episode_num = ep.get('episodeNumber')
        if not (season_num and episode_num):
            continue
        dummy_path = place_dummy_file("tv", series_title, series_year, tvdb_id,
                                       settings.TV_LIBRARY_FOLDER,
                                       season_number=season_num,
                                       episode_range=(episode_num, episode_num),
                                       episode_id=ep.get("id"))
        logger.info(f"Created dummy file for {series_title} S{season_num}E{episode_num} at {dummy_path}",
                    extra={'emoji_type': 'dummy'})
        series_folder = "/".join(dummy_path.split(os.sep)[:-2])
        unique_folders.add(series_folder)
        schedule_episode_request_update(series_title, season_num, episode_num, tvdb_id, delay=10, retries=5)
    for folder in unique_folders:
        # Refresh Plex folder
        r = requests.get(build_plex_url(f"library/sections/{settings.PLEX_TV_SECTION_ID}/refresh"),
                         headers={'X-Plex-Token': settings.PLEX_TOKEN})
        r.raise_for_status()
    return JSONResponse({"status": "success", "message": "SeriesAdd processed"})

def handle_episodefiledelete(data: dict, is_4k: bool = False):
    # Similar to seriesadd: recreate dummy for episode deletion.
    series = data.get('series', {})
    episodes = data.get('episodes', [])
    series_title = series.get('title', 'Unknown Series')
    series_year = series.get('year')
    tvdb_id = series.get('tvdbId')
    for ep in episodes:
        season_num = ep.get('seasonNumber')
        episode_num = ep.get('episodeNumber')
        if not (season_num and episode_num):
            # Try to extract season and episode from file field if missing
            file_field = data.get('file', '')
            m = re.search(r'[sS](\d{1,2})[eE](\d{1,2})', file_field)
            if m:
                season_num, episode_num = map(int, m.groups())
            else:
                logger.info("Cannot determine season/episode from data", extra={'emoji_type': 'warning'})
                continue
        dummy_path = place_dummy_file("tv", series_title, series_year, tvdb_id,
                                      settings.TV_LIBRARY_FOLDER,
                                      season_number=season_num,
                                      episode_range=(episode_num, episode_num),
                                      episode_id=ep.get("id"))
        logger.info(f"Re-created dummy file for {series_title} S{season_num}E{episode_num} at {dummy_path}",
                    extra={'emoji_type': 'dummy'})
        refresh_url = build_plex_url(f"library/sections/{settings.PLEX_TV_SECTION_ID}/refresh")
        r = requests.get(refresh_url, headers={'X-Plex-Token': settings.PLEX_TOKEN})
        r.raise_for_status()
        schedule_episode_request_update(series_title, season_num, episode_num, tvdb_id, delay=10, retries=5)
    return JSONResponse({"status": "success", "message": "EpisodeFileDelete processed"})

def handle_moviefiledelete(data: dict):
    if 'movie' in data:
        movie = data.get('movie', {})
        tmdb_id = movie.get('tmdbId') or data.get('remoteMovie', {}).get('tmdbId')
        if not tmdb_id:
            logger.error("Missing TMDB ID for movie file delete", extra={'emoji_type': 'error'})
            return JSONResponse({"status": "error"}, status_code=400)
        title = movie.get('title', 'Unknown Movie')
        year = movie.get('year')
        expected_dummy = os.path.join(settings.MOVIE_LIBRARY_FOLDER,
                                      f"{sanitize_filename(title)}{' ('+str(year)+')' if year else ''} {{tmdb-{tmdb_id}}}",
                                      f"{sanitize_filename(title)}{' ('+str(year)+')' if year else ''} (dummy).mp4")
        if not os.path.exists(expected_dummy):
            dummy_path = place_dummy_file("movie", title, year, tmdb_id, settings.MOVIE_LIBRARY_FOLDER)
            logger.info(f"Created dummy file for movie '{title}' at {dummy_path}", extra={'emoji_type': 'dummy'})
            folder = os.path.dirname(dummy_path)
            refresh_url = build_plex_url(f"library/sections/{settings.PLEX_MOVIE_SECTION_ID}/refresh")
            r = requests.get(refresh_url, headers={'X-Plex-Token': settings.PLEX_TOKEN})
            r.raise_for_status()
            schedule_movie_request_update(title, tmdb_id, delay=10, retries=5)
        else:
            logger.info(f"Dummy file already exists for movie '{title}'", extra={'emoji_type': 'info'})
    return JSONResponse({"status": "success", "message": "MovieFileDelete processed"})

def handle_movie_delete(data: dict):
    if 'movie' in data:
        movie = data.get('movie', {})
        tmdb_id = movie.get('tmdbId') or data.get('remoteMovie', {}).get('tmdbId')
        if not tmdb_id:
            logger.error("Missing TMDB ID for movie delete", extra={'emoji_type': 'error'})
            return JSONResponse({"status": "error"}, status_code=400)
        dummy_path = os.path.join(settings.MOVIE_LIBRARY_FOLDER,
                                  f"{sanitize_filename(movie.get('title', ''))}{' ('+str(movie.get('year'))+')' if movie.get('year') else ''} {{tmdb-{tmdb_id}}}",
                                  f"{sanitize_filename(movie.get('title', ''))}{' ('+str(movie.get('year'))+')' if movie.get('year') else ''} (dummy).mp4")
        if os.path.exists(dummy_path):
            os.remove(dummy_path)
            logger.info(f"Deleted dummy file for movie {movie.get('title')}", extra={'emoji_type': 'delete'})
        else:
            logger.info(f"No dummy file exists for movie {movie.get('title')}", extra={'emoji_type': 'info'})
        folder = os.path.join(settings.MOVIE_LIBRARY_FOLDER,
                              f"{sanitize_filename(movie.get('title', ''))}{' ('+str(movie.get('year'))+')' if movie.get('year') else ''} {{tmdb-{tmdb_id}}}")
        refresh_url = build_plex_url(f"library/sections/{settings.PLEX_MOVIE_SECTION_ID}/refresh")
        r = requests.get(refresh_url, headers={'X-Plex-Token': settings.PLEX_TOKEN})
        r.raise_for_status()
    return JSONResponse({"status": "success", "message": "MovieDelete processed"})

def handle_movieadd(data: dict):
    if 'movie' in data:
        movie = data.get('movie', {})
        tmdb_id = movie.get('tmdbId') or data.get('remoteMovie', {}).get('tmdbId')
        if not tmdb_id:
            logger.error("Missing TMDB ID for movie add", extra={'emoji_type': 'error'})
            return JSONResponse({"status": "error"}, status_code=400)
        title = movie.get('title', 'Unknown Movie')
        year = movie.get('year', '')
        dummy_path = place_dummy_file("movie", title, year, tmdb_id, settings.MOVIE_LIBRARY_FOLDER)
        logger.info(f"Created dummy file for movie '{title}' at {dummy_path}", extra={'emoji_type': 'dummy'})
        refresh_url = build_plex_url(f"library/sections/{settings.PLEX_MOVIE_SECTION_ID}/refresh")
        r = requests.get(refresh_url, headers={'X-Plex-Token': settings.PLEX_TOKEN})
        r.raise_for_status()
        schedule_movie_request_update(title, tmdb_id, delay=10, retries=5)
    return JSONResponse({"status": "success", "message": "MovieAdd processed"})

def handle_seriesdelete(data: dict):
    if 'series' in data:
        series = data.get('series', {})
        series_folder = os.path.join(settings.TV_LIBRARY_FOLDER,
                                     f"{sanitize_filename(series.get('title',''))}{' ('+str(series.get('year'))+')' if series.get('year') else ''} {{tvdb-{series.get('tvdbId')}}}")
        if os.path.exists(series_folder):
            import shutil
            shutil.rmtree(series_folder)
            logger.info(f"Deleted series folder for {series.get('title')}", extra={'emoji_type': 'delete'})
        refresh_url = build_plex_url(f"library/sections/{settings.PLEX_TV_SECTION_ID}/refresh")
        r = requests.get(refresh_url, headers={'X-Plex-Token': settings.PLEX_TOKEN})
        r.raise_for_status()
    return JSONResponse({"status": "success", "message": "SeriesDelete processed"})

def handle_playback(data: dict):
    try:
        media = data.get("media", {})
        file_path = media.get("file_info", {}).get("path", "")
        is_4k = is_4k_request(file_path)
        title = media.get("title", "Unknown Title")
        rating_key = media.get("ids", {}).get("plex")

        if media.get("type") == "movie":
            tmdb_id = media.get("ids", {}).get("tmdb")
            # If the payload contains a placeholder, attempt to extract the actual TMDB ID from file_info.path
            if (tmdb_id == "{tmdb_id}"):
                file_path = media.get("file_info", {}).get("path", "")
                m = re.search(r"\{tmdb-(\d+)\}", file_path)
                if m:
                    tmdb_id = m.group(1)
                    logger.info(f"Extracted numeric TMDB ID: {tmdb_id} from file path", extra={'emoji_type': 'info'})
                else:
                    logger.error("TMDB ID not found in file path", extra={'emoji_type': 'error'})
                    return JSONResponse({"status": "error", "message": "Missing valid TMDB ID"}, status_code=400)
            base_title = strip_movie_status(sanitize_filename(title))
            logger.info(f"Processing movie playback for {base_title}", extra={'emoji_type': 'processing'})
            success = search_in_radarr(tmdb_id, rating_key, is_4k=is_4k)
            if not success:
                return JSONResponse({"status": "error", "message": "Search failed"}, status_code=500)
            
            check_media_has_file(
                media_id=tmdb_id,
                base_title=base_title,
                rating_key=rating_key,
                media_type='movie',
                attempts=0,
                start_time=time.time(),
                is_4k=is_4k
            )
            return JSONResponse({"status": "success"})
            
        elif media.get("type") == "episode":
            # Extract episode_id from file path if available
            file_path = media.get("file_info", {}).get("path", "")
            episode_id = None
            id_match = re.search(r"\[ID:(\d+)\]", file_path)
            if id_match:
                episode_id = id_match.group(1)
                logger.info(f"Found episode ID: {episode_id} in filename", extra={'emoji_type': 'info'})
            else:
                logger.error("Episode ID not found in filename", extra={'emoji_type': 'error'})
                return JSONResponse({"status": "error", "message": "Episode ID not found"}, status_code=400)

            # Get episode details for monitoring
            season_number = media.get("season_num")
            episode_number = media.get("episode_num")
            tvdb_id = media.get("ids", {}).get("tvdb")
            if not (season_number and episode_number and tvdb_id):
                logger.error("Missing season, episode, or TVDB ID", extra={'emoji_type': 'error'})
                return JSONResponse({"status": "error", "message": "Missing required episode data"}, status_code=400)

            # Clean the title
            base_title = extract_episode_title(media.get("episode_title", title))
            logger.info(f"Processing episode playback for {base_title}", extra={'emoji_type': 'processing'})

            # Get series ID from Sonarr (no search, just lookup)
            series_id = search_in_sonarr(tvdb_id, rating_key, episode_mode=True, is_4k=is_4k)
            if not series_id:
                return JSONResponse({"status": "error", "message": "Failed to get series ID"}, status_code=400)

            # Trigger appropriate search based on play mode
            if settings.TV_PLAY_MODE == "episode":
                search_success = trigger_sonarr_search(series_id, episode_ids=episode_id, series_title=base_title, is_4k=is_4k)
            elif settings.TV_PLAY_MODE == "season":
                search_success = trigger_sonarr_search(series_id, season_number=season_number, series_title=base_title, is_4k=is_4k)
            else:  # series mode
                search_success = trigger_sonarr_search(series_id, series_title=base_title, is_4k=is_4k)

            if not search_success:
                return JSONResponse({"status": "error", "message": "Search failed"}, status_code=500)
            
            # Start monitoring only if search was successful
            check_media_has_file(
                media_id=tvdb_id,
                base_title=base_title,
                rating_key=rating_key,
                media_type=settings.TV_PLAY_MODE,
                attempts=0,
                season_number=season_number,
                episode_number=episode_number,
                start_time=time.time(),
                is_4k=is_4k
            )
            return JSONResponse({"status": "success"})

        else:
            logger.warning(f"Unsupported media type {media.get('type')}", extra={'emoji_type': 'warning'})
            return JSONResponse({"status": "error", "message": "Unsupported media type"}, status_code=400)

    except Exception as e:
        logger.error(f"Playback handling error: {e}", extra={'emoji_type': 'error'})
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

def handle_arrs_import(data: dict):
    # Fallback for unhandled events from other ARR providers
    logger.info(f"Handling ARR import event: {data}", extra={'emoji_type': 'webhook'})
    return JSONResponse({"status": "success", "message": "Import event processed"})

# ...end of handlers...
