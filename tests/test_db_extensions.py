import os
import pytest
from unittest.mock import AsyncMock
from datetime import datetime, timezone, timedelta
from src.config import settings
from src.db import (
    init_db,
    save_raw_tweets,
    filter_new_tweets,
    update_tweet_status_and_score,
    update_tweet_metrics,
    get_tweets_last_24h,
    mark_digest_posted,
    is_digest_posted,
)

@pytest.fixture
def temp_db_path(tmp_path, mocker):
    db_file = str(tmp_path / "test_tweets.sqlite")
    mocker.patch.object(settings, "db_path", db_file)
    yield db_file
    if os.path.exists(db_file):
        try:
            os.remove(db_file)
        except Exception:
            pass


@pytest.mark.asyncio
async def test_db_crud_and_digest(temp_db_path):
    await init_db()
    now = datetime.now(timezone.utc)

    tweets = [
        {
            "tweet_id": "test_1",
            "author_handle": "@tester1",
            "author_name": "Tester One",
            "text_raw": "Testing AI model tweet",
            "tweet_url": "https://x.com/tester1/status/test_1",
            "created_at": (now - timedelta(hours=2)).isoformat(),
            "likes_count": 100,
            "reposts_count": 20,
            "quotes_count": 5,
            "bookmarks_count": 15,
            "impressions_count": 5000,
            "media_urls": ["https://pbs.twimg.com/media/test1.jpg"],
        },
        {
            "tweet_id": "test_2",
            "author_handle": "@tester2",
            "author_name": "Tester Two",
            "text_raw": "Old tweet from 2 days ago",
            "tweet_url": "https://x.com/tester2/status/test_2",
            "created_at": (now - timedelta(days=2)).isoformat(),
            "likes_count": 10,
            "reposts_count": 2,
            "quotes_count": 0,
            "bookmarks_count": 1,
            "impressions_count": 500,
            "media_urls": [],
        }
    ]

    # 1. Filter and save
    new_tweets = await filter_new_tweets(tweets)
    assert len(new_tweets) == 2
    await save_raw_tweets(new_tweets)

    # Re-filtering should return 0 new tweets
    re_filter = await filter_new_tweets(tweets)
    assert len(re_filter) == 0

    # 2. Update status and score
    await update_tweet_status_and_score(
        tweet_id="test_1",
        score=85.0,
        is_viral=True,
        status="PROCESSED",
        impact_score=9,
        summary_json='{"title_ru": "Тест"}'
    )

    # 3. Update refreshed metrics
    await update_tweet_metrics(
        tweet_id="test_1",
        likes=250,
        reposts=50,
        quotes=10,
        bookmarks=40,
        views=12000,
        viral_score=92.0,
        is_viral=True
    )

    # 4. Query last 24h
    last_24h = await get_tweets_last_24h()
    assert len(last_24h) >= 1
    found_1 = next((t for t in last_24h if t["tweet_id"] == "test_1"), None)
    assert found_1 is not None
    assert found_1["likes_count"] == 250
    assert found_1["viral_score"] == 92.0
    assert found_1["media_urls"] == ["https://pbs.twimg.com/media/test1.jpg"]

    # 5. Digest tracking
    today_str = datetime.now().strftime("%Y-%m-%d")
    assert await is_digest_posted(today_str) is False

    await mark_digest_posted(today_str, tweet_count=1)
    assert await is_digest_posted(today_str) is True


