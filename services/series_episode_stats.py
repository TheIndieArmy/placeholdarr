"""Materialized per-series episode counters for fast library reads."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

from services.library_future_semantics import sql_episode_future_outside_lookahead
from services.postgres.models import Episode, Season, Series

SERIES_STAT_KEYS = (
    "episode_total",
    "episode_files",
    "episode_placeholders",
    "episode_future",
    "episode_missing",
)

_FULL_REFRESH_THRESHOLD = 200


def series_counts_empty() -> dict[str, int]:
    return {k: 0 for k in SERIES_STAT_KEYS}


def series_stats_dict_from_row(series: Series) -> dict[str, int]:
    """Read materialized counters from a Series ORM row."""
    return {
        "episode_total": int(getattr(series, "episode_total", 0) or 0),
        "episode_files": int(getattr(series, "episode_files", 0) or 0),
        "episode_placeholders": int(getattr(series, "episode_placeholders", 0) or 0),
        "episode_future": int(getattr(series, "episode_future", 0) or 0),
        "episode_missing": int(getattr(series, "episode_missing", 0) or 0),
    }


def compute_series_episode_stats(session: Session, series_id: int) -> dict[str, int]:
    """Aggregate episode counts for one series (respects deleted season/episode rows)."""
    row = (
        session.query(
            func.count(Episode.id).label("episode_total"),
            func.sum(case((Episode.has_file == True, 1), else_=0)).label("episode_files"),
            func.sum(case((Episode.has_placeholder == True, 1), else_=0)).label("episode_placeholders"),
            func.sum(
                case(
                    (sql_episode_future_outside_lookahead(Episode, Season), 1),
                    else_=0,
                )
            ).label("episode_future"),
            func.sum(
                case(
                    (
                        and_(
                            func.coalesce(Episode.has_file, False) == False,
                            func.coalesce(Episode.has_placeholder, False) == False,
                            or_(Episode.determination.is_(None), Episode.determination != "not_needed"),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("episode_missing"),
        )
        .select_from(Episode)
        .join(Season, Season.id == Episode.season_id)
        .filter(
            Season.series_id == int(series_id),
            Episode.is_deleted == False,
            Season.is_deleted == False,
        )
        .first()
    )
    if not row:
        return series_counts_empty()
    return {
        "episode_total": int(row.episode_total or 0),
        "episode_files": int(row.episode_files or 0),
        "episode_placeholders": int(row.episode_placeholders or 0),
        "episode_future": int(row.episode_future or 0),
        "episode_missing": int(row.episode_missing or 0),
    }


def _apply_stats_to_series(series: Series, stats: dict[str, int], *, now: datetime) -> None:
    series.episode_total = int(stats["episode_total"])
    series.episode_files = int(stats["episode_files"])
    series.episode_placeholders = int(stats["episode_placeholders"])
    series.episode_future = int(stats["episode_future"])
    series.episode_missing = int(stats["episode_missing"])
    series.stats_computed_at = now


def refresh_series_episode_stats(session: Session, series_ids: Iterable[int]) -> int:
    """Recompute and persist stats for the given series ids. Returns rows updated."""
    ids = sorted({int(x) for x in series_ids if x is not None})
    if not ids:
        return 0
    now = datetime.now(timezone.utc)
    updated = 0
    for series_id in ids:
        series = session.get(Series, series_id)
        if series is None or bool(getattr(series, "is_deleted", False)):
            continue
        stats = compute_series_episode_stats(session, series_id)
        _apply_stats_to_series(series, stats, now=now)
        updated += 1
    return updated


def refresh_all_series_episode_stats(session: Session, *, chunk_size: int = 100) -> int:
    """Recompute stats for every non-deleted series row."""
    ids = [
        int(row[0])
        for row in session.query(Series.id).filter(Series.is_deleted == False).all()
    ]
    if not ids:
        return 0
    total = 0
    for i in range(0, len(ids), chunk_size):
        total += refresh_series_episode_stats(session, ids[i : i + chunk_size])
    return total


def series_ids_needing_backfill(session: Session, *, limit: int = 1) -> bool:
    """True when at least one active series row lacks materialized stats."""
    row = (
        session.query(Series.id)
        .filter(Series.is_deleted == False, Series.stats_computed_at.is_(None))
        .limit(limit)
        .first()
    )
    return row is not None


def resolve_series_ids_from_season_ids(session: Session, season_ids: Iterable[int]) -> set[int]:
    ids = sorted({int(x) for x in season_ids if x is not None})
    if not ids:
        return set()
    rows = session.query(Season.series_id).filter(Season.id.in_(ids)).all()
    return {int(r[0]) for r in rows if r and r[0] is not None}


def should_full_refresh(dirty_series_count: int) -> bool:
    return dirty_series_count > _FULL_REFRESH_THRESHOLD


def series_episode_counts_map(session: Session, series_rows: list[Series]) -> dict[int, dict[str, int]]:
    """Build series_id -> stats dict from materialized Series columns (compute inline if not yet backfilled)."""
    out: dict[int, dict[str, int]] = {}
    for series in series_rows:
        if getattr(series, "stats_computed_at", None) is None:
            out[int(series.id)] = compute_series_episode_stats(session, int(series.id))
        else:
            out[int(series.id)] = series_stats_dict_from_row(series)
    return out
