import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, text, update

from core.config import settings
from core.logger import logger, start_verbose_stall_heartbeat
from services.event_normalization import legacy_dispatch_event_type
from services.postgres.db import get_session
from services.postgres.models import EventLog, Job
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


def _use_notify_loop() -> bool:
    return bool(getattr(settings, "USE_NOTIFY_WORKER_LOOP", True))


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
                    logger.warning(
                        f"Stale-CLAIMED reaper requeued {rowcount} job(s) older than {stale_seconds}s "
                        f"job_ids={reaped_ids} — in-flight worker threads may still be running old handlers; "
                        f"see NOTIFY worker audit report for duplicate-claim risk.",
                        extra={'emoji_type': 'warning'},
                    )
                    # Wake executor loops immediately; NOTIFY from this UPDATE may not reach our
                    # LISTEN connection the same way as app-driven job inserts.
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
    """Idempotent startup of cross-thread worker runtime: notifier bridge + reaper."""
    global _runtime_started
    with _runtime_lock:
        if _runtime_started:
            return
        if _use_notify_loop():
            _bridge_notifier_to_drain_event()
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


def _claim_next_job(session):
    now = datetime.now(timezone.utc)
    job = (
        session.query(Job)
        .filter(
            and_(
                Job.status == 'PENDING',
                (Job.run_after.is_(None) | (Job.run_after <= now)),
            )
        )
        .order_by(Job.run_after.asc().nullsfirst(), Job.id.asc())
        .with_for_update(skip_locked=True)
        .first()
    )
    if not job:
        return None
    job.status = 'CLAIMED'
    job.attempts = int(job.attempts or 0) + 1
    job.updated_at = now
    job.claim_token = str(uuid.uuid4())
    session.add(job)
    hb = start_verbose_stall_heartbeat(
        f"worker.session.commit_claim job_id={job.id} job_type={job.job_type}",
    )
    try:
        session.commit()
    finally:
        hb.set()
    logger.info(
        f"Worker claimed job_id={job.id} job_type={job.job_type} claim_token={job.claim_token[:8]}…",
        extra={'emoji_type': 'gear'},
    )
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


def _mark_job_done(session, job: Job):
    """Mark job DONE; uses claim_token so stale reaper cannot be overwritten by a late commit."""
    now = datetime.now(timezone.utc)
    jid = int(job.id)
    token = getattr(job, 'claim_token', None)
    if token:
        res = session.execute(
            update(Job)
            .where(
                Job.id == jid,
                Job.status == 'CLAIMED',
                Job.claim_token == token,
            )
            .values(status='DONE', updated_at=now, claim_token=None, error_message=None)
        )
        if res.rowcount == 0:
            raise StaleJobClaimError(
                f"job_id={jid} claim_token no longer valid (reaper or other worker won the row)"
            )
        session.expire(job)
        return
    job.status = 'DONE'
    job.updated_at = now
    job.claim_token = None
    session.add(job)


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


def _mark_job_failed(session, job: Job, error: Exception):
    attempts = int(job.attempts or 0)
    max_attempts = int(job.max_attempts or 5)
    non_retriable = bool(job.job_type == 'webhook_event' and _is_non_retriable_webhook_error(error))
    now = datetime.now(timezone.utc)
    jid = int(job.id)
    token = getattr(job, 'claim_token', None)
    err_msg = str(error)
    if token:
        new_status = 'FAILED' if (non_retriable or attempts >= max_attempts) else 'PENDING'
        res = session.execute(
            update(Job)
            .where(
                Job.id == jid,
                Job.status == 'CLAIMED',
                Job.claim_token == token,
            )
            .values(status=new_status, updated_at=now, claim_token=None, error_message=err_msg)
        )
        if res.rowcount == 0:
            raise StaleJobClaimError(f"job_id={jid} failed state not applied (stale claim)")
        session.expire(job)
    else:
        job.error_message = err_msg
        job.updated_at = now
        if non_retriable or attempts >= max_attempts:
            job.status = 'FAILED'
        else:
            job.status = 'PENDING'
        session.add(job)

    payload = job.payload or {}
    event_log_id = payload.get('event_log_id')
    if event_log_id:
        event = session.query(EventLog).filter(EventLog.id == int(event_log_id)).first()
        if event:
            event.attempts = int(event.attempts or 0) + 1
            event.error_message = str(error)
            event.updated_at = datetime.now(timezone.utc)
            if non_retriable or event.attempts >= int(event.max_attempts or 10):
                event.status = 'FAILED'
            session.add(event)


