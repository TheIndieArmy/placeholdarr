from __future__ import annotations

import os
from urllib.parse import quote

import requests

from core.config import settings
from core.logger import logger


def refresh_plex_paths(paths: set[str], *, update_type: str = "Created") -> dict[str, int]:
    """Request path-scoped Plex refresh for changed folders.

    Path-scoped refresh avoids broad library sweeps by targeting only the
    specific folders where placeholder files were created or deleted.
    """
    if not paths:
        return {"refreshed": 0, "failed": 0}

    if not getattr(settings, "plex_enabled", False):
        return {"refreshed": 0, "failed": 0}

    plex_url = getattr(settings, "PLEX_URL", None)
    plex_token = getattr(settings, "PLEX_TOKEN", None)
    movie_section_id = getattr(settings, "PLEX_MOVIE_SECTION_ID", None)
    tv_section_id = getattr(settings, "PLEX_TV_SECTION_ID", None)
    if not plex_url or not plex_token:
        return {"refreshed": 0, "failed": 0}

    refreshed = 0
    failed = 0

    normalized_folders = []
    for path in sorted(paths):
        abs_path = os.path.abspath(path)
        normalized_folders.append(os.path.dirname(abs_path) if os.path.isfile(abs_path) else abs_path)

    for folder in dict.fromkeys(normalized_folders):
        try:
            abs_folder = os.path.abspath(folder)

            section_id = None
            movie_roots = [
                getattr(settings, "MOVIE_LIBRARY_FOLDER", None),
                getattr(settings, "MOVIE_LIBRARY_4K_FOLDER", None),
            ]
            tv_roots = [
                getattr(settings, "TV_LIBRARY_FOLDER", None),
                getattr(settings, "TV_LIBRARY_4K_FOLDER", None),
            ]

            for root in [r for r in movie_roots if r]:
                try:
                    if os.path.commonpath([abs_folder, os.path.abspath(root)]) == os.path.abspath(root):
                        section_id = movie_section_id
                        break
                except Exception:
                    continue

            if section_id is None:
                for root in [r for r in tv_roots if r]:
                    try:
                        if os.path.commonpath([abs_folder, os.path.abspath(root)]) == os.path.abspath(root):
                            section_id = tv_section_id
                            break
                    except Exception:
                        continue

            if section_id is None:
                logger.debug(
                    f"Skipping Plex refresh for out-of-scope folder: {abs_folder}",
                    extra={"emoji_type": "debug"},
                )
                continue

            url = f"{str(plex_url).rstrip('/')}/library/sections/{section_id}/refresh?path={quote(abs_folder)}"
            response = requests.get(url, headers={"X-Plex-Token": plex_token}, timeout=15)
            response.raise_for_status()
            refreshed += 1
        except Exception as e:
            failed += 1
            logger.warning(
                f"Path-scoped Plex refresh failed for folder={folder}: {e}",
                extra={"emoji_type": "warning"},
            )

    return {"refreshed": refreshed, "failed": failed}


def refresh_plex_sections(has_movies: bool, has_episodes: bool) -> dict[str, int]:
    """Send a single full-section refresh per affected Plex library."""
    if not getattr(settings, "plex_enabled", False):
        return {"refreshed": 0, "failed": 0}

    plex_url = getattr(settings, "PLEX_URL", None)
    plex_token = getattr(settings, "PLEX_TOKEN", None)
    if not plex_url or not plex_token:
        return {"refreshed": 0, "failed": 0}

    section_ids: list[int] = []
    if has_movies:
        sid = getattr(settings, "PLEX_MOVIE_SECTION_ID", None)
        if sid:
            section_ids.append(int(sid))
    if has_episodes:
        sid = getattr(settings, "PLEX_TV_SECTION_ID", None)
        if sid:
            section_ids.append(int(sid))

    refreshed = 0
    failed = 0
    for section_id in dict.fromkeys(section_ids):
        try:
            url = f"{str(plex_url).rstrip('/')}/library/sections/{section_id}/refresh"
            response = requests.get(url, headers={"X-Plex-Token": plex_token}, timeout=15)
            response.raise_for_status()
            logger.info(
                f"Triggered full Plex section refresh: section_id={section_id}",
                extra={"emoji_type": "info"},
            )
            refreshed += 1
        except Exception as e:
            logger.warning(
                f"Plex section refresh failed for section_id={section_id}: {e}",
                extra={"emoji_type": "warning"},
            )
            failed += 1

    return {"refreshed": refreshed, "failed": failed}
