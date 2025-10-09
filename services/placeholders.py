"""Placeholder manager helpers.

This module provides small, focused helpers to create and manage rows in the
`placeholder` table without embedding filesystem or network operations. The
functions intentionally accept a SQLAlchemy `Session` and avoid committing by
default so callers can control transaction boundaries.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from services.postgres import models
from services.utils import resolve_final_folder, sanitize_filename, format_episode_label, format_movie_label
from core.config import settings
import os, re
import hashlib

logger = logging.getLogger(__name__)


class PlaceholderManagerError(Exception):
    pass


def _validate_target(movie_id: Optional[int], series_id: Optional[int], season_id: Optional[int], episode_id: Optional[int]):
    """Ensure exactly one target type is supplied (movie OR series/season/episode).

    Raises ValueError on invalid combinations.
    """
    # At minimum one of the ids must be present
    if not any((movie_id, series_id, season_id, episode_id)):
        raise ValueError("must provide at least one of movie_id, series_id, season_id, episode_id")

    # movie is exclusive
    if movie_id and any((series_id, season_id, episode_id)):
        raise ValueError("movie_id cannot be combined with series/season/episode ids")


def find_by_content(session: Session, movie_id: Optional[int] = None, series_id: Optional[int] = None, season_id: Optional[int] = None, episode_id: Optional[int] = None) -> Optional[models.Placeholder]:
    """Return a Placeholder row matching the provided content ids, or None."""
    _validate_target(movie_id, series_id, season_id, episode_id)

    q = session.query(models.Placeholder)
    if movie_id:
        return q.filter(models.Placeholder.movie_id == movie_id).one_or_none()
    # episode specificity
    if episode_id:
        return q.filter(
            models.Placeholder.episode_id == episode_id,
            models.Placeholder.season_id == season_id,
            models.Placeholder.series_id == series_id,
        ).one_or_none()
    # season or series
    if season_id:
        return q.filter(models.Placeholder.season_id == season_id, models.Placeholder.series_id == series_id).one_or_none()
    return q.filter(models.Placeholder.series_id == series_id).one_or_none()


def find_by_path(session: Session, path: str) -> Optional[models.Placeholder]:
    """Return Placeholder by filesystem path, or None."""
    return session.query(models.Placeholder).filter(models.Placeholder.path == path).one_or_none()


def get_or_create_placeholder(session: Session,
                              path: str,
                              movie_id: Optional[int] = None,
                              series_id: Optional[int] = None,
                              season_id: Optional[int] = None,
                              episode_id: Optional[int] = None,
                              created_by: Optional[str] = None,
                                                        metadata: Optional[Dict[str, Any]] = None,
                              commit: bool = False) -> models.Placeholder:
    """Get existing placeholder row for the provided content or create one.

    This function is idempotent and will retry/look up on unique constraint
    violations that can occur under concurrency.
    """
    _validate_target(movie_id, series_id, season_id, episode_id)

    existing = find_by_content(session, movie_id, series_id, season_id, episode_id)
    if existing:
        # Ensure path is kept up-to-date if supplied differently
        if path and existing.path != path:
            existing.path = path
            existing.updated_at = datetime.now()
            if commit:
                session.commit()
        # Merge provided metadata into existing.extra when supplied
        if metadata:
            try:
                extra = existing.extra or {}
                if not isinstance(extra, dict):
                    extra = {}
                # Merge metadata shallowly (caller metadata takes precedence)
                md = dict(metadata)
                extra.update(md)
                existing.extra = extra
                existing.updated_at = datetime.now()
                if commit:
                    session.commit()
            except Exception:
                # non-fatal; keep going
                pass
        return existing

    placeholder = models.Placeholder(
        movie_id=movie_id,
        series_id=series_id,
        season_id=season_id,
        episode_id=episode_id,
        path=path,
        exists=False,
        lifecycle_status='PENDING',
        display_status=None,
        display_progress=None,
        display_reason=None,
        format_hint=None,
    extra=metadata or {},
        created_by=created_by,
    created_at=datetime.now(),
    updated_at=datetime.now(),
    )

    session.add(placeholder)
    try:
        # flush instead of commit so callers can decide on transaction boundary
        session.flush()
        if commit:
            session.commit()
        return placeholder
    except IntegrityError:
        # Another worker likely created it concurrently; rollback the flush and fetch the row
        logger.debug("IntegrityError creating placeholder; fetching existing row")
        session.rollback()
        existing = find_by_content(session, movie_id, series_id, season_id, episode_id)
        if not existing:
            raise PlaceholderManagerError("Failed to create placeholder and existing row not found")
        # If creator supplied metadata, merge into the recovered existing row
        if metadata:
            try:
                extra = existing.extra or {}
                if not isinstance(extra, dict):
                    extra = {}
                md = dict(metadata)
                extra.update(md)
                existing.extra = extra
                existing.updated_at = datetime.now()
                if commit:
                    session.commit()
            except Exception:
                pass
        return existing


def compute_fingerprint(path: str, prefix_bytes: int = 65536) -> dict:
    """Compute a compact fingerprint for a file: size + SHA256 over the first
    `prefix_bytes` bytes. Returns a dict suitable for storing in placeholder.extra.

    The returned dict has keys: algorithm, prefix_bytes, size, hash_hex.
    """
    try:
        if not path or not os.path.isfile(path):
            return {}
        h = hashlib.sha256()
        total_read = 0
        with open(path, 'rb') as fh:
            while total_read < prefix_bytes:
                chunk = fh.read(min(8192, prefix_bytes - total_read))
                if not chunk:
                    break
                h.update(chunk)
                total_read += len(chunk)
        size = os.path.getsize(path)
        return {
            'algorithm': 'sha256-prefix',
            'prefix_bytes': total_read,
            'size': size,
            'hash_hex': h.hexdigest(),
        }
    except Exception:
        return {}


def set_lifecycle_status(session: Session, placeholder: models.Placeholder, status: str, commit: bool = False) -> models.Placeholder:
    placeholder.lifecycle_status = status
    placeholder.updated_at = datetime.now()
    if commit:
        session.commit()
    return placeholder


def mark_exists(session: Session, placeholder: models.Placeholder, exists: bool = True, commit: bool = False) -> models.Placeholder:
    placeholder.exists = exists
    placeholder.updated_at = datetime.now()
    if exists and placeholder.lifecycle_status in ('PENDING', 'CREATING'):
        placeholder.lifecycle_status = 'ACTIVE'
    if commit:
        session.commit()
    return placeholder


def update_presentation(session: Session,
                        placeholder: models.Placeholder,
                        display_status: Optional[str] = None,
                        display_progress: Optional[int] = None,
                        display_reason: Optional[str] = None,
                        format_hint: Optional[str] = None,
                        metadata: Optional[Dict[str, Any]] = None,
                        commit: bool = False) -> models.Placeholder:
    if display_status is not None:
        placeholder.display_status = display_status
    if display_progress is not None:
        placeholder.display_progress = display_progress
    if display_reason is not None:
        placeholder.display_reason = display_reason
    if format_hint is not None:
        placeholder.format_hint = format_hint
    if metadata is not None:
        # model uses `extra` to avoid clashing with SQLAlchemy's reserved name
        placeholder.extra = metadata
    placeholder.updated_at = datetime.now()
    if commit:
        session.commit()
    return placeholder


def delete_placeholder(session: Session, placeholder: models.Placeholder, hard: bool = False, commit: bool = False):
    """Soft-delete (mark DELETING) or hard-delete a placeholder row.

    Soft-delete keeps the row for audit and coordination. Hard-delete removes it
    from the DB (useful in tests where DB is wiped often).
    """
    if hard:
        session.delete(placeholder)
    else:
        placeholder.lifecycle_status = 'DELETING'
        placeholder.exists = False
    placeholder.updated_at = datetime.now()
    if commit:
        session.commit()


def decide_episode_placeholder_action(session: Session,
                                      episode: Optional[models.Episode] = None,
                                      season: Optional[models.Season] = None,
                                      series: Optional[models.Series] = None,
                                      allow_create: bool = True,
                                      allow_delete: bool = True) -> str:
    """Return a decision for an episode placeholder: one of
    'REQUEST_CREATE', 'REQUEST_DELETE', or 'NOOP'.

    Rules (ARR-authoritative):
    - If episode is marked deleted -> REQUEST_DELETE (if placeholder exists or path present)
    - If ARR/DB reports a real file (episode.has_file or episode.episodefile_path) -> REQUEST_DELETE
    - Else if ARR/DB reports no file AND not deleted AND allow_create True and no active placeholder -> REQUEST_CREATE
    - Otherwise -> NOOP
    """
    # Defensive defaults
    if episode is None:
        return 'NOOP'

    # If DB marks deleted, request delete if a placeholder exists or a dummypath is present
    try:
        if getattr(episode, 'is_deleted', False):
            ph = find_by_content(session,
                                 series_id=(getattr(series, 'id', None) if series else None),
                                 season_id=(getattr(season, 'id', None) if season else None),
                                 episode_id=episode.id)
            if ph or getattr(episode, 'dummypath', None):
                ep_label = format_episode_label(series=series, season=season, episode=episode)
                logger.verbose(f"DECISION computed for episode {ep_label} -> REQUEST_DELETE (is_deleted)", extra={'emoji_type': 'decision'})
                return 'REQUEST_DELETE'
            return 'NOOP'
    except Exception:
        # conservative fallback
        pass

    # ARR-authoritative: check has_file/episodefile_path (these are set by enrichment)
    has_file = bool(getattr(episode, 'has_file', False) or getattr(episode, 'episodefile_path', None))
    if has_file:
        ph = find_by_content(session,
                             series_id=(getattr(series, 'id', None) if series else None),
                             season_id=(getattr(season, 'id', None) if season else None),
                             episode_id=episode.id)
        if ph or getattr(episode, 'dummypath', None):
            ep_label = format_episode_label(series=series, season=season, episode=episode)
            logger.verbose(f"DECISION computed for episode {ep_label} -> REQUEST_DELETE (has_file)", extra={'emoji_type': 'decision'})
            return 'REQUEST_DELETE'
        return 'NOOP'

    # No file according to ARR; consider creating if allowed and no existing active placeholder
    if not allow_create:
        ep_label = format_episode_label(series=series, season=season, episode=episode)
        logger.verbose(f"DECISION computed for episode {ep_label} -> NOOP (allow_create=False)", extra={'emoji_type': 'decision'})
        return 'NOOP'

    # Skip create if there's already an active placeholder or dummypath
    existing = find_by_content(session,
                               series_id=(getattr(series, 'id', None) if series else None),
                               season_id=(getattr(season, 'id', None) if season else None),
                               episode_id=episode.id)
    if existing and (getattr(existing, 'exists', False) or getattr(existing, 'lifecycle_status', None) == 'ACTIVE'):
        ep_label = format_episode_label(series=series, season=season, episode=episode)
        logger.verbose(f"DECISION computed for episode {ep_label} -> NOOP (existing placeholder active)", extra={'emoji_type': 'decision'})
        return 'NOOP'
    if getattr(episode, 'dummypath', None):
        ep_label = format_episode_label(series=series, season=season, episode=episode)
        logger.verbose(f"DECISION computed for episode {ep_label} -> NOOP (dummypath present)", extra={'emoji_type': 'decision'})
        return 'NOOP'

    # Ensure we can resolve a folder for creation
    try:
        from services.utils import resolve_final_folder
        series_tvdb = getattr(series, 'tvdbid', None) if series else None
        final = resolve_final_folder(media_type='tv', title=(getattr(series, 'title', None) if series else None),
                                     year=(getattr(series, 'year', None) if series else None),
                                     media_id=series_tvdb,
                                     season_number=(getattr(season, 'season_number', None) if season else None))
        if not final:
            ep_label = format_episode_label(series=series, season=season, episode=episode)
            logger.verbose(f"DECISION computed for episode {ep_label} -> NOOP (no final folder)", extra={'emoji_type': 'decision'})
            return 'NOOP'
    except Exception:
        return 'NOOP'

    ep_label = format_episode_label(series=series, season=season, episode=episode)
    logger.verbose(f"DECISION computed for episode {ep_label} -> REQUEST_CREATE", extra={'emoji_type': 'decision'})
    return 'REQUEST_CREATE'


def decide_movie_placeholder_action(session: Session,
                                     movie: Optional[models.Movie] = None,
                                     allow_create: bool = True,
                                     allow_delete: bool = True) -> str:
    """Return a decision for a movie placeholder: 'REQUEST_CREATE', 'REQUEST_DELETE', or 'NOOP'.

    Rules (ARR-authoritative):
    - If movie.is_deleted -> REQUEST_DELETE (if placeholder exists)
    - If movie.has_file or movie.moviefile_path present -> REQUEST_DELETE
    - Else if no file and allow_create and no existing placeholder -> REQUEST_CREATE
    """
    if movie is None:
        return 'NOOP'

    try:
        if getattr(movie, 'is_deleted', False):
            ph = find_by_content(session, movie_id=movie.id)
            if ph or getattr(movie, 'dummypath', None):
                logger.verbose(f"DECISION computed for movie id={getattr(movie,'id',None)} -> REQUEST_DELETE (is_deleted)", extra={'emoji_type': 'decision'})
                return 'REQUEST_DELETE'
            return 'NOOP'
    except Exception:
        pass

    has_file = bool(getattr(movie, 'has_file', False) or getattr(movie, 'moviefile_path', None))
    if has_file:
        ph = find_by_content(session, movie_id=movie.id)
        if ph or getattr(movie, 'dummypath', None):
            logger.verbose(f"DECISION computed for movie id={getattr(movie,'id',None)} -> REQUEST_DELETE (has_file)", extra={'emoji_type': 'decision'})
            return 'REQUEST_DELETE'
        return 'NOOP'

    if not allow_create:
        logger.verbose(f"DECISION computed for movie id={getattr(movie,'id',None)} -> NOOP (allow_create=False)", extra={'emoji_type': 'decision'})
        return 'NOOP'

    existing = find_by_content(session, movie_id=movie.id)
    # Only consider an existing placeholder as blocking when it is actually present/active.
    # Treat PENDING/REQUESTED rows as non-blocking so the decider can request creation.
    if existing and (getattr(existing, 'exists', False) or getattr(existing, 'lifecycle_status', None) == 'ACTIVE'):
        logger.verbose(f"DECISION computed for movie id={getattr(movie,'id',None)} -> NOOP (existing placeholder active)", extra={'emoji_type': 'decision'})
        return 'NOOP'
    if getattr(movie, 'dummypath', None):
        logger.verbose(f"DECISION computed for movie id={getattr(movie,'id',None)} -> NOOP (dummypath present)", extra={'emoji_type': 'decision'})
        return 'NOOP'

    # Ensure folder resolved
    try:
        from services.utils import resolve_final_folder
        tmdb = getattr(movie, 'tmdbid', None)
        final = resolve_final_folder(media_type='movie', title=getattr(movie, 'title', None), year=getattr(movie, 'year', None), media_id=tmdb)
        if not final:
            logger.verbose(f"DECISION computed for movie id={getattr(movie,'id',None)} -> NOOP (no final folder)", extra={'emoji_type': 'decision'})
            return 'NOOP'
    except Exception:
        return 'NOOP'

    logger.verbose(f"DECISION computed for movie id={getattr(movie,'id',None)} -> REQUEST_CREATE", extra={'emoji_type': 'decision'})
    return 'REQUEST_CREATE'


def find_existing_placeholder_for_episode(session: Session,
                                          series: Optional[models.Series],
                                          season: Optional[models.Season],
                                          episode: Optional[models.Episode],
                                          payload: Optional[dict] = None,
                                          is_4k: bool = False) -> Optional[str]:
    """Try to locate an existing placeholder file on disk for a given episode.

    This function uses `resolve_final_folder` (which mirrors the logic used
    when creating placeholders) to determine the season folder and then checks
    for the canonical filename(s) we would create. Returns the full path if
    a file is found, otherwise None. Does not persist any DB changes.
    """
    if not (series and season and episode):
        return None

    try:
        # Build the canonical season folder using the same resolver used on create
        final = resolve_final_folder(
            media_type='tv',
            title=getattr(series, 'title', None),
            year=getattr(series, 'year', None),
            media_id=getattr(series, 'tvdbid', None),
            season_number=getattr(season, 'season_number', None),
            payload=payload
        )
        if not final or not os.path.isdir(final):
            return None

        # Prepare sanitized title pieces to match creation logic
        def _clean_title(t: Optional[str]) -> str:
            if not t:
                return ''
            s = sanitize_filename(t)
            # remove embedded year like " (2016)" if present to mirror place_dummy_file
            s = re.sub(r'\s*\(\d{4}\)', '', s).strip()
            return s

        series_clean = _clean_title(getattr(series, 'title', None))
        year = getattr(series, 'year', None)
        year_str = f" ({year})" if year else ''
        ep_title_clean = sanitize_filename(getattr(episode, 'title', None) or '')

        season_num = getattr(season, 'season_number', None)
        ep_num = getattr(episode, 'episode_number', None)

        candidates = []
        # Primary form used by place_dummy_file
        candidates.append(f"{series_clean}{year_str} - s{season_num:02d}e{ep_num:02d} - {ep_title_clean}.mp4")
        # Variant without episode title
        candidates.append(f"{series_clean}{year_str} - s{season_num:02d}e{ep_num:02d}.mp4")
        # Variant with uppercase S/E (some tools produce this capitalization)
        candidates.append(f"{series_clean}{year_str} - S{season_num:02d}E{ep_num:02d} - {ep_title_clean}.mp4")
        candidates.append(f"{series_clean}{year_str} - S{season_num:02d}E{ep_num:02d}.mp4")

        # Helper to validate that a matched file is actually a placeholder
        def _is_placeholder_file(fullpath: str) -> bool:
            # Must be a file
            if not os.path.isfile(fullpath):
                return False
            name = os.path.basename(fullpath).lower()
            # If filename contains explicit '(dummy)', accept immediately
            if '(dummy)' in name:
                return True

            # If a configured DUMMY_FILE_PATH exists, prefer inode/size comparison
            try:
                dummy_path = getattr(settings, 'DUMMY_FILE_PATH', None)
                if dummy_path and os.path.exists(dummy_path):
                    ds = os.stat(dummy_path)
                    fs = os.stat(fullpath)
                    # Hardlink/same-file check
                    if (hasattr(ds, 'st_ino') and hasattr(fs, 'st_ino') and
                            ds.st_ino == fs.st_ino and ds.st_dev == fs.st_dev):
                        return True
                    # Fallback: same size as dummy file (less strict)
                    if ds.st_size == fs.st_size:
                        return True
            except Exception:
                # If anything goes wrong with stat checks, conservatively reject
                return False

            # Not a placeholder (avoid matching real episode files)
            return False

        # Check canonical candidates in order and validate they're placeholders
        for fn in candidates:
            full = os.path.join(final, fn)
            if os.path.exists(full) and _is_placeholder_file(full):
                return full

        # As a conservative fallback, look for any file in the folder matching sXXeYY pattern
        # but require it to look like a placeholder (filename contains '(dummy)' or matches dummy file)
        pat = re.compile(rf'(?i)s{int(season_num):02d}[\. _-]?e{int(ep_num):02d}')
        try:
            for f in os.listdir(final):
                if pat.search(f):
                    cand_full = os.path.join(final, f)
                    if os.path.isfile(cand_full) and _is_placeholder_file(cand_full):
                        return cand_full
        except Exception:
            pass

    except Exception:
        return None

    return None


def find_existing_placeholder_for_movie(session: Session,
                                        movie: Optional[models.Movie],
                                        payload: Optional[dict] = None,
                                        is_4k: bool = False) -> Optional[str]:
    """Locate an existing placeholder file for a movie using resolve_final_folder
    and the canonical filename used by place_dummy_file. Returns full path or None.
    """
    if not movie:
        return None

    try:
        final = resolve_final_folder(
            media_type='movie',
            title=getattr(movie, 'title', None),
            year=getattr(movie, 'year', None),
            media_id=getattr(movie, 'tmdbid', None),
            payload=payload
        )
        if not final or not os.path.isdir(final):
            return None

        def _clean_title(t: Optional[str]) -> str:
            if not t:
                return ''
            s = sanitize_filename(t)
            s = re.sub(r'\s*\(\d{4}\)', '', s).strip()
            return s

        title_clean = _clean_title(getattr(movie, 'title', None))
        year = getattr(movie, 'year', None)
        year_str = f" ({year})" if year else ''

        candidate = f"{title_clean}{year_str} (dummy).mp4"
        full = os.path.join(final, candidate)
        # Validate found candidate is an actual placeholder
        def _is_movie_placeholder(fullpath: str) -> bool:
            if not os.path.isfile(fullpath):
                return False
            name = os.path.basename(fullpath).lower()
            if '(dummy)' in name:
                return True
            try:
                dummy_path = getattr(settings, 'DUMMY_FILE_PATH', None)
                if dummy_path and os.path.exists(dummy_path):
                    ds = os.stat(dummy_path)
                    fs = os.stat(fullpath)
                    if (hasattr(ds, 'st_ino') and hasattr(fs, 'st_ino') and
                            ds.st_ino == fs.st_ino and ds.st_dev == fs.st_dev):
                        return True
                    if ds.st_size == fs.st_size:
                        return True
            except Exception:
                return False
            return False

        if os.path.exists(full) and _is_movie_placeholder(full):
            return full

        # fallback: any file in final that contains '(dummy)'
        try:
            for f in os.listdir(final):
                if '(dummy)' in f.lower():
                    cand_full = os.path.join(final, f)
                    if os.path.isfile(cand_full):
                        return cand_full
        except Exception:
            pass

    except Exception:
        return None

    return None


__all__ = [
    'get_or_create_placeholder',
    'find_by_content',
    'find_by_path',
    'set_lifecycle_status',
    'mark_exists',
    'update_presentation',
    'delete_placeholder',
    'find_existing_placeholder_for_episode',
    'find_existing_placeholder_for_movie',
    # Reconciliation helper: check FS and persist attach/clear decisions
    'reconcile_placeholder_presence',
]


def reconcile_placeholder_presence(session: Session,
                                    media_type: str,
                                    movie: Optional[models.Movie] = None,
                                    series: Optional[models.Series] = None,
                                    season: Optional[models.Season] = None,
                                    episode: Optional[models.Episode] = None,
                                    payload: Optional[dict] = None,
                                    is_4k: bool = False,
                                    commit: bool = True,
                                    found_path: Optional[str] = None,
                                    validate_found_path: bool = False) -> tuple:
    """Check the filesystem for a placeholder and persist DB changes.

    Behaviour:
    - If the placeholder storage is unreachable, returns (None, 'SKIPPED-UNREACHABLE') and makes no DB changes.
    - If a placeholder file is found: ensure a Placeholder row exists, mark it exists, link it to the content row,
      set legacy dummypath/placeholder_exists for backward compatibility, and return (path, 'ATTACHED').
    - If no file is found: if a placeholder row exists and is not actively being created, clear legacy
      dummypath/placeholder_exists/placeholder_id on the content row and mark the Placeholder.exists=False.
      Returns (None, 'CLEARED').
    - Otherwise returns (None, 'NOCHANGE').

    The function intentionally uses the existing helpers in this module and defers commit control to the
    caller via the `commit` flag (default True).
    """
    try:
        # Lazy import to avoid cycles
        from services.utils import is_library_accessible
        from core.config import settings

        # Determine library root to gate clears
        if media_type == 'movie':
            lib_root = settings.MOVIE_LIBRARY_FOLDER_4K if is_4k else settings.MOVIE_LIBRARY_FOLDER
        else:
            lib_root = settings.TV_LIBRARY_FOLDER_4K if is_4k else settings.TV_LIBRARY_FOLDER

        # If no configured root, don't attempt destructive clears; treat as NOCHANGE
        if not lib_root:
            return (None, 'SKIPPED-NOLIB')

        if not is_library_accessible(lib_root):
            return (None, 'SKIPPED-UNREACHABLE')

        # If caller provided a found_path and asked us not to validate, trust it.
        # This avoids a duplicated expensive folder scan when the scanner already
        # located the exact file. Caller must only do this when it ran a full
        # authoritative scan just prior to calling this function.
        scanner_candidate = None
        if found_path:
            if not validate_found_path:
                scanner_candidate = found_path
            else:
                # minimal validation path: existence and basic placeholder heuristics
                try:
                    import os
                    if os.path.exists(found_path) and os.path.isfile(found_path):
                        scanner_candidate = found_path
                except Exception:
                    scanner_candidate = None

        # Movie flow
        if media_type == 'movie' and movie:
            if scanner_candidate:
                found = scanner_candidate
            else:
                found = find_existing_placeholder_for_movie(session=session, movie=movie, payload=payload, is_4k=is_4k)
            if found:
                ph = get_or_create_placeholder(session=session, path=found, movie_id=movie.id, created_by='reconcile', metadata=(compute_fingerprint(found) or {}), commit=False)
                mark_exists(session, ph, True, commit=False)
                # If reconciler attaches a placeholder, clear any previous decision hint
                try:
                    extra = ph.extra or {}
                    if isinstance(extra, dict) and 'decision' in extra:
                        extra.pop('decision', None)
                        ph.extra = extra
                        ph.updated_at = datetime.now()
                except Exception:
                    pass
                try:
                    movie.dummypath = found
                    movie.placeholder_exists = True
                    movie.placeholder_id = ph.id
                    session.add(movie)
                except Exception:
                    pass
                if commit:
                    session.commit()
                return (found, 'ATTACHED')

            existing = find_by_content(session, movie_id=movie.id)
            if existing and getattr(existing, 'lifecycle_status', None) not in ('CREATING', 'PENDING'):
                try:
                    movie.dummypath = None
                    movie.placeholder_exists = False
                    movie.placeholder_id = None
                    session.add(movie)
                    mark_exists(session, existing, False, commit=False)
                except Exception:
                    pass
                if commit:
                    session.commit()
                return (None, 'CLEARED')

            return (None, 'NOCHANGE')

        # Episode/TV flow
        if media_type != 'movie' and series and season and episode:
            if scanner_candidate:
                found = scanner_candidate
            else:
                found = find_existing_placeholder_for_episode(session=session, series=series, season=season, episode=episode, payload=payload, is_4k=is_4k)
            if found:
                ph = get_or_create_placeholder(session=session, path=found, series_id=series.id if series else None, season_id=season.id if season else None, episode_id=episode.id, created_by='reconcile', metadata=(compute_fingerprint(found) or {}), commit=False)
                mark_exists(session, ph, True, commit=False)
                # Clear previous 'decision' metadata when attaching
                try:
                    extra = ph.extra or {}
                    if isinstance(extra, dict) and 'decision' in extra:
                        extra.pop('decision', None)
                        ph.extra = extra
                        ph.updated_at = datetime.now()
                except Exception:
                    pass
                try:
                    episode.dummypath = found
                    episode.placeholder_exists = True
                    episode.placeholder_id = ph.id
                    session.add(episode)
                except Exception:
                    pass
                if commit:
                    session.commit()
                return (found, 'ATTACHED')

            existing = find_by_content(session, series_id=series.id if series else None, season_id=season.id if season else None, episode_id=episode.id)
            if existing and getattr(existing, 'lifecycle_status', None) not in ('CREATING', 'PENDING'):
                try:
                    episode.dummypath = None
                    episode.placeholder_exists = False
                    episode.placeholder_id = None
                    session.add(episode)
                    mark_exists(session, existing, False, commit=False)
                except Exception:
                    pass
                if commit:
                    session.commit()
                return (None, 'CLEARED')

            return (None, 'NOCHANGE')

        return (None, 'NOCHANGE')
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
        return (None, 'ERROR')
