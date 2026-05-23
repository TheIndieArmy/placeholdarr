"""Batched placeholder local art refresh (decoupled from NFO writes)."""

from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from core.logger import logger
from services.media_servers.refresh import refresh_all_paths, refresh_all_sections
from services.placeholder_poster_art import (
    ensure_episode_still_art,
    ensure_movie_art,
    ensure_series_art,
)
from services.postgres.db import get_session
from services.postgres.models import Episode, Job, Movie, Placeholder, Season, Series
from services.source_of_truth.idempotency import (
    placeholder_art_refresh_idempotency_key,
    try_claim_processed_key,
)
from services.source_of_truth.placeholder_job_enqueue import enqueue_batched_placeholder_job
from services.task_run_phases import FULL_SYNC_TASK_RUN_ID_KEY


PLACEHOLDER_ART_REFRESH_JOB_TYPE = "placeholder_art_refresh"
ART_BACKFILL_RUN_ID_KEY = "art_backfill_run_id"
ART_BACKFILL_REFRESH_ON_COMPLETION_KEY = "art_backfill_refresh_on_completion"
ART_BACKFILL_TASK_RUN_ID_KEY = "art_backfill_task_run_id"


def collect_active_placeholder_ids(session) -> list[int]:
    rows = (
        session.query(Placeholder.id)
        .filter(Placeholder.has_placeholder == True)  # noqa: E712
        .order_by(Placeholder.id.asc())
        .all()
    )
    return [int(r[0]) for r in rows if r and r[0] is not None]


def enqueue_placeholder_art_refresh(
    placeholder_ids: list[int],
    session=None,
    *,
    merge_into_pending: bool = True,
    payload_extras: dict[str, Any] | None = None,
) -> dict:
    owns_session = session is None
    session = session or get_session()
    try:
        return enqueue_batched_placeholder_job(
            PLACEHOLDER_ART_REFRESH_JOB_TYPE,
            placeholder_ids,
            session,
            merge_into_pending=merge_into_pending,
            payload_extras=payload_extras,
            include_player_metadata_refresh=False,
        )
    except Exception as exc:
        logger.error(f"Failed to enqueue placeholder art refresh: {exc}", exc_info=True)
        session.rollback()
        return {"ok": False, "reason": str(exc)}
    finally:
        if owns_session:
            try:
                session.close()
            except Exception:
                pass


def enqueue_placeholder_art_backfill_all(*, source: str = "full_sync", task_run_id: int | None = None) -> dict:
    """Queue art refresh for every active placeholder with completion library scan."""
    session = get_session()
    try:
        ids = collect_active_placeholder_ids(session)
        if not ids:
            return {"ok": True, "placeholder_count": 0, "enqueued": False, "source": source}
        run_id = uuid.uuid4().hex
        extras: dict[str, Any] = {
            ART_BACKFILL_RUN_ID_KEY: run_id,
            ART_BACKFILL_REFRESH_ON_COMPLETION_KEY: True,
            "art_backfill_source": source,
        }
        if task_run_id is not None:
            tid = int(task_run_id)
            extras[FULL_SYNC_TASK_RUN_ID_KEY] = tid
            extras[ART_BACKFILL_TASK_RUN_ID_KEY] = tid
        out = enqueue_placeholder_art_refresh(
            ids,
            session=session,
            merge_into_pending=False,
            payload_extras=extras,
        )
        batch_count = int(out.get("jobs_created") or 0)
        if task_run_id is not None and out.get("ok"):
            from services.task_run_phases import begin_art_backfill_phase

            begin_art_backfill_phase(
                int(task_run_id),
                art_run_id=run_id,
                placeholder_count=int(out.get("placeholder_count") or len(ids)),
                batch_count=batch_count,
                source=source,
            )
        out["art_backfill_run_id"] = run_id
        out["batch_count"] = batch_count
        out["enqueued"] = bool(out.get("ok")) and batch_count > 0
        return out
    finally:
        session.close()