@pytest.mark.asyncio
async def test_queue_virality_ranking_and_quiet_hours(temp_db_path, monkeypatch):
    from src.db import get_queued_posts, auto_schedule_queue, get_recent_active_tweets
    from src.config import adjust_for_quiet_hours, is_in_quiet_hours

    await init_db()
    now = datetime.now(timezone.utc)

    # Insert 3 tweets with different viral scores and impact scores
    tweets = [
        {
            "tweet_id": "post_low",
            "author_handle": "@low",
            "author_name": "Low Poster",
            "text_raw": "Low viral post",
            "tweet_url": "https://x.com/low/status/post_low",
            "created_at": now.isoformat(),
            "likes_count": 10,
            "reposts_count": 2,
            "impressions_count": 1000,
        },
        {
            "tweet_id": "post_high",
            "author_handle": "@high",
            "author_name": "High Poster",
            "text_raw": "High viral post",
            "tweet_url": "https://x.com/high/status/post_high",
            "created_at": now.isoformat(),
            "likes_count": 1000,
            "reposts_count": 500,
            "impressions_count": 50000,
        },
        {
            "tweet_id": "post_mid",
            "author_handle": "@mid",
            "author_name": "Mid Poster",
            "text_raw": "Mid viral post",
            "tweet_url": "https://x.com/mid/status/post_mid",
            "created_at": now.isoformat(),
            "likes_count": 200,
            "reposts_count": 50,
            "impressions_count": 10000,
        },
    ]
    await save_raw_tweets(tweets)

    await update_tweet_status_and_score("post_low", score=50.0, is_viral=False, status="PROCESSED", impact_score=5)
    await update_tweet_status_and_score("post_high", score=120.0, is_viral=True, status="PROCESSED", impact_score=10)
    await update_tweet_status_and_score("post_mid", score=80.0, is_viral=True, status="PROCESSED", impact_score=7)

    # 1. get_queued_posts should order by combined score when queue_order == 0
    queued = await get_queued_posts()
    assert len(queued) == 3
    assert queued[0]["tweet_id"] == "post_high"
    assert queued[1]["tweet_id"] == "post_mid"
    assert queued[2]["tweet_id"] == "post_low"

    # Test impact_score = 0 fallback to 7 in SQL ranking:
    # A post with viral_score=150 and impact_score=0 should get combined score 150 * 7 = 1050,
    # beating post_mid (80 * 7 = 560) but under post_high (120 * 10 = 1200)
    await update_tweet_status_and_score("post_low", score=150.0, is_viral=True, status="PROCESSED", impact_score=0)
    queued_fallback = await get_queued_posts()
    assert queued_fallback[0]["tweet_id"] == "post_high"  # 120 * 10 = 1200
    assert queued_fallback[1]["tweet_id"] == "post_low"   # 150 * 7 = 1050 (fallback multiplier 7)
    assert queued_fallback[2]["tweet_id"] == "post_mid"   # 80 * 7 = 560

    # Reset post_low back
    await update_tweet_status_and_score("post_low", score=50.0, is_viral=False, status="PROCESSED", impact_score=5)

    # 2. get_recent_active_tweets
    recent = await get_recent_active_tweets(hours=24)
    assert len(recent) == 3

    # 3. Quiet hours logic test
    night_dt = datetime(2026, 9, 14, 23, 30, tzinfo=timezone.utc)
    assert is_in_quiet_hours(night_dt, enabled=True, start_str="22:00", end_str="07:00") is True
    day_dt = datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)
    assert is_in_quiet_hours(day_dt, enabled=True, start_str="22:00", end_str="07:00") is False

    # Daytime window (e.g. 01:00 to 06:00)
    early_dt = datetime(2026, 9, 14, 3, 30)
    assert is_in_quiet_hours(early_dt, enabled=True, start_str="01:00", end_str="06:00") is True
    late_dt = datetime(2026, 9, 14, 8, 0)
    assert is_in_quiet_hours(late_dt, enabled=True, start_str="01:00", end_str="06:00") is False

    shifted = adjust_for_quiet_hours(night_dt, enabled=True, start_str="22:00", end_str="07:00")
    assert shifted.hour == 7
    assert shifted.minute == 0
    assert shifted.day == 15

    # 4. auto_schedule_queue assigns 1, 2, 3 ordered by virality
    scheduled = await auto_schedule_queue(interval_minutes=60, start_offset_minutes=30)
    assert len(scheduled) == 3
    assert scheduled[0]["tweet_id"] == "post_high"
    assert scheduled[0]["queue_order"] == 1
    assert scheduled[1]["tweet_id"] == "post_mid"
    assert scheduled[1]["queue_order"] == 2
    assert scheduled[2]["tweet_id"] == "post_low"
    assert scheduled[2]["queue_order"] == 3


@pytest.mark.asyncio
async def test_auto_schedule_skips_night_quiet_hours(temp_db_path, monkeypatch):
    """Verifies that auto_schedule_queue never assigns publication times during quiet hours (sleep mode)."""
    import aiosqlite
    from src.config import settings
    from src.db import init_db, auto_schedule_queue, get_queued_posts

    await init_db()
    monkeypatch.setattr(settings, "quiet_hours_enabled", True)
    monkeypatch.setattr(settings, "quiet_hours_start", "23:00")
    monkeypatch.setattr(settings, "quiet_hours_end", "08:00")

    now_iso = datetime.now(timezone.utc).isoformat()
    # Insert 15 posts into temp_db
    async with aiosqlite.connect(temp_db_path) as db:
        for i in range(1, 16):
            await db.execute(
                """
                INSERT INTO tweets (tweet_id, author_handle, text_raw, tweet_url, created_at, status, viral_score, impact_score)
                VALUES (?, ?, ?, ?, ?, 'PROCESSED', ?, 8)
                """,
                (f"tweet_night_{i}", "test_author", f"Post text {i}", f"https://x.com/test/{i}", now_iso, float(80 + i))
            )
        await db.commit()

    # Schedule with 60 min intervals and client UTC+4 offset (-240 mins)
    scheduled = await auto_schedule_queue(interval_minutes=60, start_offset_minutes=30, tz_offset_minutes=-240)
    assert len(scheduled) >= 15

    for post in scheduled:
        sched_str = post.get("scheduled_at")
        assert sched_str is not None
        sched_dt = datetime.fromisoformat(sched_str)
        # Check hour in the scheduled timezone
        hour = sched_dt.hour
        # Night quiet hours: 23:00 to 08:00 -> no post at 23:xx, 00:xx, ... 07:xx
        is_night = (hour >= 23 or hour < 8)
        assert not is_night, f"Post {post['tweet_id']} was scheduled at night hour {hour}: {sched_str}"


