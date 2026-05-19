"""Guard disk cleanup when the same logical title exists on another active ARR instance."""

from __future__ import annotations

import os
import re
from typing import Any

from core.config import settings
from services.postgres.models import Episode, Movie, Placeholder, Season, Series

SHARED_PLACEHOLDER_CLEANUP_PROTECT_SIBLINGS = "protect_siblings"
SHARED_PLACEHOLDER_CLEANUP_ANY_INSTANCE_HAS_FILE = "any_instance_has_file"


def _normalize_cleanup_mode(raw: str) -> str:
    value = str(raw or SHARED_PLACEHOLDER_CLEANUP_PROTECT_SIBLINGS).strip().lower()
    if value in {
        SHARED_PLACEHOLDER_CLEANUP_ANY_INSTANCE_HAS_FILE,
        "any_instance",
        "aggressive",
        "single_instance",
    }:
        return SHARED_PLACEHOLDER_CLEANUP_ANY_INSTANCE_HAS_FILE
    return SHARED_PLACEHOLDER_CLEANUP_PROTECT_SIBLINGS


def _legacy_shared_placeholder_cleanup_mode() -> str | None:
    legacy = getattr(settings, "MULTI_INSTANCE_SHARED_PLACEHOLDER_CLEANUP", None)
    if legacy is None or str(legacy).strip() == "":
        return None
    return _normalize_cleanup_mode(str(legacy))


def shared_placeholder_disk_cleanup_mode(arr_type: str) -> str:
    """How on-disk placeholder files are removed when multiple ARR instances share paths."""
    want = str(arr_type or "").strip().lower()
    if want == "radarr":
        raw = getattr(settings, "RADARR_SHARED_PLACEHOLDER_CLEANUP", None)
    elif want == "sonarr":
        raw = getattr(settings, "SONARR_SHARED_PLACEHOLDER_CLEANUP", None)
    else:
        raw = None
    if raw is None or str(raw).strip() == "":
        legacy = _legacy_shared_placeholder_cleanup_mode()
        if legacy:
            return legacy
        return SHARED_PLACEHOLDER_CLEANUP_PROTECT_SIBLINGS
    return _normalize_cleanup_mode(str(raw))


def protect_shared_placeholder_disk_paths(arr_type: str) -> bool:
    """When True, skip deleting paths still referenced by a sibling instance (default)."""
    return shared_placeholder_disk_cleanup_mode(arr_type) == SHARED_PLACEHOLDER_CLEANUP_PROTECT_SIBLINGS


def shared_placeholder_suppresses_creation(arr_type: str) -> bool:
    """When True, siblings with has_file suppress placeholder creation on this row."""
    return shared_placeholder_disk_cleanup_mode(arr_type) == SHARED_PLACEHOLDER_CLEANUP_ANY_INSTANCE_HAS_FILE


def sibling_movie_has_file(session, movie: Movie) -> bool:
    """True when another configured Radarr row shares TMDB id and has a real file."""
    tmdbid = int(getattr(movie, "tmdbid", None) or 0)
    if not tmdbid:
        return False
    allowed = configured_instance_keys("radarr")
    if not allowed:
        return False
    mid = int(getattr(movie, "id", 0) or 0)
    row = (
        session.query(Movie.id)
        .filter(
            Movie.id != mid,
            Movie.tmdbid == tmdbid,
            Movie.is_deleted == False,  # noqa: E712
            Movie.has_file == True,  # noqa: E712
            Movie.instance_key.in_(tuple(allowed)),
        )
        .first()
    )
    return bool(row)


def sibling_movie_ids(session, movie: Movie) -> list[int]:
    """Other configured movie row ids with the same TMDB id (for scoped re-determination)."""
    tmdbid = int(getattr(movie, "tmdbid", None) or 0)
    if not tmdbid:
        return []
    allowed = configured_instance_keys("radarr")
    if not allowed:
        return []
    mid = int(getattr(movie, "id", 0) or 0)
    return [
        int(r[0])
        for r in session.query(Movie.id)
        .filter(
            Movie.id != mid,
            Movie.tmdbid == tmdbid,
            Movie.is_deleted == False,  # noqa: E712
            Movie.instance_key.in_(tuple(allowed)),
        )
        .all()
    ]


def _episode_catalog_identity(session, episode: Episode) -> tuple[int, int, int] | None:
    row = (
        session.query(Series.tvdbid, Season.season_number, Episode.episode_number)
        .join(Season, Episode.season_id == Season.id)
        .join(Series, Season.series_id == Series.id)
        .filter(Episode.id == int(episode.id))
        .first()
    )
    if not row or not row[0]:
        return None
    return int(row[0]), int(row[1] or 0), int(row[2] or 0)


def sibling_episode_has_file(session, episode: Episode) -> bool:
    """True when another configured Sonarr row shares TVDB+S+E and has a real file."""
    ident = _episode_catalog_identity(session, episode)
    if not ident:
        return False
    tvdbid, season_number, episode_number = ident
    allowed = configured_instance_keys("sonarr")
    if not allowed:
        return False
    eid = int(episode.id)
    row = (
        session.query(Episode.id)
        .join(Season, Episode.season_id == Season.id)
        .join(Series, Season.series_id == Series.id)
        .filter(
            Episode.id != eid,
            Series.tvdbid == tvdbid,
            Season.season_number == season_number,
            Episode.episode_number == episode_number,
            Episode.is_deleted == False,  # noqa: E712
            Episode.has_file == True,  # noqa: E712
            Series.is_deleted == False,  # noqa: E712
            Series.instance_key.in_(tuple(allowed)),
        )
        .first()
    )
    return bool(row)


def sibling_episode_ids(session, episode: Episode) -> list[int]:
    """Other configured episode row ids with the same TVDB+S+E (for scoped re-determination)."""
    ident = _episode_catalog_identity(session, episode)
    if not ident:
        return []
    tvdbid, season_number, episode_number = ident
    allowed = configured_instance_keys("sonarr")
    if not allowed:
        return []
    eid = int(episode.id)
    return [
        int(r[0])
        for r in session.query(Episode.id)
        .join(Season, Episode.season_id == Season.id)
        .join(Series, Season.series_id == Series.id)
        .filter(
            Episode.id != eid,
            Series.tvdbid == tvdbid,
            Season.season_number == season_number,
            Episode.episode_number == episode_number,
            Episode.is_deleted == False,  # noqa: E712
            Series.is_deleted == False,  # noqa: E712
            Series.instance_key.in_(tuple(allowed)),
        )
        .all()
    ]


def expand_determination_entity_ids(
    session,
    *,
    movie_ids: list[int] | None = None,
    episode_ids: list[int] | None = None,
) -> tuple[list[int], list[int]]:
    """Expand scoped entity ids to include catalog siblings on other configured instances."""
    mids: set[int] = {int(mid) for mid in (movie_ids or []) if mid is not None}
    eids: set[int] = {int(eid) for eid in (episode_ids or []) if eid is not None}

    for mid in list(mids):
        movie = session.query(Movie).filter(Movie.id == mid).first()
        if movie:
            mids.update(sibling_movie_ids(session, movie))

    for eid in list(eids):
        episode = session.query(Episode).filter(Episode.id == eid).first()
        if episode:
            eids.update(sibling_episode_ids(session, episode))

    return sorted(mids), sorted(eids)


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
    if not protect_shared_placeholder_disk_paths("radarr"):
        return paths
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
    if not protect_shared_placeholder_disk_paths("sonarr"):
        return paths
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
