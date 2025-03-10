import time
import requests
import threading
from typing import Dict, List, Optional, Any, Tuple

from core.logger import logger
from core.config import settings
from shared.utils import get_arr_config

# Download status tracking
DOWNLOAD_STATUSES = {}  # Format: "tmdbId" -> {"status": "string", "percentage": int}

def check_download_queue(download_type: str, media_id: Optional[int] = None, is_4k: bool = False) -> Dict:
    """
    Check download queue for specific media or all media
    
    Args:
        download_type: "movie" or "tv"
        media_id: Optional ID of specific media to check
        is_4k: Whether to check 4K instance
        
    Returns:
        Dict with queue status info
    """
    try:
        config = get_arr_config(download_type, is_4k)
        
        queue_response = requests.get(
            f"{config['url']}/queue", 
            params={"pageSize": 100}, 
            headers={"X-Api-Key": config["api_key"]}
        )
        
        if queue_response.status_code != 200:
            logger.error(f"Failed to get queue: {queue_response.status_code}", extra={'emoji_type': 'error'})
            return {}
            
        queue = queue_response.json()
        records = queue.get("records", [])
        
        if media_id is not None:
            # Filter for specific media ID
            records = [r for r in records if 
                      (download_type == "movie" and r.get("movieId") == media_id) or 
                      (download_type == "tv" and r.get("seriesId") == media_id)]
            
        # Process queue items
        for item in records:
            process_queue_item(item, download_type, is_4k)
            
        return {"total": len(records), "records": records}
        
    except Exception as e:
        logger.error(f"Error checking download queue: {e}", extra={'emoji_type': 'error'})
        return {}

def extract_download_info(queue_item: Dict) -> Tuple[str, int, Optional[int]]:
    """
    Extract download status and percentage from queue item
    
    Args:
        queue_item: Queue item from Arr API
        
    Returns:
        Tuple of (status string, percentage, ETA in minutes)
    """
    status = queue_item.get("status", "Unknown")
    size_total = queue_item.get("size", 0)
    size_remaining = queue_item.get("sizeleft", 0)
    
    # Calculate percentage
    if size_total > 0 and size_remaining <= size_total:
        percentage = int(((size_total - size_remaining) / size_total) * 100)
    else:
        percentage = 0
        
    # Calculate ETA
    eta_minutes = None
    if "timeLeft" in queue_item and queue_item["timeLeft"]:
        eta_str = queue_item["timeLeft"]
        try:
            # Parse Arr timeLeft format (e.g., "00:23:15")
            hours, minutes, seconds = map(int, eta_str.split(':'))
            eta_minutes = hours * 60 + minutes
        except:
            pass
            
    return status, percentage, eta_minutes

