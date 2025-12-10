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
    monitor_season
)
from services.postgres.db import get_session
from services.postgres.movie_repo import MovieRepository
from services.postgres.series_repo import SeriesRepository
from services.scheduler import (
    handle_import_event_scheduler,
    handle_seriesadd_scheduler,
    handle_episodefiledelete_scheduler,
    handle_moviefiledelete_scheduler,
    handle_movie_delete_scheduler,
    handle_movieadd_scheduler,
    handle_seriesdelete_scheduler,
    playback_scheduler
)
from urllib.parse import quote
from services.queue_monitor import handle_download_webhook
from services.integrations import enrich_movie_from_radarr

def handle_webhook(data: dict, source_port: int = None):
    """Handle webhook with quality awareness"""
    if not data.get("instanceName") and not data.get("ServerName"):
        source = "Tautulli"
    else:
        source = data.get("ServerName") if not data.get("instanceName") else data.get("instanceName")
    
    # Log incoming webhook but keep it brief
    logger.debug(f"{source} payload: {data}", extra={'emoji_type': 'debug'})
    
    # Get file path for quality detection. Only call Jellyfin lookup when ItemId is present.
    file_path = (data.get('media', {}).get('file_info', {}).get('path') or 
                 data.get('movie', {}).get('folderPath') or 
                 data.get('file', ''))
    if not file_path and data.get("ItemId"):
        try:
            file_path = get_jellyfin_file_path(data.get("ItemId"), data.get("UserId"))
        except Exception:
            # jellyfin lookup errors are non-fatal for quality detection
            file_path = ''
    
    is_4k = is_4k_request(file_path, source_port)
    # Quality determination is helpful when debugging but noisy in normal logs
    logger.verbose(f"Quality determination: {'4K' if is_4k else 'Standard'}", extra={'emoji_type': 'debug'})
    
    event_type = (data.get('event') or data.get('eventType') or data.get('NotificationType') or 'unknown').lower()
    logger.info(f"Received webhook event: {event_type}", extra={'emoji_type': 'webhook'})
    
    # Handle import events directly for cleanup (includes upgrades)
    if event_type in ['download', 'moviefileimported', 'episodefileimported', 'upgrade', 'moviefileupgraded', 'episodefileupgraded']:
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
        return handle_moviefiledelete(data, is_4k)
    elif event_type == 'moviedelete':
        return handle_movie_delete(data, is_4k)
    elif event_type in ('movieadd', 'movieadded'):
        return handle_movieadd(data, is_4k)
    elif event_type == 'seriesdelete':
        return handle_seriesdelete(data, is_4k)
    elif event_type in ['playback.start', 'playbackstart']:
        return handle_playback(data)
    else:
        # Fallback for unhandled events from other ARR providers
        logger.info(f"Handling ARR import event: {data}", extra={'emoji_type': 'webhook'})
        return JSONResponse({"status": "success", "message": "Import event processed"})

