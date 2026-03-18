import sys
import os
import subprocess
import time
from dotenv import load_dotenv
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request, BackgroundTasks
from core.logger import logger
from services.handlers import handle_webhook
from services.migration import run_migration
from core.config import settings
from services.calendar_sync import start_calendar_sync

# Load environment variables
def load_env_and_migrate():
    load_dotenv(override=True)
    run_migration()

def clear_port(port: int, max_attempts: int = 3) -> bool:
    """Clear a port if it's in use"""
    for attempt in range(max_attempts):
        try:
            # Check if port is in use
            result = subprocess.run(['lsof', '-i', f':{port}'], capture_output=True, text=True)
            if result.stdout:
                # Extract PID and kill process
                for line in result.stdout.split('\n')[1:]:  # Skip header
                    if line:
                        pid = line.split()[1]
                        subprocess.run(['kill', '-9', pid])
                        logger.info(f"Killed process {pid} using port {port}", extra={'emoji_type': 'info'})
                time.sleep(1)  # Wait for port to clear
                return True
            return True  # Port wasn't in use
        except Exception as e:
            logger.warning(f"Attempt {attempt + 1} to clear port {port} failed: {e}", extra={'emoji_type': 'warning'})
            if attempt == max_attempts - 1:
                return False
            time.sleep(1)
    return False

def check_port(port: int) -> bool:
    """Check if port is already in use"""
    try:
        result = subprocess.run(['lsof', '-i', f':{port}'], capture_output=True, text=True)
        if result.stdout:
            logger.error(f"Port {port} is already in use. Please update PLACEHOLDARR_PORT in your .env file.", extra={'emoji_type': 'error'})
            return False
        return True
    except Exception as e:
        logger.error(f"Failed to check port {port}: {e}", extra={'emoji_type': 'error'})
        return False

app = FastAPI()

async def lifespan(app: FastAPI):
    # Load env and run migrations
    load_env_and_migrate()
    logger.info("Placeholdarr service is running.", extra={'emoji_type': 'success'})
    import asyncio
    from services.integrations import check_all_arr_webhooks

    async def arr_webhook_check_loop():
        max_attempts = 10  # 10 minutes at 60s interval
        interval = 60
        for attempt in range(max_attempts):
            all_configured = check_all_arr_webhooks()
            if all_configured:
                logger.info("Calendar sync will begin in 10 seconds...", extra={'emoji_type': 'info'})
                async def delayed_calendar_sync():
                    await asyncio.sleep(10)
                    start_calendar_sync()
                asyncio.create_task(delayed_calendar_sync())
                break
            if attempt == 0:
                # Only log a warning if NO *arrs are configured at all (not just missing webhooks)
                from core.config import settings
                arrs_configured = any([
                    getattr(settings, 'RADARR_URL', None) and getattr(settings, 'RADARR_API_KEY', None),
                    getattr(settings, 'RADARR_4K_URL', None) and getattr(settings, 'RADARR_4K_API_KEY', None),
                    getattr(settings, 'SONARR_URL', None) and getattr(settings, 'SONARR_API_KEY', None),
                    getattr(settings, 'SONARR_4K_URL', None) and getattr(settings, 'SONARR_4K_API_KEY', None)
                ])
                if not arrs_configured:
                    logger.warning("No *arr services are configured. Please add at least one Radarr or Sonarr instance in your .env file.", extra={'emoji_type': 'warning'})
            await asyncio.sleep(interval)
        else:
            logger.warning("Timed out waiting for *arr webhooks to be configured after 10 minutes. Placeholdarr will stop checking automatically, but you can restart the service to try again.", extra={'emoji_type': 'warning'})
            logger.warning("Calendar sync will not start because no *arr webhooks are configured or reachable.", extra={'emoji_type': 'warning'})

    asyncio.create_task(arr_webhook_check_loop())
    yield
    # (Optional) Add shutdown logic here if needed

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

    # Force import of media server clients to trigger connection tests and endpoint checks
    if settings.plex_enabled:
        from services.plex_client import plex  # noqa: F401
    if settings.jellyfin_enabled:
        from services.jellyfin_client import test_jellyfin_connection  # noqa: F401

    # Set port back to 8001 (your existing webhook port)
    port = int(os.getenv('PLACEHOLDARR_PORT', 8001))
    host = getattr(settings, "host", "0.0.0.0")
    logger.info(f"Using host {host} and port {port}", extra={'emoji_type': 'info'})
    
    # Check if port is in use, and try to clear it
    if not check_port(port):
        logger.info(f"Attempting to clear port {port}", extra={'emoji_type': 'info'})
        if clear_port(port):
            logger.info(f"Successfully cleared port {port}", extra={'emoji_type': 'info'})
        else:
            logger.error(f"Failed to clear port {port}. Please choose a different port.", extra={'emoji_type': 'error'})
            sys.exit(1)
    
    # Start the server
    uvicorn.run(app, host=host, port=port, log_level=settings.LOG_LEVEL.lower())
