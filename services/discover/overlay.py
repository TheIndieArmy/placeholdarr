"""Bulk Radarr overlay for TMDB catalog movies (monitored / hasFile / radarr id)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import ArrMovieOverlay, TmdbMovie
from services.source_of_truth.arr_api import fetch_radarr_movies


def _radarr_instances() -> list[dict[str, Any]]:
    try:
        from core.config import settings

        return [
            i
            for i in (settings.configured_arr_instances or [])
            if str(i.get("arr_type") or "").lower() == "radarr"
        ]
    except Exception as exc:
        logger.warning(f"Discover overlay: could not list Radarr instances: {exc}", extra={"emoji_type": "warning"})
        return []


def _instance_meta(inst: Any) -> tuple[str, str, str | None, str | None]:
    if isinstance(inst, dict):
        key = str(inst.get("instance_key") or inst.get("key") or "radarr_std")
        iid = str(inst.get("instance_id") or f"radarr:{key}")
        url = inst.get("url") or inst.get("base_url")
        api_key = inst.get("api_key") or inst.get("apikey")
        return iid, key, url, api_key
    key = str(getattr(inst, "instance_key", None) or "radarr_std")
    iid = str(getattr(inst, "instance_id", None) or f"radarr:{key}")
    url = getattr(inst, "url", None) or getattr(inst, "base_url", None)
    api_key = getattr(inst, "api_key", None) or getattr(inst, "apikey", None)
    return iid, key, url, api_key


def refresh_arr_overlays(*, session=None) -> dict[str, Any]:
    """Pull Radarr library state into arr_movie_overlay.

    Catalog ids are loaded first, then each Radarr HTTP call runs without holding
    that DB session open (avoids long idle-in-transaction during Arr fetch).

    Returns ``changed_tmdb_ids`` for titles whose monitored/has_file presence
    actually flipped (including new overlays and cleared stale rows). Downstream
    determine/materialize should prefer that set over re-walking the whole catalog.
    """
    del session  # always use short-lived sessions
    changed_tmdb_ids: set[int] = set()
    stats: dict[str, Any] = {
        "instances": 0,
        "rows": 0,
        "matched": 0,
        "changed": 0,
        "errors": [],
        "changed_tmdb_ids": [],
    }

    session = get_session()
    try:
        catalog_ids = {int(r[0]) for r in session.query(TmdbMovie.tmdb_id).all()}
    finally:
        session.close()

    if not catalog_ids:
        return stats

    instances = _radarr_instances()
    logger.info(
        f"Discover overlay: {len(catalog_ids)} catalog id(s), {len(instances)} Radarr instance(s)",
        extra={"emoji_type": "info"},
    )

    for inst in instances:
        stats["instances"] += 1
        iid, key, url, api_key = _instance_meta(inst)
        logger.info(f"Discover overlay: fetching Radarr movies ({key})…", extra={"emoji_type": "gear"})
        try:
            movies = fetch_radarr_movies(url=url, api_key=api_key, bypass_cache=True) or []
        except Exception as exc:
            stats["errors"].append({"instance": key, "error": str(exc)})
            logger.warning(f"Discover overlay: Radarr {key} failed: {exc}", extra={"emoji_type": "warning"})
            continue
        logger.info(
            f"Discover overlay: Radarr {key} returned {len(movies)} movie(s); writing matches",
            extra={"emoji_type": "info"},
        )

        session = get_session()
        try:
            seen_tmdb: set[int] = set()
            for entry in movies:
                if not isinstance(entry, dict):
                    continue
                tmdb_raw = entry.get("tmdbId") or entry.get("tmdb_id")
                if tmdb_raw is None:
                    continue
                try:
                    tmdb_id = int(tmdb_raw)
                except (TypeError, ValueError):
                    continue
                if tmdb_id not in catalog_ids:
                    continue
                seen_tmdb.add(tmdb_id)
                has_file = bool(entry.get("hasFile") or (entry.get("movieFile") or {}).get("path"))
                monitored = bool(entry.get("monitored"))
                radarr_id = entry.get("id")
                path = None
                mf = entry.get("movieFile") or {}
                if isinstance(mf, dict):
                    path = mf.get("path")
                row = (
                    session.query(ArrMovieOverlay)
                    .filter(ArrMovieOverlay.tmdb_id == tmdb_id, ArrMovieOverlay.instance_id == iid)
                    .first()
                )
                if row is None:
                    row = ArrMovieOverlay(tmdb_id=tmdb_id, instance_id=iid, instance_key=key)
                    session.add(row)
                    changed_tmdb_ids.add(tmdb_id)
                else:
                    prev_monitored = bool(row.monitored)
                    prev_has_file = bool(row.has_file)
                    if prev_monitored != monitored or prev_has_file != has_file:
                        changed_tmdb_ids.add(tmdb_id)
                row.instance_key = key
                row.radarr_id = int(radarr_id) if radarr_id is not None else row.radarr_id
                row.monitored = monitored
                row.has_file = has_file
                row.radarr_filepath = path or row.radarr_filepath
                row.updated_at = datetime.now(timezone.utc)
                stats["matched"] += 1
            # Overlays for this instance whose TMDB id vanished from Arr: clear monitored/has_file
            stale = (
                session.query(ArrMovieOverlay)
                .filter(ArrMovieOverlay.instance_id == iid, ArrMovieOverlay.tmdb_id.in_(list(catalog_ids)))
                .all()
            )
            for row in stale:
                if row.tmdb_id not in seen_tmdb:
                    if bool(row.monitored) or bool(row.has_file) or row.radarr_id is not None:
                        changed_tmdb_ids.add(int(row.tmdb_id))
                    row.monitored = False
                    row.has_file = False
                    row.radarr_id = None
                    row.updated_at = datetime.now(timezone.utc)
            stats["rows"] += len(movies)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    stats["changed"] = len(changed_tmdb_ids)
    stats["changed_tmdb_ids"] = sorted(changed_tmdb_ids)
    logger.info(
        f"Discover overlay: {stats['changed']} title(s) with monitored/hasFile changes",
        extra={"emoji_type": "info"},
    )
    return stats
