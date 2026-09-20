import json
import pytest
from src.utils.hashtags import (
    normalize_single_hashtag,
    normalize_hashtags,
    format_hashtags_line,
    ensure_all_hashtags_have_hash,
    enforce_hashtags_in_post,
    extract_trailing_tags_from_summary,
)
from src.ai.agents.publisher import format_telegram_post
from src.db import init_db, update_post_content, get_tweet_by_id, _deserialize_row


def test_normalize_single_hashtag():
    assert normalize_single_hashtag("AI") == "#AI"
    assert normalize_single_hashtag("#AI") == "#AI"
    assert normalize_single_hashtag("##AI") == "#AI"
    assert normalize_single_hashtag("  #Tech  ") == "#Tech"
    assert normalize_single_hashtag("Дипфэйк") == "#Дипфэйк"
    assert normalize_single_hashtag("кибер безопасность") == "#кибер_безопасность"
    assert normalize_single_hashtag("искусственный-интеллект") == "#искусственный_интеллект"
    assert normalize_single_hashtag("tech,") == "#tech"
    assert normalize_single_hashtag("#AI!") == "#AI"
    assert normalize_single_hashtag("") is None
    assert normalize_single_hashtag("   ") is None
    assert normalize_single_hashtag("#") is None
    assert normalize_single_hashtag("###") is None
    assert normalize_single_hashtag(None) is None


def test_normalize_hashtags_list_without_hash():
    input_tags = ["Дипфэйк", "Кибербезопасность", "Мошенничество", "ИИ"]
    expected = ["#Дипфэйк", "#Кибербезопасность", "#Мошенничество", "#ИИ"]
    assert normalize_hashtags(input_tags) == expected


def test_normalize_hashtags_string():
    input_str = "Дипфэйк Кибербезопасность Мошенничество ИИ"
    expected = ["#Дипфэйк", "#Кибербезопасность", "#Мошенничество", "#ИИ"]
    assert normalize_hashtags(input_str) == expected

    hash_str = "#AI #MachineLearning #DeepSeek"
    assert normalize_hashtags(hash_str) == ["#AI", "#MachineLearning", "#DeepSeek"]


def test_normalize_hashtags_delimiters_and_semicolons():
    # Comma without spaces inside list item
    assert normalize_hashtags(["AI,Tech"]) == ["#AI", "#Tech"]
    # Semicolon delimiters
    assert normalize_hashtags(["AI;Tech"]) == ["#AI", "#Tech"]
    assert normalize_hashtags("AI; Tech, ML") == ["#AI", "#Tech", "#ML"]
    assert normalize_hashtags("Дипфэйк,Кибербезопасность,ИИ") == ["#Дипфэйк", "#Кибербезопасность", "#ИИ"]


def test_normalize_hashtags_concatenated_and_multiword():
    # Concatenated hashes
    assert normalize_hashtags(["#AI#Tech#News"]) == ["#AI", "#Tech", "#News"]
    # Multiple hashtags inside a single list element
    assert normalize_hashtags(["#Дипфэйк #Кибербезопасность #Мошенничество #ИИ"]) == [
        "#Дипфэйк",
        "#Кибербезопасность",
        "#Мошенничество",
        "#ИИ",
    ]
    # Space-separated string of words without #
    assert normalize_hashtags("Дипфэйк Кибербезопасность Мошенничество ИИ") == [
        "#Дипфэйк",
        "#Кибербезопасность",
        "#Мошенничество",
        "#ИИ",
    ]




def test_normalize_hashtags_internal_spaces_and_hyphens():
    tags = ["кибер безопасность", "neural-networks", "big data analysis"]
    expected = ["#кибер_безопасность", "#neural_networks", "#big_data_analysis"]
    assert normalize_hashtags(tags) == expected


def test_normalize_hashtags_deduplication():
    tags = ["#AI", "ai", "AI", "#Tech", "tech"]
    assert normalize_hashtags(tags) == ["#AI", "#Tech"]


def test_normalize_hashtags_empty_and_falsy():
    assert normalize_hashtags(None) == []
    assert normalize_hashtags([]) == []
    assert normalize_hashtags("") == []
    assert normalize_hashtags(["", "   ", "#", "##"]) == []


