import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import and_, func, text, update
from sqlalchemy.exc import OperationalError

from core.config import settings
from core.logger import logger, start_verbose_stall_heartbeat


# Postgres SQLSTATE for ``lock_timeout`` (LockNotAvailable). Matched against
# ``OperationalError.orig.pgcode`` so the worker can self-heal blocked claim
# commits instead of stalling for hours behind a long-running transaction.
_PG_LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"


def _is_lock_timeout_error(exc: Exception) -> bool:
    pgcode = getattr(getattr(exc, "orig", None), "pgcode", None)
    return str(pgcode or "") == _PG_LOCK_NOT_AVAILABLE_SQLSTATE
from services.event_normalization import legacy_dispatch_event_type
from services.postgres.db import get_session
from services.postgres.models import EventLog, Job


@dataclass(frozen=True)
class ClaimedJobDescriptor:
    """Detached snapshot of a CLAIMED job row.

    The claim transaction commits and releases its DB connection BEFORE the
    handler runs, so the handler does not pin a pool slot while a slow ORM
    Job object dangles. The descriptor carries the stable identity needed to
    reload the Job in a fresh session inside the handler loop.
    """

    id: int
    job_type: str
    claim_token: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
from services.source_of_truth.event_handlers import (
    process_episode_file_deleted_event,
    process_episode_imported_event,
    process_movie_add_event,
    process_movie_deleted_event,
    process_movie_file_deleted_event,
    process_movie_imported_event,
    process_series_add_event,
    process_series_deleted_event,
)
from services.source_of_truth.event_playback import (
    PLAYBACK_FALLBACK_JOB_TYPE,
    process_playback_fallback_job,
    process_playback_start_event,
)
from services.source_of_truth.import_grace import (
    IMPORT_GRACE_JOB_TYPE,
    process_import_grace_job,
)
from services.source_of_truth.entity_materialization_job import (
    ENTITY_MATERIALIZATION_JOB_TYPE,
    process_entity_materialization_job,
)
from services.source_of_truth.entity_reconcile import (
    ENTITY_RECONCILE_JOB_TYPE,
    process_entity_reconcile_job,
)
from services.source_of_truth.placeholder_art_reconciler import (
    PLACEHOLDER_ART_REFRESH_JOB_TYPE,
    process_placeholder_art_refresh_job,
)
from services.source_of_truth.status_reconciler import (
    NFO_REFRESH_JOB_TYPE,
    process_nfo_refresh_job,
)


class StaleJobClaimError(Exception):
    """Raised when completing a job row would overwrite state after stale-CLAIMED reaper reset."""


# ----------------------------------------------------------------
# NOTIFY/LISTEN-driven worker coordination
# ----------------------------------------------------------------

# Set by the shared notifier callback (or a startup self-test) when there may be
# work to drain. Cleared by the first executor to wake; subsequent NOTIFYs while
# any executor is mid-drain re-set it so a fresh wait() returns immediately.
_drain_event = threading.Event()


# Phase 4 cooperative-exit registry: the stale-CLAIMED reaper marks job_ids
# here when it requeues a row whose original handler may still be running.
# Long-running handlers MAY check ``is_claim_revoked(job_id)`` at safe points
# and exit early; the worker clears the marker after each finish.
_revoked_claims_lock = threading.Lock()
_revoked_claim_ids: set[int] = set()


def mark_claim_revoked(job_id: int) -> None:
    """Signal that a job's claim has been revoked (e.g. by the reaper).

    Long-running handlers MAY poll ``is_claim_revoked(job_id)`` at safe
    points and exit early. This is best-effort: not all handlers check
    it, and the worker thread is never killed — this is a cooperative
    signal, not a hard kill.
    """
    try:
        with _revoked_claims_lock:
            _revoked_claim_ids.add(int(job_id))
    except Exception:
        pass


def is_claim_revoked(job_id: int) -> bool:
    """Return True if ``mark_claim_revoked`` was called for this job_id."""
    try:
        with _revoked_claims_lock:
            return int(job_id) in _revoked_claim_ids
    except Exception:
        return False


def _clear_claim_revoked(job_id: int) -> None:
    try:
        with _revoked_claims_lock:
            _revoked_claim_ids.discard(int(job_id))
    except Exception:
        pass

# Background-services lifecycle flags. The reaper and listener-bridge are
# started exactly once per process from start_runtime_background_services()
# via ensure_worker_runtime_started().
_runtime_lock = threading.Lock()
_runtime_started = False
_listener_bridged = False
_reaper_started = False
_reaper_stop = threading.Event()


def _safety_poll_seconds() -> float:
    try:
        v = float(getattr(settings, 'WORKER_SAFETY_POLL_SECONDS', 60) or 60)
    except Exception:
        v = 60.0
    return max(1.0, v)


def _stale_claimed_reset_seconds() -> int:
    try:
        v = int(getattr(settings, 'WORKER_STALE_CLAIMED_RESET_SECONDS', 1800) or 1800)
    except Exception:
        v = 1800
    return max(60, v)


def _stale_claimed_reap_interval_seconds() -> int:
    try:
        v = int(getattr(settings, 'WORKER_STALE_CLAIMED_REAP_INTERVAL_SECONDS', 300) or 300)
    except Exception:
        v = 300
    return max(30, v)


