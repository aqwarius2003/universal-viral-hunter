import json
import pytest
from unittest.mock import AsyncMock, patch
from src.db import init_db, save_raw_tweets, get_tweet_by_id
from src.ai.agents.editor import reprocess_tweet_with_editor_feedback


@pytest.mark.asyncio
async def test_reprocess_tweet_with_editor_feedback(tmp_path, monkeypatch):
    test_db = str(tmp_path / "test_reprocess.sqlite")
    from src.config import settings
    monkeypatch.setattr(settings, "db_path", test_db)
    monkeypatch.setattr(settings, "approval_required", True)

    await init_db()

    # Insert sample tweet
    tweet = {
        "tweet_id": "test_tw_1",
        "author_handle": "@driver_ca",
        "author_name": "Driver",
        "text_raw": "Driver cleared snow from previous car mirrors in traffic",
        "tweet_url": "https://x.com/driver_ca/status/1",
        "created_at": "2026-10-16T10:00:00Z",
        "likes_count": 100,
        "reposts_count": 20,
        "viral_score": 85.0,
        "status": "PROCESSED",
    }
    await save_raw_tweets([tweet])

    # Mock call_llm to return test summary
    mock_llm_response = {
        "title_ru": "Водитель помог соседу по пробке",
        "summary_ru": "В Канаде автомобилист проявил вежливость и почистил снег с зеркал стоящей впереди машины.",
        "takeaways": ["Взаимовыручка на дороге", "Безопасность движения"],
        "hashtags": ["#Добро", "#Авто", "#Канада"],
        "impact_score": 8,
    }

    with patch("src.ai.agents.editor.call_llm", AsyncMock(return_value=mock_llm_response)) as mocked_call:
        res = await reprocess_tweet_with_editor_feedback("test_tw_1", "будь милее")

        # Verify call_llm was invoked with editor_instruction
        mocked_call.assert_called_once()
        call_kwargs = mocked_call.call_args.kwargs
        assert call_kwargs.get("editor_instruction") == "будь милее"

        # Verify technical audit ran
        assert res["audit"]["is_valid"] is True
        assert res["status"] == "PENDING_APPROVAL"

        # Verify saved in DB
        db_tweet = await get_tweet_by_id("test_tw_1")
        assert db_tweet["status"] == "PENDING_APPROVAL"
        summary_obj = json.loads(db_tweet["summary_json"])
        assert summary_obj["title_ru"] == "Водитель помог соседу по пробке"


@pytest.mark.asyncio
async def test_legacy_check_constraint_migration(tmp_path, monkeypatch):
    """Test that existing databases with legacy CHECK constraints migrate seamlessly to support PROCESSING and PENDING_APPROVAL."""
    test_db = str(tmp_path / "legacy_test.sqlite")
    from src.config import settings
    monkeypatch.setattr(settings, "db_path", test_db)

    import aiosqlite
    # Manually create table with old schema (without PROCESSING or PENDING_APPROVAL)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("""
            CREATE TABLE tweets (
                tweet_id TEXT PRIMARY KEY,
                author_handle TEXT NOT NULL,
                author_name TEXT,
                text_raw TEXT NOT NULL,
                tweet_url TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                viral_score REAL NOT NULL,
                status TEXT CHECK(status IN ('RAW', 'PROCESSED', 'SCHEDULED', 'POSTED', 'FAILED', 'IGNORED', 'IN_DIGEST')) DEFAULT 'RAW'
            )
        """)
        await db.execute("INSERT INTO tweets (tweet_id, author_handle, text_raw, tweet_url, created_at, viral_score, status) VALUES ('old_1', '@user', 'text', 'https://x.com', '2026-10-16', 50.0, 'PROCESSED')")
        await db.commit()

    # Run init_db which should detect and migrate the CHECK constraint
    await init_db()

    # Now verify that updating to PROCESSING and PENDING_APPROVAL does NOT raise IntegrityError
    from src.db import set_tweet_status, get_tweet_by_id
    await set_tweet_status("old_1", "PROCESSING")
    t1 = await get_tweet_by_id("old_1")
    assert t1["status"] == "PROCESSING"

    await set_tweet_status("old_1", "PENDING_APPROVAL")
    t2 = await get_tweet_by_id("old_1")
    assert t2["status"] == "PENDING_APPROVAL"

