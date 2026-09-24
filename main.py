import sys
import os
import re
import json
import signal
import subprocess
import time

# Pydantic → pydantic_core loads typing_extensions' @deprecated, which imports
# asyncio.coroutines → contextvars. On slow roots (e.g. NFS), that nested first
# import can stall long enough that Ctrl+C looks like a crash. Preload here so
# stat/import work happens before FastAPI/Pydantic.
import contextvars  # noqa: F401
import asyncio.coroutines  # noqa: F401

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from core.logger import logger
from core.config import settings
from services.postgres.utils import check_db
from services.postgres.db import get_engine, init_db, get_session
from services.postgres.models import Job
import sqlalchemy as sa
from services.source_of_truth import schedule_all_syncs
from services.source_of_truth.queue_monitor_producer import (
    start_queue_monitor_producer,
    stop_queue_monitor_producer,
)
from services.startup_gate import startup_sync_complete
import asyncio
import threading
from sqlalchemy.sql import text
from sqlalchemy import inspect


_runtime_services_lock = threading.Lock()
_runtime_services_started = False
_sync_scheduler_lock = threading.Lock()
_sync_scheduler_started = False


def ensure_sync_scheduler_started(reason: str = "") -> None:
    """Register APScheduler sync jobs once (after boot sync decision finishes)."""
    global _sync_scheduler_started
    with _sync_scheduler_lock:
        if _sync_scheduler_started:
            return
        schedule_all_syncs()
        _sync_scheduler_started = True
        logger.info(
            f"Sync scheduler started (reason={reason or 'unspecified'})",
            extra={"emoji_type": "success"},
        )


def _onboarding_is_complete() -> bool:
    try:
        from services.app_config import get_onboarding_status

        status = get_onboarding_status()
        return bool(status.get('setup_complete'))
    except Exception as e:
        logger.warning(f"Failed to read onboarding status; defaulting to onboarding-incomplete: {e}", extra={'emoji_type': 'warning'})
        return False


def _ensure_core_tables(engine) -> bool:
    """Best-effort schema readiness check for core runtime tables."""
    required = {'app_config', 'movie', 'series', 'episode', 'job'}
    try:
        existing = set(inspect(engine).get_table_names())
    except Exception as e:
        logger.warning(f"Failed to inspect tables after init_db: {e}", extra={'emoji_type': 'warning'})
        existing = set()

    missing = sorted(required - existing)
    if not missing:
        return True

    logger.warning(
        f"Core tables missing after init_db: {missing}; attempting direct create_all fallback",
        extra={'emoji_type': 'warning'},
    )
    try:
        from services.postgres.db import Base
        import services.postgres.models  # noqa: F401
        Base.metadata.create_all(bind=engine)
        existing = set(inspect(engine).get_table_names())
        missing = sorted(required - existing)
        if not missing:
            logger.info("Recovered missing core tables via direct create_all fallback", extra={'emoji_type': 'success'})
            return True
    except Exception as e:
        logger.error(f"Direct create_all fallback failed: {e}", extra={'emoji_type': 'error'})

    logger.error(f"Core tables still missing after recovery attempts: {missing}", extra={'emoji_type': 'error'})
    return False


