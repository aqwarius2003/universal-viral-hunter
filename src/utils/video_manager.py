import os
import time
import aiohttp
from pathlib import Path
from urllib.parse import urlparse
from typing import List, Dict, Any, Optional

from src.config import settings
from src.utils.video_hasher import (
    compute_video_fingerprint,
    compute_image_dhash,
    compute_image_dhash_from_url,
    hamming_distance,
    is_fingerprint_duplicate
)

VIDEO_TEMP_DIR = Path("data/video-temp")

def get_max_quality() -> int:
    try:
        return int(getattr(settings, "video_max_quality", 720))
    except:
        return 720

def get_max_size_mb() -> int:
    try:
        return int(getattr(settings, "video_max_size_mb", 50))
    except:
        return 50

def get_ttl_days() -> int:
    try:
        return int(getattr(settings, "video_ttl_days", 2))
    except:
        return 2

def ensure_dir():
    VIDEO_TEMP_DIR.mkdir(parents=True, exist_ok=True)

def select_best_variant(variants: List[Dict[str, Any]], max_quality: int) -> Optional[str]:
    """
    Selects the best mp4 video variant that is <= max_quality (e.g. 720p).
    If no bitrate info is available but resolution is in URL, we can infer.
    """
    if not variants:
        return None

    mp4_variants = [v for v in variants if v.get("content_type") == "video/mp4" and "url" in v]
    if not mp4_variants:
        return None

    # Try to parse resolution from URL or use bitrate as a proxy
    valid_variants = []
    for v in mp4_variants:
        url = v["url"]
        bitrate = v.get("bitrate", 0)
        # Typically Twitter URLs have resolution: .../vid/720x1280/... or .../vid/1280x720/...
        # We can extract the max dimension
        import re
        match = re.search(r'/(\d+)x(\d+)/', url)
        if match:
            w, h = int(match.group(1)), int(match.group(2))
            res = min(w, h) # the smaller dimension is typically the "p" (e.g. 720p is 1280x720 or 720x1280)
        else:
            # Fallback: estimate from bitrate
            if bitrate > 2000000:
                res = 720
            elif bitrate > 800000:
                res = 480
            else:
                res = 360

        if res <= max_quality:
            valid_variants.append((res, bitrate, url))

    if not valid_variants:
        # If all are higher than max_quality, just pick the lowest one to be safe, or highest available
        valid_variants = [(0, v.get("bitrate", 0), v["url"]) for v in mp4_variants]

    # Sort by resolution desc, then bitrate desc
    valid_variants.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return valid_variants[0][2]

async def download_video(tweet_id: str, video_url: str) -> Optional[str]:
    """
    Downloads a video from a direct URL to the temp directory.
    Checks size limits.
    Returns local path or None on failure/too large.
    """
    ensure_dir()
    max_size_bytes = get_max_size_mb() * 1024 * 1024
    
    # Parse extension from url
    path = urlparse(video_url).path
    ext = os.path.splitext(path)[1]
    if not ext:
        ext = ".mp4"
        
    local_filename = f"{tweet_id}{ext}"
    local_path = VIDEO_TEMP_DIR / local_filename

    # If it already exists, return it
    if local_path.is_file():
        if local_path.stat().st_size <= max_size_bytes:
            return str(local_path)
        else:
            local_path.unlink() # Delete if it somehow exceeded size

    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(video_url, timeout=5) as head_resp:
                if head_resp.status == 200:
                    cl = head_resp.headers.get('Content-Length')
                    if cl and int(cl) > max_size_bytes:
                        print(f"⚠️ Video {tweet_id} is too large: {int(cl)/1024/1024:.2f} MB")
                        return None
                        
            async with session.get(video_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status == 200:
                    downloaded = 0
                    with open(local_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024*1024):
                            downloaded += len(chunk)
                            if downloaded > max_size_bytes:
                                f.close()
                                local_path.unlink()
                                print(f"⚠️ Video {tweet_id} exceeded max size during download.")
                                return None
                            f.write(chunk)
                    return str(local_path)
    except Exception as e:
        print(f"⚠️ Error downloading video {tweet_id}: {e}")
        if local_path.is_file():
            local_path.unlink()
    return None

def cleanup_old_videos():
    """Deletes videos older than TTL days."""
    if not VIDEO_TEMP_DIR.exists():
        return
        
    ttl_days = get_ttl_days()
    cutoff_time = time.time() - (ttl_days * 86400)
    
    count = 0
    for file_path in VIDEO_TEMP_DIR.glob("*.*"):
        if file_path.is_file():
            if file_path.stat().st_mtime < cutoff_time:
                try:
                    file_path.unlink()
                    count += 1
                except Exception as e:
                    print(f"⚠️ Error deleting {file_path}: {e}")
                    
    if count > 0:
        print(f"🧹 Очищено старых видеофайлов: {count}")


def delete_cached_video(tweet_id: str) -> bool:
    """
    Deletes cached video file(s) and thumbnails for a given tweet_id immediately from disk.
    """
    if not VIDEO_TEMP_DIR.exists() or not tweet_id:
        return False
        
    deleted = False
    for file_path in VIDEO_TEMP_DIR.glob(f"{tweet_id}*"):
        try:
            if file_path.is_file():
                file_path.unlink()
                deleted = True
        except Exception as e:
            print(f"⚠️ Error deleting cached video {file_path}: {e}")
            
    return deleted
