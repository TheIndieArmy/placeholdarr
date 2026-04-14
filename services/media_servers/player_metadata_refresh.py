"""Targeted direct status projection on Plex/Jellyfin/Emby after NFO updates.

Uses cached server item ids when present, otherwise resolves items by TMDB (movies)
or TVDB + season/episode (TV), matching how libraries are keyed in practice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.config import settings
from core.logger import logger
from services.media_servers.emby import emby_search_items, update_emby_item_text
from services.media_servers.jellyfin import jellyfin_search_items, update_jellyfin_item_text
from services.media_servers.plex import PlexMetadataRefreshResult, update_plex_item_text
from services.media_servers.refresh import refresh_selected_sections
from services.media_servers.plex_identity import (
    persist_episode_hierarchy_plex_identity,
    persist_movie_plex_identity,
    persist_series_plex_identity,
)
from services.media_servers.plex_lookup import find_episode_by_series_tvdb, find_movie_by_id, find_show_by_id
from services.postgres.models import Episode, Movie, Placeholder, Season, Series
from services.status_projection import project_summary, project_title


def _plex_coalesce_cached_rating_key(entity: object) -> str:
    """Return the first non-empty Plex ``ratingKey`` stored on a model row.

    ``plex_id`` and ``plex_dummy_id`` are kept in sync when we persist from Plex;
    both are checked for older rows written by legacy paths.
    """
    for attr in ("plex_id", "plex_dummy_id"):
        raw = str(getattr(entity, attr, "") or "").strip()
        if raw:
            return raw
    return ""


def _first_item_id(items: list[dict[str, Any]]) -> str | None:
    for it in items:
        if not isinstance(it, dict):
            continue
        iid = str(it.get("Id") or "").strip()
        if iid:
            return iid
    return None


def _find_jellyfin_movie_item_id(movie: Movie) -> str | None:
    cached = str(getattr(movie, "jellyfin_id", "") or "").strip()
    if cached:
        return cached
    tmdb = int(getattr(movie, "tmdbid", 0) or 0)
    if not tmdb:
        return None
    for key in (
        f"Tmdb-{tmdb}",
        f"MovieDb-{tmdb}",
        f"Tmdb.{tmdb}",
        f"tmdb.{tmdb}",
        f"MovieDb.{tmdb}",
    ):
        items = jellyfin_search_items(
            {
                "Recursive": "true",
                "IncludeItemTypes": "Movie",
                "AnyProviderIdEquals": key,
                "Limit": 5,
            }
        )
        hit = _first_item_id(items)
        if hit:
            return hit
    return None


def _find_emby_movie_item_id(movie: Movie, placeholder: Placeholder) -> str | None:
    cached_ph = str(getattr(placeholder, "emby_placeholder_id", "") or "").strip()
    if cached_ph:
        return cached_ph
    tmdb = int(getattr(movie, "tmdbid", 0) or 0)
    if not tmdb:
        return None
    for key in (
        f"Tmdb-{tmdb}",
        f"MovieDb-{tmdb}",
        f"Tmdb.{tmdb}",
        f"tmdb.{tmdb}",
        f"MovieDb.{tmdb}",
    ):
        items = emby_search_items(
            {
                "Recursive": "true",
                "IncludeItemTypes": "Movie",
                "AnyProviderIdEquals": key,
                "Limit": 5,
            }
        )
        hit = _first_item_id(items)
        if hit:
            return hit
    return None


def _find_jellyfin_series_item_id(series: Series) -> str | None:
    cached = str(getattr(series, "jellyfin_id", "") or "").strip()
    if cached:
        return cached
    tvdb = int(getattr(series, "tvdbid", 0) or 0)
    if not tvdb:
        return None
    for key in (f"Tvdb-{tvdb}", f"tvdb-{tvdb}", f"Tvdb.{tvdb}", f"tvdb.{tvdb}"):
        items = jellyfin_search_items(
            {
                "Recursive": "true",
                "IncludeItemTypes": "Series",
                "AnyProviderIdEquals": key,
                "Limit": 5,
            }
        )
        hit = _first_item_id(items)
        if hit:
            return hit
    return None


def _find_emby_series_item_id(series: Series) -> str | None:
    # No persisted Emby series id on Series rows; resolve via TVDB like path-based flows.
    tvdb = int(getattr(series, "tvdbid", 0) or 0)
    if not tvdb:
        return None
    for key in (f"Tvdb-{tvdb}", f"tvdb-{tvdb}", f"Tvdb.{tvdb}", f"tvdb.{tvdb}"):
        items = emby_search_items(
            {
                "Recursive": "true",
                "IncludeItemTypes": "Series",
                "AnyProviderIdEquals": key,
                "Limit": 5,
            }
        )
        hit = _first_item_id(items)
        if hit:
            return hit
    return None


def _find_jellyfin_episode_item_id(series: Series, season: Season, episode: Episode) -> str | None:
    cached = str(getattr(episode, "jellyfin_id", "") or "").strip()
    if cached:
        return cached
    etvdb = getattr(episode, "sonarr_episode_tvdbid", None)
    if etvdb is not None:
        try:
            eid = int(etvdb)
            for key in (f"Tvdb-{eid}", f"tvdb-{eid}", f"Tvdb.{eid}", f"tvdb.{eid}"):
                items = jellyfin_search_items(
                    {
                        "Recursive": "true",
                        "IncludeItemTypes": "Episode",
                        "AnyProviderIdEquals": key,
                        "Limit": 5,
                    }
                )
                hit = _first_item_id(items)
                if hit:
                    return hit
        except (TypeError, ValueError):
            pass

    parent_series_id = _find_jellyfin_series_item_id(series)
    if not parent_series_id:
        return None
    items = jellyfin_search_items(
        {
            "ParentId": parent_series_id,
            "IncludeItemTypes": "Episode",
            "ParentIndexNumber": int(season.season_number),
            "IndexNumber": int(episode.episode_number),
            "Limit": 10,
        }
    )
    return _first_item_id(items)


def _find_emby_episode_item_id(series: Series, season: Season, episode: Episode, placeholder: Placeholder) -> str | None:
    cached_ph = str(getattr(placeholder, "emby_placeholder_id", "") or "").strip()
    if cached_ph:
        return cached_ph
    etvdb = getattr(episode, "sonarr_episode_tvdbid", None)
    if etvdb is not None:
        try:
            eid = int(etvdb)
            for key in (f"Tvdb-{eid}", f"tvdb-{eid}", f"Tvdb.{eid}", f"tvdb.{eid}"):
                items = emby_search_items(
                    {
                        "Recursive": "true",
                        "IncludeItemTypes": "Episode",
                        "AnyProviderIdEquals": key,
                        "Limit": 5,
                    }
                )
                hit = _first_item_id(items)
                if hit:
                    return hit
        except (TypeError, ValueError):
            pass

    parent_series_id = _find_emby_series_item_id(series)
    if not parent_series_id:
        return None
    items = emby_search_items(
        {
            "ParentId": parent_series_id,
            "IncludeItemTypes": "Episode",
            "ParentIndexNumber": int(season.season_number),
            "IndexNumber": int(episode.episode_number),
            "Limit": 10,
        }
    )
    return _first_item_id(items)


def _projected_display_status(placeholder: Placeholder) -> str:
    status = str(getattr(placeholder, "display_status", "") or "").strip().upper()
    reason = str(getattr(placeholder, "display_reason", "") or "").strip()
    if status in {"COMING_SOON", "COMING_SOON_30", "COMING_SOON_14", "COMING_SOON_7", "COMING_SOON_1", "COMING_SOON_TODAY"} and reason:
        return reason
    if status == "DOWNLOADING" and reason:
        return reason
    if status == "SEARCHING" and reason and reason.lower() == "queued":
        return reason
    return status or "REQUEST"


def _project_text(base_title: str | None, base_summary: str | None, status: str) -> tuple[str, str]:
    return project_title(base_title or "", status), project_summary(base_summary or "", status)


@dataclass
class ProjectionFallbackAccumulator:
    plex_movie: bool = False
    plex_episode: bool = False
    jellyfin_movie: bool = False
    jellyfin_episode: bool = False
    emby_movie: bool = False
    emby_episode: bool = False

    def has_failures(self) -> bool:
        return any(
            (
                self.plex_movie,
                self.plex_episode,
                self.jellyfin_movie,
                self.jellyfin_episode,
                self.emby_movie,
                self.emby_episode,
            )
        )


def _mark_fallback(acc: ProjectionFallbackAccumulator, server: str, media_type: str) -> None:
    if server == "plex":
        if media_type == "movie":
            acc.plex_movie = True
        else:
            acc.plex_episode = True
        return
    if server == "jellyfin":
        if media_type == "movie":
            acc.jellyfin_movie = True
        else:
            acc.jellyfin_episode = True
        return
    if server == "emby":
        if media_type == "movie":
            acc.emby_movie = True
        else:
            acc.emby_episode = True


def _push_movie(session, movie: Movie, placeholder: Placeholder, fallback: ProjectionFallbackAccumulator) -> None:
    status = _projected_display_status(placeholder)
    projected_title, projected_summary = _project_text(
        getattr(movie, "title", None),
        getattr(movie, "radarr_overview", None),
        status,
    )

    if getattr(settings, "jellyfin_enabled", False):
        jf = _find_jellyfin_movie_item_id(movie)
        if jf:
            if not update_jellyfin_item_text(jf, title=projected_title, overview=projected_summary):
                logger.debug(
                    f"Jellyfin direct projection failed item_id={jf}",
                    extra={"emoji_type": "debug"},
                )
                _mark_fallback(fallback, "jellyfin", "movie")
        else:
            logger.debug(
                f"Jellyfin movie item not resolved for tmdbid={getattr(movie, 'tmdbid', None)}",
                extra={"emoji_type": "debug"},
            )
            _mark_fallback(fallback, "jellyfin", "movie")

    if getattr(settings, "emby_enabled", False):
        em = _find_emby_movie_item_id(movie, placeholder)
        if em:
            if not update_emby_item_text(em, title=projected_title, overview=projected_summary):
                logger.debug(
                    f"Emby direct projection failed item_id={em}",
                    extra={"emoji_type": "debug"},
                )
                _mark_fallback(fallback, "emby", "movie")
        else:
            logger.debug(
                f"Emby movie item not resolved for tmdbid={getattr(movie, 'tmdbid', None)}",
                extra={"emoji_type": "debug"},
            )
            _mark_fallback(fallback, "emby", "movie")

    if getattr(settings, "plex_enabled", False):
        plex_key = _plex_coalesce_cached_rating_key(movie)
        if plex_key:
            outcome: PlexMetadataRefreshResult = update_plex_item_text(
                plex_key,
                title=projected_title,
                summary=projected_summary,
            )
            # Drop cached keys on 404 or failures so the next block re-resolves by TMDB.
            if outcome in ("not_found", "failed"):
                movie.plex_id = None
                movie.plex_dummy_id = None
                session.add(movie)
                session.flush()
            elif outcome != "ok":
                _mark_fallback(fallback, "plex", "movie")
                return
        if not _plex_coalesce_cached_rating_key(movie):
            plex_movie = find_movie_by_id(
                getattr(movie, "tmdbid", None),
                title=getattr(movie, "title", None),
                year=getattr(movie, "year", None),
            )
            if plex_movie is not None and getattr(plex_movie, "ratingKey", None) is not None:
                persist_movie_plex_identity(session, movie, plex_movie)
                session.flush()
                update_plex_item_text(
                    plex_movie.ratingKey,
                    title=projected_title,
                    summary=projected_summary,
                )
            else:
                logger.debug(
                    f"Plex movie not resolved for tmdbid={getattr(movie, 'tmdbid', None)}",
                    extra={"emoji_type": "debug"},
                )
                _mark_fallback(fallback, "plex", "movie")


def _push_episode(
    session,
    placeholder: Placeholder,
    episode: Episode,
    fallback: ProjectionFallbackAccumulator,
) -> None:
    season = session.query(Season).get(episode.season_id) if episode.season_id else None
    series = session.query(Series).get(season.series_id) if season and season.series_id else None
    if not season or not series:
        return

    status = _projected_display_status(placeholder)
    projected_ep_title, projected_ep_summary = _project_text(
        getattr(episode, "title", None),
        getattr(episode, "sonarr_episode_overview", None),
        status,
    )
    projected_series_title, projected_series_summary = _project_text(
        getattr(series, "title", None),
        getattr(series, "sonarr_series_overview", None),
        status,
    )

    if getattr(settings, "jellyfin_enabled", False):
        jf_ep = _find_jellyfin_episode_item_id(series, season, episode)
        jf_series = _find_jellyfin_series_item_id(series)
        if jf_series:
            if not update_jellyfin_item_text(jf_series, title=projected_series_title, overview=projected_series_summary):
                _mark_fallback(fallback, "jellyfin", "episode")
        if jf_ep:
            if not update_jellyfin_item_text(jf_ep, title=projected_ep_title, overview=projected_ep_summary):
                _mark_fallback(fallback, "jellyfin", "episode")
        if not jf_ep and not jf_series:
            logger.debug(
                "Jellyfin TV item not resolved "
                f"tvdbid={getattr(series, 'tvdbid', None)} "
                f"S{season.season_number}E{episode.episode_number}",
                extra={"emoji_type": "debug"},
            )
            _mark_fallback(fallback, "jellyfin", "episode")

    if getattr(settings, "emby_enabled", False):
        em_ep = _find_emby_episode_item_id(series, season, episode, placeholder)
        em_series = _find_emby_series_item_id(series)
        if em_series:
            if not update_emby_item_text(em_series, title=projected_series_title, overview=projected_series_summary):
                _mark_fallback(fallback, "emby", "episode")
        if em_ep:
            if not update_emby_item_text(em_ep, title=projected_ep_title, overview=projected_ep_summary):
                _mark_fallback(fallback, "emby", "episode")
        if not em_ep and not em_series:
            logger.debug(
                "Emby TV item not resolved "
                f"tvdbid={getattr(series, 'tvdbid', None)} "
                f"S{season.season_number}E{episode.episode_number}",
                extra={"emoji_type": "debug"},
            )
            _mark_fallback(fallback, "emby", "episode")

    if getattr(settings, "plex_enabled", False):
        ep_key = _plex_coalesce_cached_rating_key(episode)
        if ep_key:
            ep_out = update_plex_item_text(ep_key, title=projected_ep_title, summary=projected_ep_summary)
            if ep_out in ("not_found", "failed"):
                episode.plex_id = None
                episode.plex_dummy_id = None
                session.add(episode)
                session.flush()
                plex_ep = find_episode_by_series_tvdb(
                    getattr(series, "tvdbid", None),
                    int(season.season_number),
                    int(episode.episode_number),
                    series_title=getattr(series, "title", None),
                )
                if plex_ep is not None and getattr(plex_ep, "ratingKey", None) is not None:
                    persist_episode_hierarchy_plex_identity(session, series, season, episode, plex_ep)
                    session.flush()
                    update_plex_item_text(
                        plex_ep.ratingKey,
                        title=projected_ep_title,
                        summary=projected_ep_summary,
                    )
                else:
                    _mark_fallback(fallback, "plex", "episode")
        if not _plex_coalesce_cached_rating_key(episode):
            plex_ep = find_episode_by_series_tvdb(
                getattr(series, "tvdbid", None),
                int(season.season_number),
                int(episode.episode_number),
                series_title=getattr(series, "title", None),
            )
            if plex_ep is not None and getattr(plex_ep, "ratingKey", None) is not None:
                persist_episode_hierarchy_plex_identity(session, series, season, episode, plex_ep)
                session.flush()
                update_plex_item_text(
                    plex_ep.ratingKey,
                    title=projected_ep_title,
                    summary=projected_ep_summary,
                )
            else:
                logger.debug(
                    "Plex episode not resolved "
                    f"tvdbid={getattr(series, 'tvdbid', None)} "
                    f"S{season.season_number}E{episode.episode_number}",
                    extra={"emoji_type": "debug"},
                )
                _mark_fallback(fallback, "plex", "episode")

        ser_key = _plex_coalesce_cached_rating_key(series)
        if ser_key:
            ser_out = update_plex_item_text(
                ser_key,
                title=projected_series_title,
                summary=projected_series_summary,
            )
            if ser_out in ("not_found", "failed"):
                series.plex_id = None
                series.plex_dummy_id = None
                session.add(series)
                session.flush()
                show = find_show_by_id(getattr(series, "tvdbid", None), title=getattr(series, "title", None))
                if show is not None and getattr(show, "ratingKey", None) is not None:
                    persist_series_plex_identity(session, series, show)
                    session.flush()
                    update_plex_item_text(
                        show.ratingKey,
                        title=projected_series_title,
                        summary=projected_series_summary,
                    )
                else:
                    _mark_fallback(fallback, "plex", "episode")
        else:
            show = find_show_by_id(getattr(series, "tvdbid", None), title=getattr(series, "title", None))
            if show is not None and getattr(show, "ratingKey", None) is not None:
                persist_series_plex_identity(session, series, show)
                session.flush()
                update_plex_item_text(
                    show.ratingKey,
                    title=projected_series_title,
                    summary=projected_series_summary,
                )
            else:
                _mark_fallback(fallback, "plex", "episode")


def _run_projection_fallback_refreshes(fallback: ProjectionFallbackAccumulator) -> None:
    """Run one fallback refresh per server/media-type bucket for this batch."""
    if not fallback.has_failures():
        return

    if fallback.plex_movie:
        refresh_selected_sections(
            has_movies=True,
            has_episodes=False,
            include_plex=True,
            include_jellyfin=False,
            include_emby=False,
        )
    if fallback.plex_episode:
        refresh_selected_sections(
            has_movies=False,
            has_episodes=True,
            include_plex=True,
            include_jellyfin=False,
            include_emby=False,
        )
    if fallback.jellyfin_movie or fallback.jellyfin_episode:
        refresh_selected_sections(
            has_movies=bool(fallback.jellyfin_movie),
            has_episodes=bool(fallback.jellyfin_episode),
            include_plex=False,
            include_jellyfin=True,
            include_emby=False,
        )
    if fallback.emby_movie or fallback.emby_episode:
        refresh_selected_sections(
            has_movies=bool(fallback.emby_movie),
            has_episodes=bool(fallback.emby_episode),
            include_plex=False,
            include_jellyfin=False,
            include_emby=True,
        )

    logger.info(
        "Status projection fallback section refreshes triggered "
        f"plex(movie={fallback.plex_movie},episode={fallback.plex_episode}) "
        f"jellyfin(movie={fallback.jellyfin_movie},episode={fallback.jellyfin_episode}) "
        f"emby(movie={fallback.emby_movie},episode={fallback.emby_episode})",
        extra={"emoji_type": "info"},
    )


def push_placeholder_player_metadata(
    session,
    placeholder: Placeholder,
    *,
    fallback: ProjectionFallbackAccumulator | None = None,
) -> ProjectionFallbackAccumulator:
    """Best-effort direct status projection to player title/summary fields."""
    acc = fallback or ProjectionFallbackAccumulator()
    if getattr(settings, "REFRESH_TRIGGER_SUPPRESSED", False):
        logger.debug(
            "Skipping player metadata refresh (REFRESH_TRIGGER_SUPPRESSED)",
            extra={"emoji_type": "debug"},
        )
        return acc

    movie = session.query(Movie).get(placeholder.movie_id) if placeholder.movie_id else None
    episode = session.query(Episode).get(placeholder.episode_id) if placeholder.episode_id else None

    if movie:
        _push_movie(session, movie, placeholder, acc)
    elif episode:
        _push_episode(session, placeholder, episode, acc)
    return acc


def push_placeholder_batch_player_metadata(session, placeholders: list[Placeholder]) -> None:
    """Project status directly for a placeholder batch and fallback-refresh once."""
    fallback = ProjectionFallbackAccumulator()
    for placeholder in placeholders:
        push_placeholder_player_metadata(session, placeholder, fallback=fallback)
    _run_projection_fallback_refreshes(fallback)
