import sys
import os
import subprocess
import time
from dotenv import load_dotenv
from fastapi import FastAPI, Request, BackgroundTasks
from core.logger import logger
from services.handlers import handle_webhook
from services.migration import run_migration
from core.config import settings
from services.postgres.utils import check_db
from services.postgres.db import get_engine, init_db, get_session
from services.postgres.models import Movie
from services.scheduler import *
from services.sync.sync_movies import schedule_all_syncs

# Ensure project root is first on sys.path so local 'services' package is resolved before any installed package named 'services'
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def load_env_and_migrate():
    load_dotenv(override=True)
    run_migration()

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

app = FastAPI()

async def lifespan(app: FastAPI):
    load_env_and_migrate()
    logger.info("Placeholdarr service is running.", extra={'emoji_type': 'success'})

    # DB Initialization
    if check_db():
        logger.info("Database is reachable, initializing...", extra={'emoji_type': 'info'})

        engine = get_engine()
        init_db(engine)

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

        # --- Schedule all Radarr syncs (startup and cron) ---
        schedule_all_syncs()

        # --- Placeholder for future Sonarr sync entrypoint ---
        # from services.sync import sync_series
        # sync_series.schedule_all_syncs()
        # Ensure job worker is running to process queued jobs (enrichment/imports)
        try:
            from services.jobs import start_worker_once
            start_worker_once()
            logger.info("Started centralized job worker", extra={'emoji_type': 'gear'})
        except Exception as e:
            logger.debug(f"Failed to start centralized job worker at startup: {e}", extra={'emoji_type': 'debug'})
        yield
    else:
        logger.error("Unable to initialize DB", extra={'emoji_type': 'error'})

    # Webhook Check Loop
    import asyncio
    from services.integrations import check_all_arr_webhooks

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

    asyncio.create_task(arr_webhook_check_loop())
    yield

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
        source_port = request.client.port
        background_tasks.add_task(handle_webhook, payload, source_port)
        return {"status": "accepted"}
    except Exception as e:
        logger.error(f"Webhook handling failed: {e}", extra={'emoji_type': 'error'})
        raise

if __name__ == '__main__':
    import uvicorn
    load_dotenv(override=True)

    if settings.plex_enabled:
        from services.plex_client import plex
    if settings.jellyfin_enabled:
        from services.jellyfin_client import test_jellyfin_connection

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