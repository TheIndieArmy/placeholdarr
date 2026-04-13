from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timezone, timedelta

from core.config import settings
from core.logger import logger
from services.media_servers.emby import refresh_emby_item_metadata, refresh_emby_paths
from services.media_servers.jellyfin import refresh_jellyfin_item_metadata, refresh_jellyfin_paths
from services.placeholders import ensure_episode_nfo, ensure_movie_nfo, ensure_series_nfo
from services.postgres.db import get_session
from services.postgres.models import Episode, Movie, Placeholder, Job, Season, Series
from services.media_servers.refresh import refresh_all_path_batches_with_section_fallback


NFO_REFRESH_JOB_TYPE = "nfo_refresh"


def _job_batch_size() -> int:
    return max(1, int(getattr(settings, "STATUS_JOB_BATCH_SIZE", 250) or 250))


def _job_debounce_seconds() -> float:
    return max(0.0, float(getattr(settings, "STATUS_JOB_DEBOUNCE_SECONDS", 0.5) or 0.5))


def _normalize_placeholder_ids(placeholder_ids: list[int] | tuple[int, ...] | None) -> list[int]:
    seen: set[int] = set()
    normalized: list[int] = []
    for value in placeholder_ids or []:
        if value is None:
            continue
        pid = int(value)
        if pid in seen:
            continue
        seen.add(pid)
        normalized.append(pid)
    return normalized


def _job_placeholder_ids(job: Job) -> list[int]:
    payload = job.payload if isinstance(job.payload, dict) else {}
    return _normalize_placeholder_ids(payload.get("placeholder_ids") or [])


def _set_job_placeholder_ids(job: Job, placeholder_ids: list[int]) -> None:
    payload = dict(job.payload or {}) if isinstance(job.payload, dict) else {}
    payload["placeholder_ids"] = placeholder_ids
    job.payload = payload


def _enqueue_batched_placeholder_job(job_type: str, placeholder_ids: list[int], session) -> dict:
    ids_remaining = _normalize_placeholder_ids(placeholder_ids)
    if not ids_remaining:
        return {"ok": False, "reason": "no_placeholder_ids"}

    batch_size = _job_batch_size()
    debounce_seconds = _job_debounce_seconds()
    now = datetime.now(timezone.utc)
    run_after = now if debounce_seconds <= 0 else now + timedelta(seconds=debounce_seconds)

    touched_job_ids: list[int] = []
    created_jobs = 0
    updated_jobs = 0

    pending_jobs = (
        session.query(Job)
        .filter(Job.job_type == job_type, Job.status == "PENDING")
        .order_by(Job.run_after.asc().nullsfirst(), Job.id.asc())
        .with_for_update(skip_locked=True)
        .all()
    )

    for job in pending_jobs:
        if not ids_remaining:
            break
        existing_ids = _job_placeholder_ids(job)
        if len(existing_ids) >= batch_size:
            continue

        additions: list[int] = []
        existing_set = set(existing_ids)
        capacity = batch_size - len(existing_ids)
        for pid in ids_remaining:
            if pid in existing_set:
                continue
            additions.append(pid)
            existing_set.add(pid)
            if len(additions) >= capacity:
                break

        if not additions:
            continue

        _set_job_placeholder_ids(job, existing_ids + additions)
        job.updated_at = now
        session.add(job)
        touched_job_ids.append(int(job.id))
        updated_jobs += 1
        added_set = set(additions)
        ids_remaining = [pid for pid in ids_remaining if pid not in added_set]

    while ids_remaining:
        chunk = ids_remaining[:batch_size]
        ids_remaining = ids_remaining[batch_size:]
        job = Job(
            job_type=job_type,
            payload={"placeholder_ids": chunk},
            status="PENDING",
            run_after=run_after,
        )
        session.add(job)
        session.flush()
        touched_job_ids.append(int(job.id))
        created_jobs += 1

    session.commit()

    logger.debug(
        f"Queued {job_type} placeholders={len(_normalize_placeholder_ids(placeholder_ids))} "
        f"jobs_touched={len(touched_job_ids)} jobs_created={created_jobs} jobs_updated={updated_jobs} "
        f"batch_size={batch_size} debounce_seconds={debounce_seconds:.3f}",
        extra={"emoji_type": "processing"},
    )

    return {
        "ok": True,
        "job_id": touched_job_ids[0] if touched_job_ids else None,
        "job_ids": touched_job_ids,
        "jobs_created": created_jobs,
        "jobs_updated": updated_jobs,
        "placeholder_count": len(_normalize_placeholder_ids(placeholder_ids)),
    }

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
        return reason

    if status == "DOWNLOADING" and reason:
        return reason

    if status == "SEARCHING" and reason and reason.lower() == "queued":
        return reason

    return status


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




def process_nfo_refresh_job(session, job: Job) -> dict:
    """Refresh placeholder sidecar NFO files for a scoped set of placeholders."""
    payload = job.payload if isinstance(job.payload, dict) else {}
    placeholder_ids = payload.get("placeholder_ids") or []
    ids = [int(pid) for pid in placeholder_ids if pid is not None]
    if not ids:
        return {"ok": False, "reason": "no_placeholder_ids"}

    placeholders = session.query(Placeholder).filter(Placeholder.id.in_(ids)).all()
    refreshed = 0
    touched_paths: set[str] = set()
    has_movies = False
    has_episodes = False

    for placeholder in placeholders:
        if not getattr(placeholder, "has_placeholder", False):
            continue

        movie = session.query(Movie).get(placeholder.movie_id) if placeholder.movie_id else None
        episode = session.query(Episode).get(placeholder.episode_id) if placeholder.episode_id else None
        if movie and _refresh_movie_nfo(placeholder, movie):
            refreshed += 1
            has_movies = True
            if placeholder.path:
                touched_paths.add(os.path.dirname(placeholder.path))
            continue
        if episode and _refresh_episode_nfo(session, placeholder, episode):
            refreshed += 1
            has_episodes = True
            if placeholder.path:
                touched_paths.add(os.path.dirname(placeholder.path))

    if touched_paths:
        refresh_all_path_batches_with_section_fallback(
            [(touched_paths, "NFO_Refresh")],
            has_movies=has_movies,
            has_episodes=has_episodes,
            include_plex=True,
        )

    return {
        "ok": True,
        "refreshed": refreshed,
        "scanned": len(placeholders),
    }




def enqueue_nfo_refresh(placeholder_ids: list[int], session=None) -> dict:
    """Enqueue a durable NFO refresh job for placeholders whose projected status changed."""
    owns_session = session is None
    session = session or get_session()

    try:
        normalized_ids = _normalize_placeholder_ids(placeholder_ids)
        if not normalized_ids:
            return {"ok": False, "reason": "no_placeholder_ids"}

        return _enqueue_batched_placeholder_job(NFO_REFRESH_JOB_TYPE, normalized_ids, session)
    except Exception as e:
        logger.error(f"Failed to enqueue NFO refresh job: {e}", exc_info=True)
        session.rollback()
        return {"ok": False, "reason": str(e)}
    finally:
        if owns_session:
            session.close()