def _job_handler_timeout_seconds() -> int:
    try:
        v = int(getattr(settings, 'JOB_HANDLER_TIMEOUT_SECONDS', 600) or 600)
    except Exception:
        v = 600
    return max(30, v)


def _notify_enabled() -> bool:
    """Whether NOTIFY-driven wakes are enabled for the worker loop.

    When False the executor uses a tight polling loop with
    ``WORKER_FALLBACK_POLL_SECONDS`` and skips the LISTEN bridge entirely.
    Operators flip this to ``false`` as a one-line rollback if NOTIFY ever
    misbehaves in production.
    """
    return bool(getattr(settings, 'WORKER_NOTIFY_ENABLED', True))


def _fallback_poll_seconds() -> float:
    try:
        v = float(getattr(settings, 'WORKER_FALLBACK_POLL_SECONDS', 5) or 5)
    except Exception:
        v = 5.0
    return max(0.5, v)


def _on_jobs_notify(_payload: Optional[str]) -> None:
    """Notifier callback. Called for every NOTIFY 'placeholdarr_jobs' AND once on
    every reconnect (with payload=None) to force a drain after disconnects.

    Cheap by design: just flips the drain event. Executors do the real work.
    """
    _drain_event.set()


def _bridge_notifier_to_drain_event() -> None:
    """Register our drain-set callback with the shared Notifier exactly once per process."""
    global _listener_bridged
    if _listener_bridged:
        return
    try:
        from services.postgres.notifier import (
            JOBS_CHANNEL,
            start_shared_notifier,
        )
        notifier = start_shared_notifier()
        notifier.listen(JOBS_CHANNEL, _on_jobs_notify)
        _listener_bridged = True
        logger.info(
            f"Worker listener bridged to NOTIFY channel '{JOBS_CHANNEL}'",
            extra={'emoji_type': 'gear'},
        )
    except Exception as exc:
        logger.error(
            f"Failed to bridge worker to NOTIFY channel; falling back to safety-poll only: {exc}",
            extra={'emoji_type': 'error'},
        )


def _stale_claimed_reaper_loop() -> None:
    """Periodically reset stale CLAIMED jobs so a per-thread crash doesn't strand them.

    Designed to be cheap: runs once per WORKER_STALE_CLAIMED_REAP_INTERVAL_SECONDS,
    bounded by a tiny UPDATE WHERE updated_at < now() - <stale_reset>.
    """
    interval = _stale_claimed_reap_interval_seconds()
    stale_seconds = _stale_claimed_reset_seconds()
    while not _reaper_stop.wait(interval):
        try:
            session = get_session()
            try:
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
                # Reset stale CLAIMED back to PENDING; do NOT exceed max_attempts.
                # The next claim will increment attempts and the existing terminal
                # logic in _mark_job_failed will retire jobs that exhaust retries.
                stmt = text(
                    """
                    UPDATE job
                       SET status='PENDING', updated_at=now(), claim_token=NULL
                     WHERE status='CLAIMED'
                       AND updated_at < :cutoff
                    RETURNING id
                    """
                )
                result = session.execute(stmt, {'cutoff': cutoff})
                reaped_ids = [int(r[0]) for r in result.fetchall()]
                rowcount = len(reaped_ids)
                if rowcount > 0:
                    session.commit()
                    # Phase 4 cooperative-exit: mark every reaped job_id so a
                    # long-running handler can poll ``is_claim_revoked`` and
                    # bail out at the next safe point.
                    for jid in reaped_ids:
                        mark_claim_revoked(jid)
                    logger.warning(
                        f"Stale-CLAIMED reaper requeued {rowcount} job(s) older than {stale_seconds}s "
                        f"job_ids={reaped_ids} — in-flight worker threads may still be running old handlers; "
                        f"claim_revoked signal set for cooperative exit.",
                        extra={'emoji_type': 'warning'},
                    )
                    _drain_event.set()
                else:
                    session.rollback()
            finally:
                try:
                    session.close()
                except Exception:
                    pass
        except Exception as exc:
            logger.warning(
                f"Stale-CLAIMED reaper iteration failed: {exc}",
                extra={'emoji_type': 'warning'},
            )


def _start_stale_claimed_reaper() -> None:
    global _reaper_started
    if _reaper_started:
        return
    _reaper_stop.clear()
    t = threading.Thread(
        target=_stale_claimed_reaper_loop,
        name='worker-stale-reaper',
        daemon=True,
    )
    t.start()
    _reaper_started = True
    logger.info(
        f"Stale-CLAIMED reaper started (interval={_stale_claimed_reap_interval_seconds()}s, "
        f"reset_threshold={_stale_claimed_reset_seconds()}s)",
        extra={'emoji_type': 'gear'},
    )


def ensure_worker_runtime_started() -> None:
    """Idempotent startup of cross-thread worker runtime: notifier bridge + reaper.

    When ``WORKER_NOTIFY_ENABLED=false`` the notifier bridge is skipped so
    operators can roll back to a pure polling loop without touching code.
    """
    global _runtime_started
    with _runtime_lock:
        if _runtime_started:
            return
        if _notify_enabled():
            _bridge_notifier_to_drain_event()
        else:
            logger.info(
                "WORKER_NOTIFY_ENABLED=false — skipping LISTEN bridge; using "
                f"polling fallback every {_fallback_poll_seconds():.1f}s",
                extra={'emoji_type': 'warning'},
            )
        _start_stale_claimed_reaper()
        _runtime_started = True


