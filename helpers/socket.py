import websockets
import websockets.exceptions
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, ConnectionClosedOK
import asyncio
import time
import json
from typing import Optional, Any
import logging
import sys
import os

# Add parent directory to path for auth_utils import
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger()

class WebSocketManager:
    def __init__(self, screener_id: str, screener_hotkey: str, watcher_host: str, auth_client=None):
        self.connection: Optional[Any] = None
        self.connection_lock = asyncio.Lock()
        self.reconnection_attempts = 0
        self.max_reconnection_attempts = 10
        self.reconnection_delay_base = 2
        self.connection_task = None
        self.should_reconnect = True
        self.is_connecting = False
        self.screener_id = screener_id
        self.screener_hotkey = screener_hotkey
        self.watcher_host = watcher_host
        self.auth_client = auth_client
        
    def _is_connection_open(self) -> bool:
        """Return True if the underlying websocket connection is open.

        Supports multiple versions of the `websockets` library by checking
        several potential attributes: `.closed` (bool), `.state` (Enum), `.open` (bool).
        """
        conn = self.connection
        if conn is None:
            return False

        # Prefer explicit boolean attributes where available
        if hasattr(conn, "closed"):
            try:
                return not bool(getattr(conn, "closed"))
            except Exception:
                pass

        # Newer versions expose a `state` Enum with name OPEN/CLOSED
        state = getattr(conn, "state", None)
        if state is not None:
            state_name = getattr(state, "name", None)
            if isinstance(state_name, str):
                return state_name == "OPEN"
            # Fallback: compare string repr
            state_str = str(state)
            if isinstance(state_str, str):
                return state_str.endswith(".OPEN") or state_str == "OPEN"

        # Some implementations expose `.open` boolean
        if hasattr(conn, "open"):
            try:
                return bool(getattr(conn, "open"))
            except Exception:
                pass

        # If unsure, be conservative and report not open
        return False

    async def connect(self) -> bool:
        """Establish websocket connection with retry logic"""
        async with self.connection_lock:
            if self.is_connecting:
                return self.connection is not None and self._is_connection_open()
                
            self.is_connecting = True
            
            try:
                if self.connection is not None and self._is_connection_open():
                    return True
                
                # Calculate exponential backoff delay
                if self.reconnection_attempts > 0:
                    delay = min(self.reconnection_delay_base * (2 ** (self.reconnection_attempts - 1)), 60)
                    logger.info(f"Waiting {delay}s before reconnection attempt {self.reconnection_attempts + 1}")
                    await asyncio.sleep(delay)
                
                logger.info(f"Attempting to connect to watcher websocket at {self.watcher_host}")
                
                self.connection = await websockets.connect(
                    f"ws://{self.watcher_host}/ws/logs",
                    ping_interval=20,  # Send ping every 20 seconds
                    ping_timeout=10,   # Wait 10 seconds for pong
                    close_timeout=10,  # Wait 10 seconds for close
                    max_size=10**7,    # 10MB max message size
                    compression=None   # Disable compression for better performance
                )
                
                self.reconnection_attempts = 0
                logger.info("Successfully connected to watcher websocket for log streaming")
                
                # Start connection monitoring task
                await self._start_background_tasks()
                
                return True
                
            except Exception as e:
                self.reconnection_attempts += 1
                logger.error(f"Failed to connect to watcher websocket (attempt {self.reconnection_attempts}): {e}")
                
                if self.reconnection_attempts >= self.max_reconnection_attempts:
                    logger.error(f"Max reconnection attempts ({self.max_reconnection_attempts}) reached. Giving up.")
                    self.should_reconnect = False
                    return False
                
                return False
            finally:
                self.is_connecting = False
    
    async def _start_background_tasks(self):
        """Start connection monitoring task"""
        # Cancel existing tasks
        await self._stop_background_tasks()
        
        # Start connection monitoring task
        self.connection_task = asyncio.create_task(self._connection_monitor())
    
    async def _stop_background_tasks(self):
        """Stop background tasks"""
        if self.connection_task and not self.connection_task.done():
            self.connection_task.cancel()
            try:
                await self.connection_task
            except asyncio.CancelledError:
                pass
    
    
    async def _connection_monitor(self):
        """Monitor connection health and handle reconnection"""
        try:
            while self.should_reconnect:
                if not self.connection or not self._is_connection_open():
                    logger.warning("Connection lost, attempting to reconnect...")
                    await self.connect()
                
                await asyncio.sleep(10)  # Check every 10 seconds
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Connection monitor error: {e}")
    
    async def send_message(self, message: dict) -> bool:
        """Send message with automatic reconnection"""
        max_retries = 3
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                # Ensure we have a connection
                if not await self.connect():
                    logger.warning(f"No websocket connection available for message send (attempt {retry_count + 1})")
                    retry_count += 1
                    await asyncio.sleep(1)
                    continue
                
                # Add authentication to message if available
                if self.auth_client and "auth" not in message:
                    auth_data = self.auth_client.create_auth_data()
                    message.update(auth_data)
                
                await self.connection.send(json.dumps(message))
                return True
                
            except (ConnectionClosed, ConnectionClosedError, ConnectionClosedOK):
                logger.warning(f"WebSocket connection closed during message send (attempt {retry_count + 1})")
                self.connection = None
                retry_count += 1
                if retry_count < max_retries:
                    await asyncio.sleep(1)
                    
            except Exception as e:
                logger.error(f"Failed to send message (attempt {retry_count + 1}): {e}")
                retry_count += 1
                if retry_count < max_retries:
                    await asyncio.sleep(1)
        
        logger.error(f"Failed to send message after {max_retries} attempts")
        return False
    
    async def close(self):
        """Close websocket connection and stop background tasks"""
        self.should_reconnect = False
        
        await self._stop_background_tasks()
        
        if self._is_connection_open():
            try:
                await self.connection.close()
            except Exception as e:
                logger.error(f"Error closing websocket connection: {e}")
        
        self.connection = None
    
    def is_connected(self) -> bool:
        """Check if WebSocket is connected"""
        return self._is_connection_open()