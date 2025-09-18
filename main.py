from dotenv import load_dotenv
load_dotenv()
import asyncio
import os
import time
import base64
import logging
import uuid
import threading
import signal
import atexit
from typing import Optional, Union, Dict, Any, List
from enum import Enum
import bittensor as bt
from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks, Request
from pydantic import BaseModel, Field
import json
import httpx
import websockets
import websockets.exceptions
import multiprocessing
from functools import partial
import psutil
from concurrent.futures import ThreadPoolExecutor
from helpers.socket import WebSocketManager
from auth_utils import AuthenticatedClient, is_auth_enabled, AuthConfig, require_auth
from models import SubmissionData, SubmissionType, FileInfo
from task import BountyTask
from streaming_logger import create_streaming_logger, StreamingLogger

# Load environment variables
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "3"))
SCORING_TIMEOUT = int(os.getenv("SCORING_TIMEOUT", "7200"))  # 2 hours default

# WebSocket configuration for log streaming
WATCHER_HOST = os.getenv("WATCHER_HOST", "localhost:8001")
SCREENER_ID = os.getenv("SCREENER_ID", None)
SCREENER_HOTKEY = os.getenv("SCREENER_HOTKEY", None)

# Screener self-registration configuration
SCREENER_NAME = os.getenv("SCREENER_NAME", "Submission Scorer")
SCREENER_API_URL = os.getenv("SCREENER_API_URL", "http://localhost:8999")
SCREENER_SUPPORTED_TYPES = os.getenv("SCREENER_SUPPORTED_TYPES", "FILE,TEXT,URL").split(",")
SCREENER_SUPPORTED_BOUNTY_IDS = os.getenv("SCREENER_SUPPORTED_BOUNTY_IDS", "").split(",") if os.getenv("SCREENER_SUPPORTED_BOUNTY_IDS") else None
SCREENER_SUPPORTED_CATEGORY_IDS = os.getenv("SCREENER_SUPPORTED_CATEGORY_IDS", "").split(",") if os.getenv("SCREENER_SUPPORTED_CATEGORY_IDS") else None
AUTO_REGISTER = os.getenv("AUTO_REGISTER", "true").lower() == "true"
COLDKEY = os.getenv("COLDKEY", None)
HOTKEY = os.getenv("HOTKEY", None)

# Authentication configuration
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() == "true"
SCORER_AUTH_ENABLED = os.getenv("SCORER_AUTH_ENABLED", "true").lower() == "true"
SCORER_ALLOWED_HOTKEYS = os.getenv("SCORER_ALLOWED_HOTKEYS", "").split(",") if os.getenv("SCORER_ALLOWED_HOTKEYS") else []

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("submission-scorer")

# Global variables for cleanup
shutdown_event = threading.Event()
active_jobs: Dict[str, Dict] = {}  # job_id -> {"process": Process, "start_time": float}
job_lock = threading.Lock()

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
                logger.info(f"Scorer authentication enabled with {len(self.allowed_hotkeys)} allowed hotkey(s)")
        else:
            logger.info("Scorer API authentication disabled")

screener_auth_config = ScreenerAuthConfig()

# Global websocket manager instance - only main process uses this
ws_manager = WebSocketManager(SCREENER_ID, SCREENER_HOTKEY, WATCHER_HOST, auth_client)

# Global streaming logger instance - only main process uses this
main_streaming_logger = create_streaming_logger(
    service_name="submission-scorer",
    logger_name="main-process",
    ws_manager=ws_manager
)

# Log queue for IPC communication between main process and subprocesses
log_queue = multiprocessing.Queue()

class LogMessage:
    """Structure for log messages sent from subprocess to main process"""
    def __init__(self, level: str, message: str, job_id: str, timestamp: float = None, **kwargs):
        self.level = level
        self.message = message
        self.job_id = job_id
        self.timestamp = timestamp or time.time()
        self.kwargs = kwargs

