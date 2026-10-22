"""Hard-purge soft-deleted catalog rows after placeholder teardown.

Gate: ``is_deleted`` plus cleaned placeholders. Never key off ``not_needed``.
"""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import or_

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import Episode, Movie, Placeholder, Season, Series, SubFlow

DEFAULT_BATCH_SIZE = 500


def _empty_stats() -> dict[str, int]:
    return {
        "movies_purged": 0,
        "series_purged": 0,
        "seasons_purged": 0,
        "episodes_purged": 0,
        "placeholders_removed": 0,
        "subflows_removed": 0,
        "skipped_not_ready": 0,
        "errors": 0,
    }


def _active_placeholder_blocks(session, *, movie_id: int | None = None, episode_id: int | None = None,
                               series_id: int | None = None, season_id: int | None = None) -> bool:
    """True when a linked Placeholder still has an active file or non-empty path."""
    q = session.query(Placeholder.id)
    if movie_id is not None:
        q = q.filter(Placeholder.movie_id == int(movie_id))
    elif episode_id is not None:
        q = q.filter(Placeholder.episode_id == int(episode_id))
    elif season_id is not None:
        q = q.filter(Placeholder.season_id == int(season_id))
    elif series_id is not None:
        q = q.filter(Placeholder.series_id == int(series_id))
    else:
        return False
    blocked = (
        q.filter(
            or_(
                Placeholder.has_placeholder == True,  # noqa: E712
                Placeholder.path != "",
            )
        )
        .limit(1)
        .first()
    )
    return blocked is not None


def movie_ready_to_purge(session, movie: Movie) -> bool:
    if not bool(getattr(movie, "is_deleted", False)):
        return False
    if bool(getattr(movie, "has_placeholder", False)):
        return False
    return not _active_placeholder_blocks(session, movie_id=int(movie.id))


def episode_ready_to_purge(session, episode: Episode) -> bool:
    if not bool(getattr(episode, "is_deleted", False)):
        return False
    if bool(getattr(episode, "has_placeholder", False)):
        return False
    return not _active_placeholder_blocks(session, episode_id=int(episode.id))


def season_ready_to_purge(session, season: Season) -> bool:
    if not bool(getattr(season, "is_deleted", False)):
        return False
    if _active_placeholder_blocks(session, season_id=int(season.id)):
        return False
    child = (
        session.query(Episode.id)
        .filter(Episode.season_id == int(season.id))
        .limit(1)
        .first()
    )
    return child is None


def series_ready_to_purge(session, series: Series) -> bool:
    if not bool(getattr(series, "is_deleted", False)):
        return False
    if _active_placeholder_blocks(session, series_id=int(series.id)):
        return False
    child = (
        session.query(Season.id)
        .filter(Season.series_id == int(series.id))
        .limit(1)
        .first()
    )
    return child is None


def _delete_subflows(
    session,
    *,
    movie_ids: list[int] | None = None,
    episode_ids: list[int] | None = None,
    season_ids: list[int] | None = None,
    series_ids: list[int] | None = None,
) -> int:
    removed = 0
    if movie_ids:
        removed += (
            session.query(SubFlow)
            .filter(SubFlow.movie_id.in_(list(movie_ids)))
            .delete(synchronize_session=False)
            or 0
        )
    if episode_ids:
        removed += (
            session.query(SubFlow)
            .filter(SubFlow.episode_id.in_(list(episode_ids)))
            .delete(synchronize_session=False)
            or 0
        )
    if season_ids:
        removed += (
            session.query(SubFlow)
            .filter(SubFlow.season_id.in_(list(season_ids)))
            .delete(synchronize_session=False)
            or 0
        )
    if series_ids:
        removed += (
            session.query(SubFlow)
            .filter(SubFlow.series_id.in_(list(series_ids)))
            .delete(synchronize_session=False)
            or 0
        )
    return int(removed)


