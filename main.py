from dotenv import load_dotenv
load_dotenv()
import asyncio
import os
import time
import base64
import logging
import uuid
from typing import Optional, Union, Dict, Any
from enum import Enum
import bittensor as bt
from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks, Request
from pydantic import BaseModel, Field
import json
import httpx
import websockets
import websockets.exceptions
from helpers.socket import WebSocketManager
from auth_utils import AuthenticatedClient, is_auth_enabled, AuthConfig, require_auth
from models import SubmissionData, SubmissionType, FileInfo
from task import BountyTask
# Load environment variables

# Global configuration for concurrent task limiting
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "3"))
task_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

# Configure logging
logger = logging.getLogger("submission-scorer")
console_handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)
logger.setLevel(logging.INFO)

# WebSocket configuration for log streaming
WATCHER_HOST = os.getenv("WATCHER_HOST", "localhost:8001")
SCREENER_ID = os.getenv("SCREENER_ID", None)  # Will be set after registration
SCREENER_HOTKEY = os.getenv("SCREENER_HOTKEY", None)  # Optional: screener hotkey for identification

# Screener self-registration configuration
SCREENER_NAME = os.getenv("SCREENER_NAME", "Submission Scorer")
SCREENER_API_URL = os.getenv("SCREENER_API_URL", "http://localhost:8999")
SCREENER_MAX_CONCURRENT = int(os.getenv("SCREENER_MAX_CONCURRENT", "5"))
SCREENER_SUPPORTED_TYPES = os.getenv("SCREENER_SUPPORTED_TYPES", "FILE,TEXT,URL").split(",")
SCREENER_SUPPORTED_BOUNTY_IDS = os.getenv("SCREENER_SUPPORTED_BOUNTY_IDS", "").split(",") if os.getenv("SCREENER_SUPPORTED_BOUNTY_IDS") else None
SCREENER_SUPPORTED_CATEGORY_IDS = os.getenv("SCREENER_SUPPORTED_CATEGORY_IDS", "").split(",") if os.getenv("SCREENER_SUPPORTED_CATEGORY_IDS") else None
AUTO_REGISTER = os.getenv("AUTO_REGISTER", "true").lower() == "true"
COLDKEY = os.getenv("COLDKEY", None)
HOTKEY = os.getenv("HOTKEY", None)

# Authentication configuration
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() == "true"

# Scorer API authentication (for incoming requests to this scorer)
SCORER_AUTH_ENABLED = os.getenv("SCORER_AUTH_ENABLED", "true").lower() == "true"
SCORER_ALLOWED_HOTKEYS = os.getenv("SCORER_ALLOWED_HOTKEYS", "").split(",") if os.getenv("SCORER_ALLOWED_HOTKEYS") else []

# Initialize wallet for authentication if enabled
auth_client = None
if AUTH_ENABLED:
    try:
        wallet = bt.wallet(name=COLDKEY, hotkey=HOTKEY)
        auth_client = AuthenticatedClient(wallet)
        logger.info(f"Authentication enabled with wallet: {wallet.name}/{wallet.hotkey_str}")
        logger.info(f"hotkey: {wallet.hotkey.ss58_address}")
    except Exception as e:
        logger.error(f"Failed to initialize wallet for authentication: {e}")
        if AUTH_ENABLED:
            raise e
else:
    logger.info("Authentication disabled")

# Initialize scorer API authentication configuration
class ScreenerAuthConfig(AuthConfig):
    """Custom auth config for scorer API endpoints"""
    def __init__(self):
        self.enabled = SCORER_AUTH_ENABLED
        self.allowed_hotkeys = [key.strip() for key in SCORER_ALLOWED_HOTKEYS if key.strip()]
        self.signature_timeout = int(os.getenv("AUTH_SIGNATURE_TIMEOUT", "300"))
        
        if self.enabled:
            if not self.allowed_hotkeys:
                logger.warning("Scorer authentication enabled but no SCORER_ALLOWED_HOTKEYS configured. All requests will be rejected.")
            else:
                logger.info(f"Scorer authentication enabled with {len(self.allowed_hotkeys)} allowed hotkey")
        else:
            logger.info("Scorer API authentication disabled")

