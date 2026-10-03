from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, text

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
from services.library_future_semantics import (
    build_series_max_known_order_within_horizon,
    episode_is_future_for_playback_search,
    movie_is_future_for_playback_search,
)
from services.source_of_truth.status_intent import DisplayStatus, StatusIntent, StatusSource
from services.source_of_truth.status_orchestrator import StatusOrchestrator
from services.media_servers.jellyfin import get_jellyfin_file_path


PLAYBACK_FALLBACK_JOB_TYPE = 'playback_fallback'


def _instance_label_for_row(row: Any) -> str | None:
    """Telemetry label from the row's resolved instance_key."""
    key = _row_instance_key(row)
    return key or None


def _row_instance_key(row: Any) -> str:
    raw = str(getattr(row, 'instance_key', '') or '').strip().lower()
    arr_type = 'radarr' if hasattr(row, 'tmdbid') else 'sonarr'
    if raw:
        inst = settings.resolve_arr_instance(arr_type, instance_key=raw)
        if inst and inst.get('instance_key'):
            canonical = str(inst.get('instance_key')).strip().lower()
            if canonical:
                return canonical
        if raw in {'4k', 'standard', 'radarr_4k', 'radarr_std', 'sonarr_4k', 'sonarr_std'}:
            mapped = _legacy_label_to_key(arr_type, raw.replace('radarr_', '').replace('sonarr_', ''))
            if mapped:
                return mapped
        return raw
    instance_id = str(getattr(row, 'instance_id', '') or '').strip().lower()
    if instance_id:
        inst = settings.resolve_arr_instance(arr_type, instance_id=instance_id)
        if inst and inst.get('instance_key'):
            return str(inst.get('instance_key')).strip().lower()
    return ''


def _instance_key_at_rank(arr_type: str, index: int) -> str | None:
    item = settings.ranked_arr_instance(arr_type, index)
    if not item:
        return None
    key = str(item.get('instance_key') or '').strip().lower()
    return key or None


def _legacy_label_to_key(arr_type: str, label: str) -> str | None:
    """Map legacy standard/4k labels to ranked instance keys (index 0 / 1)."""
    normalized = str(label or '').strip().lower()
    instances = settings.arr_instances_for_type(arr_type)
    if not instances:
        return normalized or None
    if normalized == 'standard':
        key = _instance_key_at_rank(arr_type, 0)
        if key:
            return key
        return str(instances[0].get('instance_key') or '').strip().lower() or normalized
    if normalized == '4k':
        key = _instance_key_at_rank(arr_type, 1)
        if key:
            return key
        if len(instances) > 1:
            k = str(instances[1].get('instance_key') or '').strip().lower()
            if k:
                return k
        return str(instances[0].get('instance_key') or '').strip().lower() or normalized
    return normalized or None


def _coerce_instance_key(arr_type: str, value: str | None) -> str | None:
    """Accept an instance_key or legacy standard/4k label."""
    raw = str(value or '').strip().lower()
    if not raw:
        return None
    if raw in {'standard', '4k'}:
        return _legacy_label_to_key(arr_type, raw)
    return raw


_RESERVED_INSTANCE_SEARCH_MODES = frozenset({'match', 'primary', 'secondary', 'both'})


def _normalize_instance_search_mode(raw: Any) -> str:
    """Return match/both/primary/secondary or a concrete instance_key."""
    value = str(raw or 'match').strip().lower()
    return value or 'match'


def _tv_instance_mode() -> str:
    return _normalize_instance_search_mode(getattr(settings, 'TV_PLAYBACK_INSTANCE_MODE', 'match'))


def _movie_instance_mode() -> str:
    return _normalize_instance_search_mode(getattr(settings, 'MOVIE_PLAYBACK_INSTANCE_MODE', 'match'))


def _placeholder_search_mode(media_type: str) -> str:
    """Return MOVIE/TV_PLACEHOLDER_SEARCH_MODE (match/both/primary/secondary/instance_key)."""
    if media_type == 'movie':
        return _normalize_instance_search_mode(getattr(settings, 'MOVIE_PLACEHOLDER_SEARCH_MODE', 'match'))
    return _normalize_instance_search_mode(getattr(settings, 'TV_PLACEHOLDER_SEARCH_MODE', 'match'))


def _discover_instances_for_placeholder_mode(
    media_type: str,
) -> tuple[list[dict[str, Any]], str]:
    """Arr instances for Discover playback from PLACEHOLDER_SEARCH_MODE.

    Prefer-path / library destination matching is not applied: Discover stubs are
    not Arr-rooted library paths. Legacy ``match`` behaves like All instances.
    """
    arr_type = _arr_type_for_media(media_type)
    instances = list(settings.arr_instances_for_type(arr_type) or [])
    mode = _placeholder_search_mode(media_type)
    preference = _preference_from_mode(mode)
    by_key: dict[str, dict[str, Any]] = {}
    for inst in instances:
        key = str(inst.get('instance_key') or inst.get('key') or '').strip().lower()
        if key and key not in by_key:
            by_key[key] = inst
    if not by_key:
        return [], preference

    ordered_keys: list[str] = []
    ranking = (
        list(settings.movie_instance_ranking)
        if media_type == 'movie'
        else list(settings.tv_instance_ranking)
    )
    for key in ranking:
        k = str(key or '').strip().lower()
        if k in by_key and k not in ordered_keys:
            ordered_keys.append(k)
    for key in by_key:
        if key not in ordered_keys:
            ordered_keys.append(key)

    if preference == 'both':
        return [by_key[k] for k in ordered_keys], preference

    if preference == 'primary':
        key = _instance_key_at_rank(arr_type, 0)
        if key and key in by_key:
            return [by_key[key]], preference
        return [], preference

    if preference == 'secondary':
        key = _instance_key_at_rank(arr_type, 1)
        if key and key in by_key:
            return [by_key[key]], preference
        return [], preference

    coerced = _coerce_instance_key(arr_type, preference)
    if coerced and coerced in by_key:
        return [by_key[coerced]], preference
    return [], preference


def _select_forced_instance_rows(
    rows_by_instance: dict[str, Any],
    *,
    media_type: str,
    mode: str,
    selection_reason: str,
    root_match: str | None = None,
) -> dict[str, Any]:
    """Force search to primary/secondary role or a concrete instance_key."""
    normalized = str(mode or '').strip().lower()
    if normalized == 'primary':
        return _select_role_only_rows(
            rows_by_instance,
            media_type=media_type,
            role='primary',
            selection_reason=f'{selection_reason}_primary',
            root_match=root_match,
        )
    if normalized == 'secondary':
        return _select_role_only_rows(
            rows_by_instance,
            media_type=media_type,
            role='secondary',
            selection_reason=f'{selection_reason}_secondary',
            root_match=root_match,
        )
    return _select_preferred_key_rows(
        rows_by_instance,
        media_type=media_type,
        preferred_instance=normalized,
        selection_reason=f'{selection_reason}_instance',
        root_match=root_match,
    )


def _coerce_setting_bool(raw: Any, *, default: bool = False) -> bool:
    if raw is None:
        return bool(default)
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {'1', 'true', 'yes', 'on'}:
        return True
    if text in {'0', 'false', 'no', 'off', ''}:
        return False
    return bool(default)



