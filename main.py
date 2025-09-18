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
import multiprocessing
from functools import partial
import signal
import psutil
from threading import Lock
from helpers.socket import WebSocketManager
from auth_utils import AuthenticatedClient, is_auth_enabled, AuthConfig, require_auth
from models import SubmissionData, SubmissionType, FileInfo
from task import BountyTask
from streaming_logger import create_streaming_logger, StreamingLogger
# Load environment variables

# Global configuration for concurrent task limiting
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "3"))
task_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

# Process management configuration
MAX_WORKER_PROCESSES = int(os.getenv("MAX_WORKER_PROCESSES", str(min(4, multiprocessing.cpu_count()))))

class ProcessManager:
    """Manages individual multiprocessing.Process instances for better control"""
    
    def __init__(self):
        self.active_processes = {}  # job_id -> Process object
        self.process_lock = Lock()  # Thread-safe access to active_processes
        
    def start_process(self, job_id: str, target_func, args):
        """Start a new process for the given job"""
        with self.process_lock:
            if job_id in self.active_processes:
                logger.warning(f"Process for job {job_id} already exists")
                return False
                
            process = multiprocessing.Process(
                target=target_func,
                args=args,
                name=f"scorer-{job_id}"
            )
            process.start()
            self.active_processes[job_id] = process
            logger.info(f"Started process {process.pid} for job {job_id}")
            return True
    
    def terminate_process(self, job_id: str, timeout: float = 5.0) -> bool:
        """Terminate a specific process, with graceful->forceful escalation"""
        with self.process_lock:
            process = self.active_processes.get(job_id)
            if not process:
                logger.warning(f"No process found for job {job_id}")
                return False
                
            if not process.is_alive():
                logger.info(f"Process for job {job_id} is already dead")
                self.active_processes.pop(job_id, None)
                return True
                
            try:
                logger.info(f"Terminating process {process.pid} for job {job_id}")
                
                # First try graceful termination
                process.terminate()
                process.join(timeout=timeout)
                
                # If still alive, force kill
                if process.is_alive():
                    logger.warning(f"Process {process.pid} didn't terminate gracefully, force killing")
                    process.kill()
                    process.join(timeout=2.0)
                
                # Clean up zombie processes and check if really dead
                if process.is_alive():
                    logger.error(f"Failed to kill process {process.pid} for job {job_id}")
                    return False
                else:
                    logger.info(f"Successfully terminated process {process.pid} for job {job_id}")
                    self.active_processes.pop(job_id, None)
                    return True
                    
            except Exception as e:
                logger.error(f"Error terminating process for job {job_id}: {e}")
                # Try to clean up anyway
                self.active_processes.pop(job_id, None)
                return False
    
    def cleanup_finished_processes(self):
        """Clean up processes that have finished naturally"""
        with self.process_lock:
            finished_jobs = []
            for job_id, process in self.active_processes.items():
                if not process.is_alive():
                    finished_jobs.append(job_id)
                    
            for job_id in finished_jobs:
                process = self.active_processes.pop(job_id)
                logger.debug(f"Cleaned up finished process for job {job_id} (exit code: {process.exitcode})")
    
    def get_active_job_ids(self):
        """Get list of currently active job IDs"""
        with self.process_lock:
            return list(self.active_processes.keys())
    
    def get_process_count(self):
        """Get count of active processes"""
        with self.process_lock:
            return len(self.active_processes)
    
    def shutdown_all(self, timeout: float = 10.0):
        """Shutdown all active processes"""
        with self.process_lock:
            if not self.active_processes:
                return
                
            logger.info(f"Shutting down {len(self.active_processes)} active processes...")
            
            # First try graceful termination for all
            for job_id, process in self.active_processes.items():
                if process.is_alive():
                    logger.info(f"Terminating process {process.pid} for job {job_id}")
                    process.terminate()
            
            # Wait for graceful shutdown
            start_time = time.time()
            while self.active_processes and (time.time() - start_time) < timeout:
                finished_jobs = []
                for job_id, process in self.active_processes.items():
                    if not process.is_alive():
                        finished_jobs.append(job_id)
                        
                for job_id in finished_jobs:
                    self.active_processes.pop(job_id)
                    logger.info(f"Process for job {job_id} terminated gracefully")
                
                if self.active_processes:
                    time.sleep(0.1)
            
            # Force kill any remaining processes
            if self.active_processes:
                logger.warning(f"Force killing {len(self.active_processes)} remaining processes")
                for job_id, process in list(self.active_processes.items()):
                    if process.is_alive():
                        logger.warning(f"Force killing process {process.pid} for job {job_id}")
                        process.kill()
                        process.join(timeout=1.0)
                    self.active_processes.pop(job_id)
            
            logger.info("All processes shut down")

