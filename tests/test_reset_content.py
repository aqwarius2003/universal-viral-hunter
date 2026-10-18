import pytest
import os
import aiosqlite
from pathlib import Path
from aiohttp.test_utils import TestClient, TestServer
from src.config import settings
from src.db import (
    init_db,
    save_raw_tweets,
    clear_all_content_data,
    create_topic,
    get_topics,
    create_user,
    get_user_by_username,
    save_digest_schedule,
    get_digest_schedules,
)
from src.utils.video_manager import VIDEO_TEMP_DIR, clear_all_cached_videos
from src.web.server import create_app


@pytest.mark.asyncio
async def test_clear_all_content_data_preserves_settings_and_topics(tmp_path):
    test_db = str(tmp_path / "test_reset.sqlite")
    settings.db_path = test_db

    await init_db()

    # 1. Populate content data
    sample_tweet = {
        "tweet_id": "reset_test_123",
        "author_handle": "elonmusk",
        "author_name": "Elon Musk",
        "text_raw": "Testing reset",
        "tweet_url": "https://x.com/elonmusk/status/reset_test_123",
        "created_at": "2026-10-18T12:00:00Z",
        "likes_count": 500,
        "reposts_count": 100,
        "quotes_count": 10,
        "bookmarks_count": 5,
        "impressions_count": 10000,
        "viral_score": 150.0,
        "is_viral": True,
        "media_urls": ["https://pbs.twimg.com/media/test.jpg"],
        "video_hash": "hash_12345",
        "status": "PROCESSED",
    }
    await save_raw_tweets([sample_tweet])

    # 2. Populate preserved configurations
    topic_id = await create_topic({
        "name": "TEST_CRASHES",
        "search_queries": "car crash min_faves:50",
        "media_type": "video",
        "telegram_channel_id": "@testchannel",
    })
    assert topic_id is not None

    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO system_settings (key, value, updated_at) VALUES ('gemini_api_key', 'AIzaSyTestKey123', CURRENT_TIMESTAMP)"
        )
        await db.commit()

    await create_user("testadmin", "password123", role="admin")
    sched_id = await save_digest_schedule({
        "time": "09:00",
        "title": "Morning Digest",
        "max_items_per_post": 5,
        "max_posts": 1,
        "sort_order": "BY_VIRAL_SCORE",
        "interval_minutes": 5,
        "enabled": True,
    })
    assert sched_id is not None

    # Verify rows exist before reset
    async with aiosqlite.connect(settings.db_path) as db:
        cur = await db.execute("SELECT COUNT(*) FROM tweets")
        assert (await cur.fetchone())[0] == 1
        cur = await db.execute("SELECT COUNT(*) FROM topics")
        assert (await cur.fetchone())[0] == 1
        cur = await db.execute("SELECT COUNT(*) FROM system_settings")
        assert (await cur.fetchone())[0] >= 1
        cur = await db.execute("SELECT COUNT(*) FROM users")
        assert (await cur.fetchone())[0] == 1
        cur = await db.execute("SELECT COUNT(*) FROM digest_schedules")
        assert (await cur.fetchone())[0] == 1

    # 3. Perform clean slate
    counts = await clear_all_content_data()
    assert counts.get("tweets") == 1

    # 4. Assert content tables are empty
    async with aiosqlite.connect(settings.db_path) as db:
        cur = await db.execute("SELECT COUNT(*) FROM tweets")
        assert (await cur.fetchone())[0] == 0
        cur = await db.execute("SELECT COUNT(*) FROM digests")
        assert (await cur.fetchone())[0] == 0
        cur = await db.execute("SELECT COUNT(*) FROM pipeline_runs")
        assert (await cur.fetchone())[0] == 0

    # 5. Assert configuration tables are strictly preserved
    topics = await get_topics()
    assert len(topics) == 1
    assert topics[0]["name"] == "TEST_CRASHES"

    async with aiosqlite.connect(settings.db_path) as db:
        cur = await db.execute("SELECT value FROM system_settings WHERE key = 'gemini_api_key'")
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == "AIzaSyTestKey123"

    user = await get_user_by_username("testadmin")
    assert user is not None
    assert user["role"] == "admin"

    schedules = await get_digest_schedules()
    assert len(schedules) == 1
    assert schedules[0]["title"] == "Morning Digest"


def test_clear_all_cached_videos(tmp_path, monkeypatch):
    test_video_dir = tmp_path / "video-temp"
    test_video_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("src.utils.video_manager.VIDEO_TEMP_DIR", test_video_dir)

    (test_video_dir / "vid1.mp4").write_text("dummy video")
    (test_video_dir / "vid2.mp4").write_text("dummy video")
    (test_video_dir / "vid1_thumb.jpg").write_text("dummy thumb")

    assert len(list(test_video_dir.iterdir())) == 3

    deleted = clear_all_cached_videos()
    assert deleted == 3
    assert len(list(test_video_dir.iterdir())) == 0


@pytest.mark.asyncio
async def test_reset_content_endpoint(tmp_path, monkeypatch):
    test_db = str(tmp_path / "test_api_reset.sqlite")
    settings.db_path = test_db
    settings.auth_enabled = False  # Bypasses auth in tests as admin

    await init_db()
    await save_raw_tweets([{
        "tweet_id": "api_test_999",
        "author_handle": "test",
        "text_raw": "hello",
        "tweet_url": "https://x.com/test/999",
        "created_at": "2026-10-18T12:00:00Z",
        "viral_score": 10.0,
    }])

    test_video_dir = tmp_path / "video-temp-api"
    test_video_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("src.utils.video_manager.VIDEO_TEMP_DIR", test_video_dir)
    (test_video_dir / "api_test_999.mp4").write_text("data")

    app = create_app()
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        resp = await client.post("/api/system/reset-content")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "success"
        assert data["cleared_tables"]["tweets"] == 1
        assert data["cached_videos_deleted"] == 1

        async with aiosqlite.connect(settings.db_path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM tweets")
            assert (await cur.fetchone())[0] == 0
    finally:
        await client.close()