def _delete_placeholders(
    session,
    *,
    movie_ids: list[int] | None = None,
    episode_ids: list[int] | None = None,
    season_ids: list[int] | None = None,
    series_ids: list[int] | None = None,
) -> int:
    removed = 0
    if movie_ids:
        removed += (
            session.query(Placeholder)
            .filter(Placeholder.movie_id.in_(list(movie_ids)))
            .delete(synchronize_session=False)
            or 0
        )
    if episode_ids:
        removed += (
            session.query(Placeholder)
            .filter(Placeholder.episode_id.in_(list(episode_ids)))
            .delete(synchronize_session=False)
            or 0
        )
    if season_ids:
        removed += (
            session.query(Placeholder)
            .filter(Placeholder.season_id.in_(list(season_ids)))
            .delete(synchronize_session=False)
            or 0
        )
    if series_ids:
        removed += (
            session.query(Placeholder)
            .filter(Placeholder.series_id.in_(list(series_ids)))
            .delete(synchronize_session=False)
            or 0
        )
    return int(removed)


def _purge_movie_ids(session, movie_ids: list[int], stats: dict[str, int]) -> None:
    if not movie_ids:
        return
    # Lock catalog rows for the rest of this transaction so a concurrent re-add
    # cannot clear is_deleted after readiness and still lose the row to DELETE.
    rows = (
        session.query(Movie)
        .filter(Movie.id.in_(list(movie_ids)))
        .with_for_update()
        .all()
    )
    ready_ids: list[int] = []
    for movie in rows:
        if movie_ready_to_purge(session, movie):
            ready_ids.append(int(movie.id))
        else:
            stats["skipped_not_ready"] += 1
    if not ready_ids:
        return
    stats["subflows_removed"] += _delete_subflows(session, movie_ids=ready_ids)
    stats["placeholders_removed"] += _delete_placeholders(session, movie_ids=ready_ids)
    # Conditional delete: never remove a row that was revived under the lock.
    deleted = (
        session.query(Movie)
        .filter(
            Movie.id.in_(ready_ids),
            Movie.is_deleted == True,  # noqa: E712
            Movie.has_placeholder == False,  # noqa: E712
        )
        .delete(synchronize_session=False)
        or 0
    )
    stats["movies_purged"] += int(deleted)


def _purge_episode_ids(session, episode_ids: list[int], stats: dict[str, int]) -> list[int]:
    """Purge ready episodes; return season_ids that may now be empty."""
    if not episode_ids:
        return []
    rows = (
        session.query(Episode)
        .filter(Episode.id.in_(list(episode_ids)))
        .with_for_update()
        .all()
    )
    ready_ids: list[int] = []
    season_ids: set[int] = set()
    for episode in rows:
        if episode_ready_to_purge(session, episode):
            ready_ids.append(int(episode.id))
            if getattr(episode, "season_id", None) is not None:
                season_ids.add(int(episode.season_id))
        else:
            stats["skipped_not_ready"] += 1
    if not ready_ids:
        return list(season_ids)
    stats["subflows_removed"] += _delete_subflows(session, episode_ids=ready_ids)
    stats["placeholders_removed"] += _delete_placeholders(session, episode_ids=ready_ids)
    deleted = (
        session.query(Episode)
        .filter(
            Episode.id.in_(ready_ids),
            Episode.is_deleted == True,  # noqa: E712
            Episode.has_placeholder == False,  # noqa: E712
        )
        .delete(synchronize_session=False)
        or 0
    )
    stats["episodes_purged"] += int(deleted)
    return list(season_ids)


def _purge_season_ids(session, season_ids: list[int], stats: dict[str, int]) -> list[int]:
    """Purge ready empty deleted seasons; return series_ids that may now be empty."""
    if not season_ids:
        return []
    rows = (
        session.query(Season)
        .filter(Season.id.in_(list(season_ids)))
        .with_for_update()
        .all()
    )
    ready_ids: list[int] = []
    series_ids: set[int] = set()
    for season in rows:
        if season_ready_to_purge(session, season):
            ready_ids.append(int(season.id))
            series_ids.add(int(season.series_id))
        elif bool(getattr(season, "is_deleted", False)):
            stats["skipped_not_ready"] += 1
        if getattr(season, "series_id", None) is not None:
            series_ids.add(int(season.series_id))
    if not ready_ids:
        return list(series_ids)
    stats["subflows_removed"] += _delete_subflows(session, season_ids=ready_ids)
    stats["placeholders_removed"] += _delete_placeholders(session, season_ids=ready_ids)
    deleted = (
        session.query(Season)
        .filter(
            Season.id.in_(ready_ids),
            Season.is_deleted == True,  # noqa: E712
        )
        .delete(synchronize_session=False)
        or 0
    )
    stats["seasons_purged"] += int(deleted)
    return list(series_ids)


