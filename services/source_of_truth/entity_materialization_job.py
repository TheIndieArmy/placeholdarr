"""Async scoped materialization jobs (off the webhook / hot path).

Heavy work from ``run_materialization_for_entities`` used to run synchronously
inside webhook handlers (e.g. ``movie_deleted``), holding the worker thread
until a large PostgreSQL commit finished — often tens of seconds under row-lock
contention with other workers touching the same title.

Enqueueing the same call as a normal PENDING job moves that commit to another
worker turn so the webhook_event job can finish immediately after enqueue.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.logger import logger, start_verbose_stall_heartbeat
from services.placeholders import movie_placeholder_path
from services.placeholder_activity_log import (
    append_placeholder_activity_status,
    materialization_stats_dict,
    outcome_reason_and_status_from_materialization,
)
from services.postgres.db import get_session
from services.postgres.models import Job, Movie
from services.source_of_truth.materializer import run_materialization_for_entities

ENTITY_MATERIALIZATION_JOB_TYPE = "entity_materialization"


def _payload_targets_movie_deleted(payload: dict[str, Any], movie_row_id: int) -> bool:
    """True when this materialization job is the movie_deleted follow-up for ``movie_row_id``."""
    if not isinstance(payload, dict):
        return False
    obs = str(payload.get("observation_source") or "").strip()
    finalize = bool(payload.get("finalize_movie_deleted_activity"))
    if not finalize and obs != "event_movie_deleted":
        return False
    ids = [int(x) for x in (payload.get("movie_ids") or []) if x is not None]
    mid = payload.get("movie_row_id")
    try:
        mid_int = int(mid) if mid is not None else None
    except (TypeError, ValueError):
        mid_int = None
    return int(movie_row_id) in ids or mid_int == int(movie_row_id)


def supersede_pending_movie_deleted_materialization(session, movie_row_id: int) -> int:
    """Mark pending movie_deleted materialization jobs for this row as done.

    Used when a quick re-add revives the same catalog row so deferred delete
    teardown / purge does not run after the title is live again.
    """
    now = datetime.now(timezone.utc)
    rows = (
        session.query(Job)
        .filter(
            Job.job_type == ENTITY_MATERIALIZATION_JOB_TYPE,
            Job.status == "PENDING",
        )
        .with_for_update(skip_locked=True)
        .all()
    )
    superseded = 0
    for job in rows:
        payload = job.payload if isinstance(job.payload, dict) else {}
        if not _payload_targets_movie_deleted(payload, int(movie_row_id)):
            continue
        job.status = "DONE"
        job.error_message = "superseded_by_movie_readd"
        job.updated_at = now
        session.add(job)
        superseded += 1
    if superseded:
        logger.info(
            f"Superseded {superseded} pending movie_deleted materialization job(s) "
            f"for movie_row_id={int(movie_row_id)} after re-add",
            extra={"emoji_type": "info"},
        )
    return superseded


def movie_still_soft_deleted(movie_row_id: int) -> bool:
    """Fresh read: True only when the catalog row exists and ``is_deleted`` is set."""
    sess = get_session()
    try:
        row = sess.query(Movie.id, Movie.is_deleted).filter(Movie.id == int(movie_row_id)).first()
        if not row:
            return False
        return bool(row[1])
    finally:
        sess.close()



def enqueue_entity_materialization_job(
    session,
    *,
    movie_ids: list[int],
    episode_ids: list[int] | None,
    observation_source: str,
    payload_extras: dict[str, Any] | None = None,
) -> Job:
    """Insert a Job row; caller must commit so NOTIFY reaches workers."""
    body: dict[str, Any] = {
        "movie_ids": [int(x) for x in movie_ids if x is not None],
        "episode_ids": [int(x) for x in (episode_ids or []) if x is not None],
        "observation_source": str(observation_source),
    }
    if payload_extras:
        for k, v in payload_extras.items():
            body[str(k)] = v

    from services.source_of_truth.job_priority import default_priority_for

    job = Job(
        job_type=ENTITY_MATERIALIZATION_JOB_TYPE,
        payload=body,
        status="PENDING",
        max_attempts=5,
        priority=default_priority_for(ENTITY_MATERIALIZATION_JOB_TYPE),
    )
    session.add(job)
    return job


def _finalize_movie_deleted_activity(
    *,
    movie_row_id: int,
    stats_mat: dict[str, Any] | None,
    determination_stats: Any,
) -> None:
    """Write movie-deleted result history while the Movie row still exists."""
    det_stats = determination_stats if isinstance(determination_stats, dict) else {}
    mat = materialization_stats_dict(stats_mat)
    result_reason, status_label = outcome_reason_and_status_from_materialization(
        "Movie removed from Radarr", mat
    )
    sess = get_session()
    try:
        movie_after = sess.query(Movie).filter(Movie.id == movie_row_id).first()
        result_path = ""
        item_title = "Unknown Movie"
        instance_key = None
        instance_id = None
        if movie_after:
            result_path = str(getattr(movie_after, "placeholder_filepath", "") or "").strip()
            result_path = result_path or str(movie_placeholder_path(movie_after) or "")
            item_title = str(getattr(movie_after, "title", None) or "Unknown Movie")
            instance_key = getattr(movie_after, "instance_key", None)
            instance_id = getattr(movie_after, "instance_id", None)
        # Prefer a live FK while the row exists; fall back to null + snapshot if already gone.
        history_movie_id = int(movie_row_id) if movie_after else None
        append_placeholder_activity_status(
            sess,
            item_type="movie",
            movie_id=history_movie_id,
            episode_id=None,
            series_id=None,
            season_id=None,
            season_number=None,
            instance_key=instance_key,
            instance_id=instance_id,
            event_type="placeholder_event_movie_deleted_result",
            path=str(result_path or ""),
            item_title=item_title,
            series_title=None,
            reason=result_reason,
            status_label=status_label,
            source="event_movie_deleted",
            extra_snapshot={
                "determination": det_stats,
                "materialization": mat,
                "movie_id": int(movie_row_id),
            },
        )
        c_hb = start_verbose_stall_heartbeat(
            f"entity_materialization.activity_commit movie_id={movie_row_id}",
        )
        try:
            sess.commit()
        finally:
            c_hb.set()
    finally:
        sess.close()


def process_entity_materialization_job(session, job: Job) -> dict[str, Any]:
    """Run scoped materialization and optional movie_deleted activity follow-up.

    For movie deletes, activity history is committed before catalog tombstone
    purge so ``placeholder_activity_history.movie_id`` still resolves.

    If the title was re-added before this job runs (``is_deleted`` cleared),
    skip delete teardown / purge / "removed" activity so a quick undo does not
    fight the new add.
    """
    payload = job.payload if isinstance(job.payload, dict) else {}
    movie_ids = [int(x) for x in (payload.get("movie_ids") or []) if x is not None]
    episode_ids = [int(x) for x in (payload.get("episode_ids") or []) if x is not None]
    observation_source = str(payload.get("observation_source") or "event_materialization")
    run_determination_first = bool(payload.get("run_determination_first"))
    finalize = bool(payload.get("finalize_movie_deleted_activity"))

    stop_v = start_verbose_stall_heartbeat(
        f"entity_materialization job_id={job.id} obs={observation_source}",
    )
    try:
        if finalize and movie_ids:
            movie_row_id = int(payload.get("movie_row_id") or movie_ids[0])
            if not movie_still_soft_deleted(movie_row_id):
                logger.info(
                    f"entity_materialization job_id={job.id} movie_row_id={movie_row_id} "
                    f"no longer soft-deleted (re-added or already purged); skipping delete follow-up",
                    extra={"emoji_type": "info"},
                )
                return {"ok": True, "superseded": True, "movie_id": movie_row_id}

        determination_stats: dict[str, Any] | None = None
        if run_determination_first and (movie_ids or episode_ids):
            from services.source_of_truth.determiner import run_determination_for_entities

            determination_stats = run_determination_for_entities(
                movie_ids=movie_ids or None,
                episode_ids=episode_ids or None,
            )
            logger.info(
                f"entity_materialization job_id={job.id} determination finished "
                f"movies={len(movie_ids)} episodes={len(episode_ids)}",
                extra={"emoji_type": "processing"},
            )

        # Defer hard-purge when we still need to write movie-deleted activity.
        stats_mat = run_materialization_for_entities(
            movie_ids=movie_ids or None,
            episode_ids=episode_ids or None,
            observation_source=observation_source,
            purge_catalog_tombstones=not finalize,
        )

        if finalize and movie_ids:
            movie_row_id = int(payload.get("movie_row_id") or movie_ids[0])
            # Re-check after mat: a concurrent re-add may have revived the row.
            if not movie_still_soft_deleted(movie_row_id):
                logger.info(
                    f"entity_materialization job_id={job.id} movie_row_id={movie_row_id} "
                    f"revived during materialization; skipping delete activity + purge",
                    extra={"emoji_type": "info"},
                )
                return {"ok": True, "superseded": True, "movie_id": movie_row_id}

            det_stats = payload.get("determination_stats") or determination_stats or {}
            _finalize_movie_deleted_activity(
                movie_row_id=movie_row_id,
                stats_mat=stats_mat if isinstance(stats_mat, dict) else {},
                determination_stats=det_stats,
            )
            try:
                from services.source_of_truth.catalog_tombstone_purge import purge_cleaned_deleted_catalog

                purge_stats = purge_cleaned_deleted_catalog(
                    movie_ids=movie_ids,
                    episode_ids=episode_ids,
                )
                if isinstance(stats_mat, dict) and isinstance(purge_stats, dict):
                    stats_mat["catalog_tombstone_purge"] = purge_stats
            except Exception as purge_exc:
                logger.debug(
                    f"entity_materialization job_id={job.id} deferred catalog purge skipped: {purge_exc}",
                    extra={"emoji_type": "debug"},
                )
    finally:
        stop_v.set()

    logger.info(
        f"entity_materialization job_id={job.id} observation_source={observation_source} "
        f"movies={len(movie_ids)} episodes={len(episode_ids)} ok",
        extra={"emoji_type": "success"},
    )
    return {"ok": True}


__all__ = [
    "ENTITY_MATERIALIZATION_JOB_TYPE",
    "enqueue_entity_materialization_job",
    "process_entity_materialization_job",
    "supersede_pending_movie_deleted_materialization",
    "movie_still_soft_deleted",
]
