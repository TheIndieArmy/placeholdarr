"""Fast library poster path resolution — serve pre-materialized JPEGs without per-request downloads."""

from __future__ import annotations

import os
import threading
from typing import Any

from services.placeholder_poster_art import POSTER_GRID_JPEG, POSTER_JPEG

_CACHE: dict[tuple[str, int], tuple[str, float]] = {}
_CACHE_LOCK = threading.Lock()


def _grid_or_poster_in_folder(folder: str) -> str | None:
    folder_abs = os.path.abspath(folder)
    grid = os.path.join(folder_abs, POSTER_GRID_JPEG)
    if os.path.isfile(grid):
        return grid
    poster = os.path.join(folder_abs, POSTER_JPEG)
    if os.path.isfile(poster):
        return poster
    return None


def grid_poster_path_for_movie(
    *,
    placeholder_filepath: str | None,
    has_placeholder: bool,
    movie: Any | None = None,
) -> str | None:
    """Resolve on-disk grid/poster JPEG for a movie (no HTTP, no overlay meta reads)."""
    fp = str(placeholder_filepath or "").strip()
    if fp:
        path = _grid_or_poster_in_folder(os.path.dirname(os.path.abspath(fp)))
        if path:
            return path
    if not has_placeholder or movie is None:
        return None
    try:
        from services.placeholders import movie_placeholder_path

        folder = os.path.dirname(os.path.abspath(movie_placeholder_path(movie)))
        return _grid_or_poster_in_folder(folder)
    except Exception:
        return None


def grid_poster_path_for_series(*, placeholder_folder: str | None) -> str | None:
    folder = str(placeholder_folder or "").strip()
    if not folder:
        return None
    return _grid_or_poster_in_folder(folder)


def get_cached_library_poster_path(kind: str, item_id: int) -> str | None:
    key = (str(kind or "").strip().lower(), int(item_id))
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
    if not entry:
        return None
    path, cached_mtime = entry
    try:
        mtime = float(os.path.getmtime(path))
    except OSError:
        with _CACHE_LOCK:
            _CACHE.pop(key, None)
        return None
    if mtime != cached_mtime:
        with _CACHE_LOCK:
            _CACHE.pop(key, None)
        return None
    return path


def remember_library_poster_path(kind: str, item_id: int, path: str) -> None:
    try:
        mtime = float(os.path.getmtime(path))
    except OSError:
        return
    key = (str(kind or "").strip().lower(), int(item_id))
    with _CACHE_LOCK:
        _CACHE[key] = (path, mtime)


def invalidate_library_poster_cache(kind: str | None = None, item_id: int | None = None) -> None:
    with _CACHE_LOCK:
        if kind is None and item_id is None:
            _CACHE.clear()
            return
        if kind is not None and item_id is not None:
            _CACHE.pop((str(kind).strip().lower(), int(item_id)), None)
            return
        prefix = str(kind or "").strip().lower()
        for key in list(_CACHE):
            if key[0] == prefix:
                _CACHE.pop(key, None)


def library_poster_cache_token(path: str) -> int:
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return 0


def load_library_poster_path(kind: str, item_id: int) -> tuple[str | None, str | None]:
    """Resolve local JPEG path and optional remote fallback (no downloads on this path)."""
    normalized = str(kind or "").strip().lower()
    iid = int(item_id)

    cached = get_cached_library_poster_path(normalized, iid)
    if cached:
        return cached, None

    from services.postgres.db import get_session
    from services.postgres.models import Movie, Series

    session = get_session()
    try:
        if normalized == "movie":
            movie = (
                session.query(Movie)
                .filter(Movie.id == iid, Movie.is_deleted == False)  # noqa: E712
                .first()
            )
            if not movie:
                return None, None
            path = grid_poster_path_for_movie(
                placeholder_filepath=getattr(movie, "placeholder_filepath", None),
                has_placeholder=bool(getattr(movie, "has_placeholder", False)),
                movie=movie,
            )
            remote = str(getattr(movie, "remote_poster", "") or "").strip() or None
        elif normalized == "series":
            series = (
                session.query(Series)
                .filter(Series.id == iid, Series.is_deleted == False)  # noqa: E712
                .first()
            )
            if not series:
                return None, None
            path = grid_poster_path_for_series(placeholder_folder=getattr(series, "placeholder_folder", None))
            remote = str(getattr(series, "remote_poster", "") or "").strip() or None
        else:
            return None, None
    finally:
        session.close()

    if path:
        remember_library_poster_path(normalized, iid, path)
        return path, None
    return None, remote
