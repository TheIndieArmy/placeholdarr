"""Playback monitor/search filters scoped to library destination folders."""

from __future__ import annotations

import json
from typing import Any

from core.config import settings
from services.library_destinations import (
    normalize_dest_folder,
    resolve_movie_dest,
    resolve_series_dest,
    selectable_playback_dests,
)

PLAYBACK_MONITOR_ONLY_DESTS_KEY = "PLAYBACK_MONITOR_ONLY_DESTS"
PLAYBACK_SEARCH_ALREADY_MONITORED_DESTS_KEY = "PLAYBACK_SEARCH_ALREADY_MONITORED_DESTS"
PLAYBACK_SEARCH_FUTURE_DESTS_KEY = "PLAYBACK_SEARCH_FUTURE_DESTS"


def _parse_dest_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            items = parsed if isinstance(parsed, list) else []
        except Exception:
            items = [p.strip() for p in text.split(",") if p.strip()]
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        path = normalize_dest_folder(str(item or ""))
        if not path:
            continue
        key = path.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def parse_playback_dest_list(raw: Any) -> list[str]:
    """Normalize a string_list / JSON / comma setting value into dest folder paths."""
    return _parse_dest_list(raw)


def playback_filter_dest_list(setting_key: str) -> list[str]:
    return _parse_dest_list(getattr(settings, setting_key, None))


def dest_in_playback_filter(dest_folder: str | None, setting_key: str) -> bool:
    """True when the resolved dest is listed for this filter (empty list = off everywhere)."""
    want = normalize_dest_folder(dest_folder)
    if not want:
        return False
    want_key = want.lower()
    for path in playback_filter_dest_list(setting_key):
        if path.lower() == want_key:
            return True
    return False


def monitor_only_for_dest(dest_folder: str | None) -> bool:
    return dest_in_playback_filter(dest_folder, PLAYBACK_MONITOR_ONLY_DESTS_KEY)


def search_already_monitored_for_dest(dest_folder: str | None) -> bool:
    """Affirmative: search already-monitored titles on this dest."""
    return dest_in_playback_filter(dest_folder, PLAYBACK_SEARCH_ALREADY_MONITORED_DESTS_KEY)


def search_future_for_dest(dest_folder: str | None) -> bool:
    """Affirmative: search future titles on this dest."""
    return dest_in_playback_filter(dest_folder, PLAYBACK_SEARCH_FUTURE_DESTS_KEY)


def suppress_search_already_monitored_for_dest(dest_folder: str | None) -> bool:
    """Legacy polarity helper: True = do not search already-monitored on this dest."""
    return not search_already_monitored_for_dest(dest_folder)


def suppress_search_future_for_dest(dest_folder: str | None) -> bool:
    """Legacy polarity helper: True = do not search future titles on this dest."""
    return not search_future_for_dest(dest_folder)


def dest_folder_for_movie_row(movie_row: Any) -> str:
    instance_key = str(getattr(movie_row, "instance_key", None) or "").strip() or None
    arr_path = getattr(movie_row, "radarrpath", None)
    resolved = resolve_movie_dest(instance_key=instance_key, arr_path=arr_path if isinstance(arr_path, str) else None)
    return normalize_dest_folder(getattr(resolved, "dest_folder", None))


def dest_folder_for_series_row(series_row: Any) -> str:
    instance_key = str(getattr(series_row, "instance_key", None) or "").strip() or None
    arr_path = getattr(series_row, "sonarrpath", None)
    resolved = resolve_series_dest(instance_key=instance_key, arr_path=arr_path if isinstance(arr_path, str) else None)
    return normalize_dest_folder(getattr(resolved, "dest_folder", None))


def all_selectable_dest_folders() -> list[str]:
    grouped = selectable_playback_dests()
    out: list[str] = []
    seen: set[str] = set()
    for path in list(grouped.get("movies") or []) + list(grouped.get("tv") or []):
        norm = normalize_dest_folder(path)
        if not norm:
            continue
        key = norm.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(norm)
    return out
