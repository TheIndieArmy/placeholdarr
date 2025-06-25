import os  # <-- Add this import
import threading
import time
from datetime import datetime, timedelta, timezone
import requests
from core.config import settings
from core.logger import logger
from services.integrations import place_dummy_file, schedule_episode_request_update, schedule_movie_request_update
from services.plex_client import refresh_plex_item, update_plex_title_status
from services.utils import sanitize_filename

# --- Scheduler/Timer ---

SYNC_TIMER = None

def start_calendar_sync():
    """Start the periodic calendar sync based on ENV settings."""
    global SYNC_TIMER
    interval = int(getattr(settings, "CALENDAR_SYNC_INTERVAL_HOURS", 12)) * 3600
    logger.info(f"Starting calendar sync timer (interval: {interval // 3600} hours)", extra={'emoji_type': 'process'})
    def run_and_reschedule():
        try:
            sync_calendar_episodes()
        except Exception as e:
            logger.error(f"Calendar sync failed: {e}", extra={'emoji_type': 'error'})
        finally:
            # Reschedule
            global SYNC_TIMER
            SYNC_TIMER = threading.Timer(interval, run_and_reschedule)
            SYNC_TIMER.daemon = True
            SYNC_TIMER.start()
    # Start immediately
    threading.Thread(target=run_and_reschedule, daemon=True).start()

def stop_calendar_sync():
    global SYNC_TIMER
    if SYNC_TIMER:
        SYNC_TIMER.cancel()
        SYNC_TIMER = None
        logger.info("Stopped calendar sync timer", extra={'emoji_type': 'info'})

# --- Main Sync Function ---

