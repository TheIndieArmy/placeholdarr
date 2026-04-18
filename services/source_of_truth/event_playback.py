from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_

from core.config import settings
from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import Episode, Job, Movie, Placeholder, Season, Series
from services.source_of_truth.arr_api import (
    set_radarr_movie_monitored,
    set_sonarr_episode_monitored,
    set_sonarr_series_monitored,
    trigger_radarr_movie_search,
    trigger_sonarr_search,
)
from services.source_of_truth.status_intent import DisplayStatus, StatusIntent, StatusSource
from services.source_of_truth.status_orchestrator import StatusOrchestrator
from services.media_servers.jellyfin import get_jellyfin_file_path


PLAYBACK_FALLBACK_JOB_TYPE = 'playback_fallback'


def _instance_label(is_4k: bool) -> str:
    return '4k' if bool(is_4k) else 'standard'


def _search_preference() -> str:
    # Legacy `PLAYBACK_SEARCH_PREFERENCE` removed. Use TV playback instance mode
    # to determine preference when matching/missing in TV real-file routing.
    mode = str(getattr(settings, 'TV_PLAYBACK_INSTANCE_MODE', 'match') or 'match').strip().lower()
    if mode == 'primary':
        return 'standard'
    if mode == 'secondary':
        return '4k'
    return 'both'


def _tv_instance_mode() -> str:
    value = str(getattr(settings, 'TV_PLAYBACK_INSTANCE_MODE', 'match') or 'match').strip().lower()
    return value if value in {'match', 'primary', 'secondary', 'both'} else 'match'



def _movie_instance_mode() -> str:
    value = str(getattr(settings, 'MOVIE_PLAYBACK_INSTANCE_MODE', 'match') or 'match').strip().lower()
    return value if value in {'match', 'primary', 'secondary', 'both'} else 'match'


def _placeholder_search_pref(media_type: str) -> str:
    """Map MOVIE/TV_PLACEHOLDER_SEARCH_MODE (primary/secondary/both) to instance labels (standard/4k/both)."""
    if media_type == 'movie':
        value = str(getattr(settings, 'MOVIE_PLACEHOLDER_SEARCH_MODE', 'both') or 'both').strip().lower()
    else:
        value = str(getattr(settings, 'TV_PLACEHOLDER_SEARCH_MODE', 'both') or 'both').strip().lower()
    if value == 'primary':
        return 'standard'
    if value == 'secondary':
        return '4k'
    return 'both'


def _fallback_timeout_minutes() -> int:
    try:
        return max(0, int(getattr(settings, 'PLAYBACK_FALLBACK_TIMEOUT_MINUTES', 30) or 30))
    except Exception:
        return 30


def _fallback_enabled() -> bool:
    return bool(getattr(settings, 'ENABLE_PLAYBACK_FALLBACK_SEARCH', False)) and _fallback_timeout_minutes() > 0


def _resolve_endpoint(content_type: str, is_4k: bool) -> tuple[str, str]:
    arr_type = 'radarr' if content_type == 'movie' else 'sonarr'
    return settings.resolve_arr_endpoint(arr_type, is_4k=is_4k)


