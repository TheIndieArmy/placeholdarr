from services.postgres.db import get_session
from services.postgres.models import Placeholder, Movie, Series, Season, Episode
import os
import re
from sqlalchemy import func, text
from core.logger import logger
from core.config import settings
import hashlib
from typing import List, Optional
from sqlalchemy import or_, and_
import threading
import time as _time
from services.jobs import insert_job_with_session
from services.postgres.models import SubFlow as SubFlowModel

# Aggregator for noisy "no placeholders to process" events.
# We move per-occurrence messages to VERBOSE and emit an INFO summary
# every _AGG_MAX_COUNT occurrences or when _AGG_MAX_SECONDS elapsed.
_AGG_MAX_COUNT = 1000
_AGG_MAX_SECONDS = 10
_agg_lock = threading.Lock()
_agg_map: dict = {}

def _record_no_placeholders(key: str):
    now = _time.time()
    with _agg_lock:
        entry = _agg_map.get(key)
        if not entry:
            entry = {'count': 0, 'last_emit': now}
            _agg_map[key] = entry
        entry['count'] += 1
        # Emit when threshold or timeout reached
        if entry['count'] >= _AGG_MAX_COUNT or (now - entry['last_emit']) >= _AGG_MAX_SECONDS:
            try:
                logger.info(f"➡️ {key}: no placeholders to process (aggregated {entry['count']} occurrences)")
            except Exception:
                pass
            entry['count'] = 0
            entry['last_emit'] = now

def _emit_all_summaries():
    now = _time.time()
    with _agg_lock:
        for key, entry in list(_agg_map.items()):
            if entry.get('count', 0) > 0:
                try:
                    logger.info(f"➡️ {key}: no placeholders to process (aggregated {entry['count']} occurrences)")
                except Exception:
                    pass
                entry['count'] = 0
                entry['last_emit'] = now


def compute_signature(ph_row: Placeholder) -> str:
    """Compute a short signature for a placeholder based on available attributes.
    Uses extra.fingerprint and extra.size when present, falls back to path.
    """
    extra = (ph_row.extra or {}) if hasattr(ph_row, 'extra') else {}
    fingerprint = extra.get('fingerprint') or extra.get('fp') or ''
    size = str(extra.get('size') or extra.get('filesize') or '')
    inode = str((extra.get('inode') or ''))
    path = ph_row.path or ''
    base = f"{fingerprint}|{size}|{inode}|{path}"
    # Use short hex digest
    return hashlib.sha1(base.encode('utf-8')).hexdigest()[:16]


def _parse_season_episode(filename: str):
    """Attempt to parse season and episode numbers from a filename.
    Returns (season, episode) as ints on success, otherwise None.
    Supports patterns like S01E02, 1x02, s1.e2, etc.
    """
    if not filename:
        return None
    # Normalize filename for simpler regex matching
    name = filename
    # Common SxxEyy pattern
    m = re.search(r'(?i)S(?P<s>\d{1,2})[\. _-]?E(?P<e>\d{1,2})', name)
    if m:
        try:
            return int(m.group('s')), int(m.group('e'))
        except Exception:
            return None
    # Common 1x02 pattern
    m = re.search(r'(?i)(?P<s>\d{1,2})x(?P<e>\d{1,2})', name)
    if m:
        try:
            return int(m.group('s')), int(m.group('e'))
        except Exception:
            return None
    return None


def _parse_numeric_id_from_path(path: str) -> Optional[int]:
    """Heuristic: scan path components for a numeric id likely to be tmdb/tvdb.
    Matches components like '123456', 'tmdb-123456', 'tvdb_12345' and returns int.
    """
    if not path:
        return None
    comps = [c for c in path.split(os.sep) if c]
    # Prefer explicit tokens containing 'tmdb' or 'tvdb' (e.g. '{tmdb-12345}')
    for comp in reversed(comps):
        try:
            m = re.search(r'(?i)(?:tmdb|tvdb)[^0-9]*(\d{4,8})', comp)
            if m:
                try:
                    return int(m.group(1))
                except Exception:
                    pass
        except Exception:
            pass

    # Next prefer larger ids (likely external ids) over small 4-digit years.
    for comp in reversed(comps):
        try:
            m2 = re.search(r'(\d{5,8})', comp)
            if m2:
                try:
                    return int(m2.group(1))
                except Exception:
                    pass
        except Exception:
            pass

    # Fallback: accept a 4-digit numeric (year) only as last resort
    for comp in reversed(comps):
        try:
            m3 = re.search(r'(\d{4})', comp)
            if m3:
                try:
                    return int(m3.group(1))
                except Exception:
                    pass
        except Exception:
            pass
    return None


