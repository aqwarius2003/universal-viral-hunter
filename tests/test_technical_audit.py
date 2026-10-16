from src.utils.technical_audit import (
    balance_and_sanitize_html,
    format_hashtags,
    audit_and_prepare_telegram_content,
    TELEGRAM_MAX_CAPTION_LEN,
)


def test_html_balancer_unclosed_tags():
    raw = "<b>Заголовок поста<i>с курсивом"
    balanced = balance_and_sanitize_html(raw)
    assert balanced.endswith("</i></b>") or balanced.endswith("</b></i>")


def test_html_balancer_stray_brackets():
    raw = "Цена < 100$ и > 50$"
    balanced = balance_and_sanitize_html(raw)
    assert "&lt;" in balanced
    assert "&gt;" in balanced


def test_format_hashtags():
    tags = ["ai", "#tech", "video, viral", ""]
    formatted = format_hashtags(tags)
    for t in formatted:
        assert t.startswith("#")
    assert "#ai" in formatted
    assert "#tech" in formatted
    assert "#video" in formatted
    assert "#viral" in formatted


def test_audit_within_caption_limit():
    res = audit_and_prepare_telegram_content(
        title="Тестовый заголовок",
        summary="Краткая суть новости.",
        takeaways=["Тезис 1", "Тезис 2"],
        hashtags=["#Новости", "#ИИ"],
        is_media=True,
    )
    assert res["is_valid"] is True
    assert res["char_count"] <= TELEGRAM_MAX_CAPTION_LEN
    assert "<b>Тестовый заголовок</b>" in res["formatted_text"]


def test_audit_over_limit_adaptive_compaction():
    # Long text that exceeds 1024 characters
    long_summary = "Очень длинный текст новости. " * 50
    takeaways = ["Длинный тезис 1 " * 10, "Длинный тезис 2 " * 10, "Длинный тезис 3 " * 10]
    hashtags = ["#Тег1", "#Тег2", "#Тег3", "#Тег4", "#Тег5"]

    res = audit_and_prepare_telegram_content(
        title="Заголовок длинного поста",
        summary=long_summary,
        takeaways=takeaways,
        hashtags=hashtags,
        is_media=True,
    )
    assert res["is_valid"] is True
    # MUST strictly fit within Telegram caption limit
    assert res["char_count"] <= TELEGRAM_MAX_CAPTION_LEN
    assert len(res["warnings"]) > 0


def test_normalize_channel_id():
    from src.telegram.sender import normalize_channel_id

    # User personal chat ID (must remain positive int)
    assert normalize_channel_id("581294821") == 581294821
    assert normalize_channel_id(581294821) == 581294821

    # Channel ID with 100 prefix and >= 12 digits (missing minus)
    assert normalize_channel_id("1001234567890") == -1001234567890

    # Negative channel ID
    assert normalize_channel_id("-1001234567890") == -1001234567890
    assert normalize_channel_id(-1001234567890) == -1001234567890

    # Public username
    assert normalize_channel_id("@my_tech_channel") == "@my_tech_channel"
    assert normalize_channel_id("my_tech_channel") == "@my_tech_channel"


def test_audit_omits_takeaways_when_disabled(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "show_metric_takeaways", False)

    res = audit_and_prepare_telegram_content(
        title="💥 РАЗМОТАЛО В ХЛАМ: лобовое на бешеной скорости",
        summary="Агрессивное описание происшествия без морализаторства.",
        takeaways=["Тезис 1", "Тезис 2"],
        hashtags=["#Авария"],
        is_media=True,
    )
    assert res["is_valid"] is True
    assert "Тезисы" not in res["formatted_text"]
    assert "<b>💥 РАЗМОТАЛО В ХЛАМ: лобовое на бешеной скорости</b>" in res["formatted_text"]


def test_publisher_formats_without_takeaways_and_no_80char_cut(monkeypatch):
    from src.config import settings
    from src.ai.agents.publisher import format_telegram_post

    monkeypatch.setattr(settings, "show_metric_takeaways", False)
    monkeypatch.setattr(settings, "telegram_max_caption_len", 1024)

    long_title = "💥 ЧУДОВИЩНОЕ ЛОБОВОЕ НА БЕШЕНОЙ СКОРОСТИ: вылетел на встречку и разнес три машины в щепки"
    assert len(long_title) > 80  # Demonstrating title is longer than 80 chars

    post = format_telegram_post(
        tweet_data={"author_handle": "crash_hunter"},
        ai_data={
            "title_ru": long_title,
            "summary_ru": "Водитель не справился с управлением.",
            "takeaways": ["Тезис 1", "Тезис 2"],
            "hashtags": ["#ДТП"],
        },
        is_photo=True,
    )
    # 1. Full title must be preserved (not truncated to 80 chars)
    assert long_title in post
    # 2. Takeaways must not be present
    assert "Тезисы" not in post


