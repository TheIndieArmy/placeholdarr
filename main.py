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
from services.calendar_sync import start_calendar_sync
from core.config import settings

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

@app.on_event("startup")
async def startup_event():
    # Load env and run migrations
    load_env_and_migrate()
    logger.info("Placeholdarr service is running.", extra={'emoji_type': 'success'})
    logger.info("Calendar sync will begin in 10 seconds...", extra={'emoji_type': 'info'})
    import asyncio
    async def delayed_calendar_sync():
        await asyncio.sleep(10)
        start_calendar_sync()
    asyncio.create_task(delayed_calendar_sync())

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
        from services.plex_client import plex
    if settings.jellyfin_enabled:
        from services.jellyfin_client import test_jellyfin_connection

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
