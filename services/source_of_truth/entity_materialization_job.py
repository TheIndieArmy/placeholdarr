"""Async scoped materialization jobs (off the webhook / hot path).

Heavy work from ``run_materialization_for_entities`` used to run synchronously
inside webhook handlers (e.g. ``movie_deleted``), holding the worker thread
until a large PostgreSQL commit finished — often tens of seconds under row-lock
contention with other workers touching the same title.

Enqueueing the same call as a normal PENDING job moves that commit to another
worker turn so the webhook_event job can finish immediately after enqueue.
"""

from __future__ import annotations

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


def process_entity_materialization_job(session, job: Job) -> dict[str, Any]:
    """Run scoped materialization and optional movie_deleted activity follow-up."""
    payload = job.payload if isinstance(job.payload, dict) else {}
    movie_ids = [int(x) for x in (payload.get("movie_ids") or []) if x is not None]
    episode_ids = [int(x) for x in (payload.get("episode_ids") or []) if x is not None]
    observation_source = str(payload.get("observation_source") or "event_materialization")

    stop_v = start_verbose_stall_heartbeat(
        f"entity_materialization job_id={job.id} obs={observation_source}",
    )
    try:
        stats_mat = run_materialization_for_entities(
            movie_ids=movie_ids or None,
            episode_ids=episode_ids or None,
            observation_source=observation_source,
        )

        finalize = bool(payload.get("finalize_movie_deleted_activity"))
        if finalize and movie_ids:
            movie_row_id = int(payload.get("movie_row_id") or movie_ids[0])
            det_stats = payload.get("determination_stats") or {}
            sess = get_session()
            try:
                movie_after = sess.query(Movie).filter(Movie.id == movie_row_id).first()
                mat = materialization_stats_dict(stats_mat)
                result_reason, status_label = outcome_reason_and_status_from_materialization(
                    "Movie removed from Radarr", mat
                )
                result_path = ""
                if movie_after:
                    result_path = str(getattr(movie_after, "placeholder_filepath", "") or "").strip()
                    result_path = result_path or str(movie_placeholder_path(movie_after) or "")
                append_placeholder_activity_status(
                    sess,
                    item_type="movie",
                    movie_id=movie_row_id,
                    episode_id=None,
                    series_id=None,
                    season_id=None,
                    season_number=None,
                    instance_key=getattr(movie_after, "instance_key", None) if movie_after else None,
                    instance_id=getattr(movie_after, "instance_id", None) if movie_after else None,
                    event_type="placeholder_event_movie_deleted_result",
                    path=str(result_path or ""),
                    item_title=str(getattr(movie_after, "title", None) or "Unknown Movie"),
                    series_title=None,
                    reason=result_reason,
                    status_label=status_label,
                    source="event_movie_deleted",
                    extra_snapshot={
                        "determination": det_stats if isinstance(det_stats, dict) else {},
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
]
