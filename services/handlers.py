import os, re, threading, time, shutil, requests
from fastapi.responses import JSONResponse
from core.config import settings
from core.logger import logger
from core.handler_logging import start_handler_logging, end_handler_logging
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
        if 'movie' in data:
            # Movie import handling
            movie = data['movie']
            tmdb_id = movie.get('tmdbId')
            radarr_id = movie.get('id')  # Get Radarr movie ID
            title = movie.get('title', 'Unknown Movie')
            year = movie.get('year')
            
            if not tmdb_id:
                logger.error("Missing TMDB ID for movie import", extra={'emoji_type': 'error'})
                end_handler_logging(session_id, success=False, 
                                   summary="Missing TMDB ID for movie import")
                return JSONResponse({"status": "error", "message": "Missing TMDB ID"}, status_code=400)
            
            logger.info(f"Processing movie import cleanup for: {title}", extra={'emoji_type': 'cleanup'})
            
            session = get_session()
            repo = MovieRepository(session)
            
            movie = repo.get_by_tmdbid(tmdb_id, is_4k)
            if not movie:
                logger.warning(f"Movie {title} not found in database for import event", extra={'emoji_type': 'warning'})
                return JSONResponse({"status": "success", "message": "Movie not tracked, no cleanup needed"})
            
            # Update radarrid if it changed
            if radarr_id and movie.radarrid != radarr_id:
                old_id = movie.radarrid
                movie.radarrid = radarr_id
                logger.info(f"Updated movie Radarr ID from {old_id} to {radarr_id}", extra={'emoji_type': 'update'})
            
            if 'movieFile' not in data or not data.get('movieFile') or 'path' not in data.get('movieFile'):
                logger.warning(f"Missing movie file path in import data for {title}", extra={'emoji_type': 'warning'})
                return JSONResponse({"status": "error", "message": "Missing movie file path"}, status_code=400)
            else:
                movie.filepath = data.get('movieFile').get('path')
            # Don't change the movie's action - it should keep its original action (e.g., handle_movieadd)
            # Only reset status to PENDING if it's not already being processed
            if movie.status not in ['PENDING', 'QUEUED']:
                movie.status = 'PENDING'
            session.commit()
            
            job_scheduled = handle_import_event_scheduler.enqueue(movie)
            if job_scheduled:
                logger.info(f"Enqueued 'handle_import_event' action for TMDB ID {tmdb_id}", extra={'emoji_type': 'queue'})
                # Note: Don't end logging session here - scheduler will continue processing
                session.close()
                return JSONResponse({"status": "success", "message": "Movie import cleanup scheduled"})
            else:
                logger.warning(f"Failed to enqueue 'handle_import_event' action for TMDB ID {tmdb_id}", extra={'emoji_type': 'warning'})
                end_handler_logging(session_id, success=False, 
                                   summary="Failed to enqueue movie import processing")
                session.close()
                return JSONResponse({"status": "error", "message": "Failed to schedule processing"}, status_code=500)

        elif 'episodes' in data and 'series' in data:
            series = data['series']
            episode = data['episodes'][0]  # Handle first episode in the list
            
            tvdb_id = series.get('tvdbId')
            series_id = series.get('id')  # Get Sonarr series ID
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
            
            series_entity = repo.get_by_series_tvdbid(tvdb_id, is_4k)
            if not series_entity:
                logger.warning(f"Series {series_title} not found in database for import event", extra={'emoji_type': 'warning'})
                end_handler_logging(session_id, success=True, 
                                   summary="Series not tracked, no cleanup needed")
                return JSONResponse({"status": "success", "message": "Series not tracked, no cleanup needed"})
            
            # Update sonarrid if it changed
            if series_id and series_entity.sonarrid != series_id:
                old_id = series_entity.sonarrid
                series_entity.sonarrid = series_id
                session.commit()
                logger.info(f"Updated series Sonarr ID from {old_id} to {series_id}", extra={'emoji_type': 'update'})
            
            ep = repo.get_ep_by_series(series_entity, season_num, episode_num)
            if ep:
                if 'episodeFile' not in data or not data.get('episodeFile') or 'path' not in data.get('episodeFile'):
                    logger.warning(f"Missing episode file path in import data for {full_title}", extra={'emoji_type': 'warning'})
                    return JSONResponse({"status": "error", "message": "Missing episode file path"}, status_code=400)
                else:
                    ep.filepath = data.get('episodeFile').get('path')
                ep.status = 'PENDING'
                session.commit()

                job_scheduled = handle_import_event_scheduler.enqueue(ep)
                if job_scheduled:
                    logger.info(f"Enqueued 'handle_import_event' action for episode {full_title}", extra={'emoji_type': 'queue'})
                    # Note: Don't end logging session here - scheduler will continue processing
                    session.close()
                    return JSONResponse({"status": "success", "message": "Episode import cleanup scheduled"})
                else:
                    logger.warning(f"Failed to enqueue 'handle_import_event' action for episode {full_title}", extra={'emoji_type': 'warning'})
                    end_handler_logging(session_id, success=False, 
                                       summary="Failed to enqueue episode import processing")
                    session.close()
                    return JSONResponse({"status": "error", "message": "Failed to schedule processing"}, status_code=500)
            else:
                logger.warning(f"Episode {full_title} not found in database", extra={'emoji_type': 'warning'})
                end_handler_logging(session_id, success=True, 
                                   summary="Episode not tracked, no cleanup needed")
                session.close()
                return JSONResponse({"status": "success", "message": "Episode not tracked, no cleanup needed"})

    except Exception as e:
        logger.error(f"Import event scheduling failed: {e}", extra={'emoji_type': 'error'})
        end_handler_logging(session_id, success=False, 
                           summary=f"Handler failed: {e}")
        return JSONResponse({"status": "error", "message": f"Error: {str(e)}"}, status_code=500)

    end_handler_logging(session_id, success=True, 
                       summary="Import cleanup completed")
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
    
    # Start handler logging session - we'll get the series DB ID later
    temp_session_id = start_handler_logging(
        'handle_seriesadd', 
        tvdb_id,  # Use tvdb_id as identifier for now
        'series',
        title=series_title,
        year=series_year,
        tvdb_id=tvdb_id,
        is_4k=is_4k,
        episode_count=len(episodes)
    )
    
    logger.info(f"📺 Processing SeriesAdd webhook for '{series_title}' ({series_year}) - TVDB: {tvdb_id}, Episodes: {len(episodes)}", extra={'emoji_type': 'webhook'})
    
    try:
        session = get_session()
        repo = SeriesRepository(session)
        
        series = repo.get_by_series_tvdbid(tvdb_id, is_4k)
        if not series:
            # Create the Series record using tvdbid (Sonarr provides tvdbId)
            try:
                series = repo.add(
                    title=series_title,
                    year=series_year or 0,
                    tvdbid=tvdb_id,
                    is_4k=is_4k,
                    dummypath="",
                    filepath=series_path,
                    sonarrid=series_id,
                    status="PENDING"
                )
                logger.info(f"Series added: {series}", extra={'emoji_type': 'success'})
            except ValueError as ve:
                logger.error(f"Series creation failed: {ve}", extra={'emoji_type': 'error'})
                end_handler_logging(temp_session_id, success=False, 
                                   summary=f"Series creation failed: {ve}")
                return JSONResponse({"status": "error", "message": str(ve)}, status_code=400)
        else:
            logger.info("Series already present", extra={'emoji_type':'warning'})
            repo.is_deleted(series, False)
            # Update sonarrid if it changed (Sonarr IDs can change if series was deleted and re-added)
            if series.sonarrid != series_id:
                old_id = series.sonarrid
                series.sonarrid = series_id
                session.commit()
                logger.info(f"Updated series Sonarr ID from {old_id} to {series_id}", extra={'emoji_type': 'update'})
            if series.filepath != series_path:
                old_path = series.filepath
                series.filepath = series_path
                session.commit()
                logger.info(f"Updated series filepath from {old_path} to {series_path}", extra={'emoji_type': 'update'})
        # Ensure we pass the Series instance (newly created or existing) to season/episode logic
        repo.add_missing_seasons_and_episodes(series, episodes)
        logger.info(f"Episode data inserted to db for series {series_title}", extra={'emoji_type':'success'})
        
        # Pass the model instance so the scheduler can infer model type and create subflows
        job_scheduled = handle_seriesadd_scheduler.enqueue(series)
        
        session.close()
        
        if job_scheduled:
            logger.info(f"Enqueued 'handle_seriesadd' action for TVDB ID {tvdb_id}")
            # Note: We don't end the logging session here because the scheduler will continue processing
            # The scheduler should end the session when all episodes are done
            return JSONResponse({"status": "success", "message": "SeriesAdd scheduled"})
        else:
            logger.warning(f"Failed to enqueue 'handle_seriesadd' action for TVDB ID {tvdb_id}")
            end_handler_logging(temp_session_id, success=False, 
                               summary="Failed to enqueue handler")
            return JSONResponse({"status": "error", "message": "Failed to schedule processing"})
            
    except Exception as e:
        logger.error(f"handle_seriesadd failed: {e}", extra={'emoji_type': 'error'})
        end_handler_logging(temp_session_id, success=False, 
                           summary=f"Handler failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
    finally:
        try:
            if 'session' in locals():
                session.close()
        except Exception:
            pass

def handle_episodefiledelete(data: dict, is_4k: bool = False):
    # Similar to seriesadd: recreate dummy for episode deletion.
    series = data.get('series', {})
    episodes = data.get('episodes', [])
    series_title = series.get('title', 'Unknown Series')
    series_year = series.get('year')
    tvdb_id = series.get('tvdbId')
    series_id = series.get('id')  # Get Sonarr series ID
    episode_file_path = data.get("episodeFile", {}).get("path")
    
    # Start handler logging session
    session_id = start_handler_logging(
        'handle_episodefiledelete',
        tvdb_id,
        'series',
        title=series_title,
        year=series_year,
        tvdb_id=tvdb_id,
        is_4k=is_4k,
        episode_count=len(episodes),
        episode_file_path=episode_file_path
    )
    
    logger.info(f"🗑️ Processing EpisodeFileDelete webhook for '{series_title}' - TVDB: {tvdb_id}, File: {episode_file_path}", extra={'emoji_type': 'webhook'})

    # season_num = next(ep.get('seasonNumber'))
    # episode_num = next(ep.get('episodeNumber'))
    # episode_title = next(ep.get('title'))
    
    try:
        session = get_session()
        repo = SeriesRepository(session)
        series = repo.get_by_series_tvdbid(tvdb_id, is_4k)
        if not series:
            logger.info("Series not found in database", extra={'emoji_type': 'warning'})
            end_handler_logging(session_id, success=False, 
                               summary="Series not found in database")
            return JSONResponse({"status": "error", "message": "Series not found"}, status_code=404)
        
        # Update sonarrid if it changed
        if series_id and series.sonarrid != series_id:
            old_id = series.sonarrid
            series.sonarrid = series_id
            session.commit()
            logger.info(f"Updated series Sonarr ID from {old_id} to {series_id}", extra={'emoji_type': 'update'})
        
        repo.delete_seasons_and_episodes(series, episodes)
        logger.info(f"Episode data updated in db for series {series_title}", extra={'emoji_type':'success'})
        job_scheduled = handle_episodefiledelete_scheduler.enqueue(series)
        if job_scheduled:
            logger.info(f"Enqueued 'handle_episodefiledelete' action for TVDB ID {tvdb_id}")
            logger.info(f"Re-created {len(episodes)} placeholder files for '{series_title}'", extra={'emoji_type': 'create'})
            # Note: Don't end logging session here - scheduler will continue processing
            return JSONResponse({"status": "success", "message": "EpisodeFileDelete processed"})
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
        movie_file_path = data.get("movieFile", {}).get("path")

        # Start handler logging session
        session_id = start_handler_logging(
            'handle_moviefiledelete',
            tmdb_id,
            'movie', 
            title=title,
            year=year,
            tmdb_id=tmdb_id,
            is_4k=is_4k,
            movie_file_path=movie_file_path
        )
        
        logger.info(f"🗑️ Processing MovieFileDelete webhook for '{title}' ({year}) - TMDB: {tmdb_id}, File: {movie_file_path}", extra={'emoji_type': 'webhook'})

        try:
            session = get_session()
            repo = MovieRepository(session)
            movie = repo.get_by_tmdbid(tmdb_id, is_4k)
            if movie:
                # Update radarrid if it changed
                if radarr_id and movie.radarrid != radarr_id:
                    old_id = movie.radarrid
                    movie.radarrid = radarr_id
                    session.commit()
                    logger.info(f"Updated movie Radarr ID from {old_id} to {radarr_id}", extra={'emoji_type': 'update'})
                
                job_scheduled = handle_moviefiledelete_scheduler.enqueue(movie)
                if job_scheduled:
                    logger.info(f"Enqueued 'handle_moviefiledelete' action for TMDB ID {movie.tmdbid}")
                    # Note: Don't end logging session here - scheduler will continue processing
                    return JSONResponse({"status": "success", "message": "MovieFileDelete processed"})
                else:
                    logger.warning(f"Failed to enqueue 'handle_moviefiledelete' action for TMDB ID {movie.tmdbid}")
                    end_handler_logging(session_id, success=False, 
                                       summary="Failed to enqueue moviefiledelete processing")
                    return JSONResponse({"status": "error", "message": "Failed to schedule processing"}, status_code=500)
            else:
                logger.info("Movie not found!", extra={'emoji_type':'warning'})
                end_handler_logging(session_id, success=False, 
                                   summary="Movie not found in database")
                return JSONResponse({"status": "error", "message": "Movie not found"}, status_code=404)

        except Exception as e:
            logger.error(f"handle_moviefiledelete failed: {e}", extra={'emoji_type': 'error'})
            end_handler_logging(session_id, success=False, 
                               summary=f"Handler failed: {e}")
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
        finally:
            try:
                if 'session' in locals():
                    session.close()
            except Exception:
                pass

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

        # Start handler logging session
        session_id = start_handler_logging(
            'handle_movie_delete',
            tmdb_id,
            'movie',
            title=title,
            year=year,
            tmdb_id=tmdb_id,
            is_4k=is_4k
        )
        
        logger.info(f"🗑️ Processing MovieDelete webhook for '{title}' ({year}) - TMDB: {tmdb_id}", extra={'emoji_type': 'webhook'})

        try:
            session = get_session()
            repo = MovieRepository(session)
            movie = repo.get_by_tmdbid(tmdb_id, is_4k)
            if movie:
                # Update radarrid if it changed
                if radarr_id and movie.radarrid != radarr_id:
                    old_id = movie.radarrid
                    movie.radarrid = radarr_id
                    session.commit()
                    logger.info(f"Updated movie Radarr ID from {old_id} to {radarr_id}", extra={'emoji_type': 'update'})
                
                job_scheduled = handle_movie_delete_scheduler.enqueue(movie)
                if job_scheduled:
                    logger.info(f"Enqueued 'handle_movie_delete' action for TMDB ID {movie.tmdbid}")
                    # Note: Don't end logging session here - scheduler will continue processing
                    return JSONResponse({"status": "success", "message": "MovieDelete processed"})
                else:
                    logger.warning(f"Failed to enqueue 'handle_movie_delete' action for TMDB ID {movie.tmdbid}")
                    end_handler_logging(session_id, success=False, 
                                       summary="Failed to enqueue movie delete processing")
                    return JSONResponse({"status": "error", "message": "Failed to schedule processing"}, status_code=500)
            else:
                logger.info("Movie not found!", extra={'emoji_type':'warning'})
                end_handler_logging(session_id, success=False, 
                                   summary="Movie not found in database")
                return JSONResponse({"status": "error", "message": "Movie not found"}, status_code=404)

        except Exception as e:
            logger.error(f"handle_movie_delete failed: {e}", extra={'emoji_type': 'error'})
            end_handler_logging(session_id, success=False, 
                               summary=f"Handler failed: {e}")
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
        finally:
            try:
                if 'session' in locals():
                    session.close()
            except Exception:
                pass

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

        job_scheduled = handle_movieadd_scheduler.enqueue(m)
        session.close()
        
        if job_scheduled:
            logger.info(f"Enqueued 'handle_movieadd' action for TMDB ID {m.tmdbid}")
            # Note: Session will be ended by scheduler when movie processing completes
            return JSONResponse({"status": "success", "message": "MovieAdd scheduled"})
        else:
            logger.warning(f"Failed to enqueue 'handle_movieadd' action for TMDB ID {m.tmdbid}")
            end_handler_logging(session_id, success=False, summary="Failed to enqueue movie processing")
            return JSONResponse({"status": "error", "message": "Failed to schedule processing"})

    return JSONResponse({"status": "success", "message": "MovieAdd processed"})

def handle_seriesdelete(data: dict, is_4k: bool = False):
    """Delete placeholder files when a series is deleted from Sonarr"""
    if 'series' in data:
        series = data.get('series', {})
        tvdb_id = series.get('tvdbId')
        title = series.get('title', 'Unknown Series')
        year = series.get('year')
        
        # Start handler logging session
        session_id = start_handler_logging(
            'handle_seriesdelete',
            tvdb_id,
            'series',
            title=title,
            year=year,
            tvdb_id=tvdb_id,
            is_4k=is_4k
        )
        
        logger.info(f"🗑️ Processing SeriesDelete webhook for '{title}' ({year}) - TVDB: {tvdb_id}", extra={'emoji_type': 'webhook'})
        
        if tvdb_id:
            try:
                # Construct folder path using get_folder_path for consistency
                session = get_session()
                repo = SeriesRepository(session)
                series = repo.get_by_series_tvdbid(tvdb_id, is_4k)
                if not series:
                    logger.info(f"Series {title} not found in database for deletion", extra={'emoji_type': 'warning'}) 
                    end_handler_logging(session_id, success=True, 
                                       summary="Series not tracked, no cleanup needed")
                    return JSONResponse({"status": "success", "message": "Series not tracked, no cleanup needed"})
                
                job_scheduled = handle_seriesdelete_scheduler.enqueue(series)
                if job_scheduled:
                    logger.info(f"Enqueued 'handle_seriesdelete' action for TVDB ID {series.tvdbid}")
                    # Note: Don't end logging session here - scheduler will continue processing
                    return JSONResponse({"status": "success", "message": "SeriesDelete processed"})
                else:
                    logger.warning(f"Failed to enqueue 'handle_seriesdelete' action for TVDB ID {series.tvdbid}")
                    end_handler_logging(session_id, success=False, 
                                       summary="Failed to enqueue series delete processing")
                    return JSONResponse({"status": "error", "message": "Failed to schedule processing"}, status_code=500)

            except Exception as e:
                logger.error(f"handle_seriesdelete failed: {e}", extra={'emoji_type': 'error'})
                end_handler_logging(session_id, success=False, 
                                   summary=f"Handler failed: {e}")
                return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
            finally:
                try:
                    if 'session' in locals():
                        session.close()
                except Exception:
                    pass
        else:
            logger.info("Series not found or invalid tvdbid!", extra={'emoji_type':'warning'})
            end_handler_logging(session_id, success=False, 
                               summary="Invalid or missing TVDB ID")
            return JSONResponse({"status": "error", "message": "Invalid or missing TVDB ID"}, status_code=400)

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

    # Start handler logging session
    session_id = start_handler_logging(
        'handle_playback',  # Keep as handle_playback to match your existing directory
        0,  # No specific ID available at this point
        'playback',
        file_path=file_path,
        notification_type=notification
    )
    
    logger.info(f"▶️ Processing Playback webhook - File: {file_path}, Type: {notification}", extra={'emoji_type': 'webhook'})

    # 2) Ignore if not in placeholder folders
    folders = [settings.DUMMY_MOVIE_LIBRARY_FOLDER, settings.DUMMY_TV_LIBRARY_FOLDER]
    if settings.DUMMY_MOVIE_LIBRARY_4K_FOLDER: folders.append(settings.DUMMY_MOVIE_LIBRARY_4K_FOLDER)
    if settings.DUMMY_TV_LIBRARY_4K_FOLDER:    folders.append(settings.DUMMY_TV_LIBRARY_4K_FOLDER)
    if not any(file_path.startswith(f) for f in folders if f):
        logger.info(f"Ignored real playback: {file_path}", extra={"emoji_type": "info"})
        end_handler_logging(session_id, success=True, 
                           summary="Ignored real playback - not placeholder path")
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