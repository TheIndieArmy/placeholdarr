import os, re, threading, time, shutil, requests
from fastapi.responses import JSONResponse
from core.config import settings
from core.logger import logger
from services.plex_client import plex, build_plex_url, refresh_plex_item
from services.jellyfin_client import build_jellyfin_url, refresh_jellyfin_item, get_jellyfin_file_path
from services.integrations import (
    place_dummy_file, delete_dummy_files, schedule_episode_request_update,
    schedule_movie_request_update, 
    search_in_radarr, search_in_sonarr, trigger_sonarr_search, monitor_episodes, 
    mark_series_monitored, get_episodes_for_lookahead,
    monitor_season, sanitize_filename
)
from services.queue_monitor import add_to_monitor
from services.utils import (
    strip_movie_status, sanitize_filename, extract_episode_title, 
    is_4k_request, strip_status_markers
)
from urllib.parse import quote
from services.queue_monitor import handle_download_webhook

# Series-based tracking for playback suppression
RECENT_SERIES_PLAYBACKS = {}  # Format: {tvdb_id: timestamp}

def should_process_playback(tvdb_id):
    """
    Determine if we should process this series playback or suppress it
    Returns True if we should process, False if we should suppress
    """
    # If cooldown is disabled (0), always process
    if getattr(settings, 'PLAYBACK_COOLDOWN', 30) <= 0:
        return True
        
    now = time.time()
    series_key = str(tvdb_id)
    
    # Check if this series was recently processed
    if series_key in RECENT_SERIES_PLAYBACKS:
        last_time = RECENT_SERIES_PLAYBACKS[series_key]
        if now - last_time < settings.PLAYBACK_COOLDOWN:
            # Within cooldown period - suppress this playback
            logger.info(f"Suppressing duplicate playback for series {tvdb_id} (within {settings.PLAYBACK_COOLDOWN}s cooldown)", 
                      extra={'emoji_type': 'skip'})
            return False
    
    # Update timestamp for this series
    RECENT_SERIES_PLAYBACKS[series_key] = now
    
    # Clean up old entries
    for k in list(RECENT_SERIES_PLAYBACKS.keys()):
        if now - RECENT_SERIES_PLAYBACKS[k] > settings.PLAYBACK_COOLDOWN:
            del RECENT_SERIES_PLAYBACKS[k]
    
    return True

def handle_webhook(data: dict, source_port: int = None):
    """Handle webhook with quality awareness"""
    if not data.get("instanceName") and not data.get("ServerName"):
        source = "Tautulli"
    else:
        source = data.get("ServerName") if not data.get("instanceName") else data.get("instanceName")
    
    # Log incoming webhook but keep it brief
    logger.debug(f"{source} payload: {data}", extra={'emoji_type': 'debug'})
    
    # Get file path for quality detection
    file_path = (data.get('media', {}).get('file_info', {}).get('path') or 
                 data.get('movie', {}).get('folderPath') or 
                 data.get('file', '') or
                 get_jellyfin_file_path(data.get("ItemId"), data.get("UserId")))
    
    is_4k = is_4k_request(file_path, source_port)
    logger.debug(f"Quality determination: {'4K' if is_4k else 'Standard'}", extra={'emoji_type': 'debug'})
    
    event_type = (data.get('event') or data.get('eventType') or data.get('NotificationType') or 'unknown').lower()
    logger.info(f"Received webhook event: {event_type}", extra={'emoji_type': 'webhook'})
    
    # Handle import events directly for cleanup
    if event_type in ['download', 'moviefileimported', 'episodefileimported']:
        # Add this line to update the queue monitoring
        handle_download_webhook(data)
        # Then continue with the normal handling
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
    elif event_type in ['playback.start', 'playbackstart']:
        return handle_playback(data)
    else:
        # Fallback for unhandled events from other ARR providers
        logger.info(f"Handling ARR import event: {data}", extra={'emoji_type': 'webhook'})
        return JSONResponse({"status": "success", "message": "Import event processed"})

