"""Singleton version counters for library shelf ETag polling."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from services.postgres.models import LibraryCatalogVersion


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _get_or_create_row(session: Session) -> LibraryCatalogVersion:
    row = session.get(LibraryCatalogVersion, 1)
    if row is None:
        row = LibraryCatalogVersion(id=1, movies_version=0, series_version=0)
        session.add(row)
        session.flush()
    return row


def get_library_versions(session: Session) -> dict[str, int]:
    row = session.get(LibraryCatalogVersion, 1)
    if row is None:
        return {"movies_version": 0, "series_version": 0}
    return {
        "movies_version": int(row.movies_version or 0),
        "series_version": int(row.series_version or 0),
    }


def bump_movies_version(session: Session) -> int:
    row = _get_or_create_row(session)
    row.movies_version = int(row.movies_version or 0) + 1
    row.updated_at = _utc_now()
    return int(row.movies_version)


def bump_series_version(session: Session) -> int:
    row = _get_or_create_row(session)
    row.series_version = int(row.series_version or 0) + 1
    row.updated_at = _utc_now()
    return int(row.series_version)


def library_etag_for_shelf(session: Session, media_type: str) -> str:
    versions = get_library_versions(session)
    mt = str(media_type or "").strip().lower()
    if mt in {"movie", "movies"}:
        return str(versions["movies_version"])
    if mt in {"series", "tv"}:
        return str(versions["series_version"])
    combined = int(versions["movies_version"]) + int(versions["series_version"])
    return str(combined)