screener_auth_config = ScreenerAuthConfig()

# Global websocket manager instance
ws_manager = WebSocketManager(SCREENER_ID, SCREENER_HOTKEY, WATCHER_HOST, auth_client)

# Track active jobs
active_jobs = set()  # Set of job IDs currently being processed
active_tasks = {}    # Dict mapping job_id to BountyTask instances


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
        "max_concurrent": SCREENER_MAX_CONCURRENT,
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


async def get_ws_connection():
    """Get websocket connection (compatibility function)"""
    if await ws_manager.connect():
        return ws_manager.connection
    return None

async def stream_log_to_watcher(level: str, message: str, job_id: str, **kwargs):
    """
    Stream a log line to the watcher via websocket with retry logic.
    """
    # Always log locally first
    if level.lower() == "info":
        logger.info(message, extra={"job_id": job_id, **kwargs})
    elif level.lower() == "warning":
        logger.warning(message, extra={"job_id": job_id, **kwargs})
    elif level.lower() == "error":
        logger.error(message, extra={"job_id": job_id, **kwargs})
    elif level.lower() == "debug":
        logger.debug(message, extra={"job_id": job_id, **kwargs})
    else:
        logger.info(message, extra={"job_id": job_id, **kwargs})
    
    # Try to stream to watcher using the WebSocketManager
    log_data = {
        "type": "log",
        "service": "submission-scorer",
        "level": level,
        "message": message,
        "job_id": job_id,
        "timestamp": time.time(),
        **kwargs
    }
    
    success = await ws_manager.send_message(log_data)
    if not success:
        logger.error("Failed to stream log to watcher. Log will only be available locally.")

app = FastAPI(title="Submission Scorer API", version="1.0.0")


async def periodic_job_status_reporter():
    """Periodically report job status to watcher"""
    while True:
        try:
            if SCREENER_ID and len(active_jobs) >= 0:  # Report even if no jobs running
                job_status_msg = {
                    "type": "job_status",
                    "timestamp": time.time(),
                    "screener_id": SCREENER_ID,
                    "screener_hotkey": SCREENER_HOTKEY,
                    "running_jobs": list(active_jobs),
                    "job_count": len(active_jobs),
                    "max_concurrent": MAX_CONCURRENT_TASKS
                }
                
                success = await ws_manager.send_message(job_status_msg)
                if success:
                    logger.debug(f"Reported job status: {len(active_jobs)} active jobs")
                else:
                    logger.warning("Failed to report job status to watcher")
            
            # Report every 60 seconds
            await asyncio.sleep(60)
            
        except Exception as e:
            logger.error(f"Error in job status reporter: {e}")
            await asyncio.sleep(60)  # Wait before retrying

@app.on_event("startup")
async def startup_event():
    """Handle application startup - register with watcher if enabled"""
    logger.info("Starting Submission Scorer API...")
    
    # Register with watcher service
    screener_id = await register_with_watcher()
    
    if screener_id:
        logger.info(f"Screener registered successfully with ID: {screener_id}")
    else:
        logger.warning("Failed to register with watcher or registration disabled")
    
    # Establish WebSocket connection using the new manager
    if WATCHER_HOST:
        logger.info("Establishing WebSocket connection to watcher...")
        if await ws_manager.connect():
            logger.info("WebSocket connection established successfully")
            
            # Start periodic job status reporting
            asyncio.create_task(periodic_job_status_reporter())
            logger.info("Started periodic job status reporting")
        else:
            logger.warning("Failed to establish WebSocket connection")
    else:
        logger.warning("WATCHER_HOST not configured. Skipping WebSocket connection.")




class ScoreResponse(BaseModel):
    job_id: str
    score: float
    status: str
    message: str


class ScoreNotification(BaseModel):
    job_id: str
    score: float
    timestamp: float




