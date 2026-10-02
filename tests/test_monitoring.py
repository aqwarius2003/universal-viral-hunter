import asyncio
import json
import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from aiohttp.test_utils import TestClient, TestServer

from src.config import settings
from src.db import (
    init_db,
    record_pipeline_run,
    get_recent_pipeline_runs,
)
from src.web.server import (
    create_app,
    check_twitter_health,
    check_gemini_health,
    check_telegram_health,
)


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_monitoring.sqlite"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "auth_enabled", False)
    return str(db_file)


@pytest.mark.asyncio
async def test_db_pipeline_runs_crud(test_db):
    """Tests init_db creates pipeline_runs table, record_pipeline_run and get_recent_pipeline_runs."""
    await init_db()

    # Empty initially
    runs = await get_recent_pipeline_runs(limit=10)
    assert runs == []

    # Insert a SUCCESS run
    await record_pipeline_run(
        started_at="2026-09-14T01:00:00Z",
        finished_at="2026-09-14T01:00:15Z",
        duration_sec=15.2,
        tweets_found=20,
        viral_found=5,
        status="SUCCESS",
        error_message=None,
    )

    # Insert an ERROR run
    await record_pipeline_run(
        started_at="2026-09-14T01:30:00Z",
        finished_at="2026-09-14T01:30:02Z",
        duration_sec=2.1,
        tweets_found=0,
        viral_found=0,
        status="ERROR",
        error_message="Twitter session expired",
    )

    runs = await get_recent_pipeline_runs(limit=10)
    assert len(runs) == 2

    # Verify order: newest (highest id) first
    assert runs[0]["status"] == "ERROR"
    assert runs[0]["error_message"] == "Twitter session expired"
    assert runs[0]["duration_sec"] == 2.1
    assert runs[0]["tweets_found"] == 0

    assert runs[1]["status"] == "SUCCESS"
    assert runs[1]["tweets_found"] == 20
    assert runs[1]["viral_found"] == 5
    assert runs[1]["error_message"] is None

    # Test limit parameter
    limited = await get_recent_pipeline_runs(limit=1)
    assert len(limited) == 1
    assert limited[0]["id"] == runs[0]["id"]


@pytest.mark.asyncio
async def test_run_pipeline_records_success(test_db, monkeypatch):
    """Verifies run_pipeline logs a successful run into pipeline_runs."""
    await init_db()

    mock_state = {
        "raw_tweets": [{"tweet_id": "1"}, {"tweet_id": "2"}],
        "viral_tweets": [{"tweet_id": "1"}],
        "draft_posts": [],
        "validation_errors": [],
        "ready_to_publish": [{"text": "Sample tweet", "tweet_url": "https://x.com/1"}],
    }

    mock_app = MagicMock()
    mock_app.ainvoke = AsyncMock(return_value=mock_state)

    with patch("src.main.StateGraph.compile", return_value=mock_app):
        from src.main import run_pipeline
        result = await run_pipeline()
        assert result == mock_state

    runs = await get_recent_pipeline_runs(limit=10)
    assert len(runs) == 1
    assert runs[0]["status"] == "SUCCESS"
    assert runs[0]["tweets_found"] == 2
    assert runs[0]["viral_found"] == 1
    assert runs[0]["error_message"] is None
    assert runs[0]["duration_sec"] >= 0


@pytest.mark.asyncio
async def test_run_pipeline_records_error(test_db, monkeypatch):
    """Verifies run_pipeline logs an error run into pipeline_runs when an exception occurs."""
    await init_db()

    mock_app = MagicMock()
    mock_app.ainvoke = AsyncMock(side_effect=RuntimeError("LangGraph processing failure"))

    with patch("src.main.StateGraph.compile", return_value=mock_app):
        from src.main import run_pipeline
        with pytest.raises(RuntimeError, match="LangGraph processing failure"):
            await run_pipeline()

    runs = await get_recent_pipeline_runs(limit=10)
    assert len(runs) == 1
    assert runs[0]["status"] == "ERROR"
    assert "LangGraph processing failure" in runs[0]["error_message"]
    assert runs[0]["tweets_found"] == 0
    assert runs[0]["viral_found"] == 0


