import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from aiohttp import web
from src.auth import LoginRateLimiter

STATIC_DIR = Path(__file__).parent / "static"

login_limiter = LoginRateLimiter(max_attempts=5, window_seconds=300, lockout_seconds=900)
pipeline_lock = asyncio.Lock()
pipeline_runtime: Dict[str, Any] = {
    "state": "idle",
    "tweets_found": 0,
    "viral_found": 0,
    "error_message": None,
    "started_at": None,
    "finished_at": None,
}

_failed_summaries: Dict[str, float] = {}  # tweet_id -> last_failed_timestamp
_in_progress_summaries: set = set()


def get_client_ip(request: web.Request) -> str:
    """Extracts client IP address respecting X-Forwarded-For if present."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote or "127.0.0.1"


def update_pipeline_runtime(
    state: str,
    tweets_found: int = 0,
    viral_found: int = 0,
    error_message: Optional[str] = None,
) -> None:
    """Updates runtime state of the pipeline so dashboard UI status reflects reality."""
    pipeline_runtime["state"] = state
    pipeline_runtime["tweets_found"] = tweets_found
    pipeline_runtime["viral_found"] = viral_found
    pipeline_runtime["error_message"] = error_message
    if state == "running":
        pipeline_runtime["started_at"] = datetime.now(timezone.utc).isoformat()
        pipeline_runtime["finished_at"] = None
    elif state in ("success", "error", "idle"):
        pipeline_runtime["finished_at"] = datetime.now(timezone.utc).isoformat()
