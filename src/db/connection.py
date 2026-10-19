import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import aiosqlite
from src.config import settings
from src.utils.hashtags import normalize_hashtags


def get_db_path(custom_path: Optional[str] = None) -> str:
    """Returns the effective database path."""
    return custom_path or settings.db_path


def get_db_connection(custom_path: Optional[str] = None):
    """Returns an aiosqlite connection context manager for the target database."""
    path = get_db_path(custom_path)
    db_dir = os.path.dirname(path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    return aiosqlite.connect(path)


def _deserialize_row(row_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Helper to deserialize media_urls and summary_json into python objects."""
    d = dict(row_dict)
    if isinstance(d.get("media_urls"), str):
        try:
            d["media_urls"] = json.loads(d["media_urls"])
        except Exception:
            d["media_urls"] = []
    else:
        d["media_urls"] = d.get("media_urls") or []

    if isinstance(d.get("summary_json"), str) and d["summary_json"].strip():
        try:
            d["summary_data"] = json.loads(d["summary_json"])
            if isinstance(d["summary_data"], dict):
                raw_h = (
                    d["summary_data"].get("hashtags")
                    or d["summary_data"].get("hash_tags")
                    or d["summary_data"].get("tags")
                    or d["summary_data"].get("keywords")
                )
                if raw_h is not None:
                    d["summary_data"]["hashtags"] = normalize_hashtags(raw_h)
        except Exception:
            d["summary_data"] = None
    else:
        d["summary_data"] = None

    if isinstance(d.get("video_data"), str):
        try:
            d["video_data"] = json.loads(d["video_data"])
        except Exception:
            d["video_data"] = None
    return d


def normalize_to_utc_iso(dt_input: Any = None) -> str:
    """Normalizes any datetime or ISO string to strict UTC ISO-8601 string ending with 'Z'."""
    if dt_input is None:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(dt_input, datetime):
        if dt_input.tzinfo is None:
            dt = dt_input.replace(tzinfo=timezone.utc)
        else:
            dt = dt_input.astimezone(timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    if isinstance(dt_input, (int, float)):
        dt = datetime.fromtimestamp(dt_input, tz=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    if isinstance(dt_input, str):
        s = dt_input.strip()
        if not s:
            return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt.isoformat().replace("+00:00", "Z")
        except Exception:
            return s
    return str(dt_input)
