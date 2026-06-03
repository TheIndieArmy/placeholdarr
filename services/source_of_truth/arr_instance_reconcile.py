"""Tombstone DB rows tied to ARR instance keys that are no longer configured."""

from __future__ import annotations

import json
import re
from typing import Any

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import Episode, Movie, Season, Series


def _normalize_instance_key(value: Any) -> str:
    key_raw = str(value or "").strip().lower()
    return re.sub(r"[^a-z0-9_-]+", "_", key_raw).strip("_-")


def _parse_allowed_keys(arr_instances_json: str) -> tuple[set[str], set[str]]:
    rad: set[str] = set()
    son: set[str] = set()
    raw = str(arr_instances_json or "").strip()
    if not raw:
        return rad, son
    try:
        payload = json.loads(raw)
    except Exception:
        return rad, son
    if not isinstance(payload, list):
        return rad, son
    for item in payload:
        if not isinstance(item, dict):
            continue
        arr_type = str(item.get("arr_type") or item.get("type") or "").strip().lower()
        k = _normalize_instance_key(item.get("instance_key") or item.get("key") or item.get("name") or "")
        if not k:
            continue
        if arr_type == "radarr":
            rad.add(k)
        elif arr_type == "sonarr":
            son.add(k)
    return rad, son


def tombstone_unconfigured_arr_rows(arr_instances_json: str) -> dict[str, Any]:
    """Mark movies/series (and children) deleted when their instance_key is not in the saved ARR config.

    When no Radarr (or Sonarr) rows exist in config, all corresponding DB rows are tombstoned.

    Returns stats plus ``movie_ids`` / ``episode_ids`` for scoped follow-up passes.
    """
    rad_keys, son_keys = _parse_allowed_keys(arr_instances_json)
    stats = {
        "movies_tombstoned": 0,
        "series_tombstoned": 0,
        "seasons_tombstoned": 0,
        "episodes_tombstoned": 0,
    }
    movie_ids: list[int] = []
    episode_ids: list[int] = []
    session = get_session()
    try:
        # --- Movies (Radarr) ---
        mq = session.query(Movie.id).filter(Movie.is_deleted == False)  # noqa: E712
        if rad_keys:
            mq = mq.filter(~Movie.instance_key.in_(tuple(rad_keys)))
        movie_ids = [int(r[0]) for r in mq.all()]
        if movie_ids:
            stats["movies_tombstoned"] = (
                session.query(Movie)
                .filter(Movie.id.in_(tuple(movie_ids)))
                .update({"is_deleted": True}, synchronize_session=False)
            )

        # --- Series (Sonarr) + seasons + episodes ---
        sq = session.query(Series.id).filter(Series.is_deleted == False)  # noqa: E712
        if son_keys:
            sq = sq.filter(~Series.instance_key.in_(tuple(son_keys)))
        series_ids = [int(r[0]) for r in sq.all()]
        if series_ids:
            episode_ids = [
                int(r[0])
                for r in (
                    session.query(Episode.id)
                    .join(Season, Episode.season_id == Season.id)
                    .filter(Season.series_id.in_(tuple(series_ids)), Episode.is_deleted == False)  # noqa: E712
                    .all()
                )
            ]
            stats["series_tombstoned"] = (
                session.query(Series)
                .filter(Series.id.in_(tuple(series_ids)))
                .update({"is_deleted": True}, synchronize_session=False)
            )
            stats["seasons_tombstoned"] = (
                session.query(Season)
                .filter(Season.series_id.in_(tuple(series_ids)))
                .update({"is_deleted": True}, synchronize_session=False)
            )
            season_ids = [int(r[0]) for r in session.query(Season.id).filter(Season.series_id.in_(tuple(series_ids))).all()]
            if season_ids:
                stats["episodes_tombstoned"] = (
                    session.query(Episode)
                    .filter(Episode.season_id.in_(tuple(season_ids)))
                    .update({"is_deleted": True}, synchronize_session=False)
                )

        from services.series_episode_stats_hooks import (
            bump_library_versions_after_bulk,
            refresh_series_stats_after_bulk,
        )

        if any(int(stats.get(k) or 0) for k in ("movies_tombstoned", "series_tombstoned", "seasons_tombstoned", "episodes_tombstoned")):
            refresh_series_stats_after_bulk(session, full=True)
            if int(stats.get("movies_tombstoned") or 0):
                bump_library_versions_after_bulk(session, movies=True, series=False)
        session.commit()
        if any(int(stats[k] or 0) for k in ("movies_tombstoned", "series_tombstoned", "seasons_tombstoned", "episodes_tombstoned")):
            logger.info(
                f"ARR instance reconcile (removed/unmapped keys): {stats}",
                extra={"emoji_type": "info"},
            )
        return {**stats, "movie_ids": movie_ids, "episode_ids": episode_ids}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reconcile_after_arr_settings_save(arr_instances_json: str) -> dict[str, Any]:
    """Tombstone detached rows, refresh determinations, then materialize obsolete placeholders."""
    from services.source_of_truth.determiner import run_determination_for_entities
    from services.source_of_truth.materializer import run_materialization_for_entities

    tomb = tombstone_unconfigured_arr_rows(arr_instances_json)
    movie_ids = list(tomb.get("movie_ids") or [])
    episode_ids = list(tomb.get("episode_ids") or [])
    if not movie_ids and not episode_ids:
        out = {k: v for k, v in tomb.items() if k in ("movies_tombstoned", "series_tombstoned", "seasons_tombstoned", "episodes_tombstoned")}
        return {"tombstone": out, "determination": {"skipped": True}, "materialization": {"skipped": True}}

    det = run_determination_for_entities(movie_ids=movie_ids, episode_ids=episode_ids)
    mat = run_materialization_for_entities(
        movie_ids=movie_ids,
        episode_ids=episode_ids,
        observation_source="arr_instance_detach",
    )
    out_stats = {k: v for k, v in tomb.items() if k in ("movies_tombstoned", "series_tombstoned", "seasons_tombstoned", "episodes_tombstoned")}
    return {"tombstone": out_stats, "determination": det, "materialization": mat}