def handle_import_event(data: dict, is_4k: bool = False):
    """Handle media import events and clean up placeholders"""
    try:
        if 'movie' in data:
            # Movie import handling
            movie = data['movie']
            tmdb_id = movie.get('tmdbId')
            title = movie.get('title', 'Unknown Movie')
            year = movie.get('year')
            movie_path = data.get("movieFile", {}).get("path")

            logger.info(f"Processing movie import cleanup for: {title}", extra={'emoji_type': 'cleanup'})

            # Update Plex/Jellyfin title to "Available" (remove status markers)
            from services.integrations import update_title_status
            update_title_status(
                media_type='movie',
                media_id=tmdb_id,
                title=title,
                status=None,     # None → strip markers
                year=year
            )

            # Clean up placeholder files
            folder_path = movie.get('folderPath')
            arr_root_folder = movie.get('rootFolderPath') or getattr(settings, 'RADARR_ROOT_FOLDER', None) or None
            delete_dummy_files('movie', title, year, tmdb_id, None, folder_path=folder_path, arr_root_folder=arr_root_folder)

            dummy_folder = os.path.join(settings.MOVIE_LIBRARY_FOLDER, 
            f"{sanitize_filename(title)}{' ('+str(year)+')' if year else ''} {{tmdb-{tmdb_id}}}")

            # --- NEW: Always refresh parent folder ---
            if movie_path:
                parent_folder = os.path.dirname(movie_path)
                if settings.plex_enabled:
                    refresh_plex_item(parent_folder)
                if settings.jellyfin_enabled:
                    refresh_jellyfin_item(parent_folder)

            # Also refresh the dummy folder (legacy/placeholder logic)
            if settings.plex_enabled:
                refresh_plex_item(dummy_folder)
            if settings.jellyfin_enabled:
                refresh_jellyfin_item(dummy_folder, "Deleted")

        elif 'episodes' in data and 'series' in data:
            # TV episode import handling
            series = data['series']
            episode = data['episodes'][0]  # Handle first episode in the list

            series_title = series.get('title', 'Unknown Series')
            tvdb_id = series.get('tvdbId')
            season_num = episode.get('seasonNumber')
            episode_num = episode.get('episodeNumber')
            episode_title = episode.get('title', 'Unknown Episode')
            episode_path = data.get("episodeFile", {}).get("path")

            full_title = f"{series_title} - S{season_num:02d}E{episode_num:02d} - {episode_title}"
            logger.info(f"Processing episode import cleanup for: {full_title}", extra={'emoji_type': 'cleanup'})

            # Update Plex/Jellyfin title to "Available" (remove status markers)
            from services.integrations import update_title_status
            update_title_status(
                media_type='tv',
                media_id=tvdb_id,
                title=series_title,
                status=None,  # None = remove status markers
                season=season_num,
                episode=episode_num
            )

            # Clean up placeholder files
            folder_path = series.get('folderPath')
            arr_root_folder = series.get('rootFolderPath') or getattr(settings, 'SONARR_ROOT_FOLDER', None) or None
            delete_dummy_files('tv', series_title, series.get('year'), tvdb_id, None, season_number=season_num, episode_number=episode_num, folder_path=folder_path, arr_root_folder=arr_root_folder)

            # --- NEW: Always refresh parent folder ---
            if episode_path:
                parent_folder = os.path.dirname(episode_path)
                if settings.plex_enabled:
                    refresh_plex_item(parent_folder)
                if settings.jellyfin_enabled:
                    refresh_jellyfin_item(parent_folder)

            # Also refresh the dummy folder (legacy/placeholder logic)
            folder_path = os.path.join(
                settings.TV_LIBRARY_FOLDER,
                f"{sanitize_filename(series_title)}"
                f"{' ('+str(series.get('year'))+')' if series.get('year') else ''}"
                f" {{tvdb-{tvdb_id}}}"
            )
            if settings.plex_enabled:
                refresh_plex_item(folder_path)
            if settings.jellyfin_enabled:
                refresh_jellyfin_item(folder_path, "Deleted")

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
    def delayed_placeholders():
        delay_seconds = 3  # Adjust as needed
        logger.debug(f"Delaying {delay_seconds}s before checking hasFile for series '{series_title}'", extra={'emoji_type': 'debug'})
        time.sleep(delay_seconds)
        for ep in episodes:
            season_num = ep.get('seasonNumber')
            episode_num = ep.get('episodeNumber')
            episode_title = ep.get('title')
            if not (season_num and episode_num):
                continue
            from services.queue_monitor import check_episode_has_file
            if check_episode_has_file(tvdb_id, season_num, episode_num, is_4k):
                logger.info(f"Skipping placeholder for {series_title} S{season_num}E{episode_num} (real file exists)", extra={'emoji_type': 'skip'})
                continue
            # Use folderPath from webhook if available
            folder_path = data.get('series', {}).get('folderPath')
            arr_root_folder = data.get('series', {}).get('rootFolderPath') or getattr(settings, 'SONARR_ROOT_FOLDER', None) or None
            dummy_path = place_dummy_file("tv", series_title, series_year, tvdb_id,
                                        None,
                                        season_number=season_num,
                                        episode_range=(episode_num, episode_num),
                                        episode_title=episode_title,
                                        folder_path=folder_path,
                                        arr_root_folder=arr_root_folder)
            if dummy_path:
                logger.info(f"Created placeholder file for {series_title} S{season_num}E{episode_num}", extra={'emoji_type': 'create'})
                if settings.plex_enabled:
                    refresh_plex_item(os.path.dirname(dummy_path))
                if settings.jellyfin_enabled:
                    refresh_jellyfin_item(os.path.dirname(dummy_path))
                schedule_episode_request_update(series_title, season_num, episode_num, tvdb_id, delay=10, retries=5)
            else:
                logger.error(f"Failed to create dummy file for {series_title} S{season_num}E{episode_num}; skipping refresh.", extra={'emoji_type': 'error'})
        for folder in unique_folders:
            if settings.plex_enabled:
                refresh_plex_item(folder)
            if settings.jellyfin_enabled:
                refresh_jellyfin_item(folder)

        logger.info(f"Created {len(episodes)} placeholder files for '{series_title}'", extra={'emoji_type': 'create'})

    threading.Thread(target=delayed_placeholders, daemon=True).start()
    return JSONResponse({"status": "success", "message": "SeriesAdd scheduled"})

