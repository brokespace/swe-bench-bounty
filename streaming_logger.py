import logging
import time
import asyncio
import os
from typing import Optional, Any, Dict
from helpers.socket import WebSocketManager


class StreamingLogger:
    """
    A unified logging class that handles both local logging and WebSocket streaming.
    Can be used in both main process and subprocess contexts.
    """
    
    def __init__(self, 
                 service_name: str = "submission-scorer",
                 logger_name: Optional[str] = None,
                 ws_manager: Optional[WebSocketManager] = None,
                 process_id: Optional[int] = None):
        """
        Initialize the StreamingLogger.
        
        Args:
            service_name: Name of the service for log messages
            logger_name: Name for the local logger (defaults to service_name)
            ws_manager: WebSocketManager instance for streaming (optional)
            process_id: Process ID to include in messages (auto-detected if None)
        """
        self.service_name = service_name
        self.ws_manager = ws_manager
        self.process_id = process_id or os.getpid()
        
        # Initialize local logger
        logger_name = logger_name or f"{service_name}-{self.process_id}"
        self.logger = logging.getLogger(logger_name)
        
        # Ensure logger has a handler if none exists
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                f'%(asctime)s - %(name)s - %(levelname)s - [Process {self.process_id}] %(message)s'
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)
    
    def _log_locally(self, level: str, message: str, job_id: str, **kwargs):
        """Log message locally using standard Python logging"""
        log_message = f"[{job_id}] {message}"
        extra = {"job_id": job_id, **kwargs}
        
        level_lower = level.lower()
        if level_lower == "info":
            self.logger.info(log_message, extra=extra)
        elif level_lower == "warning":
            self.logger.warning(log_message, extra=extra)
        elif level_lower == "error":
            self.logger.error(log_message, extra=extra)
        elif level_lower == "debug":
            self.logger.debug(log_message, extra=extra)
        else:
            self.logger.info(log_message, extra=extra)
    
    async def _stream_to_websocket(self, level: str, message: str, job_id: str, **kwargs) -> bool:
        """Stream log message to WebSocket if manager is available"""
        if not self.ws_manager:
            return False
        
        log_data = {
            "type": "log",
            "service": self.service_name,
            "level": level,
            "message": f"[Process {self.process_id}] {message}",
            "job_id": job_id,
            "process_id": self.process_id,
            "timestamp": time.time(),
            **kwargs
        }
        
        try:
            success = await self.ws_manager.send_message(log_data)
            if not success:
                self.logger.debug("Failed to stream log to watcher. Log available locally.")
            return success
        except Exception as e:
            self.logger.debug(f"Error streaming log to WebSocket: {e}")
            return False
    
    async def log_async(self, level: str, message: str, job_id: str, **kwargs):
        """
        Asynchronous logging method that handles both local logging and WebSocket streaming.
        """
        # Always log locally first
        self._log_locally(level, message, job_id, **kwargs)
        
        # Try to stream to WebSocket if available
        if self.ws_manager:
            await self._stream_to_websocket(level, message, job_id, **kwargs)
    
    def log_sync(self, level: str, message: str, job_id: str, **kwargs):
        """
        Synchronous logging method that handles both local logging and WebSocket streaming.
        Uses asyncio to run the async WebSocket streaming.
        """
        # Always log locally first
        self._log_locally(level, message, job_id, **kwargs)
        
        # Try to stream to WebSocket if available
        if self.ws_manager:
            try:
                # Check if we're in an event loop
                loop = None
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    pass
                
                if loop:
                    # Create a task in the existing loop
                    asyncio.create_task(self._stream_to_websocket(level, message, job_id, **kwargs))
                else:
                    # Create new event loop for this call
                    asyncio.run(self._stream_to_websocket(level, message, job_id, **kwargs))
            except Exception as e:
                # If WebSocket streaming fails, continue with local logging only
                self.logger.debug(f"WebSocket streaming failed, continuing with local logging: {e}")
    
    # Convenience methods for different log levels
    async def info_async(self, message: str, job_id: str, **kwargs):
        """Log info message asynchronously"""
        await self.log_async("info", message, job_id, **kwargs)
    
    async def warning_async(self, message: str, job_id: str, **kwargs):
        """Log warning message asynchronously"""
        await self.log_async("warning", message, job_id, **kwargs)
    
    async def error_async(self, message: str, job_id: str, **kwargs):
        """Log error message asynchronously"""
        await self.log_async("error", message, job_id, **kwargs)
    
    async def debug_async(self, message: str, job_id: str, **kwargs):
        """Log debug message asynchronously"""
        await self.log_async("debug", message, job_id, **kwargs)
    
    def info(self, message: str, job_id: str, **kwargs):
        """Log info message synchronously"""
        self.log_sync("info", message, job_id, **kwargs)
    
    def warning(self, message: str, job_id: str, **kwargs):
        """Log warning message synchronously"""
        self.log_sync("warning", message, job_id, **kwargs)
    
    def error(self, message: str, job_id: str, **kwargs):
        """Log error message synchronously"""
        self.log_sync("error", message, job_id, **kwargs)
    
    def debug(self, message: str, job_id: str, **kwargs):
        """Log debug message synchronously"""
        self.log_sync("debug", message, job_id, **kwargs)
    
    def set_websocket_manager(self, ws_manager: WebSocketManager):
        """Set or update the WebSocket manager"""
        self.ws_manager = ws_manager
    
    async def close(self):
        """Close the WebSocket connection if available"""
        if self.ws_manager:
            await self.ws_manager.close()


def create_streaming_logger(service_name: str = "submission-scorer",
                          logger_name: Optional[str] = None,
                          ws_manager: Optional[WebSocketManager] = None,
                          process_id: Optional[int] = None) -> StreamingLogger:
    """
    Factory function to create a StreamingLogger instance.
    """
    return StreamingLogger(
        service_name=service_name,
        logger_name=logger_name,
        ws_manager=ws_manager,
        process_id=process_id
    )
