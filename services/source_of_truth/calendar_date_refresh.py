from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_

from core.config import settings
from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import Episode, Movie, Season, Series
from services.source_of_truth.arr_api import fetch_radarr_calendar, fetch_sonarr_calendar
from services.source_of_truth.sync_runner import _extract_date

_PAST_DAYS = 7
_FUTURE_BUFFER_DAYS = 7
_MAX_FUTURE_DAYS = 365


def _window_bounds(now_date: date, lookahead_days: int) -> tuple[date, date]:
    if lookahead_days < 0:
        future_days = _MAX_FUTURE_DAYS
    elif lookahead_days == 0:
        # Feature off for future placeholders: keep a small forward window for near-term date shifts.
        future_days = _FUTURE_BUFFER_DAYS
    else:
        future_days = int(lookahead_days) + _FUTURE_BUFFER_DAYS

    return (now_date - timedelta(days=_PAST_DAYS), now_date + timedelta(days=future_days))


def _iter_calendar_endpoints() -> list[tuple[str, str, str, bool]]:
    endpoints: list[tuple[str, str, str, bool]] = []

    if getattr(settings, "RADARR_URL", None) and getattr(settings, "RADARR_API_KEY", None):
        endpoints.append(("movie", settings.RADARR_URL, settings.RADARR_API_KEY, False))
    if getattr(settings, "RADARR_4K_URL", None) and getattr(settings, "RADARR_4K_API_KEY", None):
        endpoints.append(("movie", settings.RADARR_4K_URL, settings.RADARR_4K_API_KEY, True))

    if getattr(settings, "SONARR_URL", None) and getattr(settings, "SONARR_API_KEY", None):
        endpoints.append(("series", settings.SONARR_URL, settings.SONARR_API_KEY, False))
    if getattr(settings, "SONARR_4K_URL", None) and getattr(settings, "SONARR_4K_API_KEY", None):
        endpoints.append(("series", settings.SONARR_4K_URL, settings.SONARR_4K_API_KEY, True))

    return endpoints


def run_calendar_date_refresh() -> dict:
    """Refresh only release/air-date metadata from ARR calendar endpoints.

    This lightweight path keeps date fields current for calendar/status decisions
    without running a full content sync.
    """
    lookahead_days = int(getattr(settings, "CALENDAR_LOOKAHEAD_DAYS", 30) or 30)
    now_date = datetime.now(timezone.utc).date()
    start_date, end_date = _window_bounds(now_date, lookahead_days)

    stats = {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "movie_rows_seen": 0,
        "movie_rows_updated": 0,
        "episode_rows_seen": 0,
        "episode_rows_updated": 0,
        "errors": 0,
    }

    session = get_session()
    try:
        for content_type, base_url, api_key, is_4k in _iter_calendar_endpoints():
            if content_type == "movie":
                rows = fetch_radarr_calendar(start_date, end_date, base_url, api_key)
                for row in rows:
                    radarr_id = row.get("id")
                    tmdb_id = row.get("tmdbId")
                    if not radarr_id and not tmdb_id:
                        continue
                    stats["movie_rows_seen"] += 1

                    query = session.query(Movie).filter(Movie.is_4k == bool(is_4k))
                    if radarr_id:
                        movie = query.filter(Movie.radarrid == int(radarr_id)).first()
                    else:
                        movie = query.filter(Movie.tmdbid == int(tmdb_id)).first()
                    if not movie:
                        continue

                    new_theater = _extract_date(row.get("inCinemas"))
                    new_digital = _extract_date(row.get("digitalRelease"))
                    new_physical = _extract_date(row.get("physicalRelease"))

                    changed = False
                    if movie.theater_release_date != new_theater:
                        movie.theater_release_date = new_theater
                        changed = True
                    if movie.digital_release_date != new_digital:
                        movie.digital_release_date = new_digital
                        changed = True
                    if movie.physical_release_date != new_physical:
                        movie.physical_release_date = new_physical
                        changed = True
                    if changed:
                        movie.last_found_in_radarr = datetime.now(timezone.utc)
                        session.add(movie)
                        stats["movie_rows_updated"] += 1

            elif content_type == "series":
                rows = fetch_sonarr_calendar(start_date, end_date, base_url, api_key)
                for row in rows:
                    episode_sonarr_id = row.get("id")
                    if not episode_sonarr_id:
                        continue
                    stats["episode_rows_seen"] += 1

                    episode = (
                        session.query(Episode)
                        .join(Season, Episode.season_id == Season.id)
                        .join(Series, Season.series_id == Series.id)
                        .filter(and_(Episode.sonarrid == int(episode_sonarr_id), Series.is_4k == bool(is_4k)))
                        .first()
                    )
                    if not episode:
                        continue

                    new_air_date = _extract_date(row.get("airDate") or row.get("airDateUtc"))
                    if episode.air_date != new_air_date:
                        episode.air_date = new_air_date
                        episode.last_found_in_sonarr = datetime.now(timezone.utc)
                        session.add(episode)
                        stats["episode_rows_updated"] += 1

        session.commit()
        logger.info(f"Calendar date refresh complete: {stats}", extra={"emoji_type": "success"})
        return stats
    except Exception as exc:
        session.rollback()
        stats["errors"] += 1
        logger.error(f"Calendar date refresh failed: {exc}", extra={"emoji_type": "error"})
        raise
    finally:
        session.close()
