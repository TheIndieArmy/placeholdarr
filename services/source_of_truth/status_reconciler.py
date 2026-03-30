from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from core.config import settings
from core.logger import logger
from services.media_servers.emby import refresh_emby_item_metadata
from services.media_servers.jellyfin import refresh_jellyfin_item_metadata
from services.placeholders import ensure_episode_nfo, ensure_movie_nfo, ensure_series_nfo
from services.media_servers.plex_status_writer import batch_update_plex_statuses
from services.postgres.db import get_session
from services.postgres.models import Episode, Movie, Placeholder, Job, Season, Series
from services.source_of_truth.media_observation import (
    _resolve_emby_episode_id,
    _resolve_emby_movie_id,
    _resolve_jellyfin_episode_id,
    _resolve_jellyfin_movie_id,
)


STATUS_PROJECTION_JOB_TYPE = "status_projection"
NFO_REFRESH_JOB_TYPE = "nfo_refresh"


def _status_updates_enabled() -> bool:
    """Check if status updates are enabled via ENV."""
    mode = str(getattr(settings, "PLACEHOLDER_STATUS_UPDATES", "ALL") or "ALL").strip().upper()
    return mode in {"REQUEST", "ALL"}


def _projection_mode() -> str:
    """Get current projection mode (summary, title, both)."""
    mode = str(getattr(settings, "PLACEHOLDER_STATUS_PROJECTION_MODE", "summary") or "summary").strip().lower()
    if mode == "off":
        mode = "summary"
    return mode if mode in {"summary", "title", "both"} else "summary"


def _placeholder_display_status(placeholder: Placeholder) -> str | None:
    status = getattr(placeholder, "display_status", None)
    if isinstance(status, str):
        status = status.strip() or None
    reason = getattr(placeholder, "display_reason", None)
    if isinstance(reason, str):
        reason = reason.strip() or None

    if status in {
        "COMING_SOON",
        "COMING_SOON_30",
        "COMING_SOON_14",
        "COMING_SOON_7",
        "COMING_SOON_1",
        "COMING_SOON_TODAY",
    } and reason:
        # Keep canonical status in DB while projecting a human-friendly daily label.
        return reason

    return status


def _projection_intents_for_placeholders(session, placeholders: list[Placeholder]) -> list[dict]:
    intents = []
    updates_enabled = _status_updates_enabled()
    projection_mode = _projection_mode()

    for placeholder in placeholders:
        movie = session.query(Movie).get(placeholder.movie_id) if placeholder.movie_id else None
        episode = session.query(Episode).get(placeholder.episode_id) if placeholder.episode_id else None
        if not movie and not episode:
            continue

        placeholder_status = _placeholder_display_status(placeholder)
        desired_status = placeholder_status if updates_enabled else None

        if desired_status or placeholder_status:
            intents.append(
                {
                    "entity_type": Movie if movie else Episode,
                    "entity_id": int(movie.id) if movie else int(episode.id),
                    "status": desired_status,
                    "projection_mode": projection_mode if updates_enabled else "off",
                }
            )

    return intents


def run_status_projection_reconciliation() -> dict:
    """Reconcile active placeholders against current status projection settings."""
    stats = {"reconciled": 0, "unchanged": 0, "skipped": 0, "errors": 0, "scanned": 0, "error_breakdown": {}}
    session = None

    try:
        session = get_session()
        placeholders = session.query(Placeholder).filter(Placeholder.has_placeholder == True).all()  # noqa: E712
        if not placeholders:
            logger.info("Status projection reconciliation: no active placeholders", extra={"emoji_type": "info"})
            return stats

        stats["scanned"] = len(placeholders)

        intents = _projection_intents_for_placeholders(session, placeholders)

        if intents and getattr(settings, "ENABLE_PLEX", False):
            result = batch_update_plex_statuses(session, intents)
            stats["reconciled"] += int(result.get("status_updates", 0) or 0)
            stats["unchanged"] += int(result.get("unchanged", 0) or 0)
            stats["skipped"] += int(result.get("skipped", 0) or 0)
            stats["errors"] += int(result.get("errors", 0) or 0)

            details = result.get("details", []) if isinstance(result, dict) else []
            error_reasons = [
                detail.get("result")
                for detail in details
                if isinstance(detail, dict)
                and str(detail.get("result") or "").lower() not in {"updated", "unchanged"}
                and not str(detail.get("result") or "").lower().startswith("skipped_")
            ]
            stats["error_breakdown"] = dict(Counter(error_reasons).most_common(8)) if error_reasons else {}

            logger.info(
                "Plex status reconciliation complete: "
                f"reconciled={result.get('status_updates', 0)}, "
                f"unchanged={result.get('unchanged', 0)}, "
                f"skipped={result.get('skipped', 0)}, "
                f"errors={result.get('errors', 0)}"
                + (
                    f", error_breakdown={stats['error_breakdown']}"
                    if stats["error_breakdown"]
                    else ""
                ),
                extra={"emoji_type": "info"},
            )
    except Exception as ex:
        logger.error(f"Error in run_status_projection_reconciliation: {ex}", extra={"emoji_type": "error"})
        stats["errors"] += 1
    finally:
        if session is not None:
            session.close()

    return stats