def _placeholders_to_process(session, placeholder_ids: Optional[List[int]] = None, limit: int = 500):
    q = session.query(Placeholder)
    if placeholder_ids:
        q = q.filter(Placeholder.id.in_(placeholder_ids))
    else:
        # default: placeholders observed since last enrichment or never enriched
        q = q.filter((Placeholder.last_observed_at != None))
        q = q.filter((Placeholder.last_enriched_at == None) | (Placeholder.last_observed_at > Placeholder.last_enriched_at))
    q = q.order_by(Placeholder.last_observed_at.desc())
    return q.limit(limit).all()


def process_enrich_and_merge(subflow_id: int, payload: dict) -> bool:
    """Main entrypoint for the worker. Processes placeholders and updates/links content rows.

    Returns True on success, False on transient failure.
    """
    placeholder_ids = payload.get('placeholder_ids')
    paths = payload.get('paths')
    try:
        session = get_session()
        try:
            if paths and isinstance(paths, list) and paths:
                # load placeholders by path
                phs = session.query(Placeholder).filter(Placeholder.path.in_(paths)).all()
            else:
                phs = _placeholders_to_process(session, placeholder_ids=placeholder_ids, limit=getattr(settings, 'ENRICH_BATCH_SIZE', 500))

            if not phs:
                # move noisy messages to VERBOSE and aggregate INFO summaries
                try:
                    logger.verbose(f"enrich_and_merge: no placeholders to process (subflow={subflow_id})")
                except Exception:
                    pass
                _record_no_placeholders('enrich_and_merge')
                return True

            processed = 0
            for ph in phs:
                try:
                    # record previous linkage so we can detect newly-linked placeholders
                    prev_movie = getattr(ph, 'movie_id', None)
                    prev_episode = getattr(ph, 'episode_id', None)
                    sig = compute_signature(ph)
                    # Quick skip
                    if ph.enriched_signature == sig and ph.last_enriched_at and ph.last_enriched_at >= ph.last_observed_at:
                        continue
                    # If placeholder already linked to a Movie/Episode, update content row with observed info
                    extra = ph.extra or {}
                    size = extra.get('size') or extra.get('filesize')
                    if ph.movie_id:
                        mv = session.query(Movie).filter(Movie.id == int(ph.movie_id)).with_for_update().first()
                        if mv:
                            if size:
                                try:
                                    mv.moviefile_size = int(size)
                                except Exception:
                                    mv.moviefile_size = mv.moviefile_size or None
                            mv.has_placeholder = True
                            # Authoritative: update placeholder_filepath when it differs.
                            try:
                                if getattr(mv, 'placeholder_filepath', None) != ph.path:
                                    mv.placeholder_filepath = ph.path
                            except Exception:
                                pass
                            # Also persist placeholder_folder derived from the observed path
                            try:
                                if ph.path:
                                    pdir = os.path.dirname(ph.path)
                                    if getattr(mv, 'placeholder_folder', None) != pdir:
                                        mv.placeholder_folder = pdir
                            except Exception:
                                pass
                            session.add(mv)
                    elif ph.episode_id:
                        ep = session.query(Episode).filter(Episode.id == int(ph.episode_id)).with_for_update().first()
                        if ep:
                            try:
                                ep.has_placeholder = True
                                # persist observed placeholder filepath (do NOT overwrite sonarr_filepath
                                # which should come from Sonarr/ARR payloads)
                                try:
                                    if getattr(ep, 'placeholder_filepath', None) != ph.path and ph.path:
                                        ep.placeholder_filepath = ph.path
                                except Exception:
                                    pass
                                # Authoritative: update episode.placeholder_filepath when it differs
                                try:
                                    if getattr(ep, 'placeholder_filepath', None) != ph.path:
                                        ep.placeholder_filepath = ph.path
                                except Exception:
                                    pass
                                # Also persist placeholder_folder on season/episode derived from observed path
                                try:
                                    if ph.path:
                                        pdir = os.path.dirname(ph.path)
                                        if getattr(ep, 'placeholder_folder', None) != pdir:
                                            ep.placeholder_folder = pdir
                                        # try to set season placeholder_folder as well
                                        try:
                                            if getattr(ep, 'season', None) and getattr(ep.season, 'placeholder_folder', None) != pdir:
                                                ep.season.placeholder_folder = pdir
                                                session.add(ep.season)
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                                session.add(ep)
                            except Exception:
                                pass
                    else:
                        # Not pre-linked: attempt the 3-step cascade
                        linked = False

                        # Step 1: try parsing numeric id from path and match TMDB (movie) or TVDB (series)
                        parsed_id = _parse_numeric_id_from_path(ph.path or '')
                        if parsed_id:
                            try:
                                mv = session.query(Movie).filter(Movie.tmdbid == int(parsed_id)).with_for_update().first()
                                if mv:
                                    ph.movie_id = mv.id
                                    mv.has_placeholder = True
                                    try:
                                        if getattr(mv, 'placeholder_filepath', None) != ph.path:
                                            mv.placeholder_filepath = ph.path
                                    except Exception:
                                        pass
                                    try:
                                        if ph.path:
                                            pdir = os.path.dirname(ph.path)
                                            if getattr(mv, 'placeholder_folder', None) != pdir:
                                                mv.placeholder_folder = pdir
                                    except Exception:
                                        pass
                                    session.add(mv)
                                    linked = True
                            except Exception:
                                pass
                            if not linked:
                                try:
                                    ss = session.query(Series).filter(Series.tvdbid == int(parsed_id)).with_for_update().first()
                                    if ss:
                                        # link placeholder to series and attempt SxxEyy
                                        ph.series_id = ss.id
                                        # persist placeholder_folder on series derived from observed path
                                        try:
                                            if ph.path:
                                                pdir = os.path.dirname(ph.path)
                                                if getattr(ss, 'placeholder_folder', None) != pdir:
                                                    ss.placeholder_folder = pdir
                                        except Exception:
                                            pass
                                        # parse season/episode from filename
                                        filename = os.path.basename(ph.path or '')
                                        sxe = _parse_season_episode(filename)
                                        if sxe:
                                            season_num, ep_num = sxe
                                            ep = session.query(Episode).join(Season).filter(Season.series_id == int(ss.id), Season.season_number == int(season_num), Episode.episode_number == int(ep_num)).with_for_update().first()
                                            if ep:
                                                ph.season_id = ep.season_id
                                                ph.episode_id = ep.id
                                                try:
                                                    ep.has_placeholder = True
                                                    try:
                                                        # observed placeholder path -> persist as placeholder_filepath
                                                        if getattr(ep, 'placeholder_filepath', None) != ph.path and ph.path:
                                                            ep.placeholder_filepath = ph.path
                                                    except Exception:
                                                        pass
                                                    try:
                                                        if getattr(ep, 'placeholder_filepath', None) != ph.path:
                                                            ep.placeholder_filepath = ph.path
                                                    except Exception:
                                                        pass
                                                    session.add(ep)
                                                except Exception:
                                                    pass
                                        session.add(ss)
                                        linked = True
                                except Exception:
                                    pass

                        # Step 2: placeholder_folder prefix matching (choose longest matching prefix)
                        if not linked:
                            try:
                                # Movie candidates ordered by length of placeholder_folder desc to prefer most-specific
                                candidate_movies = session.query(Movie).filter(Movie.placeholder_folder != None).all()
                                best_mv = None
                                best_len = 0
                                for mv in candidate_movies:
                                    try:
                                        if mv.placeholder_folder and ph.path.startswith(mv.placeholder_folder):
                                            l = len(mv.placeholder_folder)
                                            if l > best_len:
                                                best_len = l
                                                best_mv = mv
                                    except Exception:
                                        continue
                                if best_mv:
                                    ph.movie_id = best_mv.id
                                    best_mv.has_placeholder = True
                                    try:
                                        if getattr(best_mv, 'placeholder_filepath', None) != ph.path:
                                            best_mv.placeholder_filepath = ph.path
                                    except Exception:
                                        pass
                                    try:
                                        if ph.path:
                                            pdir = os.path.dirname(ph.path)
                                            if getattr(best_mv, 'placeholder_folder', None) != pdir:
                                                best_mv.placeholder_folder = pdir
                                    except Exception:
                                        pass
                                    session.add(best_mv)
                                    linked = True
                            except Exception:
                                pass

                        if not linked:
                            try:
                                candidate_series = session.query(Series).filter(Series.placeholder_folder != None).all()
                                best_ss = None
                                best_len = 0
                                for ss in candidate_series:
                                    try:
                                        if ss.placeholder_folder and ph.path.startswith(ss.placeholder_folder):
                                            l = len(ss.placeholder_folder)
                                            if l > best_len:
                                                best_len = l
                                                best_ss = ss
                                    except Exception:
                                        continue
                                if best_ss:
                                    # parse season/episode
                                    filename = os.path.basename(ph.path or '')
                                    sxe = _parse_season_episode(filename)
                                    if sxe:
                                        season_num, ep_num = sxe
                                        ep = session.query(Episode).join(Season).filter(Season.series_id == int(best_ss.id), Season.season_number == int(season_num), Episode.episode_number == int(ep_num)).with_for_update().first()
                                        if ep:
                                            ph.series_id = best_ss.id
                                            ph.season_id = ep.season_id
                                            ph.episode_id = ep.id
                                            try:
                                                ep.has_placeholder = True
                                                try:
                                                    # observed placeholder path -> persist as placeholder_filepath
                                                    if getattr(ep, 'placeholder_filepath', None) != ph.path and ph.path:
                                                        ep.placeholder_filepath = ph.path
                                                except Exception:
                                                    pass
                                                try:
                                                    # persist series/season placeholder_folder from observed path
                                                    if ph.path:
                                                        pdir = os.path.dirname(ph.path)
                                                        if getattr(best_ss, 'placeholder_folder', None) != pdir:
                                                            best_ss.placeholder_folder = pdir
                                                        try:
                                                            if getattr(ep, 'season', None) and getattr(ep.season, 'placeholder_folder', None) != pdir:
                                                                ep.season.placeholder_folder = pdir
                                                                session.add(ep.season)
                                                        except Exception:
                                                            pass
                                                except Exception:
                                                    pass
                                                session.add(ep)
                                            except Exception:
                                                pass
                                        linked = True
                                    if not linked:
                                        # link to series only (no season/episode parsed)
                                        ph.series_id = best_ss.id
                                        try:
                                            if ph.path:
                                                pdir = os.path.dirname(ph.path)
                                                if getattr(best_ss, 'placeholder_folder', None) != pdir:
                                                    best_ss.placeholder_folder = pdir
                                        except Exception:
                                            pass
                                        session.add(best_ss)
                                        linked = True
                            except Exception:
                                pass

                        # Step 3: normalized title matching (folder name -> title)
                        if not linked:
                            try:
                                # get candidate folder name (parent folder of file)
                                parent_folder = os.path.basename(os.path.dirname(ph.path or ''))
                                def _norm(s):
                                    if not s:
                                        return ''
                                    return re.sub(r'[^a-z0-9]', '', s.lower())
                                pnorm = _norm(parent_folder)
                                if pnorm:
                                    # try movies
                                    movies = session.query(Movie).all()
                                    for mv in movies:
                                        try:
                                            if _norm(mv.title) == pnorm:
                                                ph.movie_id = mv.id
                                                mv.has_placeholder = True
                                                try:
                                                    if getattr(mv, 'placeholder_filepath', None) != ph.path:
                                                        mv.placeholder_filepath = ph.path
                                                except Exception:
                                                    pass
                                                try:
                                                    if ph.path:
                                                        pdir = os.path.dirname(ph.path)
                                                        if getattr(mv, 'placeholder_folder', None) != pdir:
                                                            mv.placeholder_folder = pdir
                                                except Exception:
                                                    pass
                                                session.add(mv)
                                                linked = True
                                                break
                                        except Exception:
                                            continue
                                    # try series if still not linked
                                    if not linked:
                                        series_all = session.query(Series).all()
                                        for ss in series_all:
                                            try:
                                                if _norm(ss.title) == pnorm:
                                                        ph.series_id = ss.id
                                                        try:
                                                            if ph.path:
                                                                pdir = os.path.dirname(ph.path)
                                                                if getattr(ss, 'placeholder_folder', None) != pdir:
                                                                    ss.placeholder_folder = pdir
                                                        except Exception:
                                                            pass
                                                        session.add(ss)
                                                        linked = True
                                                        break
                                            except Exception:
                                                continue
                            except Exception:
                                pass

                    # Persist placeholder enrichment metadata
                    ph.enriched_signature = sig
                    session.add(ph)

                    # If this placeholder was newly linked to a movie/episode, enqueue
                    # a deduped determine job for the corresponding SubFlow so the
                    # authoritative decision is re-evaluated.
                    try:
                        TERMINAL_STATUSES = ('DONE', 'CANCELLED', 'FAILED')
                        if prev_movie is None and getattr(ph, 'movie_id', None):
                            try:
                                sfrow = session.query(SubFlowModel).filter(SubFlowModel.movie_id == int(ph.movie_id)).first()
                                if sfrow:
                                    steps = (sfrow.steps or '').split(',')
                                    try:
                                        det_idx = steps.index('determine')
                                    except Exception:
                                        det_idx = 0
                                    payload_det = {'run_id': None, 'phase': 'determine', 'subflow_id': sfrow.id, 'step_index': det_idx}
                                    insert_job_with_session(session, 'subjob:determine', payload_det, group_id=f"subflow:{sfrow.id}:determine")
                            except Exception:
                                pass

                        if prev_episode is None and getattr(ph, 'episode_id', None):
                            try:
                                sfrow = session.query(SubFlowModel).filter(SubFlowModel.episode_id == int(ph.episode_id)).first()
                                if sfrow:
                                    steps = (sfrow.steps or '').split(',')
                                    try:
                                        det_idx = steps.index('determine')
                                    except Exception:
                                        det_idx = 0
                                    payload_det = {'run_id': None, 'phase': 'determine', 'subflow_id': sfrow.id, 'step_index': det_idx}
                                    insert_job_with_session(session, 'subjob:determine', payload_det, group_id=f"subflow:{sfrow.id}:determine")
                            except Exception:
                                pass
                    except Exception:
                        pass

                    # write DB now() as last_enriched_at using a DB-side now() to ensure DB-authoritative timestamp
                    try:
                        session.flush()
                        session.execute(text("UPDATE placeholder SET last_enriched_at = now(), enriched_signature = :sig WHERE id = :id"), {'sig': sig, 'id': ph.id})
                        session.commit()
                    except Exception:
                        try:
                            session.rollback()
                        except Exception:
                            pass
                    processed += 1
                except Exception as ex:
                    logger.exception(f"Failed to process placeholder {getattr(ph,'id',None)}: {ex}")
                    try:
                        session.rollback()
                    except Exception:
                        pass

            # emit any pending aggregated summaries for this module
            try:
                _emit_all_summaries()
            except Exception:
                pass
            logger.info(f"enrich_and_merge: processed {processed} placeholders for subflow={subflow_id}")
            return True
        finally:
            try:
                session.close()
            except Exception:
                pass
    except Exception as exc:
        logger.exception(f"enrich_and_merge error: {exc}")
        return False


