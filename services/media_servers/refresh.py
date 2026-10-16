from __future__ import annotations

import time
from typing import Any

from services.media_servers.jellyfin import refresh_jellyfin_paths, refresh_jellyfin_sections
from services.media_servers.emby import refresh_emby_paths, refresh_emby_sections
from services.media_servers.plex import (
    empty_plex_section_trash,
    fetch_plex_section_scan_meta,
    refresh_plex_library_paths,
    refresh_plex_paths,
    refresh_plex_sections,
)
from core.config import settings
from core.logger import logger


def refresh_real_library_paths(
    paths: set[str] | list[str],
    *,
    update_type: str = "Created",
    empty_trash: bool = False,
    include_plex: bool = True,
    include_jellyfin: bool = True,
    include_emby: bool = True,
    bypass_suppression: bool = False,
) -> dict[str, Any]:
    """Path-refresh real Arr media libraries on enabled players.

    Unlike ``refresh_all_paths``, Plex matching uses library Location folders
    (Autopulse-style) rather than Placeholdarr placeholder dest roots. On
    deletes with ``empty_trash``, waits for Plex section scan idle then empties trash.
    """
    path_set = {str(p).strip() for p in (paths or []) if str(p).strip()}
    if not path_set:
        return {"refreshed": 0, "failed": 0, "skipped": 0, "trash": {}}

    if not bypass_suppression and getattr(settings, "REFRESH_TRIGGER_SUPPRESSED", False):
        logger.debug("Real-library path refresh suppressed by global sync flag.")
        return {"refreshed": 0, "failed": 0, "skipped": 0, "trash": {}}

    total_refreshed = 0
    total_failed = 0
    total_skipped = 0
    trash_stats: dict[str, Any] = {}
    before_scanned: dict[int, int | None] = {}

    want_plex = include_plex and bool(getattr(settings, "plex_enabled", False))
    if want_plex and empty_trash:
        try:
            before_scanned = {
                sid: (meta.get("scanned_at") if isinstance(meta, dict) else None)
                for sid, meta in fetch_plex_section_scan_meta().items()
            }
        except Exception as exc:
            logger.debug(
                f"Real-library refresh: could not snapshot Plex scannedAt before refresh: {exc}",
                extra={"emoji_type": "debug"},
            )

    if want_plex:
        plex_stats = refresh_plex_library_paths(path_set, update_type=update_type)
        total_refreshed += int(plex_stats.get("refreshed", 0) or 0)
        total_failed += int(plex_stats.get("failed", 0) or 0)
        total_skipped += int(plex_stats.get("skipped", 0) or 0)
        section_ids = list(plex_stats.get("section_ids") or [])
        if empty_trash and section_ids:
            trash_stats = empty_plex_section_trash(
                section_ids,
                before_scanned_at=before_scanned,
            )

    if include_jellyfin and bool(getattr(settings, "jellyfin_enabled", False)):
        jf_stats = refresh_jellyfin_paths(path_set, update_type=update_type)
        total_refreshed += int(jf_stats.get("refreshed", 0) or 0)
        total_failed += int(jf_stats.get("failed", 0) or 0)

    if include_emby and bool(getattr(settings, "emby_enabled", False)):
        emby_stats = refresh_emby_paths(path_set, update_type=update_type)
        total_refreshed += int(emby_stats.get("refreshed", 0) or 0)
        total_failed += int(emby_stats.get("failed", 0) or 0)

    return {
        "refreshed": total_refreshed,
        "failed": total_failed,
        "skipped": total_skipped,
        "trash": trash_stats,
    }


def refresh_all_paths(
    folders: set[str],
    *,
    update_type: str = "Created",
    include_plex: bool = True,
    include_jellyfin: bool = True,
    include_emby: bool = True,
    bypass_suppression: bool = False,
) -> dict[str, int]:
    """Fan out path-scoped refresh to all enabled media servers."""
    if not bypass_suppression and getattr(settings, "REFRESH_TRIGGER_SUPPRESSED", False):
        logger.debug("Media server path refresh suppressed by global sync flag.")
        return {"refreshed": 0, "failed": 0}

    total_refreshed = 0
    total_failed = 0

    refreshers = []
    if include_plex:
        refreshers.append(refresh_plex_paths)
    if include_jellyfin:
        refreshers.append(refresh_jellyfin_paths)
    if include_emby:
        refreshers.append(refresh_emby_paths)

    for fn in refreshers:
        result = fn(folders, update_type=update_type)
        total_refreshed += result.get("refreshed", 0)
        total_failed += result.get("failed", 0)

    return {"refreshed": total_refreshed, "failed": total_failed}