@pytest.mark.asyncio
async def test_check_twitter_health_missing_credentials(monkeypatch):
    """Verifies check_twitter_health returns error when credentials are not configured."""
    monkeypatch.setattr(settings, "twitter_auth_token", None)
    monkeypatch.setattr(settings, "twitter_ct0", None)

    with patch("src.web.server.load_cookies_from_file", return_value={}), \
         patch("pathlib.Path.is_file", return_value=False):
        result = await check_twitter_health()
        assert result["status"] == "error"
        assert "Сессия не настроена" in result["message"]


@pytest.mark.asyncio
async def test_check_twitter_health_success(monkeypatch):
    """Verifies check_twitter_health returns ok when client returns valid user."""
    monkeypatch.setattr(settings, "twitter_auth_token", "valid_token")
    monkeypatch.setattr(settings, "twitter_ct0", "valid_ct0")

    mock_user = MagicMock()
    mock_user.screen_name = "test_ai_user"

    mock_client = MagicMock()
    mock_client.user = AsyncMock(return_value=mock_user)

    with patch("src.web.server.get_twitter_client", AsyncMock(return_value=mock_client)):
        result = await check_twitter_health()
        assert result["status"] == "ok"
        assert "Сессия активна" in result["message"]
        assert "test_ai_user" in result["message"]


@pytest.mark.asyncio
async def test_check_twitter_health_unauthorized(monkeypatch):
    """Verifies check_twitter_health returns error on expired/unauthorized cookies."""
    monkeypatch.setattr(settings, "twitter_auth_token", "expired_token")
    monkeypatch.setattr(settings, "twitter_ct0", "expired_ct0")

    mock_client = MagicMock()
    mock_client.user = AsyncMock(side_effect=Exception("401 Unauthorized: Could not authenticate"))

    with patch("src.web.server.get_twitter_client", AsyncMock(return_value=mock_client)):
        result = await check_twitter_health()
        assert result["status"] == "error"
        assert "Сессия недействительна" in result["message"]


@pytest.mark.asyncio
async def test_check_twitter_health_transient_warning(monkeypatch):
    """Verifies check_twitter_health returns warning on transient network error."""
    monkeypatch.setattr(settings, "twitter_auth_token", "token_valid_123")
    monkeypatch.setattr(settings, "twitter_ct0", "ct0_valid_123")

    mock_client = MagicMock()
    mock_client.user = AsyncMock(side_effect=TimeoutError("Connection timed out"))

    with patch("src.web.server.get_twitter_client", AsyncMock(return_value=mock_client)):
        result = await check_twitter_health()
        assert result["status"] == "warning"
        assert "не ответил" in result["message"]


@pytest.mark.asyncio
async def test_check_gemini_health(monkeypatch):
    """Verifies check_gemini_health for ok, waiting, error, and missing key."""
    # 1. Missing key
    monkeypatch.setattr(settings, "ai_api_key", "your_gemini_api_key")
    res_nokey = await check_gemini_health()
    assert res_nokey["status"] == "error"
    assert "AI_API_KEY" in res_nokey["message"]

    # 2. Key present and ok status
    monkeypatch.setattr(settings, "ai_api_key", "AIzaSyFakeKey12345")
    monkeypatch.setattr(settings, "ai_model_name", "gemini-3.5-flash-lite")

    with patch("src.web.server.get_ai_status", return_value={"status": "ok", "message": "Готов к работе"}):
        res_ok = await check_gemini_health()
        assert res_ok["status"] == "ok"
        assert "gemini-3.5-flash-lite готов" in res_ok["message"]

    # 3. Status is waiting (rate limit)
    with patch("src.web.server.get_ai_status", return_value={"status": "waiting", "waiting_seconds": 15, "message": "Пауза RPM"}):
        res_waiting = await check_gemini_health()
        assert res_waiting["status"] == "warning"
        assert "ожидание лимитов" in res_waiting["message"]

    # 4. Status is error
    with patch("src.web.server.get_ai_status", return_value={"status": "error", "message": "Quota exceeded"}):
        res_err = await check_gemini_health()
        assert res_err["status"] == "error"
        assert "Quota exceeded" in res_err["message"]


