from dotenv import load_dotenv
load_dotenv()
import os

MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "1"))
SCORING_TIMEOUT = int(os.getenv("SCORING_TIMEOUT", 3600*5))  # 5 hours default

# WebSocket configuration for log streaming
WATCHER_HOST = os.getenv("WATCHER_HOST", "localhost:8001")
GRADER_ID = SCREENER_ID = os.getenv("SCREENER_ID", None)
GRADER_HOTKEY = SCREENER_HOTKEY = os.getenv("SCREENER_HOTKEY", None)

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