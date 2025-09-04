#!/usr/bin/env python3
"""
Authentication utilities for Bounty Watcher system using Bittensor wallet signatures.
"""

import os
import time
import json
import logging
import functools
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
import bittensor as bt
from bittensor_wallet import Keypair
from scalecodec import ScaleBytes
from fastapi import HTTPException, Request, Depends
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class AuthRequest(BaseModel):
    """Authentication request model"""
    ss58_address: str
    signature: str
    message: str
    timestamp: float


class AuthConfig:
    """Authentication configuration"""
    def __init__(self):
        self.enabled = os.getenv("AUTH_ENABLED", "true").lower() == "true"
        self.allowed_hotkeys = self._parse_allowed_hotkeys()
        self.signature_timeout = int(os.getenv("AUTH_SIGNATURE_TIMEOUT", "300"))  # 5 minutes
        
    def _parse_allowed_hotkeys(self) -> List[str]:
        """Parse allowed hotkeys from environment variable"""
        hotkeys_str = os.getenv("ALLOWED_HOTKEYS", "")
        if not hotkeys_str:
            logger.warning("No ALLOWED_HOTKEYS configured. Authentication will reject all requests.")
            return []
        
        # Support comma-separated list
        hotkeys = [key.strip() for key in hotkeys_str.split(",") if key.strip()]
        logger.info(f"Loaded {len(hotkeys)} allowed hotkeys for authentication")
        return hotkeys
    
    def is_hotkey_allowed(self, hotkey: str) -> bool:
        """Check if a hotkey is in the allowed list"""
        return hotkey in self.allowed_hotkeys


def create_auth_message(timestamp: Optional[float] = None) -> str:
    """Create a standardized authentication message"""
    if timestamp is None:
        timestamp = time.time()
    return f"bounty-watcher-auth:{int(timestamp)}"


def sign_message(wallet: bt.wallet, message: str) -> str:
    """Sign a message with the wallet's hotkey"""
    signature = wallet.hotkey.sign(message)
    return signature.hex()


def verify_signature(hotkey: str, signature_hex: str, message: str) -> bool:
    """Verify a signature against a hotkey and message"""
    try:
        # Create keypair from hotkey address
        keypair = Keypair(ss58_address=hotkey)
        
        # Convert signature from hex
        signature = bytes.fromhex(signature_hex)
        
        # Convert message to bytes
        message_bytes = bytes(message, 'utf-8')
        
        # Verify signature
        is_valid = keypair.verify(message_bytes, signature)
        return is_valid
        
    except Exception as e:
        logger.error(f"Error verifying signature: {e}")
        return False


def verify_auth_request(auth_request: AuthRequest, auth_config: AuthConfig) -> bool:
    """Verify an authentication request"""
    try:
        # Check if authentication is enabled
        if not auth_config.enabled:
            logger.debug("Authentication disabled, allowing request")
            return True
        
        # Check if coldkey is allowed
        if not auth_config.is_hotkey_allowed(auth_request.ss58_address):
            logger.warning(f"ss58_address {auth_request.ss58_address} not in allowed list")
            return False
        
        # Check timestamp (prevent replay attacks)
        current_time = time.time()
        time_diff = abs(current_time - auth_request.timestamp)
        
        if time_diff > auth_config.signature_timeout:
            logger.warning(f"Authentication request timestamp too old: {time_diff}s > {auth_config.signature_timeout}s")
            return False
        
        # Verify signature
        is_valid = verify_signature(
            auth_request.ss58_address,
            auth_request.signature,
            auth_request.message
        )
        
        if not is_valid:
            logger.warning(f"Invalid signature for ss58_address {auth_request.ss58_address}")
            return False
        
        # Verify message format (should contain timestamp)
        expected_message = create_auth_message(auth_request.timestamp)
        if auth_request.message != expected_message:
            logger.warning(f"Invalid message format. Expected: {expected_message}, Got: {auth_request.message}")
            return False
        
        logger.debug(f"Authentication successful for ss58_address {auth_request.ss58_address}")
        return True
        
    except Exception as e:
        logger.error(f"Error during authentication verification: {e}")
        return False


