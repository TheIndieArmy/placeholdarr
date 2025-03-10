import os, re, threading, time, shutil, requests
from fastapi.responses import JSONResponse

from core.config import settings
from core.logger import logger
from services.plex_client import (
    plex, build_plex_url, update_title, 
    find_movie_by_tmdb_id, find_episode_by_tvdb_id,
    refresh_specific_path, get_section_id
)
from services.utils import (
    strip_movie_status, sanitize_filename, extract_episode_title, 
    is_4k_request, strip_status_markers
)

from services.placeholdarr.episode_manager import search_in_sonarr, search_in_radarr

# Helper functions
def get_folder_name(media_type, title, year=None, media_id=None):
    """Generate folder name according to the convention"""
    title = sanitize_filename(title)
    
    if media_type == "movie":
        # Movie folder: "{Movie Title} ({Year}) {tmdb-999999}{edition-Dummy}"
        return f"{title} ({year}) {{tmdb-{media_id}}}{{edition-Dummy}}"
    else:
        # TV folder: "{Series Title} ({year}) {tvdb-999999} (dummy)"
        year_str = f" ({year})" if year else ""
        return f"{title}{year_str} {{tvdb-{media_id}}} (dummy)"

def get_file_name(media_type, title, year=None, season=None, episode=None):
    """Generate file name according to the convention"""
    title = sanitize_filename(title)
    
    if media_type == "movie":
        # Movie file: "{Movie Title} ({Year}).mp4"
        return f"{title} ({year}).mp4"
    else:
        # TV file: "{Series Title} - s01e01.mp4"
        return f"{title} - s{season:02d}e{episode:02d}.mp4"

def place_dummy_file(media_type, base_path, title, year=None, media_id=None, season=None, episode=None):
    """Create a dummy file in the specified path"""
    try:
        if media_type == "movie":
            # Create movie folder
            folder_name = get_folder_name(media_type, title, year, media_id)
            folder_path = os.path.join(base_path, folder_name)
        else:
            # Create TV series folder and season subfolder
            series_folder_name = get_folder_name(media_type, title, year, media_id)
            season_folder_name = f"Season {season:02d}"
            folder_path = os.path.join(base_path, series_folder_name, season_folder_name)
        
        os.makedirs(folder_path, exist_ok=True)
        
        # Create the file name
        file_name = get_file_name(media_type, title, year, season, episode)
        file_path = os.path.join(folder_path, file_name)
        
        if os.path.exists(file_path):
            logger.info(f"Placeholder already exists: {file_path}", extra={'emoji_type': 'info'})
            return file_path
            
        # Create the dummy file - use hard link with updated timestamp
        try:
            os.link(settings.DUMMY_FILE_PATH, file_path)
            
            # Update the file's timestamp to current time
            current_time = time.time()
            os.utime(file_path, (current_time, current_time))
            
            logger.info(f"Created placeholder file: {file_path}", extra={'emoji_type': 'info'})
            return file_path
        except Exception as e:
            # Fall back to copy if hard link fails
            import shutil
            shutil.copy(settings.DUMMY_FILE_PATH, file_path)
            
            # Update timestamp
            current_time = time.time()
            os.utime(file_path, (current_time, current_time))
            
            logger.info(f"Created placeholder file (copied): {file_path}", extra={'emoji_type': 'info'})
            return file_path
            
    except Exception as e:
        logger.error(f"Error creating placeholder: {str(e)}", extra={'emoji_type': 'error'})
        raise