def refresh_all_sections(
    has_movies: bool,
    has_episodes: bool,
    *,
    plex_force_metadata_refresh: bool = False,
) -> dict[str, int]:
    """Fan out library-level refresh to all enabled media servers."""
    return refresh_selected_sections(
        has_movies,
        has_episodes,
        include_plex=True,
        include_jellyfin=True,
        include_emby=True,
        plex_force_metadata_refresh=plex_force_metadata_refresh,
    )


def refresh_selected_sections(
    has_movies: bool,
    has_episodes: bool,
    *,
    include_plex: bool = True,
    include_jellyfin: bool = True,
    include_emby: bool = True,
    bypass_suppression: bool = False,
    plex_force_metadata_refresh: bool = False,
) -> dict[str, int]:
    """Fan out library-level refresh to selected media servers."""
    if not bypass_suppression and getattr(settings, "REFRESH_TRIGGER_SUPPRESSED", False):
        logger.debug("Media server section refresh suppressed by global sync flag.")
        return {"refreshed": 0, "failed": 0}

    total_refreshed = 0
    total_failed = 0

    refreshers = []
    if include_plex:
        refreshers.append(lambda hm, he: refresh_plex_sections(hm, he, force_refresh_metadata=plex_force_metadata_refresh))
    if include_jellyfin:
        refreshers.append(refresh_jellyfin_sections)
    if include_emby:
        refreshers.append(refresh_emby_sections)

    for fn in refreshers:
        result = fn(has_movies, has_episodes)
        total_refreshed += result.get("refreshed", 0)
        total_failed += result.get("failed", 0)

    return {"refreshed": total_refreshed, "failed": total_failed}


def refresh_all_path_batches_with_section_fallback(
    path_batches: list[tuple[set[str], str]],
    *,
    has_movies: bool,
    has_episodes: bool,
    enable_section_fallback: bool = False,
    fallback_wait_seconds: int = 0,
    include_plex: bool = True,
    include_jellyfin: bool = True,
    include_emby: bool = True,
) -> dict[str, int | bool]:
    """Run one or more path-refresh batches, then optionally issue one section fallback."""
    stats: dict[str, int | bool] = {
        "refreshed": 0,
        "failed": 0,
        "path_refreshed": 0,
        "path_failed": 0,
        "section_refreshed": 0,
        "section_failed": 0,
        "section_fallback_used": False,
    }

    any_paths = False
    any_path_failed = False
    for folders, update_type in path_batches:
        if not folders:
            continue
        any_paths = True
        path_stats = refresh_all_paths(
            folders,
            update_type=update_type,
            include_plex=include_plex,
            include_jellyfin=include_jellyfin,
            include_emby=include_emby,
        )
        refreshed = int(path_stats.get("refreshed", 0) or 0)
        failed = int(path_stats.get("failed", 0) or 0)
        stats["refreshed"] = int(stats["refreshed"] or 0) + refreshed
        stats["failed"] = int(stats["failed"] or 0) + failed
        stats["path_refreshed"] = int(stats["path_refreshed"] or 0) + refreshed
        stats["path_failed"] = int(stats["path_failed"] or 0) + failed
        if failed > 0:
            any_path_failed = True

    if not any_paths or not enable_section_fallback or (not has_movies and not has_episodes):
        return stats

    # Only broaden to section refresh when targeted path refresh reported failures.
    if not any_path_failed:
        return stats

    wait_seconds = max(0, int(fallback_wait_seconds or 0))
    if wait_seconds > 0:
        time.sleep(wait_seconds)

    section_stats = refresh_all_sections(has_movies, has_episodes)
    section_refreshed = int(section_stats.get("refreshed", 0) or 0)
    section_failed = int(section_stats.get("failed", 0) or 0)

    stats["refreshed"] = int(stats["refreshed"] or 0) + section_refreshed
    stats["failed"] = int(stats["failed"] or 0) + section_failed
    stats["section_refreshed"] = section_refreshed
    stats["section_failed"] = section_failed
    stats["section_fallback_used"] = True

    logger.info(
        "Media server section refresh fallback triggered after failed path refresh batches: "
        f"batch_count={sum(1 for folders, _ in path_batches if folders)} "
        f"has_movies={has_movies} has_episodes={has_episodes} "
        f"section_refreshed={section_refreshed} section_failed={section_failed} "
        f"settle_wait_seconds={wait_seconds}",
        extra={"emoji_type": "info"},
    )
    return stats
