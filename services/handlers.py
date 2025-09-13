import os, re, threading, time, shutil, requests
from fastapi.responses import JSONResponse
from core.config import settings
from core.logger import logger
from services.jellyfin_client import get_jellyfin_file_path
from services.utils import ( 
    is_4k_request
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
    try:
        if 'movie' in data:
            # Movie import handling
            movie = data['movie']
            tmdb_id = movie.get('tmdbId')
            title = movie.get('title', 'Unknown Movie')
            year = movie.get('year')
            
            if not tmdb_id:
                logger.error("Missing TMDB ID for movie import", extra={'emoji_type': 'error'})
                return JSONResponse({"status": "error", "message": "Missing TMDB ID"}, status_code=400)
            
            logger.info(f"Processing movie import cleanup for: {title}", extra={'emoji_type': 'cleanup'})
            
            session = get_session()
            repo = MovieRepository(session)
            
            movie = repo.get_by_tmdbid(tmdb_id, is_4k)
            if not movie:
                logger.warning(f"Movie {title} not found in database for import event", extra={'emoji_type': 'warning'})
                return JSONResponse({"status": "success", "message": "Movie not tracked, no cleanup needed"})
            
            # Don't change the movie's action - it should keep its original action (e.g., handle_movieadd)
            # Only reset status to PENDING if it's not already being processed
            if movie.status not in ['PENDING', 'QUEUED']:
                movie.status = 'PENDING'
                session.commit()
            
            job_scheduled = handle_import_event_scheduler.enqueue(movie)
            if job_scheduled:
                logger.info(f"Enqueued 'handle_import_event' action for TMDB ID {tmdb_id}", extra={'emoji_type': 'queue'})
            else:
                logger.warning(f"Failed to enqueue 'handle_import_event' action for TMDB ID {tmdb_id}", extra={'emoji_type': 'warning'})
            
            session.close()

        elif 'episodes' in data and 'series' in data:
            series = data['series']
            episode = data['episodes'][0]  # Handle first episode in the list
            
            tvdb_id = series.get('tvdbId')
            season_num = episode.get('seasonNumber')
            episode_num = episode.get('episodeNumber')
            series_title = series.get('title', 'Unknown Series')
            
            if not (tvdb_id and season_num is not None and episode_num is not None):
                logger.error("Missing required data for episode import", extra={'emoji_type': 'error'})
                return JSONResponse({"status": "error", "message": "Missing required data"}, status_code=400)
            
            full_title = f"{series_title} - S{season_num:02d}E{episode_num:02d}"
            logger.info(f"Processing episode import cleanup for: {full_title}", extra={'emoji_type': 'cleanup'})
            
            session = get_session()
            repo = SeriesRepository(session)
            
            series_entity = repo.get_by_tvdbid(tvdb_id, is_4k)
            if not series_entity:
                logger.warning(f"Series {series_title} not found in database for import event", extra={'emoji_type': 'warning'})
                return JSONResponse({"status": "success", "message": "Series not tracked, no cleanup needed"})
            ep = repo.get_ep_by_series(series, season_num, episode_num)
            ep.status = 'PENDING'
            ep.action = 'handle_episode_import'
            ep.jellyfin
            session.commit()

            job_scheduled = handle_import_event_scheduler.enqueue(ep)
            if job_scheduled:
                logger.info(f"Enqueued 'handle_movie_import' action for TMDB ID {tmdb_id}", extra={'emoji_type': 'queue'})
            else:
                logger.warning(f"Failed to enqueue 'handle_movie_import' action for TMDB ID {tmdb_id}", extra={'emoji_type': 'warning'})

            session.close()

    except Exception as e:
        logger.error(f"Import event scheduling failed: {e}", extra={'emoji_type': 'error'})
        return JSONResponse({"status": "error", "message": f"Error: {str(e)}"}, status_code=500)

    return JSONResponse({"status": "success", "message": "Import cleanup scheduled"})

def handle_seriesadd(data: dict, is_4k: bool = False):
    # Extract series info and episodes, create dummies and schedule updates.
    series = data.get('series', {})
    episodes = data.get('episodes', [])
    series_title = series.get('title', 'Unknown Series')
    series_year = series.get('year')
    tvdb_id = series.get('tvdbId')
    series_path = series.get('path')
    series_id = series.get('id')
    if not episodes:
        if series_id:
            r = requests.get(f"{settings.SONARR_URL}/episode",
                             params={'seriesId': series_id},
                             headers={'X-Api-Key': settings.SONARR_API_KEY})
            r.raise_for_status()
            episodes = r.json()
        else:
            logger.warning("No series ID provided in seriesadd event.", extra={'emoji_type': 'warning'})
            episodes = []
    session = get_session()
    repo = SeriesRepository(session)
    series = repo.get_by_tvdbid(tvdb_id, is_4k)
    if not series:
        m = repo.add(
            title=series_title, year=series_year,
            tmdbid=tvdb_id, dummypath="",
            sonarrpath=series_path, sonarrid=series_id, status="PENDING"
        )
        logger.info(f"Series added: {m}", extra={'emoji_type': 'success'})
    else:
        logger.info("Series already present", extra={'emoji_type':'warning'})
        repo.is_deleted(series, False)
    repo.add_missing_seasons_and_episodes(series, episodes)
    logger.info(f"Episode data inserted to db for series {series_title}", extra={'emoji_type':'success'})
    job_scheduled = handle_seriesadd_scheduler.enqueue(series.id)
    if job_scheduled:
        logger.info(f"Enqueued 'handle_movieadd' action for TVDB ID {tvdb_id}")
    else:
        logger.warning(f"Failed to enqueue 'handle_movieadd' action for TVDB ID {tvdb_id}")
    
    return JSONResponse({"status": "success", "message": "SeriesAdd scheduled"})

def handle_episodefiledelete(data: dict, is_4k: bool = False):
    # Similar to seriesadd: recreate dummy for episode deletion.
    series = data.get('series', {})
    episodes = data.get('episodes', [])
    series_title = series.get('title', 'Unknown Series')
    series_year = series.get('year')
    tvdb_id = series.get('tvdbId')
    episode_file_path = data.get("episodeFile", {}).get("path")

    # season_num = next(ep.get('seasonNumber'))
    # episode_num = next(ep.get('episodeNumber'))
    # episode_title = next(ep.get('title'))
    
    session = get_session()
    repo = SeriesRepository(session)
    series = repo.get_by_tvdbid(tvdb_id, is_4k)
    if not series:
        logger.info("Series not found in database", extra={'emoji_type': 'warning'})
        return JSONResponse({"status": "error", "message": "Series not found"}, status_code=404)
    repo.delete_seasons_and_episodes(series, episodes)
    logger.info(f"Episode data updated in db for series {series_title}", extra={'emoji_type':'success'})
    job_scheduled = handle_episodefiledelete_scheduler.enqueue(series)
    if job_scheduled:
        logger.info(f"Enqueued 'handle_episodefiledelete' action for TVDB ID {tvdb_id}")
    else:
        logger.warning(f"Failed to enqueue 'handle_episodefiledelete' action for TVDB ID {tvdb_id}")

    logger.info(f"Re-created {len(episodes)} placeholder files for '{series_title}'", extra={'emoji_type': 'create'})
    return JSONResponse({"status": "success", "message": "EpisodeFileDelete processed"})

def handle_moviefiledelete(data: dict, is_4k):
    if 'movie' in data:
        movie = data.get('movie', {})
        tmdb_id = movie.get('tmdbId') or data.get('remoteMovie', {}).get('tmdbId')
        if not tmdb_id:
            logger.error("Missing TMDB ID for movie file delete", extra={'emoji_type': 'error'})
            return JSONResponse({"status": "error"}, status_code=400)
        title = movie.get('title', 'Unknown Movie')
        year = movie.get('year')
        movie_file_path = data.get("movieFile", {}).get("path")

        session = get_session()
        repo = MovieRepository(session)
        movie = repo.get_by_tmdbid(tmdb_id, is_4k)
        if movie:
            job_scheduled = handle_moviefiledelete_scheduler.enqueue(movie)
            if job_scheduled:
                logger.info(f"Enqueued 'handle_moviefiledelete' action for TMDB ID {movie.tmdbid}")
            else:
                logger.warning(f"Failed to enqueue 'handle_moviefiledelete' action for TMDB ID {movie.tmdbid}")

        else:
            logger.info("Movie not found!", extra={'emoji_type':'warning'})

    return JSONResponse({"status": "success", "message": "MovieFileDelete processed"})

def handle_movie_delete(data: dict, is_4k: bool = False):
    if 'movie' in data:
        movie = data.get('movie', {})
        tmdb_id = movie.get('tmdbId') or data.get('remoteMovie', {}).get('tmdbId')
        if not tmdb_id:
            logger.error("Missing TMDB ID for movie delete", extra={'emoji_type': 'error'})
            return JSONResponse({"status": "error"}, status_code=400)
        title = movie.get('title', 'Unknown Movie')
        year = movie.get('year')

        session = get_session()
        repo = MovieRepository(session)
        movie = repo.get_by_tmdbid(tmdb_id, is_4k)
        if movie:
            job_scheduled = handle_movie_delete_scheduler.enqueue(movie)
            if job_scheduled:
                logger.info(f"Enqueued 'handle_movie_delete' action for TMDB ID {movie.tmdbid}")
            else:
                logger.warning(f"Failed to enqueue 'handle_movie_delete' action for TMDB ID {movie.tmdbid}")

        else:
            logger.info("Movie not found!", extra={'emoji_type':'warning'})

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

        session = get_session()
        repo = MovieRepository(session)
        m = repo.get_by_tmdbid(tmdb_id, is_4k)
        if not m:
            m = repo.add(
                title=title, year=year,
                tmdbid=tmdb_id, dummypath="",
                radarrpath=movie_path, radarrid=radarr_id, status="PENDING", is_4k=is_4k
            )
            print("Added:", m)
        else:
            repo.is_deleted(m, False)
            print("Already present")        
        job_scheduled = handle_movieadd_scheduler.enqueue(m)
        if job_scheduled:
            logger.info(f"Enqueued 'handle_movieadd' action for TMDB ID {m.tmdbid}")
        else:
            logger.warning(f"Failed to enqueue 'handle_movieadd' action for TMDB ID {m.tmdbid}")

        return JSONResponse({"status": "success", "message": "MovieAdd scheduled"})

    return JSONResponse({"status": "success", "message": "MovieAdd processed"})

def handle_seriesdelete(data: dict, is_4k: bool = False):
    """Delete placeholder files when a series is deleted from Sonarr"""
    if 'series' in data:
        series = data.get('series', {})
        tvdb_id = series.get('tvdbId')
        title = series.get('title', 'Unknown Series')
        year = series.get('year')
        
        if tvdb_id:
            # Construct folder path using get_folder_path for consistency
            session = get_session()
            repo = SeriesRepository(session)
            series = repo.get_by_tvdbid(tvdb_id, is_4k)
            if not series:
                logger.info(f"Series {title} not found in database for deletion", extra={'emoji_type': 'warning'}) 
                return JSONResponse({"status": "success", "message": "Series not tracked, no cleanup needed"})
            if repo.get_by_tvdbid(tvdb_id):
                job_scheduled = handle_seriesdelete_scheduler.enqueue(series)
                if job_scheduled:
                    logger.info(f"Enqueued 'handle_seriesdelete' action for TVDB ID {series.tvdbid}")
                else:
                    logger.warning(f"Failed to enqueue 'handle_seriesdelete' action for TVDB ID {series.tvdbid}")

            else:
                logger.info("Series not found!", extra={'emoji_type':'warning'})
        else:
            logger.info("Series not found or invalid tvdbid!", extra={'emoji_type':'warning'})

    return JSONResponse({"status": "success", "message": "SeriesDelete processed"})

def handle_playback(data: dict):
    """
    Enqueue the 'playback' flow for movies or episodes when a playback event is received.
    """
    # 1) Determine file_path
    notification = data.get("NotificationType")
    if notification:
        file_path = get_jellyfin_file_path(data.get("ItemId"), data.get("UserId"))
    else:
        file_path = (data.get("media") or {}).get("file_info", {}).get("path", "")

    if not file_path:
        logger.error("Playback payload missing file path", extra={"emoji_type": "error"})
        return JSONResponse({"status": "error", "message": "Missing file path"}, status_code=400)

    # 2) Ignore if not in placeholder folders
    folders = [settings.MOVIE_LIBRARY_FOLDER, settings.TV_LIBRARY_FOLDER]
    if settings.MOVIE_LIBRARY_4K_FOLDER: folders.append(settings.MOVIE_LIBRARY_4K_FOLDER)
    if settings.TV_LIBRARY_4K_FOLDER:    folders.append(settings.TV_LIBRARY_4K_FOLDER)
    if not any(file_path.startswith(f) for f in folders if f):
        logger.info(f"Ignored real playback: {file_path}", extra={"emoji_type": "info"})
        return JSONResponse({"status": "ignored", "message": "Not placeholder path"})

    is_4k = is_4k_request(file_path)
    media = data.get("media") or {}
    media_type = (media.get("type") or data.get("ItemType", "")).lower()

    session = get_session()
    try:
        if media_type == "movie":
            tmdb = media.get("ids", {}).get("tmdb") or data.get("Provider_tmdb")
            repo = MovieRepository(session)
            movie = repo.get_by_tmdbid(tmdb, is_4k) if tmdb else repo.get_by_path(file_path)
            if not movie:
                logger.error(f"No DB Movie for path {file_path}")
                return JSONResponse({"status": "error", "message": "Movie not found"}, status_code=404)
            playback_scheduler.enqueue(movie)

        elif media_type == "episode":
            # extract series, season & episode numbers
            series_tvdb   = media.get("ids", {}).get("tvdb") or data.get("Provider_tvdb")
            season_number = int(media.get("season_num")  or data.get("SeasonNumber", 0))
            episode_number= int(media.get("episode_num") or data.get("EpisodeNumber", 0))
            repo = SeriesRepository(session)
            series = repo.get_by_tvdbid(series_tvdb, is_4k)
            if not series:
                logger.error(f"No DB Series for TVDB {series_tvdb}")
                return JSONResponse({"status": "error", "message": "Series not found"}, status_code=404)

            # find matching Episode under that series
            ep = repo.get_ep_by_series(series, season_number, episode_number)
            if not ep:
                logger.error(f"No DB Episode for {series.title} S{season_number}E{episode_number}")
                return JSONResponse({"status": "error", "message": "Episode not found"}, status_code=404)

            playback_scheduler.enqueue(ep)

        else:
            logger.warning(f"Unsupported media type: {media_type}")
            return JSONResponse({"status": "error", "message": "Unsupported media type"}, status_code=400)

    except Exception as e:
        logger.error(f"Error in handle_playback: {e}")
        return JSONResponse({"status": "error", "message": "Internal error"}, status_code=500)
    finally:
        session.close()

    return JSONResponse({"status": "scheduled", "message": f"{media_type} playback enqueued"})