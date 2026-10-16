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
