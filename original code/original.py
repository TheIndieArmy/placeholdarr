#!/usr/bin/env python3
import os
import re
import time
import glob
import shutil
import threading
import logging
import requests
import urllib.parse
import json
from datetime import datetime

from flask import Flask, request, jsonify
from plexapi.server import PlexServer
from gunicorn.app.base import BaseApplication

# ========================
# Configuration
# ========================
TAUTULLI_API_KEY = 'pxCyU21BCfFXwZEGlnh55KpWUrpqkrjJ'
RADARR_API_KEY = '6f0127ae2b0b4e84aee827048b945ac8'
SONARR_API_KEY = 'e83b027409284c9c8181321edb7e04af'

RADARR_URL = 'http://192.168.1.139:7878/api/v3'
SONARR_URL = 'http://192.168.1.139:8989/api/v3'

MOVIE_LIBRARY_FOLDER = '/mnt/user/appdata/infiniteplexlibrarytest/plex/movie-library'
TV_LIBRARY_FOLDER = '/mnt/user/appdata/infiniteplexlibrarytest/plex/tv-library'
DUMMY_FILE_PATH = '/mnt/user/appdata/infiniteplexlibrarytest/dummy.mp4'

# Base URL for Plex – user should not include a trailing slash here.
PLEX_URL = 'http://192.168.1.139:32400'
PLEX_TOKEN = 'oVFxg4AHCyqCAVpzyry3'

# Plex Library Section IDs (used in refresh calls)
PLEX_MOVIE_SECTION_ID = 16   # Movies
PLEX_TV_SECTION_ID = 17      # TV Shows

MAX_MONITOR_TIME = 120         # Seconds to monitor download progress before timeout
CHECK_INTERVAL = 3             # Polling interval in seconds
WORKER_COUNT = 4
CHECK_MAX_ATTEMPTS = 1000      # Fallback attempt count

# ========================
# Global Dictionaries & Locks
# ========================
BASE_TITLES = {}               # Clean base titles keyed by rating key
PROGRESS_FLAGS = {}            # Tracks progress (by rating key)
TIMER_LOCK = threading.Lock()  # Global lock for timer operations
ACTIVE_SEARCH_TIMERS = {}      # Active search timers keyed by rating key
LAST_RADARR_SEARCH = {}        # Timestamps for last manual search trigger

# ========================
# Logging Configuration
# ========================
LOG_EMOJIS = {
    'success': '✅', 'error': '❌', 'info': 'ℹ️', 'debug': '🐛',
    'webhook': '🌐', 'playback': '🎬', 'dummy': '📁', 'search': '🔍',
    'delete': '🗑️', 'update': '🔄', 'warning': '⚠️',
    'processing': '⏳', 'monitored': '👀', 'progress': '🔄',
    'tracking': '⏳', 'tv': '📺'
}

