from dotenv import load_dotenv
load_dotenv()
import uuid
import asyncio
from pydantic import BaseModel
import time
import httpx
import logging
from auth_utils import auth_client
from config import *
logger = logging.getLogger(__name__)

class ScoreNotification(BaseModel):
    job_id: str
    score: float
    timestamp: float

async def score_complete(job_id: str, score: float):
    """Notify the external API server that scoring is complete."""
    if not WATCHER_HOST:
        logger.warning(f"WATCHER_HOST not configured, cannot notify completion for job {job_id}")
        return

    notification_data = ScoreNotification(
        job_id=job_id,
        score=score,
        timestamp=time.time()
    )

    try:
        headers = {}
        if auth_client:
            headers = auth_client.create_auth_headers()
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"http://{WATCHER_HOST}/score-complete",
                json=notification_data.dict(),
                headers=headers
            )
            response.raise_for_status()
            logger.info(f"Successfully notified API server for job {job_id} (score: {score})")
            
    except Exception as e:
        logger.error(f"Error notifying API server for job {job_id}: {e}")
        

async def score_failed(job_id: str, error_message: str):
    """Notify the external API server that scoring has failed."""
    if not WATCHER_HOST:
        logger.warning(f"WATCHER_HOST not configured, cannot notify failure for job {job_id}")
        return
    
    try:
        headers = {}
        if auth_client:
            headers = auth_client.create_auth_headers()
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"http://{WATCHER_HOST}/score-failure",
                json={
                    "job_id": job_id,
                    "error_message": error_message,
                    "timestamp": time.time()
                },
                headers=headers
            )
            response.raise_for_status()
            logger.info(f"Successfully notified API server of failure for job {job_id}")
            
    except Exception as e:
        logger.error(f"Error notifying API server of failure for job {job_id}: {e}")
        
        
def send_heartbeat() -> bool:
    """Send heartbeat to watcher via HTTP (synchronous version for thread)"""
    if not WATCHER_HOST or not SCREENER_ID:
        return False
    
    try:
        heartbeat_data = {
            "screener_id": SCREENER_ID,
            "screener_hotkey": SCREENER_HOTKEY,
            "timestamp": time.time()
        }
        
        headers = {}
        if auth_client:
            headers = auth_client.create_auth_headers()
        
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                f"http://{WATCHER_HOST}/heartbeat",
                json=heartbeat_data,
                headers=headers
            )
            response.raise_for_status()
            logger.debug("Heartbeat sent successfully via HTTP (sync)")
            return True
            
    except Exception as e:
        logger.warning(f"Error sending heartbeat via HTTP (sync): {e}")
        return False
    
def heartbeat_thread():
    """Daemon that sends heartbeat to watcher via HTTP"""
    # Import shutdown_event here to avoid circular imports
    from main import shutdown_event
    
    while not shutdown_event.is_set():
        send_heartbeat()
        # Use wait() instead of sleep() so we can be interrupted
        if shutdown_event.wait(timeout=60):
            break  # Shutdown was signaled
    
    logger.info("Heartbeat thread shutting down...")

async def register_with_watcher():
    """Register this screener with the watcher service on startup"""
    global SCREENER_ID, SCREENER_HOTKEY
    
    if not AUTO_REGISTER:
        logger.info("Auto-registration disabled. Skipping watcher registration.")
        return None
    
    if not WATCHER_HOST:
        logger.warning("WATCHER_HOST not configured. Cannot register with watcher.")
        return None
    
    # Generate a unique hotkey if not provided
    hotkey = SCREENER_HOTKEY or f"screener_{uuid.uuid4().hex[:8]}"
    
    registration_data = {
        "name": SCREENER_NAME,
        "hotkey": hotkey,
        "api_url": SCREENER_API_URL,
        "max_concurrent": MAX_CONCURRENT_TASKS,
        "supported_submission_types": SCREENER_SUPPORTED_TYPES,
        "supported_bounty_ids": SCREENER_SUPPORTED_BOUNTY_IDS,
        "supported_category_ids": SCREENER_SUPPORTED_CATEGORY_IDS
    }
    
    max_retries = 3
    retry_delay = 2
    
    for attempt in range(max_retries):
        try:
            logger.info(f"Attempting to register with watcher at {WATCHER_HOST} (attempt {attempt + 1}/{max_retries})")
            
            headers = {}
            if auth_client:
                headers = auth_client.create_auth_headers()
                logger.debug("Added authentication headers to registration request")
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"http://{WATCHER_HOST}/register-screener",
                    json=registration_data,
                    headers=headers
                )
                response.raise_for_status()
                
                result = response.json()
                screener_id = result.get("screener_id")
                
                logger.info(f"Successfully registered with watcher. Screener ID: {screener_id}, Hotkey: {hotkey}")
                
                SCREENER_ID = screener_id
                if not SCREENER_HOTKEY:
                    SCREENER_HOTKEY = hotkey
                
                return screener_id
                
        except httpx.RequestError as e:
            logger.error(f"Network error during registration (attempt {attempt + 1}): {e}")
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error during registration (attempt {attempt + 1}): {e.response.status_code} - {e.response.text}")
        except Exception as e:
            logger.error(f"Unexpected error during registration (attempt {attempt + 1}): {e}")
        
        if attempt < max_retries - 1:
            logger.info(f"Retrying registration in {retry_delay} seconds...")
            await asyncio.sleep(retry_delay)
            retry_delay *= 2  # Exponential backoff
    
    logger.error(f"Failed to register with watcher after {max_retries} attempts")
    return None
