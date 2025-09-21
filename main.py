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
from auth_utils import AuthenticatedClient, is_auth_enabled, AuthConfig, require_auth, auth_client, auth_config
from models import SubmissionData, SubmissionType, FileInfo
from task import BountyTask
from helpers.watcher import register_with_watcher, heartbeat_thread,  send_heartbeat, score_failed
from grader import Grader

from config import *
from process_manager import process_manager

# Set up logging
logger = logging.getLogger(__name__)

# Global shutdown event
shutdown_event = threading.Event()

# Global task references for proper shutdown
job_status_task = None

class ScoreResponse(BaseModel):
    job_id: str
    score: float
    status: str
    message: str

async def job_status_daemon():
    """Periodically report job status to watcher via HTTP"""
    while not shutdown_event.is_set():
        try:
            active_job_ids = list(process_manager.active_jobs.keys())
            
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
            
            # Use asyncio.wait_for with sleep to make it interruptible
            try:
                await asyncio.wait_for(asyncio.sleep(60), timeout=60.0)
            except asyncio.TimeoutError:
                pass  # Normal timeout, continue loop
            
        except Exception as e:
            logger.error(f"Error in job status reporter: {e}")
            try:
                await asyncio.wait_for(asyncio.sleep(60), timeout=60.0)
            except asyncio.TimeoutError:
                pass  # Normal timeout, continue loop


# Initialize FastAPI app
app = FastAPI(title="Submission Grader API", version="2.0.0")

@app.on_event("startup")
async def startup_event():
    """Handle application startup"""
    logger.info("Starting Submission Grader API v2.0...")
    
    # Register signal handlers for graceful shutdown
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, initiating shutdown...")
        shutdown_event.set()
        
        # Set up a timer to force exit if graceful shutdown hangs
        def force_exit():
            logger.error("Graceful shutdown timed out. Forcing exit...")
            os._exit(1)
        
        timer = threading.Timer(15.0, force_exit)  # Force exit after 15 seconds
        timer.start()
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    # Register cleanup on exit
    atexit.register(process_manager.shutdown)
    
    # Register with watcher service
    screener_id = await register_with_watcher()
    if screener_id:
        logger.info(f"Screener registered successfully with ID: {screener_id}")
    else:
        logger.warning("Failed to register with watcher or registration disabled")
    
    # Start background tasks
    if WATCHER_HOST:
        global heartbeat_thread
        # Start heartbeat sender in separate thread
        heartbeat_thread = threading.Thread(
            target=heartbeat_thread,
            name="heartbeat-sender",
        )
        heartbeat_thread.start()
        logger.info("Started heartbeat sender thread")
        
        # Start job status reporter
        global job_status_task
        job_status_task = asyncio.create_task(job_status_daemon())
        logger.info("Started periodic job status reporter")
        
        # Establish WebSocket connection
        logger.info("Establishing WebSocket connection for log streaming...")
    else:
        logger.warning("WATCHER_HOST not configured. Skipping background services.")

@app.post("/score", response_model=ScoreResponse)
@require_auth(auth_config)
async def score_submission(
    submission_data: SubmissionData,
    background_tasks: BackgroundTasks,
    http_request: Request
):
    """Score a submission containing either a file, link, or text content."""
    try:
        # Submit job to process manager (non-blocking)
        job_status = await process_manager.submit_job(
            submission_data.job_id, 
            submission_data
        )
        
        # Get current status
        status = process_manager.get_status()
        
        if job_status == "started":
            message = f"Scoring initiated for {submission_data.submission_type.value} submission. " \
                     f"Active tasks: {status['active_count']}/{status['max_concurrent']}"
        else:  # queued
            message = f"Server at capacity, {submission_data.submission_type.value} submission queued. " \
                     f"Active tasks: {status['active_count']}/{status['max_concurrent']}, " \
                     f"Queue size: {status['queue_size']}"
        
        return ScoreResponse(
            job_id=submission_data.job_id,
            score=0.0,
            status=job_status,
            message=message
        )
        
    except Exception as e:
        error_message = f"Error initiating scoring: {str(e)}"
        logger.error(error_message, exc_info=True)
        
        await score_failed(submission_data.job_id, error_message)
        raise HTTPException(status_code=500, detail=error_message)

