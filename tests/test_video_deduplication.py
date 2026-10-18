import pytest
import os
import aiosqlite
from PIL import Image, ImageDraw

from src.config import settings
from src.db import (
    init_db,
    save_raw_tweets,
    filter_new_tweets,
    find_duplicate_video_in_db,
    update_tweet_status_and_score
)
from src.utils.video_hasher import (
    compute_image_dhash,
    hamming_distance,
    is_fingerprint_duplicate,
    compute_video_fingerprint
)


@pytest.mark.asyncio
async def test_dhash_and_hamming_distance():
    # 1. Identical patterns
    img1 = Image.new("RGB", (100, 100), "white")
    draw1 = ImageDraw.Draw(img1)
    draw1.rectangle([10, 10, 50, 50], fill="black")
    draw1.ellipse([60, 60, 90, 90], fill="gray")

    # Resized copy of the same pattern
    img2 = img1.resize((180, 180))

    # Completely different pattern
    img3 = Image.new("RGB", (100, 100), "white")
    draw3 = ImageDraw.Draw(img3)
    draw3.line([0, 100, 100, 0], fill="black", width=8)

    h1 = compute_image_dhash(img1)
    h2 = compute_image_dhash(img2)
    h3 = compute_image_dhash(img3)

    assert len(h1) == 16
    assert len(h2) == 16
    assert len(h3) == 16

    # Same pattern across resolutions has distance 0 or <= 2
    dist_same = hamming_distance(h1, h2)
    assert dist_same <= 2

    # Different patterns have high distance (typically >= 15)
    dist_diff = hamming_distance(h1, h3)
    assert dist_diff >= 15

    # Fingerprint matching logic
    assert is_fingerprint_duplicate(h1, h2, max_distance=4) is True
    assert is_fingerprint_duplicate(h1, h3, max_distance=4) is False


@pytest.mark.asyncio
async def test_multi_frame_fingerprint_matching():
    # Construct 3-frame fingerprints: hA, hB, hC
    hA = "1111222233334444"
    hB = "5555666677778888"
    hC = "aaaabbbbccccdddd"

    # Exact same 3 frames
    fp1 = f"{hA}:{hB}:{hC}"
    fp2 = f"{hA}:{hB}:{hC}"
    assert is_fingerprint_duplicate(fp1, fp2) is True

    # Slightly modified 1 frame (e.g. watermark), 2 identical frames
    # Modify 1 bit in hB:
    hB_slight = f"{int(hB, 16) ^ 1:016x}"
    fp3 = f"{hA}:{hB_slight}:{hC}"
    assert is_fingerprint_duplicate(fp1, fp3) is True

    # 3 completely different frames
    hX = "0000000000000000"
    hY = "ffffffffffffffff"
    hZ = "123456789abcdef0"
    fp_diff = f"{hX}:{hY}:{hZ}"
    assert is_fingerprint_duplicate(fp1, fp_diff) is False


@pytest.mark.asyncio
async def test_db_video_hash_migration_and_deduplication(tmp_path):
    # Set isolated test database
    test_db = tmp_path / "test_dedup.sqlite"
    settings.db_path = str(test_db)
    await init_db()

    # Verify column video_hash exists and index exists
    async with aiosqlite.connect(settings.db_path) as db:
        async with db.execute("PRAGMA table_info(tweets)") as cursor:
            cols = [r[1] for r in await cursor.fetchall()]
            assert "video_hash" in cols

        async with db.execute("PRAGMA index_list(tweets)") as cursor:
            indexes = [r[1] for r in await cursor.fetchall()]
            assert "idx_tweets_video_hash" in indexes

    # Step 1: Save Tweet A with video fingerprint fp_A
    fp_A = "a0a0b1b1c2c2d3d3:1111222233334444:5555666677778888"
    tweet_a = {
        "tweet_id": "tweet_original_001",
        "author_handle": "@original_creator",
        "text_raw": "Original crash video",
        "tweet_url": "https://x.com/original_creator/status/tweet_original_001",
        "created_at": "2026-10-18T10:00:00Z",
        "likes_count": 500,
        "video_data": {"thumbnail": "https://pbs.twimg.com/thumb_1.jpg"},
        "video_hash": fp_A
    }

    filtered_first = await filter_new_tweets([tweet_a])
    assert len(filtered_first) == 1
    await save_raw_tweets(filtered_first)

    # Verify Tweet A is stored with its video_hash
    async with aiosqlite.connect(settings.db_path) as db:
        async with db.execute("SELECT video_hash FROM tweets WHERE tweet_id = ?", ("tweet_original_001",)) as cursor:
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == fp_A

    # Step 2: Now simulate Tweet B from a RE-UPLOADER with a DIFFERENT tweet_id and author
    # It has the exact same video (or with 1 slightly re-encoded frame)
    h1 = "a0a0b1b1c2c2d3d3"
    # Flip 1 bit in frame 2 due to Twitter transcoding
    h2_reencoded = f"{int('1111222233334444', 16) ^ 2:016x}"
    h3 = "5555666677778888"
    fp_B = f"{h1}:{h2_reencoded}:{h3}"

    tweet_b = {
        "tweet_id": "tweet_repost_002",
        "author_handle": "@content_stealer",
        "text_raw": "Insane video check this out!",
        "tweet_url": "https://x.com/content_stealer/status/tweet_repost_002",
        "created_at": "2026-10-18T12:00:00Z",
        "likes_count": 50,
        "video_data": {"thumbnail": "https://pbs.twimg.com/thumb_2.jpg"},
        "video_hash": fp_B
    }

    # Tweet C is a genuinely different video
    fp_C = "ffff0000ffff0000:1234567812345678:abcdef01abcdef01"
    tweet_c = {
        "tweet_id": "tweet_unique_003",
        "author_handle": "@unique_user",
        "text_raw": "Totally different clip",
        "tweet_url": "https://x.com/unique_user/status/tweet_unique_003",
        "created_at": "2026-10-18T13:00:00Z",
        "likes_count": 80,
        "video_data": {"thumbnail": "https://pbs.twimg.com/thumb_3.jpg"},
        "video_hash": fp_C
    }

    # filter_new_tweets MUST reject Tweet B (duplicate video) and accept Tweet C
    new_incoming = await filter_new_tweets([tweet_b, tweet_c])
    assert len(new_incoming) == 1
    assert new_incoming[0]["tweet_id"] == "tweet_unique_003"


@pytest.mark.asyncio
async def test_batch_duplicate_videos(tmp_path):
    # Test when two tweets in the SAME batch have the same video
    test_db = tmp_path / "test_batch_dedup.sqlite"
    settings.db_path = str(test_db)
    await init_db()

    fp_shared = "9999888877776666:5555444433332222:11110000ffffeedd"
    t1 = {
        "tweet_id": "batch_t1",
        "author_handle": "@user1",
        "text_raw": "First upload in batch",
        "tweet_url": "https://x.com/user1/status/batch_t1",
        "created_at": "2026-10-18T10:00:00Z",
        "video_hash": fp_shared
    }
    t2 = {
        "tweet_id": "batch_t2",
        "author_handle": "@user2",
        "text_raw": "Duplicate in same batch",
        "tweet_url": "https://x.com/user2/status/batch_t2",
        "created_at": "2026-10-18T10:05:00Z",
        "video_hash": fp_shared
    }

    res = await filter_new_tweets([t1, t2])
    # Only the first one in the batch should pass
    assert len(res) == 1
    assert res[0]["tweet_id"] == "batch_t1"
