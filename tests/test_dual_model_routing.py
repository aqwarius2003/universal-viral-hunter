import asyncio
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.ai.rate_limiter import ModelRateLimiter, get_model_interval
from src.ai.agents.editor import (
    call_llm,
    editor_node,
    select_key_image,
    is_rate_limit_error,
    parse_json_safely,
    ClientError,
)
from src.config import Settings, settings


def make_client_error(code=429, message="RESOURCE_EXHAUSTED: Rate limit exceeded") -> Exception:
    """Helper to instantiate google.genai.errors.ClientError safely across versions."""
    try:
        return ClientError(code=code, response_json={"error": {"code": code, "message": message}})
    except TypeError:
        try:
            return ClientError(code, {"error": {"code": code, "message": message}})
        except Exception:
            err = ClientError(message)
            err.code = code
            return err


# --- 1. Model Configuration & Parsing Tests ---

def test_settings_default_models():
    # Pass _env_file=None so local root .env doesn't override class defaults
    s = Settings(_env_file=None)
    assert s.primary_model == "gemini-3.5-flash-lite"
    assert s.fallback_model == "gemini-3.1-flash-lite"


def test_settings_single_model_specified(monkeypatch):
    monkeypatch.setattr(settings, "ai_model_name", "gemini-2.5-pro")
    assert settings.primary_model == "gemini-2.5-pro"
    assert settings.fallback_model == "gemini-3.1-flash-lite"


def test_settings_comma_separated_models(monkeypatch):
    monkeypatch.setattr(settings, "ai_model_name", "my-text-model, my-vision-model")
    assert settings.primary_model == "my-text-model"
    assert settings.fallback_model == "my-vision-model"


def test_settings_empty_model_name(monkeypatch):
    monkeypatch.setattr(settings, "ai_model_name", "")
    assert settings.primary_model == "gemini-3.5-flash-lite"
    assert settings.fallback_model == "gemini-3.1-flash-lite"


def test_settings_whitespace_handling(monkeypatch):
    monkeypatch.setattr(settings, "ai_model_name", "  model-a  ,   model-b  ")
    assert settings.primary_model == "model-a"
    assert settings.fallback_model == "model-b"


# --- 2. Per-Model Rate Limiter Tests ---

@pytest.mark.asyncio
async def test_rate_limiter_enforces_interval_on_same_model():
    limiter = ModelRateLimiter(min_interval=4.0)

    # First call to model A should not sleep
    w1 = await limiter.acquire("gemini-3.5-flash-lite")
    assert w1 == 0.0

    # Simulate 1 second elapsed
    limiter._last_call["gemini-3.5-flash-lite"] = time.monotonic() - 1.0

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        w2 = await limiter.acquire("gemini-3.5-flash-lite")
        # Should need around 3 seconds of sleep (4.0 - 1.0)
        assert 2.8 <= w2 <= 3.2
        mock_sleep.assert_called_once()
        assert 2.8 <= mock_sleep.call_args[0][0] <= 3.2


@pytest.mark.asyncio
async def test_rate_limiter_separate_per_model():
    limiter = ModelRateLimiter(min_interval=4.0)

    # Acquire model A
    await limiter.acquire("gemini-3.5-flash-lite")

    # Immediately acquire model B — must NOT wait because model B was not used yet
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        w_b = await limiter.acquire("gemini-3.1-flash-lite")
        assert w_b == 0.0
        mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_rate_limiter_respects_custom_interval():
    limiter = ModelRateLimiter(min_interval=6.0)
    await limiter.acquire("test-model")
    limiter._last_call["test-model"] = time.monotonic() - 2.0

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        w = await limiter.acquire("test-model")
        assert 3.8 <= w <= 4.2
        mock_sleep.assert_called_once()


def test_get_model_interval_minimum_four_seconds(monkeypatch):
    monkeypatch.setattr(settings, "ai_request_delay_seconds", 1)
    assert get_model_interval() >= 4.0

    monkeypatch.setattr(settings, "ai_request_delay_seconds", 10)
    assert get_model_interval() == 10.0


# --- 3. Rate Limit Error Helper Tests ---

def test_is_rate_limit_error_detection():
    # Direct 429 code
    err_code = make_client_error(code=429, message="quota reached")
    assert is_rate_limit_error(err_code) is True

    # RESOURCE_EXHAUSTED in message
    err_msg = make_client_error(code=429, message="RESOURCE_EXHAUSTED: Rate limit exceeded")
    assert is_rate_limit_error(err_msg) is True

    # Generic 400 error
    err_400 = make_client_error(code=400, message="Invalid argument: bad parameter")
    assert is_rate_limit_error(err_400) is False


