import json
import time
import logging
from typing import Iterable, List, Optional

import requests
from sqlalchemy import text

from services.postgres.db import get_session
from services.postgres.models import SubFlow, Episode, Season
from services.postgres.series_repo import SeriesRepository
from services.utils import get_arr_config, resolve_final_folder, sanitize_filename
from services.placeholders import get_or_create_placeholder
import os
from services.handlers import enqueue_import_list_job
from services.enricher import enqueue_enrichment_job
from core.logger import logger
from core.config import settings


SYNC_LOCK_KEY = 135792468


def _acquire_lock(session) -> bool:
    try:
        res = session.execute(text("SELECT pg_try_advisory_lock(:k)"), {'k': SYNC_LOCK_KEY}).scalar()
        return bool(res)
    except Exception as e:
        logger.debug(f"Advisory lock attempt failed: {e}")
        return False


def _release_lock(session) -> None:
    try:
        session.execute(text("SELECT pg_advisory_unlock(:k)"), {'k': SYNC_LOCK_KEY})
        session.commit()
    except Exception:
        session.rollback()


def _fetch_sonarr_series(is_4k: bool = False) -> List[dict]:
    cfg = get_arr_config('tv', is_4k)
    if not cfg or not cfg.get('url'):
        raise RuntimeError('Sonarr configuration not available')
    url = cfg['url'].rstrip('/') + '/series'
    headers = {'X-Api-Key': cfg['api_key']}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _fetch_radarr_movies(is_4k: bool = False) -> List[dict]:
    cfg = get_arr_config('movie', is_4k)
    if not cfg or not cfg.get('url'):
        raise RuntimeError('Radarr configuration not available')
    url = cfg['url'].rstrip('/') + '/movie'
    headers = {'X-Api-Key': cfg['api_key']}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def run_full_sync(dry_run: bool = True, batch_size: int = 50, types: Iterable[str] = ('tv',), is_4k: bool = False, report_path: Optional[str] = None) -> dict:
    """Run a Full ARR-backed sync.

    dry_run: if True, do not persist DB changes or enqueue jobs; instead return a plan/report.
    types: tuple of 'tv' and/or 'movie'.
    """
    session = get_session()
    report = {
        'tv': {'found': 0, 'created': 0, 'enriched': 0, 'enqueued': 0, 'errors': []},
        'movie': {'found': 0, 'created': 0, 'enriched': 0, 'enqueued': 0, 'errors': []},
        'started': time.time(),
    }

    got_lock = _acquire_lock(session)
    if not got_lock:
        session.close()
        raise RuntimeError('Could not acquire sync advisory lock; another sync may be running')

    try:
        if 'tv' in types:
            try:
                series_list = _fetch_sonarr_series(is_4k=is_4k)
            except Exception as e:
                logger.error(f"Failed to fetch Sonarr series: {e}")
                report['tv']['errors'].append(str(e))
                series_list = []

            report['tv']['found'] = len(series_list)
            # Process in batches
            for i in range(0, len(series_list), batch_size):
                batch = series_list[i:i + batch_size]
                tvdbs_to_enqueue = []
                for s in batch:
                    try:
                        tvdb = s.get('tvdbId') or s.get('tvdb')
                        sonarr_id = s.get('id')
                        title = s.get('title')
                        year = s.get('year')
                        if not tvdb:
                            # skip items without tvdb id
                            report['tv']['errors'].append(f"Series missing tvdbId: {title}")
                            continue

                        # Dry-run: collect planned actions
                        if dry_run:
                            tvdbs_to_enqueue.append(tvdb)
                            continue

                        # Non-dry: ensure DB row exists and enrich
                        repo = SeriesRepository(session)
                        series = repo.get_by_tvdbid(int(tvdb), is_4k)
                        if not series:
                            # create minimal Series row
                            series = repo.add(
                                title=title,
                                year=year or 0,
                                tvdbid=int(tvdb),
                                is_4k=is_4k,
                                dummypath='')
                            report['tv']['created'] += 1

                        # Ensure seasons/episodes exist by seeding from Sonarr when needed
                        try:
                            episodes = []
                            include_specials = getattr(settings, 'INCLUDE_SPECIALS', False)
                            # Try a fast DB count to avoid fetching from Sonarr when possible
                            try:
                                if include_specials:
                                    db_episode_count = session.query(__import__('services.postgres.models', fromlist=['Season']).Season).join(__import__('services.postgres.models', fromlist=['Episode']).Episode).filter(__import__('services.postgres.models', fromlist=['Season']).Season.series_id == series.id).count()
                                else:
                                    db_episode_count = session.query(__import__('services.postgres.models', fromlist=['Season']).Season).join(__import__('services.postgres.models', fromlist=['Episode']).Episode).filter(__import__('services.postgres.models', fromlist=['Season']).Season.series_id == series.id, __import__('services.postgres.models', fromlist=['Season']).Season.season_number > 0).count()
                            except Exception:
                                db_episode_count = 0

                            # Read Sonarr-reported episode count; treat 0 as unknown
                            remote_count = None
                            try:
                                raw = s.get('episodeCount') or (s.get('statistics') or {}).get('episodeCount')
                                if raw is not None:
                                    rc = int(raw)
                                    if rc > 0:
                                        remote_count = rc
                            except Exception:
                                remote_count = None

                            # If remote_count is absent or indicates more episodes than DB has, fetch episodes
                            if remote_count is None or db_episode_count < remote_count:
                                cfg = get_arr_config('tv', is_4k)
                                headers = {'X-Api-Key': cfg['api_key']} if cfg and cfg.get('api_key') else {}
                                ep_url = None
                                if sonarr_id and cfg and cfg.get('url'):
                                    ep_url = f"{cfg['url'].rstrip('/')}/episode?seriesId={sonarr_id}"
                                elif tvdb and cfg and cfg.get('url'):
                                    lu = requests.get(f"{cfg['url'].rstrip('/')}/series/lookup", params={'term': f"tvdb:{int(tvdb)}"}, headers=headers, timeout=10)
                                    if lu.ok:
                                        res = lu.json()
                                        if isinstance(res, list) and res:
                                            sid = res[0].get('id')
                                            ep_url = f"{cfg['url'].rstrip('/')}/episode?seriesId={sid}"

                                if ep_url:
                                    r = requests.get(ep_url, headers=headers, timeout=15)
                                    if r.ok:
                                        episodes = r.json() or []
                                        if not include_specials:
                                            episodes = [e for e in episodes if e.get('seasonNumber', 0) > 0]

                            # Persist fetched episodes (if any)
                            if episodes:
                                created = repo.add_missing_seasons_and_episodes(series, episodes)
                                try:
                                    created_count = int(created)
                                except Exception:
                                    created_count = len(episodes)
                                report['tv'].setdefault('seeded_episodes', 0)
                                report['tv']['seeded_episodes'] += created_count
                                skipped = max(0, len(episodes) - created_count)
                                report['tv'].setdefault('skipped_existing_episodes', 0)
                                report['tv']['skipped_existing_episodes'] += skipped
                        except Exception as e:
                            logger.warning(f"Episode seeding check/fetch failed for TVDB {tvdb}: {e}")
                            report['tv']['errors'].append(str(e))

                        # Optionally create Placeholder rows for seeded episodes (opt-in)
                        if getattr(settings, 'FULL_SYNC_CREATE_PLACEHOLDERS', False):
                            try:
                                normalized_tvdb = getattr(series, 'tvdbid', None) or None
                                eps = session.query(Episode).join(Season).filter(Season.series_id == series.id).all()
                                for ep in eps:
                                    try:
                                        if getattr(ep, 'has_file', False) or getattr(ep, 'is_deleted', False) or getattr(ep, 'dummypath', None):
                                            continue

                                        season_row = session.query(Season).get(ep.season_id) if ep.season_id else None
                                        season_num = getattr(season_row, 'season_number', None)
                                        final_folder = resolve_final_folder(
                                            media_type='tv',
                                            title=series.title,
                                            year=series.year,
                                            media_id=normalized_tvdb,
                                            season_number=season_num,
                                        )
                                        if not final_folder:
                                            continue

                                        clean_title = sanitize_filename(series.title)
                                        year_str = f" ({series.year})" if getattr(series, 'year', None) else ""
                                        file_name = f"{clean_title}{year_str} - s{int(season_num):02d}e{int(ep.episode_number):02d} - {ep.title}.mp4"
                                        file_path = os.path.join(final_folder, sanitize_filename(file_name))

                                        try:
                                            get_or_create_placeholder(
                                                session,
                                                path=file_path,
                                                series_id=series.id,
                                                season_id=season_row.id if season_row else None,
                                                episode_id=ep.id,
                                                created_by='full_sync',
                                                commit=True,
                                            )
                                        except Exception:
                                            logger.debug(f"Failed to create placeholder row for ep.id={ep.id}", extra={'emoji_type': 'debug'})
                                    except Exception:
                                        continue
                            except Exception:
                                logger.debug(f"Placeholder creation failed for series id={getattr(series, 'id', None)}", extra={'emoji_type': 'debug'})

                        # Enqueue an enrichment job rather than calling enrichment inline.
                        # The job worker will run integrations.enrich_* once and then
                        # create subflows/import_list as appropriate.
                        try:
                            payload = {'series': {'tvdbId': int(tvdb), 'id': sonarr_id}}
                            jid = enqueue_enrichment_job(payload=payload, is_4k=is_4k)
                            if jid:
                                report['tv']['enqueued'] += 1
                            else:
                                report['tv']['errors'].append(f"Failed to enqueue enrichment for TVDB {tvdb}")
                        except Exception as e:
                            logger.warning(f"Failed to enqueue enrichment for TVDB {tvdb}: {e}")
                            report['tv']['errors'].append(str(e))

                    except Exception as e:
                        logger.exception(f"Error processing Sonarr series entry: {e}")
                        report['tv']['errors'].append(str(e))

                # If dry_run, report planned enqueues for this batch
                if dry_run and tvdbs_to_enqueue:
                    report['tv']['enqueued'] += len(tvdbs_to_enqueue)

        if 'movie' in types:
            # If Radarr isn't configured in the environment, treat movies as "not enabled"
            # and skip movie syncs instead of raising. This keeps --type both safe when
            # Radarr isn't set up for the environment.
            try:
                movie_cfg = get_arr_config('movie', is_4k)
            except Exception:
                movie_cfg = None

            if not movie_cfg or not movie_cfg.get('url'):
                logger.info('Radarr configuration not available; skipping movie sync')
                report['movie']['errors'].append('Radarr not configured; skipping')
                movies = []
            else:
                try:
                    movies = _fetch_radarr_movies(is_4k=is_4k)
                except Exception as e:
                    logger.error(f"Failed to fetch Radarr movies: {e}")
                    report['movie']['errors'].append(str(e))
                    movies = []

            report['movie']['found'] = len(movies)
            for i in range(0, len(movies), batch_size):
                batch = movies[i:i + batch_size]
                tmdbs_to_enqueue = []
                for m in batch:
                    try:
                        tmdb = m.get('tmdbId') or m.get('tmdb')
                        radarr_id = m.get('id')
                        title = m.get('title')
                        year = m.get('year')
                        if not tmdb:
                            report['movie']['errors'].append(f"Movie missing tmdbId: {title}")
                            continue

                        if dry_run:
                            tmdbs_to_enqueue.append(tmdb)
                            continue

                        # Enqueue a Radarr enrichment job rather than calling enrichment inline.
                        try:
                            payload = {'movie': {'tmdbId': int(tmdb), 'id': radarr_id}}
                            jid = enqueue_enrichment_job(payload=payload, is_4k=is_4k)
                            if jid:
                                report['movie']['enqueued'] += 1
                            else:
                                report['movie']['errors'].append(f"Failed to enqueue enrichment for TMDB {tmdb}")
                        except Exception as e:
                            logger.warning(f"Radarr enqueue failed for TMDB {tmdb}: {e}")
                            report['movie']['errors'].append(str(e))

                        # enqueue import_list for movies is not currently used; we may extend later

                    except Exception as e:
                        logger.exception(f"Error processing Radarr movie entry: {e}")
                        report['movie']['errors'].append(str(e))

                if dry_run and tmdbs_to_enqueue:
                    report['movie']['enqueued'] += len(tmdbs_to_enqueue)

        report['finished'] = time.time()
        # Optionally write report
        if report_path:
            try:
                with open(report_path, 'w') as fh:
                    json.dump(report, fh, indent=2)
            except Exception as e:
                logger.warning(f"Failed to write full-sync report to {report_path}: {e}")

        return report

    finally:
        try:
            _release_lock(session)
        finally:
            session.close()
