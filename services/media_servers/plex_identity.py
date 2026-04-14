from __future__ import annotations

import re

from sqlalchemy import func

from services.postgres.models import Episode, Movie, Season, Series


def _pure_summary(summary: str | None) -> str:
    text = str(summary or "")
    return re.sub(r"^\[.*?\]\s*", "", text).strip()


def persist_movie_plex_identity(session, movie: Movie, plex_item) -> None:
    """Persist Plex identity fields onto a Movie content row.

    Writes ``plex_id`` and ``plex_dummy_id`` to the same ``ratingKey`` (mirrored),
    plus ``plex_title`` and ``plex_overview`` from the resolved Plex item.
    No-ops silently when ratingKey is absent or unchanged.
    """
    rating_key = str(getattr(plex_item, "ratingKey", "") or "")
    if not rating_key:
        return

    changed = False
    if movie.plex_dummy_id != rating_key or movie.plex_id != rating_key:
        movie.plex_dummy_id = rating_key
        movie.plex_id = rating_key
        changed = True

    title = str(getattr(plex_item, "title", "") or "")
    if title and movie.plex_title != title:
        movie.plex_title = title
        changed = True

    summary = _pure_summary(getattr(plex_item, "summary", ""))
    if summary and movie.plex_overview != summary:
        movie.plex_overview = summary
        changed = True

    if changed:
        movie.updated_at = func.now()
        session.add(movie)


def persist_series_plex_identity(session, series: Series, plex_show) -> None:
    """Persist mirrored Plex rating keys and titles from a resolved library Show."""
    rating_key = str(getattr(plex_show, "ratingKey", "") or "")
    if not rating_key:
        return

    changed = False
    if series.plex_dummy_id != rating_key or series.plex_id != rating_key:
        series.plex_dummy_id = rating_key
        series.plex_id = rating_key
        changed = True

    title = str(getattr(plex_show, "title", "") or "")
    if title and series.plex_title != title:
        series.plex_title = title
        changed = True

    summary = _pure_summary(getattr(plex_show, "summary", ""))
    if summary and series.plex_overview != summary:
        series.plex_overview = summary
        changed = True

    if changed:
        series.updated_at = func.now()
        session.add(series)


def persist_episode_hierarchy_plex_identity(
    session,
    series: Series,
    season: Season,
    episode: Episode,
    plex_episode,
) -> None:
    """Persist mirrored Plex rating keys for show/season/episode from one episode item.

    ``plex_id`` and ``plex_dummy_id`` are set to the same value at each level.
    Derives parent keys from ``grandparentRatingKey`` (show) and
    ``parentRatingKey`` (season) on the Plex episode object.
    """
    # ── Series ────────────────────────────────────────────────────────────────
    show_changed = False
    show_key = str(getattr(plex_episode, "grandparentRatingKey", "") or "")
    if show_key and (series.plex_dummy_id != show_key or series.plex_id != show_key):
        series.plex_dummy_id = show_key
        series.plex_id = show_key
        show_changed = True
    show_title = str(getattr(plex_episode, "grandparentTitle", "") or "")
    if show_title and series.plex_title != show_title:
        series.plex_title = show_title
        show_changed = True
    show_summary = _pure_summary(getattr(plex_episode, "grandparentSummary", ""))
    if show_summary and series.plex_overview != show_summary:
        series.plex_overview = show_summary
        show_changed = True
    if show_changed:
        series.updated_at = func.now()
        session.add(series)

    # ── Season ────────────────────────────────────────────────────────────────
    season_changed = False
    season_key = str(getattr(plex_episode, "parentRatingKey", "") or "")
    if season_key and (season.plex_dummy_id != season_key or season.plex_id != season_key):
        season.plex_dummy_id = season_key
        season.plex_id = season_key
        season_changed = True
    season_title = str(getattr(plex_episode, "parentTitle", "") or "")
    if season_title and season.plex_title != season_title:
        season.plex_title = season_title
        season_changed = True
    season_summary = _pure_summary(getattr(plex_episode, "parentSummary", ""))
    if season_summary and season.plex_overview != season_summary:
        season.plex_overview = season_summary
        season_changed = True
    if season_changed:
        season.updated_at = func.now()
        session.add(season)

    # ── Episode ───────────────────────────────────────────────────────────────
    episode_changed = False
    ep_key = str(getattr(plex_episode, "ratingKey", "") or "")
    if ep_key and (episode.plex_dummy_id != ep_key or episode.plex_id != ep_key):
        episode.plex_dummy_id = ep_key
        episode.plex_id = ep_key
        episode_changed = True
    ep_title = str(getattr(plex_episode, "title", "") or "")
    if ep_title and episode.plex_title != ep_title:
        episode.plex_title = ep_title
        episode_changed = True
    ep_summary = _pure_summary(getattr(plex_episode, "summary", ""))
    if ep_summary and episode.plex_overview != ep_summary:
        episode.plex_overview = ep_summary
        episode_changed = True
    if episode_changed:
        episode.updated_at = func.now()
        session.add(episode)
