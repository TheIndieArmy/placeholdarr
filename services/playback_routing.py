"""
Playback routing logic using dynamic instance rankings.

Replaces legacy standard/4k binary preference with ranked instance selection.
"""

from core.config import settings
from typing import Optional


def get_candidate_instances_for_movie() -> list[str]:
    """Return movie playback candidate instances in the order they should be searched."""
    ranking = settings.movie_instance_ranking
    if not ranking:
        return []
    return ranking


def get_candidate_instances_for_tv() -> list[str]:
    """Return TV playback candidate instances in the order they should be searched."""
    ranking = settings.tv_instance_ranking
    if not ranking:
        return []
    return ranking


def resolve_playback_instance_for_movie(
    tmdb_id: Optional[int] = None,
    is_4k: Optional[bool] = None,
) -> str:
    """
    Resolve which Radarr instance to use for movie playback.
    
    Uses ranked instance preference from settings. Falls back to legacy 4k flag if no ranking configured.
    
    Args:
        tmdb_id: Optional TMDB ID (for potential future profile matching)
        is_4k: Optional flag indicating if 4K content (legacy, kept for backward compat)
    
    Returns:
        Instance key to use for playback
        
    Raises:
        ValueError: If no Radarr instances are configured
    """
    ranking = settings.movie_instance_ranking
    
    if not ranking:
        raise ValueError("No Radarr instances configured for playback routing")
    
    # Return first ranked instance (primary choice)
    return ranking[0]


def resolve_playback_instance_for_tv(
    tvdb_id: Optional[int] = None,
    is_4k: Optional[bool] = None,
) -> str:
    """
    Resolve which Sonarr instance to use for TV playback.
    
    Uses ranked instance preference from settings. Falls back to legacy 4k flag if no ranking configured.
    
    Args:
        tvdb_id: Optional TVDB ID (for potential future profile matching)
        is_4k: Optional flag indicating if 4K content (legacy, kept for backward compat)
    
    Returns:
        Instance key to use for playback
        
    Raises:
        ValueError: If no Sonarr instances are configured
    """
    ranking = settings.tv_instance_ranking
    
    if not ranking:
        raise ValueError("No Sonarr instances configured for playback routing")
    
    # Return first ranked instance (primary choice)
    return ranking[0]


def get_fallback_instances_for_movie(primary_instance: Optional[str] = None) -> list[str]:
    """
    Get list of fallback Radarr instances to try after primary.
    
    Args:
        primary_instance: Optional primary instance to exclude from fallbacks
    
    Returns:
        List of instance keys in fallback order
    """
    ranking = settings.movie_instance_ranking
    if not ranking:
        return []
    if primary_instance:
        return [k for k in ranking if k != primary_instance]
    return ranking[1:] if len(ranking) > 1 else []


def get_fallback_instances_for_tv(primary_instance: Optional[str] = None) -> list[str]:
    """
    Get list of fallback Sonarr instances to try after primary.
    
    Args:
        primary_instance: Optional primary instance to exclude from fallbacks
    
    Returns:
        List of instance keys in fallback order
    """
    ranking = settings.tv_instance_ranking
    if not ranking:
        return []
    if primary_instance:
        return [k for k in ranking if k != primary_instance]
    return ranking[1:] if len(ranking) > 1 else []


def validate_instance_key_exists(instance_key: str, content_type: str) -> bool:
    """
    Validate that an instance key is currently configured.
    
    Args:
        instance_key: Instance key to validate
        content_type: Either 'movie' or 'series'
    
    Returns:
        True if instance is configured, False otherwise
    """
    ranking = (
        settings.movie_instance_ranking
        if content_type == "movie"
        else settings.tv_instance_ranking
    )
    return instance_key in ranking
