import requests
import time
import logging

# =========================
# Configuration
# =========================

TAUTULLI_API_URL = "http://your-tautulli-url/api/v2"  # Replace with your Tautulli URL
TAUTULLI_API_KEY = "your-tautulli-api-key"  # Replace with your Tautulli API key

SONARR_API_URL = "http://your-sonarr-url/api/v3"  # Replace with your Sonarr URL
SONARR_API_KEY = "your-sonarr-api-key"  # Replace with your Sonarr API key

# Toggle one of these
MONITOR_ENTIRE_SERIES = False   # Highest priority
MONITOR_NEXT_SEASON = False   # Next priority
MONITOR_ENTIRE_SEASON = False   # Next priority

EPISODES_TO_MONITOR = 5         # Fallback if none of the above are True

WATCHED_PERCENTAGE = 70         # Only trigger if watched progress >= this
SLEEP_INTERVAL = 60             # How many seconds to wait between checks

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def log_info(message):
    logging.info(message)

def log_error(message):
    logging.error(message)


def get_current_activity():
    """
    Fetches current Plex/Tautulli activity via Tautulli API.
    """
    try:
        params = {'apikey': TAUTULLI_API_KEY, 'cmd': 'get_activity'}
        response = requests.get(TAUTULLI_API_URL, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        log_error(f"Error fetching activity from Tautulli: {e}")
        return None


def lookup_series_by_tvdb_id(tvdb_guid):
    """
    Query Sonarr's /series/lookup endpoint using 'tvdb:xxx' term, 
    return the matched series object (or None if not found).
    """
    try:
        series_response = requests.get(
            f"{SONARR_API_URL}/series/lookup",
            headers={"X-Api-Key": SONARR_API_KEY},
            params={"term": f"tvdb:{tvdb_guid}"},
            timeout=10
        )
        series_response.raise_for_status()
        series_data = series_response.json()
        # Return the first match where 'tvdbId' matches
        matching_series = next((s for s in series_data
                                if str(s.get('tvdbId')) == tvdb_guid), None)
        return matching_series
    except requests.exceptions.RequestException as e:
        log_error(f"Error looking up series by TVDB ID {tvdb_guid}: {e}")
        return None


def fetch_series_episodes(series_id):
    """
    Fetch all episodes for a given Sonarr series_id, excluding specials (season 0).
    Returns a list of episodes sorted by (seasonNumber, episodeNumber).
    """
    try:
        response = requests.get(
            f"{SONARR_API_URL}/episode",
            headers={"X-Api-Key": SONARR_API_KEY},
            params={"seriesId": series_id},
            timeout=10
        )
        response.raise_for_status()
        episodes = response.json()
        # Exclude specials if you want
        filtered_episodes = [ep for ep in episodes if ep['seasonNumber'] != 0]
        # Sort by season, then episode
        sorted_episodes = sorted(filtered_episodes,
                                 key=lambda e: (e['seasonNumber'], e['episodeNumber']))
        return sorted_episodes
    except requests.exceptions.RequestException as e:
        log_error(f"Error fetching episodes for Series ID {series_id}: {e}")
        return []


def mark_episodes_as_monitored(episodes_list):
    """
    Mark the given episodes as monitored in Sonarr, then trigger an episode search.
    """
    if not episodes_list:
        return

    episodes_str = ', '.join(f"{ep['seasonNumber']}x{ep['episodeNumber']}"
                             for ep in episodes_list)

    # 1. Mark them as monitored
    for ep in episodes_list:
        payload = {"monitored": True}
        try:
            update_response = requests.put(
                f"{SONARR_API_URL}/episode/{ep['id']}",
                headers={"X-Api-Key": SONARR_API_KEY},
                json=payload,
                timeout=10
            )
            update_response.raise_for_status()
        except requests.exceptions.RequestException as ex:
            log_error(
                f"Failed to enable monitoring for {ep['seasonNumber']}x{ep['episodeNumber']}: {ex}"
            )

    log_info(f"Episodes marked for monitoring: {episodes_str}")

    # 2. Trigger a search
    search_payload = {
        "name": "EpisodeSearch",
        "episodeIds": [ep['id'] for ep in episodes_list]
    }
    try:
        search_response = requests.post(
            f"{SONARR_API_URL}/command",
            headers={"X-Api-Key": SONARR_API_KEY},
            json=search_payload,
            timeout=10
        )
        search_response.raise_for_status()
        log_info("Search triggered for monitored episodes.")
    except requests.exceptions.RequestException as ex:
        log_error(f"Error triggering search for episodes: {ex}")


def monitor_episodes(series_id, sorted_episodes, current_season, current_episode):
    """
    Decide which episodes to monitor based on the toggles, in the desired priority:
      1) MONITOR_ENTIRE_SERIES
      2) MONITOR_NEXT_SEASON
      3) MONITOR_ENTIRE_SEASON
      4) EPISODES_TO_MONITOR
    """
    # -------------
    # 1. ENTIRE SERIES
    # -------------
    if MONITOR_ENTIRE_SERIES:
        log_info("MONITOR_ENTIRE_SERIES => Monitoring all episodes (except specials).")
        # Find episodes that are neither downloaded nor monitored
        episodes_to_update = [
            ep for ep in sorted_episodes
            if (not ep.get('hasFile', False)) and (not ep.get('monitored', False))
        ]
        mark_episodes_as_monitored(episodes_to_update)
        return

    # -------------
    # 2. MONITOR_NEXT_SEASON
    # -------------
    if MONITOR_NEXT_SEASON:
        log_info("MONITOR_NEXT_SEASON => Monitoring current season + next season.")
        # Identify next season number
        next_season = current_season + 1
        episodes_to_update = []

        # Current season
        for ep in sorted_episodes:
            if ep['seasonNumber'] == current_season:
                if not ep['hasFile'] and not ep.get('monitored', False):
                    episodes_to_update.append(ep)

        # Next season
        for ep in sorted_episodes:
            if ep['seasonNumber'] == next_season:
                if not ep['hasFile'] and not ep.get('monitored', False):
                    episodes_to_update.append(ep)

        mark_episodes_as_monitored(episodes_to_update)
        return

    # -------------
    # 3. MONITOR_ENTIRE_SEASON
    # -------------
    if MONITOR_ENTIRE_SEASON:
        log_info("MONITOR_ENTIRE_SEASON => Monitoring the current season.")
        # Gather all episodes in the current season that need monitoring
        episodes_to_update = []
        for ep in sorted_episodes:
            if ep['seasonNumber'] == current_season:
                if not ep['hasFile'] and not ep.get('monitored', False):
                    episodes_to_update.append(ep)

        mark_episodes_as_monitored(episodes_to_update)

        # Check if the just-watched episode is the last in the current season
        max_ep_num = max(
            [ep['episodeNumber'] for ep in sorted_episodes
             if ep['seasonNumber'] == current_season],
            default=0
        )
        if current_episode == max_ep_num:
            # Monitor the next season as well
            log_info("Just watched the last episode of current season; monitoring next season.")
            next_season = current_season + 1
            next_season_to_update = [
                ep for ep in sorted_episodes
                if (ep['seasonNumber'] == next_season
                    and not ep['hasFile']
                    and not ep.get('monitored', False))
            ]
            mark_episodes_as_monitored(next_season_to_update)

        return

    # -------------
    # 4. EPISODES_TO_MONITOR
    # -------------
    log_info(f"EPISODES_TO_MONITOR => Monitoring up to {EPISODES_TO_MONITOR} future episodes.")
    episodes_to_update = []
    total_after_current = 0

    # We walk through all episodes after the current one in chronological order
    for ep in sorted_episodes:
        season = ep['seasonNumber']
        episode_no = ep['episodeNumber']
        if (season > current_season) or (season == current_season and episode_no > current_episode):
            # If we've already accounted for N episodes, stop
            if total_after_current >= EPISODES_TO_MONITOR:
                break
            total_after_current += 1

            # Mark only if not monitored, no file
            if not ep.get('monitored', False) and not ep['hasFile']:
                episodes_to_update.append(ep)

    mark_episodes_as_monitored(episodes_to_update)


def main():
    log_info("Script started.")
    while True:
        try:
            log_info("Checking current activity...")
            activity = get_current_activity()

            # If no activity or no sessions, wait
            if (not activity) or ('response' not in activity) \
               or ('data' not in activity['response']) \
               or (not activity['response']['data']['sessions']):
                log_info("No active sessions found. Sleeping...")
                time.sleep(SLEEP_INTERVAL)
                continue

            sessions = activity['response']['data']['sessions']
            log_info(f"Active sessions found: {len(sessions)}")

            for session in sessions:
                # Only proceed if it's an episode
                if session.get('media_type') != 'episode':
                    continue

                # Check watch progress
                progress = int(session.get('progress_percent', 0))
                if progress < WATCHED_PERCENTAGE:
                    continue

                # Identify show/season/episode
                title = session.get('grandparent_title', "Unknown Title")
                season_number = int(session.get('parent_media_index', 0))
                current_episode = int(session.get('media_index', 0))

                # Attempt to parse TVDB GUID
                grandparent_guids = session.get('grandparent_guids', [])
                tvdb_guid = next(
                    (guid.replace('tvdb://', '')
                     for guid in grandparent_guids if guid.startswith('tvdb://')),
                    None
                )

                log_info(f"Processing: {title}, S{season_number}E{current_episode} "
                         f"({progress}% watched)")

                if not tvdb_guid:
                    log_error(f"No TVDB GUID found for '{title}'. Skipping...")
                    continue

                # Look up the series in Sonarr
                series = lookup_series_by_tvdb_id(tvdb_guid)
                if not series or 'id' not in series:
                    log_error(f"Series not found or missing ID for '{title}' (TVDB={tvdb_guid}).")
                    continue

                series_id = series['id']
                # Fetch episodes from Sonarr
                sorted_episodes = fetch_series_episodes(series_id)
                if not sorted_episodes:
                    continue

                # Monitor the needed episodes
                monitor_episodes(series_id, sorted_episodes, season_number, current_episode)

            # Done processing sessions, sleep
            log_info(f"Sleeping for {SLEEP_INTERVAL} seconds...")
            time.sleep(SLEEP_INTERVAL)

        except Exception as e:
            log_error(f"Unhandled exception: {e}")
            time.sleep(SLEEP_INTERVAL)


if __name__ == "__main__":
    main()