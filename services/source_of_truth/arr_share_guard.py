"""Guard disk cleanup when the same logical title exists on another active ARR instance."""

from __future__ import annotations

import os
import re
from typing import Any

from core.config import settings
from services.postgres.models import Episode, Movie, Placeholder, Season, Series


def _normalize_instance_key(value: Any) -> str:
    key_raw = str(value or "").strip().lower()
    return re.sub(r"[^a-z0-9_-]+", "_", key_raw).strip("_-")


def configured_instance_keys(arr_type: str) -> set[str]:
    """Normalized instance_key values for configured Radarr or Sonarr rows."""
    want = str(arr_type or "").strip().lower()
    keys: set[str] = set()
    for item in getattr(settings, "configured_arr_instances", []) or []:
        if str(item.get("arr_type") or "").strip().lower() != want:
            continue
        k = _normalize_instance_key(item.get("instance_key") or "")
        if k:
            keys.add(k)
    return keys


def filter_movie_disk_cleanup_paths(session, movie: Movie, paths: list[str]) -> list[str]:
    """Remove paths still referenced by a non-deleted movie with the same TMDB id on a configured instance."""
    tmdbid = int(getattr(movie, "tmdbid", None) or 0)
    if not tmdbid or not paths:
        return paths
    allowed = configured_instance_keys("radarr")
    if not allowed:
        return paths
    mid = int(getattr(movie, "id", 0) or 0)
    norm_paths = {_norm_abs(p) for p in paths if p}
    if not norm_paths:
        return paths

    sibling = (
        session.query(Movie)
        .filter(
            Movie.id != mid,
            Movie.tmdbid == tmdbid,
            Movie.is_deleted == False,  # noqa: E712
            Movie.instance_key.in_(tuple(allowed)),
        )
        .all()
    )
    protected: set[str] = set()
    for m in sibling:
        fp = getattr(m, "placeholder_filepath", None)
        if fp:
            protected.add(_norm_abs(fp))
        for row in (
            session.query(Placeholder.path)
            .filter(
                Placeholder.movie_id == int(m.id),
                Placeholder.has_placeholder == True,  # noqa: E712
            )
            .all()
        ):
            if row[0]:
                protected.add(_norm_abs(row[0]))

    return [p for p in paths if p and _norm_abs(p) not in protected]


def _norm_abs(path: str) -> str:
    try:
        return os.path.abspath(str(path or ""))
    except Exception:
        return str(path or "")


def active_sibling_series_exists(
    session,
    *,
    exclude_series_id: int,
    tvdbid: int,
    allowed_instance_keys: set[str],
) -> bool:
    if not tvdbid or not allowed_instance_keys:
        return False
    row = (
        session.query(Series.id)
        .filter(
            Series.id != int(exclude_series_id),
            Series.tvdbid == int(tvdbid),
            Series.is_deleted == False,  # noqa: E712
            Series.instance_key.in_(tuple(allowed_instance_keys)),
        )
        .first()
    )
    return bool(row)


def filter_episode_disk_cleanup_paths(
    session,
    *,
    series: Series,
    paths: list[str],
) -> list[str]:
    """Episode placeholder paths still owned by a sibling series (same TVDB) must not be deleted from disk."""
    tvdbid = int(getattr(series, "tvdbid", 0) or 0)
    sid = int(getattr(series, "id", 0) or 0)
    if not tvdbid or not paths:
        return paths
    allowed = configured_instance_keys("sonarr")
    if not allowed:
        return paths

    sibling_series_ids = [
        int(r[0])
        for r in (
            session.query(Series.id)
            .filter(
                Series.id != sid,
                Series.tvdbid == tvdbid,
                Series.is_deleted == False,  # noqa: E712
                Series.instance_key.in_(tuple(allowed)),
            )
            .all()
        )
    ]
    if not sibling_series_ids:
        return paths

    protected: set[str] = set()
    q = (
        session.query(Placeholder.path)
        .join(Episode, Placeholder.episode_id == Episode.id)
        .join(Season, Episode.season_id == Season.id)
        .filter(
            Season.series_id.in_(tuple(sibling_series_ids)),
            Placeholder.has_placeholder == True,  # noqa: E712
            Placeholder.path.isnot(None),
        )
    )
    for (p,) in q.all():
        if p:
            protected.add(_norm_abs(p))

    return [p for p in paths if p and _norm_abs(p) not in protected]
