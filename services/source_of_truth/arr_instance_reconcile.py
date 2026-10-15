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
    """Primary keys and aliases count as configured (rename must not tombstone mid-flight)."""
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
        tokens = [k] if k else []
        for a in item.get("instance_key_aliases") or []:
            ak = _normalize_instance_key(a)
            if ak:
                tokens.append(ak)
        if not tokens:
            continue
        if arr_type == "radarr":
            rad.update(tokens)
        elif arr_type == "sonarr":
            son.update(tokens)
    return rad, son


def _parse_configured_instance_ids(arr_instances_json: str) -> set[str]:
    ids: set[str] = set()
    raw = str(arr_instances_json or "").strip()
    if not raw:
        return ids
    try:
        payload = json.loads(raw)
    except Exception:
        return ids
    if not isinstance(payload, list):
        return ids
    for item in payload:
        if not isinstance(item, dict):
            continue
        iid = str(item.get("instance_id") or "").strip().lower()
        if iid:
            ids.add(iid)
    return ids


def tombstone_rows_for_removed_instance_ids(removed_instance_ids: set[str] | list[str]) -> dict[str, Any]:
    """Tombstone catalog rows whose stable ``instance_id`` left the Arr config.

    Complements key-based tombstone: after remove/transplant the surviving slot
    may still alias an old key, but the removed server's ``instance_id`` must go.
    """
    removed = {str(x or "").strip().lower() for x in (removed_instance_ids or []) if str(x or "").strip()}
    empty = {
        "movies_tombstoned": 0,
        "series_tombstoned": 0,
        "seasons_tombstoned": 0,
        "episodes_tombstoned": 0,
        "movie_ids": [],
        "episode_ids": [],
    }
    if not removed:
        return empty

    stats = dict(empty)
    session = get_session()
    try:
        movie_ids = [
            int(r[0])
            for r in session.query(Movie.id)
            .filter(Movie.is_deleted == False, Movie.instance_id.in_(tuple(removed)))  # noqa: E712
            .all()
        ]
        if movie_ids:
            stats["movies_tombstoned"] = (
                session.query(Movie)
                .filter(Movie.id.in_(tuple(movie_ids)))
                .update({"is_deleted": True}, synchronize_session=False)
            )
            stats["movie_ids"] = movie_ids

        series_ids = [
            int(r[0])
            for r in session.query(Series.id)
            .filter(Series.is_deleted == False, Series.instance_id.in_(tuple(removed)))  # noqa: E712
            .all()
        ]
        episode_ids: list[int] = []
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
            stats["episode_ids"] = episode_ids

        if any(int(stats.get(k) or 0) for k in ("movies_tombstoned", "series_tombstoned", "seasons_tombstoned", "episodes_tombstoned")):
            from services.series_episode_stats_hooks import bump_library_versions_after_bulk

            bump_library_versions_after_bulk(
                session,
                movies=bool(stats["movies_tombstoned"]),
                series=bool(stats["series_tombstoned"] or stats["seasons_tombstoned"] or stats["episodes_tombstoned"]),
            )
            logger.info(
                f"ARR instance_id reconcile (removed ids): removed={sorted(removed)} stats={stats}",
                extra={"emoji_type": "info"},
            )
        session.commit()
        return stats
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


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

        from services.series_episode_stats_hooks import bump_library_versions_after_bulk

        if any(int(stats.get(k) or 0) for k in ("movies_tombstoned", "series_tombstoned", "seasons_tombstoned", "episodes_tombstoned")):
            bump_library_versions_after_bulk(
                session,
                movies=bool(int(stats.get("movies_tombstoned") or 0)),
                series=bool(
                    int(stats.get("series_tombstoned") or 0)
                    or int(stats.get("seasons_tombstoned") or 0)
                    or int(stats.get("episodes_tombstoned") or 0)
                ),
            )
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