class EmojiLogFormatter(logging.Formatter):
    def format(self, record):
        emoji = LOG_EMOJIS.get(record.__dict__.get('emoji_type', ''), '➡️')
        record.msg = f"{emoji} {record.msg}"
        formatted = super().format(record)
        if not formatted.endswith("\n"):
            formatted += "\n"
        return formatted

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
console_handler = logging.StreamHandler()
console_handler.setFormatter(EmojiLogFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
file_handler = logging.FileHandler('media_handler.log')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(console_handler)
logger.addHandler(file_handler)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# ========================
# Helper Function: Build Plex URL
# ========================
def build_plex_url(path):
    """
    Constructs a Plex API URL by ensuring exactly one slash between the base URL and the path.
    This function does not require the user to add a trailing slash in the configuration.
    """
    base = PLEX_URL.rstrip('/')
    if not path.startswith('/'):
        path = '/' + path
    return base + path

# ========================
# PlexAPI Initialization
# ========================
try:
    plex = PlexServer(PLEX_URL, PLEX_TOKEN)
    logger.info("Connected to Plex via PlexAPI.", extra={'emoji_type': 'info'})
except Exception as e:
    logger.error(f"Failed to connect to Plex: {e}", extra={'emoji_type': 'error'})
    plex = None

# ========================
# Helper Functions
# ========================
def sanitize_filename(name):
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()

def dedup_title(title):
    parts = [p.strip() for p in title.split(' - ')]
    seen = set()
    deduped = []
    for part in parts:
        if part not in seen:
            seen.add(part)
            deduped.append(part)
    return " - ".join(deduped)

def extract_episode_title(raw_title):
    clean = raw_title.split('[')[0].strip()
    parts = clean.split(" - ")
    if len(parts) >= 3 and parts[0] == parts[1]:
        return parts[2].strip()
    elif len(parts) >= 2:
        return parts[1].strip()
    return clean

def strip_movie_status(title):
    pattern = re.compile(r"\s*-\s*(Searching|Not Found - Search Timeout|Downloading\s+\d+%)(\s*-\s*)?$", re.IGNORECASE)
    prev = None
    while prev != title:
        prev = title
        title = pattern.sub("", title).strip()
    return title

def strip_status_markers(title):
    """
    Remove common status markers (e.g. "Available", "Searching", etc.) from the title.
    """
    return re.sub(r'\s*-\s*(Available|Searching|Not Found - Search Timeout|Downloading\s+\d+%)', '', title).strip()

# ========================
# Persistent Rating Key Storage Functions
# ========================
def get_rating_key_file_path(series_folder):
    return os.path.join(series_folder, ".rating_key.txt")

def store_rating_key(series_folder, rating_key):
    try:
        file_path = get_rating_key_file_path(series_folder)
        with open(file_path, "w") as f:
            f.write(str(rating_key))
        logger.info(f"Stored rating key {rating_key} in {file_path}", extra={'emoji_type': 'update'})
    except Exception as e:
        logger.error(f"Error storing rating key in {series_folder}: {e}", extra={'emoji_type': 'error'})

def load_rating_key(series_folder):
    try:
        file_path = get_rating_key_file_path(series_folder)
        if os.path.exists(file_path):
            with open(file_path, "r") as f:
                key = f.read().strip()
                logger.info(f"Loaded stored rating key {key} from {file_path}", extra={'emoji_type': 'info'})
                return key
    except Exception as e:
        logger.error(f"Error loading rating key from {series_folder}: {e}", extra={'emoji_type': 'error'})
    return None

# ========================
# Determine Series Folder
# ========================
def get_series_folder(media_type, target_base_folder, title, year, media_id, season_number=None):
    clean_title = sanitize_filename(title)
    year_str = f" ({year})" if year else ''
    if media_type == 'movie':
        series_folder = os.path.join(target_base_folder, f"{clean_title}{year_str} {{tmdb-{media_id}}}")
    else:
        series_folder = os.path.join(target_base_folder, f"{clean_title}{year_str} {{tvdb-{media_id}}}")
    return series_folder

# ========================
# Plex Title Updates with PlexAPI
# ========================
def update_plex_title(rating_key, base_title, status):
    # Remove any existing status markers from the base title
    base_title = strip_status_markers(base_title)
    new_title = f"{base_title} - {status}"
    try:
        url = build_plex_url(f"library/metadata/{rating_key}?title.value={urllib.parse.quote(new_title)}&X-Plex-Token={PLEX_TOKEN}")
        # Debug print the URL if needed:
        logger.debug(f"Updating title via URL: {url}", extra={'emoji_type': 'debug'})
        response = requests.put(url)
        response.raise_for_status()
        logger.info(f"Updated Plex title to: {new_title}", extra={'emoji_type': 'update'})
    except Exception as e:
        logger.error(f"Failed to update Plex title for {rating_key}: {str(e)}", extra={'emoji_type': 'error'})

# ========================
# Refresh Plex Folder (Partial Refresh)
# ========================
def refresh_plex_folder(folder_path, library_id):
    try:
        url = build_plex_url(f"library/sections/{library_id}/refresh?X-Plex-Token={PLEX_TOKEN}&path={urllib.parse.quote(folder_path)}")
        response = requests.get(url)
        response.raise_for_status()
        logger.info(f"Plex folder scan triggered for folder: {folder_path}", extra={'emoji_type': 'info'})
    except Exception as e:
        logger.error(f"Failed to trigger Plex folder scan for {folder_path}: {e}", extra={'emoji_type': 'error'})

# ========================
# Dummy File Management
# ========================
def place_dummy_file(media_type, title, year, media_id, target_base_folder,
                     season_number=None, episode_range=None, episode_id=None):
    try:
        clean_title = sanitize_filename(title)
        year_str = f" ({year})" if year else ''
        if media_type == 'movie':
            folder_name = f"{clean_title}{year_str} {{tmdb-{media_id}}}{{edition-Dummy}}"
            file_name = f"{clean_title}{year_str} (dummy).mp4"
            target_dir = os.path.join(target_base_folder, folder_name.strip())
        else:
            folder_name = f"{clean_title}{year_str} {{tvdb-{media_id}}}"
            season_str = f"Season {int(season_number):02d}" if season_number else ""
            target_dir = os.path.join(target_base_folder, folder_name.strip(), season_str)
            if episode_range and episode_range[0] == episode_range[1]:
                if episode_id:
                    file_name = f"{clean_title} - s{int(season_number):02d}e{int(episode_range[0]):02d} (dummy) [ID:{episode_id}].mp4"
                else:
                    logger.warning(f"Episode ID not provided for {title} S{season_number:02d}E{episode_range[0]:02d}", extra={'emoji_type': 'warning'})
                    file_name = f"{clean_title} - s{int(season_number):02d}e{int(episode_range[0]):02d} (dummy) [ID:unknown].mp4"
            else:
                ep_range = f"e{episode_range[0]:02d}-e{episode_range[1]:02d}" if episode_range else "e01-e99"
                file_name = f"{clean_title} - s{int(season_number):02d}{ep_range} (dummy).mp4"
        os.makedirs(target_dir, exist_ok=True)
        target_path = os.path.join(target_dir, file_name)
        if os.path.exists(target_path):
            os.remove(target_path)
        shutil.copy(DUMMY_FILE_PATH, target_path)
        logger.debug(f"Dummy file copied to: {target_path}", extra={'emoji_type': 'debug'})
        return target_path
    except Exception as e:
        logger.error(f"Dummy creation failed: {e}", extra={'emoji_type': 'error'})
        raise

def delete_dummy_files(media_type, title, year, media_id, target_base_folder,
                       season_number=None, episode_number=None):
    try:
        clean_title = sanitize_filename(title)
        year_str = f" ({year})" if year else ''
        if media_type == 'movie':
            folder_name = f"{clean_title}{year_str} {{tmdb-{media_id}}}"
            target_dir = os.path.join(target_base_folder, folder_name.strip())
            expected_file = os.path.join(target_dir, f"{clean_title}{year_str} (dummy).mp4")
            if os.path.exists(expected_file):
                os.remove(expected_file)
                logger.info(f"Deleted dummy file: {expected_file}", extra={'emoji_type': 'delete'})
            else:
                logger.info(f"No dummy file exists for movie {title}", extra={'emoji_type': 'info'})
        else:
            folder_name = f"{clean_title}{year_str} {{tvdb-{media_id}}}"
            target_dir = os.path.join(target_base_folder, folder_name.strip())
            if season_number:
                target_dir = os.path.join(target_dir, f"Season {int(season_number):02d}")
            if episode_number is not None:
                pattern = os.path.join(target_dir, f"*s{str(season_number).zfill(2)}e{str(episode_number).zfill(2)}*dummy*.mp4")
                dummy_files = glob.glob(pattern)
                if dummy_files:
                    for file_path in dummy_files:
                        os.remove(file_path)
                        logger.info(f"Deleted dummy file: {file_path}", extra={'emoji_type': 'delete'})
                else:
                    logger.info(f"No dummy file exists for {title} S{str(season_number).zfill(2)}E{str(episode_number).zfill(2)}", extra={'emoji_type': 'info'})
            else:
                logger.info(f"No episode number provided; not deleting dummies for {title} Season {str(season_number).zfill(2)}", extra={'emoji_type': 'info'})
    except Exception as e:
        logger.error(f"Dummy deletion failed: {e}", extra={'emoji_type': 'error'})

# ========================
# Delayed Title Updates with PlexAPI & Persistent Rating Key Storage
# ========================
def schedule_episode_request_update(series_title, season_num, episode_num, media_id, delay=10, retries=5):
    """
    After a delay, look up the show by title and update the specified episode title to include [Request].
    Once found, store its rating key persistently in the series folder.
    """
    def attempt_update(attempt=1):
        try:
            tv_section = plex.library.sectionByID(PLEX_TV_SECTION_ID)
            show = tv_section.get(series_title)  # Title-based lookup
            if not show:
                logger.debug(f"Show '{series_title}' not found on attempt {attempt}.", extra={'emoji_type': 'debug'})
                if attempt < retries:
                    threading.Timer(3, attempt_update, args=[attempt+1]).start()
                return

            episodes = show.episodes()
            target_ep = None
            for ep in episodes:
                if int(ep.index) == int(episode_num):
                    target_ep = ep
                    break

            if target_ep:
                # Strip any existing status markers from the episode's title
                base = strip_status_markers(target_ep.title)
                new_title = f"{base} - [Request]"
                target_ep.editTitle(new_title)
                target_ep.reload()
                logger.info(f"Updated episode title for '{series_title}' S{season_num:02d}E{episode_num:02d} to: {new_title}",
                            extra={'emoji_type': 'update'})
                series_folder = get_series_folder("tv", TV_LIBRARY_FOLDER, series_title, show.year, media_id)
                store_rating_key(series_folder, show.ratingKey)
            else:
                if attempt < retries:
                    logger.debug(f"Episode {episode_num} not found in '{series_title}' (attempt {attempt}). Retrying...", extra={'emoji_type': 'debug'})
                    threading.Timer(3, attempt_update, args=[attempt+1]).start()
        except Exception as e:
            logger.error(f"Failed to update '{series_title}' S{season_num:02d}E{episode_num:02d}: {e}", extra={'emoji_type': 'error'})

    threading.Timer(delay, attempt_update).start()


def schedule_movie_request_update(movie_title, media_id, delay=10, retries=5):
    """
    After a delay, look up the movie by title and update its title to include [Request].
    Once found, store its rating key persistently in the movie folder.
    """
    def attempt_update(attempt=1):
        try:
            movie_section = plex.library.sectionByID(PLEX_MOVIE_SECTION_ID)
            item = movie_section.get(movie_title)
            if item:
                # Strip any existing status markers from the movie's title
                base = strip_status_markers(item.title)
                new_title = f"{base} - [Request]"
                item.editTitle(new_title)
                item.reload()
                logger.info(f"Updated movie title for '{movie_title}' to: {new_title}", extra={'emoji_type': 'update'})
                series_folder = get_series_folder("movie", MOVIE_LIBRARY_FOLDER, movie_title, item.year, media_id)
                store_rating_key(series_folder, item.ratingKey)
            else:
                if attempt < retries:
                    logger.debug(f"Movie '{movie_title}' not found (attempt {attempt}). Retrying...", extra={'emoji_type': 'debug'})
                    threading.Timer(3, attempt_update, args=[attempt+1]).start()
        except Exception as e:
            logger.error(f"Failed to update movie '{movie_title}': {e}", extra={'emoji_type': 'error'})

    threading.Timer(delay, attempt_update).start()

# ========================
# Radarr Integration (Movies)
# ========================
def trigger_radarr_search(movie_id, movie_title=None):
    try:
        response = requests.post(f"{RADARR_URL}/command", json={'name': 'MoviesSearch', 'movieIds': [movie_id]}, headers={'X-Api-Key': RADARR_API_KEY})
        response.raise_for_status()
        logger.debug(f"Radarr search triggered for movie id {movie_id}", extra={'emoji_type': 'debug'})
        if movie_title:
            logger.info(f"Triggered search for {movie_title}", extra={'emoji_type': 'search'})
        return True
    except Exception as e:
        logger.error(f"Radarr search failed: {e}", extra={'emoji_type': 'error'})
        return False

def search_in_radarr(tmdb_id, rating_key):
    try:
        movies_response = requests.get(f"{RADARR_URL}/movie", headers={'X-Api-Key': RADARR_API_KEY})
        movies_response.raise_for_status()
        movies = movies_response.json()
        if not isinstance(movies, list):
            logger.error(f"Expected list from Radarr /movie endpoint but got {type(movies)}", extra={'emoji_type': 'error'})
            return False
        
        existing = [m for m in movies if int(m.get("tmdbId", 0)) == int(tmdb_id)]
        if existing:
            movie_data = existing[0]
            logger.info(f"Movie already exists in Radarr: {movie_data['title']}", extra={'emoji_type': 'info'})
            if not movie_data.get("monitored", False):
                movie_data["monitored"] = True
                put_response = requests.put(f"{RADARR_URL}/movie/{movie_data['id']}", json=movie_data, headers={'X-Api-Key': RADARR_API_KEY})
                put_response.raise_for_status()
                logger.info(f"Movie {movie_data['title']} marked as monitored", extra={'emoji_type': 'monitored'})
            update_plex_title(rating_key, movie_data['title'], "Searching...")
            now = time.time()
            if rating_key not in LAST_RADARR_SEARCH or (now - LAST_RADARR_SEARCH[rating_key] >= 30):
                LAST_RADARR_SEARCH[rating_key] = now
                trigger_radarr_search(movie_data['id'], movie_data['title'])
            else:
                logger.debug("Manual search already triggered recently; skipping duplicate search", extra={'emoji_type': 'debug'})
            with TIMER_LOCK:
                if rating_key not in ACTIVE_SEARCH_TIMERS:
                    timer = threading.Timer(0, check_has_file, args=[tmdb_id, movie_data['title'], rating_key, 0, time.time()])
                    ACTIVE_SEARCH_TIMERS[rating_key] = timer
                    timer.start()
            return True

        lookup = requests.get(f"{RADARR_URL}/movie/lookup", params={'term': f"tmdb:{tmdb_id}"}, headers={'X-Api-Key': RADARR_API_KEY})
        lookup.raise_for_status()
        movie_data = lookup.json()[0]
        payload = {
            'title': movie_data['title'],
            'qualityProfileId': 7,
            'tmdbId': int(movie_data['tmdbId']),
            'year': int(movie_data['year']),
            'rootFolderPath': '/mnt/user/data/infinite/movies',
            'monitored': True,
            'addOptions': {
                'searchForMovie': True,
                'addMethod': 'manual',
                'monitor': 'movieOnly'
            }
        }
        response = requests.post(f"{RADARR_URL}/movie", json=payload, headers={'X-Api-Key': RADARR_API_KEY})
        response.raise_for_status()
        logger.info(f"Added movie: {movie_data['title']}", extra={'emoji_type': 'success'})
        update_plex_title(rating_key, movie_data['title'], "Searching...")
        now = time.time()
        if rating_key not in LAST_RADARR_SEARCH or (now - LAST_RADARR_SEARCH[rating_key] >= 30):
            LAST_RADARR_SEARCH[rating_key] = now
            trigger_radarr_search(response.json()['id'], movie_data['title'])
        else:
            logger.debug("Manual search already triggered recently; skipping duplicate search", extra={'emoji_type': 'debug'})
        with TIMER_LOCK:
            if rating_key not in ACTIVE_SEARCH_TIMERS:
                timer = threading.Timer(0, check_has_file, args=[tmdb_id, movie_data['title'], rating_key, 0, time.time()])
                ACTIVE_SEARCH_TIMERS[rating_key] = timer
                timer.start()
        return True

    except Exception as e:
        logger.error(f"Radarr operation failed: {e}", extra={'emoji_type': 'error'})
        try:
            update_plex_title(rating_key, movie_data.get('title','Unknown'), "Not Found - Retry Unlikely")
        except Exception:
            pass
        return False

# ========================
# Sonarr Integration (TV Series & Episodes)
# ========================
def trigger_sonarr_search(series_id, series_title=None):
    try:
        response = requests.post(f"{SONARR_URL}/command", json={'name': 'SeriesSearch', 'seriesId': series_id}, headers={'X-Api-Key': SONARR_API_KEY})
        response.raise_for_status()
        logger.debug(f"Sonarr search triggered for series id {series_id}", extra={'emoji_type': 'debug'})
        if series_title:
            logger.info(f"Triggered search for {series_title}", extra={'emoji_type': 'search', 'tv': '📺'})
        return True
    except Exception as e:
        logger.error(f"Sonarr search failed: {e}", extra={'emoji_type': 'error', 'tv': '📺'})
        return False

def search_in_sonarr(tvdb_id, rating_key, episode_mode=False):
    try:
        existing_response = requests.get(f"{SONARR_URL}/series", params={'tvdbId': tvdb_id}, headers={'X-Api-Key': SONARR_API_KEY})
        if existing_response.status_code == 200 and existing_response.json():
            series = existing_response.json()[0]
            logger.info(f"Series already exists in Sonarr: {series['title']}", extra={'emoji_type': 'info', 'tv': '📺'})
            if not episode_mode:
                if not series.get("monitored"):
                    series_response = requests.get(f"{SONARR_URL}/series/{series['id']}", headers={'X-Api-Key': SONARR_API_KEY})
                    series_response.raise_for_status()
                    series_data = series_response.json()
                    series_data['monitored'] = True
                    update_response = requests.put(f"{SONARR_URL}/series/{series['id']}", json=series_data, headers={'X-Api-Key': SONARR_API_KEY})
                    update_response.raise_for_status()
                    logger.info(f"Series is now monitored: {series['title']}", extra={'emoji_type': 'monitored', 'tv': '📺'})
                update_plex_title(rating_key, series['title'], "Searching for Episodes...")
                return trigger_sonarr_search(series['id'], series['title'])
            else:
                return series['id']
        lookup = requests.get(f"{SONARR_URL}/series/lookup", params={'term': f"tvdb:{tvdb_id}"}, headers={'X-Api-Key': SONARR_API_KEY})
        lookup.raise_for_status()
        series_data = lookup.json()[0]
        payload = {
            'title': series_data['title'],
            'qualityProfileId': 3,
            'titleSlug': series_data['titleSlug'],
            'tvdbId': series_data['tvdbId'],
            'year': series_data['year'],
            'rootFolderPath': '/mnt/user/data/infinite/tv',
            'monitored': True,
            'addOptions': {'searchForMissingEpisodes': True},
            'seasons': []
        }
        for season in series_data.get('seasons', []):
            if season.get('seasonNumber', 0) > 0:
                payload['seasons'].append({'seasonNumber': season['seasonNumber'], 'monitored': season.get('monitored', False)})
        response = requests.post(f"{SONARR_URL}/series", json=payload, headers={'X-Api-Key': SONARR_API_KEY})
        response.raise_for_status()
        logger.info(f"Added series: {series_data['title']}", extra={'emoji_type': 'success', 'tv': '📺'})
        update_plex_title(rating_key, series_data['title'], "Searching for Episodes...")
        return trigger_sonarr_search(response.json()['id'], series_data['title'])
    except Exception as e:
        logger.error(f"Sonarr operation failed: {e}", extra={'emoji_type': 'error', 'tv': '📺'})
        try:
            update_plex_title(rating_key, series_data['title'], "Not Found - Retry Unlikely")
        except Exception:
            pass
        return False

def trigger_sonarr_episode_search(episode_id):
    try:
        episode_id_int = int(episode_id)
        response = requests.post(f"{SONARR_URL}/command", json={'name': 'EpisodeSearch', 'episodeIds': [episode_id_int]}, headers={'X-Api-Key': SONARR_API_KEY})
        response.raise_for_status()
        logger.debug(f"Sonarr episode search triggered for episode id {episode_id_int}", extra={'emoji_type': 'debug'})
        return True
    except Exception as e:
        logger.error(f"Sonarr episode search failed: {e}", extra={'emoji_type': 'error', 'tv': '📺'})
        return False

# ========================
# Monitoring Functions
# ========================
def check_has_file(tmdb_id, base_title, rating_key, attempts=0, start_time=None):
    try:
        if start_time is None:
            start_time = time.time()
        elif not PROGRESS_FLAGS.get(rating_key, False) and (time.time() - start_time > MAX_MONITOR_TIME):
            update_plex_title(rating_key, base_title, "Not Found - Search Timeout")
            logger.info(f"Movie search timeout for {base_title}", extra={'emoji_type': 'warning'})
            with TIMER_LOCK:
                ACTIVE_SEARCH_TIMERS.pop(rating_key, None)
            return
        if attempts >= CHECK_MAX_ATTEMPTS:
            update_plex_title(rating_key, base_title, "Not Found - Retry Unlikely")
            logger.info(f"Movie monitoring ended for {base_title} after {CHECK_MAX_ATTEMPTS} attempts", extra={'emoji_type': 'warning'})
            with TIMER_LOCK:
                ACTIVE_SEARCH_TIMERS.pop(rating_key, None)
            return
        radarr_response = requests.get(f"{RADARR_URL}/movie", headers={'X-Api-Key': RADARR_API_KEY})
        radarr_response.raise_for_status()
        movies = radarr_response.json()
        if not isinstance(movies, list):
            logger.error(f"Expected list from Radarr /movie endpoint but got {type(movies)}", extra={'emoji_type': 'error'})
            return
        matched_movies = [m for m in movies if int(m.get("tmdbId", 0)) == int(tmdb_id)]
        if not matched_movies:
            logger.warning(f"Movie not found in Radarr: {base_title}", extra={'emoji_type': 'warning'})
            update_plex_title(rating_key, base_title, "Searching...")
        else:
            movie = matched_movies[0]
            if movie.get("hasFile"):
                update_plex_title(rating_key, base_title, "Available")
                delete_dummy_files("movie", base_title, movie.get("year"), tmdb_id, MOVIE_LIBRARY_FOLDER)
                with TIMER_LOCK:
                    ACTIVE_SEARCH_TIMERS.pop(rating_key, None)
                return
            else:
                queue_response = requests.get(f"{RADARR_URL}/queue", headers={'X-Api-Key': RADARR_API_KEY})
                queue_response.raise_for_status()
                queue_data = queue_response.json()
                if isinstance(queue_data, dict):
                    queue_items = queue_data.get("records", [])
                else:
                    queue_items = queue_data
                if not isinstance(queue_items, list):
                    logger.error(f"Expected list from Radarr /queue endpoint but got {type(queue_items)}", extra={'emoji_type': 'error'})
                    return
                queue_item = next((item for item in queue_items if item.get('movieId') == movie.get('id')), None)
                if queue_item:
                    progress = (1 - (queue_item.get('sizeleft', 0) / queue_item.get('size', 1))) * 100
                    status = f"Downloading {int(progress)}%"
                    PROGRESS_FLAGS[rating_key] = True
                    logger.info(f"Download progress for {base_title}: {int(progress)}%", extra={'emoji_type': 'progress'})
                else:
                    if PROGRESS_FLAGS.get(rating_key, False):
                        update_plex_title(rating_key, base_title, "Download Canceled")
                        logger.info(f"Movie download canceled: {base_title}", extra={'emoji_type': 'warning'})
                        with TIMER_LOCK:
                            ACTIVE_SEARCH_TIMERS.pop(rating_key, None)
                        return
                    else:
                        status = "Searching..."
                        logger.debug(f"No queue item found for {base_title}", extra={'emoji_type': 'debug'})
                update_plex_title(rating_key, base_title, status)
        timer = threading.Timer(CHECK_INTERVAL, check_has_file,
                                args=[tmdb_id, base_title, rating_key, attempts + 1, start_time])
        with TIMER_LOCK:
            ACTIVE_SEARCH_TIMERS[rating_key] = timer
        timer.start()
    except Exception as e:
        logger.error(f"Movie file check failed: {e}", extra={'emoji_type': 'error'})
        timer = threading.Timer(CHECK_INTERVAL, check_has_file,
                                args=[tmdb_id, base_title, rating_key, attempts + 1, start_time])
        with TIMER_LOCK:
            ACTIVE_SEARCH_TIMERS[rating_key] = timer
        timer.start()

def check_tv_has_file(tvdb_id, base_title, rating_key, attempts=0, season_number=None, episode_number=None, start_time=None):
    try:
        if start_time is None:
            start_time = time.time()
        elif not PROGRESS_FLAGS.get(rating_key, False) and (time.time() - start_time > MAX_MONITOR_TIME):
            update_plex_title(rating_key, base_title, "Not Found - Search Timeout")
            logger.info(f"TV search timeout for {base_title}", extra={'emoji_type': 'warning', 'tv': '📺'})
            with TIMER_LOCK:
                ACTIVE_SEARCH_TIMERS.pop(rating_key, None)
            return
        if attempts >= CHECK_MAX_ATTEMPTS:
            update_plex_title(rating_key, base_title, "Not Found - Retry Unlikely")
            logger.info(f"TV monitoring ended for {base_title} after {CHECK_MAX_ATTEMPTS} attempts", extra={'emoji_type': 'warning', 'tv': '📺'})
            with TIMER_LOCK:
                ACTIVE_SEARCH_TIMERS.pop(rating_key, None)
            return
        logger.debug(f"Checking Sonarr for {base_title} (tvdb: {tvdb_id})", extra={'emoji_type': 'debug'})
        sonarr_response = requests.get(f"{SONARR_URL}/series", params={'tvdbId': tvdb_id}, headers={'X-Api-Key': SONARR_API_KEY})
        sonarr_response.raise_for_status()
        series_list = sonarr_response.json()
        if not series_list:
            logger.error(f"Series not found for {base_title}", extra={'emoji_type': 'error'})
            return
        series = series_list[0]
        series_id = series['id']
        episodes_response = requests.get(f"{SONARR_URL}/episode", params={'seriesId': series_id}, headers={'X-Api-Key': SONARR_API_KEY})
        episodes_response.raise_for_status()
        episodes = episodes_response.json()
        target_episode = next((ep for ep in episodes
                               if int(ep.get('seasonNumber', 0)) == int(season_number)
                               and int(ep.get('episodeNumber', 0)) == int(episode_number)), None)
        if target_episode:
            if target_episode.get('hasFile'):
                update_plex_title(rating_key, base_title, "Available")
                logger.info(f"Episode downloaded: {base_title}", extra={'emoji_type': 'success'})
                delete_dummy_files("tv", series.get("title"), series.get("year"), tvdb_id, TV_LIBRARY_FOLDER, season_number, episode_number)
                with TIMER_LOCK:
                    ACTIVE_SEARCH_TIMERS.pop(rating_key, None)
                return
            queue_response = requests.get(f"{SONARR_URL}/queue", headers={'X-Api-Key': SONARR_API_KEY})
            queue_response.raise_for_status()
            queue = queue_response.json()
            records = queue.get('records', [])
            queue_item = next((item for item in records if item.get('episodeId') == target_episode.get('id')), None)
            if queue_item:
                progress = 100 - (queue_item.get('sizeleft', 0) / queue_item.get('size', 1) * 100)
                status = f"Downloading {int(progress)}%"
                PROGRESS_FLAGS[rating_key] = True
                logger.info(f"Download progress for {base_title}: {int(progress)}%", extra={'emoji_type': 'progress'})
            else:
                if PROGRESS_FLAGS.get(rating_key, False):
                    update_plex_title(rating_key, base_title, "Download Canceled")
                    logger.info(f"Episode download canceled: {base_title}", extra={'emoji_type': 'warning', 'tv': '📺'})
                    with TIMER_LOCK:
                        ACTIVE_SEARCH_TIMERS.pop(rating_key, None)
                    return
                else:
                    status = "Searching..."
                    logger.debug(f"No queue item found for {base_title}", extra={'emoji_type': 'debug', 'tv': '📺'})
            update_plex_title(rating_key, base_title, status)
        timer = threading.Timer(CHECK_INTERVAL, check_tv_has_file,
                                args=[tvdb_id, base_title, rating_key, attempts + 1, season_number, episode_number, start_time])
        with TIMER_LOCK:
            ACTIVE_SEARCH_TIMERS[rating_key] = timer
        timer.start()
    except Exception as e:
        logger.error(f"TV file check failed: {e}", extra={'emoji_type': 'error'})
        timer = threading.Timer(CHECK_INTERVAL, check_tv_has_file,
                                args=[tvdb_id, base_title, rating_key, attempts + 1, season_number, episode_number, start_time])
        with TIMER_LOCK:
            ACTIVE_SEARCH_TIMERS[rating_key] = timer
        timer.start()

# ========================
# Webhook Handlers
# ========================
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def handle_webhook():
    try:
        data = request.get_json()
        source = data.get("instanceName", "Tautulli")
        logger.debug(f"{source} payload: {data}", extra={'emoji_type': 'debug'})
        event_type = (data.get('event') or data.get('eventType') or 'unknown').lower()
        rating_key = data.get('rating_key')
        logger.info(f"Received webhook event: {event_type}", extra={'emoji_type': 'webhook'})
        
        if event_type == 'seriesadd':
            return handle_seriesadd(data)
        elif event_type == 'episodefiledelete':
            return handle_episodefiledelete(data)
        elif event_type == 'moviefiledelete':
            return handle_moviefiledelete(data)
        elif event_type == 'moviedelete':
            return handle_movie_delete(data)
        elif event_type == 'movieadd':
            return handle_movieadd(data)
        elif event_type == 'seriesdelete':
            return handle_seriesdelete(data)
        elif event_type == 'playback.start':
            return handle_playback(data)
        return handle_arrs_import(data)
    except Exception as e:
        logger.error(f"Webhook handling failed: {e}", extra={'emoji_type': 'error'})
        return jsonify({"status": "error", "message": str(e)}), 500

def handle_seriesadd(data):
    series = data.get('series', {})
    episodes = data.get('episodes', [])
    series_title = series.get('title', 'Unknown Series')
    series_year = series.get('year')
    tvdb_id = series.get('tvdbId')
    if not episodes:
        series_id = series.get('id')
        if series_id:
            sonarr_response = requests.get(f"{SONARR_URL}/episode", params={'seriesId': series_id}, headers={'X-Api-Key': SONARR_API_KEY})
            sonarr_response.raise_for_status()
            episodes = sonarr_response.json()
        else:
            logger.warning("No series ID provided in seriesadd event.", extra={'emoji_type': 'warning'})
            episodes = []
    unique_folders = set()
    for ep in episodes:
        season_num = ep.get('seasonNumber')
        episode_num = ep.get('episodeNumber')
        if not (season_num and episode_num):
            continue
        dummy_path = place_dummy_file("tv", series_title, series_year, tvdb_id, TV_LIBRARY_FOLDER,
                                       season_number=season_num, episode_range=(episode_num, episode_num), episode_id=ep.get("id"))
        logger.info(f"Created dummy file for {series_title} S{season_num}E{episode_num} at {dummy_path}", extra={'emoji_type': 'dummy'})
        series_folder = os.path.dirname(os.path.dirname(dummy_path))
        unique_folders.add(series_folder)
        schedule_episode_request_update(series_title, season_num, episode_num, tvdb_id, delay=10, retries=5)
    for folder in unique_folders:
        refresh_plex_folder(folder, PLEX_TV_SECTION_ID)
    return jsonify({"status": "success", "message": "SeriesAdd processed"}), 200

def handle_episodefiledelete(data):
    series = data.get('series', {})
    episodes = data.get('episodes', [])
    series_title = series.get('title', 'Unknown Series')
    series_year = series.get('year')
    tvdb_id = series.get('tvdbId')
    for ep in episodes:
        season_num = ep.get('seasonNumber')
        episode_num = ep.get('episodeNumber')
        if not (season_num and episode_num):
            file_field = data.get('file', '')
            m = re.search(r'[sS](\d{1,2})[eE](\d{1,2})', file_field)
            if m:
                season_num, episode_num = map(int, m.groups())
        if not (season_num and episode_num):
            logger.info(f"No episode number provided; not creating dummy for {series_title}", extra={'emoji_type': 'info'})
            continue
        dummy_path = place_dummy_file("tv", series_title, series_year, tvdb_id, TV_LIBRARY_FOLDER,
                                       season_number=season_num, episode_range=(episode_num, episode_num), episode_id=ep.get("id"))
        logger.info(f"Created dummy file for {series_title} S{season_num}E{episode_num} at {dummy_path}", extra={'emoji_type': 'dummy'})
        series_folder = os.path.dirname(os.path.dirname(dummy_path))
        refresh_plex_folder(series_folder, PLEX_TV_SECTION_ID)
        schedule_episode_request_update(series_title, season_num, episode_num, tvdb_id, delay=10, retries=5)
    return jsonify({"status": "success"}), 200

def handle_moviefiledelete(data):
    if 'movie' in data:
        movie = data.get('movie', {})
        tmdb_id = movie.get('tmdbId') or data.get('remoteMovie', {}).get('tmdbId')
        if not tmdb_id:
            logger.error("Missing TMDB ID for movie file delete", extra={'emoji_type': 'error'})
            return jsonify({"status": "error"}), 400
        title = movie.get('title', 'Unknown Movie')
        year = movie.get('year')
        expected_dummy = os.path.join(MOVIE_LIBRARY_FOLDER,
                                      f"{sanitize_filename(title)}{' ('+str(year)+')' if year else ''} {{tmdb-{tmdb_id}}}",
                                      f"{sanitize_filename(title)}{' ('+str(year)+')' if year else ''} (dummy).mp4")
        if not os.path.exists(expected_dummy):
            dummy_path = place_dummy_file("movie", title, year, tmdb_id, MOVIE_LIBRARY_FOLDER)
            logger.info(f"Created dummy file for movie '{title}' at {dummy_path}", extra={'emoji_type': 'dummy'})
            movie_folder = os.path.dirname(dummy_path)
            refresh_plex_folder(movie_folder, PLEX_MOVIE_SECTION_ID)
            schedule_movie_request_update(title, tmdb_id, delay=10, retries=5)
        else:
            logger.info(f"Dummy file already exists for movie '{title}'", extra={'emoji_type': 'info'})
    return jsonify({"status": "success"}), 200

def handle_movie_delete(data):
    if 'movie' in data:
        movie = data.get('movie', {})
        tmdb_id = movie.get('tmdbId') or data.get('remoteMovie', {}).get('tmdbId')
        if not tmdb_id:
            logger.error("Missing TMDB ID for movie delete", extra={'emoji_type': 'error'})
            return jsonify({"status": "error"}), 400
        dummy_path = os.path.join(MOVIE_LIBRARY_FOLDER,
                                  f"{sanitize_filename(movie.get('title', ''))}{' ('+str(movie.get('year'))+')' if movie.get('year') else ''} {{tmdb-{tmdb_id}}}",
                                  f"{sanitize_filename(movie.get('title', ''))}{' ('+str(movie.get('year'))+')' if movie.get('year') else ''} (dummy).mp4")
        if os.path.exists(dummy_path):
            os.remove(dummy_path)
            logger.info(f"Deleted dummy file for movie {movie.get('title')} (movie delete event)", extra={'emoji_type': 'delete'})
        else:
            logger.info(f"No dummy file exists for movie {movie.get('title')} on movie delete event", extra={'emoji_type': 'info'})
        folder = os.path.join(MOVIE_LIBRARY_FOLDER,
                              f"{sanitize_filename(movie.get('title', ''))}{' ('+str(movie.get('year'))+')' if movie.get('year') else ''} {{tmdb-{tmdb_id}}}")
        refresh_plex_folder(folder, PLEX_MOVIE_SECTION_ID)
    return jsonify({"status": "success"}), 200

def handle_movieadd(data):
    if 'movie' in data:
        movie = data.get('movie', {})
        tmdb_id = movie.get('tmdbId') or data.get('remoteMovie', {}).get('tmdbId')
        if not tmdb_id:
            logger.error("Missing TMDB ID for movie add", extra={'emoji_type': 'error'})
            return jsonify({"status": "error"}), 400
        title = movie.get('title', 'Unknown Movie')
        year = movie.get('year', '')
        dummy_path = place_dummy_file("movie", title, year, tmdb_id, MOVIE_LIBRARY_FOLDER)
        logger.info(f"Created dummy file for newly added movie '{title}' at {dummy_path}", extra={'emoji_type': 'dummy'})
        movie_folder = os.path.dirname(dummy_path)
        refresh_plex_folder(movie_folder, PLEX_MOVIE_SECTION_ID)
        schedule_movie_request_update(title, tmdb_id, delay=10, retries=5)
    return jsonify({"status": "success"}), 200

def handle_seriesdelete(data):
    if 'series' in data:
        series = data.get('series', {})
        series_folder = os.path.join(TV_LIBRARY_FOLDER,
                                      f"{sanitize_filename(series.get('title',''))}{' ('+str(series.get('year'))+')' if series.get('year') else ''} {{tvdb-{series.get('tvdbId')}}}")
        if os.path.exists(series_folder):
            shutil.rmtree(series_folder)
            logger.info(f"Deleted series folder for {series.get('title')}", extra={'emoji_type': 'delete'})
        refresh_plex_folder(series_folder, PLEX_TV_SECTION_ID)
    return jsonify({"status": "success"}), 200

def handle_playback(data):
    try:
        media_type = data.get('media_type', '').lower()
        title = data.get('title', 'Unknown Title')
        rating_key = data.get('rating_key')
        if media_type == 'movie':
            if not data.get('tmdb_id'):
                logger.error("Missing TMDB ID for movie", extra={'emoji_type': 'error'})
                return jsonify({"status": "error", "message": "Missing TMDB ID"}), 400
            tmdb_id = data.get('tmdb_id')
            base_title = strip_movie_status(sanitize_filename(title))
            BASE_TITLES[rating_key] = base_title
            update_plex_title(rating_key, base_title, "Searching...")
            success = search_in_radarr(tmdb_id, rating_key)
            with TIMER_LOCK:
                if rating_key not in ACTIVE_SEARCH_TIMERS:
                    timer = threading.Timer(0, check_has_file, args=[tmdb_id, base_title, rating_key, 0, time.time()])
                    ACTIVE_SEARCH_TIMERS[rating_key] = timer
                    timer.start()
            return jsonify({"status": "success"}), 200
        elif media_type == 'episode':
            file_field = data.get('file', '')
            logger.debug(f"Processing episode playback: {title}", extra={'emoji_type': 'playback'})
            match = re.search(r'[sS](\d{1,2})[eE](\d{1,2})', file_field)
            if not match:
                logger.warning("Could not extract season/episode from file field", extra={'emoji_type': 'warning'})
                return jsonify({"status": "error", "message": "No episode number found"}), 400
            season_number, episode_number = map(int, match.groups())
            tvdb_id = data.get('thetvdb_id', '') or data.get('grandparent_guid', '').split('/')[-1]
            if not tvdb_id:
                logger.error("Missing TVDB ID in webhook data", extra={'emoji_type': 'error'})
                return jsonify({"status": "error", "message": "Missing TVDB ID"}), 400
            base_title = extract_episode_title(title)
            BASE_TITLES[rating_key] = base_title
            PROGRESS_FLAGS[rating_key] = False
            logger.info(f"Processing {base_title} (TVDB: {tvdb_id})", extra={'emoji_type': 'processing'})
            check_tv_has_file(tvdb_id, base_title, rating_key, 0, season_number, episode_number, start_time=time.time())
            id_match = re.search(r"\[ID:(\d+)\]", file_field)
            if id_match:
                episode_id = id_match.group(1)
                trigger_sonarr_episode_search(episode_id)
                logger.info(f"Triggered search for episode {base_title}", extra={'emoji_type': 'search'})
            else:
                logger.error("Episode ID not found in filename", extra={'emoji_type': 'error'})
            return jsonify({"status": "success"}), 200
        logger.warning(f"Unsupported media type: {media_type}", extra={'emoji_type': 'warning'})
        return jsonify({"status": "error", "message": "Unsupported media type"}), 400
    except Exception as e:
        logger.error(f"Playback handling failed: {e}", extra={'emoji_type': 'error'})
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/webhook', methods=['POST'])
def handle_arrs_import(data=None):
    try:
        if data is None:
            data = request.get_json()
        event_type = data.get('eventType', 'unknown').lower()
        logger.info(f"Handling import event of type: {event_type}", extra={'emoji_type': 'webhook'})
        return jsonify({"status": "success"}), 200
    except Exception as e:
        logger.error(f"Import handling failed: {e}", extra={'emoji_type': 'error'})
        return jsonify({"status": "error", "message": str(e)}), 500

# ========================
# Gunicorn Application Runner
# ========================
class StandaloneApplication(BaseApplication):
    def __init__(self, app, options=None):
        self.options = options or {}
        self.application = app
        super(StandaloneApplication, self).__init__()

    def load_config(self):
        config = {key: value for key, value in self.options.items() if key in self.cfg.settings and value is not None}
        for key, value in config.items():
            self.cfg.set(key, value)

    def load(self):
        return self.application

if __name__ == '__main__':
    options = {
        'bind': '0.0.0.0:5000',
        'workers': WORKER_COUNT,
    }
    StandaloneApplication(app, options).run()