def stop_worker_runtime() -> None:
    """Best-effort shutdown of background helpers (called from lifespan teardown)."""
    global _runtime_started, _reaper_started, _listener_bridged
    _reaper_stop.set()
    _runtime_started = False
    _reaper_started = False
    _listener_bridged = False


# ----------------------------------------------------------------
# Job claim + handler dispatch (unchanged contract; kept stable so Phase 1 is
# purely a wake-mechanism swap without behavioral changes for existing types).
# ----------------------------------------------------------------


def _claim_next_job() -> Optional[ClaimedJobDescriptor]:
    """Claim the next PENDING job in a short, self-contained transaction.

    Phase 1 of the holistic NOTIFY audit: the claim no longer shares a
    SQLAlchemy session with the handler. We open a session, run the
    SELECT FOR UPDATE + UPDATE, COMMIT, return a detached descriptor, and
    CLOSE the session so the connection returns to the pool BEFORE the
    handler does any work. A slow handler can no longer pin the connection
    used to claim it.
    """
    session = get_session()
    try:
        # Bound how long any single statement (and the COMMIT-time lock
        # waits) in this claim transaction may sit waiting for a heavyweight
        # lock. ``SET LOCAL`` is xact-scoped and cannot leak to other
        # sessions sharing the pool connection.
        timeout_s = int(getattr(settings, "WORKER_CLAIM_LOCK_TIMEOUT_SECONDS", 30) or 30)
        if timeout_s > 0:
            try:
                session.execute(text(f"SET LOCAL lock_timeout = '{int(timeout_s)}s'"))
            except Exception:
                pass

        now = datetime.now(timezone.utc)
        try:
            # Phase 5 priority-aware claim ordering: highest priority first,
            # then oldest scheduled run_after, then FIFO by id. ``priority``
            # defaults to 0 server-side so legacy rows enqueued before the
            # column existed sort below interactive work.
            job = (
                session.query(Job)
                .filter(
                    and_(
                        Job.status == 'PENDING',
                        (Job.run_after.is_(None) | (Job.run_after <= now)),
                    )
                )
                .order_by(
                    # COALESCE(priority, 0) so legacy rows (column added by
                    # the dynamic migration as nullable) sort as background.
                    func.coalesce(Job.priority, 0).desc(),
                    Job.run_after.asc().nullsfirst(),
                    Job.id.asc(),
                )
                .with_for_update(skip_locked=True)
                .first()
            )
        except OperationalError as exc:
            if _is_lock_timeout_error(exc):
                session.rollback()
                logger.warning(
                    "Worker claim SELECT FOR UPDATE hit lock_timeout "
                    f"({timeout_s}s) — DB contention; will retry on next wake",
                    extra={'emoji_type': 'warning'},
                )
                return None
            raise

        if not job:
            session.rollback()
            return None

        job.status = 'CLAIMED'
        job.attempts = int(job.attempts or 0) + 1
        job.updated_at = now
        job.claim_token = str(uuid.uuid4())
        session.add(job)
        hb = start_verbose_stall_heartbeat(
            f"worker.session.commit_claim job_id={job.id} job_type={job.job_type}",
            escalate_after_sec=60.0,
            escalate_message=(
                f"Stall heartbeat [worker.session.commit_claim job_id={job.id} "
                f"job_type={job.job_type}] blocked >60s — likely DB lock contention "
                "from a long-running transaction (calendar/sync), not external API; "
                f"lock_timeout={timeout_s}s safety net will retry"
            ),
        )
        try:
            session.commit()
        except OperationalError as exc:
            hb.set()
            if _is_lock_timeout_error(exc):
                session.rollback()
                logger.warning(
                    f"Worker claim COMMIT hit lock_timeout ({timeout_s}s) for "
                    f"job_id={job.id} job_type={job.job_type} — DB contention; "
                    "row will return to PENDING and retry on next wake",
                    extra={'emoji_type': 'warning'},
                )
                return None
            raise
        finally:
            hb.set()

        descriptor = ClaimedJobDescriptor(
            id=int(job.id),
            job_type=str(job.job_type or ''),
            claim_token=str(job.claim_token or ''),
            payload=dict(job.payload or {}),
            attempts=int(job.attempts or 0),
            max_attempts=int(job.max_attempts or 5),
        )
        logger.info(
            f"Worker claimed job_id={descriptor.id} job_type={descriptor.job_type} "
            f"claim_token={descriptor.claim_token[:8]}…",
            extra={'emoji_type': 'gear'},
        )
        return descriptor
    finally:
        try:
            session.close()
        except Exception:
            pass


def _reload_job_for_handler(session, descriptor: ClaimedJobDescriptor) -> Optional[Job]:
    """Reload the Job row in a fresh handler session, verifying the claim is still ours.

    If the stale-CLAIMED reaper has already requeued the row (or another
    worker subsequently re-claimed it), the claim_token will not match and
    we return ``None`` so the caller can skip this descriptor without
    risking a duplicate side effect or double-failing the row.
    """
    job = session.query(Job).filter(Job.id == descriptor.id).first()
    if job is None:
        return None
    current_token = str(getattr(job, 'claim_token', '') or '')
    if current_token != descriptor.claim_token:
        return None
    if str(getattr(job, 'status', '') or '') != 'CLAIMED':
        return None
    return job