@pytest.mark.asyncio
async def test_check_telegram_health_missing_tokens(monkeypatch):
    """Verifies check_telegram_health handles missing or placeholder tokens."""
    monkeypatch.setattr(settings, "telegram_bot_token", "your_bot_token_here")
    res = await check_telegram_health()
    assert res["status"] == "error"
    assert "TELEGRAM_BOT_TOKEN" in res["message"]


@pytest.mark.asyncio
async def test_check_telegram_health_admin_ok(monkeypatch):
    """Verifies check_telegram_health returns ok when bot is channel administrator."""
    monkeypatch.setattr(settings, "telegram_bot_token", "123456789:ABCDefGhIjKlMnOpQrStUvWxYz")
    monkeypatch.setattr(settings, "telegram_channel_id", "@test_channel")

    mock_me = MagicMock()
    mock_me.id = 123456
    mock_me.username = "ai_viral_bot"

    mock_chat = MagicMock()
    mock_chat.title = "AI News Daily"

    mock_admin = MagicMock()
    mock_admin.user = MagicMock()
    mock_admin.user.id = 123456

    mock_bot_instance = MagicMock()
    mock_bot_instance.get_me = AsyncMock(return_value=mock_me)
    mock_bot_instance.get_chat = AsyncMock(return_value=mock_chat)
    mock_bot_instance.get_chat_administrators = AsyncMock(return_value=[mock_admin])
    mock_bot_instance.session = MagicMock()
    mock_bot_instance.session.close = AsyncMock()

    with patch("aiogram.Bot", return_value=mock_bot_instance):
        res = await check_telegram_health()
        assert res["status"] == "ok"
        assert "@ai_viral_bot" in res["message"]
        assert "администратор" in res["message"]
        assert "AI News Daily" in res["message"]


@pytest.mark.asyncio
async def test_check_telegram_health_not_admin(monkeypatch):
    """Verifies check_telegram_health returns warning when bot is not an admin."""
    monkeypatch.setattr(settings, "telegram_bot_token", "123456789:ABCDefGhIjKlMnOpQrStUvWxYz")
    monkeypatch.setattr(settings, "telegram_channel_id", "@test_channel")

    mock_me = MagicMock()
    mock_me.id = 123456
    mock_me.username = "ai_viral_bot"

    mock_chat = MagicMock()
    mock_chat.title = "AI News Daily"

    other_admin = MagicMock()
    other_admin.user = MagicMock()
    other_admin.user.id = 999999

    mock_bot_instance = MagicMock()
    mock_bot_instance.get_me = AsyncMock(return_value=mock_me)
    mock_bot_instance.get_chat = AsyncMock(return_value=mock_chat)
    mock_bot_instance.get_chat_administrators = AsyncMock(return_value=[other_admin])
    mock_bot_instance.session = MagicMock()
    mock_bot_instance.session.close = AsyncMock()

    with patch("aiogram.Bot", return_value=mock_bot_instance):
        res = await check_telegram_health()
        assert res["status"] == "warning"
        assert "не является администратором" in res["message"]


