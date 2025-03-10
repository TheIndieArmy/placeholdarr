import requests
import time
import threading
from typing import List, Dict, Any, Optional

from core.logger import logger
from core.config import settings

def get_episodes_for_mode(episodes: List[Dict], play_mode: str, current_episode: Dict) -> List[Dict]:
    """
    Filter episodes based on play mode and file status
    
    Args:
        episodes: List of all episodes from Sonarr
        play_mode: The playback mode (episode, season, series)
        current_episode: The episode that is currently playing
        
    Returns:
        List of episodes to monitor and search for
    """
    current_season = current_episode['seasonNumber']
    current_episode_num = current_episode['episodeNumber']
    
    if play_mode == "episode":
        # Current episode plus lookahead
        return [ep for ep in episodes 
                if (ep['seasonNumber'] == current_season
                and current_episode_num <= ep['episodeNumber'] < current_episode_num + settings.EPISODES_LOOKAHEAD)]
    
    elif play_mode == "season":
        # Get all episodes in current season
        season_episodes = [ep for ep in episodes if ep['seasonNumber'] == current_season]
        
        # Check if this is the last episode of the season
        if season_episodes:
            last_episode_num = max(ep['episodeNumber'] for ep in season_episodes)
            
            # If current episode is the last in season, include next season too
            if current_episode_num == last_episode_num:
                next_season = current_season + 1
                next_season_exists = any(ep['seasonNumber'] == next_season for ep in episodes)
                
                if next_season_exists:
                    logger.info(f"Last episode of season {current_season} played, adding season {next_season}", 
                              extra={'emoji_type': 'info'})
                    return [ep for ep in episodes if ep['seasonNumber'] in (current_season, next_season)]
            
        # Regular case: just return current season
        return season_episodes
    
    else:  # series mode
        return episodes

def get_current_episode(episodes: List[Dict], season_number: int, episode_number: int) -> Optional[Dict]:
    """Find specific episode from episode list"""
    for ep in episodes:
        if ep['seasonNumber'] == season_number and ep['episodeNumber'] == episode_number:
            return ep
    return None

def get_all_episodes(series_id: int, is_4k: bool = False) -> List[Dict]:
    """
    Get all episodes for a series
    
    Args:
        series_id: Sonarr series ID
        is_4k: Whether to use 4K instance
        
    Returns:
        List of episodes
    """
    try:
        # Get the appropriate Sonarr URL and API key based on quality
        sonarr_url = settings.SONARR_4K_URL if is_4k else settings.SONARR_URL
        sonarr_api_key = settings.SONARR_4K_API_KEY if is_4k else settings.SONARR_API_KEY
        
        episodes_response = requests.get(
            f"{sonarr_url}/api/v3/episode",
            params={'seriesId': series_id},
            headers={'X-Api-Key': sonarr_api_key}
        )
        
        if episodes_response.status_code != 200:
            logger.error(f"Failed to get episodes: {episodes_response.status_code}", 
                        extra={'emoji_type': 'error'})
            return []
            
        return episodes_response.json()
        
    except Exception as e:
        logger.error(f"Error getting episodes: {str(e)}", extra={'emoji_type': 'error'})
        return []

def monitor_episodes(series_id: int, episode_ids: List[int], is_4k: bool = False) -> bool:
    """
    Set multiple episodes to monitored in Sonarr
    
    Args:
        series_id: Sonarr series ID
        episode_ids: List of episode IDs to set monitored
        is_4k: Whether to use 4K instance
        
    Returns:
        bool: Success or failure
    """
    try:
        # Get the appropriate Sonarr URL and API key based on quality
        sonarr_url = settings.SONARR_4K_URL if is_4k else settings.SONARR_URL
        sonarr_api_key = settings.SONARR_4K_API_KEY if is_4k else settings.SONARR_API_KEY
        
        if not episode_ids:
            logger.warning("No episodes to monitor", extra={'emoji_type': 'warning'})
            return False
            
        logger.info(f"Setting {len(episode_ids)} episodes to monitored", extra={'emoji_type': 'info'})
        
        # Use bulk monitoring endpoint
        update_response = requests.put(
            f"{sonarr_url}/api/v3/episode/monitor",
            json={'episodeIds': episode_ids, 'monitored': True},
            headers={'X-Api-Key': sonarr_api_key}
        )
        
        if update_response.status_code not in (200, 202):
            logger.error(f"Failed to set episodes as monitored: {update_response.status_code}", 
                        extra={'emoji_type': 'error'})
            return False
            
        return True
        
    except Exception as e:
        logger.error(f"Error monitoring episodes: {str(e)}", extra={'emoji_type': 'error'})
        return False

