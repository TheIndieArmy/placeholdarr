import os
import re
from datetime import date, datetime, timezone
from typing import Dict, Iterable, Tuple

from sqlalchemy import and_

from core.config import settings
from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import Episode, Movie, Season, Series
from services.source_of_truth.arr_api import (
    fetch_radarr_movie,
    fetch_radarr_movies,
    fetch_sonarr_episodefile,
    fetch_sonarr_episodes,
    fetch_sonarr_series,
    fetch_sonarr_series_item,
)


def _extract_year(value, fallback: int = 0) -> int:
    if value is None:
        return fallback
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if len(text) >= 4 and text[:4].isdigit():
        return int(text[:4])
    return fallback


def _extract_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00')).date()
    except Exception:
        pass
    try:
        return datetime.strptime(text[:10], '%Y-%m-%d').date()
    except Exception:
        return None


def _extract_quality_name(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, dict):
        nested = value.get('quality')
        if isinstance(nested, dict):
            if nested.get('name'):
                return str(nested.get('name'))
            if nested.get('title'):
                return str(nested.get('title'))
        if value.get('name'):
            return str(value.get('name'))
        if value.get('title'):
            return str(value.get('title'))
    return None


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _extract_image_url(entry: Dict, cover_types: tuple[str, ...]) -> str | None:
    normalized_cover_types = {str(ct or '').strip().lower() for ct in cover_types}

    # Some ARR payloads expose artwork as top-level convenience fields.
    if {'fanart', 'background', 'backdrop'} & normalized_cover_types:
        top_fanart = entry.get('remoteFanart') or entry.get('fanart') or entry.get('backdrop')
        if top_fanart:
            return str(top_fanart)
    if 'banner' in normalized_cover_types:
        top_banner = entry.get('remoteBanner') or entry.get('banner')
        if top_banner:
            return str(top_banner)

    images = entry.get('images') or []
    for img in images:
        if not isinstance(img, dict):
            continue
        cover_type = str(img.get('coverType') or '').strip().lower()
        if cover_type in normalized_cover_types:
            url = img.get('remoteUrl') or img.get('url')
            if url:
                return str(url)
    return None


def _fill_missing_movie_art(fields: Dict, movie_entry: Dict, base_url: str, api_key: str) -> Dict:
    if fields.get('remote_poster') and fields.get('remote_fanart'):
        return fields
    movie_id = movie_entry.get('id')
    if not movie_id:
        return fields
    detail_entry = fetch_radarr_movie(int(movie_id), base_url, api_key)
    if not isinstance(detail_entry, dict):
        return fields

    if not fields.get('remote_poster'):
        fields['remote_poster'] = _extract_poster_url(detail_entry)
    if not fields.get('remote_fanart'):
        fields['remote_fanart'] = _extract_image_url(detail_entry, ('fanart', 'background', 'backdrop'))
    if isinstance(fields.get('radarr_payload_raw'), dict):
        fields['radarr_payload_raw'] = detail_entry
    return fields


def _fill_missing_series_art(fields: Dict, series_entry: Dict, base_url: str, api_key: str) -> Dict:
    has_backdrop_like = bool(fields.get('remote_fanart') or fields.get('remote_banner'))
    if fields.get('remote_poster') and has_backdrop_like:
        return fields
    series_id = series_entry.get('id')
    if not series_id:
        return fields
    detail_entry = fetch_sonarr_series_item(int(series_id), base_url, api_key)
    if not isinstance(detail_entry, dict):
        return fields

    if not fields.get('remote_poster'):
        fields['remote_poster'] = _extract_poster_url(detail_entry)
    if not fields.get('remote_fanart'):
        fields['remote_fanart'] = _extract_image_url(detail_entry, ('fanart', 'background', 'backdrop'))
    if not fields.get('remote_banner'):
        fields['remote_banner'] = _extract_image_url(detail_entry, ('banner',))
    if isinstance(fields.get('sonarr_payload_raw'), dict):
        fields['sonarr_payload_raw'] = detail_entry
    return fields