@pytest.mark.asyncio
async def test_web_endpoints_health_and_history(test_db, monkeypatch):
    """Integration test for GET /api/system/health and GET /api/system/history."""
    await init_db()

    # Pre-populate 2 runs in history
    await record_pipeline_run(
        started_at="2026-09-14T01:10:00Z",
        finished_at="2026-09-14T01:10:05Z",
        duration_sec=5.0,
        tweets_found=15,
        viral_found=3,
        status="SUCCESS",
    )
    await record_pipeline_run(
        started_at="2026-09-14T01:20:00Z",
        finished_at="2026-09-14T01:20:03Z",
        duration_sec=3.2,
        tweets_found=10,
        viral_found=2,
        status="SUCCESS",
    )

    app = create_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    try:
        # 1. Test GET /api/system/history
        resp = await client.get("/api/system/history")
        assert resp.status == 200
        history = await resp.json()
        assert len(history) == 2
        assert history[0]["duration_sec"] == 3.2
        assert history[0]["tweets_found"] == 10
        assert history[1]["duration_sec"] == 5.0

        # Test limit query param
        resp_limit = await client.get("/api/system/history?limit=1")
        assert resp_limit.status == 200
        limited_history = await resp_limit.json()
        assert len(limited_history) == 1

        # 2. Test GET /api/system/health (all services)
        with patch("src.web.server.check_twitter_health", AsyncMock(return_value={"status": "ok", "message": "X ok"})), \
             patch("src.web.server.check_gemini_health", AsyncMock(return_value={"status": "ok", "message": "Gemini ok"})), \
             patch("src.web.server.check_telegram_health", AsyncMock(return_value={"status": "ok", "message": "TG ok"})):

            resp = await client.get("/api/system/health")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
            assert data["services"]["twitter"]["message"] == "X ok"
            assert data["services"]["gemini"]["message"] == "Gemini ok"
            assert data["services"]["telegram"]["message"] == "TG ok"

        # 3. Test GET /api/system/health?service=twitter
        with patch("src.web.server.check_twitter_health", AsyncMock(return_value={"status": "ok", "message": "X single ok"})):
            resp = await client.get("/api/system/health?service=twitter")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
            assert "twitter" in data["services"]
            assert data["services"]["twitter"]["message"] == "X single ok"

        # 4. Test overall status aggregation with warning/error
        with patch("src.web.server.check_twitter_health", AsyncMock(return_value={"status": "warning", "message": "X warn"})), \
             patch("src.web.server.check_gemini_health", AsyncMock(return_value={"status": "ok", "message": "Gemini ok"})), \
             patch("src.web.server.check_telegram_health", AsyncMock(return_value={"status": "ok", "message": "TG ok"})):

            resp = await client.get("/api/system/health")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "warning"

        with patch("src.web.server.check_twitter_health", AsyncMock(return_value={"status": "error", "message": "X err"})), \
             patch("src.web.server.check_gemini_health", AsyncMock(return_value={"status": "ok", "message": "Gemini ok"})), \
             patch("src.web.server.check_telegram_health", AsyncMock(return_value={"status": "ok", "message": "TG ok"})):

            resp = await client.get("/api/system/health")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "error"

        # 5. Test unknown service query param (falls back to all services)
        with patch("src.web.server.check_twitter_health", AsyncMock(return_value={"status": "ok", "message": "X ok"})), \
             patch("src.web.server.check_gemini_health", AsyncMock(return_value={"status": "ok", "message": "Gemini ok"})), \
             patch("src.web.server.check_telegram_health", AsyncMock(return_value={"status": "ok", "message": "TG ok"})):

            resp = await client.get("/api/system/health?service=unknown")
            assert resp.status == 200
            data = await resp.json()
            assert "twitter" in data["services"]
            assert "gemini" in data["services"]
            assert "telegram" in data["services"]

        # 6. Test history endpoint edge cases (invalid limit string, negative, excessive)
        resp_invalid = await client.get("/api/system/history?limit=invalid")
        assert resp_invalid.status == 200
        assert len(await resp_invalid.json()) == 2

        resp_neg = await client.get("/api/system/history?limit=-5")
        assert resp_neg.status == 200
        assert len(await resp_neg.json()) == 1  # Clamped to min 1

    finally:
        await client.close()


@pytest.mark.asyncio
async def test_record_pipeline_run_long_error(test_db):
    """Verifies pipeline_runs stores long error messages without truncating or failing."""
    await init_db()
    huge_error = "E" * 2000

    await record_pipeline_run(
        started_at="2026-09-14T02:00:00Z",
        finished_at="2026-09-14T02:00:05Z",
        duration_sec=5.0,
        tweets_found=0,
        viral_found=0,
        status="ERROR",
        error_message=huge_error,
    )

    runs = await get_recent_pipeline_runs(limit=1)
    assert len(runs) == 1
    assert runs[0]["error_message"] == huge_error


