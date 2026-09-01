"""Adapt Tracearr JSON webhook payloads into Placeholdarr playback shape.

Tracearr posts a fixed body (see Tracearr ``jsonWebhook.ts``)::

    {"event": "stream_started", "data": {"media": {"type", "tmdbId", "tvdbId",
     "imdbId", "subtitle": "S01 E02 · Episode Title", ...}}}

Placeholdarr matching expects Tautulli-like ``media.ids`` + ``season_num`` /
``episode_num``. There is no file path in Tracearr's webhook.
"""

from __future__ import annotations

import re
from typing import Any

# Tracearr getMediaDisplay uses spaced "S01 E02"; also accept compact S01E02.
_SEASON_EPISODE_RE = re.compile(
    r"\bS(?P<season>\d{1,3})\s*E(?P<episode>\d{1,3})\b",
    re.IGNORECASE,
)

# Events that should not trigger playback search (accepted as informational).
TRACEARR_IGNORED_EVENTS = frozenset(
    {
        "stream_stopped",
        "violation_detected",
        "server_down",
        "server_up",
        "plugin_update_available",
        "server_update_available",
        "tracearr_update_available",
        "media_added",
        "media_upgraded",
        "new_device",
        "trust_score_changed",
    }
)


def _as_int(value: Any) -> int | None:
    if value is None or value is False:
        return None
    if isinstance(value, bool):
        return None
    try:
        text = str(value).strip()
        if not text or text.lower() in {"null", "none", "undefined"}:
            return None
        return int(float(text)) if "." in text else int(text)
    except (TypeError, ValueError):
        return None


def parse_season_episode_from_subtitle(subtitle: str | None) -> tuple[int | None, int | None]:
    text = str(subtitle or "").strip()
    if not text:
        return None, None
    match = _SEASON_EPISODE_RE.search(text)
    if not match:
        return None, None
    return _as_int(match.group("season")), _as_int(match.group("episode"))


def _map_media_type(raw: Any) -> str | None:
    text = str(raw or "").strip().lower()
    if not text:
        return None
    if text in {"movie", "film"}:
        return "movie"
    if text in {"episode", "show", "series", "season"}:
        return "episode" if text == "episode" else text
    return text


def flatten_tracearr_stream_started(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert a Tracearr ``stream_started`` body into Tautulli-like playback JSON."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    media_in = data.get("media") if isinstance(data.get("media"), dict) else {}

    media_type = _map_media_type(media_in.get("type"))
    season_num, episode_num = parse_season_episode_from_subtitle(
        media_in.get("subtitle") if isinstance(media_in.get("subtitle"), str) else None
    )
    # Prefer structured fields if Tracearr adds them later.
    season_num = _as_int(media_in.get("seasonNumber")) or _as_int(media_in.get("season_num")) or season_num
    episode_num = _as_int(media_in.get("episodeNumber")) or _as_int(media_in.get("episode_num")) or episode_num

    ids: dict[str, Any] = {}
    tmdb = _as_int(media_in.get("tmdbId")) or _as_int(media_in.get("tmdb_id"))
    tvdb = _as_int(media_in.get("tvdbId")) or _as_int(media_in.get("tvdb_id"))
    imdb = media_in.get("imdbId") or media_in.get("imdb_id")
    if tmdb is not None:
        ids["tmdb"] = tmdb
    if tvdb is not None:
        ids["tvdb"] = tvdb
    if isinstance(imdb, str) and imdb.strip():
        ids["imdb"] = imdb.strip()

    media_out: dict[str, Any] = {
        "type": media_type or "",
        "title": media_in.get("title"),
        "ids": ids,
    }
    if season_num is not None:
        media_out["season_num"] = season_num
        media_out["season_number"] = season_num
    if episode_num is not None:
        media_out["episode_num"] = episode_num
        media_out["episode_number"] = episode_num
    if isinstance(media_in.get("subtitle"), str):
        media_out["subtitle"] = media_in["subtitle"]

    out = dict(payload)
    out["event"] = "playback.start"
    out["media"] = media_out
    # Keep original Tracearr envelope for logs / debugging.
    out["_tracearr"] = {
        "event": payload.get("event"),
        "timestamp": payload.get("timestamp"),
    }
    return out


def adapt_tracearr_webhook_payload(payload: dict[str, Any] | Any) -> dict[str, Any]:
    """Normalize a Tracearr webhook payload for Placeholdarr ingest.

    - ``stream_started`` → flattened ``playback.start`` payload
    - known non-start Tracearr events left with their event name (mapped to
      ``webhook_ignored`` by event normalization)
    - other payloads returned unchanged (dict copy)
    """
    if not isinstance(payload, dict):
        return {"raw": payload}

    event = str(payload.get("event") or "").strip().lower()
    if event == "stream_started":
        return flatten_tracearr_stream_started(payload)

    # Preserve a shallow copy so callers can mutate safely.
    return dict(payload)


def is_tracearr_instance(instance: str | None, tracearr_key: str) -> bool:
    return str(instance or "").strip().lower() == str(tracearr_key or "tracearr").strip().lower()
