"""Build unified template context for placeholder status projection (NFO + players).

Merges media tokens from Movie / Episode rows with optional runtime overlay so
``line.request`` and related templates receive the same fields everywhere.
"""

from __future__ import annotations

from typing import Any

from services.messages.template_engine import (
    media_tokens_for_episode,
    media_tokens_for_movie,
    media_tokens_for_season,
    media_tokens_for_series,
)


def _rounded_minutes(value: int | float | str | None) -> int:
    if value is None or value == "":
        return 0
    try:
        return max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return 0


def _format_duration_label(minutes: int) -> str:
    """Match ``format_duration_label`` in status_projection without importing it (avoid cycles)."""
    if minutes <= 0:
        return ""
    from services.messages import render

    if minutes < 60:
        return render("runtime.format.m", {"Minutes": str(minutes)})
    h, m = divmod(minutes, 60)
    if m == 0:
        return render("runtime.format.h", {"Hours": str(h)})
    return render("runtime.format.hm", {"Hours": str(h), "Minutes": str(m)})


def augment_context_with_runtime(
    base: dict[str, Any] | None,
    runtime_minutes: int | None,
) -> dict[str, Any]:
    """Overlay formatted Runtime / Hours / Minutes / RuntimeMinutes from wall-clock minutes."""
    out = dict(base or {})
    if runtime_minutes is None:
        return out
    rm = _rounded_minutes(runtime_minutes)
    if rm <= 0:
        return out
    dur = _format_duration_label(rm)
    if dur:
        out["Runtime"] = dur
    h, m = divmod(rm, 60)
    out["Hours"] = str(h) if h else ""
    out["Minutes"] = str(m) if m else ""
    out["RuntimeMinutes"] = str(rm)
    return out


def build_projection_context(
    *,
    movie: Any = None,
    episode: Any = None,
    season: Any = None,
    series: Any = None,
    runtime_minutes: int | None = None,
) -> dict[str, Any]:
    """Merge media tokens with runtime for ``line.request`` / bracket rendering."""
    if movie is not None:
        base = media_tokens_for_movie(movie)
    elif episode is not None:
        base = media_tokens_for_episode(episode, season, series)
    elif season is not None:
        base = media_tokens_for_season(season, series)
    elif series is not None:
        base = media_tokens_for_series(series)
    else:
        base = {}
    return augment_context_with_runtime(base, runtime_minutes)


def build_projection_context_from_session(
    session: Any,
    *,
    movie_id: int | None = None,
    episode_id: int | None = None,
    runtime_minutes: int | None = None,
) -> dict[str, Any]:
    """Load ORM rows and build projection context (used from materializer / orchestrator)."""
    from services.postgres.models import Episode, Movie, Season, Series

    if movie_id is not None:
        movie = session.query(Movie).filter(Movie.id == int(movie_id)).first()
        if movie:
            rm = runtime_minutes
            if rm is None:
                rm = _runtime_minutes_from_movie(movie)
            return build_projection_context(movie=movie, runtime_minutes=rm)
        return {}
    if episode_id is not None:
        ep = session.query(Episode).filter(Episode.id == int(episode_id)).first()
        if not ep:
            return {}
        season = session.query(Season).filter(Season.id == ep.season_id).first() if ep.season_id else None
        series = session.query(Series).filter(Series.id == season.series_id).first() if season else None
        rm = runtime_minutes
        if rm is None:
            rm = _runtime_minutes_from_episode(ep, series)
        return build_projection_context(episode=ep, season=season, series=series, runtime_minutes=rm)
    return {}


def _runtime_minutes_from_movie(movie: Any) -> int | None:
    try:
        r = int(getattr(movie, "radarr_runtime", 0) or 0)
        return r if r > 0 else None
    except Exception:
        return None


def _runtime_minutes_from_episode(episode: Any, series: Any) -> int | None:
    try:
        r = int(getattr(episode, "sonarr_runtime", 0) or 0)
        if r > 0:
            return r
    except Exception:
        pass
    if series is None:
        return None
    try:
        r = int(getattr(series, "sonarr_runtime", 0) or 0)
        return r if r > 0 else None
    except Exception:
        return None


def sample_projection_context(media: str, *, runtime_minutes: int = 121) -> dict[str, Any]:
    """Fixed demo context for Settings previews (`movie` vs `episode`)."""
    m = str(media or "movie").strip().lower()
    if m in ("episode", "tv", "show"):
        base: dict[str, Any] = {
            "Title": "Breaking Bad",
            "SeriesTitle": "Breaking Bad",
            "EpisodeTitle": "Cat's in the Bag...",
            "Year": "2008",
            "ReleaseYear": "2008",
            "Genres": "Crime, Drama",
            "Certification": "TV-MA",
            "Studio": "AMC",
            "SeasonNumber": "01",
            "EpisodeNumber": "02",
            "SXXEYY": "S01E02",
        }
    else:
        base = {
            "Title": "Inception",
            "Year": "2010",
            "ReleaseYear": "2010",
            "Genres": "Action, Sci-Fi",
            "Certification": "PG-13",
            "Studio": "Warner Bros.",
        }
    return augment_context_with_runtime(base, runtime_minutes)
