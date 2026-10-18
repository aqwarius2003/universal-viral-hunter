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


def is_fingerprint_duplicate(fp1: str, fp2: str, max_distance: int = 4) -> bool:
    """
    Determines if two video fingerprints represent the same video.
    For 3-frame fingerprints: matches if at least 2 corresponding frames have distance <= max_distance.
    For single-frame/thumbnail fingerprints: matches if minimum cross-distance <= max_distance.
    """
    if not fp1 or not fp2:
        return False

    parts1 = [p.strip() for p in fp1.split(":") if p.strip()]
    parts2 = [p.strip() for p in fp2.split(":") if p.strip()]

    if not parts1 or not parts2:
        return False

    # Multi-frame comparison (both have 3 frames)
    if len(parts1) >= 3 and len(parts2) >= 3:
        matches = 0
        for h1, h2 in zip(parts1[:3], parts2[:3]):
            if hamming_distance(h1, h2) <= max_distance:
                matches += 1
        if matches >= 2:
            return True

    # Cross-frame check: If any 2 frames match across or single-frame match
    cross_matches = 0
    for h1 in parts1:
        for h2 in parts2:
            if hamming_distance(h1, h2) <= max_distance:
                cross_matches += 1
                break

    # If single-frame hash was used on either side
    if min(len(parts1), len(parts2)) == 1:
        return cross_matches >= 1

    return cross_matches >= 2