def reconcile_after_arr_settings_save(
    arr_instances_json: str,
    *,
    extra_movie_ids: list[int] | None = None,
    extra_episode_ids: list[int] | None = None,
    previous_arr_instances_json: str | None = None,
) -> dict[str, Any]:
    """Tombstone detached rows, then enqueue determination + placeholder cleanup.

    Determination and materialization run as ``entity_materialization`` so Settings
    save is not blocked on large detach sets (tens of thousands of episodes).
    """
    from services.source_of_truth.entity_materialization_job import (
        enqueue_entity_materialization_job,
    )

    id_tomb: dict[str, Any] = {
        "movies_tombstoned": 0,
        "series_tombstoned": 0,
        "seasons_tombstoned": 0,
        "episodes_tombstoned": 0,
        "movie_ids": [],
        "episode_ids": [],
    }
    if previous_arr_instances_json is not None:
        prev_ids = _parse_configured_instance_ids(previous_arr_instances_json)
        new_ids = _parse_configured_instance_ids(arr_instances_json)
        removed_ids = prev_ids - new_ids
        if removed_ids:
            id_tomb = tombstone_rows_for_removed_instance_ids(removed_ids)

    tomb = tombstone_unconfigured_arr_rows(arr_instances_json)
    movie_ids = list(tomb.get("movie_ids") or [])
    episode_ids = list(tomb.get("episode_ids") or [])
    for mid in list(id_tomb.get("movie_ids") or []) + list(extra_movie_ids or []):
        try:
            mid_i = int(mid)
        except Exception:
            continue
        if mid_i not in movie_ids:
            movie_ids.append(mid_i)
    for eid in list(id_tomb.get("episode_ids") or []) + list(extra_episode_ids or []):
        try:
            eid_i = int(eid)
        except Exception:
            continue
        if eid_i not in episode_ids:
            episode_ids.append(eid_i)
    if not movie_ids and not episode_ids:
        out = {k: v for k, v in tomb.items() if k in ("movies_tombstoned", "series_tombstoned", "seasons_tombstoned", "episodes_tombstoned")}
        out_ids = {
            k: int(id_tomb.get(k) or 0)
            for k in ("movies_tombstoned", "series_tombstoned", "seasons_tombstoned", "episodes_tombstoned")
        }
        return {
            "tombstone": out,
            "tombstone_by_instance_id": out_ids,
            "determination": {"skipped": True},
            "materialization": {"skipped": True},
        }

    det: dict[str, Any] = {"enqueued": True}
    mat: dict[str, Any] = {"enqueued": False, "job_id": None}
    session = get_session()
    try:
        job = enqueue_entity_materialization_job(
            session,
            movie_ids=movie_ids,
            episode_ids=episode_ids,
            observation_source="arr_instance_detach",
            payload_extras={"run_determination_first": True},
        )
        session.commit()
        mat = {"enqueued": True, "job_id": int(job.id) if job.id is not None else None}
        logger.info(
            f"ARR instance detach: determination+materialization enqueued job_id={mat.get('job_id')} "
            f"movies={len(movie_ids)} episodes={len(episode_ids)}",
            extra={"emoji_type": "processing"},
        )
    except Exception as exc:
        session.rollback()
        logger.error(
            f"ARR instance detach: failed to enqueue determination+materialization: {exc}",
            extra={"emoji_type": "error"},
        )
        det = {"enqueued": False, "error": str(exc)}
        mat = {"enqueued": False, "error": str(exc)}
    finally:
        session.close()

    out_stats = {k: v for k, v in tomb.items() if k in ("movies_tombstoned", "series_tombstoned", "seasons_tombstoned", "episodes_tombstoned")}
    out_ids = {
        k: int(id_tomb.get(k) or 0)
        for k in ("movies_tombstoned", "series_tombstoned", "seasons_tombstoned", "episodes_tombstoned")
    }
    return {
        "tombstone": out_stats,
        "tombstone_by_instance_id": out_ids,
        "determination": det,
        "materialization": mat,
    }
