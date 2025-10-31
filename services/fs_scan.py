import os
import hashlib
from typing import List, Optional

from sqlalchemy import func, text

from services.postgres.db import get_engine, get_session
from services.postgres.models import Placeholder, Movie, Episode, Series, Season
from core.config import settings
from core.logger import logger


# Lightweight fingerprint: read first N bytes (default 4096) and hex-digest
FINGERPRINT_BYTES = 4096


def _file_fingerprint(path: str, nbytes: int = FINGERPRINT_BYTES) -> Optional[str]:
    try:
        with open(path, 'rb') as fh:
            head = fh.read(nbytes)
            h = hashlib.sha1(head).hexdigest()
            return h
    except Exception:
        return None


def _is_hardlink_to_dummy(path: str, dummy_path: str) -> bool:
    try:
        s1 = os.stat(path)
        s2 = os.stat(dummy_path)
        return (s1.st_ino == s2.st_ino) and (s1.st_dev == s2.st_dev)
    except Exception:
        return False


def _is_copy_of_dummy(path: str, dummy_path: str, nbytes: int = FINGERPRINT_BYTES) -> bool:
    try:
        s1 = os.stat(path)
        s2 = os.stat(dummy_path)
        if s1.st_size != s2.st_size:
            return False
        f1 = _file_fingerprint(path, nbytes)
        f2 = _file_fingerprint(dummy_path, nbytes)
        return f1 is not None and f1 == f2
    except Exception:
        return False


def _detect_placeholder(path: str, dummy_path: Optional[str], strategy: str) -> Optional[dict]:
    """Return detection info if path looks like a placeholder, otherwise None."""
    try:
        # Fast rule: extension or known naming pattern
        name = os.path.basename(path)
        if name.endswith('.dummy'):
            return {'reason': 'extension_dummy'}

        # If dummy path configured, check hardlink or copy
        if dummy_path and os.path.exists(dummy_path):
            if strategy == 'hardlink' and _is_hardlink_to_dummy(path, dummy_path):
                return {'reason': 'hardlink_to_dummy'}
            if strategy == 'copy' and _is_copy_of_dummy(path, dummy_path):
                return {'reason': 'copy_of_dummy'}

        # If dummy is very small (zero bytes), detect zero-size candidates
        try:
            if os.path.getsize(path) == 0 and dummy_path and os.path.exists(dummy_path) and os.path.getsize(dummy_path) == 0:
                return {'reason': 'zero_size_match'}
        except Exception:
            pass

        # Fallback filename heuristics: media_id token
        # e.g. <title>_<id>.dummy or folder named with id
        if '_' in name:
            parts = name.rsplit('_', 1)
            if parts and parts[-1].split('.')[0].isdigit():
                return {'reason': 'filename_mediaid'}

        return None
    except Exception:
        return None


def _map_path_to_item(session, path: str) -> dict:
    """Attempt to map a placeholder path to movie/series/season/episode by folder name or configured placeholder_folder.
    Returns dict with possible keys: movie_id, series_id, season_id, episode_id
    """
    info = {'movie_id': None, 'series_id': None, 'season_id': None, 'episode_id': None}
    try:
        parent = os.path.dirname(path)
        # Check per-item placeholder_folder exact match
        try:
            mv = session.query(Movie).filter(Movie.placeholder_folder == parent).first()
            if mv:
                info['movie_id'] = mv.id
                return info
        except Exception:
            pass

        try:
            ep = session.query(Episode).filter(Episode.placeholder_folder == parent).first()
            if ep:
                info['episode_id'] = ep.id
                return info
        except Exception:
            pass

        # Folder name numeric -> try as movie/series id
        folder = os.path.basename(parent)
        if folder.isdigit():
            try:
                mid = int(folder)
                mv = session.query(Movie).filter(Movie.id == mid).first()
                if mv:
                    info['movie_id'] = mv.id
                    return info
                # try series
                sr = session.query(Series).filter(Series.id == mid).first()
                if sr:
                    info['series_id'] = sr.id
                    return info
            except Exception:
                pass
    except Exception:
        pass
    return info


