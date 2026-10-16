import json
import pytest
from aiohttp.test_utils import TestClient, TestServer
from src.config import settings
from src.db import init_db, save_raw_tweets, update_tweet_status_and_score
from src.web.server import create_app


@pytest.mark.asyncio
async def test_web_app_endpoints(tmp_path, monkeypatch):
    """Integration test for web server routes and API using TestClient."""
    db_file = tmp_path / "test_web.sqlite"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "auth_enabled", False)
    await init_db()

    # Insert sample test tweets
    await save_raw_tweets([
        {
            "tweet_id": "web_1",
            "author_handle": "@tech_insider",
            "author_name": "Tech Insider",
            "text_raw": "DeepSeek V3 Released with incredible benchmark results",
            "tweet_url": "https://x.com/tech_insider/status/web_1",
            "created_at": "2026-09-13T10:00:00+00:00",
            "likes_count": 500,
            "reposts_count": 100,
            "quotes_count": 20,
            "bookmarks_count": 50,
            "impressions_count": 50000,
            "media_urls": ["https://pbs.twimg.com/media/test.jpg"],
        }
    ])

    await update_tweet_status_and_score(
        tweet_id="web_1",
        score=250.0,
        is_viral=True,
        status="PROCESSED",
        impact_score=9,
        summary_json=json.dumps({
            "title_ru": "Релиз DeepSeek V3",
            "summary_ru": "Новая модель бьет рекорды.",
            "takeaways": ["Быстрее", "Дешевле", "Открытые веса"],
            "hashtags": ["#AI", "#DeepSeek"],
            "impact_score": 9,
            "selected_image_index": 0
        })
    )

    app = create_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    try:
        # 1. Test GET / (index.html)
        resp = await client.get("/")
        assert resp.status == 200
        html_text = await resp.text()
        assert "Viral Hunter" in html_text

        # 2. Test GET /api/posts/queue
        resp = await client.get("/api/posts/queue")
        assert resp.status == 200
        queue = await resp.json()
        assert len(queue) == 1
        assert queue[0]["tweet_id"] == "web_1"
        assert queue[0]["summary_data"]["title_ru"] == "Релиз DeepSeek V3"

        # 3. Test POST /api/posts/{id}/schedule
        resp = await client.post("/api/posts/web_1/schedule", json={"scheduled_at": "2026-09-13T15:00:00Z"})
        assert resp.status == 200
        res_data = await resp.json()
        assert res_data["status"] == "success"

        # 4. Test POST /api/posts/{id}/edit with scheduled_at
        resp = await client.post("/api/posts/web_1/edit", json={
            "title_ru": "Обновленный заголовок",
            "summary_ru": "Обновленная суть новости.",
            "takeaways": ["Пункт 1", "Пункт 2"],
            "hashtags": ["#AI"],
            "selected_image_index": None,
            "impact_score": 8,
            "scheduled_at": "2026-09-13T16:30:00Z"
        })
        assert resp.status == 200
        res_data = await resp.json()
        assert res_data["status"] == "updated"

        # Verify edit in queue
        resp = await client.get("/api/posts/queue")
        queue = await resp.json()
        assert queue[0]["summary_data"]["title_ru"] == "Обновленный заголовок"
        assert queue[0]["scheduled_at"] == "2026-09-13T16:30:00Z"

        # 5. Test POST /api/posts/reorder
        resp = await client.post("/api/posts/reorder", json={"ordered_ids": ["web_1"]})
        assert resp.status == 200

        # 5b. Test POST /api/posts/auto_schedule
        resp = await client.post("/api/posts/auto_schedule", json={"tz_offset_minutes": -240})
        assert resp.status == 200
        auto_sched_data = await resp.json()
        assert auto_sched_data["status"] == "success"
        assert auto_sched_data["count"] == 1

        # 6. Test GET /api/settings
        resp = await client.get("/api/settings")
        assert resp.status == 200
        settings_data = await resp.json()
        assert "min_viral_score" in settings_data
        assert "super_viral_threshold" in settings_data
        assert "daily_digest_time" in settings_data
        assert "quiet_hours_enabled" in settings_data
        assert "quiet_hours_start" in settings_data
        assert "quiet_hours_end" in settings_data

        # 7. Test POST /api/settings
        resp = await client.post("/api/settings", json={
            "min_viral_score": 85.5,
            "super_viral_threshold": 120.0,
            "max_posts_per_run": 2,
            "daily_digest_time": "07:00, 14:00, 21:00",
            "quiet_hours_enabled": True,
            "quiet_hours_start": "23:00",
            "quiet_hours_end": "08:00",
        })
        assert resp.status == 200
        updated_settings = await resp.json()
        assert updated_settings["settings"]["min_viral_score"] == 85.5
        assert updated_settings["settings"]["super_viral_threshold"] == 120.0
        assert updated_settings["settings"]["max_posts_per_run"] == 2
        assert updated_settings["settings"]["daily_digest_time"] == "07:00, 14:00, 21:00"
        assert updated_settings["settings"]["quiet_hours_enabled"] is True
        assert updated_settings["settings"]["quiet_hours_start"] == "23:00"
        assert updated_settings["settings"]["quiet_hours_end"] == "08:00"

        # 8. Test GET /api/status
        resp = await client.get("/api/status")
        assert resp.status == 200
        status_data = await resp.json()
        assert "status" in status_data
        assert "model" in status_data

        # 9. Test DELETE /api/posts/{id} (Cancel post)
        resp = await client.delete("/api/posts/web_1")
        assert resp.status == 200

        # Verify queue is now empty
        resp = await client.get("/api/posts/queue")
        queue = await resp.json()
        assert len(queue) == 0

        # 10. Test GET /api/system/history
        resp = await client.get("/api/system/history")
        assert resp.status == 200
        history = await resp.json()
        assert isinstance(history, list)

        # 11. Test GET /api/system/health
        resp = await client.get("/api/system/health")
        assert resp.status == 200
        health = await resp.json()
        assert "status" in health
        assert "services" in health
        assert "twitter" in health["services"]
        assert "gemini" in health["services"]
        assert "telegram" in health["services"]

    finally:
        await client.close()