def start_runtime_background_services(
    reason: str = "startup",
    *,
    start_sync_scheduler: bool = False,
) -> bool:
    """Start workers/notifier/queue monitor once per process.

    Sync interval jobs are deferred by default until ``ensure_sync_scheduler_started``
    so boot sync can finish first without racing overdue catch-up.
    """
    global _runtime_services_started
    with _runtime_services_lock:
        if _runtime_services_started:
            logger.info(
                f"Runtime background services already running; skipping duplicate start (reason={reason})",
                extra={'emoji_type': 'info'},
            )
            if start_sync_scheduler:
                ensure_sync_scheduler_started(reason=f"{reason}:ensure_scheduler")
            return False

        # Start the shared Postgres LISTEN/NOTIFY listener BEFORE workers so
        # any registered channel callbacks (worker drain, queue-monitor wake)
        # are wired up the moment the consumers attach to it.
        try:
            from services.postgres.notifier import start_shared_notifier
            start_shared_notifier()
        except Exception as e:
            logger.error(
                f'Failed to start shared notifier (NOTIFY/LISTEN); falling back to safety-poll mode: {e}',
                extra={'emoji_type': 'error'},
            )

        # Start worker loops from the main process for deterministic behavior.
        try:
            from services.worker import run_loop as _worker_run_loop
            try:
                worker_count = int(getattr(settings, 'WORKER_COUNT', 4) or 4)
            except Exception:
                worker_count = 4
            for i in range(max(1, worker_count)):
                t = threading.Thread(target=_worker_run_loop, name=f'worker-loop-{i+1}', daemon=True)
                t.start()
                logger.info(f'Worker loop thread started: worker-loop-{i+1}', extra={'emoji_type': 'gear'})
        except Exception as e:
            logger.error(f'Failed to start worker loop(s): {e}', extra={'emoji_type': 'error'})

        try:
            start_queue_monitor_producer()
        except Exception as e:
            logger.error(f'Failed to start queue monitor producer: {e}', extra={'emoji_type': 'error'})

        _runtime_services_started = True
        logger.info(
            f"Runtime background services started (reason={reason})",
            extra={'emoji_type': 'success'},
        )
        if start_sync_scheduler:
            ensure_sync_scheduler_started(reason=reason)
        return True

# Ensure project root is first on sys.path so local 'services' package is resolved before any installed package named 'services'
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def load_env_and_migrate():
    # Load environment variables from project .env (if present).
    load_dotenv(override=True)


def log_db_summary():
    """Log an INFO-level summary of key DB counts to help track progress."""
    try:
        session = get_session()
        try:
            total_jobs = session.execute(text('SELECT count(*) FROM job')).scalar()
            pending = session.execute(text("SELECT count(*) FROM job WHERE status='PENDING'")).scalar()
            done = session.execute(text("SELECT count(*) FROM job WHERE status='DONE'")).scalar()
            subflows = session.execute(text('SELECT count(*) FROM subflow')).scalar()
            movie_subflows = session.execute(text("SELECT count(*) FROM subflow WHERE movie_id IS NOT NULL")).scalar()
            movies = session.execute(text('SELECT count(*) FROM movie')).scalar()
            episode_subflows = session.execute(text("SELECT count(*) FROM subflow WHERE episode_id IS NOT NULL")).scalar()
            series = session.execute(text('SELECT count(*) FROM series')).scalar()
            episodes = session.execute(text('SELECT count(*) FROM episode')).scalar()
            logger.info(f"DB SUMMARY: jobs total={total_jobs} pending={pending} done={done} subflows={subflows} movie_subflows={movie_subflows} movies={movies} ep_subflows={episode_subflows} series={series} episodes={episodes}", extra={'emoji_type': 'info'})
        finally:
            session.close()
    except Exception as e:
        logger.debug(f"Unable to compute DB summary: {e}", extra={'emoji_type': 'debug'})

def clear_port(port: int, max_attempts: int = 3) -> bool:
    for attempt in range(max_attempts):
        try:
            result = subprocess.run(['lsof', '-i', f':{port}'], capture_output=True, text=True)
            if result.stdout:
                for line in result.stdout.split('\n')[1:]:
                    if line:
                        pid = line.split()[1]
                        subprocess.run(['kill', '-9', pid])
                        logger.info(f"Killed process {pid} using port {port}", extra={'emoji_type': 'info'})
                time.sleep(1)
                return True
            return True
        except Exception as e:
            logger.warning(f"Attempt {attempt + 1} to clear port {port} failed: {e}", extra={'emoji_type': 'warning'})
            time.sleep(1)
    return False

def check_port(port: int) -> bool:
    try:
        result = subprocess.run(['lsof', '-i', f':{port}'], capture_output=True, text=True)
        if result.stdout:
            logger.error(f"Port {port} is already in use. Update PLACEHOLDARR_PORT in your .env file.", extra={'emoji_type': 'error'})
            return False
        return True
    except Exception as e:
        logger.error(f"Failed to check port {port}: {e}", extra={'emoji_type': 'error'})
        return False