def _parse_instance_key_setting(raw: Any) -> list[str]:
    """Parse a Play Actions instance-key JSON/list setting into normalized keys."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        text = str(raw).strip()
        if not text:
            return []
        try:
            import json

            parsed = json.loads(text)
            if isinstance(parsed, list):
                items = parsed
            elif isinstance(parsed, bool):
                return []
            else:
                items = [parsed]
        except Exception:
            items = [part.strip() for part in text.split(',') if part.strip()]
    out: list[str] = []
    for item in items:
        key = str(item or '').strip().lower()
        if key and key not in out:
            out.append(key)
    return out


def _play_actions_setting_name(media_type: str, kind: str) -> str:
    prefix = 'MOVIE' if media_type == 'movie' else 'TV'
    mapping = {
        'always': f'{prefix}_PLAY_PLACEHOLDER_ALWAYS_INSTANCES',
        'fallback': f'{prefix}_PLAY_PLACEHOLDER_FALLBACK_INSTANCES',
        'real_also': f'{prefix}_PLAY_REAL_ALSO_INSTANCES',
    }
    return mapping[kind]


def _play_actions_keys(media_type: str, kind: str) -> list[str]:
    return _parse_instance_key_setting(getattr(settings, _play_actions_setting_name(media_type, kind), '[]'))


def _effective_fallback_keys(media_type: str) -> list[str]:
    """Play Actions Fallback keys for this media type only.

    Empty means Fallback Off for that type. Do not consult the shared
    ENABLE_PLAYBACK_FALLBACK_SEARCH flag (it can be true because the other
    media type has Fallback rows).
    """
    return _play_actions_keys(media_type, 'fallback')


def _rank_filter_keys(media_type: str, wanted: set[str]) -> list[str]:
    """Return wanted keys in ARR Integrations list order."""
    from services.playback_routing import get_candidate_instances_for_movie, get_candidate_instances_for_tv

    ranking = (
        get_candidate_instances_for_movie()
        if media_type == 'movie'
        else get_candidate_instances_for_tv()
    )
    ordered: list[str] = []
    for key in ranking or []:
        normalized = str(key or '').strip().lower()
        if normalized and normalized in wanted and normalized not in ordered:
            ordered.append(normalized)
    for key in wanted:
        if key not in ordered:
            ordered.append(key)
    return ordered


def _fallback_timeout_minutes() -> int:
    try:
        return max(0, int(getattr(settings, 'PLAYBACK_FALLBACK_TIMEOUT_MINUTES', 30) or 30))
    except Exception:
        return 30


def _fallback_enabled(*, media_type: str | None = None) -> bool:
    """Delayed Fallback tries require a positive timeout and a non-empty per-type Fallback queue."""
    if _fallback_timeout_minutes() <= 0:
        return False
    if media_type in {'movie', 'tv', 'episode'}:
        mt = 'movie' if media_type == 'movie' else 'tv'
        return bool(_effective_fallback_keys(mt))
    return bool(_effective_fallback_keys('movie') or _effective_fallback_keys('tv'))


def _resolve_endpoint(content_type: str, *, instance_key: str | None = None) -> tuple[str, str]:
    arr_type = 'radarr' if content_type == 'movie' else 'sonarr'
    key = str(instance_key or '').strip().lower()
    if not key:
        return '', ''
    return settings.resolve_arr_endpoint(arr_type, instance_key=key)


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


def _match_instance_key_from_path(path: str | None, *, arr_type: str) -> str | None:
    """Match a played file path to an instance_key.

    Returns a single key, ``all`` when ambiguous / shared, or ``None`` when unmatched.
    Longest matching dest folder wins when multiple map rows apply.
    Unmapped paths under the default library folder map to ranking index 0.
    """
    if not path:
        return None
    arr = str(arr_type or '').strip().lower()
    if arr not in {'radarr', 'sonarr'}:
        return None

    try:
        from services.library_destinations import parse_library_destination_map

        scored: list[tuple[int, str]] = []
        for row in parse_library_destination_map():
            if str(row.get('arr_type') or '').lower() != arr:
                continue
            dest = str(row.get('dest_folder') or '')
            norm_dest = _normalize_path(dest)
            if not norm_dest or not _path_is_within_root(path, dest):
                continue
            key = str(row.get('instance_key') or '').strip().lower()
            if not key:
                continue
            scored.append((len(norm_dest), key))

        if scored:
            best_len = max(length for length, _ in scored)
            unique = list(dict.fromkeys(key for length, key in scored if length == best_len))
            if len(unique) == 1:
                return unique[0]
            if len(unique) > 1:
                return 'all'
            return None

        matched: list[str] = []
        if arr == 'sonarr':
            primary_folder = str(getattr(settings, 'TV_LIBRARY_FOLDER', '') or '')
        else:
            primary_folder = str(getattr(settings, 'MOVIE_LIBRARY_FOLDER', '') or '')

        default_key = _instance_key_at_rank(arr, 0)
        # Unmapped paths under the default dest use the first ranked instance.
        # Other instances require the dest map for path→instance matching.
        if default_key and primary_folder and _path_is_within_root(path, primary_folder):
            matched.append(default_key)

        unique = list(dict.fromkeys(matched))
        if len(unique) == 1:
            return unique[0]
        if len(unique) > 1:
            return 'all'
        return None
    except Exception:
        matched = []
        default_key = _instance_key_at_rank(arr, 0)
        if arr == 'sonarr':
            default_folder = getattr(settings, 'TV_LIBRARY_FOLDER', '')
        else:
            default_folder = getattr(settings, 'MOVIE_LIBRARY_FOLDER', '')
        if default_key and default_folder and _path_is_within_root(path, default_folder):
            matched.append(default_key)
        unique = list(dict.fromkeys(matched))
        if len(unique) == 1:
            return unique[0]
        if len(unique) > 1:
            return 'all'
        return None


def _match_tv_instance_from_path(path: str | None) -> str | None:
    """Match a played TV path to an instance_key (or ``all`` / ``None``)."""
    return _match_instance_key_from_path(path, arr_type='sonarr')


def _match_movie_instance_from_path(path: str | None) -> str | None:
    """Match a played movie path to an instance_key (or ``all`` / ``None``)."""
    return _match_instance_key_from_path(path, arr_type='radarr')


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


def _extract_plex_rating_key(payload: dict[str, Any]) -> str | None:
    media = payload.get('media') if isinstance(payload.get('media'), dict) else {}
    media_ids = media.get('ids') if isinstance(media.get('ids'), dict) else {}
    metadata = payload.get('metadata') if isinstance(payload.get('metadata'), dict) else {}
    plex_meta = payload.get('Metadata') if isinstance(payload.get('Metadata'), dict) else {}
    payload_ids = payload.get('ids') if isinstance(payload.get('ids'), dict) else {}
    data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
    data_media = data.get('media') if isinstance(data.get('media'), dict) else {}

    candidates = [
        media_ids.get('plex'),
        media_ids.get('ratingKey'),
        media_ids.get('rating_key'),
        media.get('ratingKey'),
        media.get('rating_key'),
        payload_ids.get('plex'),
        payload_ids.get('ratingKey'),
        payload_ids.get('rating_key'),
        payload.get('ratingKey'),
        payload.get('rating_key'),
        payload.get('plex_id'),
        metadata.get('ratingKey'),
        metadata.get('rating_key'),
        plex_meta.get('ratingKey'),
        data_media.get('ratingKey'),
        data_media.get('rating_key'),
    ]
    for val in candidates:
        if val is not None:
            s = str(val).strip()
            if s and s != "0":
                return s
    return None


def _extract_jellyfin_item_id(payload: dict[str, Any]) -> str | None:
    item = payload.get('Item') if isinstance(payload.get('Item'), dict) else {}
    media = payload.get('media') if isinstance(payload.get('media'), dict) else {}
    media_ids = media.get('ids') if isinstance(media.get('ids'), dict) else {}
    payload_ids = payload.get('ids') if isinstance(payload.get('ids'), dict) else {}
    candidates = [
        payload.get('ItemId'),
        payload.get('itemId'),
        payload.get('jellyfin_id'),
        payload_ids.get('jellyfin'),
        media_ids.get('jellyfin'),
        item.get('Id'),
        item.get('id'),
    ]
    for val in candidates:
        if val is not None:
            s = str(val).strip()
            if s:
                return s
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


def _movie_matches_file_path(session, movie: Movie, file_path: str | None) -> bool:
    fp = _normalize_path(file_path)
    if not fp:
        return False
    if _normalize_path(getattr(movie, 'radarr_filepath', None)) == fp:
        return True
    if _normalize_path(getattr(movie, 'placeholder_filepath', None)) == fp:
        return True
    mid = int(getattr(movie, 'id', 0) or 0)
    if not mid:
        return False
    return bool(
        session.query(Placeholder.id)
        .filter(Placeholder.movie_id == mid, Placeholder.path == fp)
        .first()
    )


def _episode_matches_file_path(session, episode: Episode, file_path: str | None) -> bool:
    fp = _normalize_path(file_path)
    if not fp:
        return False
    if _normalize_path(getattr(episode, 'sonarr_filepath', None)) == fp:
        return True
    if _normalize_path(getattr(episode, 'placeholder_filepath', None)) == fp:
        return True
    eid = int(getattr(episode, 'id', 0) or 0)
    if not eid:
        return False
    return bool(
        session.query(Placeholder.id)
        .filter(Placeholder.episode_id == eid, Placeholder.path == fp)
        .first()
    )


def _movies_for_media_id_stamp(session, movie_rows: list[Movie], context: dict[str, Any]) -> list[Movie]:
    """Restrict plex/jellyfin ID seeding to the path-matched Arr row when possible."""
    path_info = context.get('path_info') if isinstance(context.get('path_info'), dict) else {}
    movie_id = path_info.get('movie_id')
    if movie_id is not None:
        try:
            want = int(movie_id)
        except (TypeError, ValueError):
            want = None
        if want is not None:
            matched = [row for row in movie_rows if int(getattr(row, 'id', 0) or 0) == want]
            if matched:
                return matched
    file_path = context.get('file_path')
    if not _normalize_path(file_path):
        return []
    return [row for row in movie_rows if _movie_matches_file_path(session, row, file_path)]


def _episodes_for_media_id_stamp(
    session,
    episodes: list[Episode],
    *,
    file_path: str | None,
    season_number: int | None = None,
    episode_number: int | None = None,
) -> list[Episode]:
    """Prefer path-matched episodes; otherwise keep S/E (or single-target) fallback."""
    if _normalize_path(file_path):
        path_matched = [ep for ep in episodes if _episode_matches_file_path(session, ep, file_path)]
        if path_matched:
            return path_matched
        return []
    out: list[Episode] = []
    for ep in episodes:
        if season_number is not None and episode_number is not None:
            if ep.season_number == season_number and ep.episode_number == episode_number:
                out.append(ep)
        elif len(episodes) == 1:
            out.append(ep)
    return out


def _stamp_movie_media_ids(session, rows: list[Movie], *, plex_key: str, jelly_key: str) -> None:
    if not plex_key and not jelly_key:
        return
    for row in rows:
        changed = False
        if plex_key and (row.plex_id != plex_key or row.plex_dummy_id != plex_key):
            row.plex_id = plex_key
            row.plex_dummy_id = plex_key
            changed = True
        if jelly_key and getattr(row, 'jellyfin_id', None) != jelly_key:
            row.jellyfin_id = jelly_key
            changed = True
        if changed:
            row.updated_at = func.now()
            session.add(row)


def _stamp_episode_media_ids(session, episodes: list[Episode], *, plex_key: str, jelly_key: str) -> None:
    if not plex_key and not jelly_key:
        return
    for ep in episodes:
        changed = False
        if plex_key and (ep.plex_id != plex_key or ep.plex_dummy_id != plex_key):
            ep.plex_id = plex_key
            ep.plex_dummy_id = plex_key
            changed = True
        if jelly_key and getattr(ep, 'jellyfin_id', None) != jelly_key:
            ep.jellyfin_id = jelly_key
            changed = True
        if changed:
            ep.updated_at = func.now()
            session.add(ep)


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
            'matched_instance': _instance_label_for_row(movie_row),
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
            'matched_instance': _instance_label_for_row(series_row) if series_row else None,
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
                    'matched_instance': _instance_label_for_row(movie),
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
                    'matched_instance': _instance_label_for_row(series) if series else None,
                }
        # Series / season density stubs: Placeholder has series_id, no episode_id.
        if getattr(ph_row, 'series_id', None):
            from services.source_of_truth.tv_density import (
                PLACEHOLDER_KIND_SEASON_STUB,
                PLACEHOLDER_KIND_SERIES_STUB,
            )

            series = session.query(Series).filter(Series.id == int(ph_row.series_id)).first()
            if series:
                kind = str(getattr(ph_row, 'placeholder_kind', '') or '')
                season_number = 1
                episode_number = 1
                if kind == PLACEHOLDER_KIND_SEASON_STUB and getattr(ph_row, 'season_id', None):
                    season = session.query(Season).filter(Season.id == int(ph_row.season_id)).first()
                    if season is not None and getattr(season, 'season_number', None) is not None:
                        season_number = int(season.season_number)
                return {
                    'media_type': 'episode',
                    'playback_kind': 'placeholder',
                    'tvdb_id': int(series.tvdbid) if getattr(series, 'tvdbid', None) else None,
                    'season_number': season_number,
                    'episode_number': episode_number,
                    'series_id': int(series.id),
                    'matched_instance': _instance_label_for_row(series),
                    'file_path': file_path,
                    'density_stub_kind': (
                        'season' if kind == PLACEHOLDER_KIND_SEASON_STUB else 'series'
                        if kind in {PLACEHOLDER_KIND_SERIES_STUB, PLACEHOLDER_KIND_SEASON_STUB}
                        else None
                    ),
                }

    return {'media_type': 'unknown', 'playback_kind': 'unknown'}


def _playback_kind_from_episode_row(episode_row: Episode) -> str:
    """Infer real vs placeholder from Sonarr/FS flags when webhook path did not match stored paths."""
    hf = bool(getattr(episode_row, 'has_file', False))
    hp = bool(getattr(episode_row, 'has_placeholder', False))
    if hf and not hp:
        return 'real'
    if hp and not hf:
        return 'placeholder'
    if hf and hp:
        return 'real'
    sn = _normalize_path(getattr(episode_row, 'sonarr_filepath', None))
    ph = _normalize_path(getattr(episode_row, 'placeholder_filepath', None))
    if sn and not ph:
        return 'real'
    if ph and not sn:
        return 'placeholder'
    return 'unknown'


def _playback_kind_from_movie_row(movie_row: Movie) -> str:
    hf = bool(getattr(movie_row, 'has_file', False))
    hp = bool(getattr(movie_row, 'has_placeholder', False))
    if hf and not hp:
        return 'real'
    if hp and not hf:
        return 'placeholder'
    if hf and hp:
        return 'real'
    rp = _normalize_path(getattr(movie_row, 'radarr_filepath', None))
    pp = _normalize_path(getattr(movie_row, 'placeholder_filepath', None))
    if rp and not pp:
        return 'real'
    if pp and not rp:
        return 'placeholder'
    return 'unknown'


def _try_resolve_episode_from_catalog_ids(
    session,
    *,
    tvdb_id: int | None,
    sonarr_series_id: int | None,
    season_number: int | None,
    episode_number: int | None,
) -> dict[str, Any] | None:
    if season_number is None or episode_number is None:
        return None
    if tvdb_id is None and sonarr_series_id is None:
        return None

    def _pack(episode_row: Episode) -> dict[str, Any]:
        series_row = episode_row.season.series if episode_row.season else None
        return {
            'media_type': 'episode',
            'playback_kind': _playback_kind_from_episode_row(episode_row),
            'tvdb_id': int(series_row.tvdbid) if series_row and getattr(series_row, 'tvdbid', None) else None,
            'season_number': int(episode_row.season.season_number) if episode_row.season else None,
            'episode_number': int(episode_row.episode_number) if getattr(episode_row, 'episode_number', None) is not None else None,
            'series_id': int(series_row.id) if series_row else None,
            'matched_instance': _instance_label_for_row(series_row) if series_row else None,
        }

    q = (
        session.query(Episode)
        .join(Season, Episode.season_id == Season.id)
        .join(Series, Season.series_id == Series.id)
        .filter(
            Episode.is_deleted == False,  # noqa: E712
            Series.is_deleted == False,  # noqa: E712
            Season.season_number == season_number,
            Episode.episode_number == episode_number,
        )
    )
    if tvdb_id is not None:
        q = q.filter(Series.tvdbid == int(tvdb_id))
    else:
        q = q.filter(Series.sonarrid == int(sonarr_series_id))
    rows = q.all()
    if not rows and tvdb_id is not None:
        # Tracearr/Plex often put the *episode* TVDB Guid on the item, not the series id.
        rows = (
            session.query(Episode)
            .join(Season, Episode.season_id == Season.id)
            .join(Series, Season.series_id == Series.id)
            .filter(
                Episode.is_deleted == False,  # noqa: E712
                Series.is_deleted == False,  # noqa: E712
                Episode.sonarr_episode_tvdbid == int(tvdb_id),
                Season.season_number == season_number,
                Episode.episode_number == episode_number,
            )
            .all()
        )
    if not rows:
        return None
    if len(rows) > 1:
        ph_candidates = [e for e in rows if bool(e.has_placeholder) and not bool(e.has_file)]
        episode_row = ph_candidates[0] if ph_candidates else rows[0]
        logger.debug(
            'playback catalog id match: multiple episodes for tvdb/sonarr+S+E; '
            f'picked episode_id={getattr(episode_row, "id", None)}',
            extra={'emoji_type': 'debug'},
        )
    else:
        episode_row = rows[0]
    return _pack(episode_row)


def _try_resolve_movie_from_catalog_ids(session, *, tmdb_id: int | None, imdb_id: str | None) -> dict[str, Any] | None:
    if tmdb_id is None and not imdb_id:
        return None
    q = session.query(Movie).filter(Movie.is_deleted == False)  # noqa: E712
    if tmdb_id is not None:
        q = q.filter(Movie.tmdbid == int(tmdb_id))
    else:
        q = q.filter(Movie.imdbid == imdb_id)
    rows = q.all()
    if not rows:
        return None
    if len(rows) > 1:
        ph_candidates = [m for m in rows if bool(m.has_placeholder) and not bool(m.has_file)]
        movie_row = ph_candidates[0] if ph_candidates else rows[0]
        logger.debug(
            'playback catalog id match: multiple movies for tmdb/imdb; '
            f'picked movie_id={getattr(movie_row, "id", None)}',
            extra={'emoji_type': 'debug'},
        )
    else:
        movie_row = rows[0]
    return {
        'media_type': 'movie',
        'playback_kind': _playback_kind_from_movie_row(movie_row),
        'tmdb_id': int(movie_row.tmdbid) if getattr(movie_row, 'tmdbid', None) else None,
        'movie_id': int(movie_row.id),
        'matched_instance': _instance_label_for_row(movie_row),
    }


def _merge_path_info_with_catalog_ids(
    session,
    path_info: dict[str, Any],
    *,
    tmdb_id: int | None,
    tvdb_id: int | None,
    imdb_id: str | None,
    sonarr_series_id: int | None,
    season_number: int | None,
    episode_number: int | None,
    declared_media_type: str | None,
) -> dict[str, Any]:
    """When path equality fails (Docker / different roots), resolve row + playback_kind from catalog IDs."""
    pk = str(path_info.get('playback_kind') or 'unknown')
    if pk not in ('unknown', '', 'none', 'None'):
        return path_info
    merged = dict(path_info)

    episode_first = declared_media_type == 'episode' or (
        season_number is not None and episode_number is not None and (tvdb_id is not None or sonarr_series_id is not None)
    )
    if episode_first:
        cat = _try_resolve_episode_from_catalog_ids(
            session,
            tvdb_id=tvdb_id,
            sonarr_series_id=sonarr_series_id,
            season_number=season_number,
            episode_number=episode_number,
        )
        if cat:
            logger.info(
                f"playback catalog match episode tvdb={tvdb_id} sonarr_series={sonarr_series_id} "
                f"S{season_number}E{episode_number} kind={cat.get('playback_kind')}",
                extra={'emoji_type': 'playback'},
            )
            merged.update(cat)
            return merged

    # Movie: avoid treating a *series* TMDB on episode payloads as a movie id.
    if declared_media_type == 'movie' or (
        declared_media_type != 'episode' and tvdb_id is None and season_number is None and episode_number is None and (tmdb_id is not None or imdb_id)
    ):
        cat = _try_resolve_movie_from_catalog_ids(session, tmdb_id=tmdb_id, imdb_id=imdb_id)
        if cat:
            logger.info(
                f"playback catalog match movie tmdb={tmdb_id} imdb={imdb_id} kind={cat.get('playback_kind')}",
                extra={'emoji_type': 'playback'},
            )
            merged.update(cat)
            return merged

    return merged


def _payload_indicates_placeholdarr_stub(payload: dict[str, Any]) -> bool:
    """True when Tracearr/Tautulli text marks a Placeholdarr density placeholder play."""
    from services.source_of_truth.tv_density import text_marks_density_placeholder

    media = payload.get('media') if isinstance(payload.get('media'), dict) else {}
    data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
    data_media = data.get('media') if isinstance(data.get('media'), dict) else {}
    chunks = [
        media.get('subtitle'),
        media.get('title'),
        data_media.get('subtitle'),
        data_media.get('title'),
        payload.get('title'),
    ]
    blob = ' '.join(str(c or '') for c in chunks)
    return text_marks_density_placeholder(blob)


def _enrich_ids_and_path_from_plex_rating_key(
    *,
    plex_id: str | None,
    file_path: str | None,
    tmdb_id: int | None,
    tvdb_id: int | None,
    imdb_id: str | None,
    season_number: int | None,
    episode_number: int | None,
    declared_media_type: str | None,
) -> dict[str, Any]:
    """Fill missing path / Guids / S/E from Plex when the webhook is thin."""
    out: dict[str, Any] = {
        'file_path': file_path,
        'tmdb_id': tmdb_id,
        'tvdb_id': tvdb_id,
        'imdb_id': imdb_id,
        'season_number': season_number,
        'episode_number': episode_number,
        'declared_media_type': declared_media_type,
    }
    needs_ids = (tmdb_id is None and tvdb_id is None and not imdb_id) or not file_path
    if not plex_id or not needs_ids:
        return out
    try:
        from services.media_servers.plex import fetch_plex_item_playback_hints

        hints = fetch_plex_item_playback_hints(plex_id)
    except Exception as exc:
        logger.debug(
            f"Plex ratingKey enrichment skipped: {type(exc).__name__}: {exc}",
            extra={'emoji_type': 'debug'},
        )
        return out
    if not hints:
        return out
    logger.info(
        f"playback Plex ratingKey enrich rating_key={plex_id} "
        f"tvdb={hints.get('tvdb_id')} tmdb={hints.get('tmdb_id')} "
        f"path={'yes' if hints.get('file_path') else 'no'}",
        extra={'emoji_type': 'playback'},
    )
    if not out['file_path'] and hints.get('file_path'):
        out['file_path'] = _normalize_path(hints['file_path'])
    if out['tmdb_id'] is None and hints.get('tmdb_id') is not None:
        out['tmdb_id'] = int(hints['tmdb_id'])
    if out['tvdb_id'] is None and hints.get('tvdb_id') is not None:
        out['tvdb_id'] = int(hints['tvdb_id'])
    if not out['imdb_id'] and hints.get('imdb_id'):
        out['imdb_id'] = str(hints['imdb_id'])
    if out['season_number'] is None and hints.get('season_number') is not None:
        out['season_number'] = int(hints['season_number'])
    if out['episode_number'] is None and hints.get('episode_number') is not None:
        out['episode_number'] = int(hints['episode_number'])
    if not out['declared_media_type'] and hints.get('media_type') in {'movie', 'episode'}:
        out['declared_media_type'] = str(hints['media_type'])
    return out


def _upgrade_unknown_kind_via_density_stub(
    session,
    path_info: dict[str, Any],
    payload: dict[str, Any],
    *,
    season_number: int | None,
    episode_number: int | None,
) -> dict[str, Any]:
    """When catalog match leaves kind unknown, treat active density stubs as placeholder."""
    pk = str(path_info.get('playback_kind') or 'unknown')
    if pk not in ('unknown', '', 'none', 'None'):
        return path_info

    series_id = path_info.get('series_id')
    tvdb_id = path_info.get('tvdb_id')
    imdb_id = _extract_imdb_id(payload)
    series_row: Series | None = None
    if series_id is not None:
        series_row = session.query(Series).filter(Series.id == int(series_id), Series.is_deleted == False).first()  # noqa: E712
    if series_row is None and tvdb_id is not None:
        series_row = (
            session.query(Series)
            .filter(Series.tvdbid == int(tvdb_id), Series.is_deleted == False)  # noqa: E712
            .order_by(Series.id.desc())
            .first()
        )
    if series_row is None and imdb_id:
        series_row = (
            session.query(Series)
            .filter(Series.imdbid == imdb_id, Series.is_deleted == False)  # noqa: E712
            .order_by(Series.id.desc())
            .first()
        )
    if series_row is None:
        return path_info

    from services.source_of_truth.tv_density import density_uses_season_stub, density_uses_series_stub

    stub_marker = _payload_indicates_placeholdarr_stub(payload)
    season_stub = None
    stub_path = path_info.get('file_path')
    if density_uses_season_stub(series_row):
        if stub_path:
            season_stub = _find_active_season_density_stub(
                session,
                series_id=int(series_row.id),
                file_path=stub_path,
            )
        if season_stub is None and season_number is not None and (stub_marker or episode_number == 1):
            season = (
                session.query(Season)
                .filter(Season.series_id == int(series_row.id), Season.season_number == int(season_number))
                .first()
            )
            if season is not None:
                season_stub = _find_active_season_density_stub(session, season_id=int(season.id))

    series_stub = None
    if season_stub is None and density_uses_series_stub(series_row):
        series_stub = _find_active_series_density_stub(session, int(series_row.id))
    if season_stub is None and series_stub is None:
        return path_info

    # Prefer explicit stub markers; otherwise only when SxxE01 matches stub convention.
    if not stub_marker:
        if episode_number != 1:
            return path_info
        if season_stub is None and (season_number not in (None, 1)):
            return path_info
        if season_stub is not None and season_number is not None:
            season = session.query(Season).filter(Season.id == int(season_stub.season_id)).first() if getattr(season_stub, 'season_id', None) else None
            if season is not None and int(getattr(season, 'season_number', -1)) != int(season_number):
                return path_info

    stub = season_stub or series_stub
    merged = dict(path_info)
    merged['playback_kind'] = 'placeholder'
    merged['media_type'] = 'episode'
    merged['series_id'] = int(series_row.id)
    if getattr(series_row, 'tvdbid', None):
        merged['tvdb_id'] = int(series_row.tvdbid)
    merged['matched_instance'] = _instance_label_for_row(series_row)
    if stub and getattr(stub, 'path', None):
        merged['file_path'] = _normalize_path(stub.path) or stub.path
    merged['density_stub_kind'] = 'season' if season_stub else 'series'
    logger.info(
        f"playback density stub kind upgrade series_id={series_row.id} "
        f"kind={merged['density_stub_kind']} marker={stub_marker}",
        extra={'emoji_type': 'playback'},
    )
    return merged


def _resolve_playback_context(session, payload: dict[str, Any]) -> dict[str, Any]:
    tmdb_id = _extract_movie_tmdb_id(payload)
    sonarr_series_id, tvdb_id = _extract_series_ids(payload)
    imdb_id = _extract_imdb_id(payload)
    season_number, episode_number = _extract_season_episode(payload)
    file_path = _extract_file_path(payload)
    plex_id = _extract_plex_rating_key(payload)
    jellyfin_id = _extract_jellyfin_item_id(payload)
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

    enriched = _enrich_ids_and_path_from_plex_rating_key(
        plex_id=plex_id,
        file_path=file_path,
        tmdb_id=tmdb_id,
        tvdb_id=tvdb_id,
        imdb_id=imdb_id,
        season_number=season_number,
        episode_number=episode_number,
        declared_media_type=declared_media_type,
    )
    file_path = enriched['file_path']
    tmdb_id = enriched['tmdb_id']
    tvdb_id = enriched['tvdb_id']
    imdb_id = enriched['imdb_id']
    season_number = enriched['season_number']
    episode_number = enriched['episode_number']
    declared_media_type = enriched['declared_media_type']

    path_info = _resolve_media_from_path(session, file_path)
    path_info = _merge_path_info_with_catalog_ids(
        session,
        path_info,
        tmdb_id=tmdb_id,
        tvdb_id=tvdb_id,
        imdb_id=imdb_id,
        sonarr_series_id=sonarr_series_id,
        season_number=season_number,
        episode_number=episode_number,
        declared_media_type=declared_media_type,
    )
    path_info = _upgrade_unknown_kind_via_density_stub(
        session,
        path_info,
        payload,
        season_number=season_number if season_number is not None else path_info.get('season_number'),
        episode_number=episode_number if episode_number is not None else path_info.get('episode_number'),
    )

    if tmdb_id is None and path_info.get('tmdb_id') is not None:
        tmdb_id = int(path_info['tmdb_id'])
    if tvdb_id is None and path_info.get('tvdb_id') is not None:
        tvdb_id = int(path_info['tvdb_id'])
    if season_number is None and path_info.get('season_number') is not None:
        season_number = int(path_info['season_number'])
    if episode_number is None and path_info.get('episode_number') is not None:
        episode_number = int(path_info['episode_number'])
    if not file_path and path_info.get('file_path'):
        file_path = _normalize_path(path_info.get('file_path'))

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
        'plex_id': plex_id,
        'jellyfin_id': jellyfin_id,
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
        ph_rows = session.query(Placeholder).filter(Placeholder.path == file_path).all()
        ph_episode_ids = [
            int(row.episode_id)
            for row in ph_rows
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
        ph_series_ids = [
            int(row.series_id)
            for row in ph_rows
            if getattr(row, 'series_id', None) and not getattr(row, 'episode_id', None)
        ]
        if ph_series_ids:
            rows.extend(session.query(Series).filter(Series.id.in_(ph_series_ids)).all())
    return _dedupe_rows(rows)


def _active_rows_by_instance(rows: list[Any]) -> dict[str, Any]:
    active: dict[str, Any] = {}
    for row in rows:
        if bool(getattr(row, 'is_deleted', False)):
            continue
        key = _row_instance_key(row)
        if not key:
            continue
        if getattr(row, 'instance_key', None) != key:
            row.instance_key = key
        active[key] = row
    return active


def _ranked_keys_with_rows(media_type: str, rows_by_instance: dict[str, Any]) -> list[str]:
    from services.playback_routing import get_candidate_instances_for_movie, get_candidate_instances_for_tv

    ranking = (
        get_candidate_instances_for_movie()
        if media_type == 'movie'
        else get_candidate_instances_for_tv()
    )
    ordered: list[str] = []
    for key in ranking or []:
        normalized = str(key or '').strip().lower()
        if normalized and normalized in rows_by_instance and normalized not in ordered:
            ordered.append(normalized)
    for key in rows_by_instance.keys():
        if key not in ordered:
            ordered.append(key)
    return ordered


def _fallback_queue(
    preferred: str | None,
    media_type: str,
    rows_by_instance: dict[str, Any],
    *,
    fallback_keys: list[str] | None = None,
) -> list[str]:
    """Return Fallback try order after preferred (ARR Integrations list order)."""
    preferred_key = str(preferred or '').strip().lower() or None
    wanted = {str(k or '').strip().lower() for k in (fallback_keys or []) if str(k or '').strip()}
    wanted.discard(preferred_key or '')
    return [key for key in _rank_filter_keys(media_type, wanted) if key in rows_by_instance]


def _selection_result(
    rows_by_instance: dict[str, Any],
    *,
    chosen_keys: list[str],
    preferred_instance: str | None,
    fallback_instances: list[str],
    selection_reason: str,
    immediate_fallback: bool = False,
    root_match: str | None = None,
    qualifying_instances: list[str] | None = None,
) -> dict[str, Any]:
    qualifying = list(qualifying_instances) if qualifying_instances is not None else list(rows_by_instance.keys())
    fallbacks = [str(k).strip().lower() for k in fallback_instances if str(k or '').strip()]
    chosen = [str(k).strip().lower() for k in chosen_keys if str(k or '').strip() and k in rows_by_instance]
    return {
        'rows': [rows_by_instance[key] for key in chosen],
        'chosen_instances': chosen,
        'qualifying_instances': qualifying,
        'preferred_instance': preferred_instance,
        'fallback_instance': fallbacks[0] if fallbacks else None,
        'fallback_instances': fallbacks,
        'selection_reason': selection_reason,
        'immediate_fallback': immediate_fallback,
        'root_match': root_match,
    }


def _select_play_actions_placeholder_rows(
    rows_by_instance: dict[str, Any],
    *,
    media_type: str,
    file_path: str | None,
) -> dict[str, Any]:
    """Path-matched first, plus Always now; Fallback becomes the delayed/immediate queue."""
    match_fn = _match_movie_instance_from_path if media_type == 'movie' else _match_tv_instance_from_path
    root_match = match_fn(file_path)
    ranked = _ranked_keys_with_rows(media_type, rows_by_instance)
    always = set(_play_actions_keys(media_type, 'always'))
    fallback = _effective_fallback_keys(media_type)
    always_ordered = [key for key in _rank_filter_keys(media_type, always) if key in rows_by_instance]

    unique_path = (
        str(root_match).strip().lower()
        if root_match and root_match not in {'all', 'both'}
        else None
    )

    if unique_path and unique_path in rows_by_instance:
        chosen = [unique_path]
        for key in always_ordered:
            if key not in chosen:
                chosen.append(key)
        fallbacks = _fallback_queue(
            unique_path,
            media_type,
            rows_by_instance,
            fallback_keys=fallback,
        )
        fallbacks = [key for key in fallbacks if key not in chosen]
        return _selection_result(
            rows_by_instance,
            chosen_keys=chosen,
            preferred_instance=unique_path,
            fallback_instances=fallbacks,
            selection_reason='placeholder_path_match_play_actions',
            root_match=root_match,
            qualifying_instances=ranked,
        )

    # Path unclear or path-matched Arr has no active row: Always now; first Fallback immediate.
    chosen = list(always_ordered)
    fallback_with_rows = [
        key
        for key in _rank_filter_keys(media_type, set(fallback))
        if key in rows_by_instance and key not in chosen
    ]
    immediate = False
    preferred: str | None = unique_path
    fallbacks: list[str] = []
    if fallback_with_rows:
        head, *tail = fallback_with_rows
        chosen.append(head)
        preferred = head
        fallbacks = list(tail)
        immediate = bool(unique_path)  # path existed but no row, or ambiguous with fallback
        reason = (
            'placeholder_path_missing_immediate_fallback'
            if unique_path
            else 'placeholder_ambiguous_play_actions'
        )
    elif chosen:
        preferred = chosen[0]
        reason = (
            'placeholder_path_missing_always_only'
            if unique_path
            else 'placeholder_ambiguous_always_only'
        )
    else:
        # Always/Fallback all Off (or no active rows): do not invent a first-ranked target.
        return _selection_result(
            rows_by_instance,
            chosen_keys=[],
            preferred_instance=unique_path,
            fallback_instances=[],
            selection_reason=(
                'placeholder_play_actions_all_off' if ranked else 'placeholder_no_active_rows'
            ),
            root_match=root_match,
            qualifying_instances=ranked,
        )

    return _selection_result(
        rows_by_instance,
        chosen_keys=chosen,
        preferred_instance=preferred,
        fallback_instances=fallbacks,
        selection_reason=reason,
        immediate_fallback=immediate,
        root_match=root_match,
        qualifying_instances=ranked,
    )


def _select_placeholder_rows(
    rows_by_instance: dict[str, Any],
    *,
    media_type: str,
    file_path: str | None,
) -> dict[str, Any]:
    """Select which Arr instance(s) to search when a placeholder plays."""
    return _select_play_actions_placeholder_rows(
        rows_by_instance,
        media_type=media_type,
        file_path=file_path,
    )


def _select_tv_real_rows(rows_by_instance: dict[str, Any], file_path: str | None) -> dict[str, Any]:
    """Real TV play: path-matched Sonarr plus Real file · Always; ambiguous follows Placeholder play."""
    root_match = _match_tv_instance_from_path(file_path)
    ranked = _ranked_keys_with_rows('tv', rows_by_instance)
    real_also = set(_play_actions_keys('tv', 'real_also'))
    also_ordered = [key for key in _rank_filter_keys('tv', real_also) if key in rows_by_instance]
    unique_path = (
        str(root_match).strip().lower()
        if root_match and root_match not in {'all', 'both'}
        else None
    )

    if unique_path and unique_path in rows_by_instance:
        chosen: list[str] = [unique_path]
        for key in also_ordered:
            if key not in chosen:
                chosen.append(key)
        return _selection_result(
            rows_by_instance,
            chosen_keys=chosen,
            preferred_instance=unique_path,
            fallback_instances=[],
            selection_reason='tv_real_play_actions',
            root_match=root_match,
            qualifying_instances=ranked,
        )

    # Path unclear or path-matched Arr inactive: same Always / Fallback rules as Placeholder play.
    return _select_play_actions_placeholder_rows(
        rows_by_instance,
        media_type='tv',
        file_path=file_path,
    )


def _select_movie_real_rows(rows_by_instance: dict[str, Any], file_path: str | None) -> dict[str, Any]:
    """Real movie play: Real file · Always when path is clear; ambiguous follows Placeholder play."""
    root_match = _match_movie_instance_from_path(file_path)
    ranked = _ranked_keys_with_rows('movie', rows_by_instance)
    real_also = set(_play_actions_keys('movie', 'real_also'))
    unique_path = (
        str(root_match).strip().lower()
        if root_match and root_match not in {'all', 'both'}
        else None
    )

    if unique_path and unique_path in rows_by_instance:
        # Path-matched already has the file; only action Real file · Always instances.
        chosen = [key for key in _rank_filter_keys('movie', real_also) if key in rows_by_instance]
        preferred = chosen[0] if chosen else None
        return _selection_result(
            rows_by_instance,
            chosen_keys=chosen,
            preferred_instance=preferred,
            fallback_instances=[],
            selection_reason='movie_real_play_actions' if chosen else 'movie_real_noop',
            root_match=root_match,
            qualifying_instances=ranked,
        )

    # Path unclear or path-matched Arr inactive: same Always / Fallback rules as Placeholder play.
    return _select_play_actions_placeholder_rows(
        rows_by_instance,
        media_type='movie',
        file_path=file_path,
    )


def _play_mode() -> str:
    mode = str(getattr(settings, 'TV_PLAY_MODE', 'episode') or 'episode').strip().lower()
    return mode if mode in {'episode', 'season', 'series'} else 'episode'


def _episode_lookahead() -> int:
    try:
        return max(1, int(getattr(settings, 'EPISODES_LOOKAHEAD', 3) or 3))
    except Exception:
        return 5


def _find_active_series_density_stub(session, series_id: int) -> Placeholder | None:
    from services.source_of_truth.tv_density import PLACEHOLDER_KIND_SERIES_STUB

    return (
        session.query(Placeholder)
        .filter(
            Placeholder.series_id == int(series_id),
            Placeholder.placeholder_kind == PLACEHOLDER_KIND_SERIES_STUB,
            Placeholder.episode_id.is_(None),
            Placeholder.movie_id.is_(None),
            Placeholder.has_placeholder == True,  # noqa: E712
        )
        .order_by(Placeholder.id.desc())
        .first()
    )


def _season_number_hint_from_path(file_path: str) -> int | None:
    """Best-effort season number from a density stub path (sNNeXX or Season NN folder)."""
    import re

    path = str(file_path or "").strip()
    if not path:
        return None
    base = os.path.basename(path).lower().replace(" ", "")
    m = re.search(r"s(\d{1,2})e\d{1,2}", base)
    if m:
        return int(m.group(1))
    normalized = path.replace("\\", "/").lower()
    m2 = re.search(r"(?:^|/)season[ ._-]?(\d{1,2})(?:/|$)", normalized)
    if m2:
        return int(m2.group(1))
    return None


def _find_active_season_density_stub(
    session,
    *,
    series_id: int | None = None,
    season_id: int | None = None,
    file_path: str | None = None,
) -> Placeholder | None:
    from services.source_of_truth.tv_density import PLACEHOLDER_KIND_SEASON_STUB

    q = session.query(Placeholder).filter(
        Placeholder.placeholder_kind == PLACEHOLDER_KIND_SEASON_STUB,
        Placeholder.episode_id.is_(None),
        Placeholder.movie_id.is_(None),
        Placeholder.has_placeholder == True,  # noqa: E712
    )
    if season_id is not None:
        q = q.filter(Placeholder.season_id == int(season_id))
    if series_id is not None:
        q = q.filter(Placeholder.series_id == int(series_id))
    rows = q.order_by(Placeholder.id.desc()).all()
    if not rows:
        return None
    path = str(file_path or "").strip()
    if path:
        for row in rows:
            if not row.path:
                continue
            try:
                if os.path.normpath(os.path.abspath(row.path)) == os.path.normpath(os.path.abspath(path)):
                    return row
            except Exception:
                if os.path.normpath(row.path) == os.path.normpath(path):
                    return row
        # Path given but no exact match: resolve by season hint from filename/folder.
        # Never fall back to an arbitrary first stub (wrong season on multi-season shows).
        sn = _season_number_hint_from_path(path)
        if sn is not None and series_id is not None:
            season = (
                session.query(Season)
                .filter(Season.series_id == int(series_id), Season.season_number == int(sn))
                .first()
            )
            if season is not None:
                sid = int(season.id)
                for row in rows:
                    if getattr(row, "season_id", None) is not None and int(row.season_id) == sid:
                        return row
                return None
        if season_id is not None:
            # Caller already scoped to one season; path was optional disambiguation.
            return rows[0]
        return None
    # No path: only safe when scoped to one season, or only one stub exists.
    if season_id is not None or len(rows) == 1:
        return rows[0]
    return None


def _path_is_series_density_stub(
    session,
    series_row: Series,
    file_path: str | None,
    payload: dict[str, Any] | None = None,
) -> bool:
    """True when the play is this series' density stub (path and/or stub markers)."""
    from services.source_of_truth.tv_density import density_uses_series_stub, text_marks_density_placeholder

    # Ignore leftover series stub rows/filenames after density changes away from series.
    if not density_uses_series_stub(series_row):
        return False

    path = str(file_path or "").strip()
    if path:
        stub = _find_active_series_density_stub(session, int(series_row.id))
        if stub and stub.path:
            try:
                if os.path.normpath(os.path.abspath(stub.path)) == os.path.normpath(os.path.abspath(path)):
                    return True
            except Exception:
                if os.path.normpath(stub.path) == os.path.normpath(path):
                    return True
        base = os.path.basename(path).lower()
        if text_marks_density_placeholder(base) and "s01e01" in base.replace(" ", ""):
            return True
    if payload and _payload_indicates_placeholdarr_stub(payload):
        return _find_active_series_density_stub(session, int(series_row.id)) is not None
    return False


