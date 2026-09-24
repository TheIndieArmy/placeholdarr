"""Determination for TMDB Discover movie and show-level series placeholders."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from core.logger import logger
from services.discover.mode import skip_monitored_any_instance, skip_placeholder_when_monitored
from services.postgres.db import get_session
from services.postgres.models import ArrMovieOverlay, ArrSeriesOverlay, TmdbMovie, TmdbSeries
from services.source_of_truth.determiner import (
    DETERMINATION_EXISTS,
    DETERMINATION_NEEDS,
    DETERMINATION_NOT_NEEDED,
    DETERMINATION_OBSOLETE,
)


def _overlay_flags_from_rows(rows: list[Any]) -> tuple[bool, bool]:
    """Return (any_has_file, monitored_skip) from preloaded overlay rows."""
    any_file = any(bool(r.has_file) for r in rows)
    if not skip_placeholder_when_monitored():
        return any_file, False
    _ = skip_monitored_any_instance()
    monitored = any(bool(r.monitored) for r in rows)
    return any_file, monitored


def _overlay_flags(session, tmdb_id: int) -> tuple[bool, bool]:
    rows = session.query(ArrMovieOverlay).filter(ArrMovieOverlay.tmdb_id == int(tmdb_id)).all()
    return _overlay_flags_from_rows(rows)


def _series_overlay_flags(session, tmdb_id: int) -> tuple[bool, bool]:
    rows = session.query(ArrSeriesOverlay).filter(ArrSeriesOverlay.tmdb_id == int(tmdb_id)).all()
    return _overlay_flags_from_rows(rows)


def _resolve_determination(
    *,
    has_placeholder: bool,
    placeholder_filepath: str | None,
    any_file: bool,
    monitored_skip: bool,
) -> str:
    if any_file:
        if has_placeholder:
            return DETERMINATION_OBSOLETE
        return DETERMINATION_NOT_NEEDED
    if monitored_skip:
        if has_placeholder:
            return DETERMINATION_OBSOLETE
        return DETERMINATION_NOT_NEEDED
    if has_placeholder and placeholder_filepath:
        return DETERMINATION_EXISTS
    return DETERMINATION_NEEDS


def resolve_tmdb_movie_determination(
    row: TmdbMovie,
    *,
    session=None,
    overlay_rows: list[ArrMovieOverlay] | None = None,
) -> str:
    if overlay_rows is not None:
        any_file, monitored_skip = _overlay_flags_from_rows(overlay_rows)
    else:
        any_file, monitored_skip = _overlay_flags(session, int(row.tmdb_id))
    return _resolve_determination(
        has_placeholder=bool(row.has_placeholder),
        placeholder_filepath=row.placeholder_filepath,
        any_file=any_file,
        monitored_skip=monitored_skip,
    )


def resolve_tmdb_series_determination(
    row: TmdbSeries,
    *,
    session=None,
    overlay_rows: list[ArrSeriesOverlay] | None = None,
) -> str:
    if overlay_rows is not None:
        any_file, monitored_skip = _overlay_flags_from_rows(overlay_rows)
    else:
        any_file, monitored_skip = _series_overlay_flags(session, int(row.tmdb_id))
    return _resolve_determination(
        has_placeholder=bool(row.has_placeholder),
        placeholder_filepath=row.placeholder_filepath,
        any_file=any_file,
        monitored_skip=monitored_skip,
    )


def list_undetermined_tmdb_ids(*, session=None) -> list[int]:
    """Catalog movie rows that have never been scored."""
    own = session is None
    session = session or get_session()
    try:
        rows = (
            session.query(TmdbMovie.tmdb_id)
            .filter(TmdbMovie.determination.is_(None))
            .order_by(TmdbMovie.tmdb_id.asc())
            .all()
        )
        return [int(r[0]) for r in rows]
    finally:
        if own:
            session.close()


def list_undetermined_series_tmdb_ids(*, session=None) -> list[int]:
    own = session is None
    session = session or get_session()
    try:
        rows = (
            session.query(TmdbSeries.tmdb_id)
            .filter(TmdbSeries.determination.is_(None))
            .order_by(TmdbSeries.tmdb_id.asc())
            .all()
        )
        return [int(r[0]) for r in rows]
    finally:
        if own:
            session.close()


def _run_determination(
    *,
    model,
    overlay_model,
    resolve_fn,
    label: str,
    session=None,
    tmdb_ids: list[int] | None = None,
) -> dict[str, Any]:
    own = session is None
    session = session or get_session()
    stats = {
        "updated": 0,
        "needs": 0,
        "exists": 0,
        "obsolete": 0,
        "not_needed": 0,
        "scoped": tmdb_ids is not None,
        "scoped_count": len(tmdb_ids) if tmdb_ids is not None else None,
    }
    try:
        q = session.query(model)
        if tmdb_ids is not None:
            ids = [int(x) for x in tmdb_ids]
            if not ids:
                return stats
            q = q.filter(model.tmdb_id.in_(ids))
        rows = q.all()
        overlay_q = session.query(overlay_model)
        if tmdb_ids is not None:
            overlay_q = overlay_q.filter(overlay_model.tmdb_id.in_([int(x) for x in tmdb_ids]))
        by_tmdb: dict[int, list[Any]] = defaultdict(list)
        for ov in overlay_q.all():
            by_tmdb[int(ov.tmdb_id)].append(ov)

        scope_note = f" (scoped to {len(rows)} id(s))" if tmdb_ids is not None else ""
        logger.info(
            f"Discover determination: scoring {len(rows)} catalog {label}(s){scope_note} "
            f"({len(by_tmdb)} with Arr overlay)",
            extra={"emoji_type": "info"},
        )
        now = datetime.now(timezone.utc)
        for i, row in enumerate(rows, start=1):
            det = resolve_fn(row, overlay_rows=by_tmdb.get(int(row.tmdb_id), []))
            if row.determination != det:
                row.determination = det
                row.determination_updated_at = now
                stats["updated"] += 1
            key = {
                DETERMINATION_NEEDS: "needs",
                DETERMINATION_EXISTS: "exists",
                DETERMINATION_OBSOLETE: "obsolete",
                DETERMINATION_NOT_NEEDED: "not_needed",
            }.get(det)
            if key:
                stats[key] += 1
            if i % 2000 == 0:
                logger.info(
                    f"Discover determination ({label}): scored {i}/{len(rows)}…",
                    extra={"emoji_type": "info"},
                )
        session.commit()
        return stats
    except Exception:
        session.rollback()
        raise
    finally:
        if own:
            session.close()


def run_discover_determination(*, session=None, tmdb_ids: list[int] | None = None) -> dict[str, Any]:
    return _run_determination(
        model=TmdbMovie,
        overlay_model=ArrMovieOverlay,
        resolve_fn=resolve_tmdb_movie_determination,
        label="movie",
        session=session,
        tmdb_ids=tmdb_ids,
    )


def run_discover_series_determination(
    *, session=None, tmdb_ids: list[int] | None = None
) -> dict[str, Any]:
    return _run_determination(
        model=TmdbSeries,
        overlay_model=ArrSeriesOverlay,
        resolve_fn=resolve_tmdb_series_determination,
        label="series",
        session=session,
        tmdb_ids=tmdb_ids,
    )
