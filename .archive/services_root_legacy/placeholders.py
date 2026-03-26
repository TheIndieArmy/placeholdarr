"""Core placeholder helpers (ported from services_old with focused set of functions).

These helpers accept a SQLAlchemy Session and avoid committing by default so
callers can control transaction boundaries.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from services.postgres import models
from core.config import settings
import os, re
import hashlib

logger = logging.getLogger(__name__)


class PlaceholderManagerError(Exception):
    pass


def _validate_target(movie_id: Optional[int], series_id: Optional[int], season_id: Optional[int], episode_id: Optional[int]):
    if not any((movie_id, series_id, season_id, episode_id)):
        raise ValueError("must provide at least one of movie_id, series_id, season_id, episode_id")
    if movie_id and any((series_id, season_id, episode_id)):
        raise ValueError("movie_id cannot be combined with series/season/episode ids")


def find_by_content(session: Session, movie_id: Optional[int] = None, series_id: Optional[int] = None, season_id: Optional[int] = None, episode_id: Optional[int] = None) -> Optional[models.Placeholder]:
    _validate_target(movie_id, series_id, season_id, episode_id)

    q = session.query(models.Placeholder)
    if movie_id:
        return q.filter(models.Placeholder.movie_id == movie_id).one_or_none()
    if episode_id:
        return q.filter(
            models.Placeholder.episode_id == episode_id,
            models.Placeholder.season_id == season_id,
            models.Placeholder.series_id == series_id,
        ).one_or_none()
    if season_id:
        return q.filter(models.Placeholder.season_id == season_id, models.Placeholder.series_id == series_id).one_or_none()
    return q.filter(models.Placeholder.series_id == series_id).one_or_none()


def find_by_path(session: Session, path: str) -> Optional[models.Placeholder]:
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
    _validate_target(movie_id, series_id, season_id, episode_id)

    existing = find_by_content(session, movie_id, series_id, season_id, episode_id)
    if existing:
        if path and existing.path != path:
            existing.path = path
            existing.updated_at = datetime.now()
            if commit:
                session.commit()
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

    placeholder = models.Placeholder(
        movie_id=movie_id,
        series_id=series_id,
        season_id=season_id,
        episode_id=episode_id,
        path=path,
        has_placeholder=False,
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
        session.flush()
        if commit:
            session.commit()
        return placeholder
    except IntegrityError:
        logger.debug("IntegrityError creating placeholder; fetching existing row")
        session.rollback()
        existing = find_by_content(session, movie_id, series_id, season_id, episode_id)
        if not existing:
            raise PlaceholderManagerError("Failed to create placeholder and existing row not found")
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
    placeholder.has_placeholder = exists
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
        placeholder.extra = metadata
    placeholder.updated_at = datetime.now()
    if commit:
        session.commit()
    return placeholder


def delete_placeholder(session: Session, placeholder: models.Placeholder, hard: bool = False, commit: bool = False):
    if hard:
        session.delete(placeholder)
    else:
        placeholder.lifecycle_status = 'DELETING'
        placeholder.has_placeholder = False
    placeholder.updated_at = datetime.now()
    if commit:
        session.commit()


__all__ = [
    'get_or_create_placeholder',
    'find_by_content',
    'find_by_path',
    'set_lifecycle_status',
    'mark_exists',
    'update_presentation',
    'delete_placeholder',
    'compute_fingerprint',
]