def test_format_hashtags_line():
    tags = ["Дипфэйк", "Кибербезопасность", "<script>"]
    line = format_hashtags_line(tags)
    assert line == "#Дипфэйк #Кибербезопасность #script"

    truncated = format_hashtags_line(["AI", "Tech", "ML", "Data"], max_count=2)
    assert truncated == "#AI #Tech"


def test_format_telegram_post_normalizes_raw_hashtags():
    tweet_data = {
        "author_handle": "@tech_insider",
        "author_name": "Tech Insider",
        "reposts_count": 350,
        "likes_count": 1420,
        "impressions_count": 85200,
        "viral_score": 82.0,
    }
    # User's exact scenario: model returned tags without '#' and with spaces
    ai_data = {
        "title_ru": "Новые угрозы цифровой безопасности",
        "summary_ru": "Мошенники используют дипфэйки для обмана пользователей.",
        "takeaways": ["Рост числа атак", "Угроза биометрии", "Методы защиты"],
        "impact_score": 9,
        "hashtags": ["Дипфэйк", "Кибербезопасность", "Мошенничество", "ИИ"],
    }

    formatted = format_telegram_post(tweet_data, ai_data, is_photo=False)
    assert "#Дипфэйк #Кибербезопасность #Мошенничество #ИИ" in formatted
    assert "Дипфэйк Кибербезопасность" not in formatted.replace("#Дипфэйк #Кибербезопасность", "")

    # Photo post
    photo_formatted = format_telegram_post(tweet_data, ai_data, is_photo=True)
    assert "#Дипфэйк #Кибербезопасность #Мошенничество" in photo_formatted


@pytest.mark.asyncio
async def test_db_deserialize_and_update_normalizes_hashtags(tmp_path, monkeypatch):
    test_db = str(tmp_path / "test_hunter.db")
    from src.config import settings
    monkeypatch.setattr(settings, "db_path", test_db)
    await init_db()

    # Test _deserialize_row auto-normalization for legacy DB data
    legacy_row = {
        "tweet_id": "test_legacy_1",
        "media_urls": "[]",
        "summary_json": json.dumps({
            "title_ru": "Заголовок",
            "summary_ru": "Текст",
            "hashtags": ["Дипфэйк", "Кибербезопасность"]
        }),
    }
    deserialized = _deserialize_row(legacy_row)
    assert deserialized["summary_data"]["hashtags"] == ["#Дипфэйк", "#Кибербезопасность"]


def test_format_telegram_post_deepseek_string_and_list():
    """
    Verifies that format_telegram_post properly normalizes both:
    1. Exact string: "DeepSeek ИИ Программирование OpenSource Технологии"
    2. Exact list: ["DeepSeek", "ИИ", "Программирование", "OpenSource", "Технологии"]
    3. Single string in list: ["DeepSeek ИИ Программирование OpenSource Технологии"]
    4. Dict: {"hashtags": ["DeepSeek", "ИИ", "Программирование", "OpenSource", "Технологии"]}
    Guarantees output in all cases contains:
    #DeepSeek #ИИ #Программирование #OpenSource #Технологии
    """
    tweet_data = {
        "author_handle": "@deepseek_ai",
        "author_name": "DeepSeek",
        "reposts_count": 500,
        "likes_count": 3000,
        "impressions_count": 120000,
        "viral_score": 95.0,
    }
    expected_hashtags = "#DeepSeek #ИИ #Программирование #OpenSource #Технологии"

    # Case 1: hashtags as exact space-separated string without '#'
    ai_data_string = {
        "title_ru": "Релиз новой модели DeepSeek",
        "summary_ru": "DeepSeek выпустила новую модель с открытыми весами.",
        "takeaways": ["Высокая скорость", "Открытый код"],
        "impact_score": 10,
        "hashtags": "DeepSeek ИИ Программирование OpenSource Технологии",
    }
    res_str = format_telegram_post(tweet_data, ai_data_string, is_photo=False)
    assert expected_hashtags in res_str
    # Ensure no unhashed occurrences of the tags block
    assert "DeepSeek ИИ Программирование OpenSource Технологии" not in res_str.replace(expected_hashtags, "")

    # Case 2: hashtags as exact list of unhashed strings
    ai_data_list = {
        "title_ru": "Релиз новой модели DeepSeek",
        "summary_ru": "DeepSeek выпустила новую модель с открытыми весами.",
        "takeaways": ["Высокая скорость", "Открытый код"],
        "impact_score": 10,
        "hashtags": ["DeepSeek", "ИИ", "Программирование", "OpenSource", "Технологии"],
    }
    res_list = format_telegram_post(tweet_data, ai_data_list, is_photo=False)
    assert expected_hashtags in res_list
    assert "DeepSeek ИИ Программирование OpenSource Технологии" not in res_list.replace(expected_hashtags, "")

    # Case 3: single-element list containing the space-separated string
    ai_data_single_item_list = {
        "title_ru": "Релиз новой модели DeepSeek",
        "summary_ru": "DeepSeek выпустила новую модель с открытыми весами.",
        "takeaways": ["Высокая скорость", "Открытый код"],
        "impact_score": 10,
        "hashtags": ["DeepSeek ИИ Программирование OpenSource Технологии"],
    }
    res_single = format_telegram_post(tweet_data, ai_data_single_item_list, is_photo=False)
    assert expected_hashtags in res_single

    # Case 4: hashtags passed as dictionary
    ai_data_dict = {
        "title_ru": "Релиз новой модели DeepSeek",
        "summary_ru": "DeepSeek выпустила новую модель с открытыми весами.",
        "takeaways": ["Высокая скорость", "Открытый код"],
        "impact_score": 10,
        "hashtags": {"hashtags": ["DeepSeek", "ИИ", "Программирование", "OpenSource", "Технологии"]},
    }
    res_dict = format_telegram_post(tweet_data, ai_data_dict, is_photo=False)
    assert expected_hashtags in res_dict

    # Case 5: photo post compact hashtags
    photo_res = format_telegram_post(tweet_data, ai_data_string, is_photo=True)
    assert "#DeepSeek" in photo_res
    assert "#ИИ" in photo_res
    assert "#Программирование" in photo_res


