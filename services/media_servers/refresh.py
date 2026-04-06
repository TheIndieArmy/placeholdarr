from __future__ import annotations

from services.media_servers.jellyfin import refresh_jellyfin_paths, refresh_jellyfin_sections
from services.media_servers.emby import refresh_emby_paths, refresh_emby_sections


def refresh_all_paths(folders: set[str], *, update_type: str = "Created") -> dict[str, int]:
    """Fan out path-scoped refresh to enabled media servers (excluding Plex)."""
    total_refreshed = 0
    total_failed = 0

    for fn in (refresh_jellyfin_paths, refresh_emby_paths):
        result = fn(folders, update_type=update_type)
        total_refreshed += result.get("refreshed", 0)
        total_failed += result.get("failed", 0)

    return {"refreshed": total_refreshed, "failed": total_failed}


def refresh_all_sections(has_movies: bool, has_episodes: bool) -> dict[str, int]:
    """Fan out library-level refresh to enabled media servers (excluding Plex)."""
    total_refreshed = 0
    total_failed = 0

    for fn in (refresh_jellyfin_sections, refresh_emby_sections):
        result = fn(has_movies, has_episodes)
        total_refreshed += result.get("refreshed", 0)
        total_failed += result.get("failed", 0)

    return {"refreshed": total_refreshed, "failed": total_failed}