def subprocess_target(submission_data_dict: Dict[str, Any], log_queue: multiprocessing.Queue):
    """
    Target function for subprocess execution.
    This function runs in a separate process and communicates back to main process via queue.
    """
    try:
        import asyncio
        import os
        import sys
        import logging
        from pathlib import Path
        from dotenv import load_dotenv
        
        # Load environment variables in subprocess
        load_dotenv()
        
        # Add current directory to path for imports
        current_dir = os.path.dirname(os.path.abspath(__file__))
        if current_dir not in sys.path:
            sys.path.append(current_dir)
        
        from models import SubmissionData
        from task import BountyTask
        
        # Configure logging for subprocess
        logging.basicConfig(
            level=logging.INFO,
            format=f'%(asctime)s - %(name)s - %(levelname)s - [Subprocess {os.getpid()}] %(message)s'
        )
        subprocess_logger = logging.getLogger(f"scorer-subprocess-{os.getpid()}")
        
        # Reconstruct SubmissionData from dictionary
        submission_data = SubmissionData(**submission_data_dict)
        job_id = submission_data.job_id
        
        subprocess_logger.info(f"Starting scoring subprocess for job {job_id}")
        
        # Send log to main process via queue
        def log_to_main_process(level: str, message: str, **kwargs):
            """Send log message to main process via queue"""
            try:
                log_msg = LogMessage(level, message, job_id, **kwargs)
                log_queue.put(log_msg, timeout=5.0)
            except Exception as e:
                subprocess_logger.warning(f"Failed to send log to main process: {e}")
        
        # Log function for BountyTask
        def task_log(level: str, message: str, **kwargs):
            """Combined logging function that logs both locally and to main process"""
            # Log locally
            subprocess_logger.log(getattr(logging, level.upper(), logging.INFO), message)
            # Send to main process
            log_to_main_process(level, message, **kwargs)
        
        async def run_scoring():
            """Main scoring function"""
            try:
                log_to_main_process("info", f"Subprocess {os.getpid()} starting scoring", 
                                  submission_type=submission_data.submission_type.value)
                
                # Create BountyTask with our logging function
                bounty_task = BountyTask(job_id, task_log)
                
                log_to_main_process("info", "Beginning score computation")
                
                # Run the scoring
                score = await bounty_task.score(submission_data)
                
                log_to_main_process("info", f"Scoring completed with score {score}", 
                                  score=score, submission_type=submission_data.submission_type.value)
                
                subprocess_logger.info(f"Scoring completed for job {job_id} with score {score}")
                
                return {
                    "status": "success",
                    "score": score,
                    "job_id": job_id
                }
                
            except Exception as e:
                error_msg = f"Scoring failed: {str(e)}"
                log_to_main_process("error", error_msg, error=str(e), 
                                  submission_type=submission_data.submission_type.value)
                subprocess_logger.error(error_msg, exc_info=True)
                return {
                    "status": "error",
                    "error": error_msg,
                    "job_id": job_id
                }
        
        # Run the scoring
        result = asyncio.run(run_scoring())
        
        # Send final result via queue
        try:
            log_queue.put(result, timeout=10.0)
        except Exception as e:
            subprocess_logger.error(f"Failed to send result to main process: {e}")
            
    except Exception as e:
        # Handle any unexpected errors
        import logging
        logger = logging.getLogger("subprocess-error")
        error_msg = f"Subprocess failed unexpectedly: {str(e)}"
        logger.error(error_msg, exc_info=True)
        
        try:
            error_result = {
                "status": "error",
                "error": error_msg,
                "job_id": submission_data_dict.get("job_id", "unknown")
            }
            log_queue.put(error_result, timeout=5.0)
        except:
            pass  # Nothing more we can do

def cleanup_job(job_id: str):
    """Clean up a completed job"""
    with job_lock:
        if job_id in active_jobs:
            job_info = active_jobs[job_id]
            process = job_info["process"]
            
            # Clean up process if needed
            if process and process.is_alive():
                try:
                    process.terminate()
                    process.join(timeout=5.0)
                    if process.is_alive():
                        process.kill()
                        process.join(timeout=2.0)
                except:
                    pass
            
            del active_jobs[job_id]
            logger.info(f"Cleaned up job {job_id}")

def cleanup_finished_jobs():
    """Clean up any jobs that have finished"""
    with job_lock:
        finished_jobs = []
        for job_id, job_info in active_jobs.items():
            process = job_info["process"]
            if not process.is_alive():
                finished_jobs.append(job_id)
        
        for job_id in finished_jobs:
            cleanup_job(job_id)

def terminate_job(job_id: str, timeout: float = 10.0) -> bool:
    """Terminate a specific job"""
    with job_lock:
        if job_id not in active_jobs:
            return False
        
        job_info = active_jobs[job_id]
        process = job_info["process"]
        
        if not process.is_alive():
            cleanup_job(job_id)
            return True
        
        try:
            logger.info(f"Terminating job {job_id} (PID: {process.pid})")
            
            # Try graceful termination first
            process.terminate()
            process.join(timeout=timeout/2)
            
            # Force kill if needed
            if process.is_alive():
                logger.warning(f"Force killing job {job_id}")
                process.kill()
                process.join(timeout=timeout/2)
            
            success = not process.is_alive()
            if success:
                cleanup_job(job_id)
            
            return success
            
        except Exception as e:
            logger.error(f"Error terminating job {job_id}: {e}")
            cleanup_job(job_id)  # Clean up anyway
            return False

