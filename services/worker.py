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
from services.postgres.db import get_session, set_db_pool_role
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
from services.source_of_truth.poster_language_job import (
    POSTER_LANGUAGE_RESOLVE_JOB_TYPE,
    process_poster_language_resolve_job,
)
from services.source_of_truth.status_reconciler import (
    NFO_REFRESH_JOB_TYPE,
    process_nfo_refresh_job,
)


class StaleJobClaimError(Exception):
    """Raised when completing a job row would overwrite state after stale-CLAIMED reaper reset."""


class JobDeferredError(Exception):
    """Handler asks to leave the job PENDING for a later retry without failing it."""

    def __init__(self, reason: str, *, delay_seconds: int = 60):
        super().__init__(reason)
        self.reason = str(reason or "deferred")
        self.delay_seconds = max(5, int(delay_seconds or 60))


# ----------------------------------------------------------------
# NOTIFY/LISTEN-driven worker coordination
# ----------------------------------------------------------------

# Set by the shared notifier callback (or a startup self-test) when there may be
# work to drain. Cleared by the first executor to wake; subsequent NOTIFYs while
# any executor is mid-drain re-set it so a fresh wait() returns immediately.
_drain_event = threading.Event()


# Phase 4 cooperative-exit registry: keyed by (job_id, claim_token) so a reaper
# revoke cannot cancel a freshly reclaimed run of the same job_id.
_revoked_claims_lock = threading.Lock()
_revoked_claims: set[tuple[int, str]] = set()


def mark_claim_revoked(job_id: int, claim_token: str | None = None) -> None:
    """Signal that a specific claim has been revoked (e.g. by the reaper)."""
    try:
        token = str(claim_token or "").strip()
        if not token:
            return
        with _revoked_claims_lock:
            _revoked_claims.add((int(job_id), token))
    except Exception:
        pass


def is_claim_revoked(job_id: int, claim_token: str | None = None) -> bool:
    """Return True if this exact claim was revoked.

    Callers that only pass job_id (legacy) still work when a single token is
    registered for that id; prefer passing claim_token.
    """
    try:
        jid = int(job_id)
        token = str(claim_token or "").strip()
        with _revoked_claims_lock:
            if token:
                return (jid, token) in _revoked_claims
            return any(k[0] == jid for k in _revoked_claims)
    except Exception:
        return False


def _clear_claim_revoked(job_id: int, claim_token: str | None = None) -> None:
    try:
        jid = int(job_id)
        token = str(claim_token or "").strip()
        with _revoked_claims_lock:
            if token:
                _revoked_claims.discard((jid, token))
            else:
                for key in list(_revoked_claims):
                    if key[0] == jid:
                        _revoked_claims.discard(key)
    except Exception:
        pass


# Job types that may pin many workers; claim skips when active CLAIMED count >= cap.
_CLAIM_CONCURRENCY_CAPS: dict[str, int] = {
    "startup_sync_runner": 1,
    "full_sync": 1,
    "entity_reconcile": 2,
    "media_refresh": 2,
    "nfo_refresh": 2,
    "placeholder_art_refresh": 2,
    "poster_language_resolve": 2,
    # Large series-add mats amplify episode FS; keep concurrency low and rely on coalesce.
    "entity_materialization": 2,
}

# Tighter global cap when observation_source is a large series-add storm.
_ENTITY_MAT_SERIES_ADD_CLAIM_CAP = 1
_LARGE_EVENT_MAT_SOURCES = frozenset({"event_series_add"})
_INTERACTIVE_JOB_TYPES = frozenset(
    {
        "webhook_event",
        "import_grace",
        "playback_fallback",
    }
)


