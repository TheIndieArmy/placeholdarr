"""ORM hooks: materialized series episode stats + library catalog version bumps."""

from __future__ import annotations

import threading
import time
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.orm import Session, object_session

from core.logger import logger
from services.library_catalog_version import bump_movies_version, bump_series_version
from services.postgres.models import Episode, Movie, Season, Series
from services.series_episode_stats import (
    refresh_all_series_episode_stats,
    refresh_series_episode_stats,
    resolve_series_ids_from_season_ids,
    series_ids_needing_backfill,
    should_full_refresh,
)

_hooks_registered = False
_backfill_thread_started = False
_backfill_thread_lock = threading.Lock()
_DIRTY_SERIES_KEY = "series_episode_stats_dirty_ids"
_DIRTY_SEASON_KEY = "series_episode_stats_dirty_season_ids"
_BUMP_MOVIES_KEY = "library_catalog_bump_movies"
_BUMP_SERIES_KEY = "library_catalog_bump_series"
_ADVISORY_LOCK_KEY = 8_729_0002


def _mark_series_id(session: Session, series_id: int | None) -> None:
    if series_id is None:
        return
    dirty: set[int] = session.info.setdefault(_DIRTY_SERIES_KEY, set())
    dirty.add(int(series_id))
    session.info[_BUMP_SERIES_KEY] = True


def _mark_season_id(session: Session, season_id: int | None) -> None:
    if season_id is None:
        return
    dirty: set[int] = session.info.setdefault(_DIRTY_SEASON_KEY, set())
    dirty.add(int(season_id))
    session.info[_BUMP_SERIES_KEY] = True


def _after_episode_change(_mapper, _connection, target: Episode) -> None:
    session = object_session(target)
    if session is None:
        return
    _mark_season_id(session, getattr(target, "season_id", None))
    session.info[_BUMP_SERIES_KEY] = True


def _after_season_change(_mapper, _connection, target: Season) -> None:
    session = object_session(target)
    if session is None:
        return
    _mark_series_id(session, getattr(target, "series_id", None))
    session.info[_BUMP_SERIES_KEY] = True


def _after_series_change(_mapper, _connection, target: Series) -> None:
    session = object_session(target)
    if session is None:
        return
    _mark_series_id(session, getattr(target, "id", None))
    session.info[_BUMP_SERIES_KEY] = True


def _after_movie_change(_mapper, _connection, target: Movie) -> None:
    session = object_session(target)
    if session is None:
        return
    session.info[_BUMP_MOVIES_KEY] = True


def _resolve_dirty_season_ids_before_commit(session: Session) -> None:
    """Map pending season ids to series ids while the transaction is still open."""
    season_ids: set[int] = session.info.pop(_DIRTY_SEASON_KEY, set()) or set()
    if not season_ids:
        return
    series_ids: set[int] = session.info.setdefault(_DIRTY_SERIES_KEY, set())
    series_ids.update(resolve_series_ids_from_season_ids(session, season_ids))


def _collect_dirty_series_ids(session: Session) -> set[int]:
    series_ids: set[int] = session.info.pop(_DIRTY_SERIES_KEY, set()) or set()
    return {int(x) for x in series_ids if x is not None}


def _refresh_in_new_session(
    *,
    series_ids: set[int],
    bump_movies: bool,
    bump_series: bool,
    full_series_refresh: bool = False,
) -> None:
    from services.postgres.db import get_session

    sess = get_session()
    locked = False
    try:
        locked = bool(
            sess.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": _ADVISORY_LOCK_KEY},
            ).scalar()
        )
        if not locked:
            try:
                sess.rollback()
            except Exception:
                pass
            return
        try:
            sess.execute(text("SET LOCAL lock_timeout = '15s'"))
        except Exception:
            pass

        if full_series_refresh or (series_ids and should_full_refresh(len(series_ids))):
            refresh_all_series_episode_stats(sess)
        elif series_ids:
            refresh_series_episode_stats(sess, series_ids)
        if bump_movies:
            bump_movies_version(sess)
        if bump_series:
            bump_series_version(sess)
        sess.commit()
    except Exception as exc:
        try:
            sess.rollback()
        except Exception:
            pass
        logger.warning(
            "series_episode_stats refresh skipped: %s",
            exc,
            extra={"emoji_type": "warning"},
        )
    finally:
        if locked:
            try:
                sess.execute(
                    text("SELECT pg_advisory_unlock(:k)"),
                    {"k": _ADVISORY_LOCK_KEY},
                )
                sess.commit()
            except Exception:
                try:
                    sess.rollback()
                except Exception:
                    pass
        try:
            sess.close()
        except Exception:
            pass