def _path_is_season_density_stub(
    session,
    series_row: Series,
    file_path: str | None,
    payload: dict[str, Any] | None = None,
) -> Placeholder | None:
    """Return the season stub Placeholder when this play is a season density stub."""
    from services.source_of_truth.tv_density import density_uses_season_stub

    # Ignore leftover season stub rows/filenames after density changes away from season.
    if not density_uses_season_stub(series_row):
        return None

    path = str(file_path or "").strip()
    if path:
        stub = _find_active_season_density_stub(session, series_id=int(series_row.id), file_path=path)
        if stub:
            return stub
        base = os.path.basename(path).lower().replace(" ", "")
        if "placeholdarrstub" in base or "placeholdarrplaceholder" in base:
            import re

            m = re.search(r"s(\d{1,2})e01", base)
            if m:
                sn = int(m.group(1))
                season = (
                    session.query(Season)
                    .filter(Season.series_id == int(series_row.id), Season.season_number == sn)
                    .first()
                )
                if season:
                    return _find_active_season_density_stub(session, season_id=int(season.id))
    if payload and _payload_indicates_placeholdarr_stub(payload):
        season_number, _episode_number = _extract_season_episode(payload)
        if season_number is None:
            return None
        season = (
            session.query(Season)
            .filter(Season.series_id == int(series_row.id), Season.season_number == int(season_number))
            .first()
        )
        if season is None:
            return None
        return _find_active_season_density_stub(session, season_id=int(season.id))
    return None