@pytest.mark.asyncio
async def test_check_telegram_health_chat_error(monkeypatch):
    """Verifies check_telegram_health handles chat lookup errors gracefully."""
    monkeypatch.setattr(settings, "telegram_bot_token", "123456789:ABCDefGhIjKlMnOpQrStUvWxYz")
    monkeypatch.setattr(settings, "telegram_channel_id", "@nonexistent_channel")

    mock_me = MagicMock()
    mock_me.id = 123456
    mock_me.username = "ai_viral_bot"

    mock_bot_instance = MagicMock()
    mock_bot_instance.get_me = AsyncMock(return_value=mock_me)
    mock_bot_instance.get_chat = AsyncMock(side_effect=Exception("TelegramNetworkError: Chat not found"))
    mock_bot_instance.session = MagicMock()
    mock_bot_instance.session.close = AsyncMock()

    with patch("aiogram.Bot", return_value=mock_bot_instance):
        res = await check_telegram_health()
        assert res["status"] == "warning"
        assert "@ai_viral_bot" in res["message"]
        assert "не найден" in res["message"]


@pytest.mark.asyncio
async def test_check_twitter_health_v11_settings(monkeypatch):
    """Verifies check_twitter_health uses lightweight v11.settings when available."""
    monkeypatch.setattr(settings, "twitter_auth_token", "valid_auth_token")
    monkeypatch.setattr(settings, "twitter_ct0", "valid_ct0")

    mock_v11 = MagicMock()
    mock_v11.settings = AsyncMock(return_value=({"screen_name": "ai_researcher"}, {}))

    mock_client = MagicMock()
    mock_client.v11 = mock_v11

    with patch("src.web.server.get_twitter_client", AsyncMock(return_value=mock_client)):
        res = await check_twitter_health()
        assert res["status"] == "ok"
        assert "@ai_researcher" in res["message"]


@pytest.mark.asyncio
async def test_check_twitter_health_placeholder_token(monkeypatch):
    """Verifies check_twitter_health rejects template placeholder tokens in .env."""
    monkeypatch.setattr(settings, "twitter_auth_token", "your_auth_token_here")
    monkeypatch.setattr(settings, "twitter_ct0", "your_ct0_here")

    with patch("src.web.server.load_cookies_from_file", return_value={}), \
         patch("pathlib.Path.is_file", return_value=False):
        res = await check_twitter_health()
        assert res["status"] == "error"
        assert "Сессия не настроена" in res["message"]


@pytest.mark.asyncio
async def test_check_telegram_health_missing_channel_with_bot(monkeypatch):
    """Verifies check_telegram_health reports bot name and channel missing warning."""
    monkeypatch.setattr(settings, "telegram_bot_token", "123456789:ABCDefGhIjKlMnOpQrStUvWxYz")
    monkeypatch.setattr(settings, "telegram_channel_id", "")

    mock_me = MagicMock()
    mock_me.id = 123456
    mock_me.username = "configured_bot"

    mock_bot_instance = MagicMock()
    mock_bot_instance.get_me = AsyncMock(return_value=mock_me)
    mock_bot_instance.session = MagicMock()
    mock_bot_instance.session.close = AsyncMock()

    with patch("aiogram.Bot", return_value=mock_bot_instance):
        res = await check_telegram_health()
        assert res["status"] == "warning"
        assert "@configured_bot" in res["message"]
        assert "TELEGRAM_CHANNEL_ID не задан" in res["message"]


@pytest.mark.asyncio
async def test_check_telegram_health_admin_no_post_rights(monkeypatch):
    """Verifies check_telegram_health warns if bot is admin but lacks can_post_messages rights."""
    monkeypatch.setattr(settings, "telegram_bot_token", "123456789:ABCDefGhIjKlMnOpQrStUvWxYz")
    monkeypatch.setattr(settings, "telegram_channel_id", "@test_channel")

    mock_me = MagicMock()
    mock_me.id = 123456
    mock_me.username = "ai_viral_bot"

    mock_chat = MagicMock()
    mock_chat.title = "Test Channel"

    mock_admin = MagicMock()
    mock_admin.user = MagicMock()
    mock_admin.user.id = 123456
    mock_admin.status = "administrator"
    mock_admin.can_post_messages = False

    mock_bot_instance = MagicMock()
    mock_bot_instance.get_me = AsyncMock(return_value=mock_me)
    mock_bot_instance.get_chat = AsyncMock(return_value=mock_chat)
    mock_bot_instance.get_chat_administrators = AsyncMock(return_value=[mock_admin])
    mock_bot_instance.session = MagicMock()
    mock_bot_instance.session.close = AsyncMock()

    with patch("aiogram.Bot", return_value=mock_bot_instance):
        res = await check_telegram_health()
        assert res["status"] == "warning"
        assert "@ai_viral_bot" in res["message"]
        assert "не имеет права на публикацию" in res["message"]


