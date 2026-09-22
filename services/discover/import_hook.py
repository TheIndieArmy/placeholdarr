"""Clear Discover placeholders when Radarr reports a movie import."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.logger import logger
from services.discover.determine import run_discover_determination
from services.discover.materialize import apply_tmdb_movie_materialization
from services.discover.mode import is_tmdb_discover_mode
from services.postgres.db import get_session
from services.postgres.models import ArrMovieOverlay, TmdbMovie


def _tmdb_id_from_payload(payload: dict[str, Any]) -> int | None:
    movie = payload.get("movie") if isinstance(payload.get("movie"), dict) else {}
    raw = movie.get("tmdbId") or movie.get("tmdb_id") or payload.get("tmdbId")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def apply_discover_movie_import(
    payload: dict[str, Any],
    *,
    instance_key: str | None = None,
    instance_id: str | None = None,
    radarr_id: int | None = None,
) -> dict[str, Any] | None:
    """Update Arr overlay has_file and remove Discover placeholders for the TMDB id."""
    if not is_tmdb_discover_mode():
        return None
    tmdb_id = _tmdb_id_from_payload(payload)
    if tmdb_id is None:
        return {"ok": False, "reason": "missing_tmdb_id"}

    key = str(instance_key or "radarr_std").strip() or "radarr_std"
    iid = str(instance_id or "").strip() or f"radarr:{key}"
    session = get_session()
    try:
        row = session.get(TmdbMovie, tmdb_id)
        if row is None:
            return {"ok": False, "reason": "tmdb_movie_missing", "tmdb_id": tmdb_id}

        overlay = (
            session.query(ArrMovieOverlay)
            .filter(ArrMovieOverlay.tmdb_id == tmdb_id, ArrMovieOverlay.instance_id == iid)
            .first()
        )
        if overlay is None:
            # Prefer matching an existing overlay for this key if instance_id formats differ
            overlay = (
                session.query(ArrMovieOverlay)
                .filter(ArrMovieOverlay.tmdb_id == tmdb_id, ArrMovieOverlay.instance_key == key)
                .first()
            )
        if overlay is None:
            overlay = ArrMovieOverlay(tmdb_id=tmdb_id, instance_id=iid, instance_key=key)
            session.add(overlay)
        overlay.instance_key = key
        overlay.has_file = True
        overlay.monitored = True
        if radarr_id is not None:
            overlay.radarr_id = int(radarr_id)
        overlay.updated_at = datetime.now(timezone.utc)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    det = run_discover_determination(tmdb_ids=[tmdb_id])
    mat = apply_tmdb_movie_materialization(tmdb_id)
    logger.info(
        f"Discover import hook tmdb={tmdb_id} det={det} mat={mat}",
        extra={"emoji_type": "success"},
    )
    return {"ok": True, "tmdb_id": tmdb_id, "determination": det, "materialization": mat}
