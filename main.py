import sys
import os
import subprocess
import time
from dotenv import load_dotenv
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from core.logger import logger
from core.config import settings
from services.postgres.utils import check_db
from services.postgres.db import get_engine, init_db, get_session
from services.postgres.models import Movie, Series, Episode, Job
import sqlalchemy as sa
from services.source_of_truth import schedule_all_syncs
from services.startup_gate import startup_sync_complete
import asyncio
import threading
from sqlalchemy.sql import text

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
    load_env_and_migrate()
    logger.info("Placeholdarr service is running.", extra={'emoji_type': 'success'})

    # DB Initialization
    if check_db():
        logger.info("Database is reachable, initializing...", extra={'emoji_type': 'info'})

        engine = get_engine()
        init_db(engine)

        # Log a DB summary after initialization so operators can see starting counts
        try:
            log_db_summary()
        except Exception:
            pass

        session = get_session()
        try:
            # Reset queued content to PENDING so startup seeding doesn't leave items stranded
            # Movies, Series, and Episodes may be queued from previous runs; migrate them to PENDING.
            session.query(Movie).filter(Movie.status == 'QUEUED').update(
                {Movie.status: 'PENDING'},
                synchronize_session=False
            )
            try:
                session.query(Series).filter(Series.status == 'QUEUED').update(
                    {Series.status: 'PENDING'},
                    synchronize_session=False
                )
            except Exception:
                # Series table may not exist in some minimal environments; ignore failures here
                pass
            try:
                session.query(Episode).filter(Episode.status == 'QUEUED').update(
                    {Episode.status: 'PENDING'},
                    synchronize_session=False
                )
            except Exception:
                # Episode table may not exist in some minimal environments; ignore failures here
                pass
            session.commit()
            logger.info("Reset QUEUED movies/series/episodes to PENDING", extra={'emoji_type': 'info'})

            # Reset CLAIMED jobs back to PENDING on startup so crashed/restarted
            # workers do not leave jobs stranded in a non-runnable state.
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
                # Do not fail startup because of requeue logic; log and continue.
                try:
                    logger.debug(f"Failed to reset CLAIMED jobs on startup: {e}", extra={'emoji_type': 'debug'})
                except Exception:
                    pass
        finally:
            session.close()

        for name, sched in globals().items():
            if name.endswith('_scheduler') and hasattr(sched, 'start'):
                sched.start()
                logger.info(f"Started scheduler: {name}", extra={'emoji_type': 'gear'})

        # Note: background job worker startup is intentionally omitted here.
        # Worker startup and job processing are controlled separately during
        # development so the database-seeding (list-capture) can run without
        # triggering downstream processing.


        # --- Schedule all Radarr syncs (startup and cron) ---
        schedule_all_syncs()

        # Execute startup source-of-truth orchestration in a background thread so
        # Uvicorn can start accepting webhook events immediately. Workers wait on
        # startup_sync_complete before processing any jobs.
        def _run_startup_sync():
            try:
                from services.source_of_truth.startup import run_startup_source_of_truth
                startup_result = run_startup_source_of_truth()
                if startup_result.get('ran'):
                    run_ids = startup_result.get('run_ids', [])
                    scan = startup_result.get('scan', {})
                    primer = startup_result.get('primer', {})
                    determination = startup_result.get('determination', {})
                    materialization = startup_result.get('materialization', {})
                    logger.info(
                        f"Startup source-of-truth completed run_ids={run_ids} fs_scan_count={scan.get('count')}",
                        extra={'emoji_type': 'info'},
                    )
                    logger.info(
                        f"Primer phase: skipped={primer.get('skipped')} reason={primer.get('reason')} "
                        f"strategy={primer.get('strategy')} empty_roots={primer.get('empty_roots')} "
                        f"movies={primer.get('materialized_movies')} episodes={primer.get('materialized_episodes')} "
                        f"force_reprimed={primer.get('force_reprimed')} "
                        f"refresh_requested={primer.get('refresh_requested')} refresh_failed={primer.get('refresh_failed')}",
                        extra={'emoji_type': 'info'},
                    )
                    logger.info(
                        f"Determination phase: needs={determination.get('needs_placeholder')} "
                        f"exists={determination.get('placeholder_exists')} "
                        f"obsolete={determination.get('obsolete_placeholder')} "
                        f"not_needed={determination.get('not_needed')} "
                        f"movies_changed={determination.get('movies_changed')} "
                        f"episodes_changed={determination.get('episodes_changed')}",
                        extra={'emoji_type': 'info'},
                    )
                    logger.info(
                        f"Materialization phase: created={materialization.get('created')} "
                        f"deleted={materialization.get('deleted')} noop={materialization.get('noop')} "
                        f"files_created={materialization.get('files_created')} "
                        f"files_deleted={materialization.get('files_deleted')} "
                        f"errors={materialization.get('errors')} "
                        f"media_id_observed_plex={materialization.get('media_id_observed_plex')} "
                        f"media_id_observed_jellyfin={materialization.get('media_id_observed_jellyfin')} "
                        f"media_id_observed_emby={materialization.get('media_id_observed_emby')} "
                        f"media_id_observe_failed={materialization.get('media_id_observe_failed')}",
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
                startup_sync_complete.set()

        threading.Thread(target=_run_startup_sync, name='startup-sync', daemon=True).start()
        logger.info('Startup sync running in background; service accepting requests immediately', extra={'emoji_type': 'info'})

    # --- Placeholder for future Sonarr sync entrypoint ---
    # source-of-truth scheduling is handled via services.source_of_truth
    else:
        logger.error("Unable to initialize DB", extra={'emoji_type': 'error'})

    # Webhook Check Loop
    import asyncio
    # Use the new integrations checker if available; do not fall back to archived code
    try:
        from services.integrations import check_all_arr_webhooks
    except Exception:
        check_all_arr_webhooks = None
        logger.debug('services.integrations.check_all_arr_webhooks not available; skipping webhook loop', extra={'emoji_type': 'debug'})

    async def arr_webhook_check_loop():
        max_attempts = 10
        interval = 60
        for attempt in range(max_attempts):
            all_configured = check_all_arr_webhooks()
            if all_configured:
                # Calendar sync is not yet implemented; break silently once webhooks are confirmed.
                break

            if attempt == 0:
                from core.config import settings
                arrs_configured = any([
                    getattr(settings, 'RADARR_URL', None) and getattr(settings, 'RADARR_API_KEY', None),
                    getattr(settings, 'RADARR_4K_URL', None) and getattr(settings, 'RADARR_4K_API_KEY', None),
                    getattr(settings, 'SONARR_URL', None) and getattr(settings, 'SONARR_API_KEY', None),
                    getattr(settings, 'SONARR_4K_URL', None) and getattr(settings, 'SONARR_4K_API_KEY', None),
                ])
                if not arrs_configured:
                    logger.warning("No *arr services configured in .env", extra={'emoji_type': 'warning'})

            await asyncio.sleep(interval)
        else:
            logger.warning("Timed out waiting for *arr webhooks. Calendar sync will not start.", extra={'emoji_type': 'warning'})

    # Only start the webhook check loop if we have a checker available; otherwise log and skip
    if check_all_arr_webhooks:
        asyncio.create_task(arr_webhook_check_loop())
    else:
        logger.debug('No webhook checker available (services_old.integrations missing); skipping arr webhook loop', extra={'emoji_type': 'debug'})
    # After startup seeding and scheduling, start the worker loop(s).
    # We always start workers from the main process so behavior is deterministic for users.
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

    yield

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
        instance = request.query_params.get("instance", "").strip() or None
        try:
            from services.handlers import handle_webhook as new_handle, validate_webhook_payload
        except Exception:
            logger.info('Webhook received but no handler available in services.handlers; ignoring payload', extra={'emoji_type': 'info'})
            return {"status": "accepted"}

        ok, reason, event_type = validate_webhook_payload(payload, instance)
        if not ok:
            logger.warning(
                f"Webhook rejected before enqueue event_type={event_type} reason={reason}",
                extra={'emoji_type': 'warning'},
            )
            raise HTTPException(status_code=400, detail=reason)

        background_tasks.add_task(new_handle, payload, instance)
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

    port = int(os.getenv('PLACEHOLDARR_PORT', 8001))
    host = getattr(settings, "host", "0.0.0.0")

    logger.info(f"Using host {host} and port {port}", extra={'emoji_type': 'info'})

    if not check_port(port):
        logger.info(f"Attempting to clear port {port}", extra={'emoji_type': 'info'})
        if clear_port(port):
            logger.info(f"Successfully cleared port {port}", extra={'emoji_type': 'info'})
        else:
            logger.error(f"Failed to clear port {port}. Please use a different port.", extra={'emoji_type': 'error'})
            sys.exit(1)

    # uvicorn expects standard log levels; map custom values (e.g. VERBOSE) to a safe default
    raw_level = getattr(settings, 'LOG_LEVEL', 'info')
    try:
        uvicorn_level = str(raw_level).lower()
    except Exception:
        uvicorn_level = 'info'

    # Treat non-standard/extra verbose level as info to avoid KeyError in uvicorn
    if uvicorn_level not in ('critical', 'error', 'warning', 'info', 'debug', 'trace'):
        if uvicorn_level == 'verbose':
            uvicorn_level = 'info'
        else:
            uvicorn_level = 'info'

    uvicorn.run(app, host=host, port=port, log_level=uvicorn_level)