def handle_import_event(data: dict, is_4k: bool = False):
    """Handle media import events and clean up placeholders using scheduler"""
    # Start handler logging session
    session_id = start_handler_logging(
        'handle_import_event',
        0,  # Will be updated with actual ID once determined
        'import',
        is_4k=is_4k,
        data_keys=list(data.keys())
    )
    
    logger.info(f"📥 Processing Import webhook - Type: {'movie' if 'movie' in data else 'series'}, 4K: {is_4k}", extra={'emoji_type': 'webhook'})
    
    try:
        from services.utils import resolve_final_folder
        if 'movie' in data:
            # Movie import handling
            movie = data['movie']
            tmdb_id = movie.get('tmdbId')
            radarr_id = movie.get('id')  # Get Radarr movie ID
            title = movie.get('title', 'Unknown Movie')
            year = movie.get('year')
            movie_path = data.get("movieFile", {}).get("path")
            folder_path = movie.get('folderPath') or movie.get('path')
            arr_root_folder = movie.get('rootFolderPath') or getattr(settings, 'RADARR_ROOT_FOLDER', None) or None
            # Use resolve_final_folder to match dummy creation
            dummy_folder = resolve_final_folder(
                media_type="movie",
                title=title,
                year=year,
                media_id=tmdb_id,
                folder_path=folder_path,
                arr_root_folder=arr_root_folder
            )
            file_name = f"{sanitize_filename(title)}"
            if year:
                file_name += f" ({year})"
            file_name += f" (dummy).mp4"
            dummy_file_path = os.path.join(dummy_folder, file_name)
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
            # Clean up placeholder files (delete only the dummy file, not the folder)
            if os.path.exists(dummy_file_path):
                try:
                    os.remove(dummy_file_path)
                    logger.info(f"Deleted placeholder file: {dummy_file_path}", extra={'emoji_type': 'delete'})
                except Exception as e:
                    logger.error(f"Failed to delete dummy file {dummy_file_path}: {e}", extra={'emoji_type': 'error'})
            else:
                logger.debug(f"Dummy file not found for deletion: {dummy_file_path}", extra={'emoji_type': 'debug'})
            # --- NEW: Always refresh parent folder ---
            if movie_path:
                parent_folder = os.path.dirname(movie_path)
                if settings.plex_enabled:
                    refresh_plex_item(parent_folder)
                if settings.jellyfin_enabled:
                    refresh_jellyfin_item(parent_folder)
            # Also refresh the dummy folder
            if settings.plex_enabled:
                refresh_plex_item(dummy_folder)
            if settings.jellyfin_enabled:
                refresh_jellyfin_item(dummy_folder, "Deleted")
        elif 'episodes' in data and 'series' in data:
            series = data['series']
            episode = data['episodes'][0]  # Handle first episode in the list
            series_title = series.get('title', 'Unknown Series')
            series_year = series.get('year')
            tvdb_id = series.get('tvdbId')
            series_id = series.get('id')  # Get Sonarr series ID
            season_num = episode.get('seasonNumber')
            episode_num = episode.get('episodeNumber')
            episode_title = episode.get('title', 'Unknown Episode')
            episode_path = data.get("episodeFile", {}).get("path")
            folder_path = series.get('folderPath') or series.get('path')
            arr_root_folder = series.get('rootFolderPath') or getattr(settings, 'SONARR_ROOT_FOLDER', None) or None
            # Use resolve_final_folder to match dummy creation
            dummy_folder = resolve_final_folder(
                media_type="tv",
                title=series_title,
                year=series_year,
                media_id=tvdb_id,
                season_number=season_num,
                folder_path=folder_path,
                arr_root_folder=arr_root_folder
            )
            file_name = f"{sanitize_filename(series_title)}"
            if series_year:
                file_name += f" ({series_year})"
            file_name += f" - s{season_num:02d}e{episode_num:02d} - {episode_title}.mp4"
            dummy_file_path = os.path.join(dummy_folder, sanitize_filename(file_name))
            logger.info(f"Processing episode import cleanup for: {series_title} S{season_num}E{episode_num}", extra={'emoji_type': 'cleanup'})
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
            if os.path.exists(dummy_file_path):
                try:
                    os.remove(dummy_file_path)
                    logger.info(f"Deleted placeholder file: {dummy_file_path}", extra={'emoji_type': 'delete'})
                except Exception as e:
                    logger.error(f"Failed to delete dummy file {dummy_file_path}: {e}", extra={'emoji_type': 'error'})
            else:
                logger.debug(f"Dummy file not found for deletion: {dummy_file_path}", extra={'emoji_type': 'debug'})
            # --- NEW: Always refresh parent folder ---
            if episode_path:
                parent_folder = os.path.dirname(episode_path)
                if settings.plex_enabled:
                    refresh_plex_item(parent_folder)
                if settings.jellyfin_enabled:
                    refresh_jellyfin_item(parent_folder)
            # Also refresh the dummy folder
            if settings.plex_enabled:
                refresh_plex_item(dummy_folder)
            if settings.jellyfin_enabled:
                refresh_jellyfin_item(dummy_folder, "Deleted")
    except Exception as e:
        logger.error(f"Import event scheduling failed: {e}", extra={'emoji_type': 'error'})
        end_handler_logging(session_id, success=False, 
                           summary=f"Handler failed: {e}")
        return JSONResponse({"status": "error", "message": f"Error: {str(e)}"}, status_code=500)

    end_handler_logging(session_id, success=True, 
                       summary="Import cleanup completed")
    return JSONResponse({"status": "success", "message": "Import cleanup scheduled"})

