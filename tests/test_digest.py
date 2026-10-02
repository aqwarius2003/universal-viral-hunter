import json
import pytest
from datetime import datetime, timezone, timedelta
from src.ai.schema import DailyDigestSummary, DigestHighlightItem
from src.digest import format_digest_post, run_daily_digest, run_morning_digest_parts, run_evening_digest_parts


def test_format_digest_post_with_hyperlinks():
    digest_summary = DailyDigestSummary(
        main_headline_ru="Anthropic представила Claude 3.7 Sonnet",
        main_story_summary_ru="Новая гибридная модель сочетает мгновенные ответы и глубокое пошаговое рассуждение.",
        main_story_url="https://x.com/anthropic/status/999",
        key_highlights=[
            DigestHighlightItem(
                title_ru="Интерфейс ChatGPT",
                summary_ru="OpenAI обновила боковую панель и поиск.",
                source_url="https://x.com/openai/status/888",
            ),
            DigestHighlightItem(
                title_ru="DeepSeek R1 Mobile",
                summary_ru="Выпущена легковесная версия для смартфонов.",
                source_url="https://x.com/deepseek/status/777",
            ),
        ],
        takeaways_ru=[
            "Гибридные модели становятся новым стандартом индустрии",
            "Фокус смещается на локальный запуск компактных рассуждающих сетей",
        ],
        hashtags=["#AIDigest", "#Claude", "#DeepSeek"],
    )

    formatted = format_digest_post(digest_summary, "13.09.2026")

    assert "🌟 <b>ДАЙДЖЕСТ AI-НОВОСТЕЙ ЗА СУТКИ (13.09.2026)</b>" in formatted
    assert '<a href="https://x.com/anthropic/status/999"><b>Anthropic представила Claude 3.7 Sonnet</b></a>' in formatted
    assert '<a href="https://x.com/openai/status/888"><b>Интерфейс ChatGPT</b></a>: OpenAI обновила боковую панель и поиск.' in formatted
    assert '<a href="https://x.com/deepseek/status/777"><b>DeepSeek R1 Mobile</b></a>: Выпущена легковесная версия для смартфонов.' in formatted
    assert "#AIDigest" in formatted


def test_format_digest_post_legacy_strings():
    digest_summary = DailyDigestSummary(
        main_headline_ru="Anthropic представила Claude 3.7 Sonnet",
        main_story_summary_ru="Новая гибридная модель сочетает мгновенные ответы и глубокое пошаговое рассуждение.",
        key_highlights_ru=[
            "OpenAI обновила интерфейс ChatGPT",
            "DeepSeek выпустила легковесную версию R1 для смартфонов",
            "Nvidia анонсировала новые чипы для инференса",
        ],
        takeaways_ru=[
            "Гибридные модели становятся новым стандартом индустрии",
            "Фокус смещается на локальный запуск компактных рассуждающих сетей",
        ],
        hashtags=["#AIDigest", "#Claude", "#DeepSeek", "#Nvidia"],
    )

    formatted = format_digest_post(digest_summary, "13.09.2026")

    assert "🌟 <b>ДАЙДЖЕСТ AI-НОВОСТЕЙ ЗА СУТКИ (13.09.2026)</b>" in formatted
    assert "🔥 <b>ГЛАВНАЯ ТЕМА:</b> <b>Anthropic представила Claude 3.7 Sonnet</b>" in formatted
    assert "• DeepSeek выпустила легковесную версию R1 для смартфонов" in formatted
    assert "• Гибридные модели становятся новым стандартом индустрии" in formatted
    assert "#AIDigest" in formatted


