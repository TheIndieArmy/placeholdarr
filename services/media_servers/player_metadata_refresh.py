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
from services.media_servers.jellyfin import (
    jellyfin_get_item_fields,
    jellyfin_search_items,
    update_jellyfin_item_text,
)
from services.media_servers.plex import PlexMetadataRefreshResult, update_plex_item_text
from services.media_servers.refresh import refresh_selected_sections
from services.media_servers.plex_identity import (
    persist_episode_hierarchy_plex_identity,
    persist_movie_plex_identity,
)
from services.media_servers.plex_lookup import find_episode_by_series_tvdb, find_movie_by_id
from services.messages.context import build_projection_context
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


def _provider_values(item: dict[str, Any], *keys: str) -> set[str]:
    provider_ids = item.get("ProviderIds") if isinstance(item, dict) else None
    if not isinstance(provider_ids, dict):
        return set()
    out: set[str] = set()
    for key in keys:
        val = str(provider_ids.get(key) or "").strip().lower()
        if val:
            out.add(val)
    return out


def _movie_provider_match(item: dict[str, Any], movie: Movie) -> bool:
    want_tmdb = str(int(getattr(movie, "tmdbid", 0) or 0)).strip().lower()
    want_imdb = str(getattr(movie, "imdbid", "") or "").strip().lower()
    tmdb_vals = _provider_values(item, "Tmdb", "TmdbId", "MovieDb")
    imdb_vals = _provider_values(item, "Imdb", "ImdbId")
    return bool((want_tmdb and want_tmdb in tmdb_vals) or (want_imdb and want_imdb in imdb_vals))


def _series_provider_match(item: dict[str, Any], series: Series) -> bool:
    want_tvdb = str(int(getattr(series, "tvdbid", 0) or 0)).strip().lower()
    if not want_tvdb:
        return False
    tvdb_vals = _provider_values(item, "Tvdb", "TvdbId")
    return want_tvdb in tvdb_vals


def _episode_provider_match(item: dict[str, Any], episode: Episode) -> bool:
    want_tvdb_raw = getattr(episode, "sonarr_episode_tvdbid", None)
    if want_tvdb_raw is None:
        return False
    try:
        want_tvdb = str(int(want_tvdb_raw)).strip().lower()
    except (TypeError, ValueError):
        return False
    if not want_tvdb:
        return False
    tvdb_vals = _provider_values(item, "Tvdb", "TvdbId")
    return want_tvdb in tvdb_vals


def _pick_jellyfin_movie_item_id(movie: Movie, items: list[dict[str, Any]]) -> str | None:
    """Prefer exact provider-id match only (fail-safe)."""
    if not isinstance(items, list) or not items:
        return None
    for it in items:
        if not isinstance(it, dict):
            continue
        if _movie_provider_match(it, movie):
            iid = str(it.get("Id") or "").strip()
            if iid:
                return iid
    return None


def _pick_jellyfin_series_item_id(series: Series, items: list[dict[str, Any]]) -> str | None:
    if not isinstance(items, list) or not items:
        return None
    for it in items:
        if not isinstance(it, dict):
            continue
        if _series_provider_match(it, series):
            iid = str(it.get("Id") or "").strip()
            if iid:
                return iid
    return None