def handle_seriesadd(data: dict, is_4k: bool = False):
    # Extract series info and episodes, create dummies and schedule updates in batch.
    series = data.get('series', {})
    episodes = data.get('episodes', [])
    series_title = series.get('title', 'Unknown Series')
    series_year = series.get('year')
    tvdb_id = series.get('tvdbId')
    library_path = getattr(settings, 'TV_LIBRARY_FOLDER', None)
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
    episode_updates = []
    from services.utils import resolve_final_folder
    from services.integrations import batch_poll_and_update_plex_status
    from services.calendar_sync import _parse_air_date
    series_folder = resolve_final_folder(
        media_type="tv",
        title=series_title,
        year=series_year,
        media_id=tvdb_id,
        folder_path=series.get('folderPath') or series.get('path'),
        arr_root_folder=series.get('rootFolderPath') or getattr(settings, 'SONARR_ROOT_FOLDER', None),
        season_number=None,
        season_folder_name=None
    )
    episode_list = []
    for ep in episodes:
        season_num = ep.get('seasonNumber')
        episode_num = ep.get('episodeNumber')
        episode_title = ep.get('title')
        air_date_str = ep.get('airDateUtc') or ep.get('airDate')
        air_date = _parse_air_date(air_date_str) if air_date_str else None
        if not (season_num and episode_num):
            continue
        from services.queue_monitor import check_episode_has_file
        if check_episode_has_file(tvdb_id, season_num, episode_num, is_4k):
            logger.info(f"Skipping placeholder for {series_title} S{season_num}E{episode_num} (real file exists)", extra={'emoji_type': 'skip'})
            continue
        dummy_path = place_dummy_file("tv", series_title, series_year, tvdb_id,
                                    library_path,
                                    season_number=season_num,
                                    episode_range=(episode_num, episode_num),
                                    episode_title=episode_title,
                                    folder_path=series.get('folderPath') or series.get('path'),
                                    arr_root_folder=series.get('rootFolderPath') or getattr(settings, 'SONARR_ROOT_FOLDER', None))
        if dummy_path:
            episode_updates.append((series_title, season_num, episode_num, tvdb_id))
            episode_list.append({
                'season_num': season_num,
                'episode_num': episode_num,
                'episode_title': episode_title,
                'air_date': air_date
            })
            logger.debug(f"Created placeholder file for {series_title} S{season_num}E{episode_num}", extra={'emoji_type': 'create'})
        else:
            logger.error(f"Failed to create dummy file for {series_title} S{season_num}E{episode_num}; skipping.", extra={'emoji_type': 'error'})
    # Single refresh for the series folder
    if series_folder:
        if settings.plex_enabled:
            refresh_plex_item(series_folder)
        if settings.jellyfin_enabled:
            refresh_jellyfin_item(series_folder)
    # --- Batch status update for Plex using polling utility ---
    if settings.plex_enabled and episode_list:
        threading.Thread(target=batch_poll_and_update_plex_status, args=(tvdb_id, series_title, episode_list), daemon=True).start()
    # --- Batch status update for Jellyfin ---
    if settings.jellyfin_enabled and episode_list:
        from services.jellyfin_client import update_jellyfin_title_status
        for ep in episode_list:
            update_jellyfin_title_status(
                media_type='tv',
                media_id=tvdb_id,
                title=series_title,
                status='Request',
                season=ep['season_num'],
                episode=ep['episode_num']
            )
    logger.info(f"Batch created {len(episode_updates)} placeholder files and refreshed series folder for '{series_title}'", extra={'emoji_type': 'create'})
    return JSONResponse({"status": "success", "message": "SeriesAdd batch scheduled"})