@pytest.mark.asyncio
async def test_normalize_to_utc_iso():
    from src.db import normalize_to_utc_iso
    # 1. None
    res = normalize_to_utc_iso(None)
    assert res.endswith("Z")

    # 2. Naive datetime (assumes UTC)
    dt_naive = datetime(2026, 9, 20, 14, 30, 0)
    assert normalize_to_utc_iso(dt_naive) == "2026-09-20T14:30:00Z"

    # 3. Aware datetime with offset
    tz_plus_4 = timezone(timedelta(hours=4))
    dt_aware = datetime(2026, 9, 20, 18, 30, 0, tzinfo=tz_plus_4)
    assert normalize_to_utc_iso(dt_aware) == "2026-09-20T14:30:00Z"

    # 4. String with offset
    s_offset = "2026-09-20T18:30:00+04:00"
    assert normalize_to_utc_iso(s_offset) == "2026-09-20T14:30:00Z"

    # 5. String with Z
    s_z = "2026-09-20T14:30:00Z"
    assert normalize_to_utc_iso(s_z) == "2026-09-20T14:30:00Z"


@pytest.mark.asyncio
async def test_staging_digest_lifecycle(temp_db_path):
    """Verifies Many-to-Many digest_items tracking, status transition to IN_DIGEST and POSTED."""
    import aiosqlite
    from src.db import (
        init_db,
        save_raw_tweets,
        create_digest_draft,
        publish_digest,
        get_digests_list,
        get_staging_tweets,
    )

    await init_db()
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Insert 3 tweets
    raw = [
        {
            "tweet_id": f"t_stage_{i}",
            "author_handle": f"@author{i}",
            "author_name": f"Author {i}",
            "text_raw": f"Tweet content number {i}",
            "tweet_url": f"https://x.com/author/status/{i}",
            "created_at": now_iso,
        }
        for i in range(1, 4)
    ]
    await save_raw_tweets(raw)
    from src.db import update_tweet_status_and_score
    for i in range(1, 4):
        await update_tweet_status_and_score(f"t_stage_{i}", score=float(50 + i * 10), is_viral=True, status="PROCESSED")

    # 2. Check staging pool
    staging_pool = await get_staging_tweets(strategy="BY_VIRAL_SCORE")
    assert len(staging_pool) == 3
    # Ordered by viral_score descending
    assert staging_pool[0]["tweet_id"] == "t_stage_3"
    assert staging_pool[1]["tweet_id"] == "t_stage_2"
    assert staging_pool[2]["tweet_id"] == "t_stage_1"

    # 3. Create digest draft with t_stage_1 and t_stage_3
    selected_ids = ["t_stage_1", "t_stage_3"]
    digest_id = await create_digest_draft(
        title="Тестовый дайджест",
        formatted_text="🔥 Главное за сегодня:\n1. Новость\n2. Еще новость",
        tweet_ids=selected_ids
    )
    assert digest_id is not None

    # Check that tweets have status IN_DIGEST
    async with aiosqlite.connect(temp_db_path) as db:
        async with db.execute("SELECT tweet_id, status, digest_id FROM tweets WHERE tweet_id IN ('t_stage_1', 't_stage_3')") as cur:
            rows = await cur.fetchall()
            assert len(rows) == 2
            for row in rows:
                assert row[1] == "IN_DIGEST"
                assert row[2] == digest_id

        # Check digest_items records
        async with db.execute("SELECT digest_id, tweet_id, added_at FROM digest_items WHERE digest_id = ?", (digest_id,)) as cur:
            items = await cur.fetchall()
            assert len(items) == 2
            item_tids = {item[1] for item in items}
            assert item_tids == {"t_stage_1", "t_stage_3"}
            for item in items:
                assert item[2] is not None
                assert "Z" in item[2] or "+00:00" in item[2]

    # 4. Check get_digests_list
    digests = await get_digests_list()
    assert len(digests) >= 1
    found_digest = next((d for d in digests if d["id"] == digest_id), None)
    assert found_digest is not None
    assert found_digest["status"] == "DRAFT"
    assert found_digest["tweet_count"] == 2
    assert len(found_digest["tweets"]) == 2

    # 5. Publish digest
    success = await publish_digest(digest_id, telegram_message_id=9999)
    assert success is True

    # Verify digest and tweets status are now POSTED
    async with aiosqlite.connect(temp_db_path) as db:
        async with db.execute("SELECT status, telegram_message_id, posted_at FROM digests WHERE id = ?", (digest_id,)) as cur:
            d_row = await cur.fetchone()
            assert d_row[0] == "POSTED"
            assert d_row[1] == 9999
            assert d_row[2] is not None

        async with db.execute("SELECT status, posted_at FROM tweets WHERE tweet_id IN ('t_stage_1', 't_stage_3')") as cur:
            t_rows = await cur.fetchall()
            for r in t_rows:
                assert r[0] == "POSTED"
                assert r[1] is not None