def delete_dummy_files(folder_path):
    """Delete all dummy files in a folder"""
    try:
        if not os.path.exists(folder_path):
            logger.debug(f"Path does not exist: {folder_path}", extra={'emoji_type': 'debug'})
            return False
            
        deleted = False
        dummy_size = os.path.getsize(settings.DUMMY_FILE_PATH)
        
        for root, _, files in os.walk(folder_path):
            for file in files:
                file_path = os.path.join(root, file)
                try:
                    # Check if file size matches our dummy file
                    if os.path.getsize(file_path) == dummy_size:
                        os.remove(file_path)
                        logger.info(f"Deleted placeholder file: {file_path}", extra={'emoji_type': 'delete'})
                        deleted = True
                except (FileNotFoundError, PermissionError) as e:
                    logger.error(f"Error checking/deleting file {file_path}: {e}", extra={'emoji_type': 'error'})
                    
        return deleted
    except Exception as e:
        logger.error(f"Failed to delete placeholders: {e}", extra={'emoji_type': 'error'})
        return False

def check_media_has_file(series_id, season, episode, is_4k=False):
    """Check if episode has a file already"""
    try:
        sonarr_url = settings.SONARR_4K_URL if is_4k else settings.SONARR_URL
        sonarr_api_key = settings.SONARR_4K_API_KEY if is_4k else settings.SONARR_API_KEY
        
        response = requests.get(f"{sonarr_url}/api/v3/episode", params={'seriesId': series_id}, headers={'X-Api-Key': sonarr_api_key})
        if response.status_code != 200:
            logger.error(f"Failed to get episodes: {response.status_code}", extra={'emoji_type': 'error'})
            return False
            
        episodes = response.json()
        for ep in episodes:
            if ep['seasonNumber'] == season and ep['episodeNumber'] == episode:
                return bool(ep.get('hasFile', False))
                
        return False
    except Exception as e:
        logger.error(f"Error checking episode file: {e}", extra={'emoji_type': 'error'})
        return False

# Main handler functions
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
            
            # Determine the correct movie folder
            movie_folder = settings.MOVIE_LIBRARY_4K_FOLDER if is_4k else settings.MOVIE_LIBRARY_FOLDER
            
            if tmdb_id:
                # Create folder pattern and delete dummy files
                folder_pattern = f"{sanitize_filename(title)} ({year})"
                movie_path = os.path.join(movie_folder, folder_pattern)
                delete_dummy_files(movie_path)
                
                # Get the correct section ID based on quality
                section_id = get_section_id('movie', is_4k)
                
                # Schedule Plex refresh
                refresh_specific_path(section_id, movie_path)
                logger.info(f"Refreshing Plex path: {movie_path} in section {section_id}", extra={'emoji_type': 'refresh'})
            else:
                logger.warning(f"No TMDB ID for movie: {title}", extra={'emoji_type': 'warning'})
                
            return JSONResponse({"status": "success", "message": "Movie import processed", "title": title})
            
        elif 'episodes' in data and 'series' in data:
            # TV import handling
            series = data['series']
            episodes = data.get('episodes', [])
            
            if not episodes:
                return JSONResponse({"status": "error", "message": "No episodes in payload"}, status_code=400)
                
            tvdb_id = series.get('tvdbId')
            title = series.get('title', 'Unknown Series')
            
            # Process each episode
            for episode in episodes:
                season_num = episode.get('seasonNumber')
                episode_num = episode.get('episodeNumber')
                logger.info(f"Processing import for {title} S{season_num}E{episode_num}", extra={'emoji_type': 'cleanup'})
                
                # Determine the correct TV folder
                tv_folder = settings.TV_LIBRARY_4K_FOLDER if is_4k else settings.TV_LIBRARY_FOLDER
                
                # Create season folder pattern
                series_folder = os.path.join(tv_folder, sanitize_filename(title))
                season_folder = os.path.join(series_folder, f"Season {season_num}")
                
                # Delete any dummy files
                delete_dummy_files(season_folder)
                
                # Get the correct section ID based on quality
                section_id = get_section_id('episode', is_4k)
                
                # Schedule Plex refresh
                refresh_specific_path(section_id, season_folder)
                logger.info(f"Refreshing Plex path: {season_folder} in section {section_id}", extra={'emoji_type': 'refresh'})
                
            return JSONResponse({"status": "success", "message": "TV import processed", "title": title})
            
        return JSONResponse({"status": "error", "message": "Unsupported media type"}, status_code=400)
        
    except Exception as e:
        logger.error(f"Failed to process import event: {e}", extra={'emoji_type': 'error'})
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