def test_format_telegram_post_trailing_tags_in_summary():
    """
    Verifies that if summary_ru accidentally contains tags at the bottom,
    they are extracted, cleaned from summary_ru, and formatted with '#'.
    """
    tweet_data = {
        "author_handle": "@ai_news",
        "author_name": "AI News",
        "reposts_count": 100,
        "likes_count": 500,
        "impressions_count": 20000,
        "viral_score": 75.0,
    }
    ai_data = {
        "title_ru": "Заголовок новости",
        "summary_ru": "Основной текст саммари о технологии.\n\nDeepSeek ИИ Программирование OpenSource Технологии",
        "takeaways": ["Пункт 1"],
        "impact_score": 8,
        "hashtags": [],
    }
    post = format_telegram_post(tweet_data, ai_data, is_photo=False)
    assert "#DeepSeek #ИИ #Программирование #OpenSource #Технологии" in post
    # Ensure it's not present twice or unhashed
    assert "Основной текст саммари о технологии." in post
    assert "Основной текст саммари о технологии.\n\nDeepSeek" not in post


def test_ensure_all_hashtags_have_hash():
    assert ensure_all_hashtags_have_hash("DeepSeek ИИ Программирование OpenSource Технологии") == (
        "#DeepSeek #ИИ #Программирование #OpenSource #Технологии"
    )
    assert ensure_all_hashtags_have_hash("#DeepSeek ИИ #Программирование OpenSource #Технологии") == (
        "#DeepSeek #ИИ #Программирование #OpenSource #Технологии"
    )
    assert ensure_all_hashtags_have_hash("") == ""
    assert ensure_all_hashtags_have_hash("   ") == ""
    assert ensure_all_hashtags_have_hash(None) == ""


def test_enforce_hashtags_in_post():
    # Scenario: message ending with unhashed word list
    raw_msg = "<b>News</b>\n\nSome text.\n\nDeepSeek ИИ Программирование OpenSource Технологии"
    enforced = enforce_hashtags_in_post(raw_msg)
    assert enforced == "<b>News</b>\n\nSome text.\n\n#DeepSeek #ИИ #Программирование #OpenSource #Технологии"

    # Scenario: message ending with mixed hashtags
    mixed_msg = "<b>News</b>\n\nSome text.\n\n#DeepSeek ИИ #Программирование OpenSource"
    enforced_mixed = enforce_hashtags_in_post(mixed_msg)
    assert enforced_mixed == "<b>News</b>\n\nSome text.\n\n#DeepSeek #ИИ #Программирование #OpenSource"

    # Scenario: normal sentence ending with period - should NOT be touched
    normal_msg = "<b>News</b>\n\nЭто отличная новость для разработчиков."
    assert enforce_hashtags_in_post(normal_msg) == normal_msg

    # Scenario: bullet points at the end - should NOT be touched
    bullet_msg = "<b>News</b>\n\n💡 Тезисы:\n• Первый пункт\n• Второй пункт"
    assert enforce_hashtags_in_post(bullet_msg) == bullet_msg