@pytest.mark.asyncio
async def test_web_publish_now_handlers(tmp_path, monkeypatch, mocker):
    """Test handle_publish_now success and failure responses."""
    db_file = tmp_path / "test_publish_now.sqlite"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "auth_enabled", False)
    await init_db()

    await save_raw_tweets([
        {
            "tweet_id": "pub_1",
            "author_handle": "@tester",
            "author_name": "Tester",
            "text_raw": "Testing publish now",
            "tweet_url": "https://x.com/tester/status/pub_1",
            "created_at": "2026-09-13T10:00:00+00:00",
            "likes_count": 100,
            "reposts_count": 50,
            "impressions_count": 5000,
            "media_urls": [],
        }
    ])
    await update_tweet_status_and_score(
        tweet_id="pub_1",
        score=100.0,
        is_viral=True,
        status="PROCESSED",
        impact_score=8,
        summary_json=json.dumps({"title_ru": "Тест публикации", "summary_ru": "Суть"})
    )

    app = create_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    try:
        # Mock successful send_post_to_channel
        mocker.patch("src.web.server.send_post_to_channel", return_value=None)
        resp = await client.post("/api/posts/pub_1/publish_now")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "published"
        assert data["tweet_id"] == "pub_1"

        # Check published posts list
        resp = await client.get("/api/posts/published")
        assert resp.status == 200
        pub_list = await resp.json()
        assert len(pub_list) == 1
        assert pub_list[0]["status"] == "POSTED"
        assert pub_list[0]["posted_at"] is not None

        # Test error handling when Telegram throws RuntimeError
        mocker.patch("src.web.server.send_post_to_channel", side_effect=RuntimeError("Telegram-канал не найден"))
        resp = await client.post("/api/posts/pub_1/publish_now")
        assert resp.status == 400
        err_data = await resp.json()
        assert "Telegram-канал не найден" in err_data["error"]

    finally:
        await client.close()