def shutdown_all_jobs():
    """Shutdown all active jobs"""
    with job_lock:
        if not active_jobs:
            return
        
        logger.info(f"Shutting down {len(active_jobs)} active jobs...")
        
        # Terminate all processes
        for job_id, job_info in list(active_jobs.items()):
            process = job_info["process"]
            if process.is_alive():
                try:
                    logger.info(f"Terminating job {job_id}")
                    process.terminate()
                except:
                    pass
        
        # Wait for graceful termination
        start_time = time.time()
        while active_jobs and (time.time() - start_time) < 15.0:
            finished_jobs = []
            for job_id, job_info in active_jobs.items():
                if not job_info["process"].is_alive():
                    finished_jobs.append(job_id)
            
            for job_id in finished_jobs:
                cleanup_job(job_id)
            
            if active_jobs:
                time.sleep(0.1)
        
        # Force kill remaining processes
        for job_id, job_info in list(active_jobs.items()):
            process = job_info["process"]
            if process.is_alive():
                try:
                    logger.warning(f"Force killing job {job_id}")
                    process.kill()
                    process.join(timeout=2.0)
                except:
                    pass
            cleanup_job(job_id)
        
        logger.info("All jobs shut down")

async def log_processor():
    """
    Background task that processes log messages from subprocesses and forwards them to watcher
    """
    while not shutdown_event.is_set():
        try:
            # Check for messages from subprocesses (non-blocking)
            try:
                message = log_queue.get_nowait()
                
                if isinstance(message, LogMessage):
                    # Forward log to watcher via websocket
                    try:
                        await main_streaming_logger.log_async(
                            message.level, 
                            message.message, 
                            message.job_id, 
                            **message.kwargs
                        )
                    except Exception as e:
                        logger.warning(f"Failed to forward log to watcher: {e}")
                        # Log locally as fallback
                        logger.log(
                            getattr(logging, message.level.upper(), logging.INFO),
                            f"[{message.job_id}] {message.message}"
                        )
                
                elif isinstance(message, dict) and "status" in message:
                    # This is a result message from subprocess
                    job_id = message.get("job_id")
                    if message["status"] == "success":
                        score = message["score"]
                        await notify_api_server(job_id, score)
                        await main_streaming_logger.info_async(
                            f"Job completed successfully with score {score}",
                            job_id,
                            score=score
                        )
                    else:
                        error = message.get("error", "Unknown error")
                        await notify_api_server_failure(job_id, error)
                        await main_streaming_logger.error_async(
                            f"Job failed: {error}",
                            job_id,
                            error=error
                        )
                    
                    # Clean up the job
                    cleanup_job(job_id)
                    
            except:
                # No messages available, continue
                pass
            
            # Clean up finished jobs periodically
            cleanup_finished_jobs()
            
            # Small delay to prevent busy waiting
            await asyncio.sleep(0.1)
            
        except Exception as e:
            logger.error(f"Error in log processor: {e}")
            await asyncio.sleep(1.0)

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

async def send_heartbeat_http():
    """Send heartbeat to watcher via HTTP"""
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
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"http://{WATCHER_HOST}/heartbeat",
                json=heartbeat_data,
                headers=headers
            )
            response.raise_for_status()
            logger.debug("Heartbeat sent successfully via HTTP")
            return True
            
    except Exception as e:
        logger.warning(f"Error sending heartbeat via HTTP: {e}")
        return False

async def periodic_heartbeat_sender():
    """Periodically send heartbeat to watcher via HTTP"""
    while not shutdown_event.is_set():
        try:
            if SCREENER_ID:
                await send_heartbeat_http()
            await asyncio.sleep(30)
        except Exception as e:
            logger.error(f"Error in heartbeat sender: {e}")
            await asyncio.sleep(30)

async def periodic_job_status_reporter():
    """Periodically report job status to watcher via HTTP"""
    while not shutdown_event.is_set():
        try:
            with job_lock:
                active_job_ids = list(active_jobs.keys())
            
            if SCREENER_ID:
                job_status_data = {
                    "screener_id": SCREENER_ID,
                    "screener_hotkey": SCREENER_HOTKEY,
                    "running_jobs": active_job_ids,
                    "job_count": len(active_job_ids),
                    "max_concurrent": MAX_CONCURRENT_TASKS,
                    "timestamp": time.time()
                }
                
                try:
                    headers = {}
                    if auth_client:
                        headers = auth_client.create_auth_headers()
                    
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        response = await client.post(
                            f"http://{WATCHER_HOST}/job-status",
                            json=job_status_data,
                            headers=headers
                        )
                        response.raise_for_status()
                        logger.debug(f"Reported job status: {len(active_job_ids)} active jobs")
                        
                except Exception as e:
                    logger.warning(f"Error reporting job status: {e}")
            
            await asyncio.sleep(60)
            
        except Exception as e:
            logger.error(f"Error in job status reporter: {e}")
            await asyncio.sleep(60)

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