def handle_seriesadd(data, is_4k=False):
    """Handle series added event from Sonarr"""
    try:
        series = data.get('series', {})
        title = series.get('title', 'Unknown')
        year = series.get('year')
        tvdb_id = series.get('tvdbId')
        
        logger.info(f"New series added: {title} (TVDB: {tvdb_id}) ", extra={'emoji_type': 'info'})
        
        # Use the correct TV folder based on quality
        tv_folder = settings.TV_LIBRARY_4K_FOLDER if is_4k else settings.TV_LIBRARY_FOLDER
        
        # Get season information from Sonarr API if possible
        seasons = series.get('seasons', [])
        if not seasons:
            # Default to just season 1 with 10 episodes if we can't get season info
            seasons = [{"seasonNumber": 1, "episodeCount": 10}]
            
        created_files = []
        
        # Create placeholders for all episodes in each season
        for season in seasons:
            season_num = season.get('seasonNumber')
            episode_count = season.get('episodeCount', 10)  # Default to 10 if not specified
            
            # Skip specials (season 0) unless explicitly enabled
            if season_num == 0 and not settings.INCLUDE_SPECIALS:
                continue
                
            # Create placeholder for each episode in the season
            for episode_num in range(1, episode_count + 1):
                file_path = place_dummy_file("episode", tv_folder, title, year, tvdb_id, season_num, episode_num)
                created_files.append(file_path)
        
        # Schedule [Request] tag update for the first episode
        schedule_episode_request_update(tvdb_id, 1, 1, f"Episode 1", is_4k)
        
        return JSONResponse(content={
            "status": "success",
            "message": f"Created {len(created_files)} placeholder(s) for series: {title}",
            "file_paths": created_files[:5]  # Return first 5 paths to avoid huge response
        })
    except Exception as e:
        logger.error(f"Error creating series placeholder: {str(e)}", extra={'emoji_type': 'error'})
        return {"status": "error", "message": str(e)}

def handle_episodefiledelete(data: dict, is_4k: bool = False):
    """Handle episode file deletion in Sonarr"""
    try:
        episode_file = data.get('episodeFile', {})
        series = data.get('series', {})
        
        if not episode_file or not series:
            return JSONResponse({"status": "error", "message": "Missing episode or series data"})
            
        title = series.get('title', 'Unknown Series')
        tvdb_id = series.get('tvdbId')
        year = series.get('year')
        path = episode_file.get('path', '')
        
        # Extract season and episode numbers
        match = re.search(r's(\d+)e(\d+)', path.lower())
        if not match:
            logger.error(f"Could not extract season/episode from path: {path}", extra={'emoji_type': 'error'})
            return JSONResponse({"status": "error", "message": "Invalid episode path format"})
            
        season_num = int(match.group(1))
        episode_num = int(match.group(2))
        
        # Determine if it's 4K based on the Sonarr instance (source URL)
        request_url = data.get("requestUrl", "")
        if settings.SONARR_4K_URL and settings.SONARR_4K_URL in request_url:
            is_4k = True
            
        logger.info(f"Episode file deleted: {title} S{season_num}E{episode_num} {'[4K]' if is_4k else ''}", 
                   extra={'emoji_type': 'delete'})
        
        # Determine the correct TV folder
        tv_folder = settings.TV_LIBRARY_4K_FOLDER if is_4k else settings.TV_LIBRARY_FOLDER
        
        # Create placeholder for the deleted episode
        place_dummy_file("episode", tv_folder, title, year, tvdb_id, season_num, episode_num)
        
        # Schedule [Request] tag update - now with is_4k parameter
        schedule_episode_request_update(tvdb_id, season_num, episode_num, f"Episode {episode_num}", is_4k)
        
        return JSONResponse({"status": "success", "message": f"Created placeholder for deleted episode: S{season_num}E{episode_num}"})
        
    except Exception as e:
        logger.error(f"Failed to handle episode file delete: {e}", extra={'emoji_type': 'error'})
        return JSONResponse({"status": "error", "message": str(e)})