# --- 4. Multimodal Routing Tests ---

@pytest.mark.asyncio
async def test_single_image_bypasses_image_model():
    """When only 1 image is attached, no image model call is made, index 0 is returned directly."""
    mock_resp = MagicMock()
    mock_resp.text = json.dumps({
        "title_ru": "Тестовый заголовок",
        "summary_ru": "Тестовое описание",
        "takeaways": ["Т1", "Т2", "Т3"],
        "impact_score": 8,
        "impact_reason": "Причина",
        "hashtags": ["#AI"],
    })

    mock_generate = AsyncMock(return_value=mock_resp)

    with patch("src.ai.agents.editor.get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = mock_generate
        mock_get_client.return_value = mock_client

        res = await call_llm(
            text="Tweet with single image",
            media_urls=["https://pbs.twimg.com/media/single.jpg"]
        )

        assert res is not None
        assert res["selected_image_index"] == 0
        assert "Единственное" in res["image_reason"]

        # Only 1 call was made (to the text model gemini-3.5-flash-lite)
        assert mock_generate.call_count == 1
        assert mock_generate.call_args.kwargs["model"] == settings.primary_model
        # Text model contents must contain text only, no image Parts
        contents = mock_generate.call_args.kwargs["contents"]
        assert len(contents) == 1
        assert isinstance(contents[0], str)


@pytest.mark.asyncio
async def test_multiple_images_routed_isolated_to_fallback_model():
    """When multiple images are attached, key image selection is isolated to fallback model."""
    img_resp = MagicMock()
    img_resp.text = json.dumps({
        "selected_image_index": 1,
        "image_reason": "Архитектурная схема"
    })

    text_resp = MagicMock()
    text_resp.text = json.dumps({
        "title_ru": "Заголовок",
        "summary_ru": "Описание",
        "takeaways": ["1", "2", "3"],
        "impact_score": 9,
        "impact_reason": "Важно",
        "hashtags": ["#Tech"],
    })

    async def fake_generate(**kwargs):
        if kwargs.get("model") == settings.fallback_model:
            return img_resp
        return text_resp

    mock_generate = AsyncMock(side_effect=fake_generate)

    with patch("src.ai.agents.editor.get_client") as mock_get_client, \
         patch("src.ai.agents.editor.download_image", AsyncMock(return_value=(b"fakebytes", "image/jpeg"))):
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = mock_generate
        mock_get_client.return_value = mock_client

        res = await call_llm(
            text="Tweet with two images",
            media_urls=["https://pbs.twimg.com/media/1.jpg", "https://pbs.twimg.com/media/2.jpg"]
        )

        assert res is not None
        assert res["selected_image_index"] == 1
        assert res["image_reason"] == "Архитектурная схема"

        # Check call sequence:
        # First call was to fallback_model (gemini-3.1-flash-lite) for image selection
        # Second call was to primary_model (gemini-3.5-flash-lite) for text
        models_called = [call.kwargs["model"] for call in mock_generate.call_args_list]
        assert models_called == [settings.fallback_model, settings.primary_model]

        # Primary model received ONLY text prompt, preserving quota
        primary_contents = mock_generate.call_args_list[1].kwargs["contents"]
        assert len(primary_contents) == 1
        assert isinstance(primary_contents[0], str)


# --- 5. Fallback on HTTP 429 Tests ---

@pytest.mark.asyncio
async def test_call_llm_fallback_on_429_to_secondary_model():
    """If primary model returns HTTP 429, pauses 2s and succeeds on fallback model."""
    fallback_resp = MagicMock()
    fallback_resp.text = json.dumps({
        "title_ru": "Fallback Заголовок",
        "summary_ru": "Успешно через fallback модель.",
        "takeaways": ["Т1", "Т2", "Т3"],
        "impact_score": 7,
        "impact_reason": "Хорошо",
        "hashtags": ["#Fallback"],
    })

    async def fake_generate(**kwargs):
        if kwargs.get("model") == settings.primary_model:
            raise make_client_error(code=429, message="429 RESOURCE_EXHAUSTED: Rate limit exceeded")
        elif kwargs.get("model") == settings.fallback_model:
            return fallback_resp
        raise RuntimeError("Unexpected model")

    mock_generate = AsyncMock(side_effect=fake_generate)

    with patch("src.ai.agents.editor.get_client") as mock_get_client, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = mock_generate
        mock_get_client.return_value = mock_client

        res = await call_llm(text="Breaking AI News")

        assert res is not None
        assert res["title_ru"] == "Fallback Заголовок"

        # Verify models called in order: primary then fallback
        models_called = [call.kwargs["model"] for call in mock_generate.call_args_list]
        assert models_called == [settings.primary_model, settings.fallback_model]

        # Verify 2 seconds pause before fallback
        mock_sleep.assert_any_call(2)


@pytest.mark.asyncio
async def test_call_llm_both_models_429_returns_none():
    """If both primary and fallback return 429, logs warning and returns None."""
    async def fake_generate(**kwargs):
        raise make_client_error(code=429, message=f"429 RESOURCE_EXHAUSTED on {kwargs.get('model')}")

    mock_generate = AsyncMock(side_effect=fake_generate)

    with patch("src.ai.agents.editor.get_client") as mock_get_client, \
         patch("asyncio.sleep", new_callable=AsyncMock):
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = mock_generate
        mock_get_client.return_value = mock_client

        res = await call_llm(text="Unlucky tweet under heavy traffic")
        assert res is None


@pytest.mark.asyncio
async def test_call_llm_non_429_error_raises_immediately():
    """Non-429 ClientError (e.g. 400 Bad Request) must NOT trigger fallback, must re-raise."""
    async def fake_generate(**kwargs):
        raise make_client_error(code=400, message="400 Bad Request: Invalid schema")

    mock_generate = AsyncMock(side_effect=fake_generate)

    with patch("src.ai.agents.editor.get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = mock_generate
        mock_get_client.return_value = mock_client

        with pytest.raises(Exception) as exc_info:
            await call_llm(text="Invalid prompt tweet")

        assert "400" in str(exc_info.value)
        # Should only call primary model once, not fallback
        assert mock_generate.call_count == 1
        assert mock_generate.call_args.kwargs["model"] == settings.primary_model


# --- 6. Editor Node Skipping on Dual 429 (Fault Tolerance) ---

@pytest.mark.asyncio
async def test_editor_node_skips_post_on_dual_429_without_crashing():
    """Verifies that when call_llm returns None (dual 429), editor_node skips post and continues."""
    async def mock_call_llm(text, media_urls=None):
        if "QuotaExhausted" in text:
            return None  # Both models failed with 429
        return {
            "title_ru": "Успешный твит",
            "summary_ru": "Описание",
            "takeaways": ["1", "2", "3"],
            "impact_score": 8,
            "impact_reason": "Ок",
            "hashtags": ["#AI"],
            "selected_image_index": None,
            "image_reason": None,
        }

    mock_state = {
        "viral_tweets": [
            {"tweet_id": "failed_tweet", "text_raw": "QuotaExhausted tweet", "viral_score": 85.0},
            {"tweet_id": "success_tweet", "text_raw": "Normal tweet", "viral_score": 90.0},
        ],
        "draft_posts": []
    }

    with patch("src.ai.agents.editor.call_llm", side_effect=mock_call_llm), \
         patch("src.ai.agents.editor.update_tweet_status_and_score", AsyncMock()):
        result = await editor_node(mock_state)

    drafts = result.get("draft_posts", [])
    # The failed tweet was skipped without crash; success tweet was processed
    assert len(drafts) == 1
    assert drafts[0]["tweet_id"] == "success_tweet"


# --- 7. Deep Verification & Edge Case Tests ---

def test_settings_single_model_matches_fallback(monkeypatch):
    """When user specifies single model matching fallback default, fallback switches to primary default."""
    monkeypatch.setattr(settings, "ai_model_name", "gemini-3.1-flash-lite")
    assert settings.primary_model == "gemini-3.1-flash-lite"
    assert settings.fallback_model == "gemini-3.5-flash-lite"


@pytest.mark.asyncio
async def test_rate_limiter_normalizes_casing_and_whitespace():
    """Rate limiter normalizes model strings so whitespace/casing differences share the same lock and interval."""
    limiter = ModelRateLimiter(min_interval=4.0)
    await limiter.acquire("  gemini-3.5-flash-lite  ")

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        w = await limiter.acquire("GEMINI-3.5-FLASH-LITE")
        assert w > 0.0
        mock_sleep.assert_called_once()


def test_parse_json_safely_strips_markdown_fences():
    """parse_json_safely strips markdown code fences safely."""
    raw = "```json\n{\"title_ru\": \"Тест\", \"summary_ru\": \"Описание\"}\n```"
    data = parse_json_safely(raw)
    assert data["title_ru"] == "Тест"

    raw2 = "```\n{\"title_ru\": \"Тест2\"}\n```"
    data2 = parse_json_safely(raw2)
    assert data2["title_ru"] == "Тест2"


@pytest.mark.asyncio
async def test_server_generate_summary_handles_none_gracefully():
    """When call_llm returns None on dual 429, _generate_summary_for_tweet returns None without AttributeError."""
    from src.web.server import _generate_summary_for_tweet
    with patch("src.ai.agents.editor.call_llm", AsyncMock(return_value=None)):
        p = {"tweet_id": "test_none_1", "text_raw": "Tweet text", "status": "NEW"}
        res = await _generate_summary_for_tweet(p)
        assert res is None
        assert "summary_data" not in p


@pytest.mark.asyncio
async def test_generate_digest_with_gemini_fallback_on_429():
    """generate_digest_with_gemini falls back to secondary model on 429 after 2-second sleep."""
    from src.digest import generate_digest_with_gemini
    from src.ai.schema import DailyDigestSummary

    sample_tweets = [
        {
            "tweet_id": "1",
            "author_name": "Author",
            "author_handle": "author",
            "text_raw": "News 1",
            "viral_score": 50.0,
            "tweet_url": "https://x.com/a/1",
        }
    ]

    mock_digest_resp = MagicMock()
    mock_digest_resp.text = json.dumps({
        "main_headline_ru": "Заголовок дайджеста",
        "main_story_summary_ru": "Описание главной темы",
        "main_story_url": "https://x.com/a/1",
        "key_highlights": [
            {
                "title_ru": "Новость 1",
                "summary_ru": "Описание 1",
                "source_url": "https://x.com/a/1"
            }
        ],
        "takeaways_ru": ["Вывод 1", "Вывод 2"],
        "hashtags": ["#AIDigest", "#AI"]
    })

    async def fake_generate(**kwargs):
        if kwargs.get("model") == settings.primary_model:
            raise make_client_error(code=429, message="429 RESOURCE_EXHAUSTED on primary")
        elif kwargs.get("model") == settings.fallback_model:
            return mock_digest_resp
        raise RuntimeError("Unexpected model")

    mock_generate = AsyncMock(side_effect=fake_generate)

    with patch("src.digest.client") as mock_client, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_client.aio.models.generate_content = mock_generate

        digest = await generate_digest_with_gemini(sample_tweets)

        assert isinstance(digest, DailyDigestSummary)
        assert digest.main_headline_ru == "Заголовок дайджеста"
        # Primary called then fallback
        models_called = [call.kwargs["model"] for call in mock_generate.call_args_list]
        assert models_called == [settings.primary_model, settings.fallback_model]
        mock_sleep.assert_any_call(2)


@pytest.mark.asyncio
async def test_super_viral_fallback_when_call_llm_returns_none(mocker):
    """When call_llm returns None (dual 429) for super viral post, digest pipeline assigns fallback dict without crashing."""
    from src.digest import run_daily_digest
    from datetime import datetime, timezone

    mocker.patch("src.digest.init_db", return_value=None)
    mocker.patch("src.digest.is_digest_posted", return_value=False)
    mocker.patch("src.digest.mark_digest_posted", return_value=None)
    mocker.patch("src.digest.send_post_to_channel", return_value=None)
    mocker.patch("src.digest.send_photo_to_channel", return_value=None)
    mock_update_status = mocker.patch("src.digest.update_tweet_status_and_score", return_value=None)

    now = datetime.now(timezone.utc)
    mock_tweets = [
        {
            "tweet_id": "super_none",
            "author_name": "Super AI",
            "author_handle": "@super_ai",
            "text_raw": "Breaking breakthrough in AI models",
            "tweet_url": "https://x.com/super_ai/status/super_none",
            "created_at": now.isoformat(),
            "likes_count": 10000,
            "reposts_count": 5000,
            "impressions_count": 500000,
            "viral_score": 150.0,
            "is_viral": True,
            "status": "NEW",
            "media_urls": [],
            "summary_json": None,
        }
    ]

    mocker.patch("src.digest.get_tweets_last_24h", return_value=mock_tweets)
    mocker.patch("src.digest.refresh_tweet_metrics", return_value=[])
    # call_llm returns None (dual 429 quota exhaustion)
    mocker.patch("src.ai.agents.editor.call_llm", AsyncMock(return_value=None))

    from src.ai.schema import DailyDigestSummary
    mock_digest = DailyDigestSummary(
        main_headline_ru="Дайджест",
        main_story_summary_ru="Описание",
        takeaways_ru=["Вывод 1", "Вывод 2"],
        hashtags=["#AI", "#Digest"],
    )
    mocker.patch("src.digest.generate_digest_with_gemini", return_value=mock_digest)

    res = await run_daily_digest(slot_id="2026-09-14_test", is_test=False)
    assert res is not None
    mock_update_status.assert_called_once()
    # Verify impact_score was preserved with fallback default 10 instead of crashing
    assert mock_update_status.call_args.kwargs["impact_score"] == 10
    assert mock_update_status.call_args.kwargs["tweet_id"] == "super_none"


