"""Library grid 'Future' = outside calendar lookahead (why no placeholder yet).

This must stay aligned with ``_compute_determination`` in
``services/source_of_truth/determiner.py`` for the calendar guard that yields
``not_needed`` without a file/placeholder.

Policy-only ``not_needed`` (e.g. season 0 specials when ``INCLUDE_SPECIALS`` is
false) is excluded from Future counts — those rows are not "beyond lookahead".
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import Date, and_, cast, func, literal, or_
from sqlalchemy.sql import ColumnElement

from core.config import settings

if TYPE_CHECKING:
    from services.postgres.models import Episode, Movie

DETERMINATION_NOT_NEEDED = "not_needed"


def _utc_today_sql() -> ColumnElement:
    return cast(func.timezone("UTC", func.now()), Date)


def _lookahead_int() -> int:
    try:
        return int(getattr(settings, "CALENDAR_LOOKAHEAD_DAYS", 30) or 30)
    except (TypeError, ValueError):
        return 30


def _include_specials() -> bool:
    return bool(getattr(settings, "INCLUDE_SPECIALS", False))


def _placeholders_enabled() -> bool:
    return bool(settings.coming_soon_placeholders_enabled)


def _preferred_movie_release_date_column(Movie: Any) -> Any:
    preferred = str(getattr(settings, "PREFERRED_MOVIE_DATE_TYPE", "inCinemas") or "inCinemas").strip()
    mapping = {
        "inCinemas": "theater_release_date",
        "digitalRelease": "digital_release_date",
        "physicalRelease": "physical_release_date",
    }
    field = mapping.get(preferred, "theater_release_date")
    return getattr(Movie, field)


def sql_episode_future_outside_lookahead(Episode: Any, Season: Any) -> ColumnElement:
    """SQL predicate: episode counts as Future on the library grid (outside lookahead)."""
    if not _placeholders_enabled():
        return literal(False)

    eff = _utc_today_sql()
    lk = _lookahead_int()
    include_specials = _include_specials()
    policy_ok = or_(Season.season_number != 0, literal(include_specials))

    lk_lit = literal(lk)
    ad = Episode.air_date
    # Date subtraction in PostgreSQL yields integer days for date - date.
    day_delta = ad - eff

    unknown_in_strict_lookahead = and_(ad.is_(None), lk_lit >= 0)
    known_lt_neg = and_(ad.isnot(None), lk_lit < 0, day_delta > lk_lit)
    known_eq_zero = and_(ad.isnot(None), lk_lit == 0, ad > eff)
    known_gt_zero = and_(ad.isnot(None), lk_lit > 0, day_delta > lk_lit)

    calendar_ok = or_(unknown_in_strict_lookahead, known_lt_neg, known_eq_zero, known_gt_zero)

    return and_(
        policy_ok,
        Episode.determination == DETERMINATION_NOT_NEEDED,
        func.coalesce(Episode.has_file, False) == False,  # noqa: E712
        func.coalesce(Episode.has_placeholder, False) == False,  # noqa: E712
        calendar_ok,
    )


def sql_movie_future_outside_lookahead(Movie: Any) -> ColumnElement:
    """SQL predicate: movie counts as Future on the library grid (outside lookahead)."""
    if not _placeholders_enabled():
        return literal(False)

    eff = _utc_today_sql()
    lk = _lookahead_int()
    lk_lit = literal(lk)
    target = _preferred_movie_release_date_column(Movie)
    rel = Movie.radarr_release_status
    not_released = func.lower(func.coalesce(rel, "")) != "released"

    day_delta = target - eff

    unknown_in_strict_lookahead = and_(target.is_(None), not_released, lk_lit >= 0)
    known_lt_neg = and_(target.isnot(None), lk_lit < 0, day_delta > lk_lit)
    known_eq_zero = and_(target.isnot(None), lk_lit == 0, target > eff)
    known_gt_zero = and_(target.isnot(None), lk_lit > 0, day_delta > lk_lit)

    calendar_ok = or_(unknown_in_strict_lookahead, known_lt_neg, known_eq_zero, known_gt_zero)

    return and_(
        Movie.determination == DETERMINATION_NOT_NEEDED,
        func.coalesce(Movie.has_file, False) == False,  # noqa: E712
        func.coalesce(Movie.has_placeholder, False) == False,  # noqa: E712
        calendar_ok,
    )


def movie_row_is_future_outside_lookahead(movie: Movie, *, now_date: date | None = None) -> bool:
    """Python mirror of :func:`sql_movie_future_outside_lookahead` for API row building."""
    eff = now_date or datetime.now(timezone.utc).date()
    if not _placeholders_enabled():
        return False
    if str(getattr(movie, "determination", None) or "") != DETERMINATION_NOT_NEEDED:
        return False
    if bool(getattr(movie, "has_file", False)) or bool(getattr(movie, "has_placeholder", False)):
        return False

    lk = _lookahead_int()
    preferred = str(getattr(settings, "PREFERRED_MOVIE_DATE_TYPE", "inCinemas") or "inCinemas").strip()
    mapping = {
        "inCinemas": "theater_release_date",
        "digitalRelease": "digital_release_date",
        "physicalRelease": "physical_release_date",
    }
    field = mapping.get(preferred, "theater_release_date")
    target_date = getattr(movie, field, None)
    release_status = getattr(movie, "radarr_release_status", None)

    if lk < 0:
        if target_date is None:
            return False
        days_until = (target_date - eff).days
        return days_until > lk
    if target_date is None:
        return str(release_status or "").strip().lower() != "released"
    days_until = (target_date - eff).days
    if lk == 0:
        return days_until > 0
    return days_until > lk


def episode_row_is_future_outside_lookahead(episode: Episode, season_number: int, *, now_date: date | None = None) -> bool:
    """Python mirror for per-episode checks (detail views) if needed."""
    eff = now_date or datetime.now(timezone.utc).date()
    if not _placeholders_enabled():
        return False
    if season_number == 0 and not _include_specials():
        return False
    if str(getattr(episode, "determination", None) or "") != DETERMINATION_NOT_NEEDED:
        return False
    if bool(getattr(episode, "has_file", False)) or bool(getattr(episode, "has_placeholder", False)):
        return False

    lk = _lookahead_int()
    target_date = getattr(episode, "air_date", None)

    if lk < 0:
        if target_date is None:
            return False
        days_until = (target_date - eff).days
        return days_until > lk
    if target_date is None:
        return True
    days_until = (target_date - eff).days
    if lk == 0:
        return days_until > 0
    return days_until > lk


def build_series_max_known_order_within_horizon(
    session,
    series_id: int,
    *,
    now_date: date | None = None,
) -> tuple[int, int] | None:
    """Max (season, episode) with air_date inside the calendar horizon for a series.

    Mirrors determination pass logic used for unknown-air-date middle-of-run episodes.
    """
    eff = now_date or datetime.now(timezone.utc).date()
    lk = _lookahead_int()
    if not _placeholders_enabled() or lk < 0:
        return None

    from services.postgres.models import Episode, Season

    horizon = eff + timedelta(days=int(lk)) if lk >= 0 else None
    known_rows = (
        session.query(Season.season_number, Episode.episode_number)
        .join(Season, Episode.season_id == Season.id)
        .filter(
            Season.series_id == int(series_id),
            Episode.air_date.isnot(None),
            Episode.is_deleted == False,  # noqa: E712
        )
        .filter(Episode.air_date <= horizon if horizon is not None else True)
        .all()
    )
    max_order: tuple[int, int] | None = None
    for season_number, episode_number in known_rows:
        order = (int(season_number or 0), int(episode_number or 0))
        if max_order is None or order > max_order:
            max_order = order
    return max_order


def episode_is_future_for_playback_search(
    episode: Episode,
    *,
    season_number: int,
    series_max_known_order_within_horizon: tuple[int, int] | None,
    now_date: date | None = None,
) -> bool:
    """True when a playback lookahead target should not be searched yet.

    Playback suppress treats "future" as not aired yet (air date after today), not the
    calendar lookahead window used for library-grid Future rows.
    """
    eff = now_date or datetime.now(timezone.utc).date()
    air_date = getattr(episode, "air_date", None)

    if air_date is not None:
        return air_date > eff

    lk = _lookahead_int()
    if not _placeholders_enabled() or lk < 0:
        return False

    order = (int(season_number or 0), int(episode.episode_number or 0))
    if (
        series_max_known_order_within_horizon is not None
        and series_max_known_order_within_horizon > order
    ):
        return False
    return True
