import threading
import time
from datetime import datetime, timedelta, timezone
import requests
import re
from core.config import settings
from core.logger import logger
from services.integrations import place_dummy_file, schedule_episode_request_update, schedule_movie_request_update, update_title_status
from services.plex_client import refresh_plex_item
from services.jellyfin_client import refresh_jellyfin_item
from services.utils import sanitize_filename, resolve_final_folder, get_arr_config, join_endpoint, get_root_folder_from_path

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

    episodes_to_update = []
    movies_to_update = []

    # --- Sonarr: TV Episodes ---
    try:
        arr_cfg = get_arr_config("tv")
        sonarr_url = arr_cfg["url"]
        sonarr_api_key = arr_cfg["api_key"]
        calendar_url = join_endpoint(sonarr_url, "calendar")
        # Extend window to include previous 2 days
        calendar_start = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {
            "start": calendar_start,
            "end": window_end.strftime("%Y-%m-%dT%H:%M:%SZ")
        }
        headers = {"X-Api-Key": sonarr_api_key}
        response = requests.get(calendar_url, params=params, headers=headers)
        response.raise_for_status()
        episodes = response.json()
        logger.info(f"Fetched {len(episodes)} upcoming+recent episodes from Sonarr calendar", extra={'emoji_type': 'info'})

        # --- Step 1: Aggregate all eligible episodes and their info ---
        all_episodes = []
        # Cache Sonarr episode listings per series to allow authoritative hasFile checks
        sonarr_episode_cache = {}
        for ep in episodes:
            # Fetch series info if needed
            if 'series' in ep and ep['series']:
                series = ep['series']
            else:
                series_id = ep.get('seriesId')
                series = None
                if series_id:
                    s_resp = requests.get(join_endpoint(sonarr_url, f"series/{series_id}"), headers=headers)
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
            # --- Always check for both folderPath and path ---
            folder_path = series.get('folderPath') or series.get('path')
            arr_root_folder = get_root_folder_from_path(folder_path)
            # --- Authoritative hasFile from Sonarr: fetch per-series episode list once and cache ---
            series_internal_id = series.get('id') or ep.get('seriesId')
            has_file = False
            if series_internal_id:
                if series_internal_id not in sonarr_episode_cache:
                    try:
                        se_resp = requests.get(join_endpoint(sonarr_url, 'episode'), params={'seriesId': series_internal_id}, headers=headers)
                        se_resp.raise_for_status()
                        se_list = se_resp.json()
                        # Build map (season, episode) -> hasFile
                        m = {}
                        for s_ep in se_list:
                            key = (s_ep.get('seasonNumber'), s_ep.get('episodeNumber'))
                            m[key] = bool(s_ep.get('hasFile', False))
                        sonarr_episode_cache[series_internal_id] = m
                    except Exception as e:
                        logger.debug(f"Failed to fetch Sonarr episodes for series {series_internal_id}: {e}", extra={'emoji_type': 'debug'})
                        sonarr_episode_cache[series_internal_id] = {}
                has_file = sonarr_episode_cache[series_internal_id].get((season_num, episode_num), False)
            if not air_date:
                continue
            if not enable_placeholders:
                continue
            # Use local time for status comparison
            local_now = datetime.now()
            local_today = local_now.date()
            local_tomorrow = local_today + timedelta(days=1)
            if air_date:
                local_air_date = air_date.astimezone().date() if air_date.tzinfo else air_date.date()
            if air_date < now:
                ep_status = "Request"
                dummy_file = settings.DUMMY_FILE_PATH
            else:
                if local_air_date == local_today:
                    ep_status = "Airing today"
                elif local_air_date == local_tomorrow:
                    ep_status = "Airing in 1 day"
                else:
                    days_left = (local_air_date - local_today).days
                    ep_status = f"Airing in {days_left} days"
                dummy_file = getattr(settings, "COMING_SOON_DUMMY_FILE_PATH", "") or settings.DUMMY_FILE_PATH
            logger.debug(f"Aggregating episode: series={series_title}, season={season_num}, episode_num={episode_num}, title={episode_title}, air_date={air_date}", extra={'emoji_type': 'debug'})
            all_episodes.append({
                "tvdb_id": tvdb_id,
                "series_title": series_title,
                "series_year": series_year,
                "season_num": season_num,
                "episode_num": episode_num,
                "title": episode_title,
                "status": ep_status,
                "air_date": air_date,
                "dummy_file": dummy_file,
                "folder_path": folder_path,
                "arr_root_folder": arr_root_folder,
                # include authoritative hasFile flag from Sonarr when available
                "hasFile": bool(has_file)
            })

        # --- Step 2: Batch check which placeholders need creation or update ---
        episodes_needing_placeholder = get_episodes_needing_placeholder(all_episodes)

        # --- Step 2.5: Batch create all needed placeholders before any service-specific logic ---
        if episodes_needing_placeholder:
            batch_create_placeholders(episodes_needing_placeholder)
            # For Plex: scan after each series' missing placeholders are created
            if settings.plex_enabled:
                from collections import defaultdict
                folder_to_eps = defaultdict(list)
                for ep in episodes_needing_placeholder:
                    if ep.get('folder_path'):
                        folder_to_eps[ep['folder_path']].append(ep)
                for folder, eps in folder_to_eps.items():
                    resolved_folder = resolve_final_folder(folder, eps[0].get('arr_root_folder'), 'tv')
                    logger.info(f"Refreshing Plex folder for series after placeholder creation: {resolved_folder}", extra={'emoji_type': 'refresh'})
                    refresh_plex_item(resolved_folder)
                    logger.info("Waiting 3 seconds for Plex to scan new placeholders...", extra={'emoji_type': 'refresh'})
                    time.sleep(3)
            # For Jellyfin: scan after all placeholders are created
            if settings.jellyfin_enabled:
                logger.info("Refreshing Jellyfin TV library folder for batch placeholder update after all placeholders...", extra={'emoji_type': 'refresh'})
                refresh_jellyfin_item(settings.TV_LIBRARY_FOLDER)
                if settings.emby_enabled:
                    try:
                        from services.emby_client import refresh_emby_item
                        refresh_emby_item(settings.TV_LIBRARY_FOLDER)
                    except Exception:
                        logger.debug(f"Emby refresh failed for {settings.TV_LIBRARY_FOLDER}", extra={'emoji_type': 'debug'})
                logger.info("Waiting 30 seconds for Jellyfin to scan new placeholders...", extra={'emoji_type': 'refresh'})
                time.sleep(30)

        # --- Step 2.7: Group by series and season for status logic ---
        from collections import defaultdict
        series_season_groups = defaultdict(list)
        for ep in all_episodes:
            key = (ep['series_title'], ep['season_num'])
            series_season_groups[key].append(ep)
        now_date = now.date()
        series_season_status = {}
        for (series_title, season_num), eps in series_season_groups.items():
            eps_sorted = sorted(eps, key=lambda e: e['air_date'])
            latest_aired = None
            next_up = None
            for e in eps_sorted:
                if e['air_date'].date() <= now_date:
                    latest_aired = e
                elif not next_up:
                    next_up = e
            # Determine status
            status = None
            if latest_aired and (now_date - latest_aired['air_date'].date()).days <= 2:
                days_ago = (now_date - latest_aired['air_date'].date()).days
                if days_ago == 0:
                    status = "Latest aired today"
                elif days_ago == 1:
                    status = "Latest aired yesterday"
                else:
                    status = f"Latest aired {days_ago} days ago"
            elif next_up:
                days_left = (next_up['air_date'].date() - now_date).days
                if days_left == 0:
                    status = "Next episode airs today"
                elif days_left == 1:
                    status = "Next episode airs in 1 day"
                else:
                    status = f"Next episode in {days_left} days"
            else:
                status = "No upcoming episodes"
            series_season_status[(series_title, season_num)] = status
            logger.info(f"[Status] {series_title} S{season_num:02d}: {status}", extra={'emoji_type': 'update'})

        # --- Step 3-4: Batch by series folder for Plex ---
        if settings.plex_enabled:
            from collections import defaultdict
            folder_to_eps = defaultdict(list)
            for ep in episodes_needing_placeholder:
                if ep.get('folder_path'):
                    folder_to_eps[ep['folder_path']].append(ep)
            # Group all episodes by folder for status updates
            all_folder_to_eps = defaultdict(list)
            for ep in all_episodes:
                if ep.get('folder_path'):
                    all_folder_to_eps[ep['folder_path']].append(ep)
            for folder, eps in all_folder_to_eps.items():
                resolved_folder = resolve_final_folder(folder, eps[0].get('arr_root_folder'), 'tv')
                episode_list_str = ', '.join([
                    f"{ep['series_title']} S{ep['season_num']:02d}E{ep['episode_num']:02d}" for ep in eps
                ])
                logger.debug(f"[Plex] Updating episodes in folder: {resolved_folder} | Episodes: [{episode_list_str}]", extra={'emoji_type': 'debug'})
                # Removed Plex scan from status update loop
                updated_eps = []
                for ep in eps:
                    logger.debug(f"[Plex] About to update: {ep['series_title']} S{ep['season_num']:02d}E{ep['episode_num']:02d}", extra={'emoji_type': 'debug'})
                    try:
                        from services.plex_client import update_plex_title_status
                        success = update_plex_title_status(
                            media_type='tv',
                            media_id=ep["tvdb_id"],
                            title=ep["series_title"],
                            status=ep["status"],
                            season=ep["season_num"],
                            episode=ep["episode_num"]
                        )
                        logger.debug(f"[Plex] Update {'succeeded' if success else 'failed'} for id={ep['tvdb_id']} season={ep['season_num']} episode={ep['episode_num']}", extra={'emoji_type': 'debug'})
                        if success:
                            updated_eps.append(f"{ep['series_title']} S{ep['season_num']:02d}E{ep['episode_num']:02d}")
                        if ep["air_date"].date() == now.date():
                            schedule_episode_request_update(ep["series_title"], ep["season_num"], ep["episode_num"], ep["tvdb_id"], delay=3600, retries=3)
                    except Exception as e:
                        logger.error(f"Failed to update Plex title for {ep['series_title']} S{ep['season_num']:02d}E{ep['episode_num']:02d}: {e}", extra={'emoji_type': 'error'})
                if updated_eps:
                    logger.info(f"Batch updated episode titles for Plex: {', '.join(updated_eps)}", extra={'emoji_type': 'update'})
            # After updating episodes, update series/season titles/descriptions
            for (series_title, season_num), status in series_season_status.items():
                try:
                    eps = [ep for ep in all_episodes if ep['series_title'] == series_title and ep['season_num'] == season_num]
                    if eps:
                        resolved_folder = resolve_final_folder(eps[0].get('folder_path'), eps[0].get('arr_root_folder'), 'tv')
                        # Removed Plex scan before season/series summary update
                    from services.plex_client import update_plex_title_status
                    update_plex_title_status(
                        media_type='tv',
                        media_id=eps[0]["tvdb_id"] if eps else None,
                        title=series_title,
                        status=status,
                        season=season_num,
                        episode=None
                    )
                    logger.info(f"[Plex] Updated series/season: {series_title} S{season_num:02d} -> {status}", extra={'emoji_type': 'update'})
                except Exception as e:
                    logger.error(f"[Plex] Failed to update series/season {series_title} S{season_num:02d}: {e}", extra={'emoji_type': 'error'})
            # --- Jellyfin batch update: only if enabled ---
            if settings.jellyfin_enabled:
                for folder, eps in folder_to_eps.items():
                    resolved_folder = resolve_final_folder(folder, eps[0].get('arr_root_folder'), 'tv')
                    logger.info(f"Refreshing Jellyfin folder for batch placeholder update: {resolved_folder}", extra={'emoji_type': 'refresh'})
                    refresh_jellyfin_item(resolved_folder)
                    if settings.emby_enabled:
                        try:
                            from services.emby_client import refresh_emby_item
                            refresh_emby_item(resolved_folder)
                        except Exception:
                            logger.debug(f"Emby refresh failed for {resolved_folder}", extra={'emoji_type': 'debug'})
                    logger.info("Waiting 10 seconds for Jellyfin to scan new placeholders in folder...", extra={'emoji_type': 'refresh'})
                    time.sleep(10)
                    # Batch update episode statuses for Jellyfin
                    updated_eps = []
                    for ep in eps:
                        logger.debug(f"[Jellyfin] About to update: {ep['series_title']} S{ep['season_num']:02d}E{ep['episode_num']:02d}", extra={'emoji_type': 'debug'})
                        try:
                            from services.jellyfin_client import update_jellyfin_title_status
                            success = update_jellyfin_title_status(
                                media_type='tv',
                                media_id=ep["tvdb_id"],
                                title=ep["series_title"],
                                status=ep["status"],
                                season=ep["season_num"],
                                episode=ep["episode_num"]
                            )
                            if success:
                                updated_eps.append(f"{ep['series_title']} S{ep['season_num']:02d}E{ep['episode_num']:02d}")
                        except Exception as e:
                            logger.error(f"[Jellyfin] Failed to update title for {ep['series_title']} S{ep['season_num']:02d}E{ep['episode_num']:02d}: {e}", extra={'emoji_type': 'error'})
                    if updated_eps:
                        logger.info(f"Batch updated episode titles for Jellyfin: {', '.join(updated_eps)}", extra={'emoji_type': 'update'})
    except Exception as e:
        logger.error(f"Sonarr calendar sync failed: {e}", extra={'emoji_type': 'error'})

    # --- Radarr: Movies ---
    try:
        arr_cfg = get_arr_config("movie")
        radarr_url = arr_cfg["url"]
        radarr_api_key = arr_cfg["api_key"]
        calendar_url = join_endpoint(radarr_url, "calendar")
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
            # Use 'path' for API lookups (Radarr), fallback to 'folderPath' for legacy/compat
            folder_path = movie.get('path') or movie.get('folderPath')
            arr_root_folder = get_root_folder_from_path(folder_path)
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

            # --- Use Radarr's hasFile flag as authoritative; avoid filesystem checks ---
            has_file = bool(movie.get('hasFile', False))
            # If Radarr didn't include hasFile in calendar response, try fetching the movie record
            if not has_file and movie.get('id'):
                try:
                    m_resp = requests.get(join_endpoint(radarr_url, f"movie/{movie.get('id')}"), headers=headers)
                    if m_resp.status_code == 200:
                        m_data = m_resp.json()
                        has_file = bool(m_data.get('hasFile', False))
                except Exception as e:
                    logger.debug(f"Failed to fetch Radarr movie record for id {movie.get('id')}: {e}", extra={'emoji_type': 'debug'})
            if has_file:
                logger.debug(f"Radarr reports file exists for movie, skipping dummy: {title} ({year})", extra={'emoji_type': 'debug'})
                continue

            dummy_path = place_dummy_file(
                "movie", title, year, tmdb_id, settings.MOVIE_LIBRARY_FOLDER,
                dummy_file_override=dummy_file,
                folder_path=folder_path,
                arr_root_folder=arr_root_folder,
                overview=movie.get('overview') or movie.get('synopsis') or None,
                request_mark=(status == 'Request')
            )
            movies_to_update.append({
                "title": title,
                "year": year,
                "tmdb_id": tmdb_id,
                "status": status,
                "air_date": air_date,
                "folder_path": folder_path  # <-- Ensure folder_path is included
            })
    except Exception as e:
        logger.error(f"Radarr calendar sync failed: {e}", extra={'emoji_type': 'error'})

    # --- Batch Plex/Jellyfin refresh ---
    try:
        plex_folders = set()
        for movie in movies_to_update:
            folder = movie.get('folder_path')
            if folder:
                plex_folders.add(folder)
        if settings.plex_enabled and plex_folders:
            logger.info(f"Refreshing {len(plex_folders)} Plex folders for batch placeholder update...", extra={'emoji_type': 'refresh'})
            for folder in plex_folders:
                refresh_plex_item(folder)
            logger.info("Waiting 3 seconds for Plex to scan new placeholders...", extra={'emoji_type': 'refresh'})
            time.sleep(3)
        if settings.jellyfin_enabled:
            logger.info("Refreshing Jellyfin TV and Movie library folders for batch placeholder update...", extra={'emoji_type': 'refresh'})
            for folder in plex_folders:
                refresh_jellyfin_item(folder)
                if settings.emby_enabled:
                    try:
                        from services.emby_client import refresh_emby_item
                        refresh_emby_item(folder)
                    except Exception:
                        logger.debug(f"Emby refresh failed for {folder}", extra={'emoji_type': 'debug'})
            logger.info("Waiting 30 seconds for Jellyfin to scan new placeholders...", extra={'emoji_type': 'refresh'})
            time.sleep(30)
    except Exception as e:
        logger.error(f"Error during batch Plex/Jellyfin refresh: {e}", extra={'emoji_type': 'error'})

    # --- Batch update movie titles ---
    updated_movies = []
    for movie in movies_to_update:
        try:
            update_title_status(
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
            logger.error(f"Failed to update Plex/Jellyfin title for movie {movie['title']}: {e}", extra={'emoji_type': 'error'})
    if updated_movies:
        logger.info(f"Batch updated movie titles: {', '.join(updated_movies)}", extra={'emoji_type': 'update'})

    logger.info("✅ Calendar sync completed: all episodes and movies processed, placeholders and status updates applied.", extra={'emoji_type': 'process'})

def get_episodes_needing_placeholder(episodes):
    """Return a list of episodes that need a placeholder created or updated."""
    now = datetime.now(timezone.utc)
    needing = []
    tv_library_folder = settings.TV_LIBRARY_FOLDER
    for ep in episodes:
        clean_title = sanitize_filename(ep["series_title"])
        clean_title = re.sub(r'\s*\(\d{4}\)', '', clean_title).strip()
        year_str = f" ({ep['series_year']})" if ep["series_year"] else ""
        file_name = f"{clean_title}{year_str} - s{ep['season_num']:02d}e{ep['episode_num']:02d} - {ep['title']}.mp4"
        folder_path = ep["folder_path"]
        arr_root_folder = ep.get("arr_root_folder") or ""
        season_folder = ep.get('season_folder') or f"Season {ep['season_num']:02d}"
        # Use ARR-provided hasFile flag as authoritative. If ARR reports the file exists, skip creating a placeholder.
        coming_soon_dummy = getattr(settings, "COMING_SOON_DUMMY_FILE_PATH", "") or settings.DUMMY_FILE_PATH
        regular_dummy = settings.DUMMY_FILE_PATH
        air_date = ep["air_date"]
        if ep.get('hasFile', False):
            logger.debug(f"ARR reports file exists for episode, skipping placeholder: {ep['series_title']} S{ep['season_num']:02d}E{ep['episode_num']:02d}", extra={'emoji_type': 'debug'})
            continue

        # ARR did not report a file; create/update placeholder based on air date
        ep = ep.copy()
        ep["_target_dummy"] = coming_soon_dummy if air_date > now else regular_dummy
        logger.debug(f"Placeholder missing or outdated according to ARR, will create/update for: {ep['series_title']} S{ep['season_num']:02d}E{ep['episode_num']:02d}", extra={'emoji_type': 'debug'})
        needing.append(ep)
    logger.info(f"Episodes needing placeholder: {len(needing)} / {len(episodes)}", extra={'emoji_type': 'debug'})
    return needing

def batch_create_placeholders(episodes):
    """Batch create placeholders for a list of episode dicts. Only create or update if needed."""
    created = 0
    updated = 0
    for ep in episodes:
        logger.debug(f"Creating/updating placeholder: {ep}", extra={'emoji_type': 'debug'})
        dummy_file = ep.get("_target_dummy")
        dummy_path = place_dummy_file(
            "tv", ep["series_title"], ep["series_year"], ep["tvdb_id"],
            settings.TV_LIBRARY_FOLDER,
            season_number=ep["season_num"],
            episode_range=(ep["episode_num"], ep["episode_num"]),
            episode_title=ep["title"],
            dummy_file_override=dummy_file,
            folder_path=ep["folder_path"],
            arr_root_folder=ep["arr_root_folder"],
            overview=ep.get('overview') or ep.get('summary') or None,
            request_mark=(ep.get('status') == 'Request')
        )
        if dummy_path:
            logger.info(f"Created/updated placeholder file: {dummy_path}", extra={'emoji_type': 'create'})
            created += 1
        else:
            logger.error(f"Failed to create/update placeholder for {ep['series_title']} S{ep['season_num']:02d}E{ep['episode_num']}", extra={'emoji_type': 'error'})
    logger.info(f"Batch created/updated {created} placeholders for TV episodes", extra={'emoji_type': 'create'})

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