@pytest.mark.asyncio
async def test_staging_endpoints_and_limits(tmp_path, monkeypatch, mocker):
    """Test staging API endpoints: tweets pool, preview with context limit, publish, and digests list."""
    db_file = tmp_path / "test_staging_web.sqlite"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "max_digest_items", 3)
    await init_db()

    # Insert 4 tweets
    tweets = [
        {
            "tweet_id": f"t_api_{i}",
            "author_handle": f"@author{i}",
            "author_name": f"Author {i}",
            "text_raw": f"Text {i}",
            "tweet_url": f"https://x.com/status/{i}",
            "created_at": f"2026-09-1{i}T10:00:00Z",
            "viral_score": float(10 * i),
            "status": "PROCESSED",
        }
        for i in range(1, 5)
    ]
    await save_raw_tweets(tweets)
    for i in range(1, 5):
        await update_tweet_status_and_score(f"t_api_{i}", score=float(10 * i), is_viral=True, status="PROCESSED")

    app = create_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    monkeypatch.setattr(settings, "max_digest_items", 3)

    try:
        # 1. GET /api/staging/tweets
        resp = await client.get("/api/staging/tweets?strategy=BY_VIRAL_SCORE")
        assert resp.status == 200
        pool = await resp.json()
        assert len(pool) == 4
        assert pool[0]["tweet_id"] == "t_api_4"

        # 2. POST /api/staging/digest/preview - Exceeding limit (4 > max 3)
        resp = await client.post("/api/staging/digest/preview", json={
            "tweet_ids": ["t_api_1", "t_api_2", "t_api_3", "t_api_4"],
            "title": "Слишком много постов"
        })
        assert resp.status == 400
        err = await resp.json()
        assert "Превышен лимит" in err["error"]

        # 3. POST /api/staging/digest/preview - Success
        mocker.patch(
            "src.digest.generate_digest_with_gemini",
            return_value="1. Тема раз\n2. Тема два"
        )
        resp = await client.post("/api/staging/digest/preview", json={
            "tweet_ids": ["t_api_1", "t_api_2"],
            "title": "Выпуск новостей"
        })
        assert resp.status == 200
        prev_data = await resp.json()
        assert prev_data["status"] == "success"
        assert "Тема раз" in prev_data["formatted_text"]
        assert prev_data["tweets_count"] == 2

        # 4. POST /api/staging/digest/publish - Scheduled
        resp = await client.post("/api/staging/digest/publish", json={
            "tweet_ids": ["t_api_1"],
            "title": "Отложенный дайджест",
            "formatted_text": "Отложенный текст",
            "scheduled_at": "2026-09-21T18:00:00+04:00"
        })
        assert resp.status == 200
        sched_data = await resp.json()
        assert sched_data["status"] == "scheduled"
        assert sched_data["scheduled_at"] == "2026-09-21T14:00:00Z"

        # 5. POST /api/staging/digest/publish - Immediate publish
        mocker.patch("src.web.server.send_post_to_channel", return_value=12345)
        resp = await client.post("/api/staging/digest/publish", json={
            "tweet_ids": ["t_api_2", "t_api_3"],
            "title": "Срочный дайджест",
            "formatted_text": "Срочный текст"
        })
        assert resp.status == 200
        pub_data = await resp.json()
        assert pub_data["status"] == "published"
        assert "digest_id" in pub_data

        # 6. GET /api/digests - History
        resp = await client.get("/api/digests")
        assert resp.status == 200
        digests = await resp.json()
        assert len(digests) >= 2
        # Check that nested tweets are present
        assert "tweets" in digests[0]
        assert "tweet_count" in digests[0]

    finally:
        await client.close()


@pytest.mark.asyncio
async def test_settings_new_fields(tmp_path, monkeypatch):
    """Verifies that universal-engine settings fields are properly returned and updated."""
    db_file = tmp_path / "test_settings_new.sqlite"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "auth_enabled", False)
    await init_db()

    app = create_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    try:
        # GET /api/settings
        resp = await client.get("/api/settings")
        assert resp.status == 200
        s = await resp.json()
        assert "max_digest_items" in s
        assert "queue_sort_strategy" in s
        assert "backlog_digest_enabled" in s
        assert "backlog_max_chunk_size" in s
        assert "show_metric_likes" in s
        assert "show_metric_impact" in s
        assert "show_metric_author" in s
        assert "show_metric_hashtags" in s
        assert "show_metric_source_btn" in s
        assert "show_metric_takeaways" in s
        assert "max_title_words" in s
        assert "telegram_max_caption_len" in s
        assert "telegram_max_text_len" in s
        assert "ai_temperature" in s
        assert "max_hashtags" in s
        assert "scout_search_count" in s

        # POST /api/settings
        resp = await client.post("/api/settings", json={
            "max_digest_items": 15,
            "queue_sort_strategy": "RANDOM",
            "backlog_digest_enabled": False,
            "backlog_max_chunk_size": 8,
            "x_search_queries": "AI agents, LangChain",
            "x_relevance_prompt": "Релевантно ли для AI?",
            "editor_system_prompt": "Строгий редактор",
            "show_metric_likes": True,
            "show_metric_source_btn": False,
            "show_metric_takeaways": True,
            "max_title_words": 30,
            "telegram_max_caption_len": 2048,
            "telegram_max_text_len": 8192,
            "ai_temperature": 0.7,
            "max_hashtags": 7,
            "scout_search_count": 25,
        })
        assert resp.status == 200
        res = await resp.json()
        updated = res["settings"]
        assert updated["max_digest_items"] == 15
        assert updated["queue_sort_strategy"] == "RANDOM"
        assert updated["backlog_digest_enabled"] is False
        assert updated["backlog_max_chunk_size"] == 8
        assert updated["x_search_queries"] == "AI agents, LangChain"
        assert updated["x_relevance_prompt"] == "Релевантно ли для AI?"
        assert updated["editor_system_prompt"] == "Строгий редактор"
        assert updated["show_metric_source_btn"] is False
        assert updated["show_metric_takeaways"] is True
        assert updated["max_title_words"] == 30
        assert updated["telegram_max_caption_len"] == 2048
        assert updated["telegram_max_text_len"] == 8192
        assert updated["ai_temperature"] == 0.7
        assert updated["max_hashtags"] == 7
        assert updated["scout_search_count"] == 25

    finally:
        await client.close()