def _process_webhook_event(session, job: Job):
    payload = job.payload or {}
    event_log_id = payload.get('event_log_id')
    if not event_log_id:
        raise ValueError('missing_event_log_id')

    event = session.query(EventLog).filter(EventLog.id == int(event_log_id)).first()
    if not event:
        # Event row missing: mark job done to avoid poison-looping.
        return

    # Correlates with JOB_HANDLER_TIMEOUT_SECONDS watchdog ERROR if a handler blocks (Plex/Radarr/DB).
    logger.info(
        f"Webhook handler start job_id={job.id} event_log_id={event_log_id} "
        f"event_type={str(getattr(event, 'event_type', '') or '').strip().lower() or '?'}",
        extra={'emoji_type': 'processing'},
    )

    event_type = str(getattr(event, 'event_type', '') or '').strip().lower()
    payload = event.payload if isinstance(event.payload, dict) else {}
    event_meta = payload.get('_event_meta') if isinstance(payload, dict) else None
    raw_event_type = None
    if isinstance(event_meta, dict):
        raw_event_type = event_meta.get('raw_event_type')

    dispatch_type = legacy_dispatch_event_type(event_type, raw_event_type)

    # Extract instance identifier from source string (e.g. "webhook:radarr_std" -> "radarr_std")
    source_str = getattr(event, 'source', '') or ''
    instance = None
    if source_str.startswith('webhook:'):
        instance = source_str.split(':', 1)[1].strip() or None

    handled = False
    stop_wh_hb = threading.Event()

    _wh_iv = max(3.0, float(getattr(settings, "STALL_HEARTBEAT_INTERVAL_SEC", 10.0) or 10.0))

    def _webhook_stall_heartbeat():
        """VERBOSE liveness while a handler runs (not INFO)."""
        while not stop_wh_hb.wait(_wh_iv):
            logger.verbose(
                f"Webhook handler still running job_id={int(job.id)} event_log_id={event_log_id} "
                f"event_type={event_type} dispatch_type={dispatch_type} instance={instance or 'unknown'}",
                extra={"emoji_type": "processing"},
            )

    _whb = threading.Thread(
        target=_webhook_stall_heartbeat,
        name=f"webhook-stall-hb-{int(job.id)}",
        daemon=True,
    )
    _whb.start()
    try:
        if event_type == "webhook_test" or dispatch_type == "test":
            logger.info(
                f"Processed webhook connectivity test event_log_id={event.id} instance={instance or 'unknown'}",
                extra={"emoji_type": "success"},
            )
            handled = True
        elif event_type in ("movie_grab", "episode_grab"):
            logger.info(
                f"Ignored ARR grab notification (informational) event_log_id={event.id} type={event_type} instance={instance or 'unknown'}",
                extra={"emoji_type": "info"},
            )
            handled = True
        elif dispatch_type == 'seriesadd':
            result = process_series_add_event(payload, instance=instance)
            logger.info(
                f"Processed seriesadd event_log_id={event.id} result={result.get('upsert_stats', {})}",
                extra={'emoji_type': 'success'},
            )
            handled = True
        elif dispatch_type in ('movieadd', 'movieadded'):
            result = process_movie_add_event(payload, instance=instance)
            logger.info(
                f"Processed {dispatch_type} event_log_id={event.id} movie_id={result.get('movie_id')}",
                extra={'emoji_type': 'success'},
            )
            handled = True
        elif event_type == 'movie_imported' or dispatch_type == 'moviefileimported':
            result = process_movie_imported_event(payload, instance=instance)
            logger.info(
                f"Processed movie_imported event_log_id={event.id} movie_id={result.get('movie_id')}",
                extra={'emoji_type': 'success'},
            )
            handled = True
        elif event_type == 'episode_imported' or dispatch_type in ('episodefileimported', 'download'):
            result = process_episode_imported_event(payload, instance=instance)
            logger.info(
                f"Processed episode_imported event_log_id={event.id} episodes={len(result.get('episode_ids') or [])}",
                extra={'emoji_type': 'success'},
            )
            handled = True
        elif event_type == 'movie_file_deleted' or dispatch_type == 'moviefiledelete':
            result = process_movie_file_deleted_event(payload, instance=instance)
            logger.info(
                f"Processed movie_file_deleted event_log_id={event.id} movie_id={result.get('movie_id')}",
                extra={'emoji_type': 'success'},
            )
            handled = True
        elif event_type == 'episode_file_deleted' or dispatch_type == 'episodefiledelete':
            result = process_episode_file_deleted_event(payload, instance=instance)
            logger.info(
                f"Processed episode_file_deleted event_log_id={event.id} episodes={len(result.get('episode_ids') or [])}",
                extra={'emoji_type': 'success'},
            )
            handled = True
        elif event_type == 'movie_deleted' or dispatch_type == 'moviedelete':
            result = process_movie_deleted_event(payload, instance=instance)
            logger.info(
                f"Processed movie_deleted event_log_id={event.id} movie_id={result.get('movie_id')}",
                extra={'emoji_type': 'success'},
            )
            handled = True
        elif event_type == 'series_deleted' or dispatch_type == 'seriesdelete':
            result = process_series_deleted_event(payload, instance=instance)
            logger.info(
                f"Processed series_deleted event_log_id={event.id} episodes={len(result.get('episode_ids') or [])}",
                extra={'emoji_type': 'success'},
            )
            handled = True
        elif event_type == 'playback_start' or dispatch_type in ('playback.start', 'playbackstart'):
            result = process_playback_start_event(payload, instance=instance)
            logger.info(
                f"Processed playback_start event_log_id={event.id} result={result}",
                extra={'emoji_type': 'success'},
            )
            handled = True

    finally:
        stop_wh_hb.set()

    if not handled:
        event.error_message = f'unhandled_event_type:{event_type}'
        logger.warning(
            f"Webhook event not yet handled event_log_id={event.id} canonical={event_type} raw={raw_event_type or 'unknown'}",
            extra={'emoji_type': 'warning'},
        )

    event.status = 'DONE'
    event.processed_at = datetime.now(timezone.utc)
    event.updated_at = datetime.now(timezone.utc)
    session.add(event)


