import sys
import os
import subprocess
import time
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request
from core.logger import logger
from services.handlers import handle_webhook

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

app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        # Extract source port from request
        source_port = request.client.port
        response = handle_webhook(data, source_port)
        return response
    except Exception as e:
        logger.error(f"Webhook handling failed: {e}", extra={'emoji_type': 'error'})
        raise

# ...other FastAPI endpoints if needed...

if __name__ == '__main__':
    import uvicorn
    port = 8000
    
    if not clear_port(port):
        logger.error(f"Could not clear port {port}, exiting", extra={'emoji_type': 'error'})
        sys.exit(1)
        
    uvicorn.run(app, host="0.0.0.0", port=port)
