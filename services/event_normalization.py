from __future__ import annotations

from dataclasses import dataclass
from typing import Any


CANONICAL_EVENT_ALIASES: dict[str, tuple[str, ...]] = {
    "series_added": ("seriesadd",),
    "movie_added": ("movieadd", "movieadded"),
    "movie_imported": ("moviefileimported",),
    "episode_imported": ("episodefileimported",),
    "movie_file_deleted": ("moviefiledelete",),
    "episode_file_deleted": ("episodefiledelete",),
    "movie_deleted": ("moviedelete",),
    "series_deleted": ("seriesdelete",),
    "playback_start": ("playback.start", "playbackstart"),
}


@dataclass(frozen=True)
class NormalizedEventType:
    raw_event_type: str
    canonical_event_type: str
    matched_alias: str | None
    is_known: bool


def infer_raw_event_type(payload: dict[str, Any] | Any) -> str:
    if not isinstance(payload, dict):
        return "unknown"

    raw = (
        payload.get("eventType")
        or payload.get("event_type")
        or payload.get("type")
        or payload.get("event")
        or payload.get("Event")
        or payload.get("NotificationType")
        or payload.get("notificationType")
        or payload.get("notificationtype")
        or "unknown"
    )
    return str(raw or "unknown").strip().lower()


def _normalize_download_event(instance: str | None) -> NormalizedEventType:
    """Resolve ambiguous ARR download events using webhook instance."""
    normalized_instance = str(instance or "").strip().lower()

    if normalized_instance.startswith("radarr"):
        return NormalizedEventType(
            raw_event_type="download",
            canonical_event_type="movie_imported",
            matched_alias="download",
            is_known=True,
        )

    if normalized_instance.startswith("sonarr"):
        return NormalizedEventType(
            raw_event_type="download",
            canonical_event_type="episode_imported",
            matched_alias="download",
            is_known=True,
        )

    # Instance is required and validated before processing webhook events.
    return NormalizedEventType(
        raw_event_type="download",
        canonical_event_type="unknown",
        matched_alias=None,
        is_known=False,
    )


def normalize_event_type(raw_event_type: str | None, instance: str | None = None) -> NormalizedEventType:
    raw = str(raw_event_type or "unknown").strip().lower()

    if raw == "download":
        return _normalize_download_event(instance)

    for canonical, aliases in CANONICAL_EVENT_ALIASES.items():
        for alias in aliases:
            if raw == alias:
                return NormalizedEventType(
                    raw_event_type=raw,
                    canonical_event_type=canonical,
                    matched_alias=alias,
                    is_known=True,
                )

    return NormalizedEventType(
        raw_event_type=raw,
        canonical_event_type="unknown",
        matched_alias=None,
        is_known=False,
    )


def legacy_dispatch_event_type(canonical_event_type: str, raw_event_type: str | None = None) -> str:
    """Return legacy event labels expected by current handlers.

    This keeps Phase 1 backward-compatible while the worker dispatcher is still
    migrating to canonical event families.
    """
    canonical = str(canonical_event_type or "").strip().lower()
    raw = str(raw_event_type or "").strip().lower()

    if canonical == "series_added":
        return "seriesadd"
    if canonical == "movie_added":
        # Preserve the legacy raw variant for logs where possible.
        return raw if raw in {"movieadd", "movieadded"} else "movieadd"
    if canonical == "playback_start":
        return raw if raw in {"playback.start", "playbackstart"} else "playbackstart"

    # Unknown/unsupported canonical events keep their raw value for observability.
    return raw or canonical or "unknown"