def sync_calendar_episodes():
    """Fetch upcoming episodes/movies and manage placeholders/statuses in a true batch."""
    logger.info("Running calendar sync for upcoming content", extra={'emoji_type': 'process'})
    lookahead_days = int(getattr(settings, "CALENDAR_LOOKAHEAD_DAYS", 30))
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=lookahead_days)
    enable_placeholders = str(getattr(settings, "ENABLE_COMING_SOON_PLACEHOLDERS", "true")).lower() == "true"
    enable_countdown = str(getattr(settings, "ENABLE_COMING_SOON_COUNTDOWN", "true")).lower() == "true"
    preferred_movie_date = getattr(settings, "PREFERRED_MOVIE_DATE_TYPE", "inCinemas")

    # --- Gather all episodes and movies ---
    episodes_to_update = []
    movies_to_update = []

    # --- Sonarr: TV Episodes ---
    try:
        sonarr_url = settings.SONARR_URL
        sonarr_api_key = settings.SONARR_API_KEY
        calendar_url = f"{sonarr_url}/calendar"
        params = {
            "start": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": window_end.strftime("%Y-%m-%dT%H:%M:%SZ")
        }
        headers = {"X-Api-Key": sonarr_api_key}
        response = requests.get(calendar_url, params=params, headers=headers)
        response.raise_for_status()
        episodes = response.json()
        logger.info(f"Fetched {len(episodes)} upcoming episodes from Sonarr calendar", extra={'emoji_type': 'info'})

        for ep in episodes:
            # Fetch series info if needed
            if 'series' in ep and ep['series']:
                series = ep['series']
            else:
                series_id = ep.get('seriesId')
                series = None
                if series_id:
                    s_resp = requests.get(f"{sonarr_url}/series/{series_id}", headers=headers)
                    if s_resp.status_code == 200:
                        series = s_resp.json()
            if not series:
                continue
            series_title = series.get('title', 'Unknown Series')
            series_year = series.get('year')
            tvdb_id = series.get('tvdbId')
            season_num = ep.get('seasonNumber')
            episode_num = ep.get('episodeNumber')
            episode_title = ep.get('title')
            air_date_str = ep.get('airDateUtc') or ep.get('airDate')
            air_date = _parse_air_date(air_date_str)
            if not air_date:
                continue
            if not enable_placeholders:
                continue
            # Only create placeholder if not already aired
            if air_date > now:
                status = _build_coming_soon_status(air_date, now, enable_countdown)
                dummy_file = getattr(settings, "COMING_SOON_DUMMY_FILE_PATH", "") or settings.DUMMY_FILE_PATH
            else:
                status = "Request"
                dummy_file = settings.DUMMY_FILE_PATH
            dummy_path = place_dummy_file(
                "tv", series_title, series_year, tvdb_id,
                settings.TV_LIBRARY_FOLDER,
                season_number=season_num,
                episode_range=(episode_num, episode_num),
                episode_title=episode_title,
                dummy_file_override=dummy_file
            )
            episodes_to_update.append({
                "series_title": series_title,
                "series_year": series_year,
                "tvdb_id": tvdb_id,
                "season_num": season_num,
                "episode_num": episode_num,
                "status": status,
                "air_date": air_date
            })
    except Exception as e:
        logger.error(f"Sonarr calendar sync failed: {e}", extra={'emoji_type': 'error'})

    # --- Radarr: Movies ---
    try:
        radarr_url = settings.RADARR_URL
        radarr_api_key = settings.RADARR_API_KEY
        calendar_url = f"{radarr_url}/calendar"
        params = {
            "start": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": window_end.strftime("%Y-%m-%dT%H:%M:%SZ")
        }
        headers = {"X-Api-Key": radarr_api_key}
        response = requests.get(calendar_url, params=params, headers=headers)
        response.raise_for_status()
        movies = response.json()
        logger.info(f"Fetched {len(movies)} upcoming movies from Radarr calendar", extra={'emoji_type': 'info'})

        for movie in movies:
            title = movie.get('title', 'Unknown Movie')
            year = movie.get('year')
            tmdb_id = movie.get('tmdbId')
            date_str = movie.get(preferred_movie_date) or movie.get('inCinemas') or movie.get('digitalRelease') or movie.get('physicalRelease')
            air_date = _parse_air_date(date_str)
            if not air_date:
                continue
            if not enable_placeholders:
                continue
            if air_date > now:
                status = _build_coming_soon_status(air_date, now, enable_countdown)
                dummy_file = getattr(settings, "COMING_SOON_DUMMY_FILE_PATH", "") or settings.DUMMY_FILE_PATH
            else:
                status = "Request"
                dummy_file = settings.DUMMY_FILE_PATH
            dummy_path = place_dummy_file(
                "movie", title, year, tmdb_id, settings.MOVIE_LIBRARY_FOLDER,
                dummy_file_override=dummy_file
            )
            movies_to_update.append({
                "title": title,
                "year": year,
                "tmdb_id": tmdb_id,
                "status": status,
                "air_date": air_date
            })
    except Exception as e:
        logger.error(f"Radarr calendar sync failed: {e}", extra={'emoji_type': 'error'})

    # --- Batch Plex refresh ---
    try:
        logger.info("Refreshing Plex TV and Movie library folders for batch placeholder update...", extra={'emoji_type': 'refresh'})
        refresh_plex_item(settings.TV_LIBRARY_FOLDER)
        refresh_plex_item(settings.MOVIE_LIBRARY_FOLDER)
        logger.info("Waiting 30 seconds for Plex to scan new placeholders...", extra={'emoji_type': 'refresh'})
        time.sleep(30)
    except Exception as e:
        logger.error(f"Error during batch Plex refresh: {e}", extra={'emoji_type': 'error'})

    # --- Batch update episode titles ---
    updated_eps = []
    for ep in episodes_to_update:
        try:
            update_plex_title_status(
                media_type='tv',
                media_id=ep["tvdb_id"],
                title=ep["series_title"],
                status=ep["status"],
                season=ep["season_num"],
                episode=ep["episode_num"]
            )
            updated_eps.append(f"{ep['series_title']} S{ep['season_num']:02d}E{ep['episode_num']:02d}")
            # Schedule status transition if air_date is today
            if ep["air_date"].date() == now.date():
                schedule_episode_request_update(ep["series_title"], ep["season_num"], ep["episode_num"], ep["tvdb_id"], delay=3600, retries=3)
        except Exception as e:
            logger.error(f"Failed to update Plex title for {ep['series_title']} S{ep['season_num']:02d}E{ep['episode_num']:02d}: {e}", extra={'emoji_type': 'error'})
    if updated_eps:
        logger.info(f"Batch updated episode titles: {', '.join(updated_eps)}", extra={'emoji_type': 'update'})

    # --- Batch update movie titles ---
    updated_movies = []
    for movie in movies_to_update:
        try:
            update_plex_title_status(
                media_type='movie',
                media_id=movie["tmdb_id"],
                title=movie["title"],
                status=movie["status"],
                year=movie["year"]
            )
            updated_movies.append(f"{movie['title']} ({movie['year']})")
            if movie["air_date"].date() == now.date():
                schedule_movie_request_update(movie["title"], movie["tmdb_id"], year=movie["year"], delay=3600, retries=3)
        except Exception as e:
            logger.error(f"Failed to update Plex title for movie {movie['title']}: {e}", extra={'emoji_type': 'error'})
    if updated_movies:
        logger.info(f"Batch updated movie titles: {', '.join(updated_movies)}", extra={'emoji_type': 'update'})

    # --- Debugging: Jellyfin summary updates ---
    logger.info(f"[Jellyfin Debug] settings.jellyfin_enabled={getattr(settings, 'jellyfin_enabled', None)}, ENABLE_JELLYFIN={getattr(settings, 'ENABLE_JELLYFIN', None)}, JELLYFIN_URL={getattr(settings, 'JELLYFIN_URL', None)}, JELLYFIN_TOKEN={'set' if getattr(settings, 'JELLYFIN_TOKEN', None) else 'unset'}", extra={'emoji_type': 'debug'})
    for ep in episodes_to_update:
        logger.debug(f"Attempting Jellyfin overview update: {ep['series_title']} S{ep['season_num']:02d}E{ep['episode_num']:02d} (TVDB {ep['tvdb_id']})", extra={'emoji_type': 'debug'})
        try:
            from services.jellyfin_client import find_jellyfin_item_id, update_jellyfin_title_status
            jellyfin_id = find_jellyfin_item_id(
                media_type='tv',
                external_id=ep['tvdb_id'],
                title=ep['series_title'],
                season=ep['season_num'],
                episode=ep['episode_num']
            )
            logger.debug(f"Jellyfin item ID for update: {jellyfin_id}", extra={'emoji_type': 'debug'})
            if jellyfin_id:
                result = update_jellyfin_title_status(
                    media_type='tv',
                    item_id=jellyfin_id,
                    title=ep['series_title'],
                    status=ep['status'],
                    season=ep['season_num'],
                    episode=ep['episode_num']
                )
                if not result:
                    logger.warning(f"[Jellyfin Update] Update returned False for {ep['series_title']} S{ep['season_num']:02d}E{ep['episode_num']:02d} (item_id={jellyfin_id})", extra={'emoji_type': 'warning'})
                logger.info(f"Jellyfin overview update result for {ep['series_title']} S{ep['season_num']:02d}E{ep['episode_num']:02d}: {result}", extra={'emoji_type': 'debug'})
            else:
                logger.warning(f"Could not find Jellyfin item for {ep['series_title']} S{ep['season_num']:02d}E{ep['episode_num']:02d}", extra={'emoji_type': 'warning'})
        except Exception as ex:
            logger.error(f"Exception during Jellyfin overview update for {ep['series_title']} S{ep['season_num']:02d}E{ep['episode_num']:02d}: {ex}", extra={'emoji_type': 'error'})

# --- Helper Functions ---

def _build_coming_soon_status(air_date, now, enable_countdown):
    """Build the 'Coming Soon' status string, optionally with countdown."""
    if enable_countdown:
        days_left = (air_date.date() - now.date()).days
        if days_left > 0:
            return f"Coming Soon ({days_left} days)"
        else:
            return "Coming Soon (Today)"
    return "Coming Soon"

def _parse_air_date(date_str):
    """Parse air/release date string to datetime (UTC)."""
    if not date_str:
        return None
    try:
        # Try parsing with timezone info
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        try:
            # Fallback: parse as naive UTC
            return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            logger.warning(f"Could not parse air date: {date_str}", extra={'emoji_type': 'warning'})
            return None

# --- Startup Hook (optional) ---
# To start calendar sync automatically, import and call start_calendar_sync() from your app's startup code.