def _before_commit(session: Session) -> None:
    _resolve_dirty_season_ids_before_commit(session)


def _after_commit(session: Session) -> None:
    series_ids = _collect_dirty_series_ids(session)
    bump_movies = bool(session.info.pop(_BUMP_MOVIES_KEY, False))
    bump_series = bool(session.info.pop(_BUMP_SERIES_KEY, False))
    if not series_ids and not bump_movies and not bump_series:
        return
    _refresh_in_new_session(series_ids=series_ids, bump_movies=bump_movies, bump_series=bump_series)


def _after_rollback(session: Session) -> None:
    session.info.pop(_DIRTY_SERIES_KEY, None)
    session.info.pop(_DIRTY_SEASON_KEY, None)
    session.info.pop(_BUMP_MOVIES_KEY, None)
    session.info.pop(_BUMP_SERIES_KEY, None)


def register_series_episode_stats_hooks() -> None:
    global _hooks_registered
    if _hooks_registered:
        return
    _hooks_registered = True

    for model, handler in (
        (Episode, _after_episode_change),
        (Season, _after_season_change),
        (Series, _after_series_change),
        (Movie, _after_movie_change),
    ):
        event.listen(model, "after_insert", handler)
        event.listen(model, "after_update", handler)
        event.listen(model, "after_delete", handler)

    event.listen(Session, "before_commit", _before_commit)
    event.listen(Session, "after_commit", _after_commit)
    event.listen(Session, "after_rollback", _after_rollback)
    logger.info("Registered series_episode_stats ORM hooks", extra={"emoji_type": "success"})


def refresh_series_stats_after_bulk(session: Session, *, full: bool = False, series_ids: set[int] | None = None) -> None:
    """Explicit refresh for bulk SQL updates that bypass ORM listeners."""
    if full:
        refresh_all_series_episode_stats(session)
        bump_series_version(session)
        return
    ids = series_ids or set()
    if ids:
        refresh_series_episode_stats(session, ids)
    bump_series_version(session)


def bump_library_versions_after_bulk(session: Session, *, movies: bool = False, series: bool = False) -> None:
    if movies:
        bump_movies_version(session)
    if series:
        bump_series_version(session)


def ensure_series_stats_backfilled() -> None:
    """One-time backfill after migration when stats columns are null."""
    from services.postgres.db import get_session

    sess = get_session()
    try:
        if not series_ids_needing_backfill(sess):
            return
        total_series = (
            sess.query(Series.id).filter(Series.is_deleted == False, Series.stats_computed_at.is_(None)).count()
        )
        logger.info(
            "Backfilling materialized series episode stats for %s series (background)…",
            total_series,
            extra={"emoji_type": "refresh"},
        )
        started = time.monotonic()
        updated = refresh_all_series_episode_stats(sess, chunk_size=50, commit_each_chunk=True)
        bump_series_version(sess)
        sess.commit()
        logger.info(
            "Series episode stats backfill complete: updated=%s elapsed_s=%.1f",
            updated,
            time.monotonic() - started,
            extra={"emoji_type": "success"},
        )
    except Exception as exc:
        try:
            sess.rollback()
        except Exception:
            pass
        logger.warning(
            "Series episode stats backfill skipped: %s",
            exc,
            extra={"emoji_type": "warning"},
        )
    finally:
        try:
            sess.close()
        except Exception:
            pass


def schedule_series_stats_backfill() -> None:
    """Run one-time stats backfill in a daemon thread so HTTP startup is not blocked."""
    global _backfill_thread_started
    with _backfill_thread_lock:
        if _backfill_thread_started:
            return
        _backfill_thread_started = True

    def _run() -> None:
        ensure_series_stats_backfilled()

    threading.Thread(target=_run, name="series-stats-backfill", daemon=True).start()
    logger.info(
        "Series episode stats backfill scheduled in background (API will not wait for it)",
        extra={"emoji_type": "info"},
    )


def schedule_full_series_stats_refresh() -> None:
    """Post-commit full stats refresh (settings / lookahead changes)."""
    _refresh_in_new_session(
        series_ids=set(),
        bump_movies=False,
        bump_series=True,
        full_series_refresh=True,
    )