@pytest.mark.asyncio
async def test_run_daily_digest_flow(mocker):
    # Mock DB functions
    mocker.patch("src.digest.init_db", return_value=None)
    mocker.patch("src.digest.is_digest_posted", return_value=False)
    mock_mark_posted = mocker.patch("src.digest.mark_digest_posted", return_value=None)
    mock_send_post = mocker.patch("src.digest.send_post_to_channel", return_value=None)
    mock_update_status = mocker.patch("src.digest.update_tweet_status_and_score", return_value=None)

    now = datetime.now(timezone.utc)
    mock_tweets = [
        {
            "tweet_id": "101",
            "author_name": "AI Researcher",
            "author_handle": "@ai_res",
            "text_raw": "Major breakthrough in diffusion models architecture.",
            "tweet_url": "https://x.com/ai_res/status/101",
            "created_at": (now - timedelta(hours=5)).isoformat(),
            "likes_count": 800,
            "reposts_count": 200,
            "quotes_count": 30,
            "bookmarks_count": 120,
            "impressions_count": 30000,
            "viral_score": 85.0,
            "is_viral": True,
            "status": "POSTED",
            "summary_json": '{"summary_ru": "Прорыв в архитектуре диффузионных моделей."}',
        }
    ]
    mocker.patch("src.digest.get_tweets_last_24h", return_value=mock_tweets)
    mocker.patch("src.digest.refresh_tweet_metrics", return_value=[
        {
            "tweet_id": "101",
            "likes_count": 900,
            "reposts_count": 220,
            "quotes_count": 35,
            "bookmarks_count": 130,
            "impressions_count": 35000,
        }
    ])
    mocker.patch("src.digest.update_tweet_metrics", return_value=None)

    mock_digest_summary = DailyDigestSummary(
        main_headline_ru="Прорыв в диффузионных моделях",
        main_story_summary_ru="Исследователи показали новый алгоритм ускорения генерации в 10 раз.",
        main_story_url="https://x.com/ai_res/status/101",
        key_highlights=[
            DigestHighlightItem(
                title_ru="Ускорение генерации",
                summary_ru="Снижение потребления VRAM в 2 раза.",
                source_url="https://x.com/ai_res/status/101",
            )
        ],
        takeaways_ru=[
            "Генерация видео станет доступной на потребительских GPU",
            "Снижение задержки инференса в реальном времени",
        ],
        hashtags=["#AIDigest", "#Diffusion"],
    )
    mocker.patch("src.digest.generate_digest_with_gemini", return_value=mock_digest_summary)

    # Run in production mode
    res = await run_daily_digest(slot_id="2026-09-13_14:00", is_test=False)

    assert res is not None
    assert "Прорыв в диффузионных моделях" in res
    # Digest is sent without bottom button (tweet_url=None)
    mock_send_post.assert_called_once_with(res, tweet_url=None)
    mock_mark_posted.assert_called_once_with("2026-09-13_14:00", tweet_count=1)