@pytest.mark.asyncio
async def test_staging_tweets_sorting_strategies(temp_db_path):
    """Verifies get_staging_tweets with different sorting strategies."""
    from src.db import init_db, save_raw_tweets, update_tweet_status_and_score, get_staging_tweets

    await init_db()
    t1_dt = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc).isoformat()
    t2_dt = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc).isoformat()
    t3_dt = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc).isoformat()

    tweets = [
        {"tweet_id": "s_old", "author_handle": "@old", "text_raw": "old", "tweet_url": "u1", "created_at": t1_dt},
        {"tweet_id": "s_mid", "author_handle": "@mid", "text_raw": "mid", "tweet_url": "u2", "created_at": t2_dt},
        {"tweet_id": "s_new", "author_handle": "@new", "text_raw": "new", "tweet_url": "u3", "created_at": t3_dt},
    ]
    await save_raw_tweets(tweets)
    await update_tweet_status_and_score("s_old", score=100.0, is_viral=True, status="RAW")
    await update_tweet_status_and_score("s_mid", score=50.0, is_viral=True, status="PROCESSED")
    await update_tweet_status_and_score("s_new", score=75.0, is_viral=True, status="SCHEDULED")

    # BY_VIRAL_SCORE
    by_score = await get_staging_tweets("BY_VIRAL_SCORE")
    assert [t["tweet_id"] for t in by_score] == ["s_old", "s_new", "s_mid"]

    # CHRONOLOGICAL_DESC (newest first)
    by_newest = await get_staging_tweets("CHRONOLOGICAL_DESC")
    assert [t["tweet_id"] for t in by_newest] == ["s_new", "s_mid", "s_old"]

    # CHRONOLOGICAL_ASC (oldest first)
    by_oldest = await get_staging_tweets("CHRONOLOGICAL_ASC")
    assert [t["tweet_id"] for t in by_oldest] == ["s_old", "s_mid", "s_new"]

    # RANDOM
    random_pool = await get_staging_tweets("RANDOM")
    assert len(random_pool) == 3


@pytest.mark.asyncio
async def test_due_scheduled_digests_and_scheduler_publish(temp_db_path, mocker, monkeypatch):
    """Verifies that scheduled digests due for publication are detected and published by scheduler."""
    from src.config import settings
    from src.db import init_db, create_digest_draft, get_due_scheduled_digests, normalize_to_utc_iso
    from src.scheduler import check_and_publish_scheduled_digests
    import aiosqlite

    monkeypatch.setattr(settings, "ai_api_key", "test_gemini_key")
    monkeypatch.setattr(settings, "telegram_bot_token", "123456789:ABCDefGhIjKlMnOpQrStUvWxYz")
    monkeypatch.setattr(settings, "telegram_channel_id", "@test_channel")

    await init_db()
    did1 = await create_digest_draft("Past Due Digest", "Formatted text 1", [])
    did2 = await create_digest_draft("Future Digest", "Formatted text 2", [])

    past_iso = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    future_iso = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()

    async with aiosqlite.connect(temp_db_path) as db:
        await db.execute("UPDATE digests SET status = 'SCHEDULED', scheduled_at = ? WHERE id = ?", (past_iso, did1))
        await db.execute("UPDATE digests SET status = 'SCHEDULED', scheduled_at = ? WHERE id = ?", (future_iso, did2))
        await db.commit()

    due = await get_due_scheduled_digests()
    assert len(due) == 1
    assert due[0]["id"] == did1

    mock_send = mocker.patch("src.telegram.sender.send_post_to_channel", AsyncMock(return_value=8888))
    published_count = await check_and_publish_scheduled_digests()
    assert published_count == 1

    # Verify did1 is now POSTED, while did2 remains SCHEDULED
    async with aiosqlite.connect(temp_db_path) as db:
        async with db.execute("SELECT id, status, telegram_message_id FROM digests ORDER BY id ASC") as cur:
            rows = await cur.fetchall()
            assert rows[0][0] == did1
            assert rows[0][1] == "POSTED"
            assert rows[0][2] == 8888
            assert rows[1][0] == did2
            assert rows[1][1] == "SCHEDULED"