def test_format_telegram_post_metric_toggles(monkeypatch):
    from src.config import settings
    tweet_data = {
        "author_handle": "@tech_lead",
        "author_name": "Tech Lead",
        "reposts_count": 100,
        "likes_count": 500,
        "impressions_count": 25000,
        "viral_score": 88.0,
    }
    ai_data = {
        "title_ru": "Заголовок теста",
        "summary_ru": "Описание технологии.",
        "takeaways": ["Тезис раз"],
        "impact_score": 9,
        "hashtags": ["#AI", "#Tech"],
    }

    # 1. All enabled (default)
    monkeypatch.setattr(settings, "show_metric_likes", True)
    monkeypatch.setattr(settings, "show_metric_reposts", True)
    monkeypatch.setattr(settings, "show_metric_views", True)
    monkeypatch.setattr(settings, "show_metric_impact", True)
    monkeypatch.setattr(settings, "show_metric_author", True)
    monkeypatch.setattr(settings, "show_metric_hashtags", True)

    full_post = format_telegram_post(tweet_data, ai_data)
    assert "𝕏" in full_post
    assert "👁 25K" in full_post
    assert "🔁 100" in full_post
    assert "❤️ 500" in full_post
    assert "⚡ 88.0 · ⭐ 9/10" in full_post
    assert "#AI #Tech" in full_post

    # 2. Disable likes & reposts
    monkeypatch.setattr(settings, "show_metric_likes", False)
    monkeypatch.setattr(settings, "show_metric_reposts", False)
    post_no_likes = format_telegram_post(tweet_data, ai_data)
    assert "❤️" not in post_no_likes
    assert "🔁" not in post_no_likes
    assert "👁 25K" in post_no_likes

    # 3. Disable all engagement metrics
    monkeypatch.setattr(settings, "show_metric_views", False)
    monkeypatch.setattr(settings, "show_metric_impact", False)
    post_no_metrics = format_telegram_post(tweet_data, ai_data)
    assert "📊" not in post_no_metrics

    # 4. Disable author & hashtags
    monkeypatch.setattr(settings, "show_metric_author", False)
    monkeypatch.setattr(settings, "show_metric_hashtags", False)
    minimal_post = format_telegram_post(tweet_data, ai_data)
    assert "𝕏" not in minimal_post
    assert "#AI" not in minimal_post
    assert "#Tech" not in minimal_post
    assert "<b>Заголовок теста</b>" in minimal_post
    assert "Описание технологии." in minimal_post


@pytest.mark.asyncio
async def test_send_post_to_channel_respects_source_btn(monkeypatch, mocker):
    from src.config import settings
    from src.telegram.sender import send_post_to_channel

    monkeypatch.setattr(settings, "telegram_bot_token", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
    monkeypatch.setattr(settings, "telegram_channel_id", "@test_channel")

    mock_send = mocker.patch("aiogram.Bot.send_message")
    mock_sent_msg = mocker.MagicMock()
    mock_sent_msg.message_id = 777
    mock_send.return_value = mock_sent_msg

    # Case 1: show_metric_source_btn is False
    monkeypatch.setattr(settings, "show_metric_source_btn", False)
    res_id = await send_post_to_channel("Test text", tweet_url="https://x.com/post/1")
    assert res_id == 777
    assert mock_send.call_args[1]["reply_markup"] is None

    # Case 2: show_metric_source_btn is True
    monkeypatch.setattr(settings, "show_metric_source_btn", True)
    res_id2 = await send_post_to_channel("Test text", tweet_url="https://x.com/post/1")
    assert res_id2 == 777
    assert mock_send.call_args[1]["reply_markup"] is not None

