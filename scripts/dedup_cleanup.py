#!/usr/bin/env python3
"""
Скрипт ревизии и очистки дубликатов видео в базе данных.
Находит все посты в очереди (RAW, PROCESSED, SCHEDULED, PENDING_APPROVAL),
видео которых совпадает с уже опубликованными (POSTED) или удаленными (IGNORED) твитами,
помечает их статусом IGNORED и удаляет локальные кэшированные видеофайлы.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import aiosqlite
from src.config import settings
from src.db import init_db, update_tweet_status_and_score
from src.utils.video_hasher import is_fingerprint_duplicate
from src.utils.video_manager import delete_cached_video

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dedup_cleanup")


async def run_cleanup():
    await init_db()
    logger.info("Connecting to DB: %s", settings.db_path)

    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row

        # 1. Fetch reference posts (POSTED or IGNORED) that have video_hash or original_url
        async with db.execute(
            "SELECT tweet_id, status, author_handle, tweet_url, video_hash, video_data FROM tweets WHERE status IN ('POSTED', 'IGNORED')"
        ) as cursor:
            ref_rows = await cursor.fetchall()

        logger.info("Found %d reference posts (POSTED / IGNORED)", len(ref_rows))

        # 2. Fetch active queued posts
        async with db.execute(
            "SELECT tweet_id, status, author_handle, tweet_url, video_hash, video_data, viral_score, impact_score FROM tweets WHERE status IN ('RAW', 'PROCESSED', 'SCHEDULED', 'PENDING_APPROVAL')"
        ) as cursor:
            queue_rows = await cursor.fetchall()

        logger.info("Found %d queued posts to inspect", len(queue_rows))

        duplicates_found = 0

        # Also track seen within the queue to deduplicate queue itself
        seen_queue = []

        for q in queue_rows:
            q_id = q["tweet_id"]
            q_hash = q["video_hash"]
            q_vdata_raw = q["video_data"]
            q_orig_url = None
            if q_vdata_raw:
                try:
                    q_vdata = json.loads(q_vdata_raw) if isinstance(q_vdata_raw, str) else q_vdata_raw
                    if isinstance(q_vdata, dict):
                        q_orig_url = q_vdata.get("original_url")
                        if not q_hash:
                            q_hash = q_vdata.get("fingerprint")
                except Exception:
                    pass

            if not q_hash and not q_orig_url:
                continue

            dup_ref = None

            # Check against POSTED / IGNORED reference rows
            for ref in ref_rows:
                ref_id = ref["tweet_id"]
                if ref_id == q_id:
                    continue
                ref_hash = ref["video_hash"]
                ref_vdata_raw = ref["video_data"]
                ref_orig_url = None
                if ref_vdata_raw:
                    try:
                        ref_vdata = json.loads(ref_vdata_raw) if isinstance(ref_vdata_raw, str) else ref_vdata_raw
                        if isinstance(ref_vdata, dict):
                            ref_orig_url = ref_vdata.get("original_url")
                            if not ref_hash:
                                ref_hash = ref_vdata.get("fingerprint")
                    except Exception:
                        pass

                # Exact URL match
                if q_orig_url and ref_orig_url and q_orig_url == ref_orig_url:
                    dup_ref = ref
                    break

                # Perceptual hash match
                if q_hash and ref_hash and is_fingerprint_duplicate(q_hash, ref_hash):
                    dup_ref = ref
                    break

            # If not matched against reference, check within earlier inspected queue posts
            if not dup_ref:
                for prev_id, prev_hash, prev_url in seen_queue:
                    if q_orig_url and prev_url and q_orig_url == prev_url:
                        dup_ref = {"tweet_id": prev_id, "status": "QUEUED_EARLIER"}
                        break
                    if q_hash and prev_hash and is_fingerprint_duplicate(q_hash, prev_hash):
                        dup_ref = {"tweet_id": prev_id, "status": "QUEUED_EARLIER"}
                        break

            if dup_ref:
                duplicates_found += 1
                logger.warning(
                    "🚫 Duplicate found: queue tweet %s (%s) matches %s tweet %s! Marking IGNORED.",
                    q_id, q["author_handle"], dup_ref.get("status"), dup_ref["tweet_id"]
                )
                await update_tweet_status_and_score(
                    tweet_id=q_id,
                    score=float(q["viral_score"] or 0.0),
                    is_viral=False,
                    status="IGNORED",
                    impact_score=int(q["impact_score"] or 0)
                )
                delete_cached_video(q_id)
            else:
                seen_queue.append((q_id, q_hash, q_orig_url))

        logger.info("Dedup cleanup complete: %d duplicate posts removed from queue.", duplicates_found)


if __name__ == "__main__":
    asyncio.run(run_cleanup())