def handle_episodefiledelete(data: dict, is_4k: bool = False):
    # Similar to seriesadd: recreate dummy for episode deletion.
    series = data.get('series', {})
    episodes = data.get('episodes', [])
    series_title = series.get('title', 'Unknown Series')
    series_year = series.get('year')
    tvdb_id = series.get('tvdbId')
    episode_file_path = data.get("episodeFile", {}).get("path")
    for ep in episodes:
        season_num = ep.get('seasonNumber')
        episode_num = ep.get('episodeNumber')
        episode_title = ep.get('title')  # Make sure we extract episode title
        
        if not (season_num and episode_num):
            # Try to extract season and episode from file field if missing
            file_field = data.get('file', '')
            m = re.search(r'[sS](\d{1,2})[eE](\d{1,2})', file_field)
            if m:
                season_num, episode_num = map(int, m.groups())
            else:
                logger.info("Cannot determine season/episode from data", extra={'emoji_type': 'warning'})
                continue
                
        folder_path = series.get('folderPath')
        arr_root_folder = series.get('rootFolderPath') or getattr(settings, 'SONARR_ROOT_FOLDER', None) or None
        dummy_path = place_dummy_file("tv", series_title, series_year, tvdb_id,
                                      None,
                                      season_number=season_num,
                                      episode_range=(episode_num, episode_num),
                                      episode_title=episode_title,
                                      folder_path=folder_path,
                                      arr_root_folder=arr_root_folder)
        if dummy_path:
            if settings.plex_enabled:
                refresh_plex_item(os.path.dirname(dummy_path))
            if settings.jellyfin_enabled:
                refresh_jellyfin_item(os.path.dirname(dummy_path), "Changed")
        else:
            logger.error("Failed to create dummy file; skipping refresh.", extra={'emoji_type': 'error'})
        # --- NEW: Always refresh parent folder ---
        if episode_file_path:
            parent_folder = os.path.dirname(episode_file_path)
            if settings.plex_enabled:
                refresh_plex_item(parent_folder)
            if settings.jellyfin_enabled:
                refresh_jellyfin_item(parent_folder)
        schedule_episode_request_update(series_title, season_num, episode_num, tvdb_id, delay=10, retries=5)
    logger.info(f"Re-created {len(episodes)} placeholder files for '{series_title}'", extra={'emoji_type': 'create'})
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
        folder_path = movie.get('folderPath')
        arr_root_folder = movie.get('rootFolderPath') or getattr(settings, 'RADARR_ROOT_FOLDER', None) or None
        library_path = getattr(settings, 'MOVIE_LIBRARY_FOLDER', None)
        # Use the unified dummy deletion logic
        from services.integrations import delete_dummy_files
        delete_dummy_files('movie', title, year, tmdb_id, library_path=library_path, folder_path=folder_path, arr_root_folder=arr_root_folder)
        # Optionally refresh Plex/Jellyfin at the dummy folder location
        if folder_path and library_path:
            import os
            dummy_folder = os.path.join(library_path, os.path.basename(folder_path))
            if settings.plex_enabled:
                refresh_plex_item(dummy_folder)
            if settings.jellyfin_enabled:
                refresh_jellyfin_item(dummy_folder, "Deleted")
        return JSONResponse({"status": "success", "message": "MovieDelete processed"})
    return JSONResponse({"status": "success", "message": "MovieDelete processed"})

