from typing import Dict, Any
from fastapi.responses import JSONResponse

from core.logger import logger
from services.utils import is_4k_request
from services.progressarr.queue import start_queue_monitor

def handle_grab(data: Dict[Any, Any]) -> JSONResponse:
    """Handle grab events for download status tracking"""
    try:
        # Determine if this is a 4K grab
        is_4k = is_4k_request(data.get("downloadClient", {}).get("downloadPath", ""))
        
        # Handle movie grab
        if "remoteMovie" in data:
            movie_info = data.get("remoteMovie", {})
            tmdb_id = movie_info.get("tmdbId")
            title = movie_info.get("title", "Unknown Movie")
            
            if not tmdb_id:
                return JSONResponse({"status": "error", "message": "Missing TMDB ID"}, status_code=400)
                
            # Find movie in Plex
            from services.plex_client import find_movie_by_tmdb_id
            rating_key = find_movie_by_tmdb_id(tmdb_id)
            
            if not rating_key:
                logger.debug(f"Movie not found in Plex: {title} (tmdb-{tmdb_id})", extra={'emoji_type': 'debug'})
                return JSONResponse({"status": "warning", "message": "Movie not found in Plex"})
                
            # Update status to "Searching..."
            from services.progressarr.status import schedule_status_update
            schedule_status_update(rating_key, title, "Searching", None)
            
            # Start queue monitoring
            # Get movie ID from Radarr
            movie_id = get_movie_id_from_radarr(tmdb_id, is_4k)
            if movie_id:
                start_queue_monitor(movie_id, "movie", is_4k)
            
        # Handle episode grab
        elif "remoteEpisode" in data:
            episode_info = data.get("remoteEpisode", {})
            series_info = data.get("series", {})
            
            # We'll implement episode monitoring later
            
        return JSONResponse({"status": "success", "message": "Grab event processed"})
        
    except Exception as e:
        logger.error(f"Error handling grab event: {e}", extra={'emoji_type': 'error'})
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

def get_movie_id_from_radarr(tmdb_id: int, is_4k: bool = False) -> int:
    """Get movie ID from Radarr by TMDB ID"""
    try:
        import requests
        from core.config import settings
        
        # Determine which Radarr instance to use
        radarr_url = settings.RADARR_4K_URL if is_4k else settings.RADARR_URL
        radarr_api_key = settings.RADARR_4K_API_KEY if is_4k else settings.RADARR_API_KEY
        
        # Look up the movie in Radarr
        response = requests.get(
            f"{radarr_url}/api/v3/movie", 
            headers={"X-Api-Key": radarr_api_key}
        )
        response.raise_for_status()
        
        # Find the movie with matching TMDB ID
        for movie in response.json():
            if movie.get("tmdbId") == int(tmdb_id):
                return movie.get("id")
                
        logger.warning(f"Movie with TMDB ID {tmdb_id} not found in Radarr", extra={"emoji_type": "warning"})
        return None
        
    except Exception as e:
        logger.error(f"Failed to get movie ID from Radarr: {e}", extra={"emoji_type": "error"})
        return None

def handle_download(data: Dict[Any, Any]) -> JSONResponse:
    """Handle download events for status tracking"""
    try:
        # Determine if this is a 4K download
        is_4k = False
        if "movie" in data:
            movie_path = data.get("movie", {}).get("folderPath", "")
            is_4k = is_4k_request(movie_path)
        elif "series" in data:
            series_path = data.get("series", {}).get("path", "")
            is_4k = is_4k_request(series_path)
            
        # Process download event to update status
        # We'll implement this later
        
        return JSONResponse({"status": "success", "message": "Download event processed"})
        
    except Exception as e:
        logger.error(f"Error handling download event: {e}", extra={'emoji_type': 'error'})
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)