def process_placeholders(placeholder_ids: Optional[List[int]] = None, paths: Optional[List[str]] = None, limit: int = None, subflow_id: Optional[int] = None) -> dict:
    """Central API: process placeholders by ids or paths.

    Returns a dict with {'ok': bool, 'processed': int}.
    This is safe to call from callers that only know the placeholder paths (targeted scans).
    """
    if limit is None:
        limit = getattr(settings, 'ENRICH_BATCH_SIZE', 500)
    try:
        session = get_session()
        try:
            if paths and isinstance(paths, list) and paths:
                phs = session.query(Placeholder).filter(Placeholder.path.in_(paths)).all()
            else:
                phs = _placeholders_to_process(session, placeholder_ids=placeholder_ids, limit=limit)

            if not phs:
                # move noisy per-call messages to VERBOSE and aggregate INFO summaries
                try:
                    logger.verbose(f"process_placeholders: no placeholders to process (subflow={subflow_id})")
                except Exception:
                    pass
                _record_no_placeholders('process_placeholders')
                return {'ok': True, 'processed': 0}

            processed = 0
            for ph in phs:
                try:
                    # record previous linkage so we can detect newly-linked placeholders
                    prev_movie = getattr(ph, 'movie_id', None)
                    prev_episode = getattr(ph, 'episode_id', None)

                    sig = compute_signature(ph)
                    if ph.enriched_signature == sig and ph.last_enriched_at and ph.last_enriched_at >= ph.last_observed_at:
                        continue
                    extra = ph.extra or {}
                    size = extra.get('size') or extra.get('filesize')
                    if ph.movie_id:
                        mv = session.query(Movie).filter(Movie.id == int(ph.movie_id)).with_for_update().first()
                        if mv:
                            if size:
                                try:
                                    mv.moviefile_size = int(size)
                                except Exception:
                                    mv.moviefile_size = mv.moviefile_size or None
                            mv.has_placeholder = True
                            try:
                                if getattr(mv, 'placeholder_filepath', None) != ph.path:
                                    mv.placeholder_filepath = ph.path
                            except Exception:
                                pass
                            try:
                                if ph.path:
                                    pdir = os.path.dirname(ph.path)
                                    if getattr(mv, 'placeholder_folder', None) != pdir:
                                        mv.placeholder_folder = pdir
                            except Exception:
                                pass
                            session.add(mv)
                    elif ph.episode_id:
                        ep = session.query(Episode).filter(Episode.id == int(ph.episode_id)).with_for_update().first()
                        if ep:
                            try:
                                ep.has_placeholder = True
                                try:
                                    # observed placeholder path -> persist as placeholder_filepath
                                    if getattr(ep, 'placeholder_filepath', None) != ph.path and ph.path:
                                        ep.placeholder_filepath = ph.path
                                except Exception:
                                    pass
                                try:
                                    if ph.path:
                                        pdir = os.path.dirname(ph.path)
                                        if getattr(ep, 'placeholder_folder', None) != pdir:
                                            ep.placeholder_folder = pdir
                                        try:
                                            if getattr(ep, 'season', None) and getattr(ep.season, 'placeholder_folder', None) != pdir:
                                                ep.season.placeholder_folder = pdir
                                                session.add(ep.season)
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                                session.add(ep)
                            except Exception:
                                pass
                    else:
                        # reuse the existing cascade logic: numeric id -> folder prefix -> normalized title
                        linked = False
                        parsed_id = _parse_numeric_id_from_path(ph.path or '')
                        if parsed_id:
                            try:
                                mv = session.query(Movie).filter(Movie.tmdbid == int(parsed_id)).with_for_update().first()
                                if mv:
                                    ph.movie_id = mv.id
                                    mv.has_placeholder = True
                                    try:
                                        if getattr(mv, 'placeholder_filepath', None) != ph.path:
                                            mv.placeholder_filepath = ph.path
                                    except Exception:
                                        pass
                                    try:
                                        if ph.path:
                                            pdir = os.path.dirname(ph.path)
                                            if getattr(mv, 'placeholder_folder', None) != pdir:
                                                mv.placeholder_folder = pdir
                                    except Exception:
                                        pass
                                    session.add(mv)
                                    linked = True
                            except Exception:
                                pass
                            if not linked:
                                try:
                                    ss = session.query(Series).filter(Series.tvdbid == int(parsed_id)).with_for_update().first()
                                    if ss:
                                        ph.series_id = ss.id
                                        try:
                                            if ph.path:
                                                pdir = os.path.dirname(ph.path)
                                                if getattr(ss, 'placeholder_folder', None) != pdir:
                                                    ss.placeholder_folder = pdir
                                        except Exception:
                                            pass
                                        filename = os.path.basename(ph.path or '')
                                        sxe = _parse_season_episode(filename)
                                        if sxe:
                                            season_num, ep_num = sxe
                                            ep = session.query(Episode).join(Season).filter(Season.series_id == int(ss.id), Season.season_number == int(season_num), Episode.episode_number == int(ep_num)).with_for_update().first()
                                            if ep:
                                                ph.season_id = ep.season_id
                                                ph.episode_id = ep.id
                                                try:
                                                    ep.has_placeholder = True
                                                    try:
                                                        # observed placeholder path -> persist as placeholder_filepath
                                                        if getattr(ep, 'placeholder_filepath', None) != ph.path and ph.path:
                                                            ep.placeholder_filepath = ph.path
                                                    except Exception:
                                                        pass
                                                    try:
                                                        if ph.path:
                                                            pdir = os.path.dirname(ph.path)
                                                            if getattr(ep, 'placeholder_folder', None) != pdir:
                                                                ep.placeholder_folder = pdir
                                                            try:
                                                                if getattr(ep, 'season', None) and getattr(ep.season, 'placeholder_folder', None) != pdir:
                                                                    ep.season.placeholder_folder = pdir
                                                                    session.add(ep.season)
                                                            except Exception:
                                                                pass
                                                    except Exception:
                                                        pass
                                                    session.add(ep)
                                                except Exception:
                                                    pass
                                        session.add(ss)
                                        linked = True
                                except Exception:
                                    pass

                        if not linked:
                            try:
                                candidate_movies = session.query(Movie).filter(Movie.placeholder_folder != None).all()
                                best_mv = None
                                best_len = 0
                                for mv in candidate_movies:
                                    try:
                                        if mv.placeholder_folder and ph.path.startswith(mv.placeholder_folder):
                                            l = len(mv.placeholder_folder)
                                            if l > best_len:
                                                best_len = l
                                                best_mv = mv
                                    except Exception:
                                        continue
                                if best_mv:
                                    ph.movie_id = best_mv.id
                                    best_mv.has_placeholder = True
                                    try:
                                        if getattr(best_mv, 'placeholder_filepath', None) != ph.path:
                                            best_mv.placeholder_filepath = ph.path
                                    except Exception:
                                        pass
                                    try:
                                        if ph.path:
                                            pdir = os.path.dirname(ph.path)
                                            if getattr(best_mv, 'placeholder_folder', None) != pdir:
                                                best_mv.placeholder_folder = pdir
                                    except Exception:
                                        pass
                                    session.add(best_mv)
                                    linked = True
                            except Exception:
                                pass

                        if not linked:
                            try:
                                candidate_series = session.query(Series).filter(Series.placeholder_folder != None).all()
                                best_ss = None
                                best_len = 0
                                for ss in candidate_series:
                                    try:
                                        if ss.placeholder_folder and ph.path.startswith(ss.placeholder_folder):
                                            l = len(ss.placeholder_folder)
                                            if l > best_len:
                                                best_len = l
                                                best_ss = ss
                                    except Exception:
                                        continue
                                if best_ss:
                                    filename = os.path.basename(ph.path or '')
                                    sxe = _parse_season_episode(filename)
                                    if sxe:
                                        season_num, ep_num = sxe
                                        ep = session.query(Episode).join(Season).filter(Season.series_id == int(best_ss.id), Season.season_number == int(season_num), Episode.episode_number == int(ep_num)).with_for_update().first()
                                        if ep:
                                            ph.series_id = best_ss.id
                                            ph.season_id = ep.season_id
                                            ph.episode_id = ep.id
                                            try:
                                                ep.has_placeholder = True
                                                try:
                                                    if getattr(ep, 'placeholder_filepath', None) != ph.path and ph.path:
                                                        ep.placeholder_filepath = ph.path
                                                except Exception:
                                                    pass
                                                try:
                                                    if ph.path:
                                                        pdir = os.path.dirname(ph.path)
                                                        if getattr(best_ss, 'placeholder_folder', None) != pdir:
                                                            best_ss.placeholder_folder = pdir
                                                        try:
                                                            if getattr(ep, 'season', None) and getattr(ep.season, 'placeholder_folder', None) != pdir:
                                                                ep.season.placeholder_folder = pdir
                                                                session.add(ep.season)
                                                        except Exception:
                                                            pass
                                                except Exception:
                                                    pass
                                                session.add(ep)
                                            except Exception:
                                                pass
                                        linked = True
                                    if not linked:
                                        ph.series_id = best_ss.id
                                        try:
                                            if ph.path:
                                                pdir = os.path.dirname(ph.path)
                                                if getattr(best_ss, 'placeholder_folder', None) != pdir:
                                                    best_ss.placeholder_folder = pdir
                                        except Exception:
                                            pass
                                        session.add(best_ss)
                                        linked = True
                            except Exception:
                                pass

                        if not linked:
                            try:
                                parent_folder = os.path.basename(os.path.dirname(ph.path or ''))
                                def _norm(s):
                                    if not s:
                                        return ''
                                    return re.sub(r'[^a-z0-9]', '', s.lower())
                                pnorm = _norm(parent_folder)
                                if pnorm:
                                    movies = session.query(Movie).all()
                                    for mv in movies:
                                        try:
                                            if _norm(mv.title) == pnorm:
                                                ph.movie_id = mv.id
                                                mv.has_placeholder = True
                                                try:
                                                    if getattr(mv, 'placeholder_filepath', None) != ph.path:
                                                        mv.placeholder_filepath = ph.path
                                                except Exception:
                                                    pass
                                                try:
                                                    if ph.path:
                                                        pdir = os.path.dirname(ph.path)
                                                        if getattr(mv, 'placeholder_folder', None) != pdir:
                                                            mv.placeholder_folder = pdir
                                                except Exception:
                                                    pass
                                                session.add(mv)
                                                linked = True
                                                break
                                        except Exception:
                                            continue
                                    if not linked:
                                        series_all = session.query(Series).all()
                                        for ss in series_all:
                                            try:
                                                if _norm(ss.title) == pnorm:
                                                        ph.series_id = ss.id
                                                        try:
                                                            if ph.path:
                                                                pdir = os.path.dirname(ph.path)
                                                                if getattr(ss, 'placeholder_folder', None) != pdir:
                                                                    ss.placeholder_folder = pdir
                                                        except Exception:
                                                            pass
                                                        session.add(ss)
                                                        linked = True
                                                        break
                                            except Exception:
                                                continue
                            except Exception:
                                pass

                    ph.enriched_signature = sig
                    session.add(ph)

                    # If this placeholder was newly linked to a movie/episode, enqueue a deduped determine job
                    try:
                        if prev_movie is None and getattr(ph, 'movie_id', None):
                            try:
                                sfrow = session.query(SubFlowModel).filter(SubFlowModel.movie_id == int(ph.movie_id)).first()
                                if sfrow:
                                    steps = (sfrow.steps or '').split(',')
                                    try:
                                        det_idx = steps.index('determine')
                                    except Exception:
                                        det_idx = 0
                                    payload_det = {'run_id': None, 'phase': 'determine', 'subflow_id': sfrow.id, 'step_index': det_idx}
                                    insert_job_with_session(session, 'subjob:determine', payload_det, group_id=f"subflow:{sfrow.id}:determine")
                            except Exception:
                                pass
                        if prev_episode is None and getattr(ph, 'episode_id', None):
                            try:
                                sfrow = session.query(SubFlowModel).filter(SubFlowModel.episode_id == int(ph.episode_id)).first()
                                if sfrow:
                                    steps = (sfrow.steps or '').split(',')
                                    try:
                                        det_idx = steps.index('determine')
                                    except Exception:
                                        det_idx = 0
                                    payload_det = {'run_id': None, 'phase': 'determine', 'subflow_id': sfrow.id, 'step_index': det_idx}
                                    insert_job_with_session(session, 'subjob:determine', payload_det, group_id=f"subflow:{sfrow.id}:determine")
                            except Exception:
                                pass
                    except Exception:
                        pass

                    try:
                        session.flush()
                        session.execute(text("UPDATE placeholder SET last_enriched_at = now(), enriched_signature = :sig WHERE id = :id"), {'sig': sig, 'id': ph.id})
                        session.commit()
                    except Exception:
                        try:
                            session.rollback()
                        except Exception:
                            pass
                    processed += 1
                except Exception as ex:
                    logger.exception(f"Failed to process placeholder {getattr(ph,'id',None)}: {ex}")
                    try:
                        session.rollback()
                    except Exception:
                        pass

            try:
                _emit_all_summaries()
            except Exception:
                pass
            logger.info(f"process_placeholders: processed {processed} placeholders for subflow={subflow_id}")
            return {'ok': True, 'processed': processed}
        finally:
            try:
                session.close()
            except Exception:
                pass
    except Exception as exc:
        logger.exception(f"process_placeholders error: {exc}")
        return {'ok': False, 'processed': 0}
