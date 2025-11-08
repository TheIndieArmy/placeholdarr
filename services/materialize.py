"""Materialize phase helpers: take a determination and create/delete placeholders.

Provides a simple API to request creation or deletion of placeholder files. This
module enqueues durable jobs that the worker loop will process; a synchronous
path is available (enqueue=False) for tests or one-off runs.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
import os

from services.postgres.db import get_session
from services.postgres import models
from services.placeholders import get_or_create_placeholder, find_by_content, delete_placeholder
from services.jobs import insert_job
from core.logger import logger
from core.config import settings


def apply_placeholder_decision(session, media_type: str,
                               movie: Optional[models.Movie] = None,
                               series: Optional[models.Series] = None,
                               season: Optional[models.Season] = None,
                               episode: Optional[models.Episode] = None,
                               decision: str = 'NOOP',
                               is_4k: bool = False,
                               enqueue: bool = True,
                               commit: bool = True) -> dict:
    """Apply a decision (REQUEST_CREATE / REQUEST_DELETE / NOOP) for content.

    Returns a dict with keys: action, placeholder_id (if created/found), queued (bool), reason
    """
    try:
        # Movie flow
        if media_type == 'movie' and movie:
            lib_root = settings.MOVIE_LIBRARY_FOLDER_4K if is_4k else settings.MOVIE_LIBRARY_FOLDER
            if not lib_root:
                return {'action': 'SKIPPED-NOLIB', 'queued': False, 'reason': 'no_library_root'}

            if decision == 'REQUEST_CREATE':
                # planned path: use legacy resolver to mirror final folder and filename
                try:
                    from services.services_old.utils import resolve_final_folder, sanitize_filename
                    media_id = getattr(movie, 'tmdbid', None) or getattr(movie, 'id', None)
                    folder = resolve_final_folder(media_type='movie', title=getattr(movie, 'title', None), year=getattr(movie, 'year', None), media_id=media_id)
                    if not folder:
                        folder = os.path.join(lib_root, str(media_id))
                    title_safe = getattr(movie, 'title', None) or 'unknown'
                    year_val = getattr(movie, 'year', None)
                    year_str = f" ({year_val})" if year_val else ''
                    filename = f"{sanitize_filename(title_safe)}{year_str} (dummy).mp4"
                    path = os.path.join(folder, sanitize_filename(filename))
                except Exception:
                    media_id = getattr(movie, 'tmdbid', None) or getattr(movie, 'id', None)
                    folder = os.path.join(lib_root, str(media_id))
                    filename = f"{(getattr(movie,'title') or 'unknown').replace(' ', '_')}_{media_id}.dummy"
                    path = os.path.join(folder, filename)
                ph = get_or_create_placeholder(session=session, path=path, movie_id=movie.id, created_by='materialize', metadata={'decision': decision}, commit=commit)
                # mark requested
                try:
                    ph.lifecycle_status = 'REQUESTED'
                    ph.determination = decision
                    ph.determination_updated_at = datetime.now()
                    if commit:
                        session.add(ph); session.commit()
                except Exception:
                    try:
                        session.rollback()
                    except Exception:
                        pass
                queued = False
                if enqueue:
                    payload = {'placeholder_id': ph.id, 'media_type': 'movie', 'movie_id': movie.id, 'library_root': lib_root, 'title': movie.title, 'year': movie.year, 'media_id': media_id}
                    group = f"placeholder:create:movie:{movie.id}"
                    insert_job('placeholder:create', payload, group_id=group)
                    queued = True
                return {'action': 'REQUEST_CREATE', 'placeholder_id': ph.id, 'queued': queued}

            if decision == 'REQUEST_DELETE':
                existing = find_by_content(session, movie_id=movie.id)
                if not existing:
                    return {'action': 'NOOP', 'queued': False, 'reason': 'no_placeholder'}
                # mark deleting and enqueue
                delete_placeholder(session, existing, hard=False, commit=commit)
                queued = False
                if enqueue:
                    payload = {'placeholder_id': existing.id, 'path': existing.path}
                    group = f"placeholder:delete:{existing.id}"
                    insert_job('placeholder:delete', payload, group_id=group)
                    queued = True
                return {'action': 'REQUEST_DELETE', 'placeholder_id': existing.id, 'queued': queued}

            return {'action': 'NOOP', 'queued': False}

        # Episode/TV flow
        if media_type != 'movie' and series and season and episode:
            lib_root = settings.TV_LIBRARY_FOLDER_4K if is_4k else settings.TV_LIBRARY_FOLDER
            if not lib_root:
                return {'action': 'SKIPPED-NOLIB', 'queued': False, 'reason': 'no_library_root'}

            media_id = getattr(series, 'tvdbid', None) or getattr(series, 'id', None)
            # Use legacy resolver to compute final series/season folder
            try:
                from services.services_old.utils import resolve_final_folder, sanitize_filename
                folder = resolve_final_folder(media_type='tv', title=getattr(series, 'title', None), year=getattr(series, 'year', None), media_id=media_id, season_number=getattr(season, 'season_number', None))
                if not folder:
                    folder = os.path.join(lib_root, str(media_id))
                series_title_safe = getattr(series, 'title', None) or 'unknown'
                series_year = getattr(series, 'year', None)
                series_year_str = f" ({series_year})" if series_year else ''
                filename = f"{sanitize_filename(series_title_safe)}{series_year_str} - s{getattr(season,'season_number',0):02d}e{getattr(episode,'episode_number',0):02d} - {sanitize_filename(getattr(episode, 'title') or '')}.mp4"
                path = os.path.join(folder, sanitize_filename(filename))
            except Exception:
                media_id = getattr(series, 'tvdbid', None) or getattr(series, 'id', None)
                folder = os.path.join(lib_root, str(media_id))
                # use season/episode to name; simple convention for planned path
                filename = f"{(getattr(series,'title') or 'unknown').replace(' ', '_')}_{media_id}_s{getattr(season,'season_number',0):02d}e{getattr(episode,'episode_number',0):02d}.dummy"
                path = os.path.join(folder, filename)

            if decision == 'REQUEST_CREATE':
                ph = get_or_create_placeholder(session=session, path=path, series_id=series.id, season_id=season.id, episode_id=episode.id, created_by='materialize', metadata={'decision': decision}, commit=commit)
                try:
                    ph.lifecycle_status = 'REQUESTED'
                    ph.determination = decision
                    ph.determination_updated_at = datetime.now()
                    if commit:
                        session.add(ph); session.commit()
                except Exception:
                    try:
                        session.rollback()
                    except Exception:
                        pass
                queued = False
                if enqueue:
                    # Include explicit season/episode numbers and episode title so
                    # the worker doesn't need to rely on additional DB lookups.
                    payload = {
                        'placeholder_id': ph.id,
                        'media_type': 'tv',
                        'series_id': series.id,
                        'season_id': season.id,
                        'episode_id': episode.id,
                        'season_number': getattr(season, 'season_number', None),
                        'episode_number': getattr(episode, 'episode_number', None),
                        'episode_title': getattr(episode, 'title', None),
                        'library_root': lib_root,
                        'title': series.title,
                        'year': series.year,
                        'media_id': media_id,
                    }
                    group = f"placeholder:create:tv:{series.id}:{season.id}:{episode.id}"
                    insert_job('placeholder:create', payload, group_id=group)
                    queued = True
                    # Try to create the placeholder synchronously here to avoid races
                    # where worker payloads may lack derived fields or visibility in
                    # distributed setups. This is a best-effort, non-fatal step: if
                    # creation fails we'll leave the placeholder in REQUESTED so
                    # the worker can retry asynchronously.
                    try:
                        from services.integrations import place_dummy_file
                        from services.placeholders import mark_exists, compute_fingerprint
                        # Only attempt if we have the minimal inputs
                        sn = getattr(season, 'season_number', None)
                        en = getattr(episode, 'episode_number', None)
                        if lib_root and series.title and media_id is not None and sn is not None and en is not None:
                            placed = place_dummy_file('tv', series.title, series.year or 0, media_id, lib_root, season_number=int(sn), episode_number=int(en), episode_title=getattr(episode, 'title', None))
                            if placed:
                                try:
                                    ph.path = placed
                                    mark_exists(session, ph, True, commit=False)
                                    fp = compute_fingerprint(placed) or {}
                                    extra = ph.extra or {}
                                    if not isinstance(extra, dict):
                                        extra = {}
                                    extra.update({'fingerprint': fp})
                                    ph.extra = extra
                                    ph.lifecycle_status = 'ACTIVE'
                                    from datetime import datetime
                                    ph.last_observed_at = datetime.now()
                                    session.add(ph)
                                    session.commit()
                                except Exception:
                                    try:
                                        session.rollback()
                                    except Exception:
                                        pass
                    except Exception:
                        # Non-fatal; leave placeholder requested for worker to process
                        pass
                return {'action': 'REQUEST_CREATE', 'placeholder_id': ph.id, 'queued': queued}

            if decision == 'REQUEST_DELETE':
                existing = find_by_content(session, series_id=series.id, season_id=season.id, episode_id=episode.id)
                if not existing:
                    return {'action': 'NOOP', 'queued': False, 'reason': 'no_placeholder'}
                delete_placeholder(session, existing, hard=False, commit=commit)
                queued = False
                if enqueue:
                    payload = {'placeholder_id': existing.id, 'path': existing.path}
                    group = f"placeholder:delete:{existing.id}"
                    insert_job('placeholder:delete', payload, group_id=group)
                    queued = True
                return {'action': 'REQUEST_DELETE', 'placeholder_id': existing.id, 'queued': queued}

            return {'action': 'NOOP', 'queued': False}

        return {'action': 'NOOP', 'queued': False}
    except Exception as e:
        logger.exception(f"apply_placeholder_decision failed: {e}")
        try:
            session.rollback()
        except Exception:
            pass
        return {'action': 'ERROR', 'queued': False, 'reason': str(e)}