def handle_moviefiledelete(data: dict, is_4k: bool = False):
    """Handle movie file deletion in Radarr"""
    try:
        movie_file = data.get('movieFile', {})
        movie = data.get('movie', {})
        
        if not movie_file or not movie:
            return JSONResponse({"status": "error", "message": "Missing movie data"})
            
        title = movie.get('title', 'Unknown Movie')
        year = movie.get('year')
        tmdb_id = movie.get('tmdbId')
        path = movie_file.get('path', '')
        
        # Determine if it's 4K based on the Radarr instance (source URL)
        request_url = data.get("requestUrl", "")
        is_4k = False
        if settings.RADARR_4K_URL and settings.RADARR_4K_URL in request_url:
            is_4k = True
        # Also check path as a fallback
        elif is_4k_request(path):
            is_4k = True
            
        logger.info(f"Movie file deleted: {title} ({year}) {'[4K]' if is_4k else ''}", 
                   extra={'emoji_type': 'delete'})
        
        # Determine the correct movie folder
        movie_folder = settings.MOVIE_LIBRARY_4K_FOLDER if is_4k else settings.MOVIE_LIBRARY_FOLDER
        
        # Create placeholder for the deleted movie
        place_dummy_file("movie", movie_folder, title, year, tmdb_id)
        
        # Schedule [Request] tag update - now with is_4k parameter
        schedule_movie_request_update(f"{title} ({year})", tmdb_id, is_4k)
        
        return JSONResponse({"status": "success", "message": f"Created placeholder for deleted movie: {title} ({year})"})
        
    except Exception as e:
        logger.error(f"Failed to handle movie file delete: {e}", extra={'emoji_type': 'error'})
        return JSONResponse({"status": "error", "message": str(e)})

def handle_movie_delete(data: dict):
    """Handle movie deletion in Radarr"""
    try:
        movie = data.get('movie', {})
        if not movie:
            return JSONResponse({"status": "error", "message": "Missing movie data"})
            
        title = movie.get('title', 'Unknown Movie')
        year = movie.get('year')
        
        logger.info(f"Movie deleted from Radarr: {title} ({year})", extra={'emoji_type': 'delete'})
        
        return JSONResponse({"status": "success", "message": f"Processed movie deletion: {title} ({year})"})
        
    except Exception as e:
        logger.error(f"Failed to handle movie deletion: {e}", extra={'emoji_type': 'error'})
        return JSONResponse({"status": "error", "message": str(e)})

def handle_movieadd(data, is_4k=False):
    """Handle movie added event from Radarr"""
    try:
        movie = data.get('movie', {})
        title = movie.get('title', 'Unknown')
        year = movie.get('year')
        tmdb_id = movie.get('tmdbId')
        folder_path = movie.get('folderPath', '')
        
        logger.info(f"New movie added: {title} ({year}) (TMDB: {tmdb_id}) ", extra={'emoji_type': 'info'})
        
        # Use the correct movie folder based on quality
        movie_folder = settings.MOVIE_LIBRARY_4K_FOLDER if is_4k else settings.MOVIE_LIBRARY_FOLDER
        
        # Create placeholder for the movie using place_dummy_file
        file_path = place_dummy_file("movie", movie_folder, title, year, tmdb_id)
        
        # Schedule [Request] tag update
        schedule_movie_request_update(f"{title} ({year})", tmdb_id, is_4k)
        
        return JSONResponse(content={
            "status": "success",
            "message": f"Created placeholder for movie: {title}",
            "file_path": file_path
        })
    except Exception as e:
        logger.error(f"Error creating movie placeholder: {str(e)}", extra={'emoji_type': 'error'})
        return {"status": "error", "message": str(e)}