def _art_refresh_completion_scan_if_last_batch(
    session,
    job: Job,
    *,
    run_id: str,
    log_prefix: str = "Placeholder art backfill",
) -> None:
    pending_same_run = (
        session.query(Job.id)
        .filter(
            Job.job_type == PLACEHOLDER_ART_REFRESH_JOB_TYPE,
            Job.status.in_(["PENDING", "CLAIMED", "WORKING"]),
            Job.id != job.id,
            Job.payload[ART_BACKFILL_RUN_ID_KEY].as_string() == run_id,
        )
        .first()
    )
    if pending_same_run:
        return

    task_run_id = None
    try:
        payload = job.payload if isinstance(job.payload, dict) else {}
        raw_tid = payload.get(FULL_SYNC_TASK_RUN_ID_KEY) or payload.get(ART_BACKFILL_TASK_RUN_ID_KEY)
        if raw_tid is not None:
            task_run_id = int(raw_tid)
    except Exception:
        task_run_id = None

    # Close the worker DB session before long media-server HTTP (same pattern as nfo_refresh).
    try:
        session.commit()
    except Exception:
        session.rollback()
    try:
        session.close()
    except Exception:
        pass

    # Mark the full-sync task DONE before Plex/JF/Emby refresh so the Tasks UI does not
    # sit on WORKING for tens of minutes while library scans run.
    if task_run_id:
        from services.task_run_phases import finalize_art_backfill_phase

        finalize_art_backfill_phase(task_run_id, run_id)

    try:
        refresh_stats = refresh_all_sections(
            has_movies=True,
            has_episodes=True,
            plex_force_metadata_refresh=True,
        )
        logger.info(
            f"{log_prefix} run complete; triggered library section refresh "
            f"run_id={run_id} refreshed={int(refresh_stats.get('refreshed', 0) or 0)} "
            f"failed={int(refresh_stats.get('failed', 0) or 0)}",
            extra={"emoji_type": "success"},
        )
    except Exception as exc:
        logger.warning(
            f"{log_prefix} completion refresh failed run_id={run_id}: {exc}",
            extra={"emoji_type": "warning"},
        )


