"""Status-aware Coming Soon overlay copy for placeholder posters."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from services.app_timezone import local_today

# Hardcoded English for v1 (same as today's hardcoded PLACEHOLDER banner).
OVERLAY_BANNER_PLACEHOLDER = "PLACEHOLDER"
OVERLAY_BANNER_COMING_SOON = "Coming Soon"
OVERLAY_BANNER_NEW_EP_PREFIX = "New Ep."

_COMING_SOON_STATUSES = frozenset(
    {
        "COMING_SOON",
        "COMING_SOON_30",
        "COMING_SOON_14",
        "COMING_SOON_7",
        "COMING_SOON_1",
        "COMING_SOON_TODAY",
    }
)

_RELEASE_TYPE_LABELS = {
    "inCinemas": "Theatrical",
    "digitalRelease": "Digital",
    "physicalRelease": "Physical",
}


def is_coming_soon_overlay_status(status: str | None) -> bool:
    return str(status or "").strip() in _COMING_SOON_STATUSES


def release_type_label(release_type: str | None) -> str:
    key = str(release_type or "").strip()
    return _RELEASE_TYPE_LABELS.get(key, "Release")


_MONTH_ABBREV = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def format_overlay_date(value: date | datetime | None) -> str | None:
    """Locale-light absolute date for banners, e.g. ``Oct 22`` (no year)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        return None
    # Fixed English abbrevs so container locales cannot change the banner string.
    return f"{_MONTH_ABBREV[value.month - 1]} {value.day}"


def coming_soon_overlay_label(
    *,
    media_type: str,
    display_status: str | None,
    target_date: date | datetime | None = None,
    release_type: str | None = None,
) -> str | None:
    """Return Coming Soon banner text when status is coming soon; otherwise None.

    With a date: ``Digital - Oct 22`` / ``Airing - Oct 22``.
    With no date: ``Coming Soon``.
    """
    if not is_coming_soon_overlay_status(display_status):
        return None

    date_str = format_overlay_date(target_date)
    if not date_str:
        return OVERLAY_BANNER_COMING_SOON

    kind = str(media_type or "").strip().lower()
    if kind in ("episode", "tv", "show"):
        return f"Airing - {date_str}"
    label = release_type_label(release_type)
    return f"{label} - {date_str}"


def overlay_banner_text(
    *,
    media_type: str,
    display_status: str | None,
    target_date: date | datetime | None = None,
    release_type: str | None = None,
) -> str:
    """Legacy helper: Coming Soon label or ``PLACEHOLDER`` when not coming soon."""
    label = coming_soon_overlay_label(
        media_type=media_type,
        display_status=display_status,
        target_date=target_date,
        release_type=release_type,
    )
    return label or OVERLAY_BANNER_PLACEHOLDER


def resolve_draw_overlay_text(
    *,
    mode: str | None = None,
    coming_soon_banner_enabled: bool = False,
    coming_soon_label: str | None = None,
    placeholder_banner_enabled: bool | None = None,
) -> str | None:
    """Text passed into compositing.

    Prefer layered flags: Coming Soon / New Ep. date label when applicable,
    else PLACEHOLDER when the placeholder-banner toggle is on.

    Legacy ``mode == top_banner`` still implies PLACEHOLDER when the placeholder
    flag is omitted (older call sites).
    """
    want = str(mode or "").strip().lower()
    if coming_soon_banner_enabled and coming_soon_label:
        return coming_soon_label
    ph_on = placeholder_banner_enabled
    if ph_on is None:
        ph_on = want == "top_banner"
    if ph_on:
        return OVERLAY_BANNER_PLACEHOLDER
    return None


def new_ep_overlay_label(target_date: date | datetime | None) -> str | None:
    """``New Ep. - Oct 22`` when a next air date is known."""
    date_str = format_overlay_date(target_date)
    if not date_str:
        return None
    return f"{OVERLAY_BANNER_NEW_EP_PREFIX} - {date_str}"