def handle_movie_delete(data: dict):
    if 'movie' in data:
        movie = data.get('movie', {})
        tmdb_id = movie.get('tmdbId') or data.get('remoteMovie', {}).get('tmdbId')
        if not tmdb_id:
            logger.error("Missing TMDB ID for movie delete", extra={'emoji_type': 'error'})
            return JSONResponse({"status": "error"}, status_code=400)
        title = movie.get('title', 'Unknown Movie')
        year = movie.get('year')
        folder_path = movie.get('folderPath')
        arr_root_folder = movie.get('rootFolderPath') or getattr(settings, 'RADARR_ROOT_FOLDER', None) or None
        library_path = getattr(settings, 'MOVIE_LIBRARY_FOLDER', None)
        # Use the unified dummy deletion logic
        from services.integrations import delete_dummy_files
        delete_dummy_files('movie', title, year, tmdb_id, library_path=library_path, folder_path=folder_path, arr_root_folder=arr_root_folder)
        # Optionally refresh Plex/Jellyfin at the dummy folder location
        if folder_path and library_path:
            import os
            dummy_folder = os.path.join(library_path, os.path.basename(folder_path))
            if settings.plex_enabled:
                refresh_plex_item(dummy_folder)
            if settings.jellyfin_enabled:
                refresh_jellyfin_item(dummy_folder, "Deleted")
        return JSONResponse({"status": "success", "message": "MovieDelete processed"})
    return JSONResponse({"status": "success", "message": "MovieDelete processed"})

def handle_movieadd(data: dict):
    if 'movie' in data:
        movie = data.get('movie', {})
        movie_path = data.get("movie", {}).get("folderPath")
        tmdb_id = movie.get('tmdbId') or data.get('remoteMovie', {}).get('tmdbId')
        if not tmdb_id:
            logger.error("Missing TMDB ID for movie add", extra={'emoji_type': 'error'})
            return JSONResponse({"status": "error"}, status_code=400)
        title = movie.get('title', 'Unknown Movie')
        year = movie.get('year', '')
        from services.queue_monitor import check_movie_has_file
        radarr_id = movie.get('id')

        def delayed_placeholder():
            delay_seconds = 3  # Adjust as needed
            logger.debug(f"Delaying {delay_seconds}s before checking hasFile for movie '{title}'", extra={'emoji_type': 'debug'})
            time.sleep(delay_seconds)
            has_file = False
            if radarr_id and check_movie_has_file(radarr_id):
                has_file = True
            if has_file:
                logger.info(f"Skipping placeholder for movie '{title}' (real file exists)", extra={'emoji_type': 'skip'})
                return
            # Use folderPath from webhook if available
            folder_path = data.get('movie', {}).get('folderPath')
            arr_root_folder = data.get('movie', {}).get('rootFolderPath') or getattr(settings, 'RADARR_ROOT_FOLDER', None) or None
            dummy_path = place_dummy_file("movie", title, year, tmdb_id, None, folder_path=folder_path, arr_root_folder=arr_root_folder)
            if dummy_path:
                logger.info(f"Created placeholder file for movie '{title}'", extra={'emoji_type': 'create'})
                if settings.plex_enabled:
                    refresh_plex_item(os.path.dirname(dummy_path))
                if settings.jellyfin_enabled:
                    refresh_jellyfin_item(os.path.dirname(dummy_path))
                schedule_movie_request_update
            else:
                logger.error("Failed to create dummy file; skipping refresh.", extra={'emoji_type': 'error'})

        threading.Thread(target=delayed_placeholder, daemon=True).start()
        return JSONResponse({"status": "success", "message": "MovieAdd scheduled"})

    return JSONResponse({"status": "success", "message": "MovieAdd processed"})

