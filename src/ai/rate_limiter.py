import asyncio
import time
from typing import Dict, Optional


class ModelRateLimiter:
    """
    Per-model rate limiter ensuring a minimum interval between calls to the same model.
    Default interval is at least 4.0 seconds (for 15 RPM).
    """

    def __init__(self, min_interval: float = 4.0):
        self.default_interval = float(min_interval)
        self._last_call: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    def _normalize_model(self, model: str) -> str:
        return str(model).strip().lower()

    def _get_lock(self, model: str) -> asyncio.Lock:
        norm = self._normalize_model(model)
        if norm not in self._locks:
            self._locks[norm] = asyncio.Lock()
        return self._locks[norm]

    async def acquire(self, model: str, min_interval: Optional[float] = None) -> float:
        """
        Enforces rate limiting for the specified model.
        Returns the duration waited (in seconds, 0.0 if no wait was needed).
        """
        norm = self._normalize_model(model)
        async with self._global_lock:
            lock = self._get_lock(norm)

        async with lock:
            interval = min_interval if min_interval is not None else self.default_interval
            now = time.monotonic()
            last = self._last_call.get(norm, 0.0)
            elapsed = now - last
            waited = 0.0

            if elapsed < interval and last > 0.0:
                waited = interval - elapsed
                await asyncio.sleep(waited)

            self._last_call[norm] = time.monotonic()
            return waited

    def reset(self) -> None:
        """Resets all recorded timestamps (useful for tests)."""
        self._last_call.clear()


# Global rate limiter instance
model_rate_limiter = ModelRateLimiter(min_interval=4.0)


def get_model_interval() -> float:
    """Returns the effective minimal delay (at least 4.0 seconds)."""
    try:
        from src.config import settings
        delay = getattr(settings, "ai_request_delay_seconds", 4)
        return max(4.0, float(delay))
    except Exception:
        return 4.0
