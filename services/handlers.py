import os, re, threading, time, shutil, requests
from fastapi.responses import JSONResponse
from core.config import settings
from core.logger import logger
from services.plex_client import plex, build_plex_url
from services.integrations import (
    place_dummy_file, delete_dummy_files, schedule_episode_request_update,
    schedule_movie_request_update, check_media_has_file, check_has_file,
    search_in_radarr, search_in_sonarr, trigger_sonarr_search, monitor_episodes, 
    mark_series_monitored, get_episodes_for_lookahead, check_tv_has_file,
    monitor_season
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

# In handle_playback, we need to keep the existing structure but integrate with queue monitoring

def handle_playback(data: dict):
    try:
        media = data.get("media", {})
        file_path = media.get("file_info", {}).get("path", "")
        is_4k = is_4k_request(file_path)
        title = media.get("title", "Unknown Title")
        rating_key = media.get("ids", {}).get("plex")

        if media.get("type") == "movie":
            tmdb_id = media.get("ids", {}).get("tmdb")
            imdb_id = media.get("ids", {}).get("imdb")
            year = media.get("year", "")
            
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
            else:
                return JSONResponse({"status": "error", "message": "Failed to find/add movie"}, status_code=400)
            
        elif media.get("type") == "episode":
            # Extract episode details from webhook
            series_title = media.get("show_name", "Unknown Series")
            episode_title = media.get("episode_name", "Unknown Episode")
            season_number = int(media.get("season_num", 0))
            episode_number = int(media.get("episode_num", 0))
            year = media.get("year", "")
            tvdb_id = media.get("ids", {}).get("tvdb")
            
            # Skip if this series was recently processed
            if not should_process_playback(tvdb_id):
                return JSONResponse({"status": "skipped", "message": "Playback suppressed (cooldown active)"})
            
            # Rest of episode handling continues unchanged
            file_path = media.get("file_info", {}).get("path", "")
            series_id = search_in_sonarr(tvdb_id=tvdb_id, title=series_title, rating_key=rating_key, 
                            is_4k=is_4k, file_path=file_path)
            
            # Build display-friendly title
            if series_title and series_title != "{series_title}":
                full_title = f"{series_title} - S{season_number:02d}E{episode_number:02d}"
            else:
                full_title = f"{title}"
            
            logger.info(f"Processing episode playback for {full_title}", extra={'emoji_type': 'process'})
            
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
                    
                    if is_last_episode_in_season and next_season_exists:
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
                        search_success = trigger_sonarr_search(
                            series_id, season_number=season_number, series_title=full_title, is_4k=is_4k
                        )
                        
                    if search_success:
                        check_tv_has_file(tvdb_id, series_title, rating_key, 
                                        season_number=season_number, 
                                        episode_number=episode_number, 
                                        is_4k=is_4k)
                        
                except Exception as e:
                    logger.error(f"Error handling season mode: {str(e)}", extra={'emoji_type': 'error'})
                    monitor_season(series_id, season_number)
                    search_success = trigger_sonarr_search(
                        series_id, season_number=season_number, series_title=full_title, is_4k=is_4k
                    )
                    if search_success:
                        check_tv_has_file(tvdb_id, series_title, rating_key, 
                                       season_number=season_number, 
                                       episode_number=episode_number, 
                                       is_4k=is_4k)
            
            else:  # series mode
                mark_series_monitored(series_id, mark_seasons=True, include_specials=getattr(settings, 'INCLUDE_SPECIALS', False))
                
                search_success = trigger_sonarr_search(
                    series_id, series_title=full_title, is_4k=is_4k
                )
                
                if search_success:
                    check_tv_has_file(tvdb_id, series_title, rating_key, 
                                   season_number=season_number, 
                                   episode_number=episode_number, 
                                   is_4k=is_4k)
            
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

def handle_download(data, is_4k=False):
    """Handle episode/movie import webhook event from Sonarr/Radarr"""
    try:
        # Determine if this is a movie or episode
        if 'movie' in data:
            # Movie download
            movie_data = data.get('movie', {})
            title = movie_data.get('title', 'Unknown')
            year = movie_data.get('year', '')
            tmdb_id = movie_data.get('tmdbId')
            
            logger.info(f"Processing movie import cleanup for: {title} ({year})", extra={'emoji_type': 'process'})
            
            # Delete placeholder file
            library_folder = settings.MOVIE_LIBRARY_4K_FOLDER if is_4k else settings.MOVIE_LIBRARY_FOLDER
            delete_dummy_files('movie', title, year, tmdb_id, library_folder)
            
            # Refresh Plex library
            refresh_plex_library('movie')
            
        elif 'episodes' in data:
            # Episode download
            episodes = data.get('episodes', [])
            series = data.get('series', {})
            series_title = series.get('title', 'Unknown Series')
            tvdb_id = series.get('tvdbId')
            
            for episode in episodes:
                season_number = episode.get('seasonNumber')
                episode_number = episode.get('episodeNumber')
                episode_title = episode.get('title', 'Unknown')
                
                logger.info(f"Processing episode import cleanup for: {series_title} - S{season_number:02d}E{episode_number:02d} - {episode_title}", 
                          extra={'emoji_type': 'process'})
                
                # First check our monitoring registry
                episode_key = f"episode_{tvdb_id}_{season_number}_{episode_number}"
                
                from services.queue_monitor import MONITORED_MEDIA, REGISTRY_LOCK, update_media_status
                with REGISTRY_LOCK:
                    if episode_key in MONITORED_MEDIA:
                        update_media_status(episode_key, "Available")
                    else:
                        # Check if this episode has a placeholder that needs updating
                        try:
                            # This will check for a placeholder and update if found
                            from services.plex_client import check_and_update_placeholder
                            check_and_update_placeholder(
                                series_title=series_title, 
                                season_number=season_number, 
                                episode_number=episode_number,
                                status="Available"
                            )
                        except Exception as e:
                            logger.error(f"Error checking for placeholder: {e}", extra={'emoji_type': 'error'})
                
                # Always do cleanup - explicitly specify all parameters
                library_folder = settings.TV_LIBRARY_4K_FOLDER if is_4k else settings.TV_LIBRARY_FOLDER
                delete_dummy_files(
                    media_type='tv',
                    title=series_title,
                    year=series.get('year'),
                    tvdb_id=tvdb_id,
                    library_path=library_folder,
                    season_number=season_number,
                    episode_number=episode_number
                )
                
            # Refresh Plex library
            refresh_plex_library('tv')
            
        return JSONResponse({"status": "success", "message": "Download processed"})
        
    except Exception as e:
        logger.error(f"Error handling download: {str(e)}", extra={'emoji_type': 'error'})
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
    
def refresh_plex_library(media_type=None):
    """Refresh Plex libraries"""
    try:
        if media_type == 'tv' or media_type is None:
            # Refresh TV library
            refresh_url = build_plex_url(f"library/sections/{settings.PLEX_TV_SECTION_ID}/refresh")
            requests.get(refresh_url, headers={'X-Plex-Token': settings.PLEX_TOKEN})
            logger.debug("Refreshed Plex TV library", extra={'emoji_type': 'refresh'})
            
        if media_type == 'movie' or media_type is None:
            # Refresh movie library
            refresh_url = build_plex_url(f"library/sections/{settings.PLEX_MOVIE_SECTION_ID}/refresh") 
            requests.get(refresh_url, headers={'X-Plex-Token': settings.PLEX_TOKEN})
            logger.debug("Refreshed Plex movie library", extra={'emoji_type': 'refresh'})
            
    except Exception as e:
        logger.error(f"Failed to refresh Plex library: {e}", extra={'emoji_type': 'error'})