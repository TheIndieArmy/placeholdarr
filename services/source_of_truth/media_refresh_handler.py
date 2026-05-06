"""media_refresh job handler.

Phase 3 of the LISTEN/NOTIFY refactor: every threading.Timer-based media-server
refresh in services/source_of_truth/materializer.py becomes a durable Job of
type 'media_refresh' so that:

- Refreshes survive app restarts (no orphan Timers cancelled mid-flight).
- Each refresh shows up in system_activity_history for observability.
- Refresh fan-out lives behind the same FOR UPDATE SKIP LOCKED claim path the
  rest of the worker pipeline uses.

This is a MECHANICAL conversion. The same primitive functions
(`refresh_selected_sections`, `refresh_all_path_batches_with_section_fallback`)
are called with the same arguments, at the same delays. No timing redesign in
this round.

Lease semantics: existing call sites that acquire a `try_acquire_refresh_lease`
do so BEFORE enqueueing the Job (matches today's behaviour where the lease was
acquired before scheduling the threading.Timer). The handler does NOT re-check
the lease; firing is unconditional once the Job's `run_after` has passed —
identical to how the Timer would have fired.

If we later move the lease check into the handler (per the plan's FM-12 anti-
loop), the re-enqueue path here is the place to add it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.config import settings
from core.logger import logger, start_verbose_stall_heartbeat
from services.media_servers.refresh import (
    refresh_all_path_batches_with_section_fallback,
    refresh_selected_sections,
)
from services.postgres.models import Job


MEDIA_REFRESH_JOB_TYPE = "media_refresh"


# Recognised payload kinds. Each maps to a specific call surface; all share the
# same Job machinery + run_after-based delay.
KIND_BULK_INITIAL = "bulk_initial"
KIND_PHASE_INITIAL_MOVIE = "phase_initial_movie"
KIND_PHASE_FINAL_MOVIE = "phase_final_movie"
KIND_PHASE_INITIAL_EPISODE = "phase_initial_episode"
KIND_PHASE_FINAL_EPISODE = "phase_final_episode"
KIND_OVERLAP_PATH_BATCH = "overlap_path_batch"
KIND_DELAYED_FINAL = "delayed_final"


# Maximum number of times a single media_refresh Job may be re-enqueued because
# of a transient blocker (e.g. lease denied). After this it is marked FAILED so
# we don't loop forever on a configuration issue.
_MAX_REENQUEUE_ATTEMPTS = 5


def use_job_driven_refresh() -> bool:
    return bool(getattr(settings, "USE_JOB_DRIVEN_REFRESH", True))


def enqueue_media_refresh_job(
    session,
    *,
    kind: str,
    payload: dict[str, Any] | None = None,
    delay_seconds: float = 0.0,
    group_id: str | None = None,
    max_attempts: int = 5,
) -> Job:
    """Add a media_refresh Job to the session. Caller is responsible for committing.

    Mirrors the contract of `threading.Timer(delay_seconds, fn).start()` — the
    callers pass the same delay (5, 20) currently in use.
    """
    job_payload: dict[str, Any] = {"kind": str(kind)}
    if payload:
        for k, v in payload.items():
            if k == "kind":
                continue
            job_payload[k] = v

    from services.source_of_truth.job_priority import default_priority_for

    run_after = datetime.now(timezone.utc) + timedelta(seconds=max(0.0, float(delay_seconds)))
    job = Job(
        job_type=MEDIA_REFRESH_JOB_TYPE,
        payload=job_payload,
        status="PENDING",
        run_after=run_after,
        max_attempts=int(max_attempts),
        group_id=group_id,
        priority=default_priority_for(MEDIA_REFRESH_JOB_TYPE),
    )
    session.add(job)
    return job


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _reenqueue_with_delay(session, job: Job, *, delay_seconds: float, reason: str) -> dict[str, Any]:
    """Helper for FM-12 style transient denials. Re-PENDING the job with a future
    run_after so it fires again later. Anti-loop cap via max_attempts.

    Currently unused (handler fires unconditionally) but kept available so we can
    flip on lease checking inside the handler in a follow-up without rewriting
    callers.
    """
    attempts = int(getattr(job, "attempts", 0) or 0)
    if attempts >= _MAX_REENQUEUE_ATTEMPTS:
        return {"ok": False, "reason": f"reenqueue_limit_exceeded:{reason}"}
    job.status = "PENDING"
    job.run_after = datetime.now(timezone.utc) + timedelta(seconds=max(1.0, float(delay_seconds)))
    job.error_message = f"reenqueue:{reason}"
    job.updated_at = datetime.now(timezone.utc)
    session.add(job)
    return {"ok": True, "reenqueued": True, "reason": reason}


def process_media_refresh_job(session, job: Job) -> dict[str, Any]:
    """Worker entrypoint for media_refresh Jobs. Returns {ok, ...} contract.

    Phase 1 connection-lifecycle: this handler does ZERO database work, only
    media-server HTTP via ``refresh_selected_sections`` /
    ``refresh_all_path_batches_with_section_fallback``. We snapshot the
    payload + identity into plain values and CLOSE the session before the
    HTTP work so the pool slot is released for the duration of the refresh
    (which can take many seconds). The worker re-opens a fresh session for
    the finish step.

    Phase 4 idempotency: claim a per-job-id key before triggering the
    refresh so a stale-CLAIMED reaper retry does not double-fire the same
    library scan.
    """
    raw_payload = job.payload or {}
    if not isinstance(raw_payload, dict):
        return {"ok": False, "reason": "invalid_payload"}
    payload = dict(raw_payload)
    job_id = getattr(job, "id", "?")

    # Phase 4: claim idempotency key BEFORE releasing the session (the claim
    # uses the worker's session, then commits). On duplicate-claim we skip
    # the side effect entirely.
    try:
        from services.source_of_truth.idempotency import (
            media_refresh_idempotency_key,
            try_claim_processed_key,
        )

        idem_key = media_refresh_idempotency_key(job_id=int(job_id) if isinstance(job_id, int) else 0)
        if isinstance(job_id, int) and not try_claim_processed_key(
            session, idem_key, job_type=MEDIA_REFRESH_JOB_TYPE
        ):
            try:
                session.commit()
            except Exception:
                session.rollback()
            try:
                session.close()
            except Exception:
                pass
            logger.info(
                f"media_refresh job_id={job_id} skipped — idempotency key already claimed "
                "(stale-CLAIMED reaper requeued or duplicate enqueue)",
                extra={"emoji_type": "info"},
            )
            return {"ok": True, "skipped": "already_processed"}
        try:
            session.commit()
        except Exception:
            session.rollback()
    except Exception as exc:
        logger.debug(
            f"media_refresh: idempotency claim skipped due to error: {exc}",
            extra={"emoji_type": "debug"},
        )

    # Release the worker's connection BEFORE any HTTP. This handler does not
    # need DB after this line.
    try:
        session.close()
    except Exception:
        pass

    kind = str(payload.get("kind") or "").strip()
    if not kind:
        return {"ok": False, "reason": "missing_kind"}

    has_movies = _coerce_bool(payload.get("has_movies"), default=False)
    has_episodes = _coerce_bool(payload.get("has_episodes"), default=False)
    include_plex = _coerce_bool(payload.get("include_plex"), default=True)
    include_jellyfin = _coerce_bool(payload.get("include_jellyfin"), default=False)
    include_emby = _coerce_bool(payload.get("include_emby"), default=False)
    bypass_suppression = _coerce_bool(payload.get("bypass_suppression"), default=False)

    log_prefix = f"media_refresh job_id={job_id} kind={kind}"

    stop_v = start_verbose_stall_heartbeat(log_prefix)
    try:
        if kind == KIND_BULK_INITIAL:
            stats = refresh_selected_sections(
                has_movies=has_movies,
                has_episodes=has_episodes,
                include_plex=include_plex,
                include_jellyfin=include_jellyfin,
                include_emby=include_emby,
            )
            logger.info(
                f"{log_prefix} completed bulk initial media server library refresh: "
                f"refreshed={stats.get('refreshed', 0)} failed={stats.get('failed', 0)}",
                extra={"emoji_type": "success"},
            )
            return {"ok": True, "kind": kind, "stats": stats}

        if kind == KIND_PHASE_INITIAL_MOVIE or kind == KIND_PHASE_FINAL_MOVIE:
            stats = refresh_selected_sections(
                has_movies=True,
                has_episodes=False,
                bypass_suppression=bypass_suppression if bypass_suppression else True,
            )
            phase_label = "initial" if kind == KIND_PHASE_INITIAL_MOVIE else "final"
            logger.info(
                f"{log_prefix} completed full-sync movie phase {phase_label} library refresh: "
                f"refreshed={stats.get('refreshed', 0)} failed={stats.get('failed', 0)}",
                extra={"emoji_type": "success"},
            )
            return {"ok": True, "kind": kind, "stats": stats}

        if kind == KIND_PHASE_INITIAL_EPISODE or kind == KIND_PHASE_FINAL_EPISODE:
            stats = refresh_selected_sections(
                has_movies=False,
                has_episodes=True,
                bypass_suppression=bypass_suppression if bypass_suppression else True,
            )
            phase_label = "initial" if kind == KIND_PHASE_INITIAL_EPISODE else "final"
            logger.info(
                f"{log_prefix} completed full-sync episode phase {phase_label} library refresh: "
                f"refreshed={stats.get('refreshed', 0)} failed={stats.get('failed', 0)}",
                extra={"emoji_type": "success"},
            )
            return {"ok": True, "kind": kind, "stats": stats}

        if kind == KIND_OVERLAP_PATH_BATCH:
            created_paths = list(payload.get("created_paths") or [])
            delete_paths = list(payload.get("delete_paths") or [])
            if not created_paths and not delete_paths:
                logger.debug(
                    f"{log_prefix} skipping overlap path batch — no paths in payload",
                    extra={"emoji_type": "debug"},
                )
                return {"ok": True, "kind": kind, "skipped": "no_paths"}
            stats = refresh_all_path_batches_with_section_fallback(
                [
                    (set(created_paths), "Created"),
                    (set(delete_paths), "Deleted"),
                ],
                has_movies=has_movies,
                has_episodes=has_episodes,
                enable_section_fallback=False,
                fallback_wait_seconds=0,
                include_plex=include_plex,
            )
            logger.info(
                f"{log_prefix} completed delayed Plex path refresh: "
                f"refreshed={stats.get('refreshed', 0)} failed={stats.get('failed', 0)} "
                f"created_paths={len(created_paths)} delete_paths={len(delete_paths)}",
                extra={"emoji_type": "success"},
            )
            return {"ok": True, "kind": kind, "stats": stats}

        if kind == KIND_DELAYED_FINAL:
            created_paths = list(payload.get("created_paths") or [])
            delete_paths = list(payload.get("delete_paths") or [])
            stats = refresh_all_path_batches_with_section_fallback(
                [
                    (set(created_paths), "Created"),
                    (set(delete_paths), "Deleted"),
                ],
                has_movies=has_movies,
                has_episodes=has_episodes,
                enable_section_fallback=False,
                fallback_wait_seconds=0,
                include_plex=include_plex,
            )
            logger.info(
                f"{log_prefix} completed delayed final media server refresh: "
                f"refreshed={stats.get('refreshed', 0)} failed={stats.get('failed', 0)}",
                extra={"emoji_type": "success"},
            )
            return {"ok": True, "kind": kind, "stats": stats}

        logger.warning(
            f"{log_prefix} unknown media_refresh kind; skipping",
            extra={"emoji_type": "warning"},
        )
        return {"ok": True, "kind": kind, "skipped": "unknown_kind"}
    except Exception as exc:
        logger.error(
            f"{log_prefix} failed: {exc}",
            extra={"emoji_type": "error"},
        )
        return {"ok": False, "kind": kind, "reason": str(exc)}
    finally:
        stop_v.set()


__all__ = [
    "MEDIA_REFRESH_JOB_TYPE",
    "KIND_BULK_INITIAL",
    "KIND_PHASE_INITIAL_MOVIE",
    "KIND_PHASE_FINAL_MOVIE",
    "KIND_PHASE_INITIAL_EPISODE",
    "KIND_PHASE_FINAL_EPISODE",
    "KIND_OVERLAP_PATH_BATCH",
    "KIND_DELAYED_FINAL",
    "use_job_driven_refresh",
    "enqueue_media_refresh_job",
    "process_media_refresh_job",
]
