import pytest
from src.ai.agents.publisher import publisher_node, format_telegram_post
from src.config import settings

def test_format_telegram_post_author_link():
    tweet_data = {
        "author_handle": "@sama",
        "author_name": "Sam Altman",
        "reposts_count": 1200,
        "likes_count": 8500,
        "impressions_count": 250000,
        "viral_score": 88.5,
    }
    ai_data = {
        "title_ru": "Анонс новой модели",
        "summary_ru": "OpenAI представила обновленную версию модели.",
        "takeaways": ["Быстрее", "Дешевле", "Умнее"],
        "impact_score": 9,
        "hashtags": ["#OpenAI", "#GPT", "#AI"],
    }

    formatted = format_telegram_post(tweet_data, ai_data)

    # Check safe HTML link to x.com
    assert '<a href="https://x.com/sama">Sam Altman</a>' in formatted
    assert "<b>Анонс новой модели</b>" in formatted
    assert "⚡ 88.5 · ⭐ 9/10" in formatted


def test_format_telegram_post_html_escaping():
    tweet_data = {
        "author_handle": "@dev_team",
        "author_name": "AI <Innovators> & Co",
        "reposts_count": 100,
        "likes_count": 500,
        "impressions_count": 10000,
        "viral_score": 80.0,
    }
    ai_data = {
        "title_ru": "Тест тегов <script> & 'alert'",
        "summary_ru": "Модель A > Модель B в тесте 1 < 2.",
        "takeaways": ["Тезис 1 & 2", "Тезис <3>", "Тезис 4"],
        "impact_score": 8,
        "hashtags": ["#AI", "#Test"],
    }

    formatted = format_telegram_post(tweet_data, ai_data)

    assert "AI &lt;Innovators&gt; &amp; Co" in formatted
    assert "Тест тегов &lt;script&gt; &amp; &#x27;alert&#x27;" in formatted
    assert "Модель A &gt; Модель B в тесте 1 &lt; 2." in formatted
    assert "• Тезис 1 &amp; 2" in formatted


@pytest.mark.asyncio
async def test_publisher_node_top_n_batch_limit_and_photos(mocker):
    # Mock send_post_to_channel and send_photo_to_channel
    mock_send_post = mocker.patch("src.ai.agents.publisher.send_post_to_channel", return_value=None)
    mock_send_photo = mocker.patch("src.ai.agents.publisher.send_photo_to_channel", return_value=None)
    mock_db_update = mocker.patch("src.ai.agents.publisher.update_tweet_status_and_score", return_value=None)

    # Set limit to 1 post per run and simulate daytime (quiet hours inactive)
    mocker.patch.object(settings, "max_posts_per_run", 1)
    mocker.patch("src.ai.agents.publisher.is_in_quiet_hours", return_value=False)

    mock_state = {
        "draft_posts": [
            {
                "tweet_id": "top_post_with_photo",
                "original_data": {
                    "tweet_url": "https://x.com/user1/status/top_post_with_photo",
                    "author_handle": "@user1",
                    "author_name": "User One",
                    "viral_score": 95.0,
                    "likes_count": 5000,
                    "reposts_count": 1000,
                    "impressions_count": 100000,
                    "media_urls": ["https://pbs.twimg.com/media/img0.jpg", "https://pbs.twimg.com/media/img1.jpg"],
                },
                "ai_data": {
                    "title_ru": "Главный прорыв",
                    "summary_ru": "Описание прорыва.",
                    "takeaways": ["1", "2", "3"],
                    "impact_score": 10,
                    "hashtags": ["#AI"],
                    "selected_image_index": 1,
                },
                "combined_score": 950.0,
            },
            {
                "tweet_id": "second_post_no_photo",
                "original_data": {
                    "tweet_url": "https://x.com/user2/status/second_post_no_photo",
                    "author_handle": "@user2",
                    "author_name": "User Two",
                    "viral_score": 85.0,
                    "likes_count": 2000,
                    "reposts_count": 500,
                    "impressions_count": 50000,
                    "media_urls": [],
                },
                "ai_data": {
                    "title_ru": "Второй пост",
                    "summary_ru": "Описание второго поста.",
                    "takeaways": ["1", "2", "3"],
                    "impact_score": 8,
                    "hashtags": ["#AI"],
                    "selected_image_index": None,
                },
                "combined_score": 680.0,
            }
        ]
    }

    result = await publisher_node(mock_state)

    # Assert only 1 post published due to max_posts_per_run = 1
    assert len(result["ready_to_publish"]) == 1
    published = result["ready_to_publish"][0]
    assert published["tweet_url"] == "https://x.com/user1/status/top_post_with_photo"
    assert published["photo_url"] == "https://pbs.twimg.com/media/img1.jpg"

    # Photo sender was called for top post
    mock_send_photo.assert_called_once()
    mock_send_post.assert_not_called()

    # DB updates: 1 POSTED, 1 PROCESSED
    assert mock_db_update.call_count == 2
    # First call marked as POSTED
    first_call = mock_db_update.call_args_list[0]
    assert first_call.kwargs["tweet_id"] == "top_post_with_photo"
    assert first_call.kwargs["status"] == "POSTED"

    # Second call marked as PROCESSED
    second_call = mock_db_update.call_args_list[1]
    assert second_call.kwargs["tweet_id"] == "second_post_no_photo"
    assert second_call.kwargs["status"] == "PROCESSED"


