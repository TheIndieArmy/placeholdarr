import os, re, threading, time, shutil, requests
from fastapi.responses import JSONResponse
from core.config import settings
from core.logger import logger
from services.plex_client import plex, build_plex_url
from services.integrations import (
    place_dummy_file, delete_dummy_files, schedule_episode_request_update,
    schedule_movie_request_update, check_media_has_file,
    search_in_radarr, search_in_sonarr, trigger_sonarr_search, monitor_episodes, 
    mark_series_monitored, get_episodes_for_lookahead, check_tv_has_file
)
from services.utils import (
    strip_movie_status, sanitize_filename, extract_episode_title, 
    is_4k_request, strip_status_markers
)
from urllib.parse import quote

def handle_webhook(data: dict, source_port: int = None):
    """Handle webhook with quality awareness"""
    source = data.get("instanceName", "Tautulli")
    
    # Log incoming webhook but keep it brief
    logger.debug(f"{source} payload: {data}", extra={'emoji_type': 'debug'})
    
    # Get file path for quality detection
    file_path = (data.get('media', {}).get('file_info', {}).get('path') or 
                 data.get('movie', {}).get('folderPath') or 
                 data.get('file', ''))
    
    is_4k = is_4k_request(file_path, source_port)
    logger.debug(f"Quality determination: {'4K' if is_4k else 'Standard'}", extra={'emoji_type': 'debug'})
    
    event_type = (data.get('event') or data.get('eventType') or 'unknown').lower()
    logger.info(f"Received webhook event: {event_type}", extra={'emoji_type': 'webhook'})
    
    # Handle import events directly for cleanup
    if event_type in ['download', 'moviefileimported', 'episodefileimported']:
        return handle_import_event(data, is_4k)
    
    # Handle other events
    if event_type == 'seriesadd':
        return handle_seriesadd(data, is_4k)
    elif event_type == 'episodefiledelete':
        return handle_episodefiledelete(data, is_4k)
    elif event_type == 'moviefiledelete':
        return handle_moviefiledelete(data)
    elif event_type == 'moviedelete':
        return handle_movie_delete(data)
    elif event_type in ('movieadd', 'movieadded'):
        return handle_movieadd(data)
    elif event_type == 'seriesdelete':
        return handle_seriesdelete(data, is_4k)
    elif event_type == 'playback.start':
        return handle_playback(data)
    else:
        # Fallback for unhandled events from other ARR providers
        logger.info(f"Handling ARR import event: {data}", extra={'emoji_type': 'webhook'})
        return JSONResponse({"status": "success", "message": "Import event processed"})

def handle_import_event(data: dict, is_4k: bool = False):
    """Handle media import events and delete placeholders"""
    try:
        if 'movie' in data:
            # Movie import handling
            movie = data['movie']
            tmdb_id = movie.get('tmdbId')
            title = movie.get('title', 'Unknown Movie')
            year = movie.get('year')
            
            logger.info(f"Processing movie import cleanup for: {title}", extra={'emoji_type': 'cleanup'})
            delete_dummy_files('movie', title, year, tmdb_id, settings.MOVIE_LIBRARY_FOLDER)
            
            # Refresh Plex library
            refresh_url = build_plex_url(f"library/sections/{settings.PLEX_MOVIE_SECTION_ID}/refresh")
            requests.get(refresh_url, headers={'X-Plex-Token': settings.PLEX_TOKEN})
            
        elif 'episodes' in data and 'series' in data:
            # TV episode import handling
            series = data['series']
            episode = data['episodes'][0]  # Handle first episode in the list
            
            series_title = series.get('title', 'Unknown Series')
            tvdb_id = series.get('tvdbId')
            season_num = episode.get('seasonNumber')
            episode_num = episode.get('episodeNumber')
            episode_title = episode.get('title', 'Unknown Episode')
            
            # Format full episode identifier
            full_title = f"{series_title} - S{season_num:02d}E{episode_num:02d} - {episode_title}"
            logger.info(f"Processing episode import cleanup for: {full_title}", extra={'emoji_type': 'cleanup'})
            
            delete_dummy_files('tv', series_title, series.get('year'), tvdb_id, 
                              settings.TV_LIBRARY_FOLDER, season_number=season_num, episode_number=episode_num)
            
            # Refresh Plex library
            refresh_url = build_plex_url(f"library/sections/{settings.PLEX_TV_SECTION_ID}/refresh")
            requests.get(refresh_url, headers={'X-Plex-Token': settings.PLEX_TOKEN})
            
    except Exception as e:
        logger.error(f"Import cleanup failed: {e}", extra={'emoji_type': 'error'})
    
    return JSONResponse({"status": "success", "message": "Import cleanup processed"})

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
        episode_title = ep.get('title')
        if not (season_num and episode_num):
            continue
        dummy_path = place_dummy_file("tv", series_title, series_year, tvdb_id,
                                    settings.TV_LIBRARY_FOLDER,
                                    season_number=season_num,
                                    episode_range=(episode_num, episode_num),
                                    episode_title=episode_title)  # REMOVED episode_id
        logger.info(f"Created dummy file for {series_title} S{season_num}E{episode_num} at {dummy_path}",
                    extra={'emoji_type': 'dummy'})
        series_folder = "/".join(dummy_path.split(os.sep)[:-2])
        unique_folders.add(series_folder)
        schedule_episode_request_update(series_title, season_num, episode_num, tvdb_id, delay=10, retries=5)
    for folder in unique_folders:
        # Refresh specific Plex folder instead of entire library
        refresh_url = build_plex_url(f"library/sections/{settings.PLEX_TV_SECTION_ID}/refresh?path={quote(folder)}")
        r = requests.get(refresh_url, headers={'X-Plex-Token': settings.PLEX_TOKEN})
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