class AuthenticatedClient:
    """Client class for making authenticated requests"""
    
    def __init__(self, wallet: bt.wallet):
        self.wallet = wallet
        self.ss58_address = wallet.hotkey.ss58_address
        
    def create_auth_headers(self) -> Dict[str, str]:
        """Create authentication headers for HTTP requests"""
        timestamp = time.time()
        message = create_auth_message(timestamp)
        signature = sign_message(self.wallet, message)
        
        return {
            "X-Auth-SS58Address": self.ss58_address,
            "X-Auth-Signature": signature,
            "X-Auth-Message": message,
            "X-Auth-Timestamp": str(timestamp)
        }
    
    def create_auth_data(self) -> Dict[str, Any]:
        """Create authentication data for websocket or JSON payloads"""
        timestamp = time.time()
        message = create_auth_message(timestamp)
        signature = sign_message(self.wallet, message)
        
        return {
            "auth": {
                "ss58_address": self.ss58_address,
                "signature": signature,
                "message": message,
                "timestamp": timestamp
            }
        }


def extract_auth_from_headers(request: Request) -> Optional[AuthRequest]:
    """Extract authentication data from HTTP headers"""
    try:
        ss58_address = request.headers.get("X-Auth-SS58Address")
        signature = request.headers.get("X-Auth-Signature")
        message = request.headers.get("X-Auth-Message")
        timestamp_str = request.headers.get("X-Auth-Timestamp")
        
        if not all([ss58_address, signature, message, timestamp_str]):
            return None
        
        timestamp = float(timestamp_str)
        
        return AuthRequest(
            ss58_address=ss58_address,
            signature=signature,
            message=message,
            timestamp=timestamp
        )
        
    except Exception as e:
        logger.error(f"Error extracting auth from headers: {e}")
        return None


def extract_auth_from_data(data: Dict[str, Any]) -> Optional[AuthRequest]:
    """Extract authentication data from JSON payload"""
    try:
        auth_data = data.get("auth")
        if not auth_data:
            return None
        
        return AuthRequest(
            ss58_address=auth_data["ss58_address"],
            signature=auth_data["signature"],
            message=auth_data["message"],
            timestamp=auth_data["timestamp"]
        )
        
    except Exception as e:
        logger.error(f"Error extracting auth from data: {e}")
        return None


def create_auth_dependency(auth_config: AuthConfig):
    """Create a FastAPI dependency for authentication"""
    def auth_dependency(request: Request):
        if not auth_config.enabled:
            return None
            
        # Extract auth from headers
        auth_request = extract_auth_from_headers(request)
        
        if not auth_request:
            raise HTTPException(status_code=401, detail="Authentication required")
        
        if not verify_auth_request(auth_request, auth_config):
            raise HTTPException(status_code=403, detail="Authentication failed")
        
        return auth_request
    
    return auth_dependency


def require_auth(auth_config: AuthConfig):
    """Decorator for FastAPI endpoints that require authentication"""
    def decorator(func):
        if not auth_config.enabled:
            # If auth is disabled, return the original function unchanged
            return func
        
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # Find the request object to perform authentication
            request = None
            
            # Look for Request in args
            for arg in args:
                if isinstance(arg, Request):
                    request = arg
                    break
            
            # Look for Request in kwargs (check for common parameter names)
            if not request:
                for param_name in ['request', 'http_request', 'req']:
                    if param_name in kwargs and isinstance(kwargs[param_name], Request):
                        request = kwargs[param_name]
                        break
            
            if not request:
                raise HTTPException(status_code=500, detail="Request object not found for authentication")
            
            # Extract auth from headers
            auth_request = extract_auth_from_headers(request)
            
            if not auth_request:
                raise HTTPException(status_code=401, detail="Authentication required")
            
            if not verify_auth_request(auth_request, auth_config):
                raise HTTPException(status_code=403, detail="Authentication failed")
            
            return await func(*args, **kwargs)
        
        return wrapper
    return decorator


# Global auth config instance
auth_config = AuthConfig()

# Convenience function to check if auth is enabled
def is_auth_enabled() -> bool:
    return auth_config.enabled