def _purge_series_ids(session, series_ids: list[int], stats: dict[str, int]) -> None:
    if not series_ids:
        return
    rows = (
        session.query(Series)
        .filter(Series.id.in_(list(series_ids)))
        .with_for_update()
        .all()
    )
    ready_ids: list[int] = []
    for series in rows:
        if series_ready_to_purge(session, series):
            ready_ids.append(int(series.id))
        elif bool(getattr(series, "is_deleted", False)):
            stats["skipped_not_ready"] += 1
    if not ready_ids:
        return
    stats["subflows_removed"] += _delete_subflows(session, series_ids=ready_ids)
    stats["placeholders_removed"] += _delete_placeholders(session, series_ids=ready_ids)
    deleted = (
        session.query(Series)
        .filter(
            Series.id.in_(ready_ids),
            Series.is_deleted == True,  # noqa: E712
        )
        .delete(synchronize_session=False)
        or 0
    )
    stats["series_purged"] += int(deleted)


def purge_cleaned_deleted_catalog(
    *,
    movie_ids: list[int] | None = None,
    series_ids: list[int] | None = None,
    episode_ids: list[int] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Scoped hard-purge for cleaned soft-deleted catalog rows."""
    stats = _empty_stats()
    movie_ids = [int(x) for x in (movie_ids or []) if x is not None]
    episode_ids = [int(x) for x in (episode_ids or []) if x is not None]
    series_ids = [int(x) for x in (series_ids or []) if x is not None]
    if limit is not None:
        movie_ids = movie_ids[: max(0, int(limit))]
        episode_ids = episode_ids[: max(0, int(limit))]
        series_ids = series_ids[: max(0, int(limit))]
    if not movie_ids and not episode_ids and not series_ids:
        return stats

    session = get_session()
    try:
        # From scoped episodes, include deleted parent series for season/series GC.
        if episode_ids:
            parent_series = [
                int(r[0])
                for r in session.query(Series.id)
                .join(Season, Season.series_id == Series.id)
                .join(Episode, Episode.season_id == Season.id)
                .filter(Episode.id.in_(episode_ids), Series.is_deleted == True)  # noqa: E712
                .distinct()
                .all()
            ]
            series_ids = list({*series_ids, *parent_series})

        # Expand series scope to all soft-deleted child episodes first.
        if series_ids:
            child_eps = [
                int(r[0])
                for r in session.query(Episode.id)
                .join(Season, Episode.season_id == Season.id)
                .filter(Season.series_id.in_(series_ids), Episode.is_deleted == True)  # noqa: E712
                .all()
            ]
            episode_ids = list({*episode_ids, *child_eps})

        _purge_movie_ids(session, movie_ids, stats)
        touched_seasons = _purge_episode_ids(session, episode_ids, stats)

        if series_ids:
            season_ids = [
                int(r[0])
                for r in session.query(Season.id)
                .filter(Season.series_id.in_(series_ids), Season.is_deleted == True)  # noqa: E712
                .all()
            ]
            touched_seasons = list({*touched_seasons, *season_ids})

        touched_series = _purge_season_ids(session, touched_seasons, stats)
        if series_ids:
            touched_series = list({*touched_series, *series_ids})
        _purge_series_ids(session, touched_series, stats)

        session.commit()
        if (
            stats["movies_purged"]
            or stats["episodes_purged"]
            or stats["seasons_purged"]
            or stats["series_purged"]
        ):
            logger.info(f"Catalog tombstone purge (scoped): {stats}", extra={"emoji_type": "info"})
        return stats
    except Exception as exc:
        session.rollback()
        stats["errors"] += 1
        logger.warning(f"Catalog tombstone purge (scoped) failed: {exc}", extra={"emoji_type": "warning"})
        return stats
    finally:
        session.close()


def purge_cleaned_deleted_catalog_batch(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    on_progress: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Full-library sweeper: purge cleaned ``is_deleted`` catalog rows in batches.

    ``on_progress(done, total, stats)`` is optional; ``stats`` is the running purge counter dict.
    """
    stats = _empty_stats()
    batch_size = max(1, int(batch_size or DEFAULT_BATCH_SIZE))
    session = get_session()
    try:
        from services.task_run_cancel import maybe_check_active_cancel

        movie_ids = [
            int(r[0])
            for r in session.query(Movie.id).filter(Movie.is_deleted == True).order_by(Movie.id).all()  # noqa: E712
        ]
        episode_ids = [
            int(r[0])
            for r in session.query(Episode.id).filter(Episode.is_deleted == True).order_by(Episode.id).all()  # noqa: E712
        ]
        season_ids = [
            int(r[0])
            for r in session.query(Season.id).filter(Season.is_deleted == True).order_by(Season.id).all()  # noqa: E712
        ]
        series_ids = [
            int(r[0])
            for r in session.query(Series.id).filter(Series.is_deleted == True).order_by(Series.id).all()  # noqa: E712
        ]
        work_total = len(movie_ids) + len(episode_ids) + len(season_ids) + len(series_ids)
        work_done = 0

        def _emit() -> None:
            if callable(on_progress):
                try:
                    on_progress(int(work_done), int(work_total), dict(stats))
                except TypeError:
                    # Older callers that only accept (done, total).
                    try:
                        on_progress(int(work_done), int(work_total))
                    except Exception:
                        pass
                except Exception:
                    pass

        _emit()

        for i in range(0, len(movie_ids), batch_size):
            maybe_check_active_cancel(i + 1)
            chunk = movie_ids[i : i + batch_size]
            _purge_movie_ids(session, chunk, stats)
            session.commit()
            work_done += len(chunk)
            _emit()

        for i in range(0, len(episode_ids), batch_size):
            maybe_check_active_cancel(i + 1)
            chunk = episode_ids[i : i + batch_size]
            _purge_episode_ids(session, chunk, stats)
            session.commit()
            work_done += len(chunk)
            _emit()

        for i in range(0, len(season_ids), batch_size):
            maybe_check_active_cancel(i + 1)
            chunk = season_ids[i : i + batch_size]
            _purge_season_ids(session, chunk, stats)
            session.commit()
            work_done += len(chunk)
            _emit()

        for i in range(0, len(series_ids), batch_size):
            maybe_check_active_cancel(i + 1)
            chunk = series_ids[i : i + batch_size]
            _purge_series_ids(session, chunk, stats)
            session.commit()
            work_done += len(chunk)
            _emit()

        # Second pass: seasons/series that became empty after episode purge.
        leftover_seasons = [
            int(r[0])
            for r in session.query(Season.id).filter(Season.is_deleted == True).order_by(Season.id).all()  # noqa: E712
        ]
        if leftover_seasons:
            _purge_season_ids(session, leftover_seasons, stats)
            session.commit()
        leftover_series = [
            int(r[0])
            for r in session.query(Series.id).filter(Series.is_deleted == True).order_by(Series.id).all()  # noqa: E712
        ]
        if leftover_series:
            _purge_series_ids(session, leftover_series, stats)
            session.commit()

        _emit()
        logger.info(f"Catalog tombstone purge (batch): {stats}", extra={"emoji_type": "info"})
        return stats
    except Exception as exc:
        from services.task_run_cancel import TaskRunCancelled

        session.rollback()
        if isinstance(exc, TaskRunCancelled):
            raise
        stats["errors"] += 1
        logger.warning(f"Catalog tombstone purge (batch) failed: {exc}", extra={"emoji_type": "warning"})
        return stats
    finally:
        session.close()
