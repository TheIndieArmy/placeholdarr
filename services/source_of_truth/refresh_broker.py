from __future__ import annotations

import threading
from typing import Any, Callable

from core.config import settings
from core.logger import logger
from services.source_of_truth.refresh_throttle import try_acquire_refresh_lease

_BULK_LEASE_SOURCE = "materialization_bulk_initial"

_bulk_intent_lock = threading.Lock()
_bulk_intent_merged: dict[str, bool] = {"has_movies": False, "has_episodes": False}


def observation_source_skips_bulk_section_refresh(observation_source: str) -> bool:
    """Single-title work must use path refresh only, not a full library scan."""
    token = str(observation_source or "").strip().lower()
    if token == "entity_reconcile":
        return True
    if token.startswith("tag_policy_movie:") or token.startswith("tag_policy_series:"):
        return True
    return False


def bulk_section_refresh_min_interval_seconds() -> int:
    return max(
        0,
        int(getattr(settings, "MATERIALIZATION_OVERLAP_REFRESH_MIN_INTERVAL_SECONDS", 90) or 90),
    )


def bulk_section_refresh_lease_seconds() -> int:
    return max(
        0,
        int(getattr(settings, "MATERIALIZATION_OVERLAP_REFRESH_LEASE_SECONDS", 180) or 180),
    )


def _merge_bulk_intent(*, has_movies: bool, has_episodes: bool) -> tuple[bool, bool]:
    with _bulk_intent_lock:
        _bulk_intent_merged["has_movies"] = bool(_bulk_intent_merged["has_movies"]) or bool(has_movies)
        _bulk_intent_merged["has_episodes"] = bool(_bulk_intent_merged["has_episodes"]) or bool(has_episodes)
        return bool(_bulk_intent_merged["has_movies"]), bool(_bulk_intent_merged["has_episodes"])


def reset_bulk_intent_after_dispatch() -> None:
    with _bulk_intent_lock:
        _bulk_intent_merged["has_movies"] = False
        _bulk_intent_merged["has_episodes"] = False


def prepare_bulk_section_refresh(
    *,
    observation_source: str,
    has_movies: bool,
    has_episodes: bool,
    resolve_section_ids: Callable[..., list[int]],
) -> dict[str, Any]:
    """Rate-limit and coalesce intent before scheduling a bulk library refresh.

    Callers still enqueue the media_refresh job when ``allow_schedule`` is true.
    """
    if observation_source_skips_bulk_section_refresh(observation_source):
        logger.debug(
            f"Bulk section refresh skipped for path-only observation_source={observation_source}",
            extra={"emoji_type": "debug"},
        )
        return {
            "allow_schedule": False,
            "reason": "path_only_source",
            "has_movies": bool(has_movies),
            "has_episodes": bool(has_episodes),
        }

    target_sections = resolve_section_ids(has_movies=bool(has_movies), has_episodes=bool(has_episodes))
    lease = try_acquire_refresh_lease(
        section_ids=target_sections,
        source=_BULK_LEASE_SOURCE,
        min_interval_seconds=bulk_section_refresh_min_interval_seconds(),
        lease_seconds=bulk_section_refresh_lease_seconds(),
    )
    if not bool(lease.get("allowed", False)):
        reason = str(lease.get("reason") or "blocked")
        logger.info(
            "Bulk section refresh suppressed by refresh broker "
            f"reason={reason} blocked_sections={lease.get('blocked_section_ids')} "
            f"observation_source={observation_source}",
            extra={"emoji_type": "info"},
        )
        return {
            "allow_schedule": False,
            "reason": reason,
            "blocked_section_ids": list(lease.get("blocked_section_ids") or []),
            "has_movies": bool(has_movies),
            "has_episodes": bool(has_episodes),
        }

    merged_movies, merged_episodes = _merge_bulk_intent(
        has_movies=bool(has_movies),
        has_episodes=bool(has_episodes),
    )
    return {
        "allow_schedule": True,
        "reason": "granted",
        "granted_section_ids": list(lease.get("granted_section_ids") or []),
        "has_movies": merged_movies,
        "has_episodes": merged_episodes,
    }