def handle_seriesdelete(data: dict, is_4k: bool = False):
    """Delete placeholder files when a series is deleted from Sonarr"""
    if 'series' in data:
        series = data.get('series', {})
        tvdb_id = series.get('tvdbId')
        title = series.get('title', 'Unknown Series')
        year = series.get('year')
        
        if tvdb_id:
            # Construct folder path for placeholders
            library_folder = settings.TV_LIBRARY_4K_FOLDER if is_4k else settings.TV_LIBRARY_FOLDER
            series_folder = os.path.join(library_folder, f"{title} ({year}) {{tvdb-{tvdb_id}}} (dummy)")
            
            # Check if folder exists
            if os.path.exists(series_folder):
                try:
                    # 1. First refresh Plex to recognize the deletion
                    refresh_url = build_plex_url(f"library/sections/{settings.PLEX_TV_SECTION_ID}/refresh?path={quote(os.path.dirname(series_folder))}")
                    r = requests.get(refresh_url, headers={'X-Plex-Token': settings.PLEX_TOKEN})
                    r.raise_for_status()
                    logger.info(f"Refreshed Plex for series folder: {series_folder}", extra={'emoji_type': 'refresh'})
                    
                    # 2. Then delete all files and subfolder recursively
                    shutil.rmtree(series_folder)
                    logger.info(f"Deleted placeholder folder: {series_folder}", extra={'emoji_type': 'delete'})
                except Exception as e:
                    logger.error(f"Error deleting folder {series_folder}: {str(e)}", extra={'emoji_type': 'error'})
            else:
                logger.warning(f"Series folder not found: {series_folder}", extra={'emoji_type': 'warning'})
                # Still refresh Plex in case the folder was already deleted
                refresh_url = build_plex_url(f"library/sections/{settings.PLEX_TV_SECTION_ID}/refresh?path={quote(os.path.dirname(series_folder))}")
                r = requests.get(refresh_url, headers={'X-Plex-Token': settings.PLEX_TOKEN})
                r.raise_for_status()
        else:
            # Fall back to full library refresh if no TVDB ID
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
            # Get episode details directly from the webhook payload
            series_title = media.get("series_title", "")
            episode_title = media.get("episode_title", "")
            
            # Handle variable substitution issues from Tautulli
            if series_title.startswith('{') and series_title.endswith('}'): 
                # Extract series title from the main title or path
                path_match = re.search(r'/([^/]+) \(\d{4}\) \{tvdb-', file_path)
                if path_match:
                    series_title = path_match.group(1)
                else:
                    # Try extracting from the main title
                    main_title_parts = title.split(' - ')
                    if len(main_title_parts) > 0:
                        series_title = main_title_parts[0]
            
            # Get episode details and convert to integers
            try:
                season_number = int(media.get("season_num", 0))
                episode_number = int(media.get("episode_num", 0))
            except (ValueError, TypeError):
                logger.error("Invalid season or episode number format", extra={'emoji_type': 'error'})
                return JSONResponse({"status": "error", "message": "Invalid season/episode format"}, status_code=400)
                
            tvdb_id = media.get("ids", {}).get("tvdb")
            
            # Make sure we have valid TVDB ID
            if not tvdb_id or tvdb_id == "{tvdb_id}":
                # Try to extract from file path
                path_match = re.search(r'\{tvdb-(\d+)\}', file_path)
                if path_match:
                    tvdb_id = path_match.group(1)
                    logger.info(f"Extracted TVDB ID from path: {tvdb_id}", extra={'emoji_type': 'info'})
                else:
                    logger.error("Valid TVDB ID not found", extra={'emoji_type': 'error'})
                    return JSONResponse({"status": "error", "message": "Missing valid TVDB ID"}, status_code=400)
            
            # Build full episode title for monitoring and logging
            if episode_title.startswith('{') and episode_title.endswith('}'): 
                # Extract from main title if possible
                main_parts = title.split(' - ')
                if len(main_parts) >= 3:  # Format: Series - SxxExx - Episode
                    episode_title = main_parts[2].split(' [')[0]  # Remove status markers
                else:
                    episode_title = f"Episode {episode_number}"
                    
            # Format full episode identifier for logging and tracking
            full_title = f"{series_title} - S{season_number:02d}E{episode_number:02d} - {episode_title}"
            logger.info(f"Processing episode playback for {full_title}", extra={'emoji_type': 'processing'})

            # Search in Sonarr using TVDB ID and season/episode numbers
            series_id = search_in_sonarr(tvdb_id=tvdb_id, rating_key=rating_key, 
                          season_number=season_number, episode_number=episode_number,
                          episode_mode=True, is_4k=is_4k)
            if not series_id:
                return JSONResponse({"status": "error", "message": "Failed to get series ID"}, status_code=400)

            # Get episodes for lookahead (Chronicle-style)
            lookahead = getattr(settings, 'EPISODES_LOOKAHEAD', 5)
            episodes_to_monitor, reached_end = get_episodes_for_lookahead(
                series_id, 
                season_number, 
                episode_number, 
                lookahead=lookahead
            )
            
            if not episodes_to_monitor:
                logger.warning("No episodes found to monitor", extra={'emoji_type': 'warning'})
                return JSONResponse({"status": "warning", "message": "No episodes available"})
            
            # Extract episode IDs for batch monitoring
            episode_ids = [ep['id'] for ep in episodes_to_monitor]
            
            # Mark the selected episodes as monitored and trigger search
            if episode_ids:
                # First monitor all episodes in batch
                monitor_episodes(series_id, episode_ids, monitor=True)
                
                # If we've reached the end, also mark entire series
                if reached_end:
                    mark_series_monitored(series_id)
                
                # Then trigger search for all of them
                search_success = trigger_sonarr_search(
                    series_id=series_id, 
                    episode_ids=episode_ids, 
                    series_title=full_title,
                    is_4k=is_4k
                )
                
                if search_success:
                    # Track each episode individually for status updates
                    for episode in episodes_to_monitor:
                        check_tv_has_file(
                            tvdb_id, 
                            series_title, 
                            rating_key, 
                            season_number=episode['seasonNumber'], 
                            episode_number=episode['episodeNumber']
                        )
                    return JSONResponse({"status": "success", "message": f"Search triggered for {len(episode_ids)} episodes"})
                else:
                    return JSONResponse({"status": "error", "message": "Failed to trigger search"}, status_code=500)
            else:
                return JSONResponse({"status": "warning", "message": "No episodes to search for"})

        else:
            logger.warning(f"Unsupported media type {media.get('type')}", extra={'emoji_type': 'warning'})
            return JSONResponse({"status": "error", "message": "Unsupported media type"}, status_code=400)

    except Exception as e:
        logger.error(f"Playback handling error: {e}", extra={'emoji_type': 'error'})
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
import os, re, threading, time, shutil, requests
