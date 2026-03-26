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
    fetch_radarr_movies,
    fetch_sonarr_episodefile,
    fetch_sonarr_episodes,
    fetch_sonarr_series,
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


def _movie_folder_name(title: str, year: int, tmdbid: int) -> str:
    if year:
        return f"{_sanitize_name(title)} ({year}) {{tmdb-{tmdbid}}}"
    return f"{_sanitize_name(title)} {{tmdb-{tmdbid}}}"


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


def _movie_fields(entry: Dict, is_4k: bool) -> Dict:
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
        'remote_poster': entry.get('remotePoster'),
        'radarr_overview': entry.get('overview'),
        'last_found_in_radarr': datetime.now(timezone.utc),
        'is_deleted': False,
    }


def _series_fields(entry: Dict, is_4k: bool) -> Dict:
    stats = entry.get('statistics') or {}
    title = entry.get('title') or 'Unknown'
    year = _extract_year(entry.get('year') or entry.get('firstAired'), 0)
    tvdbid = int(entry.get('tvdbId') or entry.get('tvdbid') or 0)
    placeholder_folder = _placeholder_series_folder(entry, title=title, year=year, tvdbid=tvdbid, is_4k=is_4k)
    return {
        'title': title,
        'year': year,
        'tvdbid': tvdbid,
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
        'remote_poster': entry.get('remotePoster'),
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
        .filter(and_(Movie.tmdbid == fields['tmdbid'], Movie.is_4k == fields['is_4k']))
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
        .filter(and_(Series.tvdbid == fields['tvdbid'], Series.is_4k == fields['is_4k']))
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


def _iter_arr_endpoints(types: Tuple[str, ...], is_4k: bool) -> Iterable[Tuple[str, str, str, bool]]:
    if 'movie' in types:
        if is_4k:
            if settings.RADARR_4K_URL and settings.RADARR_4K_API_KEY:
                yield ('movie', settings.RADARR_4K_URL, settings.RADARR_4K_API_KEY, True)
        else:
            if settings.RADARR_URL and settings.RADARR_API_KEY:
                yield ('movie', settings.RADARR_URL, settings.RADARR_API_KEY, False)

    if 'series' in types:
        if is_4k:
            if settings.SONARR_4K_URL and settings.SONARR_4K_API_KEY:
                yield ('series', settings.SONARR_4K_URL, settings.SONARR_4K_API_KEY, True)
        else:
            if settings.SONARR_URL and settings.SONARR_API_KEY:
                yield ('series', settings.SONARR_URL, settings.SONARR_API_KEY, False)


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
        for content_type, base_url, api_key, sync_is_4k in _iter_arr_endpoints(types, is_4k):
            if content_type == 'movie':
                movies = fetch_radarr_movies(base_url, api_key)
                seen_tmdbids = set()
                for movie in movies:
                    fields = _movie_fields(movie, sync_is_4k)
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
                        .filter(and_(Movie.is_4k == sync_is_4k, ~Movie.tmdbid.in_(seen_tmdbids)))
                        .update({'is_deleted': True}, synchronize_session=False)
                    )
                    stats['movies_marked_deleted'] += int(marked_movies or 0)
                session.commit()

            if content_type == 'series':
                series_items = fetch_sonarr_series(base_url, api_key)
                seen_tvdbids = set()

                for series_entry in series_items:
                    s_fields = _series_fields(series_entry, sync_is_4k)
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
                        .filter(and_(Series.is_4k == sync_is_4k, ~Series.tvdbid.in_(seen_tvdbids)))
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
