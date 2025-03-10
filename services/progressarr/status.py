"""
Status Update Module for Progressarr

This module handles updating media status in Plex to display download progress
and other status information.

NOTE ON NFO-BASED STATUS UPDATES:
---------------------------------
Future implementation should consider using NFO files for status updates as they
offer several advantages over direct title updates:

1. How It Works:
   - Create/update NFO files with status information in the title field
   - Plex reads these files when "Prefer local metadata" is enabled
   - Status appears directly in the Plex UI without API calls

2. User Requirements:
   - User must enable "Prefer local metadata" checkbox in library's advanced settings
   - No other special configuration needed

3. NFO File Structure:
   For movies (movie.nfo):
   ```xml
   <?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
   <movie>
     <title>Movie Title [Downloading: 45%]</title>
     <year>2023</year>
     <uniqueid type="tmdb" default="true">12345</uniqueid>
     <!-- other metadata fields -->
   </movie>
"""

import threading
import time
from typing import Optional, Dict, Any

from core.logger import logger
from core.config import settings

# Status tracking dictionaries
PROGRESS_FLAGS = {}  # Format: "rating_key" -> {"status": "string", "percentage": int}
ITEM_STATUS_CACHE = {}  # Format: "rating_key" -> {"title": "original_title", "last_update": timestamp}
EPISODE_TIMERS = {}  # Format: "rating_key" -> timer_object

def update_title_with_status(rating_key: str, title: str, status: str, percentage: Optional[int] = None) -> bool:
    """
    Update a title with download progress information
    
    Args:
        rating_key: Plex rating key
        title: Original title
        status: Status string
        percentage: Download percentage
        
    Returns:
        bool: Success or failure
    """
    from services.plex_client import update_title
    
    try:
        # Format the status string
        if percentage is not None and percentage > 0:
            status_str = f"[{status} {percentage}%]"
        else:
            status_str = f"[{status}]"
            
        return update_title(rating_key, title, status_str)
        
    except Exception as e:
        logger.error(f"Failed to update title with status: {e}", extra={'emoji_type': 'error'})
        return False

def schedule_status_update(rating_key: str, title: str, status: str, percentage: Optional[int] = None, 
                          delay: int = 0, retries: int = 0) -> None:
    """
    Schedule a status update with optional retries
    
    Args:
        rating_key: Plex rating key
        title: Original title
        status: Status string
        percentage: Download percentage
        delay: Seconds to delay before update
        retries: Number of retry attempts
        
    Returns:
        None
    """
    global EPISODE_TIMERS
    
    def _do_update():
        global EPISODE_TIMERS
        result = update_title_with_status(rating_key, title, status, percentage)
        
        if not result and retries > 0:
            # Schedule retry
            logger.debug(f"Status update failed, scheduling retry in 5 seconds", extra={'emoji_type': 'debug'})
            timer = threading.Timer(5.0, _do_update)
            timer.daemon = True
            EPISODE_TIMERS[rating_key] = timer
            timer.start()
        else:
            # Clean up timer reference
            EPISODE_TIMERS.pop(rating_key, None)
    
    # Cancel any existing timer
    if rating_key in EPISODE_TIMERS:
        try:
            EPISODE_TIMERS[rating_key].cancel()
        except:
            pass
    
    # Schedule new timer
    timer = threading.Timer(delay, _do_update)
    timer.daemon = True
    EPISODE_TIMERS[rating_key] = timer
    timer.start()

def clear_status(rating_key: str) -> None:
    """
    Clear status for a specific item
    
    Args:
        rating_key: Plex rating key
        
    Returns:
        None
    """
    global PROGRESS_FLAGS, ITEM_STATUS_CACHE, EPISODE_TIMERS
    
    # Cancel any pending timers
    if rating_key in EPISODE_TIMERS:
        try:
            EPISODE_TIMERS[rating_key].cancel()
            del EPISODE_TIMERS[rating_key]
        except:
            pass
            
    # Clear status flags
    PROGRESS_FLAGS.pop(rating_key, None)
    
    # Restore original title if cached
    if rating_key in ITEM_STATUS_CACHE:
        title = ITEM_STATUS_CACHE.get(rating_key, {}).get('title')
        if title:
            from services.plex_client import update_title
            update_title(rating_key, title, '')  # Clear status
        ITEM_STATUS_CACHE.pop(rating_key, None)