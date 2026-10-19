"""
Perceptual Video Fingerprinting & Deduplication Module.

Computes difference hashes (dHash) on sample keyframes across video duration
to detect re-uploaded, re-encoded, or resized duplicates regardless of tweet ID,
author, bitrates, or compression artifacts.
"""

import io
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple, Union
import aiohttp
from PIL import Image

logger = logging.getLogger(__name__)


def compute_image_dhash(img: Image.Image, hash_size: int = 8) -> str:
    """
    Computes difference hash (dHash) for a PIL Image.
    Resizes image to (hash_size + 1, hash_size), converts to grayscale,
    and compares adjacent horizontal pixels.
    Returns a hexadecimal string representing (hash_size * hash_size) bits (default: 64 bits = 16 hex chars).
    """
    # Convert to grayscale and resize with high-quality resampling
    gray = img.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
    if hasattr(gray, "get_flattened_data"):
        pixels = list(gray.get_flattened_data())
    else:
        pixels = list(gray.getdata())

    diff_bits = 0
    # Process row by row: hash_size rows, each with (hash_size + 1) pixels
    row_len = hash_size + 1
    for row in range(hash_size):
        row_offset = row * row_len
        for col in range(hash_size):
            left = pixels[row_offset + col]
            right = pixels[row_offset + col + 1]
            diff_bits = (diff_bits << 1) | (1 if left > right else 0)

    # Format as fixed-length hex string (64 bits -> 16 chars)
    hex_len = (hash_size * hash_size) // 4
    return f"{diff_bits:0{hex_len}x}"


def compute_image_dhash_from_bytes(data: bytes, hash_size: int = 8) -> Optional[str]:
    """Computes dHash from raw image bytes."""
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            return compute_image_dhash(img, hash_size=hash_size)
    except Exception as e:
        logger.debug("Failed to compute dHash from image bytes: %s", e)
        return None


def compute_image_dhash_from_file(file_path: Union[str, Path], hash_size: int = 8) -> Optional[str]:
    """Computes dHash from an image file path."""
    p = Path(file_path)
    if not p.is_file():
        return None
    try:
        with Image.open(p) as img:
            return compute_image_dhash(img, hash_size=hash_size)
    except Exception as e:
        logger.debug("Failed to compute dHash from file %s: %s", p, e)
        return None


async def compute_image_dhash_from_url(url: str, session: Optional[aiohttp.ClientSession] = None) -> Optional[str]:
    """Downloads an image from URL and computes its dHash."""
    if not url:
        return None
    try:
        if session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    return compute_image_dhash_from_bytes(data)
        else:
            async with aiohttp.ClientSession() as new_session:
                async with new_session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        return compute_image_dhash_from_bytes(data)
    except Exception as e:
        logger.debug("Failed to compute dHash from url %s: %s", url, e)
    return None