def process_placeholder_art_refresh_job(session, job: Job) -> dict:
    payload = job.payload if isinstance(job.payload, dict) else {}
    placeholder_ids = payload.get("placeholder_ids") or []
    ids = [int(pid) for pid in placeholder_ids if pid is not None]
    if not ids:
        return {"ok": False, "reason": "no_placeholder_ids"}

    try:
        job_id_val = getattr(job, "id", None)
        if isinstance(job_id_val, int):
            idem_key = placeholder_art_refresh_idempotency_key(job_id=int(job_id_val))
            if not try_claim_processed_key(session, idem_key, job_type=PLACEHOLDER_ART_REFRESH_JOB_TYPE):
                logger.info(
                    f"placeholder_art_refresh job_id={job_id_val} skipped — already processed",
                    extra={"emoji_type": "info"},
                )
                return {"ok": True, "skipped": "already_processed"}
    except Exception as exc:
        logger.debug(f"placeholder_art_refresh idempotency skipped: {exc}", extra={"emoji_type": "debug"})

    from services.task_run_phases import accumulate_art_backfill_counts, empty_art_counts

    placeholders = session.query(Placeholder).filter(Placeholder.id.in_(ids)).all()
    n_total = len(placeholders)
    wrote = 0
    batch_counts = empty_art_counts()
    refresh_paths: set[str] = set()
    processed_movies: set[int] = set()
    processed_series: set[int] = set()
    loop_pos = [0]
    started = time.monotonic()
    stop_hb = threading.Event()

    def _heartbeat():
        while not stop_hb.wait(10.0):
            logger.verbose(
                f"placeholder_art_refresh job_id={job.id}: still running "
                f"progress={loop_pos[0]}/{n_total} wrote={wrote} "
                f"elapsed_s={time.monotonic() - started:.1f}",
                extra={"emoji_type": "processing"},
            )

    hb_thread = None
    if n_total:
        hb_thread = threading.Thread(
            target=_heartbeat,
            name=f"art-refresh-hb-{job.id}",
            daemon=True,
        )
        hb_thread.start()

    try:
        for placeholder in placeholders:
            loop_pos[0] += 1
            if not bool(getattr(placeholder, "has_placeholder", False)):
                continue

            movie = session.query(Movie).get(placeholder.movie_id) if placeholder.movie_id else None
            episode = session.query(Episode).get(placeholder.episode_id) if placeholder.episode_id else None

            if movie and bool(getattr(movie, "has_placeholder", False)):
                mid = int(movie.id)
                path = str(getattr(placeholder, "path", "") or getattr(movie, "placeholder_filepath", "") or "").strip()
                if path and mid not in processed_movies:
                    processed_movies.add(mid)
                    result = ensure_movie_art(movie, path)
                    if result.wrote_any:
                        wrote += 1
                        for k, v in result.art_counts.items():
                            batch_counts[k] = int(batch_counts.get(k, 0)) + int(v)
                    refresh_paths.add(os.path.dirname(os.path.abspath(path)))
                continue

            if not episode or not bool(getattr(episode, "has_placeholder", False)):
                continue

            season = session.query(Season).get(episode.season_id) if episode.season_id else None
            series = session.query(Series).get(season.series_id) if season and season.series_id else None
            if not season or not series:
                continue

            path = str(
                getattr(placeholder, "path", "") or getattr(episode, "placeholder_filepath", "") or ""
            ).strip()
            if not path:
                continue

            sid = int(series.id)
            series_folder = getattr(series, "placeholder_folder", None) or os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(path)))
            )
            if sid not in processed_series:
                processed_series.add(sid)
                series_result = ensure_series_art(series, series_folder=series_folder)
                if series_result.wrote_any:
                    wrote += 1
                    for k, v in series_result.art_counts.items():
                        batch_counts[k] = int(batch_counts.get(k, 0)) + int(v)
                if series_folder:
                    refresh_paths.add(os.path.abspath(series_folder))

            still_result = ensure_episode_still_art(episode, season, series, path)
            if still_result.wrote_any:
                wrote += 1
                for k, v in still_result.art_counts.items():
                    batch_counts[k] = int(batch_counts.get(k, 0)) + int(v)
            refresh_paths.add(os.path.dirname(os.path.abspath(path)))
    finally:
        stop_hb.set()

    bulk_completion = bool(payload.get(ART_BACKFILL_REFRESH_ON_COMPLETION_KEY))
    run_id = str(payload.get(ART_BACKFILL_RUN_ID_KEY) or "").strip()
    task_run_id_raw = payload.get(FULL_SYNC_TASK_RUN_ID_KEY) or payload.get(ART_BACKFILL_TASK_RUN_ID_KEY)
    if task_run_id_raw is not None and run_id:
        try:
            accumulate_art_backfill_counts(int(task_run_id_raw), run_id, batch_counts)
        except Exception as exc:
            logger.debug(f"art backfill phase stats update skipped: {exc}", extra={"emoji_type": "debug"})

    if refresh_paths and not bulk_completion:
        try:
            refresh_all_paths(refresh_paths, update_type="Modified", include_plex=True, include_jellyfin=True, include_emby=True)
        except Exception as exc:
            logger.warning(f"placeholder_art_refresh path refresh failed: {exc}", extra={"emoji_type": "warning"})

    if bulk_completion and run_id:
        _art_refresh_completion_scan_if_last_batch(session, job, run_id=run_id)

    return {"ok": True, "wrote": wrote, "scanned": len(placeholders), "series_count": len(processed_series)}
