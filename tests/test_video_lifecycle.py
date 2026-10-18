import json
import pytest
from pathlib import Path
from src.config import settings
from src.db import (
    init_db,
    save_raw_tweets,
    filter_new_tweets,
    cancel_scheduled_post,
    mark_post_published,
    get_queued_posts,
    get_published_posts,
    get_tweet_by_id,
)
from src.utils.video_manager import delete_cached_video, VIDEO_TEMP_DIR


@pytest.mark.asyncio
async def test_video_deletion_and_deduplication(tmp_path, monkeypatch):
    """
    Verifies that:
    1. Canceling/deleting a post sets status to IGNORED.
    2. Cached video file on disk is immediately deleted.
    3. The tweet record and URL remain in SQLite so Scout deduplication filters it out in future runs.
    """
    db_file = tmp_path / "test_lifecycle.sqlite"
    video_dir = tmp_path / "video-temp"
    video_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr("src.utils.video_manager.VIDEO_TEMP_DIR", video_dir)
    await init_db()

    tweet_id = "test_del_123"
    fake_video = video_dir / f"{tweet_id}.mp4"
    fake_video.write_bytes(b"dummy video content")
    assert fake_video.exists()

    # 1. Save raw tweet
    await save_raw_tweets([{
        "tweet_id": tweet_id,
        "author_handle": "@driver_fails",
        "author_name": "Driver Fails",
        "text_raw": "Crazy car crash on highway",
        "tweet_url": f"https://x.com/driver_fails/status/{tweet_id}",
        "created_at": "2026-10-18T10:00:00+00:00",
        "likes_count": 500,
        "reposts_count": 100,
        "video_data": {"local_path": str(fake_video), "original_url": "https://video.twimg.com/test.mp4"},
    }])

    # 2. Verify Scout deduplication detects it
    dup_check = await filter_new_tweets([{"tweet_id": tweet_id}])
    assert len(dup_check) == 0, "Tweet must be filtered out by Scout deduplication"

    # 3. Simulate user clicking 'Удалить' (cancel_scheduled_post + delete_cached_video)
    await cancel_scheduled_post(tweet_id)
    deleted = delete_cached_video(tweet_id)
    assert deleted is True, "delete_cached_video should return True"
    assert not fake_video.exists(), "Cached video file on disk must be removed"

    # 4. Check DB state
    tweet = await get_tweet_by_id(tweet_id)
    assert tweet is not None, "Tweet record must remain in database"
    assert tweet["status"] == "IGNORED", "Status must be IGNORED"
    assert tweet["tweet_url"] == f"https://x.com/driver_fails/status/{tweet_id}"

    # 5. Future Scout runs must STILL ignore this tweet
    future_scout = await filter_new_tweets([{"tweet_id": tweet_id}])
    assert len(future_scout) == 0, "Ignored tweet must still be filtered out by Scout"


@pytest.mark.asyncio
async def test_published_video_purge_and_deduplication(tmp_path, monkeypatch):
    """
    Verifies that:
    1. Publishing a post sets status to POSTED.
    2. Cached video file on disk is immediately deleted to save VPS storage.
    3. The tweet record and URL remain in SQLite with status POSTED so Scout never re-processes it.
    """
    db_file = tmp_path / "test_pub_lifecycle.sqlite"
    video_dir = tmp_path / "video-temp"
    video_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr("src.utils.video_manager.VIDEO_TEMP_DIR", video_dir)
    await init_db()

    tweet_id = "test_pub_456"
    fake_video = video_dir / f"{tweet_id}.mp4"
    fake_video.write_bytes(b"dummy published video")
    assert fake_video.exists()

    await save_raw_tweets([{
        "tweet_id": tweet_id,
        "author_handle": "@speed_cams",
        "author_name": "Speed Cams",
        "text_raw": "Near miss video",
        "tweet_url": f"https://x.com/speed_cams/status/{tweet_id}",
        "created_at": "2026-10-18T10:00:00+00:00",
        "likes_count": 800,
        "video_data": {"local_path": str(fake_video), "original_url": "https://video.twimg.com/test2.mp4"},
    }])

    # Publish post
    await mark_post_published(tweet_id)
    delete_cached_video(tweet_id)
    assert not fake_video.exists(), "Published video must be purged from disk"

    # Verify status in database
    tweet = await get_tweet_by_id(tweet_id)
    assert tweet["status"] == "POSTED"
    assert tweet["posted_at"] is not None

    # Verify Scout deduplication
    dup_check = await filter_new_tweets([{"tweet_id": tweet_id}])
    assert len(dup_check) == 0, "Posted tweet must never be re-processed by Scout"

    # Verify published archive has it
    published = await get_published_posts()
    assert any(p["tweet_id"] == tweet_id for p in published)