@pytest.mark.asyncio
async def test_run_daily_digest_with_super_viral_post(mocker):
    """Verifies that an unposted tweet with viral_score >= super_viral_threshold gets posted standalone before digest."""
    mocker.patch("src.digest.init_db", return_value=None)
    mocker.patch("src.digest.is_digest_posted", return_value=False)
    mocker.patch("src.digest.mark_digest_posted", return_value=None)
    mock_send_post = mocker.patch("src.digest.send_post_to_channel", return_value=None)
    mock_send_photo = mocker.patch("src.digest.send_photo_to_channel", return_value=None)
    mock_update_status = mocker.patch("src.digest.update_tweet_status_and_score", return_value=None)

    now = datetime.now(timezone.utc)
    mock_tweets = [
        {
            "tweet_id": "super_1",
            "author_name": "Super AI Lab",
            "author_handle": "@super_ai",
            "text_raw": "GPT-5 official announcement and open weights release!",
            "tweet_url": "https://x.com/super_ai/status/super_1",
            "created_at": (now - timedelta(hours=2)).isoformat(),
            "likes_count": 50000,
            "reposts_count": 20000,
            "impressions_count": 2000000,
            "viral_score": 180.0,  # Super viral >= 100
            "is_viral": True,
            "status": "PROCESSED",
            "media_urls": ["https://pbs.twimg.com/media/super.jpg"],
            "summary_json": json.dumps({
                "title_ru": "Релиз GPT-5",
                "summary_ru": "Главный анонс года.",
                "takeaways": ["Открытые веса", "Рекордные бенчмарки"],
                "hashtags": ["#GPT5", "#AI"],
                "impact_score": 10,
                "selected_image_index": 0,
            }),
        },
        {
            "tweet_id": "normal_2",
            "author_name": "Tech Daily",
            "author_handle": "@tech_daily",
            "text_raw": "Smaller update on python libraries.",
            "tweet_url": "https://x.com/tech_daily/status/normal_2",
            "created_at": (now - timedelta(hours=6)).isoformat(),
            "likes_count": 500,
            "reposts_count": 100,
            "impressions_count": 20000,
            "viral_score": 80.0,
            "is_viral": True,
            "status": "PROCESSED",
            "media_urls": [],
            "summary_json": json.dumps({
                "title_ru": "Обновление библиотек",
                "summary_ru": "Пакет инструментов обновился.",
                "takeaways": ["Удобнее API"],
                "hashtags": ["#Python"],
                "impact_score": 6,
            }),
        },
    ]

    mocker.patch("src.digest.get_tweets_last_24h", return_value=mock_tweets)
    mocker.patch("src.digest.refresh_tweet_metrics", return_value=[])

    mock_digest_summary = DailyDigestSummary(
        main_headline_ru="Обновление библиотек",
        main_story_summary_ru="Пакет инструментов обновился для исследователей.",
        main_story_url="https://x.com/tech_daily/status/normal_2",
        key_highlights=[
            DigestHighlightItem(
                title_ru="Обновление API",
                summary_ru="Новые методы инференса.",
                source_url="https://x.com/tech_daily/status/normal_2",
            )
        ],
        takeaways_ru=["Улучшение стека разработки", "Стабилизация SDK"],
        hashtags=["#AIDigest", "#Python"],
    )
    mock_gen = mocker.patch("src.digest.generate_digest_with_gemini", return_value=mock_digest_summary)

    res = await run_daily_digest(slot_id="2026-09-13_21:00", is_test=False)

    # 1. Super viral post sent with photo & individual tweet URL
    mock_send_photo.assert_called_once()
    assert mock_send_photo.call_args[0][1] == "https://pbs.twimg.com/media/super.jpg"
    assert mock_send_photo.call_args[0][2] == "https://x.com/super_ai/status/super_1"

    # 2. Super viral tweet status updated to POSTED
    # It will be called twice: once for super_viral tweet immediately, and once in the digest loop for normal_2
    assert mock_update_status.call_count >= 1
    # Check that it was called with super_1 and status POSTED
    super_call = [call for call in mock_update_status.call_args_list if call.kwargs.get("tweet_id") == "super_1"][0]
    assert super_call.kwargs["status"] == "POSTED"

    # 3. Digest generated without the super viral tweet in its list (or remaining list)
    gen_call_tweets = mock_gen.call_args[0][0]
    assert len(gen_call_tweets) == 1
    assert gen_call_tweets[0]["tweet_id"] == "normal_2"

    # 4. Digest sent via send_post_to_channel without bottom button
    mock_send_post.assert_called_once_with(res, tweet_url=None)


@pytest.mark.asyncio
async def test_run_daily_digest_skips_when_already_posted(mocker):
    mocker.patch("src.digest.init_db", return_value=None)
    mocker.patch("src.digest.is_digest_posted", return_value=True)
    mock_send_post = mocker.patch("src.digest.send_post_to_channel", return_value=None)

    res = await run_daily_digest(is_test=False)
    assert res is None
    mock_send_post.assert_not_called()


@pytest.mark.asyncio
async def test_run_daily_digest_gemini_fallback(mocker):
    """Verifies that if Gemini fails, a structured fallback digest is created from raw tweets."""
    mocker.patch("src.digest.init_db", return_value=None)
    mocker.patch("src.digest.is_digest_posted", return_value=False)
    mocker.patch("src.digest.mark_digest_posted", return_value=None)
    mock_send_post = mocker.patch("src.digest.send_post_to_channel", return_value=None)

    now = datetime.now(timezone.utc)
    mock_tweets = [
        {
            "tweet_id": "fallback_1",
            "author_name": "AI Dev",
            "author_handle": "@aidev",
            "text_raw": "New model weights are now available for local execution.",
            "tweet_url": "https://x.com/aidev/status/fallback_1",
            "created_at": now.isoformat(),
            "likes_count": 300,
            "reposts_count": 80,
            "viral_score": 60.0,
            "status": "PROCESSED",
            "summary_json": json.dumps({"title_ru": "Локальные веса", "summary_ru": "Веса открыты для скачивания."}),
        }
    ]

    mocker.patch("src.digest.get_tweets_last_24h", return_value=mock_tweets)
    mocker.patch("src.digest.refresh_tweet_metrics", return_value=[])
    mocker.patch("src.digest.generate_digest_with_gemini", side_effect=RuntimeError("Gemini Quota Exceeded"))

    res = await run_daily_digest(slot_id="2026-09-13_fallback", is_test=False)
    assert res is not None
    assert "https://x.com/aidev/status/fallback_1" in res
    assert "Веса открыты для скачивания." in res
    mock_send_post.assert_called_once_with(res, tweet_url=None)