def _is_non_retriable_webhook_error(error: Exception) -> bool:
    message = str(error or '').strip().lower()
    if not message:
        return False

    # Permanent payload-shape / validation failures will not improve by retrying
    # the same stored webhook payload.
    if message == 'missing_event_log_id':
        return True
    if message == 'unresolved_playback_media_type':
        return True
    if '_missing_' in message:
        return True
    return False


def _try_complete_task_runs_after_job_commit(
    descriptor: ClaimedJobDescriptor,
    *,
    failed: bool = False,
    error_message: str | None = None,
) -> None:
    """Close linked task runs after a follow-up job finish has committed."""
    if descriptor.job_type not in (PLACEHOLDER_ART_REFRESH_JOB_TYPE, NFO_REFRESH_JOB_TYPE):
        return
    payload = descriptor.payload if isinstance(descriptor.payload, dict) else {}
    raw_tid = payload.get("full_sync_task_run_id") or payload.get("art_backfill_task_run_id")
    if raw_tid is not None:
        try:
            from services.task_run_phases import try_complete_full_sync_task_run

            try_complete_full_sync_task_run(
                int(raw_tid),
                failed=bool(failed),
                error_message=str(error_message or "") or None,
            )
        except Exception as exc:
            logger.debug(
                f"full_sync completion check after job_id={descriptor.id} skipped: {exc}",
                extra={"emoji_type": "debug"},
            )
    raw_refresh_tid = payload.get("placeholder_refresh_task_run_id")
    if raw_refresh_tid is not None:
        try:
            from services.source_of_truth.placeholder_refresh import try_complete_placeholder_refresh_task_run

            try_complete_placeholder_refresh_task_run(
                int(raw_refresh_tid),
                failed=bool(failed),
                error_message=str(error_message or "") or None,
            )
        except Exception as exc:
            logger.debug(
                f"placeholder_refresh completion check after job_id={descriptor.id} skipped: {exc}",
                extra={"emoji_type": "debug"},
            )


def _mark_descriptor_done(session, descriptor: ClaimedJobDescriptor) -> None:
    """Mark a CLAIMED job DONE in a session, conditional on the claim_token.

    Phase 1 of the holistic NOTIFY audit: this finish step runs in its own
    session (separate from the handler's session) so handlers may release
    their connection before HTTP/disk work without blocking the finish
    transaction. The conditional UPDATE on (id, status, claim_token)
    preserves the stale-CLAIMED reaper's correctness guarantees: if the
    reaper requeued the row (or another worker stole it), this UPDATE
    affects 0 rows and we raise ``StaleJobClaimError`` so the caller can
    log + skip without overwriting state.
    """
    now = datetime.now(timezone.utc)
    res = session.execute(
        update(Job)
        .where(
            Job.id == descriptor.id,
            Job.status == 'CLAIMED',
            Job.claim_token == descriptor.claim_token,
        )
        .values(status='DONE', updated_at=now, claim_token=None, error_message=None)
    )
    if res.rowcount == 0:
        raise StaleJobClaimError(
            f"job_id={descriptor.id} claim_token no longer valid (reaper or other worker won the row)"
        )


def _mark_descriptor_failed(session, descriptor: ClaimedJobDescriptor, error: Exception) -> None:
    """Record a job failure in a session, conditional on the claim_token.

    Mirrors ``_mark_descriptor_done`` for the failure path: conditional Job
    UPDATE plus the matching EventLog bookkeeping for ``webhook_event``
    payloads (which still need their attempts/error_message bumped so the
    UI surfaces the failure even though the Job row carries the error too).
    """
    attempts = int(descriptor.attempts)
    max_attempts = int(descriptor.max_attempts)
    non_retriable = bool(
        descriptor.job_type == 'webhook_event' and _is_non_retriable_webhook_error(error)
    )
    new_status = 'FAILED' if (non_retriable or attempts >= max_attempts) else 'PENDING'
    err_msg = str(error)
    now = datetime.now(timezone.utc)
    res = session.execute(
        update(Job)
        .where(
            Job.id == descriptor.id,
            Job.status == 'CLAIMED',
            Job.claim_token == descriptor.claim_token,
        )
        .values(status=new_status, updated_at=now, claim_token=None, error_message=err_msg)
    )
    if res.rowcount == 0:
        raise StaleJobClaimError(
            f"job_id={descriptor.id} failed state not applied (stale claim)"
        )
    payload = descriptor.payload if isinstance(descriptor.payload, dict) else {}
    event_log_id = payload.get('event_log_id')
    if event_log_id:
        event = session.query(EventLog).filter(EventLog.id == int(event_log_id)).first()
        if event:
            event.attempts = int(event.attempts or 0) + 1
            event.error_message = err_msg
            event.updated_at = now
            if non_retriable or event.attempts >= int(event.max_attempts or 10):
                event.status = 'FAILED'
            session.add(event)


