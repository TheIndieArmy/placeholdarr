from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

from core.config import settings
from core.logger import logger
from services.placeholders import ensure_episode_nfo, ensure_movie_nfo, ensure_series_nfo
from sqlalchemy import func

from services.postgres.db import get_session
from services.postgres.models import AppConfig, Episode, Movie, Placeholder, Job, Season, Series
from services.media_servers.player_metadata_refresh import push_placeholder_batch_player_metadata
from services.media_servers.refresh import refresh_all_sections


NFO_REFRESH_JOB_TYPE = "nfo_refresh"
_REQUEST_NFO_BACKFILL_DONE_KEY = "REQUEST_STATUS_NFO_BACKFILL_DONE"


def _job_batch_size() -> int:
    return max(1, int(getattr(settings, "STATUS_JOB_BATCH_SIZE", 250) or 250))


def _job_debounce_seconds() -> float:
    return max(0.0, float(getattr(settings, "STATUS_JOB_DEBOUNCE_SECONDS", 0.5) or 0.5))


def _nfo_refresh_subject_summary(session, placeholders: list[Placeholder]) -> str:
    """Short human-readable line for logs (movies / series+episode)."""
    if not placeholders:
        return ""
    mids = [int(ph.movie_id) for ph in placeholders if getattr(ph, "movie_id", None)]
    eids = [int(ph.episode_id) for ph in placeholders if getattr(ph, "episode_id", None)]
    movie_title: dict[int, str] = {}
    if mids:
        for m in session.query(Movie).filter(Movie.id.in_(mids)).all():
            t = str(getattr(m, "title", "") or "").strip()
            movie_title[int(m.id)] = t or f"movie id {m.id}"
    ep_label: dict[int, str] = {}
    if eids:
        rows = (
            session.query(Episode, Season, Series)
            .join(Season, Episode.season_id == Season.id)
            .join(Series, Season.series_id == Series.id)
            .filter(Episode.id.in_(eids))
            .all()
        )
        for ep, season, series in rows:
            sn = int(season.season_number or 0)
            en = int(ep.episode_number or 0)
            st = str(getattr(series, "title", "") or "").strip() or "Series"
            et = str(getattr(ep, "title", "") or "").strip()
            base = f"{st} S{sn:02d}E{en:02d}"
            if et:
                base += f' — "{et}"'
            ep_label[int(ep.id)] = base

    per_row: list[str] = []
    for ph in placeholders:
        if ph.movie_id and int(ph.movie_id) in movie_title:
            per_row.append(movie_title[int(ph.movie_id)])
        elif ph.episode_id and int(ph.episode_id) in ep_label:
            per_row.append(ep_label[int(ph.episode_id)])
        else:
            per_row.append(f"placeholder id {int(ph.id)}")

    uniq: list[str] = []
    seen: set[str] = set()
    for label in per_row:
        if label not in seen:
            seen.add(label)
            uniq.append(label)

    if len(uniq) == 1:
        if len(per_row) > 1:
            return f'{len(per_row)} rows · "{uniq[0]}"'
        return f'"{uniq[0]}"'

    head = uniq[:4]
    tail_ct = len(uniq) - len(head)
    text = "; ".join(f'"{u}"' for u in head)
    if tail_ct > 0:
        text += f" (+{tail_ct} more titles)"
    if len(per_row) > len(uniq):
        text += f" · {len(per_row)} placeholder rows"
    return text


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


def _job_player_metadata_refresh(job: Job) -> bool:
    payload = job.payload if isinstance(job.payload, dict) else {}
    return bool(payload.get("player_metadata_refresh", True))


def _placeholder_player_merge_flag(placeholder_ids: list[int], by_id: dict[int, bool] | None) -> bool:
    if not by_id:
        return True
    return any(bool(by_id.get(int(pid), True)) for pid in placeholder_ids)


