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
            existing.updated_at = datetime.utcnow()
            if commit:
                session.commit()
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
    metadata=metadata or {},
        created_by=created_by,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
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
        return existing


def set_lifecycle_status(session: Session, placeholder: models.Placeholder, status: str, commit: bool = False) -> models.Placeholder:
    placeholder.lifecycle_status = status
    placeholder.updated_at = datetime.utcnow()
    if commit:
        session.commit()
    return placeholder


def mark_exists(session: Session, placeholder: models.Placeholder, exists: bool = True, commit: bool = False) -> models.Placeholder:
    placeholder.exists = exists
    placeholder.updated_at = datetime.utcnow()
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
    placeholder.updated_at = datetime.utcnow()
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
        placeholder.updated_at = datetime.utcnow()
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
]