def get_video_duration(video_path: Union[str, Path]) -> Optional[float]:
    """
    Returns the duration of a video in seconds using ffprobe.
    Returns None if ffprobe is unavailable or fails.
    """
    ffprobe_cmd = shutil.which("ffprobe")
    if not ffprobe_cmd:
        return None
    p = Path(video_path)
    if not p.is_file():
        return None

    try:
        cmd = [
            ffprobe_cmd,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(p)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            val = float(result.stdout.strip())
            return val if val > 0 else None
    except Exception as e:
        logger.debug("ffprobe error on %s: %s", p, e)
    return None


def extract_frame_at_timestamp(video_path: Union[str, Path], timestamp_sec: float) -> Optional[Image.Image]:
    """
    Extracts a single frame from video at given timestamp using ffmpeg as PIL Image.
    Returns None if ffmpeg is unavailable or fails.
    """
    ffmpeg_cmd = shutil.which("ffmpeg")
    if not ffmpeg_cmd:
        return None
    p = Path(video_path)
    if not p.is_file():
        return None

    try:
        ts_str = f"{max(0.0, float(timestamp_sec)):.3f}"
        cmd = [
            ffmpeg_cmd,
            "-y",
            "-ss", ts_str,
            "-i", str(p),
            "-frames:v", "1",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "-"
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=15)
        if proc.returncode == 0 and proc.stdout:
            img = Image.open(io.BytesIO(proc.stdout))
            img.load()  # Read image into memory before closing buffer
            return img
    except Exception as e:
        logger.debug("ffmpeg frame extraction failed for %s at %s: %s", p, timestamp_sec, e)
    return None


def compute_video_fingerprint(
    video_path: Optional[Union[str, Path]] = None,
    duration_sec: Optional[float] = None,
    thumbnail_bytes: Optional[bytes] = None,
    thumbnail_path: Optional[Union[str, Path]] = None
) -> Optional[str]:
    """
    Generates a multi-point perceptual fingerprint for a video.
    Samples 3 keyframes at 20%, 50%, and 80% duration if video file is available.
    Format: 'frame1_hash:frame2_hash:frame3_hash' or 'thumb_hash'.
    """
    hashes: List[str] = []

    if video_path and Path(video_path).is_file():
        p = Path(video_path)
        # Determine duration
        total_duration = duration_sec or get_video_duration(p) or 5.0

        # Sample at 20%, 50%, and 80%
        timestamps = [
            max(0.2, total_duration * 0.20),
            max(0.5, total_duration * 0.50),
            max(0.8, total_duration * 0.80),
        ]

        for ts in timestamps:
            frame_img = extract_frame_at_timestamp(p, ts)
            if frame_img:
                h = compute_image_dhash(frame_img)
                hashes.append(h)

    # Fallback to thumbnail if video extraction did not yield 3 frames
    if len(hashes) < 2:
        thumb_hash = None
        if thumbnail_bytes:
            thumb_hash = compute_image_dhash_from_bytes(thumbnail_bytes)
        elif thumbnail_path and Path(thumbnail_path).is_file():
            thumb_hash = compute_image_dhash_from_file(thumbnail_path)

        if thumb_hash:
            if not hashes:
                hashes.append(thumb_hash)
            elif thumb_hash not in hashes:
                hashes.append(thumb_hash)

    if not hashes:
        return None

    return ":".join(hashes)


def hamming_distance(hash1: str, hash2: str) -> int:
    """
    Calculates the Hamming distance (number of differing bits) between two hex hashes.
    """
    if not hash1 or not hash2 or len(hash1) != len(hash2):
        return 64  # Maximum distance for mismatched sizes
    try:
        val1 = int(hash1, 16)
        val2 = int(hash2, 16)
        xor_diff = val1 ^ val2
        return bin(xor_diff).count("1")
    except Exception:
        return 64


def is_degenerate_frame_hash(h: str) -> bool:
    """
    Checks if a frame hash is degenerate (e.g. solid black, pure white, or flat uniform color).
    Such frames (like fade-in from black) have trivial bit patterns and must not be used on their own
    as proof of duplicate videos.
    """
    if not h or len(h) < 16:
        return True
    try:
        val = int(h, 16)
        set_bits = bin(val).count("1")
        if set_bits <= 3 or set_bits >= 61:
            return True
        if h.lower() in ("0000000000000000", "ffffffffffffffff", "aaaaaaaaaaaaaaaa", "5555555555555555"):
            return True
        return False
    except Exception:
        return True


def is_fingerprint_duplicate(fp1: str, fp2: str, max_distance: int = 5) -> bool:
    """
    Determines if two video fingerprints represent the same video.
    Features:
    1. Exact match fast-path.
    2. First-frame comparison: If Frame 1 (or thumbnail) matches with distance <= max_distance (default 5),
       the videos share the exact opening scene.
    3. Cross-frame comparison: If ANY non-degenerate frame of Video 1 matches ANY frame of Video 2
       with distance <= max_distance, it detects trimmed re-uploads (intro/outro cuts).
    4. Multi-frame drift tolerance: If at least 2 corresponding frames have distance <= max_distance + 3 (<= 8),
       or the average distance across aligned frames <= 7.0, it matches even with re-compression artifacts.
    5. Single-frame / thumbnail comparison: matches if minimum distance to any frame is <= max_distance + 1 (<= 6).
    """
    if not fp1 or not fp2:
        return False

    if fp1 == fp2:
        return True

    parts1 = [p.strip() for p in fp1.split(":") if p.strip()]
    parts2 = [p.strip() for p in fp2.split(":") if p.strip()]

    if not parts1 or not parts2:
        return False

    # Filter out degenerate frame hashes for safety against solid black/white frames
    valid1 = [(idx, h) for idx, h in enumerate(parts1) if not is_degenerate_frame_hash(h)]
    valid2 = [(idx, h) for idx, h in enumerate(parts2) if not is_degenerate_frame_hash(h)]

    if not valid1 or not valid2:
        return False

    # 1. First non-degenerate frame matching:
    # If the first valid frame of both videos matches (distance <= max_distance), it's the same video
    if valid1[0][0] == 0 and valid2[0][0] == 0:
        first_dist = hamming_distance(valid1[0][1], valid2[0][1])
        if first_dist <= max_distance:
            return True

    # 2. Cross-frame minimum distance check:
    # Catches clips trimmed at start or end by matching any keyframe
    min_cross_dist = 64
    cross_matches_count = 0
    for _, h1 in valid1:
        for _, h2 in valid2:
            d = hamming_distance(h1, h2)
            if d < min_cross_dist:
                min_cross_dist = d
            if d <= max_distance:
                cross_matches_count += 1
                break

    # If any frame is practically identical (<= max_distance), it's a duplicate
    if min_cross_dist <= max_distance:
        return True

    # 3. If either side is a single frame / thumbnail, allow tolerance up to max_distance + 1 (e.g. 6)
    if min(len(valid1), len(valid2)) == 1:
        return min_cross_dist <= (max_distance + 1)

    # 4. Multi-frame comparison (corresponding frames with drift tolerance)
    aligned_distances = []
    for h1, h2 in zip(parts1, parts2):
        if not is_degenerate_frame_hash(h1) and not is_degenerate_frame_hash(h2):
            aligned_distances.append(hamming_distance(h1, h2))

    if aligned_distances:
        # If at least 2 corresponding frames have distance <= max_distance + 3 (e.g. <= 8)
        close_frames = sum(1 for d in aligned_distances if d <= (max_distance + 3))
        if close_frames >= 2:
            return True

        # Or if average distance across aligned frames <= 7.0
        avg_dist = sum(aligned_distances) / len(aligned_distances)
        if avg_dist <= 7.0 and min_cross_dist <= 7:
            return True

    return cross_matches_count >= 2