def search_episodes(episode_ids: List[int], is_4k: bool = False) -> bool:
    """
    Trigger search for multiple episodes
    
    Args:
        episode_ids: List of episode IDs to search for
        is_4k: Whether to use 4K instance
        
    Returns:
        bool: Success or failure
    """
    try:
        # Get the appropriate Sonarr URL and API key based on quality
        sonarr_url = settings.SONARR_4K_URL if is_4k else settings.SONARR_URL
        sonarr_api_key = settings.SONARR_4K_API_KEY if is_4k else settings.SONARR_API_KEY
        
        if not episode_ids:
            logger.warning("No episodes to search for", extra={'emoji_type': 'warning'})
            return False
            
        logger.info(f"Triggering search for {len(episode_ids)} episodes", extra={'emoji_type': 'search'})
        
        # Use bulk search command
        search_response = requests.post(
            f"{sonarr_url}/api/v3/command",
            headers={'X-Api-Key': sonarr_api_key},
            json={"name": "EpisodeSearch", "episodeIds": episode_ids}
        )
        
        if search_response.status_code not in (200, 201, 202):
            logger.error(f"Failed to trigger search: {search_response.status_code}", 
                        extra={'emoji_type': 'error'})
            return False
            
        return True
        
    except Exception as e:
        logger.error(f"Error searching for episodes: {str(e)}", extra={'emoji_type': 'error'})
        return False

