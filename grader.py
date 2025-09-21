import asyncio
from dotenv import load_dotenv
load_dotenv()
import os
from streaming_logger import create_streaming_logger, StreamingLogger
from helpers.socket import WebSocketManager
from models import SubmissionData
from helpers.watcher import score_complete, score_failed
from task import BountyTask
from config import *
from auth_utils import auth_client

class Grader():
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.ws_manager = WebSocketManager(
            screener_id=GRADER_ID,
            screener_hotkey=GRADER_HOTKEY,
            watcher_host=WATCHER_HOST,
            auth_client=auth_client)
        
        self.logger = create_streaming_logger(
            service_name="grader",
            ws_manager=self.ws_manager,
            logger_name="grader",
            job_id=self.job_id
        )
        self.logger.info("Grader initialized")

    def grade(self, submission_data: SubmissionData):
        self.logger.info(f"Grading submission for job {self.job_id}")
        try:
            self.task = BountyTask(self.job_id, self.logger)
            score = self.task.score(submission_data)
            asyncio.run(score_complete(self.job_id, score))
        except Exception as e:
            asyncio.run(score_failed(self.job_id, str(e)))
    
    def kill(self):
        # stubbed
        pass
            
        