def _process_claimed_job(session, job: Job):
    if job.job_type == 'webhook_event':
        _process_webhook_event(session, job)
        _mark_job_done(session, job)
        return


    if job.job_type == NFO_REFRESH_JOB_TYPE:
        result = process_nfo_refresh_job(session, job)
        if not result.get('ok', False):
            raise ValueError(str(result.get('reason') or 'nfo_refresh_failed'))
        _mark_job_done(session, job)
        return

    if job.job_type == ENTITY_MATERIALIZATION_JOB_TYPE:
        result = process_entity_materialization_job(session, job)
        if not result.get('ok', False):
            raise ValueError(str(result.get('reason') or 'entity_materialization_failed'))
        _mark_job_done(session, job)
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
        _mark_job_done(session, job)
        return

    if job.job_type == PLAYBACK_FALLBACK_JOB_TYPE:
        result = process_playback_fallback_job(session, job)
        if not result.get('ok', False):
            raise ValueError(str(result.get('reason') or 'playback_fallback_failed'))
        logger.info(
            f"Processed playback fallback job_id={getattr(job, 'id', '?')} result={result}",
            extra={'emoji_type': 'success'},
        )
        _mark_job_done(session, job)
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
        _mark_job_done(session, job)
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
        _mark_job_done(session, job)
        return

    # Keep unknown job types non-blocking while rebuild is in progress.
    logger.debug(f'Skipping unhandled job_type={job.job_type}', extra={'emoji_type': 'debug'})
    _mark_job_done(session, job)


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


def _drain_once(session) -> int:
    """Claim and process jobs until no more are claimable. Returns count drained."""
    drained = 0
    while True:
        try:
            job = _claim_next_job(session)
        except Exception:
            session.rollback()
            raise
        if not job:
            return drained

        watchdog_cancel = _start_handler_watchdog(int(job.id), str(job.job_type))
        try:
            _process_claimed_job(session, job)
            hb_done = start_verbose_stall_heartbeat(
                f"worker.session.commit_after_job job_id={job.id} job_type={job.job_type}",
            )
            try:
                session.commit()
            finally:
                hb_done.set()
            drained += 1
        except StaleJobClaimError as stale:
            session.rollback()
            logger.info(
                f'Worker skipped commit for job_id={job.id} ({stale})',
                extra={'emoji_type': 'info'},
            )
        except Exception as e:
            session.rollback()
            jid = int(job.id)
            job_row = session.query(Job).filter(Job.id == jid).first()
            try:
                if job_row is not None and str(job_row.status or '') == 'CLAIMED':
                    _mark_job_failed(session, job_row, e)
                    hb_fail = start_verbose_stall_heartbeat(
                        f"worker.session.commit_failure job_id={jid} job_type={getattr(job_row, 'job_type', '?')}",
                    )
                    try:
                        session.commit()
                    finally:
                        hb_fail.set()
                else:
                    logger.warning(
                        f'Worker could not record failure for job_id={jid} (row missing or not CLAIMED)',
                        extra={'emoji_type': 'warning'},
                    )
            except StaleJobClaimError:
                session.rollback()
                logger.info(
                    f'Worker skipped failure recording for job_id={jid} (stale claim)',
                    extra={'emoji_type': 'info'},
                )
            except Exception as commit_err:
                session.rollback()
                logger.error(
                    f'Worker failed to record job failure for job_id={jid}: {commit_err}',
                    extra={'emoji_type': 'error'},
                )
            logger.error(
                f'Worker job {jid} failed: {e}',
                extra={'emoji_type': 'error'},
            )
        finally:
            watchdog_cancel.set()


def _next_run_after_seconds(session) -> Optional[float]:
    """Return seconds until the soonest pending future-scheduled job, or None.

    None signals "no future-scheduled work known"; caller will fall back to the
    safety-poll interval.
    """
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