def handle_seriesdelete(data: dict, is_4k: bool = False):
    """Delete placeholder files when a series is deleted from Sonarr using universal path logic"""
    if 'series' in data:
        series = data.get('series', {})
        tvdb_id = series.get('tvdbId')
        title = series.get('title', 'Unknown Series')
        year = series.get('year')
        folder_path = series.get('folderPath')
        arr_root_folder = series.get('rootFolderPath') or getattr(settings, 'SONARR_ROOT_FOLDER', None) or None
        library_folder = getattr(settings, 'TV_LIBRARY_FOLDER', None)
        # Use the unified dummy deletion logic
        from services.integrations import delete_dummy_files
        delete_dummy_files('tv', title, year, tvdb_id, library_path=library_folder, folder_path=folder_path, arr_root_folder=arr_root_folder)
        # Optionally refresh Plex/Jellyfin at the dummy folder location
        if folder_path and library_folder:
            import os
            dummy_folder = os.path.join(library_folder, os.path.basename(folder_path))
            if settings.plex_enabled:
                refresh_plex_item(dummy_folder)
            if settings.jellyfin_enabled:
                refresh_jellyfin_item(dummy_folder, "Deleted")
        return JSONResponse({"status": "success", "message": "SeriesDelete processed"})
    return JSONResponse({"status": "success", "message": "SeriesDelete processed"})

def handle_series_delete(payload):
    """Handle 'seriesdelete' event from Sonarr using unified dummy deletion logic"""
    if 'series' in payload:
        series = payload.get('series', {})
        tvdb_id = series.get('tvdbId')
        title = series.get('title', 'Unknown Series')
        year = series.get('year')
        folder_path = series.get('folderPath')
        arr_root_folder = series.get('rootFolderPath') or getattr(settings, 'SONARR_ROOT_FOLDER', None) or None
        library_folder = getattr(settings, 'TV_LIBRARY_FOLDER', None)
        # Use the unified dummy deletion logic
        from services.integrations import delete_dummy_files
        delete_dummy_files('tv', title, year, tvdb_id, library_path=library_folder, folder_path=folder_path, arr_root_folder=arr_root_folder)
        # Optionally refresh Plex/Jellyfin at the dummy folder location
        if folder_path and library_folder:
            import os
            dummy_folder = os.path.join(library_folder, os.path.basename(folder_path))
            if settings.plex_enabled:
                refresh_plex_item(dummy_folder)
            if settings.jellyfin_enabled:
                refresh_jellyfin_item(dummy_folder, "Deleted")
        return JSONResponse({"status": "success", "message": "SeriesDelete processed"})
    return JSONResponse({"status": "success", "message": "SeriesDelete processed"})

# In handle_playback, we need to keep the existing structure but integrate with queue monitoring