def _catalog_anchor_season_episode(
    session,
    series_row: Series,
    *,
    restrict_season_number: int | None = None,
) -> tuple[int | None, int | None]:
    """First episode that still needs content (for density-stub play targeting)."""
    from datetime import date as date_cls

    from services.source_of_truth.determiner import episode_contributes_to_density_stub as ep_contrib

    include_specials = bool(getattr(settings, "INCLUDE_SPECIALS", False))
    placeholders_enabled = bool(getattr(settings, "coming_soon_placeholders_enabled", True))
    lookahead_days = int(getattr(settings, "CALENDAR_LOOKAHEAD_DAYS", 30) or 30)
    today = date_cls.today()

    if bool(getattr(series_row, "block_placeholder", False)):
        policy = "never"
    elif bool(getattr(series_row, "force_placeholder", False)):
        policy = "pinned"
    else:
        policy = "auto"

    q = (
        session.query(Episode, Season.season_number)
        .join(Season, Episode.season_id == Season.id)
        .filter(
            Season.series_id == int(series_row.id),
            Episode.is_deleted == False,  # noqa: E712
        )
    )
    if restrict_season_number is not None:
        q = q.filter(Season.season_number == int(restrict_season_number))
    rows = q.order_by(Season.season_number.asc(), Episode.episode_number.asc()).all()
    for ep, sn in rows:
        sn_i = int(sn or 0)
        if not include_specials and sn_i == 0:
            continue
        meta = (int(series_row.id), sn_i, int(getattr(ep, "episode_number", 0) or 0))
        if ep_contrib(
            session,
            ep,
            placeholders_enabled=placeholders_enabled,
            lookahead_days=lookahead_days,
            now_date=today,
            episode_order_meta=meta,
            series_policy=policy,
        ):
            return sn_i, int(getattr(ep, "episode_number", 0) or 0)
    for ep, sn in rows:
        sn_i = int(sn or 0)
        if not include_specials and sn_i == 0:
            continue
        if not bool(getattr(ep, "has_file", False)):
            return sn_i, int(getattr(ep, "episode_number", 0) or 0)
    return None, None