# single FastAPI app instance is created below with a lifespan handler

async def lifespan(app: FastAPI):
    # Keep Ctrl-C disabled for accidental local interrupts, but allow
    # normal process termination via SIGTERM/SIGHUP (Docker stop, VS Code
    # terminal delete, remote session disconnect).
    # These must be applied here — AFTER uvicorn.run() installs handlers.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGHUP, signal.SIG_DFL)

    load_env_and_migrate()
    logger.info("Placeholdarr service is running.", extra={'emoji_type': 'success'})
    # Helps verify Docker entrypoint drop-priv (PUID/PGID): if uid/gid are 0 here, files on bind mounts will be owned by root.
    logger.info(
        "Runtime process identity uid=%s gid=%s euid=%s egid=%s",
        os.getuid(),
        os.getgid(),
        os.geteuid(),
        os.getegid(),
        extra={"emoji_type": "info"},
    )

    # Ensure default dummy media files are present before onboarding/settings use.
    try:
        from services.placeholders import ensure_runtime_dummy_files

        dummy_result = ensure_runtime_dummy_files()
        created = dummy_result.get('created') or []
        missing_sources = dummy_result.get('missing_sources') or []
        errors = dummy_result.get('errors') or []

        if created:
            logger.info(
                f"Ensured startup dummy media files: {created}",
                extra={'emoji_type': 'dummy'},
            )
        if missing_sources:
            logger.warning(
                f"Dummy media sources unavailable for: {missing_sources}",
                extra={'emoji_type': 'warning'},
            )
        if errors:
            logger.warning(
                f"Dummy media startup ensure completed with warnings: {errors}",
                extra={'emoji_type': 'warning'},
            )
    except Exception as e:
        logger.warning(f"Failed to ensure startup dummy media files: {e}", extra={'emoji_type': 'warning'})

    # DB Initialization
    if check_db():
        logger.info("Database is reachable, initializing...", extra={'emoji_type': 'info'})

        engine = get_engine()
        init_db(engine)
        try:
            from services.series_episode_stats_hooks import schedule_series_stats_backfill

            schedule_series_stats_backfill()
        except Exception as e:
            logger.warning(
                f"Could not schedule series episode stats backfill: {e}",
                extra={"emoji_type": "warning"},
            )
        try:
            from services.library_poster_backfill import schedule_library_poster_grid_backfill

            schedule_library_poster_grid_backfill()
        except Exception as e:
            logger.warning(
                f"Could not schedule library poster grid backfill: {e}",
                extra={"emoji_type": "warning"},
            )
        if not _ensure_core_tables(engine):
            startup_sync_complete.set()
            logger.error(
                "Database schema is not ready; deferring startup pipelines to avoid crashing",
                extra={'emoji_type': 'error'},
            )
            yield
            return

        try:
            from services.app_config import apply_persisted_settings

            apply_result = apply_persisted_settings()
            if apply_result.get('count'):
                logger.info(
                    f"Applied persisted app settings count={apply_result.get('count')}",
                    extra={'emoji_type': 'info'},
                )
        except Exception as e:
            logger.warning(f"Failed to apply persisted app settings: {e}", extra={'emoji_type': 'warning'})

        onboarding_complete = _onboarding_is_complete()
        if not onboarding_complete:
            startup_sync_complete.set()
            logger.info(
                'Onboarding incomplete; startup sync, schedulers, workers, and queue monitoring are deferred until setup is completed.',
                extra={'emoji_type': 'info'},
            )
            yield
            return

        # Catalog housekeeping must not block Uvicorn from accepting HTTP. Movie/Series/
        # Episode.status QUEUED is legacy (nothing writes it anymore); do not scan those
        # tables on boot. Keep CLAIMED job reset + orphaned task-run abandon.
        def _startup_catalog_housekeeping():
            try:
                try:
                    log_db_summary()
                except Exception:
                    pass
                session = get_session()
                try:
                    try:
                        claimed_q = session.query(Job).filter(Job.status == 'CLAIMED')
                        claimed_count = claimed_q.count()
                        if claimed_count:
                            claimed_q.update({Job.status: 'PENDING', Job.updated_at: sa.func.now()}, synchronize_session=False)
                            session.commit()
                            logger.info(
                                f"Reset {claimed_count} CLAIMED jobs -> PENDING on startup",
                                extra={'emoji_type': 'info'},
                            )
                        else:
                            session.rollback()
                    except Exception as e:
                        try:
                            logger.debug(f"Failed to reset CLAIMED jobs on startup: {e}", extra={'emoji_type': 'debug'})
                        except Exception:
                            pass

                    try:
                        from services.task_run_history import abandon_orphaned_working_task_runs

                        abandon_orphaned_working_task_runs(reason="interrupted_by_restart")
                    except Exception as e:
                        logger.warning(
                            f"Failed to abandon orphaned working task runs on startup: {e}",
                            extra={"emoji_type": "warning"},
                        )
                finally:
                    session.close()
            except Exception as exc:
                logger.warning(
                    f"Startup catalog housekeeping failed (API is up): {exc}",
                    extra={"emoji_type": "warning"},
                )

        threading.Thread(
            target=_startup_catalog_housekeeping,
            name="startup-catalog-housekeeping",
            daemon=True,
        ).start()

        # Start workers / notifier / queue monitor on a background thread so this lifespan
        # can reach ``yield`` immediately. If anything in that chain blocks (locks, scheduler,
        # DB), Uvicorn would otherwise never finish "application startup" and the UI/API would
        # not accept connections — even though DB init already succeeded.
        def _bootstrap_runtime_services():
            try:
                logger.info(
                    "Runtime bootstrap thread: starting workers, notifier, schedulers, queue monitor…",
                    extra={"emoji_type": "gear"},
                )
                start_runtime_background_services(reason="lifespan_onboarding_complete")
                logger.info("Runtime bootstrap thread: finished (workers should be running).", extra={"emoji_type": "success"})
            except Exception as exc:
                logger.error(
                    f"Runtime bootstrap thread failed (API is up but background jobs may be broken): {exc}",
                    exc_info=True,
                    extra={"emoji_type": "error"},
                )

        threading.Thread(
            target=_bootstrap_runtime_services,
            name="runtime-services-bootstrap",
            daemon=True,
        ).start()

        async def _startup_gate_watchdog():
            """FM-21: never leave workers wedged behind the gate if sync never finishes."""
            await asyncio.sleep(1800)
            if not startup_sync_complete.is_set():
                startup_sync_complete.set()
                logger.warning(
                    'Startup sync watchdog fired after 30 minutes — forcing startup_sync_complete open '
                    '(sync may not have completed).',
                    extra={'emoji_type': 'warning'},
                )

        asyncio.create_task(_startup_gate_watchdog())

        # Execute startup source-of-truth orchestration in a background thread so
        # Uvicorn can start accepting webhook events immediately. Workers wait on
        # startup_sync_complete before processing any jobs.
        #
        # This must remain a thread (not a Job): workers are blocked on the gate
        # until this finishes, so a startup_sync_runner Job would deadlock.
        def _run_startup_sync():
            try:
                from services.discover.mode import is_tmdb_discover_mode

                if is_tmdb_discover_mode():
                    from services.discover.mode import should_run_discover_startup_sync
                    from services.discover.pipeline import run_discover_startup

                    run_discover, discover_reason = should_run_discover_startup_sync()
                    if not run_discover:
                        logger.info(
                            f"Startup TMDB Discover pipeline skipped ({discover_reason})",
                            extra={"emoji_type": "info"},
                        )
                        return
                    logger.info(
                        f"Startup TMDB Discover pipeline starting ({discover_reason})",
                        extra={"emoji_type": "gear"},
                    )
                    discover_result = run_discover_startup()
                    logger.info(
                        f"Startup TMDB Discover pipeline completed: {discover_result}",
                        extra={"emoji_type": "success"},
                    )
                    return

                from services.source_of_truth.startup import run_startup_source_of_truth
                startup_result = run_startup_source_of_truth()
                if startup_result.get('ran'):
                    run_ids = startup_result.get('run_ids', [])
                    scan = startup_result.get('scan', {})
                    primer = startup_result.get('primer', {})
                    determination = startup_result.get('determination', {})
                    calendar_date_refresh = startup_result.get('calendar_date_refresh', {})
                    calendar = startup_result.get('calendar', {})
                    status_reconcile = startup_result.get('status_reconcile', {})
                    materialization = startup_result.get('materialization', {})
                    logger.info(
                        f"Startup source-of-truth completed run_ids={run_ids} fs_scan_count={scan.get('count')} "
                        f"date_refresh_movies={calendar_date_refresh.get('movie_rows_updated', 0)} "
                        f"date_refresh_episodes={calendar_date_refresh.get('episode_rows_updated', 0)} "
                        f"needs={determination.get('needs_placeholder')} exists={determination.get('placeholder_exists')} "
                        f"not_needed={determination.get('not_needed')} obsolete={determination.get('obsolete_placeholder')} "
                        f"calendar_intents={calendar.get('status_intents', 0)} "
                        f"calendar_applied={calendar.get('status_applied', 0)} "
                        f"variant_switched={calendar.get('variant_switched', 0)} "
                        f"status_reconciled={status_reconcile.get('reconciled')} "
                        f"status_skipped={status_reconcile.get('skipped', 0)} "
                        f"status_errors={status_reconcile.get('errors')} "
                        f"materialized_created={materialization.get('created')} "
                        f"materialized_deleted={materialization.get('deleted')} "
                        f"materialization_errors={materialization.get('errors')}",
                        extra={'emoji_type': 'info'},
                    )
                    if status_reconcile.get('errors'):
                        logger.warning(
                            f"Startup status reconcile errors={status_reconcile.get('errors')} "
                            f"error_breakdown={status_reconcile.get('error_breakdown') or {}}",
                            extra={'emoji_type': 'warning'},
                        )
                    if primer.get('skipped') is False:
                        logger.info(
                            f"Startup primer applied: strategy={primer.get('strategy')} "
                            f"movies={primer.get('materialized_movies')} episodes={primer.get('materialized_episodes')} "
                            f"refresh_requested={primer.get('refresh_requested')} refresh_failed={primer.get('refresh_failed')}",
                            extra={'emoji_type': 'info'},
                        )
                    try:
                        log_db_summary()
                    except Exception:
                        pass
                else:
                    logger.debug('Startup source-of-truth disabled by config flags', extra={'emoji_type': 'debug'})
            except Exception as e:
                logger.error(f'Startup source-of-truth failed: {e}', extra={'emoji_type': 'error'})
            finally:
                try:
                    ensure_sync_scheduler_started(reason="after_boot_sync")
                except Exception as sched_exc:
                    logger.error(
                        f"Failed to start sync scheduler after boot sync: {sched_exc}",
                        extra={"emoji_type": "error"},
                    )
                startup_sync_complete.set()

        threading.Thread(target=_run_startup_sync, name='startup-sync', daemon=True).start()
        logger.info('Startup sync running in background; service accepting requests immediately', extra={'emoji_type': 'info'})

    # --- Placeholder for future Sonarr sync entrypoint ---
    # source-of-truth scheduling is handled via services.source_of_truth
    else:
        logger.error("Unable to initialize DB", extra={'emoji_type': 'error'})

    # Integration connection checks (Media + ARR). Results feed Settings badges.
    # Nested ``import asyncio`` would shadow the module name for all of ``lifespan``.
    try:
        from services.integration_status import refresh_integration_connection_status
    except Exception:
        refresh_integration_connection_status = None
        logger.debug(
            "services.integration_status.refresh_integration_connection_status not available; skipping connection checks",
            extra={"emoji_type": "debug"},
        )

    async def integration_connection_check_loop():
        max_attempts = 10
        interval = 60
        for attempt in range(max_attempts):
            try:
                status = await asyncio.to_thread(refresh_integration_connection_status)
            except Exception as exc:
                logger.warning(
                    f"Integration connection check failed: {exc}",
                    extra={"emoji_type": "warning"},
                )
                status = {}
            arr = status.get("arr") or {}
            if arr and any(bool(item.get("ok")) for item in arr.values()):
                break
            if attempt == 0:
                from core.config import settings

                arrs_configured = bool(getattr(settings, "configured_arr_instances", []) or [])
                if not arrs_configured:
                    logger.warning("No *arr services configured", extra={"emoji_type": "warning"})
            await asyncio.sleep(interval)
        else:
            logger.warning(
                "Timed out waiting for a reachable *arr instance during startup connection checks.",
                extra={"emoji_type": "warning"},
            )

    if refresh_integration_connection_status:
        asyncio.create_task(integration_connection_check_loop())
    else:
        logger.debug("No integration connection checker available; skipping", extra={"emoji_type": "debug"})
    logger.info(
        'Lifespan startup complete — HTTP and /api routes are ready. '
        '(Worker threads may still log "holding: waiting for startup sync" until the background sync thread finishes; '
        'that does not block the dashboard.)',
        extra={'emoji_type': 'success'},
    )
    yield

    try:
        stop_queue_monitor_producer()
    except Exception as e:
        logger.warning(f'Failed to stop queue monitor producer cleanly: {e}', extra={'emoji_type': 'warning'})

    try:
        from services.worker import stop_worker_runtime
        stop_worker_runtime()
    except Exception as e:
        logger.warning(f'Failed to stop worker runtime cleanly: {e}', extra={'emoji_type': 'warning'})

    try:
        from services.postgres.notifier import stop_shared_notifier
        stop_shared_notifier()
    except Exception as e:
        logger.warning(f'Failed to stop shared notifier cleanly: {e}', extra={'emoji_type': 'warning'})