def _worker_count() -> int:
    try:
        return max(1, int(getattr(settings, "WORKER_COUNT", 4) or 4))
    except Exception:
        return 4

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
    set_db_pool_role("worker")
    while not _reaper_stop.wait(interval):
        try:
            session = get_session()
            try:
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
                # Fail rows that already exhausted attempts while CLAIMED.
                fail_stmt = text(
                    """
                    UPDATE job
                       SET status='FAILED',
                           updated_at=now(),
                           claim_token=NULL,
                           error_message=COALESCE(error_message, 'max_attempts_exhausted_while_claimed')
                     WHERE status='CLAIMED'
                       AND updated_at < :cutoff
                       AND attempts >= GREATEST(COALESCE(max_attempts, 1), 1)
                    RETURNING id
                    """
                )
                failed_ids = [int(r[0]) for r in session.execute(fail_stmt, {"cutoff": cutoff}).fetchall()]
                # Capture claim_token before clearing so revoke is claim-scoped.
                select_stmt = text(
                    """
                    SELECT id, claim_token
                      FROM job
                     WHERE status='CLAIMED'
                       AND updated_at < :cutoff
                       AND attempts < GREATEST(COALESCE(max_attempts, 1), 1)
                    """
                )
                stale_rows = list(session.execute(select_stmt, {"cutoff": cutoff}).fetchall())
                reaped_ids: list[int] = []
                reaped_tokens: list[tuple[int, str]] = []
                if stale_rows:
                    ids = [int(r[0]) for r in stale_rows]
                    for r in stale_rows:
                        jid = int(r[0])
                        tok = str(r[1] or "").strip()
                        if tok:
                            reaped_tokens.append((jid, tok))
                        reaped_ids.append(jid)
                    (
                        session.query(Job)
                        .filter(Job.id.in_(ids), Job.status == "CLAIMED")
                        .update(
                            {
                                Job.status: "PENDING",
                                Job.updated_at: datetime.now(timezone.utc),
                                Job.claim_token: None,
                            },
                            synchronize_session=False,
                        )
                    )
                if reaped_ids or failed_ids:
                    session.commit()
                    for jid, tok in reaped_tokens:
                        mark_claim_revoked(jid, tok)
                    if failed_ids:
                        logger.warning(
                            f"Stale-CLAIMED reaper failed {len(failed_ids)} job(s) at max_attempts "
                            f"job_ids={failed_ids}",
                            extra={"emoji_type": "warning"},
                        )
                    if reaped_ids:
                        logger.warning(
                            f"Stale-CLAIMED reaper requeued {len(reaped_ids)} job(s) older than {stale_seconds}s "
                            f"job_ids={reaped_ids} — in-flight worker threads may still be running old handlers; "
                            f"claim_revoked signal set for cooperative exit.",
                            extra={"emoji_type": "warning"},
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
        # Retire PENDING poison pills that already hit max_attempts.
        try:
            exhausted = (
                session.query(Job)
                .filter(
                    Job.status == "PENDING",
                    Job.attempts >= func.greatest(func.coalesce(Job.max_attempts, 1), 1),
                    (Job.run_after.is_(None) | (Job.run_after <= now)),
                )
                .limit(25)
                .all()
            )
            for row in exhausted:
                row.status = "FAILED"
                row.updated_at = now
                row.error_message = str(row.error_message or "max_attempts_exhausted")
                session.add(row)
            if exhausted:
                session.commit()
                if timeout_s > 0:
                    try:
                        session.execute(text(f"SET LOCAL lock_timeout = '{int(timeout_s)}s'"))
                    except Exception:
                        pass
        except Exception:
            try:
                session.rollback()
            except Exception:
                pass

        job = None
        try:
            # Priority-aware claim with per-type concurrency caps. Interactive
            # types are never capped here (UX-first).
            #
            # Lock ONE candidate at a time. A prior ``LIMIT 20 … FOR UPDATE
            # SKIP LOCKED`` held locks on a whole window, so parallel workers
            # jumped to jobs 21 / 41 / … and art batch bars looked scrambled.
            pending_filter = and_(
                Job.status == "PENDING",
                (Job.run_after.is_(None) | (Job.run_after <= now)),
                Job.attempts < func.greatest(func.coalesce(Job.max_attempts, 1), 1),
            )
            claim_order = (
                func.coalesce(Job.priority, 0).desc(),
                Job.run_after.asc().nullsfirst(),
                Job.id.asc(),
            )
            # Reserve at least one worker for short interactive types when any
            # interactive PENDING exists and non-interactive CLAIMED already fill
            # worker_count - 1.
            interactive_pending = (
                session.query(Job.id)
                .filter(pending_filter, Job.job_type.in_(tuple(_INTERACTIVE_JOB_TYPES)))
                .limit(1)
                .first()
                is not None
            )
            non_interactive_claimed = 0
            if interactive_pending:
                non_interactive_claimed = int(
                    session.query(func.count(Job.id))
                    .filter(
                        Job.status == "CLAIMED",
                        ~Job.job_type.in_(tuple(_INTERACTIVE_JOB_TYPES)),
                    )
                    .scalar()
                    or 0
                )
            reserve_interactive = interactive_pending and non_interactive_claimed >= max(
                1, _worker_count() - 1
            )

            excluded_ids: list[int] = []
            for _ in range(40):
                nested = session.begin_nested()
                try:
                    q = session.query(Job).filter(pending_filter).order_by(*claim_order)
                    if excluded_ids:
                        q = q.filter(~Job.id.in_(excluded_ids))
                    cand = q.with_for_update(skip_locked=True).limit(1).first()
                    if not cand:
                        nested.rollback()
                        break
                    jtype = str(cand.job_type or "")
                    skip = False
                    if reserve_interactive and jtype not in _INTERACTIVE_JOB_TYPES:
                        skip = True
                    else:
                        cap = _CLAIM_CONCURRENCY_CAPS.get(jtype)
                        if cap is not None and jtype not in _INTERACTIVE_JOB_TYPES:
                            active = (
                                session.query(func.count(Job.id))
                                .filter(Job.job_type == jtype, Job.status == "CLAIMED")
                                .scalar()
                            )
                            if int(active or 0) >= int(cap):
                                skip = True
                    if skip:
                        excluded_ids.append(int(cand.id))
                        nested.rollback()
                        continue
                    if jtype == "entity_materialization":
                        cand_payload = cand.payload if isinstance(cand.payload, dict) else {}
                        cand_obs = str(cand_payload.get("observation_source") or "").strip()
                        if cand_obs in _LARGE_EVENT_MAT_SOURCES:
                            series_add_claimed = 0
                            for row in (
                                session.query(Job.payload)
                                .filter(
                                    Job.job_type == "entity_materialization",
                                    Job.status == "CLAIMED",
                                )
                                .all()
                            ):
                                pl = row[0] if row else None
                                if isinstance(pl, dict) and str(pl.get("observation_source") or "").strip() in _LARGE_EVENT_MAT_SOURCES:
                                    series_add_claimed += 1
                            if series_add_claimed >= int(_ENTITY_MAT_SERIES_ADD_CLAIM_CAP):
                                excluded_ids.append(int(cand.id))
                                nested.rollback()
                                continue
                    # RELEASE SAVEPOINT keeps the row lock for the claim UPDATE.
                    nested.commit()
                    job = cand
                    break
                except Exception:
                    try:
                        nested.rollback()
                    except Exception:
                        pass
                    raise
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
    from services.postgres.db import release_handler_session, session_scope

    payload = job.payload or {}
    event_log_id = payload.get('event_log_id')
    if not event_log_id:
        raise ValueError('missing_event_log_id')

    event = session.query(EventLog).filter(EventLog.id == int(event_log_id)).first()
    if not event:
        # Event row missing: mark job done to avoid poison-looping.
        release_handler_session(session)
        return

    job_id = int(job.id) if getattr(job, "id", None) is not None else None
    event_id = int(event.id)
    # Correlates with JOB_HANDLER_TIMEOUT_SECONDS watchdog ERROR if a handler blocks (Plex/Radarr/DB).
    logger.info(
        f"Webhook handler start job_id={job_id} event_log_id={event_log_id} "
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

    # Nested event handlers open their own sessions; do not pin this claim session across Arr/Plex.
    release_handler_session(session)

    handled = False
    unhandled_error: str | None = None
    stop_wh_hb = threading.Event()

    _wh_iv = max(3.0, float(getattr(settings, "STALL_HEARTBEAT_INTERVAL_SEC", 10.0) or 10.0))

    def _webhook_stall_heartbeat():
        """VERBOSE liveness while a handler runs (not INFO)."""
        while not stop_wh_hb.wait(_wh_iv):
            logger.verbose(
                f"Webhook handler still running job_id={job_id} event_log_id={event_log_id} "
                f"event_type={event_type} dispatch_type={dispatch_type} instance={instance or 'unknown'}",
                extra={"emoji_type": "processing"},
            )

    _whb = threading.Thread(
        target=_webhook_stall_heartbeat,
        name=f"webhook-stall-hb-{job_id}",
        daemon=True,
    )
    _whb.start()
    try:
        if event_type == "webhook_test" or dispatch_type == "test":
            logger.info(
                f"Processed webhook connectivity test event_log_id={event_id} instance={instance or 'unknown'}",
                extra={"emoji_type": "success"},
            )
            handled = True
        elif event_type in ("movie_grab", "episode_grab", "webhook_ignored"):
            logger.info(
                f"Ignored informational webhook event_log_id={event_id} type={event_type} instance={instance or 'unknown'}",
                extra={"emoji_type": "info"},
            )
            handled = True
        elif dispatch_type == 'seriesadd':
            result = process_series_add_event(payload, instance=instance)
            logger.info(
                f"Processed seriesadd event_log_id={event_id} result={result.get('upsert_stats', {})}",
                extra={'emoji_type': 'success'},
            )
            handled = True
        elif dispatch_type in ('movieadd', 'movieadded'):
            result = process_movie_add_event(payload, instance=instance)
            logger.info(
                f"Processed {dispatch_type} event_log_id={event_id} movie_id={result.get('movie_id')}",
                extra={'emoji_type': 'success'},
            )
            handled = True
        elif event_type == 'movie_imported' or dispatch_type == 'moviefileimported':
            try:
                result = process_movie_imported_event(payload, instance=instance)
            except ValueError as exc:
                # Catalog row not ready yet (common during ARR full sync / wipe rebuild).
                if 'movieimport_not_found:' in str(exc):
                    raise JobDeferredError(str(exc), delay_seconds=30) from exc
                raise
            logger.info(
                f"Processed movie_imported event_log_id={event_id} movie_id={result.get('movie_id')}",
                extra={'emoji_type': 'success'},
            )
            handled = True
        elif event_type == 'episode_imported' or dispatch_type in ('episodefileimported', 'download'):
            try:
                result = process_episode_imported_event(payload, instance=instance)
            except ValueError as exc:
                if 'episodeimport_not_found:' in str(exc):
                    raise JobDeferredError(str(exc), delay_seconds=30) from exc
                raise
            logger.info(
                f"Processed episode_imported event_log_id={event_id} episodes={len(result.get('episode_ids') or [])}",
                extra={'emoji_type': 'success'},
            )
            handled = True
        elif event_type == 'movie_file_deleted' or dispatch_type == 'moviefiledelete':
            result = process_movie_file_deleted_event(payload, instance=instance)
            logger.info(
                f"Processed movie_file_deleted event_log_id={event_id} movie_id={result.get('movie_id')}",
                extra={'emoji_type': 'success'},
            )
            handled = True
        elif event_type == 'episode_file_deleted' or dispatch_type == 'episodefiledelete':
            result = process_episode_file_deleted_event(payload, instance=instance)
            logger.info(
                f"Processed episode_file_deleted event_log_id={event_id} episodes={len(result.get('episode_ids') or [])}",
                extra={'emoji_type': 'success'},
            )
            handled = True
        elif event_type == 'movie_deleted' or dispatch_type == 'moviedelete':
            result = process_movie_deleted_event(payload, instance=instance)
            logger.info(
                f"Processed movie_deleted event_log_id={event_id} movie_id={result.get('movie_id')}",
                extra={'emoji_type': 'success'},
            )
            handled = True
        elif event_type == 'series_deleted' or dispatch_type == 'seriesdelete':
            result = process_series_deleted_event(payload, instance=instance)
            logger.info(
                f"Processed series_deleted event_log_id={event_id} episodes={len(result.get('episode_ids') or [])}",
                extra={'emoji_type': 'success'},
            )
            handled = True
        elif event_type == 'playback_start' or dispatch_type in ('playback.start', 'playbackstart'):
            result = process_playback_start_event(payload, instance=instance)
            logger.info(
                f"Processed playback_start event_log_id={event_id} result={result}",
                extra={'emoji_type': 'success'},
            )
            handled = True

    finally:
        stop_wh_hb.set()

    if not handled:
        unhandled_error = f'unhandled_event_type:{event_type}'
        logger.warning(
            f"Webhook event not yet handled event_log_id={event_id} canonical={event_type} raw={raw_event_type or 'unknown'}",
            extra={'emoji_type': 'warning'},
        )

    with session_scope(role="worker") as finish_session:
        row = finish_session.get(EventLog, int(event_id))
        if row is None:
            return
        if unhandled_error:
            row.error_message = unhandled_error
        row.status = 'DONE'
        row.processed_at = datetime.now(timezone.utc)
        row.updated_at = datetime.now(timezone.utc)
        finish_session.add(row)
        finish_session.commit()


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
    # Delete webhooks after catalog purge: row is already gone; retrying cannot help.
    if 'moviedelete_movie_not_found:' in message:
        return True
    if 'seriesdelete_series_not_found:' in message:
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
    if failed and descriptor.job_type == PLACEHOLDER_ART_REFRESH_JOB_TYPE:
        try:
            from services.job_fail_active import publish_art_batch_failed_for_job

            publish_art_batch_failed_for_job(
                int(descriptor.id),
                reason=str(error_message or "failed"),
            )
        except Exception as exc:
            logger.debug(
                f"art batch live fail publish after job_id={descriptor.id} skipped: {exc}",
                extra={"emoji_type": "debug"},
            )
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


def _mark_descriptor_deferred(session, descriptor: ClaimedJobDescriptor, *, delay_seconds: int, reason: str) -> None:
    """Return a CLAIMED job to PENDING with run_after, conditional on claim_token.

    Decrements attempts so pipeline/lock deferrals do not burn max_attempts
    (claim already incremented before the handler ran).
    """
    now = datetime.now(timezone.utc)
    run_after = now + timedelta(seconds=max(5, int(delay_seconds or 60)))
    res = session.execute(
        update(Job)
        .where(
            Job.id == descriptor.id,
            Job.status == 'CLAIMED',
            Job.claim_token == descriptor.claim_token,
        )
        .values(
            status='PENDING',
            updated_at=now,
            claim_token=None,
            run_after=run_after,
            error_message=None,
            attempts=func.greatest(func.coalesce(Job.attempts, 1) - 1, 0),
        )
    )
    if res.rowcount == 0:
        raise StaleJobClaimError(
            f"job_id={descriptor.id} defer not applied (stale claim); reason={reason}"
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


# Finish-record retries after handler work succeeded. Claim already self-heals
# on lock_timeout; finish used to log and leave CLAIMED (zombie until reaper).
_FINISH_RECORD_MAX_ATTEMPTS = 5
_FINISH_RECORD_RETRY_BASE_SECONDS = 2.0


def _record_descriptor_finish(
    descriptor: ClaimedJobDescriptor,
    *,
    outcome: str,
    handler_error: Optional[Exception],
) -> str:
    """Persist DONE/FAILED/deferred after the handler; retry lock_timeout.

    Returns the effective outcome string used by ``_execute_claimed_job``:
    ``done`` / ``failed`` / ``deferred`` on success, ``stale_claim`` when the
    reaper/other worker already took the row, or ``finish_failed`` if every
    attempt hit DB contention (row stays CLAIMED for the stale reaper).
    """
    timeout_s = int(getattr(settings, "WORKER_CLAIM_LOCK_TIMEOUT_SECONDS", 30) or 30)
    last_err: Optional[Exception] = None

    for attempt in range(1, _FINISH_RECORD_MAX_ATTEMPTS + 1):
        finish_session = get_session()
        try:
            if timeout_s > 0:
                try:
                    finish_session.execute(text(f"SET LOCAL lock_timeout = '{int(timeout_s)}s'"))
                except Exception:
                    pass

            if outcome == "deferred" and isinstance(handler_error, JobDeferredError):
                _mark_descriptor_deferred(
                    finish_session,
                    descriptor,
                    delay_seconds=handler_error.delay_seconds,
                    reason=handler_error.reason,
                )
            elif handler_error is None:
                _mark_descriptor_done(finish_session, descriptor)
            else:
                _mark_descriptor_failed(finish_session, descriptor, handler_error)

            hb_fin = start_verbose_stall_heartbeat(
                f"worker.session.commit_finish job_id={descriptor.id} "
                f"job_type={descriptor.job_type} outcome={outcome} attempt={attempt}",
            )
            try:
                finish_session.commit()
            finally:
                hb_fin.set()

            if outcome == "deferred" and isinstance(handler_error, JobDeferredError):
                logger.info(
                    f"Worker deferred job_id={descriptor.id} job_type={descriptor.job_type} "
                    f"reason={handler_error.reason} run_after(+{handler_error.delay_seconds}s)",
                    extra={'emoji_type': 'info'},
                )
            elif handler_error is not None:
                logger.error(
                    f'Worker job {descriptor.id} failed: {handler_error}',
                    extra={'emoji_type': 'error'},
                )

            if outcome in ("done", "failed"):
                _try_complete_task_runs_after_job_commit(
                    descriptor,
                    failed=handler_error is not None,
                    error_message=str(handler_error) if handler_error is not None else None,
                )
            return outcome
        except StaleJobClaimError as stale:
            try:
                finish_session.rollback()
            except Exception:
                pass
            logger.info(
                f'Worker skipped finish for job_id={descriptor.id} ({stale})',
                extra={'emoji_type': 'info'},
            )
            return "stale_claim"
        except Exception as commit_err:
            try:
                finish_session.rollback()
            except Exception:
                pass
            last_err = commit_err
            lock_timeout = isinstance(commit_err, OperationalError) and _is_lock_timeout_error(commit_err)
            if lock_timeout and attempt < _FINISH_RECORD_MAX_ATTEMPTS:
                sleep_s = _FINISH_RECORD_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    f"Worker finish COMMIT hit lock_timeout ({timeout_s}s) for "
                    f"job_id={descriptor.id} job_type={descriptor.job_type} "
                    f"outcome={outcome} attempt={attempt}/{_FINISH_RECORD_MAX_ATTEMPTS}; "
                    f"retrying in {sleep_s:.1f}s (handler already finished)",
                    extra={'emoji_type': 'warning'},
                )
                time.sleep(sleep_s)
                continue
            logger.error(
                f'Worker failed to record finish for job_id={descriptor.id} '
                f'outcome={outcome} attempt={attempt}/{_FINISH_RECORD_MAX_ATTEMPTS}: {commit_err}',
                extra={'emoji_type': 'error'},
            )
            if not lock_timeout:
                break
        finally:
            try:
                finish_session.close()
            except Exception:
                pass

    if last_err is not None:
        logger.error(
            f"Worker exhausted finish retries for job_id={descriptor.id} "
            f"job_type={descriptor.job_type} outcome={outcome}; row left CLAIMED "
            f"for stale reaper. last_error={last_err}",
            extra={'emoji_type': 'error'},
        )
    return "finish_failed"


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
    # Retriable failures must not bounce back to PENDING with run_after=NULL
    # (workers reclaim in a tight loop and burn max_attempts in milliseconds).
    retry_after = None
    if new_status == 'PENDING':
        retry_after = now + timedelta(seconds=min(300, max(2, 2 ** max(0, attempts - 1))))
    res = session.execute(
        update(Job)
        .where(
            Job.id == descriptor.id,
            Job.status == 'CLAIMED',
            Job.claim_token == descriptor.claim_token,
        )
        .values(
            status=new_status,
            updated_at=now,
            claim_token=None,
            error_message=err_msg,
            run_after=retry_after,
        )
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

    if job.job_type == POSTER_LANGUAGE_RESOLVE_JOB_TYPE:
        result = process_poster_language_resolve_job(session, job)
        if not result.get('ok', False):
            raise ValueError(str(result.get('reason') or 'poster_language_resolve_failed'))
        return

    if job.job_type == ENTITY_MATERIALIZATION_JOB_TYPE:
        result = process_entity_materialization_job(session, job)
        if not result.get('ok', False):
            raise ValueError(str(result.get('reason') or 'entity_materialization_failed'))
        return

    if job.job_type == ENTITY_RECONCILE_JOB_TYPE:
        result = process_entity_reconcile_job(session, job)
        if result.get('defer'):
            # Title Refresh waits its turn behind lite/full sync; ARR webhook mats do not (#152).
            raise JobDeferredError(
                str(result.get('skipped') or 'deferred'),
                delay_seconds=int(result.get('defer_seconds') or 90),
            )
        if not result.get('ok', False):
            raise ValueError(str(result.get('reason') or 'entity_reconcile_failed'))
        return

    if job.job_type == IMPORT_GRACE_JOB_TYPE:
        logger.debug(f"Processing import_grace job: job_id={getattr(job, 'id', '?')}, payload={getattr(job, 'payload', '?')}", extra={'emoji_type': 'debug'})
        result = process_import_grace_job(session, job)
        logger.debug(f"import_grace job result: job_id={getattr(job, 'id', '?')}, result={result}", extra={'emoji_type': 'debug'})
        if result.get('defer'):
            raise JobDeferredError(
                str(result.get('skipped') or 'deferred'),
                delay_seconds=int(result.get('defer_seconds') or 5),
            )
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
        if result.get('defer'):
            # Bubble to finish step: leave PENDING with run_after (do not mark DONE).
            raise JobDeferredError(
                str(result.get('skipped') or 'deferred'),
                delay_seconds=int(result.get('defer_seconds') or 120),
            )
        return

    logger.debug(f'Skipping unhandled job_type={job.job_type}', extra={'emoji_type': 'debug'})


# ----------------------------------------------------------------
# Per-job handler watchdog (FM-6 observability)
# ----------------------------------------------------------------


def _start_handler_watchdog(
    job_id: int,
    job_type: str,
    claim_token: str | None = None,
) -> threading.Event:
    """Start a one-shot watchdog if a handler exceeds the timeout.

    Does NOT kill the thread (FS/NFS hangs cannot be safely aborted). On timeout
    the job row is marked FAILED for this claim_token so the stale-CLAIMED reaper
    cannot recycle it to PENDING forever.

    A firing watchdog means the handler is still running (often blocked on HTTP to a
    media server, Postgres, or disk), not that long runtime is expected for that job type.
    """
    timeout = _job_handler_timeout_seconds()
    cancel = threading.Event()
    token = str(claim_token or "").strip() or None

    def _watchdog():
        if cancel.wait(timeout):
            return
        logger.error(
            f"Job handler still running after {timeout}s (watchdog; thread not stopped) "
            f"job_id={job_id} job_type={job_type} — "
            f"often blocked on media-server API, DB, or disk; check logs for "
            f"'Plex: loading full' / 'section.all() still in progress' / scoped mat phase lines. "
            f"Increase JOB_HANDLER_TIMEOUT_SECONDS only if work is legitimately slower.",
            extra={'emoji_type': 'error'},
        )
        if token:
            try:
                from services.job_fail_active import (
                    WATCHDOG_TIMEOUT_REASON,
                    fail_claimed_job_for_watchdog,
                )

                fail_claimed_job_for_watchdog(
                    job_id,
                    token,
                    reason=WATCHDOG_TIMEOUT_REASON,
                )
            except Exception as exc:
                logger.warning(
                    f"Watchdog could not fail job_id={job_id}: {exc}",
                    extra={"emoji_type": "warning"},
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
    watchdog_cancel = _start_handler_watchdog(
        descriptor.id,
        descriptor.job_type,
        getattr(descriptor, "claim_token", None),
    )
    handler_outcome = "unknown"
    handler_error: Optional[Exception] = None
    heartbeat_stop = threading.Event()

    def _claim_heartbeat() -> None:
        """Keep updated_at fresh so the stale-CLAIMED reaper does not requeue live work."""
        set_db_pool_role("worker")
        while not heartbeat_stop.wait(60.0):
            hb_session = None
            try:
                hb_session = get_session()
                res = hb_session.execute(
                    update(Job)
                    .where(
                        Job.id == descriptor.id,
                        Job.status == "CLAIMED",
                        Job.claim_token == descriptor.claim_token,
                    )
                    .values(updated_at=datetime.now(timezone.utc))
                )
                if int(res.rowcount or 0) == 0:
                    return
                hb_session.commit()
            except Exception:
                try:
                    if hb_session is not None:
                        hb_session.rollback()
                except Exception:
                    pass
            finally:
                try:
                    if hb_session is not None:
                        hb_session.close()
                except Exception:
                    pass

    heartbeat_thread = threading.Thread(
        target=_claim_heartbeat,
        name=f"job-hb-{descriptor.id}",
        daemon=True,
    )
    heartbeat_thread.start()

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
        else:
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
            except JobDeferredError as defer_exc:
                handler_error = defer_exc
                handler_outcome = "deferred"
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

    # Step 3: mark DONE/FAILED in a fresh finish session (retries lock_timeout).
    # Keep the claim heartbeat alive until finish lands so the stale reaper
    # does not requeue mid-retry while sync still holds row locks.
    try:
        if handler_outcome in ("done", "failed", "deferred"):
            handler_outcome = _record_descriptor_finish(
                descriptor,
                outcome=handler_outcome,
                handler_error=handler_error,
            )
    finally:
        heartbeat_stop.set()

    watchdog_cancel.set()
    # Clear only this claim's revoke marker (not a newer claim of the same job_id).
    _clear_claim_revoked(descriptor.id, descriptor.claim_token)
    elapsed_s = time.monotonic() - started
    log_fn = logger.info if handler_outcome in ("done", "deferred") else logger.warning
    try:
        log_fn(
            f"job_done job_id={descriptor.id} job_type={descriptor.job_type} "
            f"outcome={handler_outcome} elapsed_s={elapsed_s:.3f}",
            extra={'emoji_type': 'info' if handler_outcome in ("done", "deferred") else 'warning'},
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
    # Worker threads always check out from the worker pool so UI stays responsive.
    set_db_pool_role("worker")
    from services.startup_gate import startup_sync_complete

    if not startup_sync_complete.is_set():
        logger.info(
            "Worker holding: waiting for startup gate to open…",
            extra={"emoji_type": "info"},
        )
        startup_sync_complete.wait(timeout=1800)
        if startup_sync_complete.is_set():
            logger.info(
                "Worker startup gate open; beginning job drain",
                extra={"emoji_type": "info"},
            )
        else:
            logger.warning(
                "Worker startup gate still closed after wait timeout; draining anyway",
                extra={"emoji_type": "warning"},
            )

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