def _process_claimed_job(session, job: Job):
    """Dispatch a claimed Job to its handler.

    Phase 1 contract change: handlers no longer call ``_mark_job_done``. The
    worker's outer loop marks DONE in a fresh finish session AFTER this
    function returns successfully (and FAILED on raise). Handlers may
    therefore release their session before any external HTTP / disk I/O
    without complicating the finish-step transaction.
    """
    if job.job_type == 'webhook_event':
        _process_webhook_event(session, job)
        return

    if job.job_type == NFO_REFRESH_JOB_TYPE:
        result = process_nfo_refresh_job(session, job)
        if not result.get('ok', False):
            raise ValueError(str(result.get('reason') or 'nfo_refresh_failed'))
        return

    if job.job_type == PLACEHOLDER_ART_REFRESH_JOB_TYPE:
        result = process_placeholder_art_refresh_job(session, job)
        if not result.get('ok', False):
            raise ValueError(str(result.get('reason') or 'placeholder_art_refresh_failed'))
        return

    if job.job_type == ENTITY_MATERIALIZATION_JOB_TYPE:
        result = process_entity_materialization_job(session, job)
        if not result.get('ok', False):
            raise ValueError(str(result.get('reason') or 'entity_materialization_failed'))
        return

    if job.job_type == ENTITY_RECONCILE_JOB_TYPE:
        result = process_entity_reconcile_job(session, job)
        if not result.get('ok', False):
            raise ValueError(str(result.get('reason') or 'entity_reconcile_failed'))
        return

    if job.job_type == IMPORT_GRACE_JOB_TYPE:
        logger.debug(f"Processing import_grace job: job_id={getattr(job, 'id', '?')}, payload={getattr(job, 'payload', '?')}", extra={'emoji_type': 'debug'})
        result = process_import_grace_job(session, job)
        logger.debug(f"import_grace job result: job_id={getattr(job, 'id', '?')}, result={result}", extra={'emoji_type': 'debug'})
        if not result.get('ok', False):
            reason = str(result.get('reason') or 'import_grace_failed')
            logger.warning(f"import_grace job failed: job_id={getattr(job, 'id', '?')}, reason={reason}", extra={'emoji_type': 'warning'})
            raise ValueError(reason)
        logger.info(f"import_grace job completed: job_id={getattr(job, 'id', '?')}, phase={result.get('phase')}", extra={'emoji_type': 'success'})
        return

    if job.job_type == PLAYBACK_FALLBACK_JOB_TYPE:
        result = process_playback_fallback_job(session, job)
        if not result.get('ok', False):
            raise ValueError(str(result.get('reason') or 'playback_fallback_failed'))
        logger.info(
            f"Processed playback fallback job_id={getattr(job, 'id', '?')} result={result}",
            extra={'emoji_type': 'success'},
        )
        return

    # Phase 3: media_refresh handled here when registered.
    try:
        from services.source_of_truth.media_refresh_handler import (
            MEDIA_REFRESH_JOB_TYPE,
            process_media_refresh_job,
        )
    except Exception:
        MEDIA_REFRESH_JOB_TYPE = None
        process_media_refresh_job = None
    if MEDIA_REFRESH_JOB_TYPE is not None and job.job_type == MEDIA_REFRESH_JOB_TYPE:
        result = process_media_refresh_job(session, job)
        if not result.get('ok', False):
            raise ValueError(str(result.get('reason') or 'media_refresh_failed'))
        return

    # Phase 4: startup_sync_runner handled here when registered.
    try:
        from services.source_of_truth.startup_sync_job import (
            STARTUP_SYNC_RUNNER_JOB_TYPE,
            process_startup_sync_runner_job,
        )
    except Exception:
        STARTUP_SYNC_RUNNER_JOB_TYPE = None
        process_startup_sync_runner_job = None
    if STARTUP_SYNC_RUNNER_JOB_TYPE is not None and job.job_type == STARTUP_SYNC_RUNNER_JOB_TYPE:
        result = process_startup_sync_runner_job(session, job)
        if not result.get('ok', False):
            raise ValueError(str(result.get('reason') or 'startup_sync_runner_failed'))
        return

    logger.debug(f'Skipping unhandled job_type={job.job_type}', extra={'emoji_type': 'debug'})


# ----------------------------------------------------------------
# Per-job handler watchdog (FM-6 observability)
# ----------------------------------------------------------------


def _start_handler_watchdog(job_id: int, job_type: str) -> threading.Event:
    """Start a one-shot watchdog that logs an ERROR if a handler exceeds the timeout.

    Does NOT kill the thread; this is purely an observability hook so operators
    can see hung handlers before the stale-CLAIMED reaper requeues them.

    A firing watchdog means the handler is still running (often blocked on HTTP to a
    media server or on Postgres), not that long runtime is expected for that job type.
    """
    timeout = _job_handler_timeout_seconds()
    cancel = threading.Event()

    def _watchdog():
        if cancel.wait(timeout):
            return
        logger.error(
            f"Job handler still running after {timeout}s (watchdog; thread not stopped) "
            f"job_id={job_id} job_type={job_type} — "
            f"often blocked on media-server API or DB; check logs for "
            f"'Plex: loading full' / 'section.all() still in progress' or batch progress lines. "
            f"Increase JOB_HANDLER_TIMEOUT_SECONDS only if work is legitimately slower; "
            f"stale CLAIMED jobs reset via WORKER_STALE_CLAIMED_RESET_SECONDS.",
            extra={'emoji_type': 'error'},
        )

    t = threading.Thread(target=_watchdog, name=f'job-watchdog-{job_id}', daemon=True)
    t.start()
    return cancel