# Global process manager instance
process_manager = ProcessManager()

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
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "5"))
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

# Global streaming logger instance
main_streaming_logger = create_streaming_logger(
    service_name="submission-scorer",
    logger_name="main-process",
    ws_manager=ws_manager
)

# Active jobs are now fully managed by ProcessManager
# No need for separate tracking - process_manager is the single source of truth


def run_scoring_in_process_target(submission_data_dict: Dict[str, Any], result_queue: multiprocessing.Queue):
    """Wrapper function to run scoring in a separate process.
    
    This function must be pickleable and runs in a separate process context.
    It cannot access the main process's variables or async functions.
    """
    try:
        # Import modules needed in the subprocess
        import os
        import sys
        import logging
        import time
        import asyncio
        from pathlib import Path
        from dotenv import load_dotenv
        
        # Load environment variables in subprocess
        load_dotenv()
        
        # Add the current directory to Python path for imports
        current_dir = os.path.dirname(os.path.abspath(__file__))
        if current_dir not in sys.path:
            sys.path.append(current_dir)
        
        # Import our modules
        from models import SubmissionData, SubmissionType
        from task import BountyTask
        from helpers.socket import WebSocketManager
        from auth_utils import AuthenticatedClient
        from streaming_logger import create_streaming_logger
        import bittensor as bt
        
        # Configure logging for this process
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - [Process] %(message)s'
        )
        logger = logging.getLogger(f"scorer-process-{os.getpid()}")
        
        # Reconstruct SubmissionData from dictionary
        submission_data = SubmissionData(**submission_data_dict)
        
        logger.info(f"Starting scoring process for job {submission_data.job_id} in process {os.getpid()}")
        
        # Initialize authentication client for subprocess if enabled
        auth_client = None
        auth_enabled = os.getenv("AUTH_ENABLED", "true").lower() == "true"
        if auth_enabled:
            try:
                coldkey = os.getenv("COLDKEY", None)
                hotkey = os.getenv("HOTKEY", None)
                if coldkey and hotkey:
                    wallet = bt.wallet(name=coldkey, hotkey=hotkey)
                    auth_client = AuthenticatedClient(wallet)
                    logger.info(f"Subprocess authentication enabled with wallet: {wallet.name}/{wallet.hotkey_str}")
            except Exception as e:
                logger.error(f"Failed to initialize wallet for subprocess authentication: {e}")
        
        # Initialize WebSocket manager for this subprocess
        watcher_host = os.getenv("WATCHER_HOST", "localhost:8001")
        screener_id = os.getenv("SCREENER_ID", None)
        screener_hotkey = os.getenv("SCREENER_HOTKEY", None)
        
        subprocess_ws_manager = WebSocketManager(screener_id, screener_hotkey, watcher_host, auth_client)
        
        # Create StreamingLogger for this subprocess
        subprocess_streaming_logger = create_streaming_logger(
            service_name="submission-scorer",
            logger_name=f"subprocess-{os.getpid()}",
            ws_manager=subprocess_ws_manager,
            process_id=os.getpid()
        )
        
        # Create synchronous wrapper for compatibility with BountyTask
        def subprocess_log(level: str, message: str, job_id: str, **kwargs):
            """Synchronous wrapper for StreamingLogger"""
            subprocess_streaming_logger.log_sync(level, message, job_id, **kwargs)
        
        # Main async function that handles websocket connection and scoring
        async def run_scoring_with_websocket():
            """Main scoring function with websocket connection management"""
            try:
                # Establish WebSocket connection
                logger.info(f"Establishing WebSocket connection for subprocess {os.getpid()}")
                connected = await subprocess_ws_manager.connect()
                if connected:
                    logger.info(f"WebSocket connection established successfully in subprocess {os.getpid()}")
                else:
                    logger.warning(f"Failed to establish WebSocket connection in subprocess {os.getpid()}, will use local logging only")
                
                # Create BountyTask with subprocess logger
                bounty_task = BountyTask(submission_data.job_id, subprocess_log)
                
                # Log start of scoring with websocket streaming
                await subprocess_streaming_logger.info_async(
                    f"Starting scoring in subprocess {os.getpid()}", 
                    submission_data.job_id,
                    submission_type=submission_data.submission_type.value
                )
                
                # Run the scoring
                score = await bounty_task.score(submission_data)
                
                await subprocess_streaming_logger.info_async(
                    f"Scoring completed with score {score}", 
                    submission_data.job_id,
                    score=score,
                    submission_type=submission_data.submission_type.value
                )
                
                logger.info(f"Scoring completed for job {submission_data.job_id} with score {score}")
                
                return {
                    "status": "success",
                    "score": score,
                    "job_id": submission_data.job_id
                }
                
            except Exception as e:
                error_msg = f"Scoring failed in subprocess: {str(e)}"
                await subprocess_streaming_logger.error_async(
                    error_msg, 
                    submission_data.job_id,
                    error=str(e),
                    submission_type=submission_data.submission_type.value
                )
                logger.error(error_msg, exc_info=True)
                raise e
                
            finally:
                # Clean up WebSocket connection
                try:
                    await subprocess_streaming_logger.close()
                    logger.info(f"WebSocket connection closed for subprocess {os.getpid()}")
                except Exception as e:
                    logger.error(f"Error closing WebSocket connection in subprocess: {e}")
        
        # Run the main scoring function with asyncio
        result = asyncio.run(run_scoring_with_websocket())
        result_queue.put(result)
        
    except Exception as e:
        # Import logging in case it's not available
        import logging
        logger = logging.getLogger(f"scorer-process-error")
        error_msg = f"Scoring failed in subprocess: {str(e)}"
        logger.error(error_msg, exc_info=True)
        
        result = {
            "status": "error",
            "error": error_msg,
            "job_id": submission_data_dict.get("job_id", "unknown")
        }
        result_queue.put(result)


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
    if not WATCHER_HOST:
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
        logger.error(f"Error sending heartbeat via HTTP: {e}")
        return False