def handle_seriesdelete(data):
    """Handle series delete event from Sonarr"""
    try:
        series = data.get('series', {})
        title = series.get('title', 'Unknown')
        tvdb_id = series.get('tvdbId')
        year = series.get('year')
        
        logger.info(f"Series deleted from Sonarr: {title}", extra={'emoji_type': 'delete'})
        
        # Find and remove placeholder folders from both standard and 4K libraries
        for is_4k in [False, True]:
            # Get the appropriate library folder
            library_folder = settings.TV_LIBRARY_4K_FOLDER if is_4k else settings.TV_LIBRARY_FOLDER
            
            # Skip if this quality isn't enabled
            if not library_folder:
                continue
                
            # Build the expected folder name
            folder_name = get_folder_name("episode", title, year, tvdb_id)
            full_path = os.path.join(library_folder, folder_name)
            
            # Check if folder exists and delete it
            if os.path.exists(full_path):
                try:
                    import shutil
                    shutil.rmtree(full_path)
                    logger.info(f"Deleted placeholder folder: {full_path}", extra={'emoji_type': 'delete'})
                except Exception as e:
                    logger.error(f"Failed to delete folder {full_path}: {str(e)}", extra={'emoji_type': 'error'})
        
        return {"status": "success", "message": f"Processed series deletion: {title}"}
        
    except Exception as e:
        logger.error(f"Error handling series deletion: {str(e)}", extra={'emoji_type': 'error'})
        return {"status": "error", "message": str(e)}

def handle_playback(data: dict):
    """Handle media playback events"""
    try:
        media = data.get("media", {})
        file_path = media.get("file_info", {}).get("path", "")
        is_4k = is_4k_request(file_path)
        media_type = media.get("type")
        title = media.get("title", "Unknown Title")
        rating_key = media.get("ids", {}).get("plex")

        if media_type == "movie":
            tmdb_id = media.get("ids", {}).get("tmdb")
            # If the payload contains a placeholder, extract the actual TMDB ID from file_info.path
            if tmdb_id == "{tmdb_id}":
                # Extract TMDB ID from the file path
                import re
                tmdb_match = re.search(r'tmdb-(\d+)', file_path)
                if tmdb_match:
                    tmdb_id = tmdb_match.group(1)
                else:
                    logger.error("TMDB ID not found in file path", extra={'emoji_type': 'error'})
                    return JSONResponse({"status": "error", "message": "Missing valid TMDB ID"}, status_code=400)
            
            # Process movie playback
            base_title = strip_movie_status(sanitize_filename(title))
            logger.info(f"Processing movie playback for {base_title}", extra={'emoji_type': 'processing'})
            
            # Update title to "Searching..."
            update_title(rating_key, base_title, "[Searching...]")
            
            # Start search in Radarr
            success = search_in_radarr(tmdb_id, rating_key, is_4k)
            
            if not success:
                return JSONResponse({"status": "error", "message": "Search failed"}, status_code=500)
                
            return JSONResponse({"status": "success", "message": "Search triggered"})
            
        elif media_type == "episode":
            # Extract TV show identification
            show_title = media.get("grandparent_title", "Unknown Show")
            tvdb_id = media.get("ids", {}).get("tvdb")
            season_number = media.get("season", 1)
            episode_number = media.get("episode", 1)
            
            # If tvdb_id is not provided or is a placeholder
            if not tvdb_id or tvdb_id == "{tvdb_id}":
                tvdb_match = re.search(r'tvdb-(\d+)', file_path)
                if tvdb_match:
                    tvdb_id = tvdb_match.group(1)
                else:
                    logger.error("TVDB ID not found in file path", extra={'emoji_type': 'error'})
                    return JSONResponse({"status": "error", "message": "Missing valid TVDB ID"}, status_code=400)
            
            # Process episode playback
            logger.info(f"Processing episode playback for {show_title} S{season_number}E{episode_number}", 
                       extra={'emoji_type': 'processing'})
            
            # Update to Searching status
            episode_title = media.get("title", f"Episode {episode_number}")
            update_title(rating_key, episode_title, "[Searching...]")
            
            # Use episode_manager to search for episodes based on play mode
            series_id = search_in_sonarr(tvdb_id, rating_key, season_number, episode_number, is_4k)
            
            if not series_id:
                return JSONResponse({"status": "error", "message": "Search failed"}, status_code=500)
                
            return JSONResponse({"status": "success", "message": "Search triggered"})
            
    except Exception as e:
        logger.error(f"Failed to process playback: {e}", extra={'emoji_type': 'error'})
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