async def notify_api_server_failure(job_id: str, error_message: str):
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

async def background_scoring_process(submission_data: SubmissionData):
    """Background task that manages scoring in a subprocess"""
    job_id = submission_data.job_id
    
    # Check if we have capacity
    with job_lock:
        if len(active_jobs) >= MAX_CONCURRENT_TASKS:
            error_msg = f"Maximum concurrent tasks ({MAX_CONCURRENT_TASKS}) reached, rejecting job {job_id}"
            logger.warning(error_msg)
            await main_streaming_logger.warning_async(error_msg, job_id)
            await notify_api_server_failure(job_id, "Server at capacity")
            return
    
    try:
        await main_streaming_logger.info_async(f"Starting background scoring", job_id,
                                             submission_type=submission_data.submission_type.value)
        
        # Convert submission_data to dictionary for subprocess
        submission_data_dict = submission_data.dict()
        
        # Start subprocess
        process = multiprocessing.Process(
            target=subprocess_target,
            args=(submission_data_dict, log_queue),
            name=f"scorer-{job_id}"
        )
        process.start()
        
        # Track the job
        with job_lock:
            active_jobs[job_id] = {
                "process": process,
                "start_time": time.time()
            }
        
        logger.info(f"Started scoring process for job {job_id} (PID: {process.pid})")
        await main_streaming_logger.info_async(f"Scoring process started (PID: {process.pid})", job_id)
        
        # Monitor the process
        start_time = time.time()
        while process.is_alive() and (time.time() - start_time) < SCORING_TIMEOUT:
            await asyncio.sleep(1.0)
        
        # Check if process completed normally
        if process.is_alive():
            # Timeout - terminate the process
            logger.warning(f"Job {job_id} timed out after {SCORING_TIMEOUT} seconds")
            await main_streaming_logger.warning_async(f"Job timed out after {SCORING_TIMEOUT} seconds", job_id)
            
            terminate_job(job_id)
            await notify_api_server_failure(job_id, f"Scoring timed out after {SCORING_TIMEOUT} seconds")
        else:
            # Process completed - result should be in queue and handled by log_processor
            logger.info(f"Job {job_id} completed (exit code: {process.exitcode})")
            
            # Clean up if process failed
            if process.exitcode != 0:
                await main_streaming_logger.error_async(f"Scoring process failed with exit code {process.exitcode}", job_id)
                cleanup_job(job_id)
                await notify_api_server_failure(job_id, f"Scoring process failed with exit code {process.exitcode}")
        
    except Exception as e:
        error_msg = f"Error in background scoring: {str(e)}"
        logger.error(error_msg, exc_info=True)
        await main_streaming_logger.error_async(error_msg, job_id, error=str(e))
        
        # Clean up
        cleanup_job(job_id)
        await notify_api_server_failure(job_id, error_msg)

# Initialize FastAPI app
app = FastAPI(title="Submission Scorer API", version="2.0.0")

@app.on_event("startup")
async def startup_event():
    """Handle application startup"""
    logger.info("Starting Submission Scorer API v2.0...")
    
    # Register signal handlers for graceful shutdown
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, initiating shutdown...")
        shutdown_event.set()
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    # Register cleanup on exit
    atexit.register(shutdown_all_jobs)
    
    # Register with watcher service
    screener_id = await register_with_watcher()
    if screener_id:
        logger.info(f"Screener registered successfully with ID: {screener_id}")
    else:
        logger.warning("Failed to register with watcher or registration disabled")
    
    # Start background tasks
    if WATCHER_HOST:
        logger.info("Starting background services...")
        
        # Start log processor
        asyncio.create_task(log_processor())
        logger.info("Started log processor")
        
        # Start heartbeat sender
        asyncio.create_task(periodic_heartbeat_sender())
        logger.info("Started periodic heartbeat sender")
        
        # Start job status reporter
        asyncio.create_task(periodic_job_status_reporter())
        logger.info("Started periodic job status reporter")
        
        # Establish WebSocket connection
        logger.info("Establishing WebSocket connection for log streaming...")
        if await ws_manager.connect():
            logger.info("WebSocket connection established successfully")
        else:
            logger.warning("Failed to establish WebSocket connection")
    else:
        logger.warning("WATCHER_HOST not configured. Skipping background services.")