def _resolve_episode_file_payload(entry: Dict, base_url: str, api_key: str) -> Dict:
    ep_file = entry.get('episodeFile') or {}
    if isinstance(ep_file, dict) and ep_file.get('path'):
        return ep_file

    # Sonarr may return episodeFileId without embedding episodeFile on /episode.
    episode_file_id = entry.get('episodeFileId')
    if episode_file_id:
        fetched = fetch_sonarr_episodefile(int(episode_file_id), base_url, api_key)
        if isinstance(fetched, dict):
            return fetched
    return ep_file if isinstance(ep_file, dict) else {}


def _derive_movie_arr_status(entry: Dict) -> str | None:
    """Derive a coarse ARR availability/download status from Radarr payload fields.

    Radarr's movie.status field = release lifecycle: tba / announced / inCinemas / released / deleted.
    The UI states 'Missing' / 'Downloaded' are derived from monitored + hasFile + status.
    This captures that derivation during sync so we have it without a separate queue call.

    Values: 'downloaded' | 'missing' | 'unmonitored' | 'unreleased' | 'announced' | None
    Deliberately kept separate from radarr_release_status (lifecycle) and placeholder_status
    (our own placeholder tracking system).
    """
    has_file = bool(entry.get('hasFile') or (entry.get('movieFile') or {}).get('path'))
    if has_file:
        return 'downloaded'
    monitored = bool(entry.get('monitored'))
    if not monitored:
        return 'unmonitored'
    release_status = str(entry.get('status') or '').lower()
    if release_status == 'released':
        return 'missing'
    if release_status in ('announced', 'tba'):
        return 'announced'
    if release_status == 'incinemas':
        return 'unreleased'
    return None


def _derive_episode_arr_status(entry: Dict, episode_file: Dict) -> str | None:
    """Derive a coarse ARR availability/download status for a Sonarr episode.

    Sonarr series.status = continuing / ended / upcoming / deleted (series lifecycle).
    Episode-level 'missing' / 'downloaded' are computed from hasFile + monitored + airDate.

    Values: 'downloaded' | 'missing' | 'unaired' | 'unmonitored' | None
    Distinct from the series-level sonarr_status and placeholder_status.
    """
    has_file = bool(entry.get('hasFile') or (episode_file or {}).get('path'))
    if has_file:
        return 'downloaded'
    monitored = bool(entry.get('monitored'))
    if not monitored:
        return 'unmonitored'
    air_dt = _extract_date(entry.get('airDate') or entry.get('airDateUtc'))
    if air_dt is None:
        return None
    if air_dt > date.today():
        return 'unaired'
    return 'missing'