# ----------------------------------------------------------------
# Executor loops
# ----------------------------------------------------------------


def _process_one_descriptor(descriptor: ClaimedJobDescriptor) -> None:
    """Run handler + finish transaction in fresh sessions for one claimed job.

    Three connection lifecycle steps, each acquiring + releasing a pool slot:

    1. Handler session: opens, the handler does its DB reads/writes and
       commits its OWN business state, the worker closes it. Handlers MAY
       close this session early if they only need to do external HTTP or
       disk I/O afterwards (see Phase 1.2 handler audit).
    2. (External I/O happens here, with NO DB connection held if the
       handler released it.)
    3. Finish session: opens, marks the Job DONE/FAILED with a conditional
       UPDATE on (id, status, claim_token), commits, closes.

    Per-job duration + queue-wait observability is emitted in the finally
    block so operators can see which work classes dominate runtime.
    """
    started = time.monotonic()
    watchdog_cancel = _start_handler_watchdog(descriptor.id, descriptor.job_type)
    handler_outcome = "unknown"
    handler_error: Optional[Exception] = None

    handler_session = get_session()
    try:
        job = _reload_job_for_handler(handler_session, descriptor)
        if job is None:
            handler_outcome = "claim_revoked"
            logger.info(
                f"Worker descriptor abandoned (claim revoked) job_id={descriptor.id} "
                f"job_type={descriptor.job_type} — likely stale-CLAIMED reaper "
                "requeued the row before the handler could start",
                extra={'emoji_type': 'info'},
            )
            return

        # Phase 2 observability: log queue wait when payload carries enqueued_at.
        try:
            enq = descriptor.payload.get('enqueued_at') if isinstance(descriptor.payload, dict) else None
            if enq:
                queue_wait_s = max(0.0, time.time() - float(enq))
                logger.verbose(
                    f"Worker handler start job_id={descriptor.id} job_type={descriptor.job_type} "
                    f"queue_wait_s={queue_wait_s:.2f}",
                    extra={'emoji_type': 'processing'},
                )
        except Exception:
            pass

        try:
            _process_claimed_job(handler_session, job)
            hb_done = start_verbose_stall_heartbeat(
                f"worker.session.commit_after_job job_id={descriptor.id} job_type={descriptor.job_type}",
            )
            try:
                # Idempotent flush of any business state the handler added.
                # If the handler already closed the session we swallow the
                # ResourceClosedError; the handler is responsible for having
                # committed before close.
                handler_session.commit()
            except Exception as commit_err:
                msg = str(commit_err).lower()
                if 'closed' not in msg:
                    handler_error = commit_err
            finally:
                hb_done.set()
            if handler_error is None:
                handler_outcome = "done"
            else:
                handler_outcome = "failed"
        except Exception as exc:
            try:
                handler_session.rollback()
            except Exception:
                pass
            handler_error = exc
            handler_outcome = "failed"
    finally:
        try:
            handler_session.close()
        except Exception:
            pass

    # Step 3: mark DONE/FAILED in a fresh finish session. Even if the
    # handler released its session before HTTP, this small transaction is
    # still bounded by lock_timeout and observability is preserved.
    if handler_outcome in ("done", "failed"):
        finish_session = get_session()
        try:
            try:
                # Bound the finish-step lock waits the same way claim does
                # so a long-running transaction elsewhere cannot stall us.
                timeout_s = int(getattr(settings, "WORKER_CLAIM_LOCK_TIMEOUT_SECONDS", 30) or 30)
                if timeout_s > 0:
                    try:
                        finish_session.execute(text(f"SET LOCAL lock_timeout = '{int(timeout_s)}s'"))
                    except Exception:
                        pass

                if handler_error is None:
                    _mark_descriptor_done(finish_session, descriptor)
                else:
                    _mark_descriptor_failed(finish_session, descriptor, handler_error)
                    logger.error(
                        f'Worker job {descriptor.id} failed: {handler_error}',
                        extra={'emoji_type': 'error'},
                    )
                hb_fin = start_verbose_stall_heartbeat(
                    f"worker.session.commit_finish job_id={descriptor.id} "
                    f"job_type={descriptor.job_type} outcome={handler_outcome}",
                )
                try:
                    finish_session.commit()
                finally:
                    hb_fin.set()
                _try_complete_task_runs_after_job_commit(
                    descriptor,
                    failed=handler_error is not None,
                    error_message=str(handler_error) if handler_error is not None else None,
                )
            except StaleJobClaimError as stale:
                try:
                    finish_session.rollback()
                except Exception:
                    pass
                handler_outcome = "stale_claim"
                logger.info(
                    f'Worker skipped finish for job_id={descriptor.id} ({stale})',
                    extra={'emoji_type': 'info'},
                )
            except Exception as commit_err:
                try:
                    finish_session.rollback()
                except Exception:
                    pass
                logger.error(
                    f'Worker failed to record finish for job_id={descriptor.id}: {commit_err}',
                    extra={'emoji_type': 'error'},
                )
        finally:
            try:
                finish_session.close()
            except Exception:
                pass

    watchdog_cancel.set()
    # Phase 4: clear the cooperative-exit marker so a future re-claim of
    # the same row does not see a stale "revoked" flag.
    _clear_claim_revoked(descriptor.id)
    elapsed_s = time.monotonic() - started
    log_fn = logger.info if handler_outcome == "done" else logger.warning
    try:
        log_fn(
            f"job_done job_id={descriptor.id} job_type={descriptor.job_type} "
            f"outcome={handler_outcome} elapsed_s={elapsed_s:.3f}",
            extra={'emoji_type': 'info' if handler_outcome == 'done' else 'warning'},
        )
    except Exception:
        pass


