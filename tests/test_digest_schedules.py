import pytest

from src.config import settings
from src.db import (
    delete_digest_schedule,
    get_digest_schedules,
    init_db,
    save_digest_schedule,
)
from src.digest import run_digest_schedule_rule


@pytest.mark.asyncio
async def test_digest_schedule_rule_sorts_and_limits(monkeypatch):
    tweets = [
        {"tweet_id": "low", "viral_score": 10, "created_at": "2026-09-21T10:00:00+00:00"},
        {"tweet_id": "high", "viral_score": 90, "created_at": "2026-09-21T09:00:00+00:00"},
        {"tweet_id": "mid", "viral_score": 50, "created_at": "2026-09-21T08:00:00+00:00"},
        {"tweet_id": "overflow", "viral_score": 1, "created_at": "2026-09-21T07:00:00+00:00"},
    ]
    captured = {}

    async def fake_build(**kwargs):
        captured.update(kwargs)
        return ["digest"]

    async def fake_init_db():
        return None

    async def fake_get_unposted_tweets():
        return tweets.copy()

    monkeypatch.setattr("src.digest.init_db", fake_init_db)
    monkeypatch.setattr("src.digest.get_unposted_tweets", fake_get_unposted_tweets)
    monkeypatch.setattr("src.digest.build_and_send_digest_chunks", fake_build)

    result = await run_digest_schedule_rule({
        "title": "Топ новостей",
        "max_items_per_post": 1,
        "max_posts": 2,
        "sort_order": "BY_VIRAL_SCORE",
        "interval_minutes": 7,
    })

    assert result == ["digest"]
    assert [tweet["tweet_id"] for tweet in captured["tweets"]] == ["high", "mid"]
    assert captured["chunk_size"] == 1
    assert captured["max_posts"] == 2
    assert captured["interval_minutes"] == 7


@pytest.mark.asyncio
async def test_digest_schedule_crud(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "digest-schedules.sqlite"))
    await init_db()
    schedule_id = await save_digest_schedule({
        "time": "07:00",
        "title": "Пока вы спали",
        "max_items_per_post": 5,
        "max_posts": 3,
        "sort_order": "BY_VIRAL_SCORE",
        "interval_minutes": 5,
        "enabled": True,
    })
    schedules = await get_digest_schedules()
    assert len(schedules) == 1
    assert schedules[0]["id"] == schedule_id
    assert schedules[0]["max_posts"] == 3
    await delete_digest_schedule(schedule_id)
    assert await get_digest_schedules() == []