def _find_jellyfin_movie_item_id(movie: Movie) -> str | None:
    cached = str(getattr(movie, "jellyfin_id", "") or "").strip()
    if cached:
        cached_item = jellyfin_get_item_fields(cached, fields="ProviderIds,Name,ProductionYear")
        if isinstance(cached_item, dict) and _movie_provider_match(cached_item, movie):
            return cached
        logger.debug(
            f"Jellyfin cached movie id mismatch movie_id={getattr(movie, 'id', None)} cached_item_id={cached}; re-resolving",
            extra={"emoji_type": "debug"},
        )
    tmdb = int(getattr(movie, "tmdbid", 0) or 0)
    imdb = str(getattr(movie, "imdbid", "") or "").strip().lower()
    title = str(getattr(movie, "title", "") or "").strip()
    year = int(getattr(movie, "year", 0) or 0)
    if not tmdb and not imdb:
        return None
    # Jellyfin does not reliably support provider-id equality filters at query time.
    # Use documented GetItems filters, then perform exact ProviderIds match client-side.
    search_terms: list[str] = []
    if title:
        search_terms.append(title)
        if ":" in title:
            search_terms.append(title.split(":", 1)[0].strip())
    search_terms.append("")

    seen_ids: set[str] = set()
    for term in search_terms:
        query: dict[str, Any] = {
            "recursive": "true",
            "includeItemTypes": "Movie",
            "fields": "ProviderIds,Name,ProductionYear,Path",
            "limit": 100,
            "hasTmdbId": "true",
        }
        if year:
            query["years"] = str(year)
        if term:
            query["searchTerm"] = term
        items = jellyfin_search_items(query)
        deduped: list[dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            iid = str(it.get("Id") or "").strip()
            if iid and iid in seen_ids:
                continue
            if iid:
                seen_ids.add(iid)
            deduped.append(it)
        hit = _pick_jellyfin_movie_item_id(movie, deduped)
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
        cached_item = jellyfin_get_item_fields(cached, fields="ProviderIds,Name")
        if isinstance(cached_item, dict) and _series_provider_match(cached_item, series):
            return cached
        logger.debug(
            f"Jellyfin cached series id mismatch series_id={getattr(series, 'id', None)} cached_item_id={cached}; re-resolving",
            extra={"emoji_type": "debug"},
        )
    tvdb = int(getattr(series, "tvdbid", 0) or 0)
    title = str(getattr(series, "title", "") or "").strip()
    year = int(getattr(series, "year", 0) or 0)
    if not tvdb:
        return None
    search_terms: list[str] = []
    if title:
        search_terms.append(title)
    search_terms.append("")
    seen_ids: set[str] = set()
    for term in search_terms:
        query: dict[str, Any] = {
            "recursive": "true",
            "includeItemTypes": "Series",
            "fields": "ProviderIds,Name,ProductionYear,Path",
            "limit": 100,
            "hasTvdbId": "true",
        }
        if year:
            query["years"] = str(year)
        if term:
            query["searchTerm"] = term
        items = jellyfin_search_items(query)
        deduped: list[dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            iid = str(it.get("Id") or "").strip()
            if iid and iid in seen_ids:
                continue
            if iid:
                seen_ids.add(iid)
            deduped.append(it)
        hit = _pick_jellyfin_series_item_id(series, deduped)
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
        cached_item = jellyfin_get_item_fields(
            cached,
            fields="ProviderIds,SeriesId,ParentIndexNumber,IndexNumber,Name,Path",
        )
        if isinstance(cached_item, dict):
            if _episode_provider_match(cached_item, episode):
                return cached
            cached_series = str(cached_item.get("SeriesId") or "").strip()
            season_ok = int(cached_item.get("ParentIndexNumber") or 0) == int(getattr(season, "season_number", 0) or 0)
            episode_ok = int(cached_item.get("IndexNumber") or 0) == int(getattr(episode, "episode_number", 0) or 0)
            if cached_series and season_ok and episode_ok:
                return cached
        logger.debug(
            f"Jellyfin cached episode id mismatch episode_id={getattr(episode, 'id', None)} cached_item_id={cached}; re-resolving",
            extra={"emoji_type": "debug"},
        )
    parent_series_id = _find_jellyfin_series_item_id(series)
    if not parent_series_id:
        return None
    items = jellyfin_search_items(
        {
            "parentId": parent_series_id,
            "includeItemTypes": "Episode",
            "recursive": "true",
            "fields": "SeriesId,ParentIndexNumber,IndexNumber,ProviderIds,Path",
            "limit": 200,
        }
    )
    exact_matches: list[str] = []
    tvdb_matches: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        iid = str(item.get("Id") or "").strip()
        if not iid:
            continue
        series_id = str(item.get("SeriesId") or "").strip()
        s_ok = int(item.get("ParentIndexNumber") or 0) == int(season.season_number or 0)
        e_ok = int(item.get("IndexNumber") or 0) == int(episode.episode_number or 0)
        if series_id == str(parent_series_id) and s_ok and e_ok:
            exact_matches.append(iid)
            if _episode_provider_match(item, episode):
                tvdb_matches.append(iid)
    if len(tvdb_matches) == 1:
        return tvdb_matches[0]
    if len(exact_matches) == 1:
        return exact_matches[0]
    return None


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
    if status == "NOT_FOUND":
        # User-facing projection should read as outcome text, not enum token.
        return reason or "NO QUALIFYING RELEASE FOUND"
    return status or "REQUEST"


def _runtime_minutes_movie(movie: Movie) -> int | None:
    v = getattr(movie, "radarr_runtime", None)
    try:
        n = int(v) if v is not None else 0
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _runtime_minutes_episode(episode: Episode, series: Series) -> int | None:
    for src in (episode, series):
        v = getattr(src, "sonarr_runtime", None)
        try:
            n = int(v) if v is not None else 0
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return None


def _project_text(
    base_title: str | None,
    base_summary: str | None,
    status: str,
    *,
    suffix_template_key: str = "title.suffix.movie",
    runtime_minutes: int | None = None,
    media_context: dict | None = None,
) -> tuple[str, str]:
    return project_title(
        base_title or "",
        status,
        suffix_template_key=suffix_template_key,
        runtime_minutes=runtime_minutes,
        media_context=media_context,
    ), project_summary(
        base_summary or "",
        status,
        runtime_minutes=runtime_minutes,
        media_context=media_context,
    )


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


@dataclass
class ProjectionBatchSummary:
    plex_ok: int = 0
    plex_failed: int = 0
    plex_disabled: int = 0
    jellyfin_ok: int = 0
    jellyfin_failed: int = 0
    jellyfin_disabled: int = 0
    emby_ok: int = 0
    emby_failed: int = 0
    emby_disabled: int = 0


def _summary_mark(summary: ProjectionBatchSummary, server: str, ok: bool) -> None:
    if server == "plex":
        summary.plex_ok += 1 if ok else 0
        summary.plex_failed += 0 if ok else 1
        return
    if server == "jellyfin":
        summary.jellyfin_ok += 1 if ok else 0
        summary.jellyfin_failed += 0 if ok else 1
        return
    if server == "emby":
        summary.emby_ok += 1 if ok else 0
        summary.emby_failed += 0 if ok else 1
        return


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


def _push_movie(
    session,
    movie: Movie,
    placeholder: Placeholder,
    fallback: ProjectionFallbackAccumulator,
    summary: ProjectionBatchSummary,
) -> None:
    status = _projected_display_status(placeholder)
    runtime_minutes = _runtime_minutes_movie(movie) if status.strip().upper() == "REQUEST" else None
    media_ctx = build_projection_context(movie=movie, runtime_minutes=runtime_minutes)
    projected_title, projected_summary = _project_text(
        getattr(movie, "title", None),
        getattr(movie, "radarr_overview", None),
        status,
        suffix_template_key="title.suffix.movie",
        runtime_minutes=runtime_minutes,
        media_context=media_ctx,
    )

    if getattr(settings, "jellyfin_enabled", False):
        jf = _find_jellyfin_movie_item_id(movie)
        if jf:
            if str(getattr(movie, "jellyfin_id", "") or "").strip() != str(jf):
                movie.jellyfin_id = str(jf)
                session.add(movie)
                session.flush()
            jf_ok = update_jellyfin_item_text(jf, title=projected_title, overview=projected_summary)
            _summary_mark(summary, "jellyfin", jf_ok)
            logger.info(
                "Jellyfin direct projection movie outcome="
                f"{'ok' if jf_ok else 'failed'} item_id={jf} movie_id={int(getattr(movie, 'id', 0) or 0)} "
                f"status={status!r}",
                extra={"emoji_type": "info"},
            )
            if not jf_ok:
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
            _summary_mark(summary, "jellyfin", False)
            _mark_fallback(fallback, "jellyfin", "movie")
    else:
        summary.jellyfin_disabled += 1

    if getattr(settings, "emby_enabled", False):
        em = _find_emby_movie_item_id(movie, placeholder)
        if em:
            em_ok = update_emby_item_text(em, title=projected_title, overview=projected_summary)
            _summary_mark(summary, "emby", em_ok)
            logger.info(
                "Emby direct projection movie outcome="
                f"{'ok' if em_ok else 'failed'} item_id={em} movie_id={int(getattr(movie, 'id', 0) or 0)} "
                f"status={status!r}",
                extra={"emoji_type": "info"},
            )
            if not em_ok:
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
            _summary_mark(summary, "emby", False)
            _mark_fallback(fallback, "emby", "movie")
    else:
        summary.emby_disabled += 1

    if getattr(settings, "plex_enabled", False):
        plex_key = _plex_coalesce_cached_rating_key(movie)
        if plex_key:
            outcome: PlexMetadataRefreshResult = update_plex_item_text(
                plex_key,
                title=projected_title,
                summary=projected_summary,
            )
            logger.info(
                "Plex direct projection movie cached-key outcome="
                f"{outcome} rating_key={plex_key} movie_id={int(getattr(movie, 'id', 0) or 0)} "
                f"status={status!r}",
                extra={"emoji_type": "info"},
            )
            _summary_mark(summary, "plex", outcome == "ok")
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
                outcome = update_plex_item_text(
                    plex_movie.ratingKey,
                    title=projected_title,
                    summary=projected_summary,
                )
                logger.info(
                    "Plex direct projection movie resolved-key outcome="
                    f"{outcome} rating_key={plex_movie.ratingKey} movie_id={int(getattr(movie, 'id', 0) or 0)} "
                    f"status={status!r}",
                    extra={"emoji_type": "info"},
                )
                _summary_mark(summary, "plex", outcome == "ok")
                if outcome != "ok":
                    _mark_fallback(fallback, "plex", "movie")
            else:
                logger.debug(
                    f"Plex movie not resolved for tmdbid={getattr(movie, 'tmdbid', None)}",
                    extra={"emoji_type": "debug"},
                )
                _summary_mark(summary, "plex", False)
                _mark_fallback(fallback, "plex", "movie")
    else:
        summary.plex_disabled += 1


def _push_episode(
    session,
    placeholder: Placeholder,
    episode: Episode,
    fallback: ProjectionFallbackAccumulator,
    summary: ProjectionBatchSummary,
) -> None:
    season = session.query(Season).get(episode.season_id) if episode.season_id else None
    series = session.query(Series).get(season.series_id) if season and season.series_id else None
    if not season or not series:
        return

    status = _projected_display_status(placeholder)
    runtime_minutes = (
        _runtime_minutes_episode(episode, series) if status.strip().upper() == "REQUEST" else None
    )
    media_ctx = build_projection_context(
        episode=episode,
        season=season,
        series=series,
        runtime_minutes=runtime_minutes,
    )
    projected_ep_title, projected_ep_summary = _project_text(
        getattr(episode, "title", None),
        getattr(episode, "sonarr_episode_overview", None),
        status,
        suffix_template_key="title.suffix.episode",
        runtime_minutes=runtime_minutes,
        media_context=media_ctx,
    )
    if getattr(settings, "jellyfin_enabled", False):
        jf_ep = _find_jellyfin_episode_item_id(series, season, episode)
        if jf_ep:
            if str(getattr(episode, "jellyfin_id", "") or "").strip() != str(jf_ep):
                episode.jellyfin_id = str(jf_ep)
                session.add(episode)
                session.flush()
            jf_ep_ok = update_jellyfin_item_text(jf_ep, title=projected_ep_title, overview=projected_ep_summary)
            _summary_mark(summary, "jellyfin", jf_ep_ok)
            logger.info(
                "Jellyfin direct projection episode outcome="
                f"{'ok' if jf_ep_ok else 'failed'} item_id={jf_ep} "
                f"episode_id={int(getattr(episode, 'id', 0) or 0)} status={status!r}",
                extra={"emoji_type": "info"},
            )
            if not jf_ep_ok:
                _mark_fallback(fallback, "jellyfin", "episode")
        if not jf_ep:
            logger.debug(
                "Jellyfin episode item not resolved "
                f"tvdbid={getattr(series, 'tvdbid', None)} "
                f"S{season.season_number}E{episode.episode_number}",
                extra={"emoji_type": "debug"},
            )
            _summary_mark(summary, "jellyfin", False)
            _mark_fallback(fallback, "jellyfin", "episode")
    else:
        summary.jellyfin_disabled += 1

    if getattr(settings, "emby_enabled", False):
        em_ep = _find_emby_episode_item_id(series, season, episode, placeholder)
        if em_ep:
            em_ep_ok = update_emby_item_text(em_ep, title=projected_ep_title, overview=projected_ep_summary)
            _summary_mark(summary, "emby", em_ep_ok)
            logger.info(
                "Emby direct projection episode outcome="
                f"{'ok' if em_ep_ok else 'failed'} item_id={em_ep} "
                f"episode_id={int(getattr(episode, 'id', 0) or 0)} status={status!r}",
                extra={"emoji_type": "info"},
            )
            if not em_ep_ok:
                _mark_fallback(fallback, "emby", "episode")
        if not em_ep:
            logger.debug(
                "Emby episode item not resolved "
                f"tvdbid={getattr(series, 'tvdbid', None)} "
                f"S{season.season_number}E{episode.episode_number}",
                extra={"emoji_type": "debug"},
            )
            _summary_mark(summary, "emby", False)
            _mark_fallback(fallback, "emby", "episode")
    else:
        summary.emby_disabled += 1

    if getattr(settings, "plex_enabled", False):
        ep_key = _plex_coalesce_cached_rating_key(episode)
        if ep_key:
            ep_out = update_plex_item_text(ep_key, title=projected_ep_title, summary=projected_ep_summary)
            logger.info(
                "Plex direct projection episode cached-key outcome="
                f"{ep_out} rating_key={ep_key} episode_id={int(getattr(episode, 'id', 0) or 0)} "
                f"status={status!r}",
                extra={"emoji_type": "info"},
            )
            _summary_mark(summary, "plex", ep_out == "ok")
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
                    retry_out = update_plex_item_text(
                        plex_ep.ratingKey,
                        title=projected_ep_title,
                        summary=projected_ep_summary,
                    )
                    logger.info(
                        "Plex direct projection episode resolved-key outcome="
                        f"{retry_out} rating_key={plex_ep.ratingKey} "
                        f"episode_id={int(getattr(episode, 'id', 0) or 0)} status={status!r}",
                        extra={"emoji_type": "info"},
                    )
                    _summary_mark(summary, "plex", retry_out == "ok")
                    if retry_out != "ok":
                        _mark_fallback(fallback, "plex", "episode")
                else:
                    _summary_mark(summary, "plex", False)
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
                outcome = update_plex_item_text(
                    plex_ep.ratingKey,
                    title=projected_ep_title,
                    summary=projected_ep_summary,
                )
                logger.info(
                    "Plex direct projection episode lookup-only outcome="
                    f"{outcome} rating_key={plex_ep.ratingKey} "
                    f"episode_id={int(getattr(episode, 'id', 0) or 0)} status={status!r}",
                    extra={"emoji_type": "info"},
                )
                _summary_mark(summary, "plex", outcome == "ok")
                if outcome != "ok":
                    _mark_fallback(fallback, "plex", "episode")
            else:
                logger.debug(
                    "Plex episode not resolved "
                    f"tvdbid={getattr(series, 'tvdbid', None)} "
                    f"S{season.season_number}E{episode.episode_number}",
                    extra={"emoji_type": "debug"},
                )
                _summary_mark(summary, "plex", False)
                _mark_fallback(fallback, "plex", "episode")

        # Intentionally do not project series metadata for TV status updates.
    else:
        summary.plex_disabled += 1


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
    summary: ProjectionBatchSummary | None = None,
) -> ProjectionFallbackAccumulator:
    """Best-effort direct status projection to player title/summary fields."""
    acc = fallback or ProjectionFallbackAccumulator()
    run_summary = summary or ProjectionBatchSummary()
    if getattr(settings, "REFRESH_TRIGGER_SUPPRESSED", False):
        logger.debug(
            "Skipping player metadata refresh (REFRESH_TRIGGER_SUPPRESSED)",
            extra={"emoji_type": "debug"},
        )
        return acc

    movie = session.query(Movie).get(placeholder.movie_id) if placeholder.movie_id else None
    episode = session.query(Episode).get(placeholder.episode_id) if placeholder.episode_id else None

    if movie:
        _push_movie(session, movie, placeholder, acc, run_summary)
    elif episode:
        _push_episode(session, placeholder, episode, acc, run_summary)
    return acc


def push_placeholder_batch_player_metadata(session, placeholders: list[Placeholder]) -> None:
    """Project status directly for a placeholder batch and fallback-refresh once."""
    fallback = ProjectionFallbackAccumulator()
    summary = ProjectionBatchSummary()
    for placeholder in placeholders:
        push_placeholder_player_metadata(session, placeholder, fallback=fallback, summary=summary)
    logger.info(
        "Direct projection summary: "
        f"plex(ok={summary.plex_ok},failed={summary.plex_failed},disabled={summary.plex_disabled}) "
        f"jellyfin(ok={summary.jellyfin_ok},failed={summary.jellyfin_failed},disabled={summary.jellyfin_disabled}) "
        f"emby(ok={summary.emby_ok},failed={summary.emby_failed},disabled={summary.emby_disabled})",
        extra={"emoji_type": "info"},
    )
    _run_projection_fallback_refreshes(fallback)