def process_status_projection_job(session, job: Job) -> dict:
    """Project statuses for a scoped set of placeholders from a durable job payload."""
    payload = job.payload if isinstance(job.payload, dict) else {}
    placeholder_ids = payload.get("placeholder_ids") or []
    ids = [int(pid) for pid in placeholder_ids if pid is not None]
    if not ids:
        return {"ok": False, "reason": "no_placeholder_ids"}

    placeholders = (
        session.query(Placeholder)
        .filter(Placeholder.id.in_(ids), Placeholder.has_placeholder == True)  # noqa: E712
        .all()
    )

    intents = _projection_intents_for_placeholders(session, placeholders)
    if not intents or not getattr(settings, "ENABLE_PLEX", False):
        return {"ok": True, "projected": 0, "scanned": len(placeholders), "reason": "nothing_to_project"}

    result = batch_update_plex_statuses(session, intents)
    return {
        "ok": True,
        "projected": int(result.get("status_updates", 0) or 0),
        "unchanged": int(result.get("unchanged", 0) or 0),
        "skipped": int(result.get("skipped", 0) or 0),
        "errors": int(result.get("errors", 0) or 0),
        "scanned": len(placeholders),
    }


def _refresh_movie_nfo(placeholder: Placeholder, movie: Movie) -> bool:
    target_path = str(getattr(placeholder, "path", "") or getattr(movie, "placeholder_filepath", "") or "").strip()
    if not target_path:
        return False
    setattr(movie, "placeholder_status", _placeholder_display_status(placeholder) or "REQUEST")
    return ensure_movie_nfo(target_path, movie)


def _refresh_episode_nfo(session, placeholder: Placeholder, episode: Episode) -> bool:
    season = session.query(Season).get(episode.season_id) if episode.season_id else None
    series = session.query(Series).get(season.series_id) if season and season.series_id else None
    if not season or not series:
        return False

    target_path = str(getattr(placeholder, "path", "") or getattr(episode, "placeholder_filepath", "") or "").strip()
    if not target_path:
        return False

    status = _placeholder_display_status(placeholder) or "REQUEST"
    setattr(episode, "placeholder_status", status)
    setattr(series, "placeholder_status", status)
    episode_written = ensure_episode_nfo(target_path, episode, season, series)
    series_written = ensure_series_nfo(series, folder=getattr(series, "placeholder_folder", None))
    return bool(episode_written or series_written)


def _resolve_item_ids_for_remote_refresh(session, placeholder: Placeholder, movie: Movie | None, episode: Episode | None) -> dict[str, str]:
    ids: dict[str, str] = {}

    jf_id = str(getattr(placeholder, "jellyfin_placeholder_id", "") or "").strip()
    emby_id = str(getattr(placeholder, "emby_placeholder_id", "") or "").strip()

    season = None
    series = None
    if episode is not None:
        season = session.query(Season).get(episode.season_id) if episode.season_id else None
        series = session.query(Series).get(season.series_id) if season and season.series_id else None

    if not jf_id and getattr(settings, "ENABLE_JELLYFIN", False):
        try:
            if movie is not None:
                jf_id = str(_resolve_jellyfin_movie_id(movie) or "").strip()
            elif episode is not None and season and series:
                jf_id = str(_resolve_jellyfin_episode_id(episode, season, series) or "").strip()
        except Exception:
            jf_id = ""
        if jf_id:
            placeholder.jellyfin_placeholder_id = jf_id
            if not getattr(placeholder, "jellyfin_id_observed_at", None):
                placeholder.jellyfin_id_observed_at = datetime.now(timezone.utc)

    if not emby_id and getattr(settings, "ENABLE_EMBY", False):
        try:
            if movie is not None:
                emby_id = str(_resolve_emby_movie_id(movie) or "").strip()
            elif episode is not None and season and series:
                emby_id = str(_resolve_emby_episode_id(episode, season, series) or "").strip()
        except Exception:
            emby_id = ""
        if emby_id:
            placeholder.emby_placeholder_id = emby_id
            if not getattr(placeholder, "emby_id_observed_at", None):
                placeholder.emby_id_observed_at = datetime.now(timezone.utc)

    if jf_id:
        ids["jellyfin"] = jf_id
    if emby_id:
        ids["emby"] = emby_id

    return ids


