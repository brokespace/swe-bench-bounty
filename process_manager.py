import asyncio
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from threading import Thread
import threading
import queue
import time
from typing import Dict, Optional
import logging
from config import *
logger = logging.getLogger(__name__)

def run_grader_process(job_id: str, submission_data):
    """Standalone function to run grader in subprocess (must be at module level for pickling)"""
    # Import inside the process to avoid pickle issues
    from grader import Grader
    grader = Grader(job_id)
    grader.grade(submission_data)

class ProcessManager:
    """Manages multiprocessing jobs without blocking the main event loop"""
    
    def __init__(self, max_concurrent_tasks: int = 10):
        self.max_concurrent_tasks = max_concurrent_tasks
        self.active_jobs: Dict[str, multiprocessing.Process] = {}
        self.job_queue = queue.Queue()
        
        # Use a separate thread for process management
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="process-manager")
        self.manager_thread = None
        self.shutdown_event = threading.Event()
        
        # Start the process manager thread
        self.start_manager()
    
    def start_manager(self):
        """Start the background thread that manages processes"""
        self.manager_thread = Thread(target=self._process_manager_loop, daemon=True)
        self.manager_thread.start()
        logger.info("Process manager thread started")
    
    def _process_manager_loop(self):
        """Main loop for the process manager thread"""
        while not self.shutdown_event.is_set():
            try:
                # Clean up finished jobs
                self._cleanup_finished_jobs()
                
                # Process queued jobs if we have capacity
                self._process_queue()
                
                # Small sleep to prevent CPU spinning
                time.sleep(0.1)
                
            except Exception as e:
                logger.error(f"Error in process manager loop: {e}")
                time.sleep(1)
    
    def _cleanup_finished_jobs(self):
        """Remove finished processes from active jobs"""
        finished = []
        for job_id, process in list(self.active_jobs.items()):
            if not process.is_alive():
                finished.append(job_id)
                
        for job_id in finished:
            del self.active_jobs[job_id]
            logger.info(f"Job {job_id} completed and removed")
    
    def _process_queue(self):
        """Start queued jobs if we have capacity"""
        while len(self.active_jobs) < self.max_concurrent_tasks:
            try:
                # Non-blocking get with timeout
                job_data = self.job_queue.get_nowait()
                self._start_job_process(job_data['job_id'], job_data['submission_data'])
            except queue.Empty:
                break
            except Exception as e:
                logger.error(f"Error processing queued job: {e}")
    
    def _start_job_process(self, job_id: str, submission_data):
        """Actually start the process (called from manager thread)"""
        try:
            process = multiprocessing.Process(
                target=run_grader_process, 
                args=(job_id, submission_data),
                daemon=True  # Ensure child processes are cleaned up
            )
            process.start()
            self.active_jobs[job_id] = process
            logger.info(f"Started process for job {job_id}")
        except Exception as e:
            logger.error(f"Failed to start process for job {job_id}: {e}")
    
    async def submit_job(self, job_id: str, submission_data) -> str:
        """Submit a job for processing (called from async context)"""
        # Run the submission in executor to avoid blocking
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self.executor,
            self._submit_job_sync,
            job_id,
            submission_data
        )
    
    def _submit_job_sync(self, job_id: str, submission_data) -> str:
        """Synchronous job submission (runs in thread pool)"""
        if len(self.active_jobs) < self.max_concurrent_tasks:
            # Can start immediately
            self._start_job_process(job_id, submission_data)
            return "started"
        else:
            # Queue the job
            self.job_queue.put({
                'job_id': job_id,
                'submission_data': submission_data,
                'queued_at': time.time()
            })
            return "queued"
    
    async def terminate_job(self, job_id: str) -> bool:
        """Terminate a specific job"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self.executor,
            self._terminate_job_sync,
            job_id
        )
    
    def _terminate_job_sync(self, job_id: str) -> bool:
        """Synchronous job termination"""
        if job_id in self.active_jobs:
            process = self.active_jobs[job_id]
            process.terminate()
            process.join(timeout=3.0)
            if process.is_alive():
                process.kill()
            del self.active_jobs[job_id]
            return True
        return False
    
    def get_status(self) -> dict:
        """Get current status of the process manager"""
        return {
            'active_jobs': list(self.active_jobs.keys()),
            'active_count': len(self.active_jobs),
            'queue_size': self.job_queue.qsize(),
            'max_concurrent': self.max_concurrent_tasks
        }
    
    def shutdown(self):
        """Shutdown all processes and the manager"""
        self.shutdown_event.set()
        
        # Terminate all active processes
        for job_id, process in self.active_jobs.items():
            try:
                process.terminate()
                process.join(timeout=2.0)
                if process.is_alive():
                    process.kill()
            except Exception as e:
                logger.error(f"Error shutting down job {job_id}: {e}")
        
        self.active_jobs.clear()
        
        # Shutdown the executor
        self.executor.shutdown(wait=True, timeout=5)
        
        # Wait for manager thread
        if self.manager_thread and self.manager_thread.is_alive():
            self.manager_thread.join(timeout=5.0)

# Initialize the process manager globally
process_manager = ProcessManager(max_concurrent_tasks=MAX_CONCURRENT_TASKS)
