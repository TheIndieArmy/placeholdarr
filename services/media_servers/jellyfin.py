from __future__ import annotations

import os

import requests

from core.config import settings
from core.logger import logger


def _build_url(endpoint: str) -> str:
    base = str(getattr(settings, "JELLYFIN_URL", "") or "").rstrip("/")
    return f"{base}/{endpoint.lstrip('/')}"


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "X-Emby-Token": getattr(settings, "JELLYFIN_TOKEN", "") or "",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    return s


def refresh_jellyfin_paths(folders: set[str]) -> dict[str, int]:
    """Notify Jellyfin about changed folders via Library/Media/Updated."""
    if not getattr(settings, "jellyfin_enabled", False):
        return {"refreshed": 0, "failed": 0}
    if not folders:
        return {"refreshed": 0, "failed": 0}

    refreshed = 0
    failed = 0
    sess = _session()
    url = _build_url("Library/Media/Updated")

    for folder in sorted(folders):
        try:
            abs_folder = os.path.abspath(folder)
            payload = {"Updates": [{"Path": abs_folder, "UpdateType": "Created"}]}
            resp = sess.post(url, json=payload, timeout=15)
            if resp.status_code in (200, 204):
                refreshed += 1
                logger.info(
                    f"Jellyfin scan triggered for: {abs_folder}",
                    extra={"emoji_type": "refresh"},
                )
            else:
                failed += 1
                logger.warning(
                    f"Jellyfin scan returned {resp.status_code} for {abs_folder}: {resp.text}",
                    extra={"emoji_type": "warning"},
                )
        except Exception as e:
            failed += 1
            logger.warning(
                f"Jellyfin path refresh failed for folder={folder}: {e}",
                extra={"emoji_type": "warning"},
            )

    return {"refreshed": refreshed, "failed": failed}


def refresh_jellyfin_sections(has_movies: bool, has_episodes: bool) -> dict[str, int]:
    """Trigger a Jellyfin library scan for movie and/or TV root folders.

    Jellyfin responds better to one broad root-folder scan than many
    small per-folder requests, so this sends the configured library root
    paths rather than individual sub-folders.
    """
    if not getattr(settings, "jellyfin_enabled", False):
        return {"refreshed": 0, "failed": 0}

    roots: list[str] = []
    if has_movies:
        for key in ("MOVIE_LIBRARY_FOLDER", "MOVIE_LIBRARY_4K_FOLDER"):
            val = getattr(settings, key, None)
            if val:
                roots.append(val)
    if has_episodes:
        for key in ("TV_LIBRARY_FOLDER", "TV_LIBRARY_4K_FOLDER"):
            val = getattr(settings, key, None)
            if val:
                roots.append(val)

    if not roots:
        return {"refreshed": 0, "failed": 0}

    refreshed = 0
    failed = 0
    sess = _session()
    url = _build_url("Library/Media/Updated")

    for root in dict.fromkeys(roots):
        try:
            payload = {"Updates": [{"Path": root, "UpdateType": "Created"}]}
            resp = sess.post(url, json=payload, timeout=15)
            if resp.status_code in (200, 204):
                refreshed += 1
                logger.info(
                    f"Jellyfin library scan triggered for root: {root}",
                    extra={"emoji_type": "refresh"},
                )
            else:
                failed += 1
                logger.warning(
                    f"Jellyfin library scan returned {resp.status_code} for {root}: {resp.text}",
                    extra={"emoji_type": "warning"},
                )
        except Exception as e:
            failed += 1
            logger.warning(
                f"Jellyfin section refresh failed for root={root}: {e}",
                extra={"emoji_type": "warning"},
            )

    return {"refreshed": refreshed, "failed": failed}