def _sanitize_name(value: str | None) -> str:
    text = str(value or "").strip()
    text = re.sub(r'[<>:"/\\|?*]', "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or "unknown"


def _movie_library_root(is_4k: bool) -> str:
    return settings.MOVIE_LIBRARY_4K_FOLDER if is_4k and settings.MOVIE_LIBRARY_4K_FOLDER else settings.MOVIE_LIBRARY_FOLDER


def _tv_library_root(is_4k: bool) -> str:
    return settings.TV_LIBRARY_4K_FOLDER if is_4k and settings.TV_LIBRARY_4K_FOLDER else settings.TV_LIBRARY_FOLDER


def _default_instance_key(content_type: str, is_4k: bool) -> str:
    """Get default/first instance key for given content type and 4k flag from configured instances."""
    arr_type = 'radarr' if content_type == 'movie' else 'sonarr'
    # Try to find matching 4k flag first
    for item in (getattr(settings, 'configured_arr_instances', []) or []):
        if str(item.get('arr_type', '')).lower() == arr_type and bool(item.get('is_4k', False)) == is_4k:
            return str(item.get('instance_key', '')).lower()
    # Fall back to first instance of type
    for item in (getattr(settings, 'configured_arr_instances', []) or []):
        if str(item.get('arr_type', '')).lower() == arr_type:
            return str(item.get('instance_key', '')).lower()
    raise ValueError(f"No {arr_type} instances configured for default key generation")


def _movie_folder_name(title: str, year: int, tmdbid: int) -> str:
    if year:
        return f"{_sanitize_name(title)} ({year}) {{tmdb-{tmdbid}}}"
    return f"{_sanitize_name(title)} {{tmdb-{tmdbid}}}"


def _extract_poster_url(entry: Dict) -> str | None:
    """Extract poster URL from an ARR entry via fallback chain.

    Priority:
    1. ``remotePoster`` top-level field (Radarr/Sonarr convenience field)
    2. ``images`` array entry with ``coverType == 'poster'``, using ``remoteUrl`` then ``url``
    3. First entry in ``images`` array as last resort
    """
    url = entry.get('remotePoster')
    if url:
        return url
    images = entry.get('images') or []
    for img in images:
        if isinstance(img, dict) and str(img.get('coverType') or '').lower() in ('poster', 'cover'):
            url = img.get('remoteUrl') or img.get('url')
            if url:
                return str(url)
    if images and isinstance(images[0], dict):
        url = images[0].get('remoteUrl') or images[0].get('url')
        if url:
            return url
    return None


def _series_folder_name(title: str, year: int, tvdbid: int) -> str:
    if year:
        return f"{_sanitize_name(title)} ({year}) {{tvdb-{tvdbid}}}"
    return f"{_sanitize_name(title)} {{tvdb-{tvdbid}}}"


def _placeholder_movie_folder(entry: Dict, *, title: str, year: int, tmdbid: int, is_4k: bool) -> str:
    root = _movie_library_root(is_4k)
    arr_path = entry.get('path') or entry.get('folderPath') or None
    folder_name = os.path.basename(arr_path) if arr_path else _movie_folder_name(title, year, tmdbid)
    return os.path.join(root, folder_name)


def _placeholder_series_folder(entry: Dict, *, title: str, year: int, tvdbid: int, is_4k: bool) -> str:
    root = _tv_library_root(is_4k)
    arr_path = entry.get('path') or entry.get('folderPath') or None
    folder_name = os.path.basename(arr_path) if arr_path else _series_folder_name(title, year, tvdbid)
    return os.path.join(root, folder_name)


def _movie_fields(entry: Dict, is_4k: bool, instance_key: str) -> Dict:
    movie_file = entry.get('movieFile') or {}
    movie_file_path = movie_file.get('path')
    title = entry.get('title') or 'Unknown'
    year = _extract_year(entry.get('year') or entry.get('inCinemas') or entry.get('physicalRelease'), 0)
    tmdbid = int(entry.get('tmdbId') or entry.get('tmdb') or 0)
    placeholder_folder = _placeholder_movie_folder(entry, title=title, year=year, tmdbid=tmdbid, is_4k=is_4k)
    return {
        'title': title,
        'year': year,
        'tmdbid': tmdbid,
        'instance_key': str(instance_key).strip().lower(),
        'is_4k': is_4k,
        'radarrid': entry.get('id'),
        'placeholder_folder': placeholder_folder,
        'radarrpath': entry.get('path') or entry.get('rootFolderPath'),
        'radarr_filepath': movie_file_path,
        'moviefile_size': movie_file.get('size') or movie_file.get('sizeOnDisk') or entry.get('sizeOnDisk'),
        'has_file': bool(entry.get('hasFile') or movie_file_path),
        'radarr_quality': _extract_quality_name(movie_file.get('quality')),
        'radarr_release_status': entry.get('status'),
        'radarr_status': _derive_movie_arr_status(entry),
        'theater_release_date': _extract_date(entry.get('inCinemas')),
        'digital_release_date': _extract_date(entry.get('digitalRelease')),
        'physical_release_date': _extract_date(entry.get('physicalRelease')),
        'radarr_monitored': bool(entry.get('monitored')),
        'imdbid': entry.get('imdbId'),
        'remote_poster': _extract_poster_url(entry),
        'remote_fanart': _extract_image_url(entry, ('fanart', 'background')),
        'radarr_runtime': _as_int(entry.get('runtime') or 0, 0) or None,
        'radarr_certification': entry.get('certification'),
        'radarr_genres': entry.get('genres') if isinstance(entry.get('genres'), list) else None,
        'radarr_studio': entry.get('studio'),
        'radarr_ratings': entry.get('ratings') if isinstance(entry.get('ratings'), dict) else None,
        'radarr_collection': entry.get('collection') if isinstance(entry.get('collection'), dict) else None,
        'radarr_actors': entry.get('actors') if isinstance(entry.get('actors'), list) else None,
        'radarr_directors': entry.get('directors') if isinstance(entry.get('directors'), list) else None,
        'radarr_credits': entry.get('writers') if isinstance(entry.get('writers'), list) else None,
        'radarr_trailer': entry.get('youTubeTrailerId') or entry.get('trailer'),
        'radarr_premiered': _extract_date(entry.get('inCinemas') or entry.get('physicalRelease')),
        'radarr_payload_raw': entry,
        'radarr_overview': entry.get('overview'),
        'last_found_in_radarr': datetime.now(timezone.utc),
        'is_deleted': False,
    }


def _series_fields(entry: Dict, is_4k: bool, instance_key: str) -> Dict:
    stats = entry.get('statistics') or {}
    title = entry.get('title') or 'Unknown'
    year = _extract_year(entry.get('year') or entry.get('firstAired'), 0)
    tvdbid = int(entry.get('tvdbId') or entry.get('tvdbid') or 0)
    placeholder_folder = _placeholder_series_folder(entry, title=title, year=year, tvdbid=tvdbid, is_4k=is_4k)
    return {
        'title': title,
        'year': year,
        'tvdbid': tvdbid,
        'instance_key': str(instance_key).strip().lower(),
        'is_4k': is_4k,
        'sonarrid': entry.get('id'),
        'placeholder_folder': placeholder_folder,
        'sonarrpath': entry.get('path') or entry.get('rootFolderPath'),
        'sonarr_series_overview': entry.get('overview'),
        'has_files': bool(stats.get('episodeFileCount', 0)),
        'seriesfile_count': stats.get('episodeFileCount'),
        'sonarr_status': entry.get('status'),
        'sonarr_monitored': bool(entry.get('monitored')),
        'imdbid': entry.get('imdbId'),
        'remote_poster': _extract_poster_url(entry),
        'remote_fanart': _extract_image_url(entry, ('fanart', 'background')),
        'remote_banner': _extract_image_url(entry, ('banner',)),
        'sonarr_runtime': _as_int(entry.get('runtime') or 0, 0) or None,
        'sonarr_certification': entry.get('certification'),
        'sonarr_genres': entry.get('genres') if isinstance(entry.get('genres'), list) else None,
        'sonarr_network': entry.get('network') or entry.get('studio'),
        'sonarr_ratings': entry.get('ratings') if isinstance(entry.get('ratings'), dict) else None,
        'sonarr_tmdbid': _as_int(entry.get('tmdbId') or 0, 0) or None,
        'sonarr_tvmazeid': _as_int(entry.get('tvMazeId') or 0, 0) or None,
        'sonarr_first_aired': _extract_date(entry.get('firstAired')),
        'sonarr_actors': entry.get('actors') if isinstance(entry.get('actors'), list) else None,
        'sonarr_payload_raw': entry,
        'last_found_in_sonarr': datetime.now(timezone.utc),
        'is_deleted': False,
    }


def _episode_fields(series: Series, season: Season, entry: Dict, episode_file: Dict | None = None) -> Dict:
    episode_file = episode_file if isinstance(episode_file, dict) else (entry.get('episodeFile') or {})
    sonarr_filepath = episode_file.get('path')
    season_folder = getattr(season, 'placeholder_folder', None) or ''
    episode_sonarrpath = os.path.dirname(sonarr_filepath) if sonarr_filepath else season_folder
    return {
        'season_id': season.id,
        'episode_number': int(entry.get('episodeNumber') or 0),
        'title': entry.get('title') or f"Episode {int(entry.get('episodeNumber') or 0)}",
        'year': _extract_year(entry.get('airDateUtc') or entry.get('airDate') or series.year, series.year),
        'sonarrid': entry.get('id'),
        'placeholder_folder': season_folder,
        'sonarrpath': episode_sonarrpath,
        'sonarr_filepath': sonarr_filepath,
        'episodefile_size': episode_file.get('size') or episode_file.get('sizeOnDisk'),
        'sonarr_episode_overview': entry.get('overview'),
        'sonarr_episode_tvdbid': _as_int(entry.get('tvdbId') or 0, 0) or None,
        'sonarr_episode_still': _extract_image_url(entry, ('screenshot', 'still', 'cover')),
        'sonarr_episode_directors': entry.get('directors') if isinstance(entry.get('directors'), list) else None,
        'sonarr_episode_credits': entry.get('writers') if isinstance(entry.get('writers'), list) else None,
        'sonarr_payload_raw': entry,
        'sonarr_episodefile_payload_raw': episode_file if isinstance(episode_file, dict) else None,
        'has_file': bool(entry.get('hasFile') or sonarr_filepath or entry.get('episodeFileId')),
        'sonarr_quality': _extract_quality_name(episode_file.get('quality'))
        or _extract_quality_name(entry.get('quality')),
        'sonarr_monitored': bool(entry.get('monitored')),
        'sonarr_status': _derive_episode_arr_status(entry, episode_file),
        'air_date': _extract_date(entry.get('airDate') or entry.get('airDateUtc')),
        'last_found_in_sonarr': datetime.now(timezone.utc),
        'is_deleted': False,
    }


def _upsert_movie(session, fields: Dict):
    existing = (
        session.query(Movie)
        .filter(and_(Movie.tmdbid == fields['tmdbid'], Movie.instance_key == fields['instance_key']))
        .first()
    )
    if existing:
        for key, value in fields.items():
            setattr(existing, key, value)
        return existing, False
    created = Movie(**fields)
    session.add(created)
    return created, True


def _upsert_series(session, fields: Dict):
    existing = (
        session.query(Series)
        .filter(and_(Series.tvdbid == fields['tvdbid'], Series.instance_key == fields['instance_key']))
        .first()
    )
    if existing:
        for key, value in fields.items():
            setattr(existing, key, value)
        return existing, False
    created = Series(**fields)
    session.add(created)
    session.flush()
    return created, True


def _upsert_season(session, series: Series, season_number: int):
    season_folder = os.path.join(
        getattr(series, 'placeholder_folder', None) or _tv_library_root(bool(getattr(series, 'is_4k', False))),
        f"Season {season_number:02d}" if season_number > 0 else "Season 00",
    )
    season = (
        session.query(Season)
        .filter(and_(Season.series_id == series.id, Season.season_number == season_number))
        .first()
    )
    if season:
        season.title = f"{series.title} S{season_number:02d}" if season_number > 0 else f"{series.title} Specials"
        season.year = series.year
        season.sonarrid = series.sonarrid
        season.placeholder_folder = season_folder
        season.sonarrpath = season_folder
        season.is_deleted = False
        return season, False

    created = Season(
        series_id=series.id,
        season_number=season_number,
        title=f"{series.title} S{season_number:02d}" if season_number > 0 else f"{series.title} Specials",
        year=series.year,
        placeholder_folder=season_folder,
        sonarrpath=season_folder,
        sonarrid=series.sonarrid,
        is_deleted=False,
    )
    session.add(created)
    session.flush()
    return created, True


def _upsert_episode(session, fields: Dict):
    existing = (
        session.query(Episode)
        .filter(
            and_(
                Episode.season_id == fields['season_id'],
                Episode.episode_number == fields['episode_number'],
            )
        )
        .first()
    )
    if existing:
        for key, value in fields.items():
            setattr(existing, key, value)
        return existing, False
    created = Episode(**fields)
    session.add(created)
    return created, True


def _iter_arr_endpoints(types: Tuple[str, ...], is_4k: bool) -> Iterable[Tuple[str, str, str, bool, str]]:
    """Yield (arr_type, url, api_key, is_4k, instance_key) tuples for configured ARR instances."""
    for item in (getattr(settings, 'configured_arr_instances', []) or []):
        arr_type = str(item.get('arr_type', '')).lower()
        if arr_type not in types:
            continue
        if bool(item.get('is_4k', False)) != is_4k:
            continue
        url = str(item.get('url', '')).strip()
        api_key = str(item.get('api_key', '')).strip()
        instance_key = str(item.get('instance_key', '')).strip().lower()
        if url and api_key and instance_key:
            yield (arr_type, url, api_key, is_4k, instance_key)


def run_full_sync(
    dry_run: bool = False,
    batch_size: int = 50,
    types: Tuple[str, ...] = ('movie', 'series'),
    is_4k: bool = False,
):
    """Source-of-truth full sync for ARR -> DB materialization."""
    _ = batch_size  # reserved for future chunking without changing public API
    started_at = datetime.now(timezone.utc)
    stats = {
        'movies_seen': 0,
        'movies_created': 0,
        'movies_updated': 0,
        'movies_marked_deleted': 0,
        'series_seen': 0,
        'series_created': 0,
        'series_updated': 0,
        'series_marked_deleted': 0,
        'seasons_marked_deleted': 0,
        'episodes_marked_deleted': 0,
        'seasons_created': 0,
        'episodes_seen': 0,
        'episodes_created': 0,
        'episodes_updated': 0,
    }

    session = get_session()
    try:
        for content_type, base_url, api_key, sync_is_4k, sync_instance_key in _iter_arr_endpoints(types, is_4k):
            if content_type == 'movie':
                movies = fetch_radarr_movies(base_url, api_key)
                seen_tmdbids = set()
                for movie in movies:
                    fields = _movie_fields(movie, sync_is_4k, sync_instance_key)
                    fields = _fill_missing_movie_art(fields, movie, base_url, api_key)
                    if not fields['tmdbid']:
                        continue
                    seen_tmdbids.add(fields['tmdbid'])
                    stats['movies_seen'] += 1
                    _, created = _upsert_movie(session, fields)
                    if created:
                        stats['movies_created'] += 1
                    else:
                        stats['movies_updated'] += 1

                if not dry_run and seen_tmdbids:
                    marked_movies = (
                        session.query(Movie)
                        .filter(and_(Movie.instance_key == sync_instance_key, ~Movie.tmdbid.in_(seen_tmdbids)))
                        .update({'is_deleted': True}, synchronize_session=False)
                    )
                    stats['movies_marked_deleted'] += int(marked_movies or 0)
                session.commit()

            if content_type == 'series':
                series_items = fetch_sonarr_series(base_url, api_key)
                seen_tvdbids = set()

                for series_entry in series_items:
                    s_fields = _series_fields(series_entry, sync_is_4k, sync_instance_key)
                    s_fields = _fill_missing_series_art(s_fields, series_entry, base_url, api_key)
                    if not s_fields['tvdbid']:
                        continue

                    seen_tvdbids.add(s_fields['tvdbid'])
                    stats['series_seen'] += 1
                    series_row, created = _upsert_series(session, s_fields)
                    if created:
                        stats['series_created'] += 1
                    else:
                        stats['series_updated'] += 1

                    sonarr_series_id = series_entry.get('id')
                    episodes = fetch_sonarr_episodes(sonarr_series_id, base_url, api_key) if sonarr_series_id else []
                    season_rows_by_number: Dict[int, Season] = {}
                    season_rollups: Dict[int, Dict[str, int | bool | str | None]] = {}
                    season_overview_by_number: Dict[int, str] = {}
                    include_specials = bool(getattr(settings, 'INCLUDE_SPECIALS', False))
                    for ep in episodes:
                        season_number = int(ep.get('seasonNumber') or 0)
                        if season_number == 0 and not include_specials:
                            continue
                        season_row, season_created = _upsert_season(session, series_row, season_number)
                        season_rows_by_number[season_number] = season_row
                        if season_created:
                            stats['seasons_created'] += 1

                        episode_file = _resolve_episode_file_payload(ep, base_url, api_key)
                        ep_fields = _episode_fields(series_row, season_row, ep, episode_file)

                        overview = str(ep.get('overview') or '').strip()
                        if overview and season_number not in season_overview_by_number:
                            season_overview_by_number[season_number] = overview

                        rollup = season_rollups.setdefault(
                            season_number,
                            {'files': 0, 'episodes': 0, 'monitored': False, 'status': None},
                        )
                        rollup['episodes'] = int(rollup['episodes'] or 0) + 1
                        if ep_fields.get('has_file'):
                            rollup['files'] = int(rollup['files'] or 0) + 1
                        rollup['monitored'] = bool(rollup['monitored']) or bool(ep_fields.get('sonarr_monitored'))
                        if not rollup.get('status') and ep_fields.get('sonarr_status'):
                            rollup['status'] = ep_fields.get('sonarr_status')

                        stats['episodes_seen'] += 1
                        _, ep_created = _upsert_episode(session, ep_fields)
                        if ep_created:
                            stats['episodes_created'] += 1
                        else:
                            stats['episodes_updated'] += 1

                    for season_number, season_row in season_rows_by_number.items():
                        rollup = season_rollups.get(season_number) or {}
                        files_count = int(rollup.get('files') or 0)
                        season_row.has_files = bool(files_count)
                        season_row.seasonfile_count = files_count
                        season_row.sonarr_monitored = bool(rollup.get('monitored'))
                        if rollup.get('status'):
                            season_row.sonarr_status = str(rollup['status'])
                        if season_overview_by_number.get(season_number):
                            season_row.sonarr_season_overview = season_overview_by_number[season_number]
                        season_row.is_deleted = False
                        session.add(season_row)

                if not dry_run and seen_tvdbids:
                    deleted_series_rows = (
                        session.query(Series.id)
                        .filter(and_(Series.instance_key == sync_instance_key, ~Series.tvdbid.in_(seen_tvdbids)))
                        .all()
                    )
                    deleted_series_ids = [row[0] for row in deleted_series_rows]

                    if deleted_series_ids:
                        marked_series = (
                            session.query(Series)
                            .filter(Series.id.in_(deleted_series_ids))
                            .update({'is_deleted': True}, synchronize_session=False)
                        )
                        stats['series_marked_deleted'] += int(marked_series or 0)

                        marked_seasons = (
                            session.query(Season)
                            .filter(Season.series_id.in_(deleted_series_ids))
                            .update({'is_deleted': True}, synchronize_session=False)
                        )
                        stats['seasons_marked_deleted'] += int(marked_seasons or 0)

                        deleted_season_rows = (
                            session.query(Season.id)
                            .filter(Season.series_id.in_(deleted_series_ids))
                            .all()
                        )
                        deleted_season_ids = [row[0] for row in deleted_season_rows]
                        if deleted_season_ids:
                            marked_episodes = (
                                session.query(Episode)
                                .filter(Episode.season_id.in_(deleted_season_ids))
                                .update({'is_deleted': True}, synchronize_session=False)
                            )
                            stats['episodes_marked_deleted'] += int(marked_episodes or 0)

                session.commit()

        elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
        stats['duration_seconds'] = round(elapsed, 2)
        logger.info(f"Source-of-truth fullsync complete: {stats}", extra={'emoji_type': 'success'})
        return stats
    except Exception as e:
        session.rollback()
        logger.error(f"Source-of-truth fullsync failed: {e}", extra={'emoji_type': 'error'})
        raise
    finally:
        session.close()


def sync_radarr_movies_by_ids(
    movie_ids: Iterable[int],
    *,
    base_url: str,
    api_key: str,
    is_4k: bool,
    instance_key: str | None = None,
) -> dict:
    """Targeted movie sync for a specific Radarr instance and movie IDs."""
    ids = sorted({int(mid) for mid in (movie_ids or []) if mid})
    stats = {
        'movies_requested': len(ids),
        'movies_seen': 0,
        'movies_created': 0,
        'movies_updated': 0,
        'movies_marked_deleted': 0,
    }
    if not ids:
        return stats

    effective_instance_key = str(instance_key or _default_instance_key('movie', is_4k)).strip().lower()

    session = get_session()
    try:
        for movie_id in ids:
            movie = fetch_radarr_movie(movie_id, base_url, api_key)
            if not isinstance(movie, dict):
                marked = (
                    session.query(Movie)
                    .filter(and_(Movie.radarrid == movie_id, Movie.instance_key == effective_instance_key))
                    .update({'is_deleted': True}, synchronize_session=False)
                )
                stats['movies_marked_deleted'] += int(marked or 0)
                continue

            fields = _movie_fields(movie, is_4k, effective_instance_key)
            if not fields['tmdbid']:
                continue
            stats['movies_seen'] += 1
            _, created = _upsert_movie(session, fields)
            if created:
                stats['movies_created'] += 1
            else:
                stats['movies_updated'] += 1

        session.commit()
        return stats
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def sync_sonarr_series_by_ids(
    series_ids: Iterable[int],
    *,
    base_url: str,
    api_key: str,
    is_4k: bool,
    instance_key: str | None = None,
) -> dict:
    """Targeted series+episode sync for a specific Sonarr instance and series IDs."""
    ids = sorted({int(sid) for sid in (series_ids or []) if sid})
    stats = {
        'series_requested': len(ids),
        'series_seen': 0,
        'series_created': 0,
        'series_updated': 0,
        'series_marked_deleted': 0,
        'seasons_created': 0,
        'episodes_seen': 0,
        'episodes_created': 0,
        'episodes_updated': 0,
    }
    if not ids:
        return stats

    effective_instance_key = str(instance_key or _default_instance_key('series', is_4k)).strip().lower()

    session = get_session()
    try:
        include_specials = bool(getattr(settings, 'INCLUDE_SPECIALS', False))
        for series_id in ids:
            series_entry = fetch_sonarr_series_item(series_id, base_url, api_key)
            if not isinstance(series_entry, dict):
                marked = (
                    session.query(Series)
                    .filter(and_(Series.sonarrid == series_id, Series.instance_key == effective_instance_key))
                    .update({'is_deleted': True}, synchronize_session=False)
                )
                stats['series_marked_deleted'] += int(marked or 0)
                continue

            s_fields = _series_fields(series_entry, is_4k, effective_instance_key)
            if not s_fields['tvdbid']:
                continue

            stats['series_seen'] += 1
            series_row, created = _upsert_series(session, s_fields)
            if created:
                stats['series_created'] += 1
            else:
                stats['series_updated'] += 1

            episodes = fetch_sonarr_episodes(series_id, base_url, api_key)
            season_rows_by_number: Dict[int, Season] = {}
            season_rollups: Dict[int, Dict[str, int | bool | str | None]] = {}
            season_overview_by_number: Dict[int, str] = {}

            for ep in episodes:
                season_number = int(ep.get('seasonNumber') or 0)
                if season_number == 0 and not include_specials:
                    continue
                season_row, season_created = _upsert_season(session, series_row, season_number)
                season_rows_by_number[season_number] = season_row
                if season_created:
                    stats['seasons_created'] += 1

                episode_file = _resolve_episode_file_payload(ep, base_url, api_key)
                ep_fields = _episode_fields(series_row, season_row, ep, episode_file)

                overview = str(ep.get('overview') or '').strip()
                if overview and season_number not in season_overview_by_number:
                    season_overview_by_number[season_number] = overview

                rollup = season_rollups.setdefault(
                    season_number,
                    {'files': 0, 'episodes': 0, 'monitored': False, 'status': None},
                )
                rollup['episodes'] = int(rollup['episodes'] or 0) + 1
                if ep_fields.get('has_file'):
                    rollup['files'] = int(rollup['files'] or 0) + 1
                rollup['monitored'] = bool(rollup['monitored']) or bool(ep_fields.get('sonarr_monitored'))
                if not rollup.get('status') and ep_fields.get('sonarr_status'):
                    rollup['status'] = ep_fields.get('sonarr_status')

                stats['episodes_seen'] += 1
                _, ep_created = _upsert_episode(session, ep_fields)
                if ep_created:
                    stats['episodes_created'] += 1
                else:
                    stats['episodes_updated'] += 1

            for season_number, season_row in season_rows_by_number.items():
                rollup = season_rollups.get(season_number) or {}
                files_count = int(rollup.get('files') or 0)
                season_row.has_files = bool(files_count)
                season_row.seasonfile_count = files_count
                season_row.sonarr_monitored = bool(rollup.get('monitored'))
                if rollup.get('status'):
                    season_row.sonarr_status = str(rollup['status'])
                if season_overview_by_number.get(season_number):
                    season_row.sonarr_season_overview = season_overview_by_number[season_number]
                season_row.is_deleted = False
                session.add(season_row)

        session.commit()
        return stats
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
