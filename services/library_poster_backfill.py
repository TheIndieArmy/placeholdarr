"""Background creation of poster-grid.jpg for library grids (offline, not on browser request)."""

from __future__ import annotations

import os
import threading
import time

from core.logger import logger
from services.library_poster_paths import invalidate_library_poster_cache
from services.placeholder_poster_art import (
    POSTER_GRID_JPEG,
    POSTER_JPEG,
    ensure_library_grid_poster_for_movie,
    ensure_library_grid_poster_for_series,
    write_library_grid_poster,
)
from services.postgres.db import get_session
from services.postgres.models import Movie, Series

_BACKFILL_THREAD_STARTED = False
_BACKFILL_THREAD_LOCK = threading.Lock()

_BATCH_SIZE = 80
_BATCH_SLEEP_S = 0.05


def _folder_needs_grid(folder: str) -> bool:
    folder_abs = os.path.abspath(folder)
    grid = os.path.join(folder_abs, POSTER_GRID_JPEG)
    if os.path.isfile(grid):
        return False
    poster = os.path.join(folder_abs, POSTER_JPEG)
    return os.path.isfile(poster)


def backfill_library_grid_posters(*, batch_size: int = _BATCH_SIZE) -> int:
    """Create missing poster-grid.jpg beside existing poster.jpg. Returns rows touched."""
    session = get_session()
    written = 0
    try:
        movie_rows = (
            session.query(Movie)
            .filter(Movie.is_deleted == False, Movie.has_placeholder == True)  # noqa: E712
            .order_by(Movie.id.asc())
            .all()
        )
        for movie in movie_rows:
            fp = str(getattr(movie, "placeholder_filepath", "") or "").strip()
            if not fp:
                continue
            folder = os.path.dirname(os.path.abspath(fp))
            if not _folder_needs_grid(folder):
                continue
            if ensure_library_grid_poster_for_movie(movie, fp):
                written += 1
            elif write_library_grid_poster(folder, getattr(movie, "remote_poster", None)):
                written += 1
            if written % batch_size == 0:
                time.sleep(_BATCH_SLEEP_S)

        series_rows = (
            session.query(Series)
            .filter(Series.is_deleted == False)  # noqa: E712
            .filter(Series.placeholder_folder.isnot(None))
            .order_by(Series.id.asc())
            .all()
        )
        for series in series_rows:
            folder = str(getattr(series, "placeholder_folder", "") or "").strip()
            if not folder or not _folder_needs_grid(folder):
                continue
            if ensure_library_grid_poster_for_series(series, folder):
                written += 1
            elif write_library_grid_poster(folder, getattr(series, "remote_poster", None)):
                written += 1
            if written % batch_size == 0:
                time.sleep(_BATCH_SLEEP_S)
    except Exception as exc:
        logger.warning(
            "Library poster grid backfill error: %s",
            exc,
            extra={"emoji_type": "warning"},
        )
    finally:
        session.close()
    return written


def schedule_library_poster_grid_backfill() -> None:
    """Run grid backfill in a daemon thread so HTTP startup is not blocked."""
    global _BACKFILL_THREAD_STARTED
    with _BACKFILL_THREAD_LOCK:
        if _BACKFILL_THREAD_STARTED:
            return
        _BACKFILL_THREAD_STARTED = True

    def _run() -> None:
        started = time.monotonic()
        try:
            written = backfill_library_grid_posters()
            if written:
                invalidate_library_poster_cache()
                logger.info(
                    "Library poster grid backfill complete: written=%s elapsed_s=%.1f",
                    written,
                    time.monotonic() - started,
                    extra={"emoji_type": "success"},
                )
        except Exception as exc:
            logger.warning(
                "Library poster grid backfill skipped: %s",
                exc,
                extra={"emoji_type": "warning"},
            )

    threading.Thread(target=_run, name="library-poster-grid-backfill", daemon=True).start()
    logger.info(
        "Library poster grid backfill scheduled in background (browser poster requests stay read-only)",
        extra={"emoji_type": "info"},
    )