def _enqueue_batched_placeholder_job(
    job_type: str,
    placeholder_ids: list[int],
    session,
    *,
    player_metadata_refresh_by_id: dict[int, bool] | None = None,
    merge_into_pending: bool = True,
    payload_extras: dict[str, Any] | None = None,
) -> dict:
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

    pending_jobs: list[Job] = []
    if merge_into_pending:
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

        merged_flag = _job_player_metadata_refresh(job) or _placeholder_player_merge_flag(
            additions, player_metadata_refresh_by_id
        )
        _set_job_placeholder_ids(job, existing_ids + additions)
        payload = dict(job.payload or {}) if isinstance(job.payload, dict) else {}
        payload["player_metadata_refresh"] = merged_flag
        if payload_extras:
            payload.update(payload_extras)
        job.payload = payload
        job.updated_at = now
        session.add(job)
        touched_job_ids.append(int(job.id))
        updated_jobs += 1
        added_set = set(additions)
        ids_remaining = [pid for pid in ids_remaining if pid not in added_set]

    while ids_remaining:
        chunk = ids_remaining[:batch_size]
        ids_remaining = ids_remaining[batch_size:]
        chunk_player_flag = _placeholder_player_merge_flag(chunk, player_metadata_refresh_by_id)
        job = Job(
            job_type=job_type,
            payload={
                "placeholder_ids": chunk,
                "player_metadata_refresh": chunk_player_flag,
                **(payload_extras or {}),
            },
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
    n_total = len(placeholders)
    refreshed = 0
    # (Placeholder row, entity dedupe key) for rows whose NFO was rewritten this run
    refreshed_for_player_push: list[tuple[Placeholder, tuple[str, int]]] = []
    loop_pos = [0]
    nfo_started = time.monotonic()
    stop_nfo_hb = threading.Event()

    def _nfo_refresh_stall_heartbeat():
        while not stop_nfo_hb.wait(10.0):
            logger.verbose(
                f"nfo_refresh job_id={job.id}: still running "
                f"progress={loop_pos[0]}/{n_total} nfo_refreshed={refreshed} "
                f"elapsed_s={time.monotonic() - nfo_started:.1f}",
                extra={"emoji_type": "processing"},
            )

    _nfo_hb_t = None
    if n_total:
        _nfo_hb_t = threading.Thread(
            target=_nfo_refresh_stall_heartbeat,
            name=f"nfo-refresh-hb-{job.id}",
            daemon=True,
        )
        _nfo_hb_t.start()
    try:
        for placeholder in placeholders:
            loop_pos[0] += 1
            movie = session.query(Movie).get(placeholder.movie_id) if placeholder.movie_id else None
            episode = session.query(Episode).get(placeholder.episode_id) if placeholder.episode_id else None
            effective_has_placeholder = bool(getattr(placeholder, "has_placeholder", False))
            if movie and bool(getattr(movie, "has_placeholder", False)):
                effective_has_placeholder = True
            if episode and bool(getattr(episode, "has_placeholder", False)):
                effective_has_placeholder = True
            if effective_has_placeholder and not bool(getattr(placeholder, "has_placeholder", False)):
                # Self-heal drift so follow-up jobs do not keep skipping valid placeholders.
                placeholder.has_placeholder = True
                placeholder.updated_at = datetime.now(timezone.utc)
                session.add(placeholder)
            if not effective_has_placeholder:
                continue

            if movie and _refresh_movie_nfo(placeholder, movie):
                refreshed += 1
                refreshed_for_player_push.append((placeholder, ("movie", int(movie.id))))
                continue
            if episode and _refresh_episode_nfo(session, placeholder, episode):
                refreshed += 1
                refreshed_for_player_push.append((placeholder, ("episode", int(episode.id))))
    finally:
        stop_nfo_hb.set()

    do_player = _job_player_metadata_refresh(job)
    run_id = str(payload.get("request_backfill_run_id") or "").strip()
    completion_refresh = bool(payload.get("request_backfill_refresh_on_completion"))
    subject = _nfo_refresh_subject_summary(session, placeholders)
    subj_part = f" · {subject}" if subject else ""
    logger.debug(
        f"Placeholder follow-up processed · {len(ids)} id(s){subj_part} · nfo_refreshed={refreshed} · player_metadata_refresh={do_player}",
        extra={"emoji_type": "info"},
    )

    if do_player and refreshed_for_player_push:
        # One media-server refresh sequence per underlying movie/episode row, even if several
        # placeholder rows pointed at the same title (rare) or a batch carried duplicates.
        seen_entity: set[tuple[str, int]] = set()
        unique_for_projection: list[Placeholder] = []
        for placeholder, entity_key in refreshed_for_player_push:
            if not getattr(placeholder, "has_placeholder", False):
                continue
            if entity_key in seen_entity:
                continue
            seen_entity.add(entity_key)
            unique_for_projection.append(placeholder)
        try:
            push_placeholder_batch_player_metadata(session, unique_for_projection)
            proj_subject = _nfo_refresh_subject_summary(session, unique_for_projection)
            ps = f" · {proj_subject}" if proj_subject else ""
            logger.info(
                f"Direct player projection attempted · {len(unique_for_projection)} placeholder(s){ps}",
                extra={"emoji_type": "info"},
            )
        except Exception as ex:
            logger.warning(
                f"Player metadata refresh after NFO failed for placeholder batch size={len(unique_for_projection)}: {ex}",
                extra={"emoji_type": "warning"},
            )
    elif completion_refresh and run_id:
        pending_same_run = (
            session.query(Job.id)
            .filter(
                Job.job_type == NFO_REFRESH_JOB_TYPE,
                Job.status.in_(["PENDING", "CLAIMED", "WORKING"]),
                Job.id != job.id,
                Job.payload["request_backfill_run_id"].astext == run_id,
            )
            .first()
        )
        if not pending_same_run:
            try:
                refresh_stats = refresh_all_sections(
                    has_movies=True,
                    has_episodes=True,
                    plex_force_metadata_refresh=True,
                )
                logger.info(
                    "REQUEST NFO backfill run complete; triggered one library section refresh "
                    f"run_id={run_id} refreshed={int(refresh_stats.get('refreshed', 0) or 0)} "
                    f"failed={int(refresh_stats.get('failed', 0) or 0)}",
                    extra={"emoji_type": "success"},
                )
            except Exception as ex:
                logger.warning(
                    f"REQUEST NFO backfill completion refresh failed run_id={run_id}: {ex}",
                    extra={"emoji_type": "warning"},
                )

    return {
        "ok": True,
        "refreshed": refreshed,
        "scanned": len(placeholders),
    }




def enqueue_nfo_refresh(
    placeholder_ids: list[int],
    session=None,
    *,
    player_metadata_refresh: dict[int, bool] | None = None,
    merge_into_pending: bool = True,
    payload_extras: dict[str, Any] | None = None,
) -> dict:
    """Enqueue a durable NFO refresh job for placeholders whose projected status changed."""
    owns_session = session is None
    session = session or get_session()

    try:
        normalized_ids = _normalize_placeholder_ids(placeholder_ids)
        if not normalized_ids:
            return {"ok": False, "reason": "no_placeholder_ids"}

        return _enqueue_batched_placeholder_job(
            NFO_REFRESH_JOB_TYPE,
            normalized_ids,
            session,
            player_metadata_refresh_by_id=player_metadata_refresh,
            merge_into_pending=merge_into_pending,
            payload_extras=payload_extras,
        )
    except Exception as e:
        logger.error(f"Failed to enqueue NFO refresh job: {e}", exc_info=True)
        session.rollback()
        return {"ok": False, "reason": str(e)}
    finally:
        if owns_session:
            session.close()


def enqueue_request_status_nfo_backfill(session=None) -> dict:
    """One-time startup backfill for REQUEST placeholder NFO text projection.

    Runs once when REQUEST placeholders exist, then marks completion in ``app_config`` so
    subsequent startups skip it (similar to specials backfill behavior).
    """
    owns_session = session is None
    session = session or get_session()

    try:
        done_row = session.query(AppConfig).filter(AppConfig.key == _REQUEST_NFO_BACKFILL_DONE_KEY).first()
        if done_row and bool(done_row.value):
            return {
                "ok": True,
                "skipped": True,
                "reason": "already_done",
                "placeholder_count": 0,
                "done": True,
            }

        ids = [
            int(r[0])
            for r in (
                session.query(Placeholder.id)
                .filter(
                    Placeholder.has_placeholder == True,  # noqa: E712
                    func.upper(func.trim(Placeholder.display_status)) == "REQUEST",
                )
                .order_by(Placeholder.id.asc())
                .all()
            )
        ]
        if not ids:
            return {
                "ok": True,
                "skipped": True,
                "reason": "no_matching_request_placeholders",
                "placeholder_count": 0,
                "done": False,
            }

        run_id = uuid.uuid4().hex
        out = enqueue_nfo_refresh(
            ids,
            session=session,
            player_metadata_refresh={int(pid): False for pid in ids},
            merge_into_pending=False,
            payload_extras={
                "request_backfill_run_id": run_id,
                "request_backfill_refresh_on_completion": True,
            },
        )
        if not isinstance(out, dict) or not out.get("ok"):
            return out if isinstance(out, dict) else {"ok": False, "reason": "enqueue_return"}

        if done_row:
            done_row.value = True
            done_row.value_type = "bool"
            session.add(done_row)
        else:
            session.add(
                AppConfig(
                    key=_REQUEST_NFO_BACKFILL_DONE_KEY,
                    value=True,
                    value_type="bool",
                    restart_required=False,
                    description="Internal flag: one-time REQUEST status NFO backfill has completed.",
                )
            )
        session.commit()
        out["placeholder_count"] = len(ids)
        out["done"] = True
        return out
    except Exception as e:
        logger.warning(f"REQUEST NFO backfill enqueue failed: {e}", exc_info=True)
        try:
            session.rollback()
        except Exception:
            pass
        return {"ok": False, "reason": str(e), "placeholder_count": 0}
    finally:
        if owns_session:
            session.close()
