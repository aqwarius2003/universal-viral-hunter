import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional, Dict, Any, Tuple, List
from src.config import settings


def hash_password(password: str, salt: Optional[str] = None) -> Tuple[str, str]:
    """
    Hashes password using PBKDF2-HMAC-SHA256 with 100,000 iterations.
    Returns (password_hash, salt).
    """
    if not salt:
        salt = os.urandom(16).hex()
    pwd_bytes = password.encode("utf-8")
    salt_bytes = salt.encode("utf-8")
    pwd_hash = hashlib.pbkdf2_hmac("sha256", pwd_bytes, salt_bytes, 100_000).hex()
    return pwd_hash, salt


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    """Verifies that the provided password matches the expected hash."""
    computed_hash, _ = hash_password(password, salt)
    return hmac.compare_digest(computed_hash, expected_hash)


def create_session_token(
    username: str,
    role: str,
    secret_key: Optional[str] = None,
    max_age_days: int = 14,
    max_age_seconds: Optional[int] = None,
) -> str:
    """Creates a base64-encoded, HMAC-SHA256 signed session token."""
    key = (secret_key or settings.secret_key).encode("utf-8")
    if max_age_seconds is not None:
        exp = int(time.time()) + max_age_seconds
    else:
        exp = int(time.time()) + max_age_days * 86400
    payload = {"u": username, "r": role, "exp": exp}
    payload_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_json).decode("utf-8").rstrip("=")
    
    signature = hmac.new(key, payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{signature}"


def decode_session_token(
    token: str,
    secret_key: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Decodes and validates an HMAC-SHA256 signed session token."""
    if not token or "." not in token:
        return None

    try:
        payload_b64, signature = token.split(".", 1)
        key = (secret_key or settings.secret_key).encode("utf-8")
        expected_signature = hmac.new(key, payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(signature, expected_signature):
            return None

        # Fix base64 padding
        padding = 4 - (len(payload_b64) % 4)
        if padding != 4:
            payload_b64 += "=" * padding

        payload_json = base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8")
        payload = json.loads(payload_json)

        if int(payload.get("exp", 0)) < time.time():
            return None

        return {
            "username": payload.get("u"),
            "role": payload.get("r"),
            "exp": payload.get("exp"),
        }
    except Exception:
        return None


class LoginRateLimiter:
    """
    In-memory rate limiter to protect against brute-force password guessing.
    Tracks failed attempts per IP. Blocks IP after 5 failed attempts for 15 minutes.
    """
    def __init__(self, max_attempts: int = 5, window_seconds: int = 300, lockout_seconds: int = 900):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._attempts: Dict[str, List[float]] = {}
        self._lockouts: Dict[str, float] = {}

    def is_locked(self, ip: str) -> bool:
        """Checks if the given IP is currently locked out."""
        now = time.time()
        lock_until = self._lockouts.get(ip, 0)
        if now < lock_until:
            return True
        if ip in self._lockouts:
            del self._lockouts[ip]
        return False

    def lockout_remaining(self, ip: str) -> int:
        """Returns remaining seconds for locked out IP, or 0 if not locked."""
        now = time.time()
        lock_until = self._lockouts.get(ip, 0)
        if now < lock_until:
            return max(1, int(lock_until - now))
        return 0

    def record_failure(self, ip: str) -> bool:
        """Records a failed login attempt for an IP. Returns True if now locked."""
        now = time.time()
        if self.is_locked(ip):
            return True

        attempts = self._attempts.get(ip, [])
        attempts = [t for t in attempts if now - t < self.window_seconds]
        attempts.append(now)
        self._attempts[ip] = attempts

        if len(attempts) >= self.max_attempts:
            self._lockouts[ip] = now + self.lockout_seconds
            self._attempts.pop(ip, None)
            return True

        return False

    def record_success(self, ip: str) -> None:
        """Clears failed attempts and lockouts for an IP upon successful login."""
        self._attempts.pop(ip, None)
        self._lockouts.pop(ip, None)

    def reset(self, ip: str) -> None:
        """Alias for record_success."""
        self.record_success(ip)


login_rate_limiter = LoginRateLimiter()


def mask_secret(secret_str: Optional[str], visible_start: int = 7, visible_end: int = 4) -> str:
    """Masks a secret token/key for safe display in UI/API (e.g. 'AQ.Ab8RN6...****')."""
    if not secret_str:
        return ""
    s = str(secret_str).strip()
    if not s or s.startswith("your_"):
        return s
    if len(s) <= 4:
        return "****"
    if len(s) <= (visible_start + visible_end):
        return f"{s[0]}***{s[-1]}"
    return f"{s[:visible_start]}...{s[-visible_end:]}"