async def send_job_status_http(status_data: dict):
    """Send job status to watcher via HTTP"""
    if not WATCHER_HOST:
        return False
    
    try:
        headers = {}
        if auth_client:
            headers = auth_client.create_auth_headers()
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"http://{WATCHER_HOST}/job-status",
                json=status_data,
                headers=headers
            )
            response.raise_for_status()
            logger.debug("Job status sent successfully via HTTP")
            return True
            
    except Exception as e:
        logger.error(f"Error sending job status via HTTP: {e}")
        return False


async def periodic_heartbeat_sender():
    """Periodically send heartbeat to watcher via HTTP"""
    while True:
        try:
            if SCREENER_ID:
                success = await send_heartbeat_http()
                if not success:
                    logger.warning("Failed to send heartbeat to watcher")
            
            # Send heartbeat every 30 seconds
            await asyncio.sleep(30)
            
        except Exception as e:
            logger.error(f"Error in heartbeat sender: {e}")
            await asyncio.sleep(30)  # Wait before retrying


async def get_ws_connection():
    """Get websocket connection (compatibility function)"""
    if await ws_manager.connect():
        return ws_manager.connection
    return None

async def stream_log_to_watcher(level: str, message: str, job_id: str, **kwargs):
    """
    Stream a log line to the watcher via websocket with retry logic.
    Wrapper function for backward compatibility - uses StreamingLogger internally.
    """
    await main_streaming_logger.log_async(level, message, job_id, **kwargs)


async def stream_process_log_to_watcher(level: str, message: str, job_id: str, process_id: int, **kwargs):
    """Stream logs from subprocess to watcher with process context"""
    await stream_log_to_watcher(level, f"[Process {process_id}] {message}", job_id, process_id=process_id, **kwargs)

app = FastAPI(title="Submission Scorer API", version="1.0.0")


async def periodic_job_status_reporter():
    """Periodically report job status to watcher via HTTP"""
    while True:
        try:
            # Get active jobs directly from process manager
            active_job_ids = process_manager.get_active_job_ids()
            
            if SCREENER_ID and len(active_job_ids) >= 0:  # Report even if no jobs running
                job_status_data = {
                    "screener_id": SCREENER_ID,
                    "screener_hotkey": SCREENER_HOTKEY,
                    "running_jobs": active_job_ids,
                    "job_count": len(active_job_ids),
                    "max_concurrent": MAX_CONCURRENT_TASKS,
                    "timestamp": time.time()
                }
                
                success = await send_job_status_http(job_status_data)
                if success:
                    logger.debug(f"Reported job status via HTTP: {len(active_job_ids)} active jobs")
                else:
                    logger.warning("Failed to report job status to watcher via HTTP")
            
            # Report every 60 seconds
            await asyncio.sleep(60)
            
        except Exception as e:
            logger.error(f"Error in job status reporter: {e}")
            await asyncio.sleep(60)  # Wait before retrying