def next_unaired_episode_air_date(
    *,
    series_id: int,
    season_id: int | None = None,
    now_date: date | None = None,
) -> date | None:
    """Earliest future (or today) air date among episodes that do not yet have a file."""
    from services.postgres.db import session_scope
    from services.postgres.models import Episode, Season

    today = now_date or local_today()
    try:
        with session_scope() as session:
            q = (
                session.query(Episode.air_date)
                .join(Season, Season.id == Episode.season_id)
                .filter(
                    Season.series_id == int(series_id),
                    Season.is_deleted == False,  # noqa: E712
                    Episode.is_deleted == False,  # noqa: E712
                    Episode.has_file == False,  # noqa: E712
                    Episode.air_date.isnot(None),
                    Episode.air_date >= today,
                )
            )
            if season_id is not None:
                q = q.filter(Episode.season_id == int(season_id))
            row = q.order_by(Episode.air_date.asc()).first()
            if not row:
                return None
            return _coerce_date(row[0])
    except Exception:
        return None


def resolve_series_new_ep_label(
    series: Any,
    *,
    season: Any | None = None,
    now_date: date | None = None,
) -> str | None:
    """New Ep. banner for a series or season poster from the next unaired episode."""
    series_id = getattr(series, "id", None)
    if series_id is None:
        return None
    season_id = getattr(season, "id", None) if season is not None else None
    air = next_unaired_episode_air_date(
        series_id=int(series_id),
        season_id=int(season_id) if season_id is not None else None,
        now_date=now_date,
    )
    return new_ep_overlay_label(air)


def _coerce_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def resolve_movie_coming_soon_label(movie: Any, *, display_status: str | None = None) -> str | None:
    """Coming Soon label for a movie, or None when not in a coming-soon status."""
    from services.source_of_truth.calendar_phase import (
        _preferred_movie_release_date,
        compute_calendar_decision_for_movie,
    )

    target_date, release_type, _preferred = _preferred_movie_release_date(movie)
    status = display_status
    if status is None:
        status = compute_calendar_decision_for_movie(movie).status
    return coming_soon_overlay_label(
        media_type="movie",
        display_status=status,
        target_date=_coerce_date(target_date),
        release_type=release_type,
    )


def resolve_episode_coming_soon_label(
    episode: Any,
    *,
    display_status: str | None = None,
    session=None,
) -> str | None:
    """Coming Soon label for an episode, or None when not in a coming-soon status."""
    from services.source_of_truth.calendar_phase import compute_calendar_decision_for_episode

    status = display_status
    if status is None:
        status = compute_calendar_decision_for_episode(episode, session=session).status
    return coming_soon_overlay_label(
        media_type="episode",
        display_status=status,
        target_date=_coerce_date(getattr(episode, "air_date", None)),
        release_type=None,
    )


# Backward-compatible aliases used by earlier Coming Soon overlay work.
def resolve_movie_overlay_banner_text(movie: Any, *, display_status: str | None = None) -> str:
    return resolve_movie_coming_soon_label(movie, display_status=display_status) or OVERLAY_BANNER_PLACEHOLDER


def resolve_episode_overlay_banner_text(
    episode: Any,
    *,
    display_status: str | None = None,
    session=None,
) -> str:
    return (
        resolve_episode_coming_soon_label(episode, display_status=display_status, session=session)
        or OVERLAY_BANNER_PLACEHOLDER
    )


def lookup_active_placeholder_display_status(
    *,
    movie_id: int | None = None,
    episode_id: int | None = None,
) -> str | None:
    """Return ``display_status`` for the active placeholder row, if any."""
    if movie_id is None and episode_id is None:
        return None
    try:
        from services.postgres.db import session_scope
        from services.postgres.models import Placeholder

        with session_scope() as session:
            q = session.query(Placeholder).filter(Placeholder.has_placeholder == True)  # noqa: E712
            if movie_id is not None:
                q = q.filter(Placeholder.movie_id == int(movie_id))
            else:
                q = q.filter(Placeholder.episode_id == int(episode_id))
            row = q.order_by(Placeholder.id.desc()).first()
            if row is None:
                return None
            status = getattr(row, "display_status", None)
            return str(status) if status else None
    except Exception:
        return None