def test_normalize_schedule_times():
    from src.scheduler import normalize_schedule_times

    assert normalize_schedule_times("07:00, 14:00, 21:00") == ["07:00", "14:00", "21:00"]
    assert normalize_schedule_times("7:00, 14:0, 21:00") == ["07:00", "14:00", "21:00"]
    assert normalize_schedule_times("7:5, 14:30, invalid, 25:00") == ["07:05", "14:30"]
    assert normalize_schedule_times(" 09:30 , 18:45 ") == ["09:30", "18:45"]
    assert normalize_schedule_times("21:00") == ["21:00"]
    assert normalize_schedule_times("") == []
    assert normalize_schedule_times("invalid") == []


@pytest.mark.asyncio
async def test_run_morning_digest_parts_multi_part(mocker):
    """Verifies that morning digest generates blocks with header title '🌅 ДАЙДЖЕСТ «ПОКА ВЫ СПАЛИ»'."""
    mocker.patch("src.digest.init_db", return_value=None)
    mocker.patch("asyncio.sleep", return_value=None)
    mock_send = mocker.patch("src.digest.send_post_to_channel", return_value=None)
    mocker.patch("src.digest.update_tweet_status_and_score", return_value=None)

    tweets = [
        {
            "tweet_id": f"night_{i}",
            "author_name": f"Author {i}",
            "author_handle": f"@author_{i}",
            "text_raw": f"Night news content {i}",
            "tweet_url": f"https://x.com/author_{i}/status/night_{i}",
            "viral_score": 80.0 + i,
            "status": "PROCESSED",
            "summary_json": json.dumps({"title_ru": f"Заголовок {i}", "summary_ru": f"Содержание {i}"})
        }
        for i in range(12)  # 12 tweets -> 2 chunks of 6
    ]
    mocker.patch("src.digest.get_unposted_tweets", return_value=tweets)

    mock_summary = DailyDigestSummary(
        main_headline_ru="Ночная тема",
        main_story_summary_ru="Саммари ночного события",
        main_story_url="https://x.com/author_0/status/night_0",
        key_highlights=[
            DigestHighlightItem(title_ru="Новость", summary_ru="Детали", source_url="https://x.com/author_0/status/night_0")
        ],
        takeaways_ru=["Тезис 1", "Тезис 2"],
        hashtags=["#NightNews"]
    )
    mocker.patch("src.digest.generate_digest_with_gemini", return_value=mock_summary)

    published = await run_morning_digest_parts(is_test=False)
    assert len(published) == 2
    assert "🌅 ДАЙДЖЕСТ «ПОКА ВЫ СПАЛИ» (Часть 1 из 2)" in published[0]
    assert "🌅 ДАЙДЖЕСТ «ПОКА ВЫ СПАЛИ» (Часть 2 из 2)" in published[1]
    assert mock_send.call_count == 2


@pytest.mark.asyncio
async def test_run_evening_digest_parts_multi_part(mocker):
    """Verifies that evening digest generates blocks with header title '🌙 ВЕЧЕРНИЙ ДАЙДЖЕСТ'."""
    mocker.patch("src.digest.init_db", return_value=None)
    mock_send = mocker.patch("src.digest.send_post_to_channel", return_value=None)
    mocker.patch("src.digest.update_tweet_status_and_score", return_value=None)

    tweets = [
        {
            "tweet_id": "day_1",
            "author_name": "Day Author",
            "author_handle": "@day_author",
            "text_raw": "Day news content",
            "tweet_url": "https://x.com/day_author/status/day_1",
            "viral_score": 90.0,
            "status": "PROCESSED",
            "summary_json": json.dumps({"title_ru": "Заголовок дня", "summary_ru": "Содержание дня"})
        }
    ]
    mocker.patch("src.digest.get_unposted_tweets", return_value=tweets)

    mock_summary = DailyDigestSummary(
        main_headline_ru="Главная дневная тема",
        main_story_summary_ru="Обзор дня",
        main_story_url="https://x.com/day_author/status/day_1",
        takeaways_ru=["Итог дня 1", "Итог дня 2"],
        hashtags=["#EveningDigest"]
    )
    mocker.patch("src.digest.generate_digest_with_gemini", return_value=mock_summary)

    published = await run_evening_digest_parts(is_test=False)
    assert len(published) == 1
    assert "🌙 ВЕЧЕРНИЙ ДАЙДЖЕСТ" in published[0]
    mock_send.assert_called_once()