def search_in_sonarr(tvdb_id: int, rating_key: str, season_number: int, episode_number: int, is_4k: bool = False) -> Optional[int]:
    """
    Search for a series in Sonarr and monitor episodes based on play mode
    
    Args:
        tvdb_id: TVDB ID of the series
        rating_key: Plex rating key of the played episode
        season_number: Season number of the played episode
        episode_number: Episode number of the played episode
        is_4k: Whether to use 4K Sonarr instance
        
    Returns:
        int: Sonarr series ID or None if failed
    """
    try:
        # Get the appropriate Sonarr URL and API key based on quality
        sonarr_url = settings.SONARR_4K_URL if is_4k else settings.SONARR_URL
        sonarr_api_key = settings.SONARR_4K_API_KEY if is_4k else settings.SONARR_API_KEY
        
        # Get series information
        series_response = requests.get(
            f"{sonarr_url}/api/v3/series",
            params={'tvdbId': tvdb_id},
            headers={'X-Api-Key': sonarr_api_key}
        )
        series_response.raise_for_status()
        
        if not series_response.json():
            logger.error(f"Series not found with TVDB ID: {tvdb_id}", extra={'emoji_type': 'error'})
            return None
            
        series = series_response.json()[0]
        
        # Get all episodes
        all_episodes = get_all_episodes(series['id'], is_4k)
        if not all_episodes:
            logger.error("Failed to get episodes", extra={'emoji_type': 'error'})
            return None
        
        # Find current episode
        current_episode = get_current_episode(all_episodes, season_number, episode_number)
        if not current_episode:
            logger.error(f"Could not find episode S{season_number:02d}E{episode_number:02d}", 
                         extra={'emoji_type': 'error'})
            return None
        
        # Always include the currently played episode in our search, regardless of status
        episode_ids = [current_episode['id']]
            
        # Get episodes to monitor based on play mode (includes next season logic for season mode)
        episodes_to_monitor = get_episodes_for_mode(
            all_episodes, settings.TV_PLAY_MODE, current_episode
        )
        
        # Filter for episodes that need downloading
        episodes_without_files = [ep for ep in episodes_to_monitor if not ep.get('hasFile', False)]
        
        if episodes_without_files:
            # Add IDs from episodes without files (if not already in the list)
            for ep in episodes_without_files:
                if ep['id'] not in episode_ids:
                    episode_ids.append(ep['id'])
            
            # Monitor episodes
            monitor_success = monitor_episodes(series['id'], episode_ids, is_4k)
            if not monitor_success:
                logger.warning("Failed to monitor episodes", extra={'emoji_type': 'warning'})
            
            # Search for episodes
            search_success = search_episodes(episode_ids, is_4k)
            if not search_success:
                logger.warning("Failed to trigger episode search", extra={'emoji_type': 'warning'})
                
            # Log what we're monitoring in a friendly way
            if len(episodes_without_files) > 1:
                seasons = sorted(set(ep['seasonNumber'] for ep in episodes_without_files))
                if len(seasons) == 1:
                    # All in same season
                    s_num = seasons[0]
                    start_ep = min(ep['episodeNumber'] for ep in episodes_without_files if ep['seasonNumber'] == s_num)
                    end_ep = max(ep['episodeNumber'] for ep in episodes_without_files if ep['seasonNumber'] == s_num)
                    logger.info(f"Monitoring S{s_num}E{start_ep}-E{end_ep}", 
                            extra={'emoji_type': 'monitor'})
                else:
                    # Multiple seasons
                    season_str = ", ".join(f"S{s}" for s in seasons)
                    logger.info(f"Monitoring episodes across multiple seasons: {season_str}", 
                            extra={'emoji_type': 'monitor'})
            else:
                # Just one episode
                ep = episodes_without_files[0]
                logger.info(f"Monitoring S{ep['seasonNumber']}E{ep['episodeNumber']}", 
                        extra={'emoji_type': 'monitor'})
        else:
            # If no episodes need files but we're still searching for the current one
            logger.info(f"Searching for current episode S{season_number}E{episode_number}", 
                       extra={'emoji_type': 'search'})
            
            # Monitor and search just the current episode
            monitor_episodes(series['id'], episode_ids, is_4k)
            search_episodes(episode_ids, is_4k)
            
        return series['id']
            
    except Exception as e:
        logger.error(f"Failed to search in Sonarr: {e}", extra={'emoji_type': 'error'})
        return None

def search_in_radarr(tmdb_id: int, rating_key: str, is_4k: bool = False) -> bool:
    """
    Search for a movie in Radarr
    
    Args:
        tmdb_id: TMDB ID of the movie
        rating_key: Plex rating key of the movie
        is_4k: Whether to use 4K Radarr instance
        
    Returns:
        bool: Success or failure
    """
    try:
        # Get the appropriate Radarr URL and API key based on quality
        radarr_url = settings.RADARR_4K_URL if is_4k else settings.RADARR_URL
        radarr_api_key = settings.RADARR_4K_API_KEY if is_4k else settings.RADARR_API_KEY
        
        # Make sure tmdb_id is an integer
        tmdb_id = int(tmdb_id)
        
        # Check if movie exists
        movies_response = requests.get(
            f"{radarr_url}/api/v3/movie", 
            headers={'X-Api-Key': radarr_api_key}
        )
        existing = [m for m in movies_response.json() if int(m.get("tmdbId", 0)) == tmdb_id]
        
        if existing:
            movie_data = existing[0]
            
            # Trigger movie search
            search_response = requests.post(
                f"{radarr_url}/api/v3/command", 
                json={'name': 'MoviesSearch', 'movieIds': [movie_data['id']]}, 
                headers={'X-Api-Key': radarr_api_key}
            )
            
            if search_response.status_code not in (200, 201, 202):
                logger.error(f"Failed to trigger movie search: {search_response.status_code}", 
                           extra={'emoji_type': 'error'})
                return False
                
            logger.info(f"Triggered search for {movie_data['title']}", 
                       extra={'emoji_type': 'search'})
                
            return True
            
        logger.error(f"Movie not found with TMDB ID: {tmdb_id}", 
                    extra={'emoji_type': 'error'})
        return False
        
    except Exception as e:
        logger.error(f"Failed to search in Radarr: {e}", extra={'emoji_type': 'error'})
        return False