def _forward_episode_window(
    scoped_query,
    *,
    season_number: int,
    episode_number: int,
    lookahead: int,
) -> list[Episode]:
    """Next ``lookahead`` episodes in airing order from the played episode (may cross seasons)."""
    return (
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


def _series_last_episode(scoped_query):
    """Last episode Placeholdarr stores for the series (respects scoped_query specials filter)."""
    return (
        scoped_query
        .order_by(Season.season_number.desc(), Episode.episode_number.desc())
        .first()
    )


def _window_includes_later_season(window: list[Episode], season_number: int) -> bool:
    return any(
        int(getattr(getattr(ep, 'season', None), 'season_number', 0) or 0) > int(season_number)
        for ep in window
    )


def _window_reaches_series_end(window: list[Episode], last_episode: Episode | None) -> bool:
    if not window or not last_episode:
        return False
    final_window_episode = window[-1]
    final_target_season = final_window_episode.season.season_number if final_window_episode.season else 0
    final_target_episode = int(final_window_episode.episode_number or 0)
    max_season = last_episode.season.season_number if last_episode.season else 0
    max_episode = int(last_episode.episode_number or 0)
    return bool(
        final_target_season > max_season
        or (final_target_season == max_season and final_target_episode >= max_episode)
    )


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

        if episode_number is not None:
            window = _forward_episode_window(
                scoped_query,
                season_number=int(season_number),
                episode_number=int(episode_number),
                lookahead=lookahead,
            )
            metadata['window_size'] = len(window)

            if _window_includes_later_season(window, int(season_number)):
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

            # Match Episode-mode behavior: when the lookahead window reaches the end of known
            # episodes, mark the whole series monitored in Sonarr for newly-added seasons.
            last_episode = _series_last_episode(scoped_query)
            metadata['reached_end'] = _window_reaches_series_end(window, last_episode)

        metadata['scope'] = 'season'
        return targets, metadata

    if episode_number is None:
        return [], {'mode': mode, 'reason': 'missing_episode_number'}

    window = _forward_episode_window(
        scoped_query,
        season_number=int(season_number),
        episode_number=int(episode_number),
        lookahead=lookahead,
    )
    targets.extend([ep for ep in window if not bool(getattr(ep, 'has_file', False))])

    last_episode = _series_last_episode(scoped_query)
    metadata['reached_end'] = _window_reaches_series_end(window, last_episode)
    metadata['window_size'] = len(window)
    metadata['target_count'] = len(targets)

    metadata['scope'] = 'episode'
    return targets, metadata


def _activate_queue_monitor_after_playback_search(
    session,
    intents: list[StatusIntent],
    *,
    search_triggered: bool,
) -> None:
    """Mark placeholders for NOTIFY-driven queue monitoring after a playback ARR search."""
    if not search_triggered or not intents:
        return
    now = datetime.now(timezone.utc)
    for intent in intents:
        ph = session.query(Placeholder).filter(Placeholder.id == int(intent.placeholder_id)).first()
        if ph:
            ph.queue_monitor_active = True
            ph.queue_monitor_active_set_at = now
            session.add(ph)
    try:
        session.execute(text("NOTIFY placeholdarr_queue_monitor_signal, ''"))
    except Exception as exc:
        logger.debug(
            f"NOTIFY placeholdarr_queue_monitor_signal failed (non-fatal): {exc}",
            extra={"emoji_type": "debug"},
        )


def _placeholder_intents_for_targets(
    session,
    targets: list[Episode],
    movie_row: Movie | None = None,
    *,
    series_stub: Placeholder | None = None,
) -> list[StatusIntent]:
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
    elif series_stub is not None and getattr(series_stub, "id", None):
        rows = [series_stub]
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
    # Playback may enable Radarr monitoring and trigger search. Placeholder removal for
    # monitored titles is deferred to sync/determination (SKIP_PLACEHOLDERS_WHEN_MONITORED);
    # import still removes placeholders when a real file lands.
    instance_key = _row_instance_key(movie_row)
    if bool(getattr(movie_row, 'is_deleted', False)):
        return {'ok': True, 'skipped': 'deleted_in_arr', 'movie_id': int(movie_row.id), 'instance': instance_key}

    if bool(getattr(movie_row, 'has_file', False)):
        return {'ok': True, 'skipped': 'has_file', 'movie_id': int(movie_row.id), 'instance': instance_key}

    base_url, api_key = _resolve_endpoint('movie', instance_key=instance_key or None)
    if not base_url or not api_key:
        return {'ok': False, 'reason': 'missing_movie_arr_config', 'movie_id': int(movie_row.id), 'instance': instance_key}

    was_monitored = bool(getattr(movie_row, 'radarr_monitored', False))
    is_future = movie_is_future_for_playback_search(movie_row)
    from services.playback_dest_filters import dest_folder_for_movie_row

    dest_folder = dest_folder_for_movie_row(movie_row)

    monitored_updated = False
    if not was_monitored and getattr(movie_row, 'radarrid', None):
        monitored_updated = set_radarr_movie_monitored(int(movie_row.radarrid), True, url=base_url, api_key=api_key)
        if monitored_updated:
            movie_row.radarr_monitored = True
            session.add(movie_row)

    search_triggered = False
    skipped_search_reason: str | None = None
    if _playback_monitor_only_no_search(dest_folder):
        skipped_search_reason = "monitor_only_no_search"
    elif was_monitored and _suppress_search_for_already_monitored(dest_folder):
        skipped_search_reason = "already_monitored"
    elif is_future and _suppress_search_for_future_titles(dest_folder):
        skipped_search_reason = "future_only"
    elif getattr(movie_row, 'radarrid', None):
        search_triggered = trigger_radarr_movie_search(int(movie_row.radarrid), url=base_url, api_key=api_key)

    intents = _placeholder_intents_for_targets(session, [], movie_row=movie_row)
    if intents:
        StatusOrchestrator(session=session).apply_and_project_statuses(intents)

    _activate_queue_monitor_after_playback_search(session, intents, search_triggered=bool(search_triggered))

    return {
        'ok': True,
        'event': 'playback_start',
        'media_type': 'movie',
        'movie_id': int(movie_row.id),
        'instance': instance_key,
        'dest_folder': dest_folder or '',
        'monitored_updated': monitored_updated,
        'search_triggered': bool(search_triggered),
        'skipped_search_reason': skipped_search_reason,
        'was_monitored': was_monitored,
        'is_future': is_future,
        'status_intents_applied': len(intents),
    }


def _episode_season_number(session, episode: Episode) -> int:
    season = getattr(episode, "season", None)
    if season is not None:
        return int(getattr(season, "season_number", 0) or 0)
    season_row = session.query(Season).filter(Season.id == episode.season_id).first()
    return int(getattr(season_row, "season_number", 0) or 0) if season_row else 0


def _suppress_search_for_already_monitored(dest_folder: str | None = None) -> bool:
    from services.playback_dest_filters import suppress_search_already_monitored_for_dest

    return suppress_search_already_monitored_for_dest(dest_folder)


def _suppress_search_for_future_titles(dest_folder: str | None = None) -> bool:
    from services.playback_dest_filters import suppress_search_future_for_dest

    return suppress_search_future_for_dest(dest_folder)


def _playback_monitor_only_no_search(dest_folder: str | None = None) -> bool:
    from services.playback_dest_filters import monitor_only_for_dest

    return monitor_only_for_dest(dest_folder)


def _partition_playback_targets(
    session,
    series_row: Series,
    targets: list[Episode],
    *,
    now_date: date | None = None,
    dest_folder: str | None = None,
) -> dict[str, Any]:
    """Split lookahead targets into monitor vs search buckets."""
    eff = now_date or datetime.now(timezone.utc).date()
    series_id = int(series_row.id)
    max_known_order = build_series_max_known_order_within_horizon(session, series_id, now_date=eff)

    to_monitor: list[Episode] = []
    search_eligible: list[Episode] = []
    future_episodes: list[Episode] = []
    is_future_by_episode: list[bool] = []
    monitor_only = _playback_monitor_only_no_search(dest_folder)
    suppress_future = (not monitor_only) and _suppress_search_for_future_titles(dest_folder)
    suppress_monitored = (not monitor_only) and _suppress_search_for_already_monitored(dest_folder)

    for ep in targets:
        season_number = _episode_season_number(session, ep)
        is_future = episode_is_future_for_playback_search(
            ep,
            season_number=season_number,
            series_max_known_order_within_horizon=max_known_order,
            now_date=eff,
        )
        is_future_by_episode.append(is_future)
        if is_future:
            future_episodes.append(ep)

        if not bool(getattr(ep, "sonarr_monitored", False)):
            to_monitor.append(ep)

        if monitor_only:
            continue
        if is_future and suppress_future:
            continue
        if bool(getattr(ep, "sonarr_monitored", False)) and suppress_monitored:
            continue
        search_eligible.append(ep)

    skipped_search_reason: str | None = None
    if monitor_only and targets:
        skipped_search_reason = "monitor_only_no_search"
    elif not search_eligible and targets:
        if future_episodes and suppress_future:
            skipped_search_reason = "future_only"
        elif not to_monitor:
            skipped_search_reason = "no_unmonitored_search_targets"
        elif suppress_monitored:
            skipped_search_reason = "suppressed_monitored"

    monitor_ids = [int(ep.sonarrid) for ep in to_monitor if getattr(ep, "sonarrid", None)]
    return {
        "to_monitor": to_monitor,
        "monitor_ids": monitor_ids,
        "search_eligible": search_eligible,
        "future_episodes": future_episodes,
        "future_count": len(future_episodes),
        "already_monitored_count": sum(1 for ep in targets if bool(getattr(ep, "sonarr_monitored", False))),
        "search_count": len(search_eligible),
        "skipped_search_reason": skipped_search_reason,
        "dest_folder": dest_folder or "",
    }


def _search_eligible_ids(search_eligible: list[Episode]) -> list[int]:
    return [int(ep.sonarrid) for ep in search_eligible if getattr(ep, "sonarrid", None)]


def _trigger_playback_sonarr_search_for_row(
    session,
    *,
    series_row: Series,
    targets: list[Episode],
    search_eligible: list[Episode],
    mode: str,
    target_meta: dict[str, Any],
    base_url: str,
    api_key: str,
) -> bool:
    if not search_eligible or not getattr(series_row, "sonarrid", None):
        return False

    series_id = int(series_row.sonarrid)
    eligible_ids = _search_eligible_ids(search_eligible)
    if not eligible_ids:
        return False

    eligible_set = {int(ep.id) for ep in search_eligible if getattr(ep, "id", None) is not None}
    target_set = {int(ep.id) for ep in targets if getattr(ep, "id", None) is not None}
    full_target_scope = eligible_set == target_set and len(target_set) > 0

    if mode == "series" and full_target_scope:
        return bool(trigger_sonarr_search(series_id=series_id, url=base_url, api_key=api_key))

    if mode == "season" and full_target_scope:
        season_numbers = {_episode_season_number(session, ep) for ep in search_eligible}
        if len(season_numbers) == 1:
            return bool(
                trigger_sonarr_search(
                    series_id=series_id,
                    season_number=next(iter(season_numbers)),
                    url=base_url,
                    api_key=api_key,
                )
            )

    return bool(
        trigger_sonarr_search(
            series_id=series_id,
            episode_ids=eligible_ids,
            url=base_url,
            api_key=api_key,
        )
    )


def _run_episode_search_for_row(session, series_row: Series, payload: dict[str, Any]) -> dict[str, Any]:
    # Same as movie playback: do not run determination/materialization here; monitored
    # cleanup is handled on the next ARR sync when SKIP_PLACEHOLDERS_WHEN_MONITORED is on.
    instance_key = _row_instance_key(series_row)
    if bool(getattr(series_row, 'is_deleted', False)):
        return {'ok': True, 'skipped': 'deleted_in_arr', 'series_id': int(series_row.id), 'instance': instance_key}

    file_path = _extract_file_path(payload)
    season_stub_row = _path_is_season_density_stub(session, series_row, file_path, payload)
    series_density_stub = False if season_stub_row else _path_is_series_density_stub(
        session, series_row, file_path, payload
    )
    density_stub = bool(series_density_stub or season_stub_row)
    # When Tracearr omitted path, density detection still works via stub markers; inject
    # the stub path so status intents and path-based helpers share one code path.
    if density_stub and not file_path:
        stub_ph = season_stub_row or _find_active_series_density_stub(session, int(series_row.id))
        if stub_ph and getattr(stub_ph, 'path', None):
            file_path = _normalize_path(stub_ph.path) or str(stub_ph.path)
            payload['file_path'] = file_path
    season_number, episode_number = _extract_season_episode(payload)
    if density_stub:
        restrict_sn = None
        if season_stub_row is not None and getattr(season_stub_row, "season_id", None):
            season_row = session.query(Season).filter(Season.id == int(season_stub_row.season_id)).first()
            if season_row is not None:
                restrict_sn = int(getattr(season_row, "season_number", 0) or 0)
                season_number = restrict_sn
        # Stub filename is always e01; resolve search window from catalog state.
        anchor_s, anchor_e = _catalog_anchor_season_episode(
            session,
            series_row,
            restrict_season_number=restrict_sn,
        )
        if anchor_s is not None:
            season_number = anchor_s
        if anchor_e is not None:
            episode_number = anchor_e

    targets, target_meta = _collect_episode_targets(session, series_row, season_number, episode_number)
    if density_stub:
        target_meta = dict(target_meta or {})
        target_meta["density_stub"] = True
        target_meta["density_stub_kind"] = "season" if season_stub_row else "series"
        target_meta["catalog_anchor"] = {"season": season_number, "episode": episode_number}
    if not targets:
        return {
            'ok': True,
            'event': 'playback_start',
            'media_type': 'episode',
            'series_id': int(series_row.id),
            'instance': instance_key,
            'skipped': 'no_targets',
            'target_meta': target_meta,
        }

    # Seed media player IDs directly from playback payload if available
    plex_key = _extract_plex_rating_key(payload)
    jelly_key = _extract_jellyfin_item_id(payload)
    if plex_key or jelly_key:
        stamp_targets = _episodes_for_media_id_stamp(
            session,
            targets,
            file_path=file_path,
            season_number=season_number,
            episode_number=episode_number,
        )
        _stamp_episode_media_ids(session, stamp_targets, plex_key=plex_key or '', jelly_key=jelly_key or '')

    base_url, api_key = _resolve_endpoint('series', instance_key=instance_key or None)
    if not base_url or not api_key:
        return {'ok': False, 'reason': 'missing_series_arr_config', 'series_id': int(series_row.id), 'instance': instance_key}

    from services.playback_dest_filters import dest_folder_for_series_row

    dest_folder = dest_folder_for_series_row(series_row)
    partition = _partition_playback_targets(session, series_row, targets, dest_folder=dest_folder)
    to_monitor: list[Episode] = partition["to_monitor"]
    search_eligible: list[Episode] = partition["search_eligible"]
    monitor_ids: list[int] = partition["monitor_ids"]
    skipped_search_reason = partition.get("skipped_search_reason")

    monitor_result = {"updated": 0, "failed": 0}
    if monitor_ids:
        monitor_result = set_sonarr_episode_monitored(monitor_ids, True, url=base_url, api_key=api_key)
        for ep in to_monitor:
            ep.sonarr_monitored = True
            session.add(ep)

    mode = _play_mode()
    search_triggered = False
    if search_eligible:
        search_triggered = _trigger_playback_sonarr_search_for_row(
            session,
            series_row=series_row,
            targets=targets,
            search_eligible=search_eligible,
            mode=mode,
            target_meta=target_meta,
            base_url=base_url,
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

    series_stub_row = _find_active_series_density_stub(session, int(series_row.id)) if series_density_stub else None
    density_stub_ph = season_stub_row or series_stub_row
    # Density stubs: only project SEARCHING when an Arr search was actually triggered.
    # Monitor-only, future suppress, or empty eligible must not flip the stub to SEARCHING.
    intents = _placeholder_intents_for_targets(
        session,
        search_eligible if not density_stub_ph else [],
        series_stub=density_stub_ph if (density_stub_ph is not None and search_triggered) else None,
    )
    if intents:
        StatusOrchestrator(session=session).apply_and_project_statuses(intents)

    _activate_queue_monitor_after_playback_search(session, intents, search_triggered=bool(search_triggered))

    return {
        'ok': True,
        'event': 'playback_start',
        'media_type': 'episode',
        'series_id': int(series_row.id),
        'instance': instance_key,
        'mode': mode,
        'targets': len(targets),
        'target_meta': target_meta,
        'future_targets': int(partition.get('future_count') or 0),
        'search_targets': len(search_eligible),
        'monitor_only_targets': max(0, len(targets) - len(search_eligible)),
        'skipped_search_reason': skipped_search_reason,
        'monitor_updated': int(monitor_result.get('updated', 0)),
        'monitor_failed': int(monitor_result.get('failed', 0)),
        'search_triggered': bool(search_triggered),
        'reached_end_marked': bool(reached_end_marked),
        'status_intents_applied': len(intents),
    }


def _should_schedule_delayed_fallback(
    selection: dict[str, Any],
    chosen_instance: str | None,
    *,
    media_type: str,
) -> bool:
    mt = 'movie' if media_type == 'movie' else 'tv'
    if not _fallback_enabled(media_type=mt):
        return False
    if not chosen_instance:
        return False
    fallbacks = selection.get('fallback_instances') or []
    if not fallbacks and selection.get('fallback_instance'):
        fallbacks = [selection.get('fallback_instance')]
    if not fallbacks:
        return False
    # Schedule when the preferred (usually path-matched) Arr search fired, even if Always
    # instances were also searched in the same play.
    return chosen_instance == selection.get('preferred_instance')


def _enqueue_delayed_fallback(
    session,
    *,
    media_type: str,
    payload: dict[str, Any],
    preferred_instance: str,
    fallback_instances: list[str],
    source_instance: str | None,
) -> int | None:
    mt = 'movie' if media_type == 'movie' else 'tv'
    if not _fallback_enabled(media_type=mt):
        return None

    timeout_minutes = _fallback_timeout_minutes()
    if timeout_minutes <= 0:
        return None

    queue = [str(k).strip().lower() for k in (fallback_instances or []) if str(k or '').strip()]
    if not queue:
        return None

    from services.source_of_truth.job_priority import default_priority_for

    job = Job(
        job_type=PLAYBACK_FALLBACK_JOB_TYPE,
        payload={
            'media_type': media_type,
            'preferred_instance': preferred_instance,
            'fallback_instances': queue,
            'fallback_instance': queue[0],
            'source_instance': source_instance,
            'payload': dict(payload),
        },
        status='PENDING',
        max_attempts=3,
        run_after=datetime.now(timezone.utc) + timedelta(minutes=timeout_minutes),
        priority=default_priority_for(PLAYBACK_FALLBACK_JOB_TYPE),
    )
    session.add(job)
    session.flush()
    logger.info(
        f"Enqueued playback fallback job job_id={job.id} media_type={media_type} preferred={preferred_instance} fallback_queue={queue}",
        extra={'emoji_type': 'processing'},
    )
    return int(job.id)


def _process_discover_movie_on_instance(
    session,
    *,
    row: Any,
    tmdb_id: int,
    inst: dict[str, Any],
    search: bool,
) -> dict[str, Any]:
    """Lookup / add / monitor / search one Radarr instance for a Discover movie play."""
    from routes.collections import _fetch_instance_quality_profiles, _fetch_instance_root_folders
    from services.postgres.models import ArrMovieOverlay
    from services.source_of_truth.arr_api import lookup_movie

    instance_key = str(inst.get('instance_key') or inst.get('key') or 'radarr_std')
    instance_id = str(inst.get('instance_id') or f'radarr:{instance_key}')
    url = str(inst.get('url') or inst.get('base_url') or '')
    api_key = str(inst.get('api_key') or inst.get('apikey') or '')

    overlay = (
        session.query(ArrMovieOverlay)
        .filter(ArrMovieOverlay.tmdb_id == tmdb_id, ArrMovieOverlay.instance_id == instance_id)
        .first()
    )
    if overlay is None:
        overlay = (
            session.query(ArrMovieOverlay)
            .filter(ArrMovieOverlay.tmdb_id == tmdb_id, ArrMovieOverlay.instance_key == instance_key)
            .first()
        )
    added = False
    skipped_existing = False
    radarr_id = int(overlay.radarr_id) if overlay and overlay.radarr_id else None

    def _upsert_overlay(rid: int, *, monitored: bool | None = None, has_file: bool | None = None) -> ArrMovieOverlay:
        nonlocal overlay
        ov = overlay
        if ov is None:
            ov = ArrMovieOverlay(tmdb_id=tmdb_id, instance_id=instance_id, instance_key=instance_key)
            session.add(ov)
            overlay = ov
        ov.instance_id = instance_id
        ov.instance_key = instance_key
        ov.radarr_id = int(rid)
        if monitored is not None:
            ov.monitored = bool(monitored)
        if has_file is not None:
            ov.has_file = bool(has_file)
        ov.updated_at = datetime.now(timezone.utc)
        session.add(ov)
        return ov

    # Prefer a live Radarr lookup before POST /movie/import so unmonitored
    # library titles are monitored+searched instead of treated as a new add.
    if radarr_id is None and url and api_key:
        try:
            hit = lookup_movie(
                url=url,
                api_key=api_key,
                tmdb_id=tmdb_id,
                title=getattr(row, 'title', None),
                year=getattr(row, 'year', None),
            )
        except Exception as exc:
            logger.warning(
                f"Discover playback Radarr lookup failed tmdb={tmdb_id} "
                f"instance={instance_key}: {exc}",
                extra={'emoji_type': 'warning'},
            )
            hit = None
        if isinstance(hit, dict):
            try:
                existing_id = int(hit.get('id') or 0)
            except (TypeError, ValueError):
                existing_id = 0
            if existing_id > 0:
                radarr_id = existing_id
                skipped_existing = True
                _upsert_overlay(
                    existing_id,
                    monitored=bool(hit.get('monitored')) if 'monitored' in hit else None,
                    has_file=bool(hit.get('hasFile') or (hit.get('movieFile') or {}).get('path'))
                    if ('hasFile' in hit or hit.get('movieFile'))
                    else None,
                )
                session.flush()

    if radarr_id is None:
        profiles = _fetch_instance_quality_profiles(inst) or []
        roots = _fetch_instance_root_folders(inst) or []
        if not profiles or not roots:
            return {
                'ok': False,
                'reason': 'missing_radarr_profile_or_root',
                'instance_key': instance_key,
                'tmdb_id': tmdb_id,
            }
        quality_profile_id = int(profiles[0].get('id') or profiles[0].get('value') or 0)
        root_folder_path = str(roots[0].get('path') or roots[0].get('value') or '')
        if not quality_profile_id or not root_folder_path:
            return {
                'ok': False,
                'reason': 'invalid_radarr_profile_or_root',
                'instance_key': instance_key,
                'tmdb_id': tmdb_id,
            }

        item = SimpleNamespace(title=row.title, year=row.year, tmdb_id=tmdb_id)
        results = add_missing_titles(
            media_type='movie',
            url=url,
            api_key=api_key,
            items=[item],
            quality_profile_id=quality_profile_id,
            root_folder_path=root_folder_path,
            monitored=True,
            search=search,
            instance_key=instance_key,
        )
        status = (results[0].get('status') if results else None)
        if status == 'error':
            return {
                'ok': False,
                'reason': 'radarr_add_failed',
                'instance_key': instance_key,
                'tmdb_id': tmdb_id,
                'detail': results[0].get('error'),
                'results': results,
            }
        if status == 'skipped':
            skipped_existing = True
        elif status == 'ok':
            added = True
        try:
            arr_id = int((results[0] or {}).get('arr_id') or 0) or None
        except (TypeError, ValueError, IndexError):
            arr_id = None
        # Never call refresh_arr_overlays() here: a second session writing
        # arr_movie_overlay deadlocks against this playback transaction.
        if arr_id is None and url and api_key:
            try:
                hit = lookup_movie(
                    url=url,
                    api_key=api_key,
                    tmdb_id=tmdb_id,
                    title=getattr(row, 'title', None),
                    year=getattr(row, 'year', None),
                )
            except Exception as exc:
                logger.warning(
                    f"Discover playback Radarr post-add lookup failed tmdb={tmdb_id} "
                    f"instance={instance_key}: {exc}",
                    extra={'emoji_type': 'warning'},
                )
                hit = None
            if isinstance(hit, dict):
                try:
                    arr_id = int(hit.get('id') or 0) or None
                except (TypeError, ValueError):
                    arr_id = None
        if arr_id:
            _upsert_overlay(int(arr_id), monitored=True, has_file=False)
            radarr_id = int(arr_id)
        else:
            radarr_id = None

    monitored_updated = False
    search_triggered = False
    # Always monitor+search when we have a Radarr id, except a brand-new add that
    # already requested search via addOptions (Radarr searches on add).
    need_explicit_monitor_search = bool(radarr_id) and not (added and search)
    if need_explicit_monitor_search:
        if overlay is None or not overlay.monitored:
            monitored_updated = set_radarr_movie_monitored(int(radarr_id), True, url=url, api_key=api_key)
            if monitored_updated:
                _upsert_overlay(int(radarr_id), monitored=True)
        if search:
            search_triggered = trigger_radarr_movie_search(int(radarr_id), url=url, api_key=api_key)
    elif added and search:
        search_triggered = True
        if overlay is not None:
            overlay.monitored = True
            session.add(overlay)

    search_for_monitor = bool(search_triggered) or (added and search)
    return {
        'ok': True,
        'instance_key': instance_key,
        'instance_id': instance_id,
        'added': added,
        'skipped_existing': skipped_existing,
        'radarr_id': radarr_id,
        'monitored_updated': monitored_updated,
        'search_triggered': search_for_monitor,
    }


def _process_discover_movie_playback(session, context: dict[str, Any]) -> dict[str, Any] | None:
    """TMDB Discover: resolve Radarr per Placeholder Search Mode, then add/monitor/search.

    Targets MOVIE_PLACEHOLDER_SEARCH_MODE instances (All / primary / secondary /
    named). Prefer matched library path is ignored. Already-in-Radarr titles
    (including unmonitored) are still monitored and searched on each target.
    """
    from services.discover.mode import is_tmdb_discover_mode
    from services.postgres.models import TmdbMovie

    if not is_tmdb_discover_mode():
        return None
    tmdb_id = context.get('tmdb_id') or context.get('tmdb_movie_id')
    if tmdb_id is None:
        path = str(context.get('file_path') or '')
        m = re.search(r'\{tmdb-(\d+)\}', path, flags=re.I)
        if m:
            tmdb_id = int(m.group(1))
    if tmdb_id is None:
        return None
    tmdb_id = int(tmdb_id)
    row = session.get(TmdbMovie, tmdb_id)
    if row is None:
        return None

    targets, preference = _discover_instances_for_placeholder_mode('movie')
    if not targets:
        has_any = bool(settings.arr_instances_for_type('radarr'))
        return {
            'ok': False,
            'reason': 'no_matching_radarr_instance' if has_any else 'no_radarr_instance',
            'tmdb_id': tmdb_id,
            'preference': preference,
        }

    monitor_only = _playback_monitor_only_no_search()
    search = not monitor_only
    instance_results: list[dict[str, Any]] = []
    for inst in targets:
        try:
            result = _process_discover_movie_on_instance(
                session, row=row, tmdb_id=tmdb_id, inst=inst, search=search
            )
        except Exception as exc:
            key = str(inst.get('instance_key') or inst.get('key') or '')
            logger.warning(
                f"Discover movie playback failed tmdb={tmdb_id} instance={key}: {exc}",
                extra={'emoji_type': 'warning'},
            )
            result = {
                'ok': False,
                'reason': 'instance_error',
                'instance_key': key,
                'tmdb_id': tmdb_id,
                'detail': str(exc),
            }
        instance_results.append(result)

    any_ok = any(bool(r.get('ok')) for r in instance_results)
    search_for_monitor = any(bool(r.get('search_triggered')) for r in instance_results if r.get('ok'))
    added = any(bool(r.get('added')) for r in instance_results if r.get('ok'))
    skipped_existing = any(bool(r.get('skipped_existing')) for r in instance_results if r.get('ok'))
    monitored_updated = any(bool(r.get('monitored_updated')) for r in instance_results if r.get('ok'))
    radarr_ids = [r.get('radarr_id') for r in instance_results if r.get('ok') and r.get('radarr_id')]

    if not any_ok:
        first_fail = next((r for r in instance_results if not r.get('ok')), {})
        return {
            'ok': False,
            'reason': first_fail.get('reason') or 'radarr_all_failed',
            'tmdb_id': tmdb_id,
            'preference': preference,
            'instances': instance_results,
            'detail': first_fail.get('detail'),
        }

    intents: list[StatusIntent] = []
    ph_rows = session.query(Placeholder).filter(Placeholder.tmdb_movie_id == tmdb_id).all()
    for ph in ph_rows:
        intents.append(
            StatusIntent(
                placeholder_id=int(ph.id),
                new_status=DisplayStatus.SEARCHING.value if search_for_monitor else DisplayStatus.REQUEST.value,
                reason=render_message("queue.searching", {}) if search_for_monitor else None,
                source=StatusSource.EVENT_PLAYBACK_STARTED,
                trigger_nfo_refresh=True,
            )
        )
    if intents:
        StatusOrchestrator(session=session).apply_and_project_statuses(intents)
    _activate_queue_monitor_after_playback_search(
        session, intents, search_triggered=search_for_monitor
    )

    # Persist overlay / status before Discover determination so skip-when-monitored
    # can clear the Discover placeholder without waiting for the next catalog sync.
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    try:
        from services.discover.determine import run_discover_determination
        from services.discover.materialize import apply_tmdb_movie_materialization

        run_discover_determination(tmdb_ids=[tmdb_id])
        apply_tmdb_movie_materialization(tmdb_id)
    except Exception as exc:
        logger.warning(
            f"Discover playback follow-up determine/materialize failed tmdb={tmdb_id}: {exc}",
            extra={'emoji_type': 'warning'},
        )

    return {
        'ok': True,
        'event': 'playback_start',
        'media_type': 'movie',
        'playback_kind': 'placeholder',
        'discover': True,
        'tmdb_id': tmdb_id,
        'added': added,
        'skipped_existing': skipped_existing,
        'radarr_id': radarr_ids[0] if len(radarr_ids) == 1 else None,
        'radarr_ids': radarr_ids,
        'monitored_updated': monitored_updated,
        'search_triggered': search_for_monitor,
        'monitor_only': monitor_only,
        'preference': preference,
        'chosen_instances': [
            str(r.get('instance_key') or '') for r in instance_results if r.get('ok')
        ],
        'instances': instance_results,
    }


def _process_discover_series_on_instance(
    session,
    *,
    row: Any,
    tmdb_id: int,
    inst: dict[str, Any],
    search: bool,
) -> dict[str, Any]:
    """Lookup / add / monitor / search one Sonarr instance for a Discover series play."""
    from routes.collections import _fetch_instance_quality_profiles, _fetch_instance_root_folders
    from services.postgres.models import ArrSeriesOverlay
    from services.source_of_truth.arr_api import lookup_series

    instance_key = str(inst.get('instance_key') or inst.get('key') or 'sonarr_std')
    instance_id = str(inst.get('instance_id') or f'sonarr:{instance_key}')
    url = str(inst.get('url') or inst.get('base_url') or '')
    api_key = str(inst.get('api_key') or inst.get('apikey') or '')

    overlay = (
        session.query(ArrSeriesOverlay)
        .filter(ArrSeriesOverlay.tmdb_id == tmdb_id, ArrSeriesOverlay.instance_id == instance_id)
        .first()
    )
    if overlay is None:
        overlay = (
            session.query(ArrSeriesOverlay)
            .filter(ArrSeriesOverlay.tmdb_id == tmdb_id, ArrSeriesOverlay.instance_key == instance_key)
            .first()
        )
    added = False
    skipped_existing = False
    sonarr_id = int(overlay.sonarr_id) if overlay and overlay.sonarr_id else None
    tvdb_id = int(row.tvdb_id) if getattr(row, 'tvdb_id', None) else None

    def _upsert_overlay(sid: int, *, monitored: bool | None = None, has_file: bool | None = None) -> ArrSeriesOverlay:
        nonlocal overlay
        ov = overlay
        if ov is None:
            ov = ArrSeriesOverlay(tmdb_id=tmdb_id, instance_id=instance_id, instance_key=instance_key)
            session.add(ov)
            overlay = ov
        ov.instance_id = instance_id
        ov.instance_key = instance_key
        ov.sonarr_id = int(sid)
        if monitored is not None:
            ov.monitored = bool(monitored)
        if has_file is not None:
            ov.has_file = bool(has_file)
        ov.updated_at = datetime.now(timezone.utc)
        session.add(ov)
        return ov

    if sonarr_id is None and url and api_key:
        try:
            hit = lookup_series(
                url=url,
                api_key=api_key,
                tvdb_id=tvdb_id,
                tmdb_id=tmdb_id,
                title=getattr(row, 'title', None),
                year=getattr(row, 'year', None),
            )
        except Exception as exc:
            logger.warning(
                f"Discover playback Sonarr lookup failed tmdb={tmdb_id} "
                f"instance={instance_key}: {exc}",
                extra={'emoji_type': 'warning'},
            )
            hit = None
        if isinstance(hit, dict):
            try:
                existing_id = int(hit.get('id') or 0)
            except (TypeError, ValueError):
                existing_id = 0
            if existing_id > 0:
                sonarr_id = existing_id
                skipped_existing = True
                stats = hit.get('statistics') if isinstance(hit.get('statistics'), dict) else {}
                try:
                    has_file = int(stats.get('episodeFileCount') or 0) > 0
                except (TypeError, ValueError):
                    has_file = bool(hit.get('hasFile'))
                _upsert_overlay(
                    existing_id,
                    monitored=bool(hit.get('monitored')) if 'monitored' in hit else None,
                    has_file=has_file if ('statistics' in hit or 'hasFile' in hit) else None,
                )
                session.flush()
                if not tvdb_id:
                    try:
                        tvdb_id = int(hit.get('tvdbId') or 0) or None
                    except (TypeError, ValueError):
                        tvdb_id = None
                    if tvdb_id:
                        row.tvdb_id = tvdb_id
                        session.add(row)

    if sonarr_id is None:
        profiles = _fetch_instance_quality_profiles(inst) or []
        roots = _fetch_instance_root_folders(inst) or []
        if not profiles or not roots:
            return {
                'ok': False,
                'reason': 'missing_sonarr_profile_or_root',
                'instance_key': instance_key,
                'tmdb_id': tmdb_id,
            }
        quality_profile_id = int(profiles[0].get('id') or profiles[0].get('value') or 0)
        root_folder_path = str(roots[0].get('path') or roots[0].get('value') or '')
        if not quality_profile_id or not root_folder_path:
            return {
                'ok': False,
                'reason': 'invalid_sonarr_profile_or_root',
                'instance_key': instance_key,
                'tmdb_id': tmdb_id,
            }

        item = SimpleNamespace(
            title=row.title,
            year=row.year,
            tmdb_id=tmdb_id,
            tvdb_id=tvdb_id,
        )
        results = add_missing_titles(
            media_type='series',
            url=url,
            api_key=api_key,
            items=[item],
            quality_profile_id=quality_profile_id,
            root_folder_path=root_folder_path,
            monitored=True,
            search=search,
            instance_key=instance_key,
        )
        status = (results[0].get('status') if results else None)
        if status == 'error':
            return {
                'ok': False,
                'reason': 'sonarr_add_failed',
                'instance_key': instance_key,
                'tmdb_id': tmdb_id,
                'detail': results[0].get('error'),
                'results': results,
            }
        if status == 'skipped':
            skipped_existing = True
        elif status == 'ok':
            added = True
        try:
            arr_id = int((results[0] or {}).get('arr_id') or 0) or None
        except (TypeError, ValueError, IndexError):
            arr_id = None
        # Never call refresh_arr_series_overlays() here: a second session writing
        # arr_series_overlay deadlocks against this playback transaction.
        if arr_id is None and url and api_key:
            try:
                hit = lookup_series(
                    url=url,
                    api_key=api_key,
                    tvdb_id=tvdb_id,
                    tmdb_id=tmdb_id,
                    title=getattr(row, 'title', None),
                    year=getattr(row, 'year', None),
                )
            except Exception as exc:
                logger.warning(
                    f"Discover playback Sonarr post-add lookup failed tmdb={tmdb_id} "
                    f"instance={instance_key}: {exc}",
                    extra={'emoji_type': 'warning'},
                )
                hit = None
            if isinstance(hit, dict):
                try:
                    arr_id = int(hit.get('id') or 0) or None
                except (TypeError, ValueError):
                    arr_id = None
                if not tvdb_id and isinstance(hit, dict):
                    try:
                        tvdb_id = int(hit.get('tvdbId') or 0) or None
                    except (TypeError, ValueError):
                        tvdb_id = None
                    if tvdb_id:
                        row.tvdb_id = tvdb_id
                        session.add(row)
        if arr_id:
            _upsert_overlay(int(arr_id), monitored=True, has_file=False)
            sonarr_id = int(arr_id)
        else:
            sonarr_id = None

    monitored_updated = False
    search_triggered = False
    need_explicit_monitor_search = bool(sonarr_id) and not (added and search)
    if need_explicit_monitor_search:
        if overlay is None or not overlay.monitored:
            monitored_updated = set_sonarr_series_monitored(
                int(sonarr_id), True, url=url, api_key=api_key
            )
            if monitored_updated:
                _upsert_overlay(int(sonarr_id), monitored=True)
        if search:
            search_triggered = trigger_sonarr_search(
                series_id=int(sonarr_id), url=url, api_key=api_key
            )
    elif added and search:
        search_triggered = True
        if overlay is not None:
            overlay.monitored = True
            session.add(overlay)

    search_for_monitor = bool(search_triggered) or (added and search)
    return {
        'ok': True,
        'instance_key': instance_key,
        'instance_id': instance_id,
        'added': added,
        'skipped_existing': skipped_existing,
        'sonarr_id': sonarr_id,
        'monitored_updated': monitored_updated,
        'search_triggered': search_for_monitor,
    }


def _process_discover_series_playback(session, context: dict[str, Any]) -> dict[str, Any] | None:
    """TMDB Discover show stub: Sonarr add/monitor/search per Placeholder Search Mode.

    Targets TV_PLACEHOLDER_SEARCH_MODE instances. Prefer matched library path is
    ignored for Discover paths.
    """
    from services.discover.mode import is_tmdb_discover_mode
    from services.postgres.models import TmdbSeries

    if not is_tmdb_discover_mode():
        return None
    tmdb_id = context.get('tmdb_series_id') or (
        context.get('tmdb_id') if context.get('discover_series') else None
    )
    if tmdb_id is None:
        path = str(context.get('file_path') or '')
        m = re.search(r'\{tmdb-(\d+)\}', path, flags=re.I)
        if m and '/Season ' in path.replace('\\', '/'):
            tmdb_id = int(m.group(1))
    if tmdb_id is None:
        return None
    tmdb_id = int(tmdb_id)
    row = session.get(TmdbSeries, tmdb_id)
    if row is None:
        return None

    targets, preference = _discover_instances_for_placeholder_mode('tv')
    if not targets:
        has_any = bool(settings.arr_instances_for_type('sonarr'))
        return {
            'ok': False,
            'reason': 'no_matching_sonarr_instance' if has_any else 'no_sonarr_instance',
            'tmdb_id': tmdb_id,
            'preference': preference,
        }

    monitor_only = _playback_monitor_only_no_search()
    search = not monitor_only
    instance_results: list[dict[str, Any]] = []
    for inst in targets:
        try:
            result = _process_discover_series_on_instance(
                session, row=row, tmdb_id=tmdb_id, inst=inst, search=search
            )
        except Exception as exc:
            key = str(inst.get('instance_key') or inst.get('key') or '')
            logger.warning(
                f"Discover series playback failed tmdb={tmdb_id} instance={key}: {exc}",
                extra={'emoji_type': 'warning'},
            )
            result = {
                'ok': False,
                'reason': 'instance_error',
                'instance_key': key,
                'tmdb_id': tmdb_id,
                'detail': str(exc),
            }
        instance_results.append(result)

    any_ok = any(bool(r.get('ok')) for r in instance_results)
    search_for_monitor = any(bool(r.get('search_triggered')) for r in instance_results if r.get('ok'))
    added = any(bool(r.get('added')) for r in instance_results if r.get('ok'))
    skipped_existing = any(bool(r.get('skipped_existing')) for r in instance_results if r.get('ok'))
    monitored_updated = any(bool(r.get('monitored_updated')) for r in instance_results if r.get('ok'))
    sonarr_ids = [r.get('sonarr_id') for r in instance_results if r.get('ok') and r.get('sonarr_id')]

    if not any_ok:
        first_fail = next((r for r in instance_results if not r.get('ok')), {})
        return {
            'ok': False,
            'reason': first_fail.get('reason') or 'sonarr_all_failed',
            'tmdb_id': tmdb_id,
            'preference': preference,
            'instances': instance_results,
            'detail': first_fail.get('detail'),
        }

    intents: list[StatusIntent] = []
    ph_rows = session.query(Placeholder).filter(Placeholder.tmdb_series_id == tmdb_id).all()
    for ph in ph_rows:
        intents.append(
            StatusIntent(
                placeholder_id=int(ph.id),
                new_status=DisplayStatus.SEARCHING.value if search_for_monitor else DisplayStatus.REQUEST.value,
                reason=render_message("queue.searching", {}) if search_for_monitor else None,
                source=StatusSource.EVENT_PLAYBACK_STARTED,
                trigger_nfo_refresh=True,
            )
        )
    if intents:
        StatusOrchestrator(session=session).apply_and_project_statuses(intents)
    _activate_queue_monitor_after_playback_search(
        session, intents, search_triggered=search_for_monitor
    )

    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    try:
        from services.discover.determine import run_discover_series_determination
        from services.discover.materialize_series import apply_tmdb_series_materialization

        run_discover_series_determination(tmdb_ids=[tmdb_id])
        apply_tmdb_series_materialization(tmdb_id)
    except Exception as exc:
        logger.warning(
            f"Discover series post-playback rematerialize failed tmdb={tmdb_id}: {exc}",
            extra={'emoji_type': 'warning'},
        )

    return {
        'ok': True,
        'event': 'playback_start',
        'media_type': 'episode',
        'playback_kind': 'placeholder',
        'discover': True,
        'discover_series': True,
        'tmdb_id': tmdb_id,
        'added': added,
        'skipped_existing': skipped_existing,
        'sonarr_id': sonarr_ids[0] if len(sonarr_ids) == 1 else None,
        'sonarr_ids': sonarr_ids,
        'monitored_updated': monitored_updated,
        'search_triggered': search_for_monitor,
        'monitor_only': monitor_only,
        'preference': preference,
        'chosen_instances': [
            str(r.get('instance_key') or '') for r in instance_results if r.get('ok')
        ],
        'instances': instance_results,
    }



def _process_movie_playback(session, payload: dict[str, Any], context: dict[str, Any], source_instance: str | None) -> dict[str, Any]:
    discover_result = _process_discover_movie_playback(session, context)
    if discover_result is not None:
        return discover_result

    movie_rows = _find_movie_rows(
        session,
        tmdb_id=context.get('tmdb_id'),
        imdb_id=context.get('imdb_id'),
        file_path=context.get('file_path'),
    )
    # Seed media player IDs directly from playback payload if available
    plex_key = str(context.get('plex_id') or '').strip()
    jelly_key = str(context.get('jellyfin_id') or '').strip()
    if plex_key or jelly_key:
        stamp_rows = _movies_for_media_id_stamp(session, movie_rows, context)
        _stamp_movie_media_ids(session, stamp_rows, plex_key=plex_key, jelly_key=jelly_key)

    active_rows = _active_rows_by_instance(movie_rows)
    playback_kind = str(context.get('playback_kind') or 'unknown')

    if playback_kind == 'real':
        selection = _select_movie_real_rows(active_rows, context.get('file_path'))
    elif playback_kind == 'placeholder':
        selection = _select_placeholder_rows(
            active_rows,
            media_type='movie',
            file_path=context.get('file_path'),
        )
    else:
        return {'ok': False, 'reason': 'unresolved_movie_playback_kind'}

    selected_rows = selection.get('rows') or []
    if not selected_rows:
        return {
            'ok': True,
            'event': 'playback_start',
            'media_type': 'movie',
            'playback_kind': playback_kind,
            'skipped': 'real_movie_noop' if playback_kind == 'real' else 'no_active_row',
            'selection_reason': selection.get('selection_reason'),
            'root_match': selection.get('root_match'),
            'qualifying_instances': selection.get('qualifying_instances') or sorted(active_rows.keys()),
        }

    per_instance_results: list[dict[str, Any]] = []
    fallback_job_id: int | None = None
    for row in selected_rows:
        result = _run_movie_search_for_row(session, row)
        per_instance_results.append(result)
        chosen_instance = str(result.get('instance') or '')
        # Ambiguous real movie plays reuse Placeholder Always/Fallback selection, including
        # delayed Fallback tries after timeout (same as placeholder / real TV).
        if result.get('search_triggered') and _should_schedule_delayed_fallback(
            selection, chosen_instance, media_type='movie'
        ):
            fallback_job_id = _enqueue_delayed_fallback(
                session,
                media_type='movie',
                payload=payload,
                preferred_instance=str(selection.get('preferred_instance') or ''),
                fallback_instances=list(
                    selection.get('fallback_instances')
                    or ([selection.get('fallback_instance')] if selection.get('fallback_instance') else [])
                ),
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
        'root_match': selection.get('root_match'),
        'fallback_job_id': fallback_job_id,
        'results': per_instance_results,
    }


def _process_episode_playback(session, payload: dict[str, Any], context: dict[str, Any], source_instance: str | None) -> dict[str, Any]:
    discover_result = _process_discover_series_playback(session, context)
    if discover_result is not None:
        return discover_result

    # Ensure density-stub path from context is visible to search helpers that read payload.
    event_payload = payload
    ctx_path = context.get('file_path')
    if ctx_path and not _extract_file_path(payload):
        event_payload = dict(payload)
        event_payload['file_path'] = ctx_path

    sonarr_id, _ = _extract_series_ids(event_payload)
    series_rows = _find_series_rows(
        session,
        sonarr_id=sonarr_id,
        tvdb_id=context.get('tvdb_id'),
        imdb_id=context.get('imdb_id'),
        file_path=context.get('file_path') or _extract_file_path(event_payload),
    )
    # Density stub / Tracearr may only give us series_id on path_info.
    if not series_rows:
        path_info = context.get('path_info') if isinstance(context.get('path_info'), dict) else {}
        sid = path_info.get('series_id') or context.get('series_id')
        if sid is not None:
            row = session.query(Series).filter(Series.id == int(sid), Series.is_deleted == False).first()  # noqa: E712
            if row is not None:
                series_rows = [row]
    active_rows = _active_rows_by_instance(series_rows)
    playback_kind = str(context.get('playback_kind') or 'unknown')

    if playback_kind == 'placeholder':
        selection = _select_placeholder_rows(
            active_rows,
            media_type='tv',
            file_path=context.get('file_path') or _extract_file_path(event_payload),
        )
    elif playback_kind == 'real':
        selection = _select_tv_real_rows(
            active_rows,
            context.get('file_path') or _extract_file_path(event_payload),
        )
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
        result = _run_episode_search_for_row(session, row, event_payload)
        per_instance_results.append(result)
        chosen_instance = str(result.get('instance') or '')
        if result.get('search_triggered') and _should_schedule_delayed_fallback(
            selection, chosen_instance, media_type='episode'
        ):
            fallback_job_id = _enqueue_delayed_fallback(
                session,
                media_type='episode',
                payload=event_payload,
                preferred_instance=str(selection.get('preferred_instance') or ''),
                fallback_instances=list(selection.get('fallback_instances') or ([selection.get('fallback_instance')] if selection.get('fallback_instance') else [])),
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
    payload = job.payload or {}
    media_type = str(payload.get('media_type') or '').strip().lower()
    preferred_raw = str(payload.get('preferred_instance') or '').strip().lower()
    event_payload = payload.get('payload') if isinstance(payload.get('payload'), dict) else {}

    if media_type not in {'movie', 'episode'}:
        return {'ok': False, 'reason': 'invalid_media_type'}

    if not _fallback_enabled(media_type='movie' if media_type == 'movie' else 'tv'):
        return {'ok': True, 'skipped': 'playback_fallback_disabled'}

    arr_type = 'radarr' if media_type == 'movie' else 'sonarr'
    preferred_instance = _coerce_instance_key(arr_type, preferred_raw)

    raw_queue = payload.get('fallback_instances')
    if not isinstance(raw_queue, list) or not raw_queue:
        legacy = payload.get('fallback_instance')
        raw_queue = [legacy] if legacy else []
    fallback_instances = [
        key
        for key in (_coerce_instance_key(arr_type, item) for item in raw_queue)
        if key
    ]
    if not preferred_instance or not fallback_instances:
        return {'ok': False, 'reason': 'invalid_fallback_instances'}

    context = _resolve_playback_context(session, event_payload)
    head, *tail = fallback_instances

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

        fallback_row = active_rows.get(head)
        if fallback_row is None:
            if tail:
                requeue_id = _enqueue_delayed_fallback(
                    session,
                    media_type='movie',
                    payload=event_payload,
                    preferred_instance=preferred_instance,
                    fallback_instances=tail,
                    source_instance=payload.get('source_instance'),
                )
                return {
                    'ok': True,
                    'skipped': 'fallback_no_active_row',
                    'fallback_instance': head,
                    'fallback_instances': fallback_instances,
                    'requeue_job_id': requeue_id,
                }
            return {
                'ok': True,
                'skipped': 'fallback_no_active_row',
                'fallback_instance': head,
                'fallback_instances': fallback_instances,
            }

        result = _run_movie_search_for_row(session, fallback_row)
        requeue_id = None
        if result.get('search_triggered') and tail and not _preferred_movie_import_succeeded(preferred_row):
            requeue_id = _enqueue_delayed_fallback(
                session,
                media_type='movie',
                payload=event_payload,
                preferred_instance=preferred_instance,
                fallback_instances=tail,
                source_instance=payload.get('source_instance'),
            )
        return {
            'ok': True,
            'event': 'playback_fallback',
            'media_type': 'movie',
            'preferred_instance': preferred_instance,
            'fallback_instance': head,
            'fallback_instances': fallback_instances,
            'requeue_job_id': requeue_id,
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

    fallback_row = active_rows.get(head)
    if fallback_row is None:
        if tail:
            requeue_id = _enqueue_delayed_fallback(
                session,
                media_type='episode',
                payload=event_payload,
                preferred_instance=preferred_instance,
                fallback_instances=tail,
                source_instance=payload.get('source_instance'),
            )
            return {
                'ok': True,
                'skipped': 'fallback_no_active_row',
                'fallback_instance': head,
                'fallback_instances': fallback_instances,
                'requeue_job_id': requeue_id,
            }
        return {
            'ok': True,
            'skipped': 'fallback_no_active_row',
            'fallback_instance': head,
            'fallback_instances': fallback_instances,
        }

    result = _run_episode_search_for_row(session, fallback_row, event_payload)
    requeue_id = None
    if result.get('search_triggered') and tail and not _preferred_episode_import_succeeded(session, preferred_row, event_payload):
        requeue_id = _enqueue_delayed_fallback(
            session,
            media_type='episode',
            payload=event_payload,
            preferred_instance=preferred_instance,
            fallback_instances=tail,
            source_instance=payload.get('source_instance'),
        )
    return {
        'ok': True,
        'event': 'playback_fallback',
        'media_type': 'episode',
        'preferred_instance': preferred_instance,
        'fallback_instance': head,
        'fallback_instances': fallback_instances,
        'requeue_job_id': requeue_id,
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


def apply_search_queued_for_playback(session, payload: dict[str, Any]) -> int:
    """Apply DisplayStatus.SEARCH_QUEUED to placeholder(s) matching a playback payload.

    Used by the webhook receiver when a playback_start event arrives while the
    startup-sync gate is closed. Provides immediate UI feedback ("Search queued")
    so the user knows their playback request was received and will be processed
    once prerequisites finish.

    Returns: number of intents successfully applied (0 if no matching placeholders).

    Caller is responsible for owning the transaction. We DO NOT commit here.
    """
    try:
        context = _resolve_playback_context(session, payload)
    except Exception as exc:
        logger.debug(
            f"apply_search_queued_for_playback: context resolution failed: {exc}",
            extra={'emoji_type': 'debug'},
        )
        return 0

    media_type = str(context.get('media_type') or 'unknown')

    intents: list[StatusIntent] = []

    if media_type == 'movie':
        movie_rows = _find_movie_rows(
            session,
            tmdb_id=context.get('tmdb_id'),
            imdb_id=context.get('imdb_id'),
            file_path=context.get('file_path'),
        )
        active_rows = _active_rows_by_instance(movie_rows)
        plex_key = str(context.get('plex_id') or '').strip()
        jelly_key = str(context.get('jellyfin_id') or '').strip()
        if plex_key or jelly_key:
            stamp_rows = _movies_for_media_id_stamp(session, list(active_rows.values()), context)
            _stamp_movie_media_ids(session, stamp_rows, plex_key=plex_key, jelly_key=jelly_key)
        for row in active_rows.values():
            ph_rows = (
                session.query(Placeholder)
                .filter(
                    Placeholder.movie_id == row.id,
                    Placeholder.has_placeholder == True,  # noqa: E712
                )
                .all()
            )
            for ph in ph_rows:
                intents.append(
                    StatusIntent(
                        placeholder_id=int(ph.id),
                        new_status=DisplayStatus.SEARCH_QUEUED.value,
                        reason='search_queued',
                        source=StatusSource.EVENT_PLAYBACK_STARTED,
                        trigger_nfo_refresh=True,
                        metadata={'gated_playback': True},
                    )
                )
    elif media_type == 'episode':
        sonarr_id, _tvdb = _extract_series_ids(payload)
        path_info = context.get('path_info') if isinstance(context.get('path_info'), dict) else {}
        series_rows = _find_series_rows(
            session,
            sonarr_id=sonarr_id,
            tvdb_id=context.get('tvdb_id'),
            imdb_id=context.get('imdb_id'),
            file_path=context.get('file_path'),
        )
        # Density stub / Tracearr / episode-level TVDB may only give series_id on path_info.
        if not series_rows:
            sid = path_info.get('series_id') or context.get('series_id')
            if sid is not None:
                row = session.query(Series).filter(Series.id == int(sid), Series.is_deleted == False).first()  # noqa: E712
                if row is not None:
                    series_rows = [row]
        active_rows = _active_rows_by_instance(series_rows)
        path_series_id = path_info.get('series_id')
        try:
            path_series_id_int = int(path_series_id) if path_series_id is not None else None
        except (TypeError, ValueError):
            path_series_id_int = None
        for series_row in active_rows.values():
            season_number = None
            episode_number = None
            try:
                season_number, episode_number = _extract_season_episode(payload)
                targets, _meta = _collect_episode_targets(session, series_row, season_number, episode_number)
            except Exception:
                targets = []
            plex_key = str(context.get('plex_id') or '').strip()
            jelly_key = str(context.get('jellyfin_id') or '').strip()
            if plex_key or jelly_key:
                # Only stamp the path-matched series (or path-matched episodes) so sibling
                # Arr rows do not inherit the played Plex/Jellyfin item id.
                if path_series_id_int is not None:
                    if int(getattr(series_row, 'id', 0) or 0) != path_series_id_int:
                        stamp_targets = []
                    else:
                        stamp_targets = _episodes_for_media_id_stamp(
                            session,
                            targets,
                            file_path=context.get('file_path'),
                            season_number=season_number,
                            episode_number=episode_number,
                        )
                elif _normalize_path(context.get('file_path')):
                    stamp_targets = _episodes_for_media_id_stamp(
                        session,
                        targets,
                        file_path=context.get('file_path'),
                        season_number=None,
                        episode_number=None,
                    )
                else:
                    stamp_targets = []
                _stamp_episode_media_ids(
                    session,
                    stamp_targets,
                    plex_key=plex_key,
                    jelly_key=jelly_key,
                )

            # Season/series density stubs have episode_id null; status the stub row itself.
            file_path = _normalize_path(context.get('file_path')) or _extract_file_path(payload)
            season_stub_row = _path_is_season_density_stub(session, series_row, file_path, payload)
            series_density_stub = (
                False
                if season_stub_row
                else _path_is_series_density_stub(session, series_row, file_path, payload)
            )
            density_stub_ph = season_stub_row
            if density_stub_ph is None and series_density_stub:
                density_stub_ph = _find_active_series_density_stub(session, int(series_row.id))
            if density_stub_ph is None:
                stub_kind = str(path_info.get('density_stub_kind') or '').strip().lower()
                if stub_kind == 'season' and path_series_id_int == int(getattr(series_row, 'id', 0) or 0):
                    season_id = path_info.get('season_id')
                    try:
                        season_id_int = int(season_id) if season_id is not None else None
                    except (TypeError, ValueError):
                        season_id_int = None
                    if season_id_int is not None:
                        density_stub_ph = _find_active_season_density_stub(
                            session, season_id=season_id_int
                        )
                elif stub_kind == 'series' and path_series_id_int == int(getattr(series_row, 'id', 0) or 0):
                    density_stub_ph = _find_active_series_density_stub(session, int(series_row.id))

            if density_stub_ph is not None and getattr(density_stub_ph, 'id', None):
                intents.append(
                    StatusIntent(
                        placeholder_id=int(density_stub_ph.id),
                        new_status=DisplayStatus.SEARCH_QUEUED.value,
                        reason='search_queued',
                        source=StatusSource.EVENT_PLAYBACK_STARTED,
                        trigger_nfo_refresh=True,
                        metadata={'gated_playback': True, 'density_stub': True},
                    )
                )
                continue

            episode_ids = [int(ep.id) for ep in targets if getattr(ep, 'id', None)]
            if not episode_ids:
                continue
            ph_rows = (
                session.query(Placeholder)
                .filter(
                    Placeholder.episode_id.in_(episode_ids),
                    Placeholder.has_placeholder == True,  # noqa: E712
                )
                .all()
            )
            for ph in ph_rows:
                intents.append(
                    StatusIntent(
                        placeholder_id=int(ph.id),
                        new_status=DisplayStatus.SEARCH_QUEUED.value,
                        reason='search_queued',
                        source=StatusSource.EVENT_PLAYBACK_STARTED,
                        trigger_nfo_refresh=True,
                        metadata={'gated_playback': True},
                    )
                )

    if not intents:
        return 0

    try:
        return int(StatusOrchestrator(session=session).apply_status_intents(intents))
    except Exception as exc:
        logger.warning(
            f"apply_search_queued_for_playback: failed to apply intents: {exc}",
            extra={'emoji_type': 'warning'},
        )
        return 0