@app.on_event("startup")
async def startup_event():
    """Handle application startup - register with watcher if enabled"""
    
    logger.info("Starting Submission Scorer API...")
    
    # Initialize the process manager
    logger.info(f"ProcessManager initialized with support for {MAX_WORKER_PROCESSES} max processes")
    logger.info("Using multiprocessing.Process for individual job control")
    
    # Register with watcher service
    screener_id = await register_with_watcher()
    
    if screener_id:
        logger.info(f"Screener registered successfully with ID: {screener_id}")
    else:
        logger.warning("Failed to register with watcher or registration disabled")
    
    # Start HTTP-based status reporting
    if WATCHER_HOST:
        logger.info("Starting HTTP-based status reporting...")
        
        # Start periodic heartbeat sender
        asyncio.create_task(periodic_heartbeat_sender())
        logger.info("Started periodic heartbeat sender (HTTP)")
        
        # Start periodic job status reporting
        asyncio.create_task(periodic_job_status_reporter())
        logger.info("Started periodic job status reporting (HTTP)")
        
        # Establish WebSocket connection for log streaming only
        logger.info("Establishing WebSocket connection for log streaming...")
        if await ws_manager.connect():
            logger.info("WebSocket connection established successfully for log streaming")
        else:
            logger.warning("Failed to establish WebSocket connection for log streaming")
    else:
        logger.warning("WATCHER_HOST not configured. Skipping status reporting and log streaming.")




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
    Uses semaphore to limit concurrent tasks and multiprocessing.Process for CPU-intensive work.
    """
    # Acquire semaphore to limit concurrent tasks (will wait in queue if limit reached)
    async with task_semaphore:
        result_queue = None
        try:
            
            active_task_count = MAX_CONCURRENT_TASKS - task_semaphore._value
            await stream_log_to_watcher(
                "info",
                f"Starting background scoring (Active tasks: {active_task_count}/{MAX_CONCURRENT_TASKS})",
                submission_data.job_id,
                active_tasks=active_task_count,
                max_concurrent_tasks=MAX_CONCURRENT_TASKS,
                submission_type=submission_data.submission_type.value
            )
            
            # Convert submission_data to dictionary for pickling
            submission_data_dict = submission_data.dict()
            
            # Create a queue for getting results from the subprocess
            result_queue = multiprocessing.Queue()
            
            await stream_log_to_watcher(
                "info",
                "Starting scoring process",
                submission_data.job_id
            )
            
            # Start the process using our ProcessManager
            success = process_manager.start_process(
                submission_data.job_id,
                run_scoring_in_process_target,
                (submission_data_dict, result_queue)
            )
            
            if not success:
                raise Exception("Failed to start scoring process")
            
            # Wait for the process to complete or timeout
            process_timeout = int(os.getenv("SCORING_TIMEOUT", 60*60*2))  # 2 hr default
            
            await stream_log_to_watcher(
                "info",
                f"Waiting for scoring process to complete (timeout: {process_timeout}s)",
                submission_data.job_id
            )
            
            # Poll for result with timeout
            start_time = time.time()
            result = None
            
            while time.time() - start_time < process_timeout:
                try:
                    # Check if result is available (non-blocking)
                    if not result_queue.empty():
                        result = result_queue.get_nowait()
                        break
                except:
                    pass
                
                # Check if process is still alive
                if submission_data.job_id not in process_manager.get_active_job_ids():
                    # Process finished, try to get result one more time
                    try:
                        if not result_queue.empty():
                            result = result_queue.get_nowait()
                        break
                    except:
                        break
                
                # Small sleep to prevent busy waiting
                await asyncio.sleep(0.1)
            
            # Clean up finished processes
            process_manager.cleanup_finished_processes()
            
            if result is None:
                # Timeout or process died without result
                await stream_log_to_watcher(
                    "warning",
                    f"Scoring process timed out or died after {process_timeout}s, terminating",
                    submission_data.job_id
                )
                
                # Try to terminate the process
                process_manager.terminate_process(submission_data.job_id)
                
                error_message = f"Scoring timed out after {process_timeout} seconds"
                await notify_api_server_failure(submission_data.job_id, error_message)
                
                await stream_log_to_watcher(
                    "error",
                    f"Background scoring failed: {error_message}",
                    submission_data.job_id,
                    error=error_message,
                    submission_type=submission_data.submission_type.value
                )
                return
            
            # Check result and handle success/failure
            if result["status"] == "success":
                score = result["score"]
                
                # Notify API server of successful completion
                await notify_api_server(submission_data.job_id, score)
                
                await stream_log_to_watcher(
                    "info",
                    f"Background scoring completed successfully with score {score}",
                    submission_data.job_id,
                    score=score,
                    submission_type=submission_data.submission_type.value
                )
                
            else:
                # Handle process failure
                error_message = result.get("error", "Unknown process error")
                await stream_log_to_watcher(
                    "error",
                    f"Background scoring failed in subprocess: {error_message}",
                    submission_data.job_id,
                    error=error_message,
                    submission_type=submission_data.submission_type.value
                )
                
                # Notify API server of failure
                await notify_api_server_failure(submission_data.job_id, error_message)
            
        except asyncio.CancelledError:
            error_message = "Scoring was cancelled"
            await stream_log_to_watcher(
                "warning",
                "Background scoring cancelled",
                submission_data.job_id,
                submission_type=submission_data.submission_type.value
            )
            
            # Try to terminate the process if it exists
            process_manager.terminate_process(submission_data.job_id)
            
            # Notify API server of cancellation
            await notify_api_server_failure(submission_data.job_id, error_message)
            
        except Exception as e:
            error_message = f"Scoring failed: {str(e)}"
            await stream_log_to_watcher(
                "error",
                f"Background scoring failed: {error_message}",
                submission_data.job_id,
                error=str(e),
                submission_type=submission_data.submission_type.value
            )
            
            # Try to terminate the process if it exists
            process_manager.terminate_process(submission_data.job_id)
            
            # Notify API server of failure
            await notify_api_server_failure(submission_data.job_id, error_message)
            
        finally:
            # Clean up
            if result_queue:
                try:
                    result_queue.close()
                except:
                    pass
            
            # Ensure process is cleaned up
            process_manager.cleanup_finished_processes()

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
        if job_id in process_manager.get_active_job_ids():
            logger.info(f"Killing job {job_id} as requested by watcher")
            
            # Try to terminate the process
            success = process_manager.terminate_process(job_id, timeout=3.0)
            
            if success:
                logger.info(f"Successfully terminated process for job {job_id}")
                status_message = f"Job {job_id} process has been terminated successfully"
                kill_status = "terminated"
            else:
                logger.warning(f"Failed to terminate process for job {job_id}, marking as killed anyway")
                status_message = f"Job {job_id} marked as killed (process termination failed)"
                kill_status = "marked_killed"
            
            # Notify watcher of job failure due to kill request
            await notify_api_server_failure(job_id, "Job killed by watcher request")
            
            await stream_log_to_watcher(
                "warning", 
                f"Job {job_id} killed by watcher request", 
                job_id,
                kill_status=kill_status
            )
            
            return {
                "message": status_message, 
                "status": "success",
                "kill_status": kill_status,
                "process_terminated": success
            }
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
    active_job_ids = process_manager.get_active_job_ids()
    
    return {
        "status": "healthy",
        "service": "submission-scorer",
        "screener_id": SCREENER_ID,
        "communication": {
            "status_reporting": "HTTP",
            "log_streaming": "WebSocket",
            "websocket_connected": ws_manager.is_connected() if ws_manager else False
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
            "active_tasks": active_tasks,
            "available_slots": available_slots,
            "queue_full": available_slots == 0
        },
        "process_manager": {
            "max_worker_processes": MAX_WORKER_PROCESSES,
            "active_processes": process_manager.get_process_count(),
            "active_process_jobs": active_job_ids,
            "manager_initialized": process_manager is not None
        }
    }


@app.on_event("shutdown")
async def shutdown_event():
    """Handle application shutdown"""
    
    logger.info("Shutting down Submission Scorer API...")
    
    # Shutdown all active processes
    logger.info("Shutting down ProcessManager...")
    process_manager.shutdown_all(timeout=15.0)
    logger.info("ProcessManager shut down successfully")
    
    # Send final heartbeat to indicate shutdown
    if SCREENER_ID:
        try:
            await send_heartbeat_http()
            logger.info("Sent final heartbeat before shutdown")
        except Exception as e:
            logger.error(f"Error sending final heartbeat: {e}")
    
    await ws_manager.close()
    logger.info("WebSocket manager shut down successfully")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=19000)