@pytest.mark.asyncio
async def test_publisher_node_quiet_hours_skips_posting(mocker):
    # Mock is_in_quiet_hours to return True
    mocker.patch("src.ai.agents.publisher.is_in_quiet_hours", return_value=True)
    mock_send_post = mocker.patch("src.ai.agents.publisher.send_post_to_channel", return_value=None)
    mock_send_photo = mocker.patch("src.ai.agents.publisher.send_photo_to_channel", return_value=None)
    mock_db_update = mocker.patch("src.ai.agents.publisher.update_tweet_status_and_score", return_value=None)

    mock_state = {
        "draft_posts": [
            {
                "tweet_id": "night_draft_1",
                "original_data": {
                    "tweet_url": "https://x.com/user1/status/night_draft_1",
                    "author_handle": "@user1",
                    "author_name": "User One",
                    "viral_score": 95.0,
                    "likes_count": 5000,
                    "reposts_count": 1000,
                    "impressions_count": 100000,
                    "media_urls": [],
                },
                "ai_data": {
                    "title_ru": "Ночной пост",
                    "summary_ru": "Суть ночного поста.",
                    "takeaways": ["1", "2"],
                    "impact_score": 9,
                    "hashtags": ["#Night"],
                    "selected_image_index": None,
                },
            }
        ]
    }

    result = await publisher_node(mock_state)

    # Nothing should be published directly during quiet hours
    assert result["ready_to_publish"] == []
    mock_send_post.assert_not_called()
    mock_send_photo.assert_not_called()

    # Draft should be saved as PROCESSED in DB
    mock_db_update.assert_called_once()
    assert mock_db_update.call_args.kwargs["tweet_id"] == "night_draft_1"
    assert mock_db_update.call_args.kwargs["status"] == "PROCESSED"



def test_format_telegram_post_photo_compact_length():
    """Ensures photo posts are formatted compactly and fit within 1024 characters."""
    tweet_data = {
        "author_handle": "@ai_researcher",
        "author_name": "Dr. AI Researcher",
        "reposts_count": 5000,
        "likes_count": 25000,
        "impressions_count": 1000000,
        "viral_score": 99.0,
    }
    # Create very long text that would exceed 1024 chars in standard mode
    long_summary = "Это очень подробный разбор новой нейросетевой архитектуры, которая меняет правила игры. " * 20
    ai_data = {
        "title_ru": "Глобальный прорыв в архитектуре трансформеров от ведущей исследовательской лаборатории мира",
        "summary_ru": long_summary,
        "takeaways": [
            "Тезис номер один с длинным объяснением контекста",
            "Тезис номер два с деталями обучения модели",
            "Тезис номер три с результатами бенчмарков",
            "Тезис номер четыре",
            "Тезис номер пять",
        ],
        "impact_score": 10,
        "hashtags": ["#AI", "#DeepLearning", "#Transformers", "#Research", "#TechNews", "#Future"],
    }

    # Format for text (is_photo=False)
    text_formatted = format_telegram_post(tweet_data, ai_data, is_photo=False)
    assert len(text_formatted) > 1024

    # Format for photo (is_photo=True)
    photo_formatted = format_telegram_post(tweet_data, ai_data, is_photo=True)
    assert len(photo_formatted) <= 1024
    assert "<b>Глобальный прорыв" in photo_formatted
    assert "Dr. AI Researcher" in photo_formatted
    assert "..." in photo_formatted or "Тезисы:" in photo_formatted


def test_normalize_channel_id():
    from src.telegram.sender import normalize_channel_id

    assert normalize_channel_id("@my_channel") == "@my_channel"
    assert normalize_channel_id("my_channel") == "@my_channel"
    assert normalize_channel_id("  @news_ai  ") == "@news_ai"
    assert normalize_channel_id("-1001234567890") == -1001234567890
    assert normalize_channel_id("1001234567890") == -1001234567890

    with pytest.raises(ValueError):
        normalize_channel_id("")

    with pytest.raises(ValueError):
        normalize_channel_id(None)


def test_friendly_telegram_error():
    from src.telegram.sender import get_friendly_telegram_error

    err_chat = Exception("Telegram server says - Bad Request: chat not found")
    msg_chat = get_friendly_telegram_error(err_chat)
    assert "не найден" in msg_chat
    assert "администратором" in msg_chat

    err_auth = Exception("Telegram server says - Unauthorized")
    msg_auth = get_friendly_telegram_error(err_auth)
    assert "токен" in msg_auth.lower()


def test_format_telegram_post_photo_extreme_html():
    """Verify that even with extensive HTML entities and special characters, caption is <= 1024 chars."""
    tweet_data = {
        "author_handle": "@" + "long_author_" * 5,
        "author_name": "Special & <Chars> " * 5,
        "reposts_count": 9999,
        "likes_count": 88888,
        "impressions_count": 500000,
        "viral_score": 99.9,
    }
    ai_data = {
        "title_ru": "Заголовок с & < > \" ' знаками " * 5,
        "summary_ru": "Текст с & < > \" ' знаками " * 100,
        "takeaways": ["Тезис & < > 1", "Тезис & < > 2", "Тезис & < > 3"],
        "impact_score": 10,
        "hashtags": ["#AI", "#Test"],
    }
    photo_post = format_telegram_post(tweet_data, ai_data, is_photo=True)
    assert len(photo_post) <= 1024
    assert photo_post.endswith("...") or "#AI" in photo_post