@pytest.mark.asyncio
async def test_version_and_reprocess_api(tmp_path, monkeypatch, mocker):
    """Test /api/version endpoint and /api/posts/{id}/reprocess endpoint."""
    db_file = tmp_path / "test_version_api.sqlite"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "auth_enabled", False)
    await init_db()

    # Save a test tweet
    await save_raw_tweets([{
        "tweet_id": "test_reprocess_1",
        "author_handle": "@tester",
        "author_name": "Tester",
        "text_raw": "Original tweet text",
        "tweet_url": "https://x.com/tester/status/123",
        "created_at": "2026-10-16T10:00:00Z",
    }])
    await update_tweet_status_and_score(
        tweet_id="test_reprocess_1",
        score=100.0,
        is_viral=True,
        status="PROCESSED",
        impact_score=8,
        summary_json=json.dumps({
            "title_ru": "Заголовок",
            "summary_ru": "Суть поста",
            "takeaways": ["Тезис"],
            "hashtags": ["#Тест"],
        })
    )

    app = create_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    try:
        # 1. Test GET /api/version
        resp = await client.get("/api/version")
        assert resp.status == 200
        ver_data = await resp.json()
        assert "version" in ver_data
        assert "git_commit" in ver_data
        assert ver_data["version"] == "v2.1.0"
        assert ver_data["raw_version"] == "2.1.0"

        # 2. Mock reprocess_tweet_with_editor_feedback and test POST /api/posts/{id}/reprocess
        mocker.patch(
            "src.web.server.reprocess_tweet_with_editor_feedback",
            return_value={
                "tweet_id": "test_reprocess_1",
                "summary_data": {
                    "title_ru": "Милый заголовок",
                    "summary_ru": "Милая суть без жести",
                    "takeaways": ["Милый тезис"],
                    "hashtags": ["#Добро"],
                }
            }
        )
        resp = await client.post("/api/posts/test_reprocess_1/reprocess", json={
            "instruction": "будь милее, без жести"
        })
        assert resp.status == 200
        rep_data = await resp.json()
        assert rep_data["status"] == "success"
        assert rep_data["tweet"]["summary_data"]["title_ru"] == "Милый заголовок"

        # 3. Test empty instruction error
        resp = await client.post("/api/posts/test_reprocess_1/reprocess", json={"instruction": ""})
        assert resp.status == 400

        # 4. Test POST /api/telegram/sync without admin_id
        monkeypatch.setattr(settings, "telegram_admin_id", None)
        resp = await client.post("/api/telegram/sync")
        assert resp.status == 400
        sync_err = await resp.json()
        assert "Telegram Admin ID не настроен" in sync_err["error"]

        # 5. Test POST /api/telegram/sync with admin_id
        monkeypatch.setattr(settings, "telegram_admin_id", 123456789)
        mocker.patch("src.web.server.send_post_card_to_admin", return_value=True)
        resp = await client.post("/api/telegram/sync")
        assert resp.status == 200
        sync_res = await resp.json()
        assert sync_res["status"] == "success"
        assert sync_res["synced_count"] >= 1
    finally:
        await client.close()