@pytest.mark.asyncio
async def test_db_get_recent_pipeline_runs_clamping(test_db):
    """Verifies get_recent_pipeline_runs clamps negative or zero limit to default 10."""
    await init_db()
    await record_pipeline_run(
        started_at="2026-09-14T01:00:00Z",
        finished_at="2026-09-14T01:00:05Z",
        duration_sec=5.0,
        tweets_found=1,
        viral_found=1,
        status="SUCCESS",
    )
    runs_neg = await get_recent_pipeline_runs(limit=-5)
    assert len(runs_neg) == 1
    runs_zero = await get_recent_pipeline_runs(limit=0)
    assert len(runs_zero) == 1


@pytest.mark.asyncio
async def test_ai_status_pending_and_promotion(monkeypatch):
    """Verifies AI status begins in pending state and is promoted to ok on health check."""
    from src.ai.agents.editor import get_ai_status, set_ai_status
    set_ai_status("pending", "Проверка состояния...")
    stat = get_ai_status()
    assert stat["status"] == "pending"

    monkeypatch.setattr(settings, "ai_api_key", "AIzaSyTestKey1234567")
    monkeypatch.setattr(settings, "ai_model_name", "gemini-2.5-flash")

    res = await check_gemini_health()
    assert res["status"] == "ok"
    assert "готов" in res["message"]
    # Internal status should now be promoted to ok
    assert get_ai_status()["status"] == "ok"


@pytest.mark.asyncio
async def test_save_system_settings_dual_safeguards(tmp_path, monkeypatch):
    """Verifies save_system_settings_dual backs up target env and ignores masked secrets."""
    from src.db import save_system_settings_dual

    target_env = tmp_path / "isolated.env"
    target_env.write_text('EXISTING_SECRET="real_secret_value"\nAI_MODEL_NAME="gemini-2.5-flash"\n', encoding="utf-8")

    db_path = tmp_path / "test_safe.sqlite"
    monkeypatch.setattr(settings, "db_path", str(db_path))
    await init_db()

    # Dual write with masked secret - should NOT overwrite real secret
    await save_system_settings_dual(
        {
            "existing_secret": "real***alue",
            "ai_model_name": "gemini-custom",
        },
        env_path=str(target_env),
    )

    content = target_env.read_text(encoding="utf-8")
    assert 'EXISTING_SECRET="real_secret_value"' in content
    assert 'AI_MODEL_NAME="gemini-custom"' in content

    # Verify backup exists
    backup_file = tmp_path / "isolated.env.bak"
    assert backup_file.exists()

    # Dual write with template placeholder - should NOT overwrite real secret
    await save_system_settings_dual(
        {
            "existing_secret": "your_placeholder_secret_here",
            "ai_model_name": "gemini-updated",
        },
        env_path=str(target_env),
    )
    content2 = target_env.read_text(encoding="utf-8")
    assert 'EXISTING_SECRET="real_secret_value"' in content2
    assert 'AI_MODEL_NAME="gemini-updated"' in content2

    # Dual write with empty string - should NOT overwrite real secret
    await save_system_settings_dual(
        {
            "existing_secret": "",
        },
        env_path=str(target_env),
    )
    content3 = target_env.read_text(encoding="utf-8")
    assert 'EXISTING_SECRET="real_secret_value"' in content3


@pytest.mark.asyncio
async def test_save_system_settings_dual_protects_root_env(monkeypatch):
    """Verifies save_system_settings_dual refuses to write to repository root .env during tests."""
    from pathlib import Path
    from src.db import save_system_settings_dual

    root_env = Path(__file__).resolve().parent.parent / ".env"
    orig_mtime = root_env.stat().st_mtime if root_env.exists() else None

    # Attempt to write to root .env directly
    await save_system_settings_dual(
        {"ai_model_name": "malicious-test-model"},
        env_path=str(root_env),
    )

    if root_env.exists() and orig_mtime is not None:
        assert root_env.stat().st_mtime == orig_mtime