def _refresh_remote_item_metadata(session, placeholder: Placeholder, movie: Movie | None, episode: Episode | None) -> tuple[int, int]:
    ids = _resolve_item_ids_for_remote_refresh(session, placeholder, movie, episode)

    refreshed = 0
    failed = 0

    jf_id = ids.get("jellyfin")
    if jf_id and getattr(settings, "ENABLE_JELLYFIN", False):
        if refresh_jellyfin_item_metadata(jf_id):
            refreshed += 1
        else:
            failed += 1

    emby_id = ids.get("emby")
    if emby_id and getattr(settings, "ENABLE_EMBY", False):
        if refresh_emby_item_metadata(emby_id):
            refreshed += 1
        else:
            failed += 1

    return refreshed, failed


def process_nfo_refresh_job(session, job: Job) -> dict:
    """Refresh placeholder sidecar NFO files for a scoped set of placeholders."""
    payload = job.payload if isinstance(job.payload, dict) else {}
    placeholder_ids = payload.get("placeholder_ids") or []
    ids = [int(pid) for pid in placeholder_ids if pid is not None]
    if not ids:
        return {"ok": False, "reason": "no_placeholder_ids"}

    placeholders = session.query(Placeholder).filter(Placeholder.id.in_(ids)).all()
    refreshed = 0
    remote_refreshed = 0
    remote_failed = 0

    for placeholder in placeholders:
        if not getattr(placeholder, "has_placeholder", False):
            continue

        movie = session.query(Movie).get(placeholder.movie_id) if placeholder.movie_id else None
        episode = session.query(Episode).get(placeholder.episode_id) if placeholder.episode_id else None
        if movie and _refresh_movie_nfo(placeholder, movie):
            refreshed += 1
            rr, rf = _refresh_remote_item_metadata(session, placeholder, movie, None)
            remote_refreshed += rr
            remote_failed += rf
            continue
        if episode and _refresh_episode_nfo(session, placeholder, episode):
            refreshed += 1
            rr, rf = _refresh_remote_item_metadata(session, placeholder, None, episode)
            remote_refreshed += rr
            remote_failed += rf

    return {
        "ok": True,
        "refreshed": refreshed,
        "scanned": len(placeholders),
        "remote_refreshed": remote_refreshed,
        "remote_refresh_failed": remote_failed,
    }


def enqueue_status_projection(placeholder_ids: list[int], session=None) -> dict:
    """
    Enqueue a Job to project placeholder statuses to media servers.
    
    This is called by StatusOrchestrator.apply_and_project_statuses() to trigger
    downstream status projection after DB writes.
    
    Args:
        placeholder_ids: List of Placeholder IDs to project
        session: Optional DB session (creates one if not provided)
    
    Returns:
        Dict with {ok, job_id} or {ok: False, reason}
    """
    owns_session = session is None
    session = session or get_session()
    
    try:
        if not placeholder_ids:
            return {"ok": False, "reason": "no_placeholder_ids"}
        
        # Create a job to project these statuses
        job = Job(
            job_type=STATUS_PROJECTION_JOB_TYPE,
            payload={"placeholder_ids": placeholder_ids},
            status="PENDING",
            run_after=datetime.now(timezone.utc),
        )
        session.add(job)
        session.commit()
        
        logger.debug(
            f"Enqueued status projection job: job_id={job.id}, "
            f"placeholder_ids={len(placeholder_ids)}"
        )
        
        return {"ok": True, "job_id": job.id}
    
    except Exception as e:
        logger.error(f"Failed to enqueue status projection job: {e}", exc_info=True)
        session.rollback()
        return {"ok": False, "reason": str(e)}
    
    finally:
        if owns_session:
            session.close()


def enqueue_nfo_refresh(placeholder_ids: list[int], session=None) -> dict:
    """Enqueue a durable NFO refresh job for placeholders whose projected status changed."""
    owns_session = session is None
    session = session or get_session()

    try:
        if not placeholder_ids:
            return {"ok": False, "reason": "no_placeholder_ids"}

        job = Job(
            job_type=NFO_REFRESH_JOB_TYPE,
            payload={"placeholder_ids": placeholder_ids},
            status="PENDING",
            run_after=datetime.now(timezone.utc),
        )
        session.add(job)
        session.commit()
        logger.debug(
            f"Enqueued NFO refresh job: job_id={job.id}, placeholder_ids={len(placeholder_ids)}"
        )
        return {"ok": True, "job_id": job.id}
    except Exception as e:
        logger.error(f"Failed to enqueue NFO refresh job: {e}", exc_info=True)
        session.rollback()
        return {"ok": False, "reason": str(e)}
    finally:
        if owns_session:
            session.close()