def process_queue_item(queue_item: Dict, media_type: str, is_4k: bool = False) -> None:
    """
    Process a queue item and update status
    
    Args:
        queue_item: Queue item from Arr API
        media_type: "movie" or "tv"
        is_4k: Whether this is a 4K download
        
    Returns:
        None
    """
    try:
        from services.progressarr.status import schedule_status_update, PROGRESS_FLAGS
        
        # Extract media info
        if media_type == "movie":
            media_id = queue_item.get("movieId")
            if not media_id:
                return
                
            # Get movie details
            config = get_arr_config("movie", is_4k)
            movie_response = requests.get(
                f"{config['url']}/movie/{media_id}",
                headers={"X-Api-Key": config["api_key"]}
            )
            
            if movie_response.status_code != 200:
                return
                
            movie = movie_response.json()
            title = movie.get("title", "Unknown")
            tmdb_id = movie.get("tmdbId")
            
            # Find in Plex
            from services.placeholdarr.plex import find_movie_by_tmdb_id
            rating_key = find_movie_by_tmdb_id(tmdb_id)
            if not rating_key:
                return
                
        elif media_type == "tv":
            # Handle TV episodes
            episode_ids = queue_item.get("episodeIds", [])
            if not episode_ids:
                return
                
            # Get episode details
            config = get_arr_config("tv", is_4k)
            
            # For simplicity, just use the first episode in the queue item
            episode_id = episode_ids[0]
            episode_response = requests.get(
                f"{config['url']}/episode/{episode_id}",
                headers={"X-Api-Key": config["api_key"]}
            )
            
            if episode_response.status_code != 200:
                return
                
            episode = episode_response.json()
            series_id = episode.get("seriesId")
            season_number = episode.get("seasonNumber")
            episode_number = episode.get("episodeNumber")
            
            # Get series info
            series_response = requests.get(
                f"{config['url']}/series/{series_id}",
                headers={"X-Api-Key": config["api_key"]}
            )
            
            if series_response.status_code != 200:
                return
                
            series = series_response.json()
            title = episode.get("title", f"Episode {episode_number}")
            tvdb_id = series.get("tvdbId")
            
            # Find in Plex
            from services.placeholdarr.plex import find_episode_by_tvdb_id
            rating_key = find_episode_by_tvdb_id(tvdb_id, season_number, episode_number)
            if not rating_key:
                return
        else:
            return  # Unsupported media type
            
        # Extract download info
        status, percentage, eta = extract_download_info(queue_item)
        
        # Format status string
        status_str = get_status_string(status, percentage, eta)
        
        # Schedule status update if changed
        current_status = PROGRESS_FLAGS.get(rating_key, {})
        if current_status.get("percentage", -1) != percentage or current_status.get("status") != status_str:
            PROGRESS_FLAGS[rating_key] = {"status": status_str, "percentage": percentage}
            schedule_status_update(rating_key, title, status_str, percentage)
            
    except Exception as e:
        logger.error(f"Error processing queue item: {e}", extra={'emoji_type': 'error'})

def get_status_string(status: str, percentage: int, eta_minutes: Optional[int] = None) -> str:
    """
    Format a status string based on download status
    
    Args:
        status: Status string from queue
        percentage: Download percentage
        eta_minutes: Estimated time remaining in minutes
        
    Returns:
        str: Formatted status string
    """
    if status.lower() in ("downloading", "download"):
        if eta_minutes is not None and eta_minutes > 0:
            if eta_minutes > 60:
                hours = eta_minutes // 60
                mins = eta_minutes % 60
                return f"Downloading {percentage}% ({hours}h {mins}m)"
            else:
                return f"Downloading {percentage}% ({eta_minutes}m)"
        else:
            return f"Downloading {percentage}%"
            
    elif status.lower() in ("queued", "delay"):
        return "Queued"
        
    elif status.lower() in ("warning", "error"):
        return f"Download Error"
        
    else:
        return f"{status} {percentage}%"

def start_queue_monitor(media_id: int, media_type: str, is_4k: bool = False, interval: int = 30, 
                      max_checks: int = 60) -> None:
    """
    Start periodic monitoring of download queue
    
    Args:
        media_id: ID of media to monitor
        media_type: "movie" or "tv"
        is_4k: Whether to check 4K instance
        interval: Check interval in seconds
        max_checks: Maximum number of checks
        
    Returns:
        None
    """
    check_count = [0]  # Using list for mutable closure variable
    
    def _check_queue():
        try:
            check_count[0] += 1
            if check_count[0] > max_checks:
                logger.debug(f"Queue monitoring reached max checks ({max_checks}), stopping", 
                           extra={'emoji_type': 'debug'})
                return
                
            result = check_download_queue(media_type, media_id, is_4k)
            
            if result.get("total", 0) > 0:
                # Schedule next check if still in queue
                threading.Timer(interval, _check_queue).start()
            else:
                logger.debug(f"Media no longer in queue, stopping monitoring", extra={'emoji_type': 'debug'})
                
        except Exception as e:
            logger.error(f"Error in queue monitor: {e}", extra={'emoji_type': 'error'})
    
    # Start initial check
    threading.Timer(interval, _check_queue).start()