def schedule_episode_request_update(tvdb_id, season, episode, title, is_4k=False):
    """Schedule an episode title update to add [Request] tag"""
    try:
        def update_episode_title(attempts=3):
            try:
                # Find the episode in Plex
                section_id = get_section_id('episode', is_4k)
                rating_key = find_episode_by_tvdb_id(tvdb_id, season, episode, is_4k)
                
                if not rating_key:
                    logger.debug(f"Episode not found in Plex, retrying ({attempts} attempts left)", 
                                extra={'emoji_type': 'debug'})
                    if attempts > 0:
                        # Try again in a few seconds
                        threading.Timer(5.0, lambda: update_episode_title(attempts - 1)).start()
                    return
                    
                # Update the episode title with [Request] tag
                update_title(rating_key, title, "[Request]")
                
            except Exception as e:
                logger.error(f"Failed to update episode title: {e}", extra={'emoji_type': 'error'})
                if attempts > 0:
                    threading.Timer(5.0, lambda: update_episode_title(attempts - 1)).start()
        
        # Start the update process with a short delay to allow Plex to update
        threading.Timer(10.0, update_episode_title).start()
        lib_type = "4K" if is_4k and settings.use_4k_plex_library('episode', is_4k) else "standard"
        logger.info(f"Scheduled [Request] tag for S{season:02d}E{episode:02d} in {lib_type} library", 
                   extra={'emoji_type': 'info'})
        
    except Exception as e:
        logger.error(f"Failed to schedule episode title update: {e}", extra={'emoji_type': 'error'})

def schedule_movie_request_update(title, tmdb_id, is_4k=False):
    """Schedule a movie title update to add [Request] tag"""
    try:
        def update_movie_title(attempts=3):
            try:
                # Find the movie in Plex
                section_id = get_section_id('movie', is_4k)
                rating_key = find_movie_by_tmdb_id(tmdb_id, is_4k)
                
                if not rating_key:
                    logger.debug(f"Movie not found in Plex, retrying ({attempts} attempts left)",
                               extra={'emoji_type': 'debug'})
                    if attempts > 0:
                        # Try again in a few seconds
                        threading.Timer(5.0, lambda: update_movie_title(attempts - 1)).start()
                    return
                    
                # Update the movie title with [Request] tag
                update_title(rating_key, title, "[Request]")
                
            except Exception as e:
                logger.error(f"Failed to update movie title: {e}", extra={'emoji_type': 'error'})
                if attempts > 0:
                    threading.Timer(5.0, lambda: update_movie_title(attempts - 1)).start()
        
        # Start the update process with a short delay to allow Plex to update
        threading.Timer(10.0, update_movie_title).start()
        lib_type = "4K" if is_4k and settings.use_4k_plex_library('movie', is_4k) else "standard"
        logger.info(f"Scheduled [Request] tag for '{title}' in {lib_type} library", 
                   extra={'emoji_type': 'info'})
        
    except Exception as e:
        logger.error(f"Failed to schedule movie title update: {e}", extra={'emoji_type': 'error'})