def _drain_once() -> int:
    """Claim and process jobs one at a time until no more are claimable.

    Each iteration uses an isolated session for the claim AND an isolated
    session for the handler. A slow handler does not block other workers
    from claiming new work, and connections are returned to the pool
    between back-to-back jobs.
    """
    drained = 0
    # Cap how many jobs a single drain pass processes so the pool gets
    # breathing room between bursts. Other workers and a re-set
    # _drain_event will pick up the rest.
    try:
        max_per_drain = int(getattr(settings, 'WORKER_MAX_JOBS_PER_DRAIN', 50) or 50)
    except Exception:
        max_per_drain = 50
    max_per_drain = max(1, max_per_drain)
    while drained < max_per_drain:
        try:
            descriptor = _claim_next_job()
        except Exception as exc:
            logger.error(
                f'Worker claim iteration failed: {exc}',
                extra={'emoji_type': 'error'},
            )
            return drained
        if descriptor is None:
            return drained
        _process_one_descriptor(descriptor)
        drained += 1
    return drained


def _next_run_after_seconds() -> Optional[float]:
    """Return seconds until the soonest pending future-scheduled job, or None.

    Uses a short-lived session so we never pin a connection while waiting on
    NOTIFY. Returns ``None`` if no future-scheduled work is known; caller
    will fall back to the safety-poll interval.
    """
    session = get_session()
    try:
        try:
            row = session.execute(
                text(
                    "SELECT EXTRACT(EPOCH FROM (MIN(run_after) - now())) "
                    "FROM job WHERE status='PENDING' AND run_after IS NOT NULL AND run_after > now()"
                )
            ).first()
        except Exception:
            session.rollback()
            return None
        if not row or row[0] is None:
            return None
        try:
            return max(0.0, float(row[0]))
        except Exception:
            return None
    finally:
        try:
            session.close()
        except Exception:
            pass


def _executor_loop():
    """Job executor: drain on NOTIFY (and safety-poll timeout), with run_after-aware waits.

    When ``WORKER_NOTIFY_ENABLED=false`` the loop falls back to a tight
    polling interval (``WORKER_FALLBACK_POLL_SECONDS``) and ignores the
    drain event. This is the documented one-line rollback path if NOTIFY
    ever wedges in production.
    """
    from services.startup_gate import startup_sync_complete

    if not startup_sync_complete.is_set():
        logger.info(
            'Worker holding: waiting for startup sync to complete...',
            extra={'emoji_type': 'info'},
        )
        startup_sync_complete.wait(timeout=1800)

    notify_on = _notify_enabled()
    if notify_on:
        logger.info('Worker executor loop started (NOTIFY mode)', extra={'emoji_type': 'gear'})
    else:
        logger.info(
            f"Worker executor loop started (POLLING mode, every {_fallback_poll_seconds():.1f}s)",
            extra={'emoji_type': 'gear'},
        )

    safety = _safety_poll_seconds()
    fallback = _fallback_poll_seconds()
    while True:
        try:
            # Phase 3.1 drain-race fix: clear the wake event BEFORE draining
            # so any NOTIFY that arrives while we drain re-sets it and forces
            # another immediate pass. Without this, a NOTIFY between the last
            # claim and the clear() would be lost until safety_poll fires.
            _drain_event.clear()
            try:
                _drain_once()
            except Exception as exc:
                logger.error(
                    f'Worker drain iteration failed: {exc}',
                    extra={'emoji_type': 'error'},
                )

            try:
                next_seconds = _next_run_after_seconds()
            except Exception:
                next_seconds = None

            if notify_on:
                wait_timeout = safety
                if next_seconds is not None:
                    wait_timeout = min(safety, max(0.1, float(next_seconds) + 0.05))
                _drain_event.wait(timeout=wait_timeout)
            else:
                # Polling fallback: shorter floor than safety_poll because we
                # have no NOTIFY-driven wake to compensate for the gap.
                wait_timeout = fallback
                if next_seconds is not None:
                    wait_timeout = min(fallback, max(0.1, float(next_seconds) + 0.05))
                time.sleep(wait_timeout)
        except Exception as exc:
            logger.error(
                f'Worker executor loop iteration failed: {exc}',
                extra={'emoji_type': 'error'},
            )
            time.sleep(1.0)


def run_loop() -> None:
    """Worker thread entry: LISTEN/NOTIFY + safety poll (see ``_executor_loop``)."""
    # Notifier bridge + stale-CLAIMED reaper before blocking on work. Idempotent.
    try:
        ensure_worker_runtime_started()
    except Exception as exc:
        logger.warning(
            f"Failed to start worker runtime helpers: {exc}",
            extra={'emoji_type': 'warning'},
        )

    _executor_loop()