@app.post("/kill-job/{job_id}")
@require_auth(auth_config)
async def kill_job_endpoint(job_id: str, http_request: Request):
    """Endpoint for watcher to request killing a specific job"""
    try:
        success = await process_manager.terminate_job(job_id)
        
        if success:
            await score_failed(job_id, "Job killed by watcher request")
            return {
                "message": f"Job {job_id} has been terminated successfully",
                "status": "success",
                "kill_status": "terminated"
            }
        else:
            return {
                "message": f"Job {job_id} not found",
                "status": "not_found"
            }
            
    except Exception as e:
        logger.error(f"Error killing job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error killing job: {str(e)}")

@app.get("/health")
async def health_check():
    """Health check endpoint with comprehensive status information"""
    active_job_ids = list(process_manager.active_jobs.keys())
    active_count = len(process_manager.active_jobs)
    
    # Get queued job IDs safely from the queue
    queued_job_ids = []
    temp_queue = []
    try:
        # Extract all items from queue to get job IDs
        while not process_manager.job_queue.empty():
            job_data = process_manager.job_queue.get_nowait()
            queued_job_ids.append(job_data['job_id'])
            temp_queue.append(job_data)
        
        # Put items back in the queue
        for job_data in temp_queue:
            process_manager.job_queue.put(job_data)
            
    except Exception as e:
        logger.warning(f"Error getting queued job IDs: {e}")
        queued_job_ids = []
    
    queue_count = len(queued_job_ids)
    
    return {
        "status": "healthy",
        "service": "submission-scorer",
        "version": "2.0.0",
        "screener_id": SCREENER_ID,
        "authentication": {
            "outbound": {
                "enabled": AUTH_ENABLED,
                "wallet_configured": auth_client is not None,
                "ss58_address": auth_client.ss58_address if auth_client else None
            },
            "inbound": {
                "enabled": auth_config.enabled,
                "allowed_hotkeys": len(auth_config.allowed_hotkeys),
                "signature_timeout": auth_config.signature_timeout
            }
        },
        "active_jobs": active_job_ids,
        "queued_jobs": queued_job_ids,
        "task_queue": {
            "max_concurrent_tasks": MAX_CONCURRENT_TASKS,
            "active_tasks": active_count,
            "queued_tasks": queue_count,
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
    global heartbeat_thread, job_status_task
    logger.info("Shutting down Submission Scorer API...")
    
    # Set shutdown event
    shutdown_event.set()
    
    try:
        # Cancel async background tasks
        if job_status_task and not job_status_task.done():
            logger.info("Cancelling job status reporter task...")
            job_status_task.cancel()
            try:
                await asyncio.wait_for(job_status_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                logger.info("Job status reporter task cancelled")
        
        # Shutdown all active jobs first to stop any ongoing work
        logger.info("Shutting down process manager...")
        process_manager.shutdown()
        
        # Wait for heartbeat thread to finish
        if heartbeat_thread and heartbeat_thread.is_alive():
            logger.info("Waiting for heartbeat thread to shutdown...")
            heartbeat_thread.join(timeout=3.0)
            if heartbeat_thread.is_alive():
                logger.warning("Heartbeat thread did not shutdown gracefully within timeout")
            else:
                logger.info("Heartbeat thread shutdown completed")
        
        # Send final heartbeat
        if SCREENER_ID:
            try:
                send_heartbeat()
                logger.info("Sent final heartbeat before shutdown")
            except Exception as e:
                logger.error(f"Error sending final heartbeat: {e}")
        
        # Close streaming logger
        try:
            logger.info("Streaming logger closed")
        except Exception as e:
            logger.error(f"Error closing streaming logger: {e}")
        
        logger.info("Shutdown completed")
    
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")
        # Force exit if there's an error in shutdown
        os._exit(1)

if __name__ == "__main__":
    multiprocessing.set_start_method('spawn', force=True)
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=19000)