@pytest.mark.asyncio
async def test_publish_single_top_post(mocker):
    """Verifies that publish_single_top_post picks the top queued tweet and publishes it."""
    mock_send_photo = mocker.patch("src.ai.agents.publisher.send_photo_to_channel", return_value=None)
    mocker.patch("src.ai.agents.publisher.update_tweet_status_and_score", return_value=None)

    mock_queued = [
        {
            "tweet_id": "top_1",
            "author_name": "Top Author",
            "author_handle": "@top_author",
            "text_raw": "Super viral post",
            "tweet_url": "https://x.com/top_author/status/top_1",
            "viral_score": 150.0,
            "impact_score": 9,
            "status": "PROCESSED",
            "media_urls": ["https://pbs.twimg.com/media/top.jpg"],
            "summary_json": json.dumps({
                "title_ru": "Топ пост дня",
                "summary_ru": "Самая виральная новость.",
                "takeaways": ["Тезис 1"],
                "hashtags": ["#TopNews"],
                "impact_score": 9,
                "selected_image_index": 0,
            }),
        }
    ]
    mocker.patch("src.db.get_queued_posts", return_value=mock_queued)

    from src.ai.agents.publisher import publish_single_top_post
    res = await publish_single_top_post(is_test=False)

    assert res is not None
    assert res["tweet_id"] == "top_1"
    assert "Топ пост дня" in res["text"]
    mock_send_photo.assert_called_once()


@pytest.mark.asyncio
async def test_backlog_digest_disabled_setting(monkeypatch, mocker):
    """Verifies that run_morning_digest_parts returns [] when backlog_digest_enabled is False."""
    from src.config import settings
    mocker.patch("src.digest.init_db", return_value=None)
    monkeypatch.setattr(settings, "backlog_digest_enabled", False)

    res_morning = await run_morning_digest_parts()
    assert res_morning == []

    res_evening = await run_evening_digest_parts()
    assert res_evening == []


@pytest.mark.asyncio
async def test_publish_single_top_post_skips_future_scheduled(mocker):
    """Verifies that publish_single_top_post skips posts scheduled for the future and prioritizes due ones."""
    from src.ai.agents.publisher import publish_single_top_post
    now = datetime.now(timezone.utc)
    future_time = (now + timedelta(hours=3)).isoformat()
    past_due_time = (now - timedelta(minutes=10)).isoformat()

    # Two posts: #1 has higher viral score but is scheduled in future; #2 has lower score but is due now
    mock_queued = [
        {
            "tweet_id": "future_post",
            "author_name": "Future",
            "author_handle": "@future",
            "text_raw": "Future tweet",
            "tweet_url": "https://x.com/future",
            "viral_score": 200.0,
            "scheduled_at": future_time,
            "media_urls": [],
            "summary_json": '{"title_ru": "Будущее", "summary_ru": "Будущее", "takeaways": [], "hashtags": []}',
        },
        {
            "tweet_id": "due_post",
            "author_name": "Due",
            "author_handle": "@due",
            "text_raw": "Due tweet",
            "tweet_url": "https://x.com/due",
            "viral_score": 100.0,
            "scheduled_at": past_due_time,
            "media_urls": [],
            "summary_json": '{"title_ru": "Текущее", "summary_ru": "Текущее", "takeaways": [], "hashtags": []}',
        },
    ]
    mocker.patch("src.db.get_queued_posts", return_value=mock_queued)
    mocker.patch("src.ai.agents.publisher.send_post_to_channel", return_value=123)
    mocker.patch("src.ai.agents.publisher.update_tweet_status_and_score", return_value=None)

    res = await publish_single_top_post(is_test=False)
    assert res is not None
    # Must pick the due_post, NOT the future_post!
    assert res["tweet_id"] == "due_post"