async def notify_api_server(job_id: str, score: float):
    """
    Notify the external API server that scoring is complete.
    """
    if not WATCHER_HOST:
        logger.warning("WATCHER_HOST not configured in .env file", extra={"job_id": job_id})
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
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"http://{WATCHER_HOST}/score-complete",
                json=notification_data.dict(),
                headers=headers,
                timeout=30.0
            )
            response.raise_for_status()
            logger.info(
                f"Successfully notified API server for job {job_id}",
                extra={"job_id": job_id, "score": score}
            )
    except httpx.RequestError as e:
        logger.error(
            f"Error notifying API server: {e}",
            extra={"job_id": job_id, "error": str(e)}
        )
    except httpx.HTTPStatusError as e:
        logger.error(
            f"HTTP error notifying API server: {e}",
            extra={
                "job_id": job_id,
                "error": str(e),
                "status_code": e.response.status_code
            }
        )

async def notify_api_server_failure(job_id: str, error_message: str):
    """
    Notify the external API server that scoring has failed.
    """
    if not WATCHER_HOST:
        logger.warning("WATCHER_HOST not configured in .env file for failure notification", extra={"job_id": job_id})
        return
    
    try:
        headers = {}
        if auth_client:
            headers = auth_client.create_auth_headers()
        
        async with httpx.AsyncClient() as client:
            # Call the failure handling endpoint
            response = await client.post(
                f"http://{WATCHER_HOST}/score-failure",
                json={
                    "job_id": job_id,
                    "error_message": error_message,
                    "timestamp": time.time()
                },
                headers=headers,
                timeout=30.0
            )
            response.raise_for_status()
            logger.info(f"Successfully notified API server of failure for job {job_id}", extra={"job_id": job_id, "error_message": error_message})
    except httpx.RequestError as e:
        logger.error(f"Error notifying API server of failure: {e}", extra={"job_id": job_id, "error": str(e)})
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error notifying API server of failure: {e}", extra={"job_id": job_id, "error": str(e), "status_code": e.response.status_code})

async def background_scoring_process(submission_data: SubmissionData):
    """
    Background task that performs the actual scoring and handles success/failure notifications.
    Uses semaphore to limit concurrent tasks, with queueing for excess tasks.
    """
    # Acquire semaphore to limit concurrent tasks (will wait in queue if limit reached)
    async with task_semaphore:
        bounty_task = None
        try:
            # Add job to active jobs tracking
            active_jobs.add(submission_data.job_id)
            
            # Create BountyTask instance and store it for potential cancellation
            bounty_task = BountyTask(submission_data.job_id, stream_log_to_watcher)
            active_tasks[submission_data.job_id] = bounty_task
            
            active_task_count = MAX_CONCURRENT_TASKS - task_semaphore._value
            logger.info(
                f"Starting background scoring for job {submission_data.job_id} (Active tasks: {active_task_count}/{MAX_CONCURRENT_TASKS})",
                extra={
                    "job_id": submission_data.job_id,
                    "active_tasks": active_task_count,
                    "max_concurrent_tasks": MAX_CONCURRENT_TASKS
                }
            )
            
            # Run the actual scoring process using BountyTask
            score = await bounty_task.score(submission_data)
            
            # Notify API server of successful completion
            await notify_api_server(submission_data.job_id, score)
            
            logger.info(
                f"Background scoring completed successfully for job {submission_data.job_id} with score {score}",
                extra={
                    "job_id": submission_data.job_id,
                    "score": score,
                    "submission_type": submission_data.submission_type.value
                }
            )
            
        except asyncio.CancelledError:
            error_message = "Scoring was cancelled"
            logger.warning(
                f"Background scoring cancelled for job {submission_data.job_id}",
                extra={
                    "job_id": submission_data.job_id,
                    "submission_type": submission_data.submission_type.value
                }
            )
            # Notify API server of cancellation
            await notify_api_server_failure(submission_data.job_id, error_message)
        except Exception as e:
            error_message = f"Scoring failed: {str(e)}"
            logger.error(
                f"Background scoring failed for job {submission_data.job_id}: {error_message}",
                extra={
                    "job_id": submission_data.job_id,
                    "error": str(e),
                    "submission_type": submission_data.submission_type.value
                }
            )
            
            # Notify API server of failure
            await notify_api_server_failure(submission_data.job_id, error_message)
        finally:
            # Clean up task instance and call cleanup
            if bounty_task:
                bounty_task.cleanup()
            
            # Remove job from tracking
            active_jobs.discard(submission_data.job_id)
            active_tasks.pop(submission_data.job_id, None)