@app.post("/score", response_model=ScoreResponse)
@require_auth(screener_auth_config)
async def score_submission(
    submission_data: SubmissionData,
    background_tasks: BackgroundTasks,
    http_request: Request
):
    """Score a submission containing either a file, link, or text content."""
    try:
        # Add the scoring task to background processing
        background_tasks.add_task(background_scoring_process, submission_data)
        
        # Return immediately with "started" status
        with job_lock:
            active_count = len(active_jobs)
        
        return ScoreResponse(
            job_id=submission_data.job_id,
            score=0.0,  # Score not available yet
            status="started",
            message=f"Scoring has been initiated for {submission_data.submission_type.value} submission. "
                   f"Active tasks: {active_count}/{MAX_CONCURRENT_TASKS}"
        )
        
    except Exception as e:
        error_message = f"Error initiating scoring: {str(e)}"
        logger.error(error_message, exc_info=True)
        
        try:
            await main_streaming_logger.error_async(error_message, submission_data.job_id, error=str(e))
            await notify_api_server_failure(submission_data.job_id, error_message)
        except Exception as notify_error:
            logger.error(f"Failed to notify API server of failure: {notify_error}")
        
        raise HTTPException(status_code=500, detail=error_message)

@app.post("/kill-job/{job_id}")
@require_auth(screener_auth_config)
async def kill_job_endpoint(job_id: str, http_request: Request):
    """Endpoint for watcher to request killing a specific job"""
    try:
        with job_lock:
            job_exists = job_id in active_jobs
        
        if job_exists:
            logger.info(f"Killing job {job_id} as requested by watcher")
            success = terminate_job(job_id, timeout=3.0)
            
            if success:
                status_message = f"Job {job_id} has been terminated successfully"
                kill_status = "terminated"
            else:
                status_message = f"Job {job_id} termination failed"
                kill_status = "failed"
            
            await main_streaming_logger.warning_async(f"Job {job_id} killed by watcher request", job_id)
            await notify_api_server_failure(job_id, "Job killed by watcher request")
            
            return {
                "message": status_message, 
                "status": "success" if success else "partial",
                "kill_status": kill_status,
                "process_terminated": success
            }
        else:
            return {"message": f"Job {job_id} not found in active jobs", "status": "not_found"}
            
    except Exception as e:
        logger.error(f"Error killing job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error killing job: {str(e)}")

@app.get("/health")
async def health_check():
    """Health check endpoint with comprehensive status information"""
    with job_lock:
        active_job_ids = list(active_jobs.keys())
        active_count = len(active_jobs)
    
    return {
        "status": "healthy",
        "service": "submission-scorer",
        "version": "2.0.0",
        "screener_id": SCREENER_ID,
        "communication": {
            "status_reporting": "HTTP",
            "log_streaming": "WebSocket",
            "websocket_connected": ws_manager.is_connected() if ws_manager else False,
            "log_queue_size": log_queue.qsize() if log_queue else 0
        },
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
        "active_jobs": active_job_ids,
        "task_queue": {
            "max_concurrent_tasks": MAX_CONCURRENT_TASKS,
            "active_tasks": active_count,
            "available_slots": MAX_CONCURRENT_TASKS - active_count,
            "queue_full": active_count >= MAX_CONCURRENT_TASKS
        },
        "configuration": {
            "scoring_timeout": SCORING_TIMEOUT,
            "auto_register": AUTO_REGISTER,
            "watcher_host": WATCHER_HOST
        }
    }

@app.on_event("shutdown")
async def shutdown_event_handler():
    """Handle application shutdown"""
    logger.info("Shutting down Submission Scorer API...")
    
    # Set shutdown event
    shutdown_event.set()
    
    # Shutdown all active jobs
    shutdown_all_jobs()
    
    # Send final heartbeat
    if SCREENER_ID:
        try:
            await send_heartbeat_http()
            logger.info("Sent final heartbeat before shutdown")
        except Exception as e:
            logger.error(f"Error sending final heartbeat: {e}")
    
    # Close WebSocket manager
    try:
        await ws_manager.close()
        logger.info("WebSocket manager closed")
    except Exception as e:
        logger.error(f"Error closing WebSocket manager: {e}")
    
    # Close streaming logger
    try:
        await main_streaming_logger.close()
        logger.info("Streaming logger closed")
    except Exception as e:
        logger.error(f"Error closing streaming logger: {e}")
    
    logger.info("Shutdown completed")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=19000)