app = FastAPI(lifespan=lifespan)

# Dashboard authentication (session cookie + API gate). SessionMiddleware must wrap AuthGate.
from starlette.middleware.sessions import SessionMiddleware
from services.auth import ensure_session_secret
from services.auth_middleware import AuthGateMiddleware
from routes.auth import router as auth_router

app.include_router(auth_router)

# Dashboard UI
from routes.dashboard import router as dashboard_router
app.include_router(dashboard_router)

from routes.tasks import router as tasks_router
app.include_router(tasks_router)

# Rule-based Plex collection builder
from routes.collections import router as collections_router
app.include_router(collections_router)

from routes.discover import router as discover_router
app.include_router(discover_router)

# Customizable status message templates
from routes.messages import router as messages_router
app.include_router(messages_router)

from routes.whats_new import router as whats_new_router
app.include_router(whats_new_router)

app.add_middleware(AuthGateMiddleware)
_session_https_only = bool(getattr(settings, "AUTH_COOKIE_SECURE", False))
app.add_middleware(
    SessionMiddleware,
    secret_key=ensure_session_secret(),
    session_cookie="placeholdarr_session",
    same_site="lax",
    https_only=_session_https_only,
    max_age=60 * 60 * 24 * 14,
)

@app.post("/webhook")
async def webhook(request: Request):
    try:
        from services.auth import get_auth_mode, verify_webhook_api_key

        # /webhook is exempt from cookie/CSRF checks; machine callers use ?apikey=.
        # Skipped only in AUTH_MODE=disabled.
        if get_auth_mode() != "disabled":
            provided_key = request.query_params.get("apikey")
            if not verify_webhook_api_key(provided_key):
                raw_instance = request.query_params.get("instance", "")
                safe_instance = re.sub(r"[^A-Za-z0-9_-]", "", raw_instance)[:64] or "unknown"
                logger.warning(
                    "Webhook rejected: missing or invalid apikey "
                    f"instance={safe_instance} "
                    f"present={'yes' if provided_key else 'no'}",
                    extra={"emoji_type": "warning"},
                )
                raise HTTPException(status_code=401, detail="Invalid or missing apikey")

        instance_raw = request.query_params.get("instance", "").strip() or None
        instance_id_raw = request.query_params.get("instance_id", "").strip() or None
        safe_instance = re.sub(r"[^A-Za-z0-9_-]", "", instance_raw or "")[:64] or "unknown"
        content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        body = await request.body()
        if not body or not body.strip():
            logger.warning(
                "Webhook rejected: empty body "
                f"instance={safe_instance} content_type={content_type or 'none'} "
                "(Emby live TV / misconfigured notification often POSTs with no JSON)",
                extra={"emoji_type": "warning"},
            )
            raise HTTPException(
                status_code=400,
                detail="Webhook body must be non-empty JSON",
            )
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            logger.warning(
                "Webhook rejected: invalid JSON "
                f"instance={safe_instance} content_type={content_type or 'none'} "
                f"body_bytes={len(body)} error={e}",
                extra={"emoji_type": "warning"},
            )
            raise HTTPException(
                status_code=400,
                detail="Webhook body must be valid JSON",
            )

        # During onboarding, accept webhook calls but intentionally ignore them.
        # Post-onboarding startup full sync is the source of truth and avoids
        # processing transient events while settings are still being configured.
        if not _onboarding_is_complete():
            logger.info(
                f"Webhook accepted but ignored because onboarding is incomplete "
                f"instance={instance_raw or 'unknown'} instance_id={instance_id_raw or 'none'}",
                extra={'emoji_type': 'info'},
            )
            return {"status": "accepted", "ignored": True, "reason": "onboarding_incomplete"}

        try:
            from services.handlers import (
                resolve_canonical_webhook_instance,
                validate_webhook_payload,
            )
        except Exception:
            logger.info('Webhook received but no handler available in services.handlers; ignoring payload', extra={'emoji_type': 'info'})
            return {"status": "accepted"}

        canonical_instance, route_err = resolve_canonical_webhook_instance(instance_raw, instance_id_raw)
        if route_err:
            logger.warning(
                f"Webhook rejected: {route_err} "
                f"instance={safe_instance} instance_id={instance_id_raw or 'none'}",
                extra={"emoji_type": "warning"},
            )
            raise HTTPException(status_code=400, detail=route_err)

        from core.config import settings as _settings
        from services.tracearr_playback import adapt_tracearr_webhook_payload, is_tracearr_instance

        if isinstance(payload, dict) and is_tracearr_instance(
            canonical_instance, getattr(_settings, "TRACEARR_INSTANCE_KEY", "tracearr")
        ):
            payload = adapt_tracearr_webhook_payload(payload)

        ok, reason, event_type, _event_meta = validate_webhook_payload(payload, canonical_instance)
        if not ok:
            logger.warning(
                f"Webhook rejected before enqueue event_type={event_type} reason={reason}",
                extra={'emoji_type': 'warning'},
            )
            raise HTTPException(status_code=400, detail=reason)

        from services.webhook_ingest import enqueue_webhook_persist

        enqueue_webhook_persist(payload, canonical_instance)
        return {"status": "accepted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Webhook handling failed: {e}", extra={'emoji_type': 'error'})
        raise

if __name__ == '__main__':
    import uvicorn
    load_dotenv(override=True)

    # Do not import media clients at startup; initialize them lazily in their modules when needed.

    port = int(os.getenv('PLACEHOLDARR_PORT', 8000))
    host = getattr(settings, "host", "0.0.0.0")

    logger.info(f"Using host {host} and port {port}", extra={'emoji_type': 'info'})

    if not check_port(port):
        logger.error(
            f"Port {port} is unavailable. Set PLACEHOLDARR_PORT to an unused port and restart.",
            extra={'emoji_type': 'error'},
        )
        sys.exit(1)

    # Access log verbosity only; Placeholdarr app logs go through core.logger (full capture to files).
    uvicorn.run(app, host=host, port=port, log_level="info")