@app.post("/score", response_model=ScoreResponse)
@require_auth(screener_auth_config)
async def score_submission(
    submission_data: SubmissionData,
    background_tasks: BackgroundTasks,
    http_request: Request
):
    """
    Score a submission containing either a file, link, or text content.
    Returns immediately and runs scoring in the background.
    """
    try:
        # Add the scoring task to background processing
        background_tasks.add_task(background_scoring_process, submission_data)
        
        # Return immediately with "started" status
        active_tasks = MAX_CONCURRENT_TASKS - task_semaphore._value
        return ScoreResponse(
            job_id=submission_data.job_id,
            score=0.0,  # Score not available yet
            status="started",
            message=f"Scoring has been initiated for {submission_data.submission_type.value} submission. "
                   f"Active tasks: {active_tasks}/{MAX_CONCURRENT_TASKS}"
        )
        
    except Exception as e:
        error_message = f"Error initiating scoring: {str(e)}"
        await stream_log_to_watcher("error", error_message, submission_data.job_id, error=str(e), submission_type=submission_data.submission_type.value)
        
        # Notify API server of immediate failure
        try:
            await notify_api_server_failure(submission_data.job_id, error_message)
        except Exception as notify_error:
            logger.error(f"Failed to notify API server of failure: {notify_error}", extra={"job_id": submission_data.job_id, "notify_error": str(notify_error)})
        
        raise HTTPException(status_code=500, detail=error_message)


@app.post("/kill-job/{job_id}")
@require_auth(screener_auth_config)
async def kill_job_endpoint(job_id: str, http_request: Request):
    """Endpoint for watcher to request killing a specific job"""
    try:
        if job_id in active_jobs:
            logger.info(f"Killing job {job_id} as requested by watcher")
            
            # Get the BountyTask instance if it exists
            bounty_task = active_tasks.get(job_id)
            if bounty_task:
                # Call cleanup to cancel the task and clean up resources
                bounty_task.cleanup()
                logger.info(f"Called cleanup for job {job_id}")
            else:
                logger.warning(f"No BountyTask instance found for job {job_id}")
            
            # Remove from active jobs and tasks tracking
            active_jobs.discard(job_id)
            active_tasks.pop(job_id, None)
            
            # Notify watcher of job failure due to kill request
            await notify_api_server_failure(job_id, "Job killed by watcher request")
            
            return {"message": f"Job {job_id} has been killed and cleaned up", "status": "success"}
        else:
            logger.warning(f"Requested to kill job {job_id} but it's not in active jobs")
            return {"message": f"Job {job_id} not found in active jobs", "status": "not_found"}
            
    except Exception as e:
        logger.error(f"Error killing job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error killing job: {str(e)}")


@app.get("/health")
async def health_check():
    """Health check endpoint with task queue status"""
    active_tasks = MAX_CONCURRENT_TASKS - task_semaphore._value
    available_slots = task_semaphore._value
    
    return {
        "status": "healthy",
        "service": "submission-scorer",
        "screener_id": SCREENER_ID,
        "authentication": {
            "outbound": {
                "enabled": AUTH_ENABLED,
                "wallet_configured": auth_client is not None,
                "ss58_address": auth_client.ss58_address if auth_client else None
            },
            "inbound": {
                "enabled": screener_auth_config.enabled,
                "allowed_hotkeys": len(screener_auth_config.allowed_hotkeys),
                "signature_timeout": screener_auth_config.signature_timeout
            }
        },
        "active_jobs": list(active_jobs),
        "task_queue": {
            "max_concurrent_tasks": MAX_CONCURRENT_TASKS,
            "active_tasks": active_tasks,
            "available_slots": available_slots,
            "queue_full": available_slots == 0
        }
    }


@app.on_event("shutdown")
async def shutdown_event():
    """Handle application shutdown"""
    logger.info("Shutting down Submission Scorer API...")
    await ws_manager.close()
    logger.info("WebSocket manager shut down successfully")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=19000)