def _executor_notify_loop():
    """NOTIFY-driven executor body. Used when USE_NOTIFY_WORKER_LOOP is on."""
    from services.startup_gate import startup_sync_complete

    if not startup_sync_complete.is_set():
        logger.info(
            'Worker holding: waiting for startup sync to complete...',
            extra={'emoji_type': 'info'},
        )
        startup_sync_complete.wait(timeout=1800)
    logger.info('Worker NOTIFY loop started', extra={'emoji_type': 'gear'})

    safety = _safety_poll_seconds()
    while True:
        session = get_session()
        try:
            # Drain anything currently runnable.
            try:
                _drain_once(session)
            except Exception as e:
                logger.error(
                    f'Worker drain iteration failed: {e}',
                    extra={'emoji_type': 'error'},
                )
                try:
                    session.rollback()
                except Exception:
                    pass

            # Compute how long we may sleep without missing scheduled work.
            try:
                next_seconds = _next_run_after_seconds(session)
            except Exception:
                next_seconds = None

            wait_timeout = safety
            if next_seconds is not None:
                wait_timeout = min(safety, max(0.1, float(next_seconds) + 0.05))

            # Consume any prior wake signal so wait() blocks until a fresh one
            # arrives (or the timeout hits). New NOTIFYs during drain re-set the
            # event; new NOTIFYs while we wait wake us immediately.
            _drain_event.clear()
            session.close()
            session = None
            _drain_event.wait(timeout=wait_timeout)
        except Exception as e:
            logger.error(
                f'Worker executor loop iteration failed: {e}',
                extra={'emoji_type': 'error'},
            )
            time.sleep(1.0)
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass


def _executor_polling_loop(poll_interval_seconds: float):
    """Legacy polling executor body. Used when USE_NOTIFY_WORKER_LOOP is off."""
    from services.startup_gate import startup_sync_complete

    if not startup_sync_complete.is_set():
        logger.info(
            'Worker holding: waiting for startup sync to complete...',
            extra={'emoji_type': 'info'},
        )
        startup_sync_complete.wait(timeout=1800)
    logger.info('Worker polling loop started', extra={'emoji_type': 'gear'})
    while True:
        session = get_session()
        try:
            job = _claim_next_job(session)
            if not job:
                session.close()
                time.sleep(max(0.25, poll_interval_seconds))
                continue

            watchdog_cancel = _start_handler_watchdog(int(job.id), str(job.job_type))
            try:
                _process_claimed_job(session, job)
                session.commit()
            except StaleJobClaimError as stale:
                session.rollback()
                logger.info(
                    f'Worker skipped commit for job_id={job.id} ({stale})',
                    extra={'emoji_type': 'info'},
                )
            except Exception as e:
                session.rollback()
                jid = int(job.id)
                job_row = session.query(Job).filter(Job.id == jid).first()
                try:
                    if job_row is not None and str(job_row.status or '') == 'CLAIMED':
                        _mark_job_failed(session, job_row, e)
                        session.commit()
                    else:
                        logger.warning(
                            f'Worker could not record failure for job_id={jid} (row missing or not CLAIMED)',
                            extra={'emoji_type': 'warning'},
                        )
                except StaleJobClaimError:
                    session.rollback()
                    logger.info(
                        f'Worker skipped failure recording for job_id={jid} (stale claim)',
                        extra={'emoji_type': 'info'},
                    )
                logger.error(f'Worker job {jid} failed: {e}', extra={'emoji_type': 'error'})
            finally:
                watchdog_cancel.set()
        except Exception as e:
            try:
                session.rollback()
            except Exception:
                pass
            logger.error(f'Worker loop iteration failed: {e}', extra={'emoji_type': 'error'})
            time.sleep(max(0.25, poll_interval_seconds))
        finally:
            try:
                session.close()
            except Exception:
                pass


def run_loop(poll_interval_seconds: float = 2.0):
    """Worker entrypoint. Routes to the NOTIFY-driven loop when USE_NOTIFY_WORKER_LOOP
    is enabled, else falls back to the legacy 2s-polling loop.

    Either path preserves the startup_sync_complete gate, FOR UPDATE SKIP LOCKED
    claim semantics, and the existing handler dispatch table.
    """
    # Make sure the cross-process helpers (notifier bridge + stale-CLAIMED reaper)
    # are running before this thread blocks on work. Idempotent.
    try:
        ensure_worker_runtime_started()
    except Exception as exc:
        logger.warning(
            f"Failed to start worker runtime helpers: {exc}",
            extra={'emoji_type': 'warning'},
        )

    if _use_notify_loop():
        _executor_notify_loop()
    else:
        _executor_polling_loop(poll_interval_seconds=poll_interval_seconds)
