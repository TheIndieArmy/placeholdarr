import sys
import os
import subprocess
import time
from dotenv import load_dotenv
from fastapi import FastAPI, Request, BackgroundTasks
from core.logger import logger
from core.config import settings
from services.postgres.utils import check_db
from services.postgres.db import get_engine, init_db, get_session
from services.postgres.models import Movie
from services.sync import schedule_all_syncs
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
            episode_subflows = session.execute(text("SELECT count(*) FROM subflow WHERE episode_id IS NOT NULL")).scalar()
            series = session.execute(text('SELECT count(*) FROM series')).scalar()
            episodes = session.execute(text('SELECT count(*) FROM episode')).scalar()
            logger.info(f"DB SUMMARY: jobs total={total_jobs} pending={pending} done={done} subflows={subflows} moviesf={movie_subflows} ep_subflows={episode_subflows} series={series} episodes={episodes}", extra={'emoji_type': 'info'})
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
            session.query(Movie).filter(Movie.status == 'QUEUED').update(
                {Movie.status: 'PENDING'},
                synchronize_session=False
            )
            session.commit()
            logger.info("Reset QUEUED movies to PENDING", extra={'emoji_type': 'info'})
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

        # If configured, perform immediate list-capture seeding for RADARR/SONARR
        try:
            from services.list_capture import capture_movies_fullsync_and_create_run, capture_series_fullsync_and_create_run
            if getattr(settings, 'RADARR_SYNC_ON_STARTUP', False):
                try:
                    run = capture_movies_fullsync_and_create_run(run_note='startup movie fullsync')
                    logger.info(f"Created startup movie fullsync run: {run.run_id}", extra={'emoji_type': 'info'})
                except Exception as e:
                    logger.error(f"Movie list-capture at startup failed: {e}", extra={'emoji_type': 'error'})
                else:
                    # After seeding, log a DB summary so we can see how many jobs/subflows were created
                    try:
                        log_db_summary()
                    except Exception:
                        pass
            if getattr(settings, 'SONARR_SYNC_ON_STARTUP', False):
                try:
                    run = capture_series_fullsync_and_create_run(run_note='startup tv fullsync')
                    logger.info(f"Created startup tv fullsync run: {run.run_id}", extra={'emoji_type': 'info'})
                except Exception as e:
                    logger.error(f"TV list-capture at startup failed: {e}", extra={'emoji_type': 'error'})
                else:
                    try:
                        log_db_summary()
                    except Exception:
                        pass
        except Exception:
            logger.debug('List-capture modules not available or failed to import', extra={'emoji_type': 'debug'})

        # Perform a single filesystem placeholder scan at startup when a fullsync was requested.
            try:
                from services.fs_scan import scan_once_if_needed
                # Consider 4K variants as well when deciding to run a startup fullsync scan
                radarr_flags = any([
                    getattr(settings, 'RADARR_SYNC_ON_STARTUP', False),
                    getattr(settings, 'RADARR_4K_SYNC_ON_STARTUP', False)
                ])
                sonarr_flags = any([
                    getattr(settings, 'SONARR_SYNC_ON_STARTUP', False),
                    getattr(settings, 'SONARR_4K_SYNC_ON_STARTUP', False)
                ])
                if radarr_flags or sonarr_flags:
                    try:
                        count = scan_once_if_needed()
                        logger.info(f'FS-scan seeded {count} placeholders at startup', extra={'emoji_type': 'info'})
                    except Exception as e:
                        logger.error(f'FS-scan at startup failed: {e}', extra={'emoji_type': 'error'})
            except Exception:
                logger.debug('services.fs_scan not available; skipping startup FS-scan', extra={'emoji_type': 'debug'})

        # Startup syncs are scheduled via services.sync.schedule_all_syncs()
    
        # which is already invoked above. Avoid running duplicate full-syncs here.

    # --- Placeholder for future Sonarr sync entrypoint ---
    # from services.sync import sync_series
    # sync_series.schedule_all_syncs()
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
                logger.info("Calendar sync will begin in 10 seconds...", extra={'emoji_type': 'info'})
                async def delayed_calendar_sync():
                    await asyncio.sleep(10)
                    # start_calendar_sync() -- Uncomment when implemented
                asyncio.create_task(delayed_calendar_sync())
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
        source_port = request.client.port
        # Prefer using the new handler if available; fall back to archived handler if present.
        try:
            from services.handlers import handle_webhook as new_handle
            background_tasks.add_task(new_handle, payload, source_port)
        except Exception:
            logger.info('Webhook received but no handler available in services.handlers; ignoring payload', extra={'emoji_type': 'info'})
        return {"status": "accepted"}
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