def scan_placeholder_roots(roots: List[str], return_paths: bool = False):
    """Scan given roots for placeholder files and upsert Placeholder rows.
    Returns number of placeholders upserted by default. If return_paths=True,
    returns a list of the placeholder paths that were created or updated.
    """
    engine = get_engine()
    dummy = getattr(settings, 'DUMMY_FILE_PATH', None) or None
    strategy = getattr(settings, 'PLACEHOLDER_STRATEGY', 'hardlink')
    upserted = 0
    upserted_paths = []
    # Acquire an advisory lock so only one process scans at a time
    try:
        with engine.connect() as conn:
            got = conn.execute(text('SELECT pg_try_advisory_lock(:k)'), {'k': 987654321}).scalar()
            if not got:
                logger.info('FS-scan already running elsewhere; skipping this scan', extra={'emoji_type': 'info'})
                return 0
            session = get_session()
            try:
                for root in roots:
                    if not root:
                        continue
                    if not os.path.exists(root):
                        logger.warning(f'Placeholder root does not exist: {root}', extra={'emoji_type': 'warning'})
                        continue
                    logger.info(f'Scanning placeholder root: {root}', extra={'emoji_type': 'placeholder'})
                    for dirpath, dirnames, filenames in os.walk(root):
                        for fn in filenames:
                            path = os.path.join(dirpath, fn)
                            det = _detect_placeholder(path, dummy, strategy)
                            if not det:
                                continue
                            # Map to item if possible
                            mapping = _map_path_to_item(session, path)
                            try:
                                ph = session.query(Placeholder).filter(Placeholder.path == path).first()
                                stat = os.stat(path)
                                extra = {
                                    'inode': stat.st_ino,
                                    'dev': stat.st_dev,
                                    'size': stat.st_size,
                                    'fingerprint': _file_fingerprint(path) if stat.st_size > 0 else None,
                                    'detection_reason': det.get('reason')
                                }
                                if not ph:
                                    ph = Placeholder(
                                        path=path,
                                        movie_id=mapping.get('movie_id'),
                                        series_id=mapping.get('series_id'),
                                        season_id=mapping.get('season_id'),
                                        episode_id=mapping.get('episode_id'),
                                        has_placeholder=True,
                                        last_observed_at=func.now(),
                                        extra=extra,
                                        created_by='fs_scan'
                                    )
                                    session.add(ph)
                                    upserted += 1
                                    upserted_paths.append(path)
                                else:
                                    ph.has_placeholder = True
                                    ph.last_observed_at = func.now()
                                    ph.extra = extra
                                    # update mapping if missing
                                    if not ph.movie_id and mapping.get('movie_id'):
                                        ph.movie_id = mapping.get('movie_id')
                                    if not ph.episode_id and mapping.get('episode_id'):
                                        ph.episode_id = mapping.get('episode_id')
                                    session.add(ph)
                                session.commit()
                            except Exception as e:
                                try:
                                    session.rollback()
                                except Exception:
                                    pass
                                logger.debug(f'Failed to upsert placeholder {path}: {e}', extra={'emoji_type': 'debug'})
            finally:
                try:
                    session.close()
                except Exception:
                    pass
            # Release advisory lock
            try:
                conn.execute(text('SELECT pg_advisory_unlock(:k)'), {'k': 987654321})
            except Exception:
                pass
    except Exception as e:
        logger.error(f'FS-scan failed: {e}', extra={'emoji_type': 'error'})
    logger.info(f'FS-scan completed, placeholders upserted: {upserted}', extra={'emoji_type': 'placeholder'})
    if return_paths:
        return upserted_paths
    return upserted


def scan_once_if_needed(run_id: Optional[str] = None) -> int:
    """Convenience: scan configured roots once. Returns upsert count."""
    roots = []
    try:
        if getattr(settings, 'MOVIE_LIBRARY_FOLDER', None):
            roots.append(settings.MOVIE_LIBRARY_FOLDER)
        if getattr(settings, 'TV_LIBRARY_FOLDER', None):
            roots.append(settings.TV_LIBRARY_FOLDER)
    except Exception:
        pass
    # Deduplicate
    roots = list(dict.fromkeys([r for r in roots if r]))
    if not roots:
        logger.warning('No library roots configured for FS-scan; skipping', extra={'emoji_type': 'warning'})
        return 0
    # Heuristic guard: if we've recently observed placeholders (within short interval), skip a full walk.
    # This prevents repeated full-disk scans when many per-item SubFlows enqueue fs_scan during a fullsync.
    try:
        session = get_session()
        try:
            # configurable threshold (seconds)
            threshold = getattr(settings, 'FS_SCAN_MIN_SECONDS_BETWEEN', 300)
            row = session.execute(text('SELECT max(last_observed_at) FROM placeholder')).scalar()
            if row is not None:
                from datetime import datetime as _dt, timezone as _tz
                try:
                    now = session.execute(text('SELECT now()')).scalar()
                except Exception:
                    now = _dt.utcnow().replace(tzinfo=_tz.utc)
                try:
                    delta = (now - row).total_seconds()
                    if delta is not None and delta < int(threshold):
                        # Log the skip (still return an informative payload so callers can explain why a scan wasn't run)
                        logger.info(f'FS-scan skipped: last placeholder observation {int(delta)}s ago (<{int(threshold)}s)', extra={'emoji_type': 'placeholder'})
                        return 0, {'reason': 'time_guard', 'delta': int(delta), 'threshold': int(threshold)}
                except Exception:
                    pass
        finally:
            try:
                session.close()
            except Exception:
                pass
    except Exception:
        # best-effort only; proceed to scanning if anything fails
        pass

    # If a run_id was provided, attempt to claim a run-scoped marker so only one
    # FS-scan runs for that external run (e.g. fullsync:<id>). This is more
    # robust than the time-based guard for avoiding repeated full-disk walks.
    # This helper can optionally return observed paths if called with return_paths=True
    def _inner(run_id_local: Optional[str] = None, return_paths: bool = False):
        if run_id_local:
            try:
                engine = get_engine()
                try:
                    with engine.begin() as conn:
                        claim_sql = text(
                            "INSERT INTO fs_scan_run (run_id, claimed_by, claimed_at) VALUES (:run_id, :claimed_by, now()) "
                            "ON CONFLICT (run_id) DO NOTHING RETURNING id"
                        )
                        res = conn.execute(claim_sql, {'run_id': run_id_local, 'claimed_by': 'fs_scan'})
                        row = res.fetchone()
                        if not row:
                            logger.info(f'FS-scan run {run_id_local} already claimed; skipping', extra={'emoji_type': 'placeholder'})
                            return [] if return_paths else 0
                except Exception as e:
                    # If claiming fails due to transient DB problems, log and proceed to scanning
                    logger.exception(f'Failed to claim fs_scan_run marker for {run_id_local}; proceeding to scan (best-effort): {e}')
            except Exception:
                # best-effort only; proceed if engine not available
                pass
        return scan_placeholder_roots(roots, return_paths=return_paths)

    # call inner with default behavior (return count)
    return _inner(run_id, return_paths=False)