def handle_playback(data: dict):
    try:
        media = data.get("media", {}) or {}
        notification = data.get("NotificationType")
        # Determine file path source
        if notification:
            # Jellyfin payload
            item_id = data.get("ItemId")
            user_id = data.get("UserId")
            file_path = get_jellyfin_file_path(item_id, user_id) or ""
        else:
            # Tautulli payload
            file_path = media.get("file_info", {}).get("path", "")
        is_4k = is_4k_request(file_path)
        title = media.get("title", data.get("Name", "Unknown Title"))
        rating_key = media.get("ids", {}).get("plex") or data.get("ItemId")
        media_type = media.get("type") or data.get("ItemType", "").lower()

        # Debug log the file path 
        logger.debug(f"Processing playback for file path: {file_path}", extra={'emoji_type': 'debug'})

        # Check if file is in one of our placeholder library folders
        # Build placeholder folders list
        placeholder_folders = [settings.MOVIE_LIBRARY_FOLDER, settings.TV_LIBRARY_FOLDER]
        if getattr(settings, 'MOVIE_LIBRARY_4K_FOLDER', None):
            placeholder_folders.append(settings.MOVIE_LIBRARY_4K_FOLDER)
        if getattr(settings, 'TV_LIBRARY_4K_FOLDER', None):
            placeholder_folders.append(settings.TV_LIBRARY_4K_FOLDER)

        logger.debug(f"Checking path against placeholder folders: {placeholder_folders}", extra={'emoji_type': 'debug'})
        is_placeholder = any(file_path.startswith(folder) for folder in placeholder_folders if folder)

        # Debug log the placeholder check result
        logger.debug(f"Is placeholder check result: {is_placeholder}", extra={'emoji_type': 'debug'})
        
        if not is_placeholder:
            logger.info(f"Ignoring playback of real movie file: {file_path}", extra={'emoji_type': 'info'})
            return JSONResponse({"status": "ignored", "message": "Not a placeholder movie file"})
        
        if media_type == "movie":
            tmdb_id = media.get("ids", {}).get("tmdb") or data.get("Provider_tmdb")
            imdb_id = media.get("ids", {}).get("imdb") or data.get("Provider_imdb")
            year = media.get("year") or data.get("Year", "")
            
            logger.info(f"Processing movie playback for {title}", extra={'emoji_type': 'process'})
            
            radarr_id = search_in_radarr(title=title, tmdb_id=tmdb_id, imdb_id=imdb_id, 
                                       year=year, rating_key=rating_key, is_4k=is_4k)
            
            if radarr_id:
                # Movie exists in Radarr, add to our monitoring system
                add_to_monitor({
                    'media_type': 'movie',
                    'tmdb_id': tmdb_id,
                    'radarr_id': radarr_id,
                    'title': title,
                    'rating_key': rating_key,
                    'is_4k': is_4k,
                    'hasFile': False
                })
                return JSONResponse({"status": "success", "message": "Search triggered"})
            return JSONResponse({"status": "error", "message": "Failed to find/add movie"}, status_code=400)
            
        elif media_type == "episode":
            # Extract episode details from webhook
            series_title   = media.get("show_name") or data.get("SeriesName", "Unknown Series")
            episode_title  = media.get("episode_name") or data.get("Name", "Unknown Episode")
            season_number  = int(media.get("season_num") or data.get("SeasonNumber", 0))
            episode_number = int(media.get("episode_num") or data.get("EpisodeNumber", 0))
            tvdb_id        = media.get("ids", {}).get("tvdb") or data.get("Provider_tvdb")
            year = media.get("year", "")
            
            full_title = (
                f"{series_title} - S{season_number:02d}E{episode_number:02d}"
                if series_title and "{series_title}" not in series_title
                else title
            )

            if not should_process_playback(tvdb_id):
                return JSONResponse({"status": "skipped", "message": "Playback suppressed (cooldown active)"})

            logger.info(f"Processing episode playback for {full_title}", extra={'emoji_type': 'process'})
            series_id = search_in_sonarr(
                tvdb_id=tvdb_id, title=series_title, rating_key=rating_key,
                is_4k=is_4k, file_path=file_path
            )
            if not series_id:
                return JSONResponse({"status": "error", "message": "Failed to get series ID"}, status_code=400)
                
            play_mode = settings.TV_PLAY_MODE.lower()
            search_success = False
            
            if play_mode == "episode":
                lookahead = getattr(settings, 'EPISODES_LOOKAHEAD', 5)
                episodes_to_monitor, reached_end = get_episodes_for_lookahead(
                    series_id, season_number, episode_number, lookahead
                )
                
                if not episodes_to_monitor:
                    logger.warning("No episodes found to monitor", extra={'emoji_type': 'warning'})
                    return JSONResponse({"status": "warning", "message": "No episodes available"})
                
                episode_ids = [ep['id'] for ep in episodes_to_monitor]
                monitor_episodes(series_id, episode_ids, monitor=True)
                
                if reached_end:
                    mark_series_monitored(series_id, mark_seasons=False)
                    
                search_success = trigger_sonarr_search(
                    series_id, episode_ids=episode_ids, series_title=full_title, is_4k=is_4k
                )
                
                if search_success:
                    # Add each episode to our monitoring system
                    for episode in episodes_to_monitor:
                        add_to_monitor({
                            'media_type': 'episode',
                            'tvdb_id': tvdb_id,
                            'series_title': series_title,
                            'title': f"{series_title} - S{episode['seasonNumber']:02d}E{episode['episodeNumber']:02d}",
                            'rating_key': rating_key,
                            'season_number': episode['seasonNumber'],
                            'episode_number': episode['episodeNumber'],
                            'episode_id': episode['id'],  # Make sure we include the episode ID
                            'is_4k': is_4k,
                            'hasFile': episode.get('hasFile', False)
                        })
            
            elif play_mode == "season":
                url = f"{settings.SONARR_URL}/episode"
                params = {'seriesId': series_id}
                headers = {'X-Api-Key': settings.SONARR_API_KEY}
                
                try:
                    response = requests.get(url, params=params, headers=headers)
                    response.raise_for_status()
                    all_episodes = response.json()
                    
                    season_episodes = [ep for ep in all_episodes if ep.get('seasonNumber') == int(season_number)]
                    season_episodes.sort(key=lambda x: x.get('episodeNumber', 0))
                    
                    is_last_episode_in_season = False
                    next_season_exists = False
                    next_season = int(season_number) + 1
                    
                    if season_episodes and season_episodes[-1].get('episodeNumber') == int(episode_number):
                        is_last_episode_in_season = True
                        next_season_episodes = [ep for ep in all_episodes if ep.get('seasonNumber') == next_season]
                        if next_season_episodes:
                            next_season_exists = True
                    
                    monitor_season(series_id, season_number)
                    
                    if is_last_episode_in_season:
                        if next_season_exists:
                            # Current behavior - monitor the next season
                            monitor_season(series_id, next_season)
                            logger.info(f"Last episode of season {season_number} played, adding season {next_season}", 
                                      extra={'emoji_type': 'info'})
                        
                            search_success = trigger_sonarr_search(
                                series_id, season_number=season_number, series_title=full_title, is_4k=is_4k
                            )
                            trigger_sonarr_search(
                                series_id, season_number=next_season, series_title=full_title, is_4k=is_4k
                            )
                        else:
                            # NEW: If no next season exists, mark the entire series for future monitoring
                            mark_series_monitored(series_id, mark_seasons=False)
                            logger.info(f"Last episode of final season {season_number} played, marking series for future monitoring", 
                                      extra={'emoji_type': 'info'})
                        
                            search_success = trigger_sonarr_search(
                                series_id, season_number=season_number, series_title=full_title, is_4k=is_4k
                            )
                    else:
                        search_success = trigger_sonarr_search(
                            series_id, season_number=season_number, series_title=full_title, is_4k=is_4k
                        )
                        
                    if search_success:
                        # Get all episodes for this season
                        url = f"{settings.SONARR_URL}/episode"
                        params = {'seriesId': series_id}
                        headers = {'X-Api-Key': settings.SONARR_API_KEY}
                        try:
                            response = requests.get(url, params=params, headers=headers)
                            response.raise_for_status()
                            
                            # Filter for this season (and next season if applicable)
                            season_to_monitor = [int(season_number)]
                            if is_last_episode_in_season and next_season_exists:
                                season_to_monitor.append(int(next_season))
                            
                            all_episodes = response.json()
                            episodes_to_monitor = [ep for ep in all_episodes if ep.get('seasonNumber') in season_to_monitor]
                            
                            # Add each episode to monitoring if it doesn't have a file
                            for ep in episodes_to_monitor:
                                if not ep.get('hasFile', False):
                                    add_to_monitor({
                                        'media_type': 'episode',
                                        'tvdb_id': tvdb_id,
                                        'series_title': series_title,
                                        'title': f"{series_title} - S{ep['seasonNumber']:02d}E{ep['episodeNumber']:02d}",
                                        'rating_key': rating_key,
                                        'season_number': ep['seasonNumber'],
                                        'episode_number': ep['episodeNumber'],
                                        'episode_id': ep['id'],
                                        'is_4k': is_4k,
                                        'hasFile': ep.get('hasFile', False)
                                    })
                        except Exception as e:
                            logger.error(f"Error adding season episodes to monitoring: {e}", extra={'emoji_type': 'error'})
                        
                except Exception as e:
                    logger.error(f"Error handling season mode: {str(e)}", extra={'emoji_type': 'error'})
                    monitor_season(series_id, season_number)
                    search_success = trigger_sonarr_search(
                        series_id, season_number=season_number, series_title=full_title, is_4k=is_4k
                    )
                    
                    if search_success:
                        # Fallback to get episodes and add to batch monitoring
                        try:
                            url = f"{settings.SONARR_URL}/episode"
                            params = {'seriesId': series_id}
                            headers = {'X-Api-Key': settings.SONARR_API_KEY}
                            response = requests.get(url, params=params, headers=headers)
                            response.raise_for_status()
                            
                            # Get episodes for this season
                            all_episodes = response.json()
                            season_episodes = [ep for ep in all_episodes if ep.get('seasonNumber') == int(season_number)]
                            
                            # Add episodes to monitoring
                            for ep in season_episodes:
                                if not ep.get('hasFile', False):
                                    add_to_monitor({
                                        'media_type': 'episode',
                                        'tvdb_id': tvdb_id,
                                        'series_title': series_title,
                                        'title': f"{series_title} - S{ep['seasonNumber']:02d}E{ep['episodeNumber']:02d}",
                                        'rating_key': rating_key,
                                        'season_number': ep['seasonNumber'],
                                        'episode_number': ep['episodeNumber'],
                                        'episode_id': ep['id'],
                                        'is_4k': is_4k,
                                        'hasFile': ep.get('hasFile', False)
                                    })
                        except Exception as e2:
                            logger.error(f"Error in fallback season monitoring: {e2}", extra={'emoji_type': 'error'})
            
            else:  # series mode
                # First check if all episodes already have files
                url = f"{settings.SONARR_URL}/episode"
                params = {'seriesId': series_id}
                headers = {'X-Api-Key': settings.SONARR_API_KEY}
                
                try:
                    response = requests.get(url, params=params, headers=headers)
                    response.raise_for_status()
                    all_episodes = response.json()
                    
                    # For series mode, we might want to exclude specials or future episodes based on configuration
                    include_specials = getattr(settings, 'INCLUDE_SPECIALS', False)
                    episodes_to_check = all_episodes if include_specials else [ep for ep in all_episodes if ep.get('seasonNumber', 0) > 0]
                    
                    # Check if all episodes already have files
                    missing_episodes = [ep for ep in episodes_to_check if not ep.get('hasFile', False)]
                    
                    if not missing_episodes:
                        logger.info(f"All episodes of '{series_title}' already have files. Skipping search.", 
                                  extra={'emoji_type': 'skip'})
                        return JSONResponse({"status": "success", "message": "All files already available"})
                        
                    # Log the number of missing episodes
                    logger.info(f"Found {len(missing_episodes)} episodes without files for '{series_title}'", 
                              extra={'emoji_type': 'info'})
                    
                    # Now continue with normal flow - mark series as monitored
                    mark_series_monitored(series_id, mark_seasons=True, include_specials=include_specials)
                    
                    # Trigger search only if we have missing episodes
                    search_success = trigger_sonarr_search(
                        series_id, series_title=full_title, is_4k=is_4k
                    )
                    
                    if search_success:
                        # Only monitor episodes that don't have files
                        for ep in missing_episodes:
                            add_to_monitor({
                                'media_type': 'episode',
                                'tvdb_id': tvdb_id,
                                'series_title': series_title,
                                'title': f"{series_title} - S{ep['seasonNumber']:02d}E{ep['episodeNumber']:02d}",
                                'rating_key': rating_key,
                                'season_number': ep['seasonNumber'],
                                'episode_number': ep['episodeNumber'],
                                'episode_id': ep['id'],
                                'is_4k': is_4k,
                                'hasFile': False
                            })
                except Exception as e:
                    logger.error(f"Error checking series files: {e}", extra={'emoji_type': 'error'})
                    # Fallback to original behavior on error
                    mark_series_monitored(series_id, mark_seasons=True, include_specials=getattr(settings, 'INCLUDE_SPECIALS', False))
                    
                    search_success = trigger_sonarr_search(
                        series_id, series_title=full_title, is_4k=is_4k
                    )
                    
                    if search_success:
                        # Fall back to getting all episodes
                        try:
                            response = requests.get(url, params=params, headers=headers)
                            response.raise_for_status()
                            all_episodes = response.json()
                            
                            # Add each episode to monitoring if it doesn't have a file
                            for ep in all_episodes:
                                if not ep.get('hasFile', False):
                                    add_to_monitor({
                                        'media_type': 'episode',
                                        'tvdb_id': tvdb_id,
                                        'series_title': series_title,
                                        'title': f"{series_title} - S{ep['seasonNumber']:02d}E{ep['episodeNumber']:02d}",
                                        'rating_key': rating_key,
                                        'season_number': ep['seasonNumber'],
                                        'episode_number': ep['episodeNumber'],
                                        'episode_id': ep['id'],
                                        'is_4k': is_4k,
                                        'hasFile': False
                                    })
                        except Exception as e2:
                            logger.error(f"Error in fallback episode monitoring: {e2}", extra={'emoji_type': 'error'})
            
            if search_success:
                return JSONResponse({"status": "success", "message": "Search triggered"})
            else:
                return JSONResponse({"status": "error", "message": "Failed to trigger search"}, status_code=500)
                
        else:
            logger.warning(f"Unsupported media type: {media.get('type')}", extra={'emoji_type': 'warning'})
            return JSONResponse({"status": "error", "message": "Unsupported media type"}, status_code=400)

    except Exception as e:
        logger.error(f"Playback handling error: {e}", extra={'emoji_type': 'error'})
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)