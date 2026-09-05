"""Force-placeholder pin preview and apply helpers for library detail UI."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from core.config import settings
from services.postgres.models import Episode, Movie, Season
from services.source_of_truth.arr_share_guard import (
    shared_placeholder_suppresses_creation,
    sibling_episode_has_file,
    sibling_movie_has_file,
)
from services.source_of_truth.determiner import (
    _preferred_movie_release_date,
    _series_monitored_for_episode,
    _sibling_would_suppress_creation,
    _skip_placeholders_when_monitored_enabled,
    _skip_placeholders_when_series_monitored_enabled,
)


def _preferred_movie_date_label() -> str:
    preferred = str(getattr(settings, "PREFERRED_MOVIE_DATE_TYPE", "inCinemas") or "inCinemas").strip()
    mapping = {
        "inCinemas": "theatrical release",
        "digitalRelease": "digital release",
        "physicalRelease": "physical release",
    }
    return mapping.get(preferred, "theatrical release")


def _calendar_would_block(
    *,
    has_file: bool,
    is_deleted: bool,
    target_date: date | None,
    release_status: str | None,
    now_date: date,
) -> bool:
    if not bool(settings.coming_soon_placeholders_enabled):
        return False
    if has_file or is_deleted:
        return False
    lookahead = int(getattr(settings, "CALENDAR_LOOKAHEAD_DAYS", 30) or 30)
    if lookahead < 0:
        return False
    if target_date is None:
        return str(release_status or "").strip().lower() != "released"
    days_until = (target_date - now_date).days
    if lookahead == 0:
        return days_until > 0
    return days_until > lookahead


def _movie_blocking_reasons(session, movie: Movie, *, now_date: date) -> list[str]:
    reasons: list[str] = []
    has_file = bool(getattr(movie, "has_file", False))
    is_deleted = bool(getattr(movie, "is_deleted", False))
    target_date = _preferred_movie_release_date(movie)
    if _calendar_would_block(
        has_file=has_file,
        is_deleted=is_deleted,
        target_date=target_date,
        release_status=getattr(movie, "radarr_release_status", None),
        now_date=now_date,
    ):
        label = _preferred_movie_date_label()
        if target_date is None:
            reasons.append(f"Outside calendar window ({label} date unknown).")
        else:
            days = (target_date - now_date).days
            reasons.append(
                f"Outside calendar window ({label} {target_date.isoformat()}, {days} days away)."
            )
    if (
        _skip_placeholders_when_monitored_enabled()
        and not has_file
        and not is_deleted
        and bool(getattr(movie, "radarr_monitored", False))
    ):
        reasons.append("Skip-when-monitored is on and this title is monitored in Radarr.")
    return reasons


def _episode_blocking_reasons(session, episode: Episode, *, now_date: date) -> list[str]:
    reasons: list[str] = []
    has_file = bool(getattr(episode, "has_file", False))
    is_deleted = bool(getattr(episode, "is_deleted", False))
    season = session.query(Season).filter(Season.id == episode.season_id).first()
    season_number = int(getattr(season, "season_number", 0) or 0) if season else 0
    include_specials = bool(getattr(settings, "INCLUDE_SPECIALS", False))
    if not include_specials and season_number == 0:
        reasons.append("Season 0 specials are excluded by settings.")
    air_date = getattr(episode, "air_date", None)
    if _calendar_would_block(
        has_file=has_file,
        is_deleted=is_deleted,
        target_date=air_date,
        release_status=None,
        now_date=now_date,
    ):
        if air_date is None:
            reasons.append("Outside calendar window (air date unknown).")
        else:
            days = (air_date - now_date).days
            reasons.append(f"Outside calendar window (airs {air_date.isoformat()}, {days} days away).")
    series_monitored = (
        _series_monitored_for_episode(session, episode)
        if _skip_placeholders_when_series_monitored_enabled()
        else False
    )
    if (
        _skip_placeholders_when_monitored_enabled()
        and not has_file
        and not is_deleted
        and (bool(getattr(episode, "sonarr_monitored", False)) or series_monitored)
    ):
        reasons.append("Skip-when-monitored is on and this episode (or series) is monitored in Sonarr.")
    return reasons


def preview_movie_force_placeholder(session, movie: Movie) -> dict[str, Any]:
    now_date = datetime.now(timezone.utc).date()
    has_file = bool(getattr(movie, "has_file", False))
    is_deleted = bool(getattr(movie, "is_deleted", False))
    sibling_has_file = sibling_movie_has_file(session, movie)
    shared_on = shared_placeholder_suppresses_creation("radarr")
    can_force = not has_file and not is_deleted
    block_message = None
    if has_file:
        block_message = "A real file already exists. Pin does nothing in that case."
    elif is_deleted:
        block_message = "This title was removed from Radarr. Pin cannot be applied."
    return {
        "ok": True,
        "media_type": "movie",
        "title": str(getattr(movie, "title", "") or "Movie"),
        "force_placeholder": bool(getattr(movie, "force_placeholder", False)),
        "force_placeholder_despite_sibling": bool(
            getattr(movie, "force_placeholder_despite_sibling", False)
        ),
        "can_force": can_force,
        "block_message": block_message,
        "has_file": has_file,
        "is_deleted": is_deleted,
        "blocking_reasons": _movie_blocking_reasons(session, movie, now_date=now_date),
        "sibling_has_file": sibling_has_file,
        "shared_suppression_enabled": shared_on,
        "sibling_option_available": bool(
            can_force and shared_on and sibling_has_file
        ),
        "sibling_would_suppress": _sibling_would_suppress_creation(
            arr_type="radarr",
            has_file=has_file,
            is_deleted=is_deleted,
            sibling_has_file=sibling_has_file,
        ),
    }


def preview_episode_force_placeholder(session, episode: Episode) -> dict[str, Any]:
    now_date = datetime.now(timezone.utc).date()
    has_file = bool(getattr(episode, "has_file", False))
    is_deleted = bool(getattr(episode, "is_deleted", False))
    sibling_has_file = sibling_episode_has_file(session, episode)
    shared_on = shared_placeholder_suppresses_creation("sonarr")
    can_force = not has_file and not is_deleted
    block_message = None
    if has_file:
        block_message = "A real file already exists. Pin does nothing in that case."
    elif is_deleted:
        block_message = "This episode was removed from Sonarr. Pin cannot be applied."
    title = str(getattr(episode, "title", "") or f"Episode {getattr(episode, 'episode_number', '')}")
    return {
        "ok": True,
        "media_type": "episode",
        "title": title,
        "force_placeholder": bool(getattr(episode, "force_placeholder", False)),
        "force_placeholder_despite_sibling": bool(
            getattr(episode, "force_placeholder_despite_sibling", False)
        ),
        "can_force": can_force,
        "block_message": block_message,
        "has_file": has_file,
        "is_deleted": is_deleted,
        "blocking_reasons": _episode_blocking_reasons(session, episode, now_date=now_date),
        "sibling_has_file": sibling_has_file,
        "shared_suppression_enabled": shared_on,
        "sibling_option_available": bool(
            can_force and shared_on and sibling_has_file
        ),
        "sibling_would_suppress": _sibling_would_suppress_creation(
            arr_type="sonarr",
            has_file=has_file,
            is_deleted=is_deleted,
            sibling_has_file=sibling_has_file,
        ),
    }


def apply_force_placeholder_flags(
    entity: Movie | Episode,
    *,
    enabled: bool,
    despite_sibling: bool,
) -> None:
    """Mutate entity force flags. Caller owns session commit."""
    from services.source_of_truth.placeholder_policy import apply_placeholder_policy

    apply_placeholder_policy(
        entity,
        policy="pinned" if enabled else "auto",
        despite_sibling=despite_sibling,
    )