def handle_episodefiledelete(data: dict, is_4k: bool = False):
    # Similar to seriesadd: recreate dummy for episode deletion.
    series = data.get('series', {})
    episodes = data.get('episodes', [])
    series_title = series.get('title', 'Unknown Series')
    series_year = series.get('year')
    tvdb_id = series.get('tvdbId')
    series_id = series.get('id')  # Get Sonarr series ID
    episode_file_path = data.get("episodeFile", {}).get("path")
    library_path = getattr(settings, 'TV_LIBRARY_FOLDER', None)
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
                
        # Use folderPath from webhook if available
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
            logger.warning(f"Failed to enqueue 'handle_episodefiledelete' action for TVDB ID {tvdb_id}")
            end_handler_logging(session_id, success=False, 
                               summary="Failed to enqueue episodefiledelete processing")
            return JSONResponse({"status": "error", "message": "Failed to schedule processing"}, status_code=500)

    except Exception as e:
        logger.error(f"handle_episodefiledelete failed: {e}", extra={'emoji_type': 'error'})
        end_handler_logging(session_id, success=False, 
                           summary=f"Handler failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
    finally:
        try:
            if 'session' in locals():
                session.close()
        except Exception:
            pass

def handle_moviefiledelete(data: dict, is_4k):
    if 'movie' in data:
        movie = data.get('movie', {})
        tmdb_id = movie.get('tmdbId') or data.get('remoteMovie', {}).get('tmdbId')
        radarr_id = movie.get('id')  # Get Radarr movie ID
        if not tmdb_id:
            logger.error("Missing TMDB ID for movie file delete", extra={'emoji_type': 'error'})
            return JSONResponse({"status": "error"}, status_code=400)
        title = movie.get('title', 'Unknown Movie')
        year = movie.get('year')
        folder_path = movie.get('folderPath')
        arr_root_folder = movie.get('rootFolderPath') or getattr(settings, 'RADARR_ROOT_FOLDER', None) or None
        library_path = getattr(settings, 'MOVIE_LIBRARY_FOLDER', None)
        # Create a placeholder dummy file for the deleted movie
        from services.integrations import place_dummy_file
        dummy_path = place_dummy_file('movie', title, year, tmdb_id, base_path=library_path, folder_path=folder_path, arr_root_folder=arr_root_folder)
        if dummy_path:
            if settings.plex_enabled:
                refresh_plex_item(os.path.dirname(dummy_path))
            if settings.jellyfin_enabled:
                refresh_jellyfin_item(os.path.dirname(dummy_path), "Changed")
            logger.info(f"Created placeholder file for movie '{title}'", extra={'emoji_type': 'create'})
        else:
            logger.error(f"Failed to create dummy file for movie '{title}'; skipping.", extra={'emoji_type': 'error'})
        return JSONResponse({"status": "success", "message": "MovieFileDelete processed"})
    return JSONResponse({"status": "success", "message": "MovieFileDelete processed"})

def handle_movie_delete(data: dict, is_4k: bool = False):
    if 'movie' in data:
        movie = data.get('movie', {})
        tmdb_id = movie.get('tmdbId') or data.get('remoteMovie', {}).get('tmdbId')
        radarr_id = movie.get('id')  # Get Radarr movie ID
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

def handle_movieadd(data: dict, is_4k: bool = False):
    if 'movie' in data:
        movie = data.get('movie', {})
        movie_path = data.get("movie", {}).get("folderPath")
        tmdb_id = movie.get('tmdbId') or data.get('remoteMovie', {}).get('tmdbId')
        if not tmdb_id:
            logger.error("Missing TMDB ID for movie add", extra={'emoji_type': 'error'})
            return JSONResponse({"status": "error"}, status_code=400)
        title = movie.get('title', 'Unknown Movie')
        year = movie.get('year', '')
        radarr_id = movie.get('id')
        
        # Start handler logging session
        session_id = start_handler_logging(
            'handle_movieadd',
            tmdb_id,
            'movie',
            title=title,
            year=year,
            tmdb_id=tmdb_id,
            is_4k=is_4k,
            radarr_id=radarr_id
        )
        
        logger.info(f"➕ Processing MovieAdd webhook for '{title}' ({year}) - TMDB: {tmdb_id}, Radarr ID: {radarr_id}", extra={'emoji_type': 'webhook'})

        # Extract movieFile / hasFile / quality information when present in the webhook
        movie_file = movie.get('movieFile') or data.get('movieFile') or {}
        moviefile_path = movie_file.get('path') or movie.get('folderPath') or movie_path or ''
        moviefile_size = movie_file.get('size') or movie_file.get('sizeInBytes')
        has_file = bool(movie.get('hasFile', False) or movie_file)
        radarr_quality = None
        q = movie_file.get('quality') or movie.get('quality')
        if isinstance(q, dict):
            radarr_quality = q.get('name') or (q.get('quality') or {}).get('name')

        # Release lifecycle and monitored flag
        radarr_release_status = movie.get('status')
        radarr_monitored = bool(movie.get('monitored', False))

        # Determine is_4k based on provided paths
        is_4k = is_4k or is_4k_request(moviefile_path or movie_path or '')

        session = get_session()
        repo = MovieRepository(session)
        m = repo.get_by_tmdbid(tmdb_id, is_4k)
        if not m:
            m = repo.add(
                title=title,
                year=year,
                tmdbid=tmdb_id,
                dummypath="",
                filepath=movie_path,
                radarrid=radarr_id,
                status="PENDING",
                is_4k=is_4k,
                moviefile_path=moviefile_path,
                moviefile_size=moviefile_size,
                has_file=has_file,
                radarr_quality=radarr_quality,
                radarr_release_status=radarr_release_status,
                radarr_monitored=radarr_monitored
            )
            logger.info(f"Added: {m}", extra={'emoji_type': 'success'})
        else:
            # Update existing record with any new fields provided by the webhook
            updated_fields = {
                'title': title,
                'year': year,
                'filepath': movie_path,
                'radarrid': radarr_id,
                'moviefile_path': moviefile_path,
                'moviefile_size': moviefile_size,
                'has_file': has_file,
                'radarr_quality': radarr_quality,
                'radarr_release_status': radarr_release_status,
                'radarr_monitored': radarr_monitored,
            }
            for k, v in updated_fields.items():
                if getattr(m, k, None) != v:
                    setattr(m, k, v)

            # If this movie was previously marked as deleted, resurrect it so it is
            # treated the same as a fresh user add: reset deleted flag, status, and
            # current step so the scheduler will process it again.
            resurrected = False
            if getattr(m, 'is_deleted', False):
                m.is_deleted = False
                resurrected = True

            if getattr(m, 'status', None) != 'PENDING':
                m.status = 'PENDING'
                resurrected = True

            # Reset step pointer so flow starts from the beginning
            m.current_step_name = None

            # Clear stale placeholder indicators so placeholder creation runs anew
            cleared_any = False
            if getattr(m, 'dummypath', None):
                m.dummypath = None
                cleared_any = True

            for fld in ('plex_dummy_id', 'jellyfin_dummy_id', 'plex_title', 'jellyfin_title', 'plex_overview', 'jellyfin_overview'):
                if getattr(m, fld, None):
                    setattr(m, fld, None)
                    cleared_any = True

            session.commit()
            logger.info("Updated existing movie with webhook data", extra={'emoji_type': 'update'})

            if resurrected:
                logger.info(f"Resurrected previously deleted movie {m.tmdbid} and reset status to PENDING", extra={'emoji_type': 'refresh'})
            elif cleared_any:
                logger.debug(f"Cleared stale placeholder metadata for movie {m.tmdbid}", extra={'emoji_type': 'debug'})

        # Start enrichment in background to fetch authoritative data from Radarr
        try:
            threading.Thread(
                target=enrich_movie_from_radarr,
                args=(tmdb_id, radarr_id, is_4k),
                daemon=True
            ).start()
            logger.debug(f"Enrichment started for TMDB {tmdb_id} radarr {radarr_id}", extra={'emoji_type': 'debug'})
        except Exception as e:
            logger.error(f"Failed to start enrichment thread: {e}", extra={'emoji_type': 'error'})

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
    """Delete placeholder files when a series is deleted from Sonarr using universal path logic."""
    if 'series' in data:
        series = data.get('series', {})
        tvdb_id = series.get('tvdbId')
        title = series.get('title', 'Unknown Series')
        year = series.get('year')
        library_folder = getattr(settings, 'TV_LIBRARY_FOLDER', None)
        if library_folder and str(library_folder).strip():
            base_path = library_folder
            folder_path = None
            arr_root_folder = None
        else:
            base_path = None
            folder_path = series.get('folderPath') or series.get('path')
            arr_root_folder = series.get('rootFolderPath') or getattr(settings, 'SONARR_ROOT_FOLDER', None) or None
        from services.integrations import delete_dummy_files
        delete_dummy_files('tv', title, year, tvdb_id, base_path, folder_path=folder_path, arr_root_folder=arr_root_folder)
        # Optionally refresh Plex/Jellyfin at the dummy folder location
        if base_path:
            import os
            dummy_folder = os.path.join(base_path, f"{sanitize_filename(title)} ({year}) {{tvdb-{tvdb_id}}}")
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

        # Check if file is in one of our placeholder library folders (for logging/analytics only)
        placeholder_folders = [settings.MOVIE_LIBRARY_FOLDER, settings.TV_LIBRARY_FOLDER]
        if getattr(settings, 'MOVIE_LIBRARY_4K_FOLDER', None):
            placeholder_folders.append(settings.MOVIE_LIBRARY_4K_FOLDER)
        if getattr(settings, 'TV_LIBRARY_4K_FOLDER', None):
            placeholder_folders.append(settings.TV_LIBRARY_4K_FOLDER)

        logger.debug(f"Checking path against placeholder folders: {placeholder_folders}", extra={'emoji_type': 'debug'})
        is_placeholder = any(file_path.startswith(folder) for folder in placeholder_folders if folder)

        # Debug log the placeholder check result
        logger.debug(f"Is placeholder check result: {is_placeholder}", extra={'emoji_type': 'debug'})
        
        if media_type == "movie":
            tmdb = media.get("ids", {}).get("tmdb") or data.get("Provider_tmdb")
            repo = MovieRepository(session)
            movie = repo.get_by_tmdbid(tmdb, is_4k) if tmdb else repo.get_by_path(file_path)
            if not movie:
                logger.error(f"No DB Movie for path {file_path}")
                end_handler_logging(session_id, success=False, 
                                   summary="Movie not found in database")
                return JSONResponse({"status": "error", "message": "Movie not found"}, status_code=404)
            
            job_scheduled = playback_scheduler.enqueue(movie)
            if job_scheduled:
                logger.info(f"Enqueued playback for movie {movie.title}")
                # Keep logging session open - it will be closed when playback flow completes
                return JSONResponse({"status": "scheduled", "message": "Movie playback enqueued"})
            else:
                end_handler_logging(session_id, success=False, 
                                   summary="Failed to enqueue movie playback processing")
                return JSONResponse({"status": "error", "message": "Failed to schedule processing"}, status_code=500)

        elif media_type == "episode":
            # extract series, season & episode numbers
            series_tvdb   = media.get("ids", {}).get("tvdb") or data.get("Provider_tvdb")
            # item_id = None
            # if "ServerName" in data and data.get("ServerName") == "jellyfin":
            #     item_id = data.get("ItemId")
            # else:
            #     logger.debug("Non-Jellyfin playback event or missing ItemId", extra={"emoji_type": "debug"})
            season_number = int(media.get("season_num")  or data.get("SeasonNumber", 0))
            episode_number= int(media.get("episode_num") or data.get("EpisodeNumber", 0))
            repo = SeriesRepository(session)
            # if item_id:
            #     series = repo.get_by_jellyfin_itemid(item_id, is_4k)
            # else:
            series = repo.get_by_ep_tvdbid(series_tvdb, is_4k)
            if not series:
                logger.error(f"No DB Series for TVDB {series_tvdb}")
                end_handler_logging(session_id, success=False, 
                                   summary="Series not found in database")
                return JSONResponse({"status": "error", "message": "Series not found"}, status_code=404)

            # find matching Episode under that series
            ep = repo.get_ep_by_series(series, season_number, episode_number)
            if not ep:
                logger.error(f"No DB Episode for {series.title} S{season_number}E{episode_number}")
                end_handler_logging(session_id, success=False, 
                                   summary="Episode not found in database")
                return JSONResponse({"status": "error", "message": "Episode not found"}, status_code=404)

            job_scheduled = playback_scheduler.enqueue(ep)
            if job_scheduled:
                logger.info(f"Enqueued playback for episode {ep.title}")
                # Keep logging session open - it will be closed when playback flow completes
                return JSONResponse({"status": "scheduled", "message": "Episode playback enqueued"})
            else:
                end_handler_logging(session_id, success=False, 
                                   summary="Failed to enqueue episode playback processing")
                return JSONResponse({"status": "error", "message": "Failed to schedule processing"}, status_code=500)

        else:
            logger.warning(f"Unsupported media type: {media_type}")
            end_handler_logging(session_id, success=False, 
                               summary=f"Unsupported media type: {media_type}")
            return JSONResponse({"status": "error", "message": "Unsupported media type"}, status_code=400)

    except Exception as e:
        logger.error(f"Error in handle_playback: {e}")
        end_handler_logging(session_id, success=False, 
                           summary=f"Handler failed: {e}")
        return JSONResponse({"status": "error", "message": "Internal error"}, status_code=500)
    finally:
        session.close()