def _normalize_path(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return os.path.normpath(text)


def _path_is_within_root(path: str | None, root: str | None) -> bool:
    norm_path = _normalize_path(path)
    norm_root = _normalize_path(root)
    if not norm_path or not norm_root:
        return False
    try:
        return os.path.commonpath([norm_path, norm_root]) == norm_root
    except Exception:
        return False


def _match_tv_instance_from_path(path: str | None) -> str | None:
    matches: list[str] = []
    if _path_is_within_root(path, getattr(settings, 'TV_LIBRARY_FOLDER', '')):
        matches.append('standard')
    if _path_is_within_root(path, getattr(settings, 'TV_LIBRARY_4K_FOLDER', '')):
        matches.append('4k')

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return 'both'
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


def _extract_movie_tmdb_id(payload: dict[str, Any]) -> int | None:
    movie = payload.get('movie') if isinstance(payload.get('movie'), dict) else {}
    metadata = payload.get('metadata') if isinstance(payload.get('metadata'), dict) else {}
    media = payload.get('media') if isinstance(payload.get('media'), dict) else {}
    media_ids = media.get('ids') if isinstance(media.get('ids'), dict) else {}
    item = payload.get('Item') if isinstance(payload.get('Item'), dict) else {}
    provider_ids = item.get('ProviderIds') if isinstance(item.get('ProviderIds'), dict) else {}
    return (
        _as_int(movie.get('tmdbId'))
        or _as_int(payload.get('tmdbId'))
        or _as_int(payload.get('tmdb_id'))
        or _as_int(metadata.get('tmdbId'))
        or _as_int(media_ids.get('tmdb'))
        or _as_int(media_ids.get('tmdbId'))
        or _as_int(payload.get('Provider_tmdb'))
        or _as_int(provider_ids.get('Tmdb'))
        or _as_int(provider_ids.get('tmdb'))
    )


def _extract_movie_radarr_id(payload: dict[str, Any]) -> int | None:
    movie = payload.get('movie') if isinstance(payload.get('movie'), dict) else {}
    return (
        _as_int(movie.get('id'))
        or _as_int(payload.get('movieId'))
        or _as_int(payload.get('movie_id'))
    )


def _extract_series_ids(payload: dict[str, Any]) -> tuple[int | None, int | None]:
    series = payload.get('series') if isinstance(payload.get('series'), dict) else {}
    metadata = payload.get('metadata') if isinstance(payload.get('metadata'), dict) else {}
    media = payload.get('media') if isinstance(payload.get('media'), dict) else {}
    media_ids = media.get('ids') if isinstance(media.get('ids'), dict) else {}
    item = payload.get('Item') if isinstance(payload.get('Item'), dict) else {}
    provider_ids = item.get('ProviderIds') if isinstance(item.get('ProviderIds'), dict) else {}

    sonarr_id = (
        _as_int(series.get('id'))
        or _as_int(payload.get('seriesId'))
        or _as_int(payload.get('series_id'))
        or _as_int(item.get('ProviderIds', {}).get('sonarr'))
        or _as_int(item.get('ProviderIds', {}).get('Sonarr'))
    )
    tvdb_id = (
        _as_int(series.get('tvdbId'))
        or _as_int(series.get('tvdbid'))
        or _as_int(payload.get('tvdbId'))
        or _as_int(payload.get('tvdb_id'))
        or _as_int(metadata.get('tvdbId'))
        or _as_int(media_ids.get('tvdb'))
        or _as_int(media_ids.get('tvdbId'))
        or _as_int(payload.get('Provider_tvdb'))
        or _as_int(provider_ids.get('Tvdb'))
        or _as_int(provider_ids.get('tvdb'))
    )
    return sonarr_id, tvdb_id


def _extract_imdb_id(payload: dict[str, Any]) -> str | None:
    movie = payload.get('movie') if isinstance(payload.get('movie'), dict) else {}
    series = payload.get('series') if isinstance(payload.get('series'), dict) else {}
    metadata = payload.get('metadata') if isinstance(payload.get('metadata'), dict) else {}
    media = payload.get('media') if isinstance(payload.get('media'), dict) else {}
    media_ids = media.get('ids') if isinstance(media.get('ids'), dict) else {}
    item = payload.get('Item') if isinstance(payload.get('Item'), dict) else {}
    provider_ids = item.get('ProviderIds') if isinstance(item.get('ProviderIds'), dict) else {}
    candidates = [
        movie.get('imdbId'),
        movie.get('imdbid'),
        series.get('imdbId'),
        series.get('imdbid'),
        payload.get('imdbId'),
        payload.get('imdb_id'),
        metadata.get('imdbId'),
        metadata.get('imdbid'),
        media_ids.get('imdb'),
        media_ids.get('imdbId'),
        payload.get('Provider_imdb'),
        provider_ids.get('Imdb'),
        provider_ids.get('imdb'),
    ]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_season_episode(payload: dict[str, Any]) -> tuple[int | None, int | None]:
    ep = payload.get('episode') if isinstance(payload.get('episode'), dict) else {}
    metadata = payload.get('metadata') if isinstance(payload.get('metadata'), dict) else {}
    media = payload.get('media') if isinstance(payload.get('media'), dict) else {}
    item = payload.get('Item') if isinstance(payload.get('Item'), dict) else {}

    season = (
        _as_int(ep.get('seasonNumber'))
        or _as_int(payload.get('seasonNumber'))
        or _as_int(payload.get('SeasonNumber'))
        or _as_int(payload.get('season_number'))
        or _as_int(metadata.get('seasonNumber'))
        or _as_int(media.get('seasonNumber'))
        or _as_int(media.get('season_number'))
        or _as_int(media.get('season_num'))
        or _as_int(item.get('ParentIndexNumber'))
    )
    episode = (
        _as_int(ep.get('episodeNumber'))
        or _as_int(payload.get('episodeNumber'))
        or _as_int(payload.get('EpisodeNumber'))
        or _as_int(payload.get('episode_number'))
        or _as_int(metadata.get('episodeNumber'))
        or _as_int(media.get('episodeNumber'))
        or _as_int(media.get('episode_number'))
        or _as_int(media.get('episode_num'))
        or _as_int(item.get('IndexNumber'))
    )
    return season, episode


def _extract_file_path(payload: dict[str, Any]) -> str | None:
    candidates = [
        payload.get('file_path'),
        payload.get('filePath'),
        payload.get('path'),
        payload.get('fullPath'),
        payload.get('full_path'),
        payload.get('media_path'),
    ]

    media = payload.get('media') if isinstance(payload.get('media'), dict) else {}
    if media:
        candidates.extend([
            media.get('path'),
            media.get('filePath'),
            media.get('file_path'),
            media.get('fullPath'),
            media.get('full_path'),
        ])
        media_file_info = media.get('file_info') if isinstance(media.get('file_info'), dict) else {}
        if media_file_info:
            candidates.extend([
                media_file_info.get('path'),
                media_file_info.get('filePath'),
                media_file_info.get('file_path'),
                media_file_info.get('fullPath'),
                media_file_info.get('full_path'),
            ])

    metadata = payload.get('metadata') if isinstance(payload.get('metadata'), dict) else {}
    if metadata:
        candidates.extend([
            metadata.get('path'),
            metadata.get('filePath'),
            metadata.get('file_path'),
            metadata.get('fullPath'),
            metadata.get('full_path'),
        ])

    item = payload.get('Item') if isinstance(payload.get('Item'), dict) else {}
    if item:
        candidates.extend([
            item.get('Path'),
            item.get('path'),
        ])

    playback_info = payload.get('PlaybackInfo') if isinstance(payload.get('PlaybackInfo'), dict) else {}
    media_source = playback_info.get('MediaSource') if isinstance(playback_info.get('MediaSource'), dict) else {}
    if media_source:
        candidates.extend([
            media_source.get('Path'),
            media_source.get('path'),
        ])

    for value in candidates:
        normalized = _normalize_path(value)
        if normalized:
            return normalized
    return None


def _extract_declared_media_type(payload: dict[str, Any]) -> str | None:
    media = payload.get('media') if isinstance(payload.get('media'), dict) else {}
    item = payload.get('Item') if isinstance(payload.get('Item'), dict) else {}
    candidates = [
        payload.get('media_type'),
        payload.get('mediaType'),
        payload.get('type'),
        payload.get('ItemType'),
        media.get('type'),
        media.get('media_type'),
        item.get('Type'),
        item.get('MediaType'),
    ]
    for value in candidates:
        if not isinstance(value, str):
            continue
        normalized = value.strip().lower()
        if normalized in {'movie', 'episode'}:
            return normalized
        if normalized in {'show', 'episodefile', 'tv', 'tvshow', 'season'}:
            return 'episode'
        if normalized in {'film'}:
            return 'movie'
    return None


def _dedupe_rows(rows: list[Any]) -> list[Any]:
    deduped: dict[int, Any] = {}
    for row in rows:
        row_id = getattr(row, 'id', None)
        if row_id is None:
            continue
        deduped[int(row_id)] = row
    return list(deduped.values())


def _resolve_media_from_path(session, path: str | None) -> dict[str, Any]:
    file_path = _normalize_path(path)
    if not file_path:
        return {'media_type': 'unknown', 'playback_kind': 'unknown'}

    movie_row = (
        session.query(Movie)
        .filter(
            or_(
                Movie.radarr_filepath == file_path,
                Movie.placeholder_filepath == file_path,
            )
        )
        .first()
    )
    if movie_row:
        playback_kind = 'real' if _normalize_path(getattr(movie_row, 'radarr_filepath', None)) == file_path else 'placeholder'
        return {
            'media_type': 'movie',
            'playback_kind': playback_kind,
            'tmdb_id': int(movie_row.tmdbid) if getattr(movie_row, 'tmdbid', None) else None,
            'movie_id': int(movie_row.id),
            'matched_instance': _instance_label(bool(getattr(movie_row, 'is_4k', False))),
        }

    episode_row = (
        session.query(Episode)
        .join(Season, Episode.season_id == Season.id)
        .join(Series, Season.series_id == Series.id)
        .filter(
            or_(
                Episode.sonarr_filepath == file_path,
                Episode.placeholder_filepath == file_path,
            )
        )
        .first()
    )
    if episode_row:
        series_row = episode_row.season.series if episode_row.season else None
        playback_kind = 'real' if _normalize_path(getattr(episode_row, 'sonarr_filepath', None)) == file_path else 'placeholder'
        return {
            'media_type': 'episode',
            'playback_kind': playback_kind,
            'tvdb_id': int(series_row.tvdbid) if series_row and getattr(series_row, 'tvdbid', None) else None,
            'season_number': int(episode_row.season.season_number) if episode_row.season else None,
            'episode_number': int(episode_row.episode_number) if getattr(episode_row, 'episode_number', None) else None,
            'series_id': int(series_row.id) if series_row else None,
            'matched_instance': _instance_label(bool(getattr(series_row, 'is_4k', False))) if series_row else None,
        }

    ph_row = session.query(Placeholder).filter(Placeholder.path == file_path).first()
    if ph_row:
        if getattr(ph_row, 'movie_id', None):
            movie = session.query(Movie).filter(Movie.id == int(ph_row.movie_id)).first()
            if movie:
                return {
                    'media_type': 'movie',
                    'playback_kind': 'placeholder',
                    'tmdb_id': int(movie.tmdbid) if getattr(movie, 'tmdbid', None) else None,
                    'movie_id': int(movie.id),
                    'matched_instance': _instance_label(bool(getattr(movie, 'is_4k', False))),
                }
        if getattr(ph_row, 'episode_id', None):
            ep = (
                session.query(Episode)
                .join(Season, Episode.season_id == Season.id)
                .join(Series, Season.series_id == Series.id)
                .filter(Episode.id == int(ph_row.episode_id))
                .first()
            )
            if ep:
                series = ep.season.series if ep.season else None
                return {
                    'media_type': 'episode',
                    'playback_kind': 'placeholder',
                    'tvdb_id': int(series.tvdbid) if series and getattr(series, 'tvdbid', None) else None,
                    'season_number': int(ep.season.season_number) if ep.season else None,
                    'episode_number': int(ep.episode_number) if getattr(ep, 'episode_number', None) else None,
                    'series_id': int(series.id) if series else None,
                    'matched_instance': _instance_label(bool(getattr(series, 'is_4k', False))) if series else None,
                }

    return {'media_type': 'unknown', 'playback_kind': 'unknown'}


def _resolve_playback_context(session, payload: dict[str, Any]) -> dict[str, Any]:
    tmdb_id = _extract_movie_tmdb_id(payload)
    _, tvdb_id = _extract_series_ids(payload)
    imdb_id = _extract_imdb_id(payload)
    season_number, episode_number = _extract_season_episode(payload)
    file_path = _extract_file_path(payload)
    # If no path found in the payload, attempt to fetch it from Jellyfin using ItemId/UserId
    if not file_path:
        try:
            item_id = None
            # Payload may include ItemId or nested Item.Id
            if isinstance(payload.get('ItemId'), (str, int)):
                item_id = str(payload.get('ItemId'))
            elif isinstance(payload.get('Item'), dict) and payload.get('Item').get('Id'):
                item_id = str(payload.get('Item').get('Id'))

            if item_id and getattr(settings, 'ENABLE_JELLYFIN', False):
                user_id = None
                user_obj = payload.get('User') or payload.get('user') or {}
                if isinstance(user_obj, dict):
                    user_id = user_obj.get('Id') or user_obj.get('id')
                if not user_id:
                    user_id = payload.get('UserId') or payload.get('userId')

                fetched_path = get_jellyfin_file_path(item_id, user_id)
                if fetched_path:
                    file_path = _normalize_path(fetched_path)
                    logger.debug(f"Fetched Jellyfin file path for item {item_id}: {file_path}", extra={'emoji_type': 'debug'})
        except Exception as e:
            logger.debug(f"Error fetching Jellyfin file path: {e}", extra={'emoji_type': 'debug'})
    declared_media_type = _extract_declared_media_type(payload)
    path_info = _resolve_media_from_path(session, file_path)

    if tmdb_id is None and path_info.get('tmdb_id') is not None:
        tmdb_id = int(path_info['tmdb_id'])
    if tvdb_id is None and path_info.get('tvdb_id') is not None:
        tvdb_id = int(path_info['tvdb_id'])
    if season_number is None and path_info.get('season_number') is not None:
        season_number = int(path_info['season_number'])
    if episode_number is None and path_info.get('episode_number') is not None:
        episode_number = int(path_info['episode_number'])

    media_type = 'unknown'
    if path_info.get('media_type') in {'movie', 'episode'}:
        media_type = str(path_info['media_type'])
    elif declared_media_type in {'movie', 'episode'}:
        media_type = declared_media_type
    elif tvdb_id is not None or season_number is not None:
        media_type = 'episode'
    elif tmdb_id is not None:
        media_type = 'movie'

    return {
        'media_type': media_type,
        'playback_kind': str(path_info.get('playback_kind') or 'unknown'),
        'file_path': file_path,
        'tmdb_id': tmdb_id,
        'tvdb_id': tvdb_id,
        'imdb_id': imdb_id,
        'season_number': season_number,
        'episode_number': episode_number,
        'path_info': path_info,
    }


def _find_movie_rows(session, *, tmdb_id: int | None, imdb_id: str | None, file_path: str | None) -> list[Movie]:
    rows: list[Movie] = []
    if tmdb_id is not None:
        rows.extend(session.query(Movie).filter(Movie.tmdbid == tmdb_id).all())
    if imdb_id:
        rows.extend(session.query(Movie).filter(Movie.imdbid == imdb_id).all())
    if file_path:
        rows.extend(
            session.query(Movie)
            .filter(
                or_(
                    Movie.radarr_filepath == file_path,
                    Movie.placeholder_filepath == file_path,
                )
            )
            .all()
        )
        ph_movie_ids = [
            int(row.movie_id)
            for row in session.query(Placeholder).filter(
                Placeholder.path == file_path,
                Placeholder.movie_id.isnot(None),
            ).all()
            if getattr(row, 'movie_id', None)
        ]
        if ph_movie_ids:
            rows.extend(session.query(Movie).filter(Movie.id.in_(ph_movie_ids)).all())
    return _dedupe_rows(rows)


def _find_series_rows(session, *, sonarr_id: int | None, tvdb_id: int | None, imdb_id: str | None, file_path: str | None) -> list[Series]:
    rows: list[Series] = []
    if tvdb_id is not None:
        rows.extend(session.query(Series).filter(Series.tvdbid == tvdb_id).all())
    if sonarr_id is not None:
        rows.extend(session.query(Series).filter(Series.sonarrid == sonarr_id).all())
    if imdb_id:
        rows.extend(session.query(Series).filter(Series.imdbid == imdb_id).all())
    if file_path:
        episode_rows = (
            session.query(Episode)
            .join(Season, Episode.season_id == Season.id)
            .join(Series, Season.series_id == Series.id)
            .filter(
                or_(
                    Episode.sonarr_filepath == file_path,
                    Episode.placeholder_filepath == file_path,
                )
            )
            .all()
        )
        rows.extend([ep.season.series for ep in episode_rows if ep.season and ep.season.series])
        ph_episode_ids = [
            int(row.episode_id)
            for row in session.query(Placeholder).filter(
                Placeholder.path == file_path,
                Placeholder.episode_id.isnot(None),
            ).all()
            if getattr(row, 'episode_id', None)
        ]
        if ph_episode_ids:
            episode_rows = (
                session.query(Episode)
                .join(Season, Episode.season_id == Season.id)
                .join(Series, Season.series_id == Series.id)
                .filter(Episode.id.in_(ph_episode_ids))
                .all()
            )
            rows.extend([ep.season.series for ep in episode_rows if ep.season and ep.season.series])
    return _dedupe_rows(rows)


def _active_rows_by_instance(rows: list[Any]) -> dict[str, Any]:
    active: dict[str, Any] = {}
    for row in rows:
        if bool(getattr(row, 'is_deleted', False)):
            continue
        label = _instance_label(bool(getattr(row, 'is_4k', False)))
        active[label] = row
    return active


def _select_rows_by_preference(rows_by_instance: dict[str, Any], preference: str) -> dict[str, Any]:
    qualifying_instances = [label for label in ('standard', '4k') if rows_by_instance.get(label) is not None]
    if preference == 'both':
        return {
            'rows': [rows_by_instance[label] for label in qualifying_instances],
            'chosen_instances': qualifying_instances,
            'qualifying_instances': qualifying_instances,
            'preferred_instance': None,
            'fallback_instance': None,
            'selection_reason': 'preference_both',
            'immediate_fallback': False,
        }

    preferred_instance = '4k' if preference == '4k' else 'standard'
    fallback_instance = 'standard' if preferred_instance == '4k' else '4k'
    preferred_row = rows_by_instance.get(preferred_instance)
    if preferred_row is not None:
        return {
            'rows': [preferred_row],
            'chosen_instances': [preferred_instance],
            'qualifying_instances': qualifying_instances,
            'preferred_instance': preferred_instance,
            'fallback_instance': fallback_instance if rows_by_instance.get(fallback_instance) is not None else None,
            'selection_reason': 'preferred_active_row',
            'immediate_fallback': False,
        }

    fallback_row = rows_by_instance.get(fallback_instance)
    if fallback_row is not None:
        return {
            'rows': [fallback_row],
            'chosen_instances': [fallback_instance],
            'qualifying_instances': qualifying_instances,
            'preferred_instance': preferred_instance,
            'fallback_instance': fallback_instance,
            'selection_reason': 'preferred_missing_active_row',
            'immediate_fallback': True,
        }

    return {
        'rows': [],
        'chosen_instances': [],
        'qualifying_instances': qualifying_instances,
        'preferred_instance': preferred_instance,
        'fallback_instance': None,
        'selection_reason': 'no_active_rows',
        'immediate_fallback': False,
    }


def _select_tv_real_rows(rows_by_instance: dict[str, Any], file_path: str | None) -> dict[str, Any]:
    mode = _tv_instance_mode()
    if mode == 'both':
        selection = _select_rows_by_preference(rows_by_instance, 'both')
        selection['selection_reason'] = 'tv_mode_both'
        selection['root_match'] = _match_tv_instance_from_path(file_path)
        return selection

    if mode == 'match':
        matched_instance = _match_tv_instance_from_path(file_path)
        if matched_instance in {'standard', '4k'} and rows_by_instance.get(matched_instance) is not None:
            fallback_instance = '4k' if matched_instance == 'standard' else 'standard'
            return {
                'rows': [rows_by_instance[matched_instance]],
                'chosen_instances': [matched_instance],
                'qualifying_instances': [label for label in ('standard', '4k') if rows_by_instance.get(label) is not None],
                'preferred_instance': matched_instance,
                'fallback_instance': fallback_instance if rows_by_instance.get(fallback_instance) is not None else None,
                'selection_reason': 'matched_by_root',
                'immediate_fallback': False,
                'root_match': matched_instance,
            }

        selection = _select_rows_by_preference(rows_by_instance, _search_preference())
        selection['selection_reason'] = 'match_ambiguous_used_preference' if matched_instance == 'both' else 'match_missing_used_preference'
        selection['root_match'] = matched_instance
        return selection

    if mode == 'primary':
        matched_instance = _match_tv_instance_from_path(file_path)
        qualifying_instances = [label for label in ('standard', '4k') if rows_by_instance.get(label) is not None]
        row = rows_by_instance.get('standard')
        fallback = rows_by_instance.get('4k')
        if row is not None:
            return {
                'rows': [row],
                'chosen_instances': ['standard'],
                'qualifying_instances': qualifying_instances,
                'preferred_instance': 'standard',
                'fallback_instance': '4k' if fallback is not None else None,
                'selection_reason': 'tv_mode_primary',
                'immediate_fallback': False,
                'root_match': matched_instance,
            }
        return {
            'rows': [],
            'chosen_instances': [],
            'qualifying_instances': qualifying_instances,
            'preferred_instance': 'standard',
            'fallback_instance': None,
            'selection_reason': 'tv_mode_primary_no_row',
            'immediate_fallback': False,
            'root_match': matched_instance,
        }

    if mode == 'secondary':
        matched_instance = _match_tv_instance_from_path(file_path)
        qualifying_instances = [label for label in ('standard', '4k') if rows_by_instance.get(label) is not None]
        row = rows_by_instance.get('4k')
        fallback = rows_by_instance.get('standard')
        if row is not None:
            return {
                'rows': [row],
                'chosen_instances': ['4k'],
                'qualifying_instances': qualifying_instances,
                'preferred_instance': '4k',
                'fallback_instance': 'standard' if fallback is not None else None,
                'selection_reason': 'tv_mode_secondary',
                'immediate_fallback': False,
                'root_match': matched_instance,
            }
        return {
            'rows': [],
            'chosen_instances': [],
            'qualifying_instances': qualifying_instances,
            'preferred_instance': '4k',
            'fallback_instance': None,
            'selection_reason': 'tv_mode_secondary_no_row',
            'immediate_fallback': False,
            'root_match': matched_instance,
        }

    selection = _select_rows_by_preference(rows_by_instance, _search_preference())
    selection['selection_reason'] = 'tv_preference_mode'
    selection['root_match'] = _match_tv_instance_from_path(file_path)
    return selection


def _play_mode() -> str:
    mode = str(getattr(settings, 'TV_PLAY_MODE', 'episode') or 'episode').strip().lower()
    return mode if mode in {'episode', 'season', 'series'} else 'episode'


def _episode_lookahead() -> int:
    try:
        return max(1, int(getattr(settings, 'EPISODES_LOOKAHEAD', 5) or 5))
    except Exception:
        return 5


def _collect_episode_targets(session, series_row: Series, season_number: int | None, episode_number: int | None) -> tuple[list[Episode], dict[str, Any]]:
    mode = _play_mode()
    include_specials = bool(getattr(settings, 'INCLUDE_SPECIALS', False))
    lookahead = _episode_lookahead()

    scoped_query = (
        session.query(Episode)
        .join(Season, Episode.season_id == Season.id)
        .filter(
            Season.series_id == series_row.id,
            Episode.is_deleted == False,  # noqa: E712
        )
    )
    if not include_specials:
        scoped_query = scoped_query.filter(Season.season_number > 0)

    targets: list[Episode] = []
    metadata = {'mode': mode, 'reached_end': False, 'lookahead': lookahead}

    if mode == 'series':
        targets = (
            scoped_query
            .filter(Episode.has_file == False)  # noqa: E712
            .order_by(Season.season_number.asc(), Episode.episode_number.asc())
            .all()
        )
        metadata['scope'] = 'series'
        return targets, metadata

    if season_number is None:
        return [], {'mode': mode, 'reason': 'missing_season_number'}

    if mode == 'season':
        season_targets = (
            scoped_query
            .filter(
                Season.season_number == season_number,
                Episode.has_file == False,  # noqa: E712
            )
            .order_by(Episode.episode_number.asc())
            .all()
        )
        targets.extend(season_targets)

        max_in_season = None
        if episode_number is not None:
            max_in_season = (
                session.query(Episode)
                .join(Season, Episode.season_id == Season.id)
                .filter(
                    Season.series_id == series_row.id,
                    Season.season_number == season_number,
                    Episode.is_deleted == False,  # noqa: E712
                )
                .order_by(Episode.episode_number.desc())
                .first()
            )
            if max_in_season and int(episode_number) >= int(max_in_season.episode_number or 0):
                next_targets = (
                    scoped_query
                    .filter(
                        Season.season_number == (int(season_number) + 1),
                        Episode.has_file == False,  # noqa: E712
                    )
                    .order_by(Episode.episode_number.asc())
                    .all()
                )
                targets.extend(next_targets)
                metadata['season_boundary_next_included'] = True

        # Match Episode-mode behavior: when the user reaches the end of the highest season Placeholdarr
        # stores, mark the whole series monitored in Sonarr so newly-added future seasons/episodes are picked up.
        max_season_q = session.query(func.max(Season.season_number)).filter(Season.series_id == series_row.id)
        if not include_specials:
            max_season_q = max_season_q.filter(Season.season_number > 0)
        max_season_num = max_season_q.scalar()
        if (
            episode_number is not None
            and max_in_season is not None
            and max_season_num is not None
            and int(season_number) == int(max_season_num)
            and int(episode_number) >= int(max_in_season.episode_number or 0)
        ):
            metadata['reached_end'] = True

        metadata['scope'] = 'season'
        return targets, metadata

    if episode_number is None:
        return [], {'mode': mode, 'reason': 'missing_episode_number'}

    window = (
        scoped_query
        .filter(
            or_(
                Season.season_number > int(season_number),
                and_(Season.season_number == int(season_number), Episode.episode_number >= int(episode_number)),
            )
        )
        .order_by(Season.season_number.asc(), Episode.episode_number.asc())
        .limit(int(lookahead))
        .all()
    )
    targets.extend([ep for ep in window if not bool(getattr(ep, 'has_file', False))])

    last_episode = (
        session.query(Episode)
        .join(Season, Episode.season_id == Season.id)
        .filter(
            Season.series_id == series_row.id,
            Episode.is_deleted == False,  # noqa: E712
            (Season.season_number > 0) if not include_specials else True,
        )
        .order_by(Season.season_number.desc(), Episode.episode_number.desc())
        .first()
    )
    if window and last_episode:
        final_window_episode = window[-1]
        final_target_season = final_window_episode.season.season_number if final_window_episode.season else 0
        final_target_episode = int(final_window_episode.episode_number or 0)
        max_season = last_episode.season.season_number if last_episode.season else 0
        max_episode = int(last_episode.episode_number or 0)
        metadata['reached_end'] = bool(
            final_target_season > max_season
            or (final_target_season == max_season and final_target_episode >= max_episode)
        )
    metadata['window_size'] = len(window)
    metadata['target_count'] = len(targets)

    metadata['scope'] = 'episode'
    return targets, metadata


def _placeholder_intents_for_targets(session, targets: list[Episode], movie_row: Movie | None = None) -> list[StatusIntent]:
    intents: list[StatusIntent] = []
    rows: list[Placeholder] = []
    if movie_row is not None:
        rows = (
            session.query(Placeholder)
            .filter(
                Placeholder.movie_id == movie_row.id,
                Placeholder.has_placeholder == True,  # noqa: E712
            )
            .all()
        )
    elif targets:
        episode_ids = [int(ep.id) for ep in targets if getattr(ep, 'id', None)]
        rows = (
            session.query(Placeholder)
            .filter(
                Placeholder.episode_id.in_(episode_ids),
                Placeholder.has_placeholder == True,  # noqa: E712
            )
            .all()
        )

    for row in rows:
        intents.append(
            StatusIntent(
                placeholder_id=int(row.id),
                new_status=DisplayStatus.SEARCHING.value,
                reason='Playback started; triggering ARR search',
                source=StatusSource.EVENT_PLAYBACK_STARTED,
                trigger_nfo_refresh=True,
                metadata={'playback': True},
            )
        )
    return intents


def _run_movie_search_for_row(session, movie_row: Movie) -> dict[str, Any]:
    if bool(getattr(movie_row, 'is_deleted', False)):
        return {'ok': True, 'skipped': 'deleted_in_arr', 'movie_id': int(movie_row.id), 'instance': _instance_label(bool(getattr(movie_row, 'is_4k', False)))}

    if bool(getattr(movie_row, 'has_file', False)):
        return {'ok': True, 'skipped': 'has_file', 'movie_id': int(movie_row.id), 'instance': _instance_label(bool(getattr(movie_row, 'is_4k', False)))}

    base_url, api_key = _resolve_endpoint('movie', bool(getattr(movie_row, 'is_4k', False)))
    if not base_url or not api_key:
        return {'ok': False, 'reason': 'missing_movie_arr_config', 'movie_id': int(movie_row.id), 'instance': _instance_label(bool(getattr(movie_row, 'is_4k', False)))}

    monitored_updated = False
    if not bool(getattr(movie_row, 'radarr_monitored', False)) and getattr(movie_row, 'radarrid', None):
        monitored_updated = set_radarr_movie_monitored(int(movie_row.radarrid), True, url=base_url, api_key=api_key)
        if monitored_updated:
            movie_row.radarr_monitored = True
            session.add(movie_row)

    search_triggered = False
    if getattr(movie_row, 'radarrid', None):
        search_triggered = trigger_radarr_movie_search(int(movie_row.radarrid), url=base_url, api_key=api_key)

    intents = _placeholder_intents_for_targets(session, [], movie_row=movie_row)
    if intents:
        StatusOrchestrator(session=session).apply_and_project_statuses(intents)

    return {
        'ok': True,
        'event': 'playback_start',
        'media_type': 'movie',
        'movie_id': int(movie_row.id),
        'instance': _instance_label(bool(getattr(movie_row, 'is_4k', False))),
        'monitored_updated': monitored_updated,
        'search_triggered': bool(search_triggered),
        'status_intents_applied': len(intents),
    }


def _run_episode_search_for_row(session, series_row: Series, payload: dict[str, Any]) -> dict[str, Any]:
    if bool(getattr(series_row, 'is_deleted', False)):
        return {'ok': True, 'skipped': 'deleted_in_arr', 'series_id': int(series_row.id), 'instance': _instance_label(bool(getattr(series_row, 'is_4k', False)))}

    season_number, episode_number = _extract_season_episode(payload)
    targets, target_meta = _collect_episode_targets(session, series_row, season_number, episode_number)
    if not targets:
        return {
            'ok': True,
            'event': 'playback_start',
            'media_type': 'episode',
            'series_id': int(series_row.id),
            'instance': _instance_label(bool(getattr(series_row, 'is_4k', False))),
            'skipped': 'no_targets',
            'target_meta': target_meta,
        }

    base_url, api_key = _resolve_endpoint('series', bool(getattr(series_row, 'is_4k', False)))
    if not base_url or not api_key:
        return {'ok': False, 'reason': 'missing_series_arr_config', 'series_id': int(series_row.id), 'instance': _instance_label(bool(getattr(series_row, 'is_4k', False)))}

    target_episode_ids = [int(ep.sonarrid) for ep in targets if getattr(ep, 'sonarrid', None)]
    monitor_result = set_sonarr_episode_monitored(target_episode_ids, True, url=base_url, api_key=api_key)

    for ep in targets:
        ep.sonarr_monitored = True
        session.add(ep)

    mode = _play_mode()
    search_triggered = False
    if mode == 'series':
        if getattr(series_row, 'sonarrid', None):
            search_triggered = trigger_sonarr_search(
                series_id=int(series_row.sonarrid),
                url=base_url,
                api_key=api_key,
            )
    else:
        if target_episode_ids and getattr(series_row, 'sonarrid', None):
            search_triggered = trigger_sonarr_search(
                series_id=int(series_row.sonarrid),
                episode_ids=target_episode_ids,
                url=base_url,
                api_key=api_key,
            )

    reached_end_marked = False
    if bool(target_meta.get('reached_end')) and getattr(series_row, 'sonarrid', None):
        reached_end_marked = set_sonarr_series_monitored(
            int(series_row.sonarrid),
            True,
            include_specials=bool(getattr(settings, 'INCLUDE_SPECIALS', False)),
            url=base_url,
            api_key=api_key,
        )
        if reached_end_marked:
            series_row.sonarr_monitored = True
            session.add(series_row)

    intents = _placeholder_intents_for_targets(session, targets)
    if intents:
        StatusOrchestrator(session=session).apply_and_project_statuses(intents)

    return {
        'ok': True,
        'event': 'playback_start',
        'media_type': 'episode',
        'series_id': int(series_row.id),
        'instance': _instance_label(bool(getattr(series_row, 'is_4k', False))),
        'mode': mode,
        'targets': len(targets),
        'target_meta': target_meta,
        'monitor_updated': int(monitor_result.get('updated', 0)),
        'monitor_failed': int(monitor_result.get('failed', 0)),
        'search_triggered': bool(search_triggered),
        'reached_end_marked': bool(reached_end_marked),
        'status_intents_applied': len(intents),
    }


def _should_schedule_delayed_fallback(selection: dict[str, Any], chosen_instance: str | None) -> bool:
    if not _fallback_enabled():
        return False
    if not chosen_instance:
        return False
    if len(selection.get('chosen_instances') or []) != 1:
        return False
    if not selection.get('fallback_instance'):
        return False
    return chosen_instance == selection.get('preferred_instance')


def _enqueue_delayed_fallback(
    session,
    *,
    media_type: str,
    payload: dict[str, Any],
    preferred_instance: str,
    fallback_instance: str,
    source_instance: str | None,
) -> int | None:
    if not _fallback_enabled():
        return None

    timeout_minutes = _fallback_timeout_minutes()
    if timeout_minutes <= 0:
        return None

    job = Job(
        job_type=PLAYBACK_FALLBACK_JOB_TYPE,
        payload={
            'media_type': media_type,
            'preferred_instance': preferred_instance,
            'fallback_instance': fallback_instance,
            'source_instance': source_instance,
            'payload': dict(payload),
        },
        status='PENDING',
        max_attempts=3,
        run_after=datetime.now(timezone.utc) + timedelta(minutes=timeout_minutes),
    )
    session.add(job)
    session.flush()
    logger.info(
        f"Enqueued playback fallback job job_id={job.id} media_type={media_type} preferred={preferred_instance} fallback={fallback_instance}",
        extra={'emoji_type': 'processing'},
    )
    return int(job.id)


def _process_movie_playback(session, payload: dict[str, Any], context: dict[str, Any], source_instance: str | None) -> dict[str, Any]:
    movie_rows = _find_movie_rows(
        session,
        tmdb_id=context.get('tmdb_id'),
        imdb_id=context.get('imdb_id'),
        file_path=context.get('file_path'),
    )
    active_rows = _active_rows_by_instance(movie_rows)
    playback_kind = str(context.get('playback_kind') or 'unknown')

    if playback_kind == 'real':
        return {
            'ok': True,
            'event': 'playback_start',
            'media_type': 'movie',
            'skipped': 'real_movie_noop',
            'qualifying_instances': sorted(active_rows.keys()),
        }

    if playback_kind != 'placeholder':
        return {'ok': False, 'reason': 'unresolved_movie_playback_kind'}

    selection = _select_rows_by_preference(active_rows, _placeholder_search_pref('movie'))
    selected_rows = selection.get('rows') or []
    if not selected_rows:
        return {
            'ok': True,
            'event': 'playback_start',
            'media_type': 'movie',
            'skipped': 'no_active_row',
            'qualifying_instances': selection.get('qualifying_instances') or [],
            'selection_reason': selection.get('selection_reason'),
        }

    per_instance_results: list[dict[str, Any]] = []
    fallback_job_id: int | None = None
    for row in selected_rows:
        result = _run_movie_search_for_row(session, row)
        per_instance_results.append(result)
        chosen_instance = str(result.get('instance') or '')
        if result.get('search_triggered') and _should_schedule_delayed_fallback(selection, chosen_instance):
            fallback_job_id = _enqueue_delayed_fallback(
                session,
                media_type='movie',
                payload=payload,
                preferred_instance=str(selection.get('preferred_instance')),
                fallback_instance=str(selection.get('fallback_instance')),
                source_instance=source_instance,
            )

    return {
        'ok': True,
        'event': 'playback_start',
        'media_type': 'movie',
        'playback_kind': playback_kind,
        'selection_reason': selection.get('selection_reason'),
        'qualifying_instances': selection.get('qualifying_instances') or [],
        'chosen_instances': selection.get('chosen_instances') or [],
        'fallback_job_id': fallback_job_id,
        'results': per_instance_results,
    }


def _process_episode_playback(session, payload: dict[str, Any], context: dict[str, Any], source_instance: str | None) -> dict[str, Any]:
    sonarr_id, _ = _extract_series_ids(payload)
    series_rows = _find_series_rows(
        session,
        sonarr_id=sonarr_id,
        tvdb_id=context.get('tvdb_id'),
        imdb_id=context.get('imdb_id'),
        file_path=context.get('file_path'),
    )
    active_rows = _active_rows_by_instance(series_rows)
    playback_kind = str(context.get('playback_kind') or 'unknown')

    if playback_kind == 'placeholder':
        selection = _select_rows_by_preference(active_rows, _placeholder_search_pref('tv'))
    elif playback_kind == 'real':
        selection = _select_tv_real_rows(active_rows, context.get('file_path'))
    else:
        return {'ok': False, 'reason': 'unresolved_episode_playback_kind'}

    selected_rows = selection.get('rows') or []
    if not selected_rows:
        return {
            'ok': True,
            'event': 'playback_start',
            'media_type': 'episode',
            'playback_kind': playback_kind,
            'skipped': 'no_active_row',
            'qualifying_instances': selection.get('qualifying_instances') or [],
            'selection_reason': selection.get('selection_reason'),
            'root_match': selection.get('root_match'),
        }

    per_instance_results: list[dict[str, Any]] = []
    fallback_job_id: int | None = None
    for row in selected_rows:
        result = _run_episode_search_for_row(session, row, payload)
        per_instance_results.append(result)
        chosen_instance = str(result.get('instance') or '')
        if result.get('search_triggered') and _should_schedule_delayed_fallback(selection, chosen_instance):
            fallback_job_id = _enqueue_delayed_fallback(
                session,
                media_type='episode',
                payload=payload,
                preferred_instance=str(selection.get('preferred_instance')),
                fallback_instance=str(selection.get('fallback_instance')),
                source_instance=source_instance,
            )

    return {
        'ok': True,
        'event': 'playback_start',
        'media_type': 'episode',
        'playback_kind': playback_kind,
        'selection_reason': selection.get('selection_reason'),
        'qualifying_instances': selection.get('qualifying_instances') or [],
        'chosen_instances': selection.get('chosen_instances') or [],
        'root_match': selection.get('root_match'),
        'fallback_job_id': fallback_job_id,
        'results': per_instance_results,
    }


def _preferred_movie_import_succeeded(preferred_row: Movie | None) -> bool:
    return bool(preferred_row and getattr(preferred_row, 'has_file', False))


def _preferred_episode_import_succeeded(session, preferred_row: Series | None, payload: dict[str, Any]) -> bool:
    if preferred_row is None or bool(getattr(preferred_row, 'is_deleted', False)):
        return False
    season_number, episode_number = _extract_season_episode(payload)
    targets, _ = _collect_episode_targets(session, preferred_row, season_number, episode_number)
    return len(targets) == 0


def process_playback_fallback_job(session, job: Job) -> dict[str, Any]:
    if not _fallback_enabled():
        return {'ok': True, 'skipped': 'playback_fallback_disabled'}

    payload = job.payload or {}
    media_type = str(payload.get('media_type') or '').strip().lower()
    preferred_instance = str(payload.get('preferred_instance') or '').strip().lower()
    fallback_instance = str(payload.get('fallback_instance') or '').strip().lower()
    event_payload = payload.get('payload') if isinstance(payload.get('payload'), dict) else {}

    if media_type not in {'movie', 'episode'}:
        return {'ok': False, 'reason': 'invalid_media_type'}
    if preferred_instance not in {'standard', '4k'} or fallback_instance not in {'standard', '4k'}:
        return {'ok': False, 'reason': 'invalid_fallback_instances'}

    context = _resolve_playback_context(session, event_payload)
    if media_type == 'movie':
        movie_rows = _find_movie_rows(
            session,
            tmdb_id=context.get('tmdb_id'),
            imdb_id=context.get('imdb_id'),
            file_path=context.get('file_path'),
        )
        active_rows = _active_rows_by_instance(movie_rows)
        preferred_row = active_rows.get(preferred_instance)
        if _preferred_movie_import_succeeded(preferred_row):
            return {'ok': True, 'skipped': 'preferred_imported', 'preferred_instance': preferred_instance}

        fallback_row = active_rows.get(fallback_instance)
        if fallback_row is None:
            return {'ok': True, 'skipped': 'fallback_no_active_row', 'fallback_instance': fallback_instance}

        result = _run_movie_search_for_row(session, fallback_row)
        return {
            'ok': True,
            'event': 'playback_fallback',
            'media_type': 'movie',
            'preferred_instance': preferred_instance,
            'fallback_instance': fallback_instance,
            'result': result,
        }

    sonarr_id, _ = _extract_series_ids(event_payload)
    series_rows = _find_series_rows(
        session,
        sonarr_id=sonarr_id,
        tvdb_id=context.get('tvdb_id'),
        imdb_id=context.get('imdb_id'),
        file_path=context.get('file_path'),
    )
    active_rows = _active_rows_by_instance(series_rows)
    preferred_row = active_rows.get(preferred_instance)
    if _preferred_episode_import_succeeded(session, preferred_row, event_payload):
        return {'ok': True, 'skipped': 'preferred_imported', 'preferred_instance': preferred_instance}

    fallback_row = active_rows.get(fallback_instance)
    if fallback_row is None:
        return {'ok': True, 'skipped': 'fallback_no_active_row', 'fallback_instance': fallback_instance}

    result = _run_episode_search_for_row(session, fallback_row, event_payload)
    return {
        'ok': True,
        'event': 'playback_fallback',
        'media_type': 'episode',
        'preferred_instance': preferred_instance,
        'fallback_instance': fallback_instance,
        'result': result,
    }


def process_playback_start_event(payload: dict[str, Any], instance: str | None = None) -> dict[str, Any]:
    session = get_session()
    try:
        context = _resolve_playback_context(session, payload)
        media_type = str(context.get('media_type') or 'unknown')

        logger.info(
            f"Playback routing source={instance or 'unknown'} media_type={media_type} kind={context.get('playback_kind')} path={context.get('file_path')}",
            extra={'emoji_type': 'playback'},
        )

        if media_type == 'movie':
            result = _process_movie_playback(session, payload, context, instance)
        elif media_type == 'episode':
            result = _process_episode_playback(session, payload, context, instance)
        else:
            result = {'ok': False, 'reason': 'unresolved_playback_media_type'}

        session.commit()
        logger.info(
            f"Processed playback_start media_type={media_type} result={result}",
            extra={'emoji_type': 'processing'},
        )
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
