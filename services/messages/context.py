"""Build unified template context for placeholder status projection (NFO + players).

Merges media tokens from Movie / Episode rows with optional runtime overlay so
``line.request.movie`` / ``line.request.tv`` and related templates receive the
same fields everywhere.
"""

from __future__ import annotations

from typing import Any

from services.messages.template_engine import (
    media_tokens_for_episode,
    media_tokens_for_movie,
    media_tokens_for_season,
    media_tokens_for_series,
)

# Internal marker for REQUEST line key selection (not a user {Token}).
MEDIA_KIND_KEY = "_media_kind"


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
    """Merge media tokens with runtime for REQUEST / bracket rendering."""
    if movie is not None:
        base = media_tokens_for_movie(movie)
        base[MEDIA_KIND_KEY] = "movie"
    elif episode is not None:
        base = media_tokens_for_episode(episode, season, series)
        base[MEDIA_KIND_KEY] = "tv"
    elif season is not None:
        base = media_tokens_for_season(season, series)
        base[MEDIA_KIND_KEY] = "tv"
    elif series is not None:
        base = media_tokens_for_series(series)
        base[MEDIA_KIND_KEY] = "tv"
    else:
        base = {}
    return augment_context_with_runtime(base, runtime_minutes)


def _season_premiere_from_session(session: Any, season_id: int | None) -> str:
    """Earliest episode air date for a season (YYYY-MM-DD), or empty."""
    if session is None or season_id is None:
        return ""
    try:
        from sqlalchemy import func

        from services.messages.template_engine import _token_date
        from services.postgres.models import Episode

        raw = (
            session.query(func.min(Episode.air_date))
            .filter(Episode.season_id == int(season_id))
            .scalar()
        )
        return _token_date(raw)
    except Exception:
        return ""


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
        ctx = build_projection_context(episode=ep, season=season, series=series, runtime_minutes=rm)
        if season is not None and not str(ctx.get("SeasonPremiereDate") or "").strip():
            ctx["SeasonPremiereDate"] = _season_premiere_from_session(session, getattr(season, "id", None))
        return ctx
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
            "SeriesPremiereDate": "2008-01-20",
            "SeasonPremiereDate": "2008-01-20",
            "AirDate": "2008-01-27",
            MEDIA_KIND_KEY: "tv",
        }
    else:
        base = {
            "Title": "Inception",
            "Year": "2010",
            "ReleaseYear": "2010",
            "Genres": "Action, Sci-Fi",
            "Certification": "PG-13",
            "Studio": "Warner Bros.",
            "TheatricalDate": "2010-07-16",
            "DigitalDate": "2010-12-07",
            "PhysicalDate": "2010-12-07",
            MEDIA_KIND_KEY: "movie",
        }
    return augment_context_with_runtime(base, runtime_minutes)
