import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.ai.agents.editor import editor_node, download_image, call_llm
from src.ai.schema import TweetSummary

def test_tweet_summary_schema_with_image():
    # Valid with image selected
    summary = TweetSummary(
        title_ru="Релиз новой открытой модели",
        summary_ru="Компания выпустила новую модель с открытыми весами. Она превосходит предыдущие аналоги.",
        takeaways=["Высокая скорость работы", "Открытые веса", "Поддержка длинного контекста"],
        impact_score=9,
        impact_reason="Крупный прорыв в открытых моделях",
        hashtags=["#AI", "#OpenSource", "#LLM"],
        selected_image_index=0,
        image_reason="На первой картинке представлен ключевой бенчмарк сравнения точности."
    )
    assert summary.selected_image_index == 0
    assert summary.impact_score == 9

    # Valid with null image
    summary_no_img = TweetSummary(
        title_ru="Обновление API платформы",
        summary_ru="В API добавлена поддержка потоковой передачи. Разработчикам стало удобнее работать.",
        takeaways=["Новый эндпоинт", "Снижение задержки", "Улучшена стабильность"],
        impact_score=7,
        impact_reason="Удобное обновление для разработчиков",
        hashtags=["#API", "#DevTools", "#AI"],
        selected_image_index=None,
        image_reason=None
    )
    assert summary_no_img.selected_image_index is None


@pytest.mark.asyncio
async def test_editor_node_combined_score_and_sorting(mocker):
    # Mock call_llm to return different impact_scores
    async def mock_call_llm(text, media_urls=None, **kwargs):
        if "HighImpact" in text:
            return {
                "title_ru": "Супер прорыв",
                "summary_ru": "Важное событие в индустрии ИИ. Все изменится.",
                "takeaways": ["Тезис 1", "Тезис 2", "Тезис 3"],
                "impact_score": 10,
                "impact_reason": "Максимальное влияние",
                "hashtags": ["#AI", "#Tech", "#Future"],
                "selected_image_index": 0,
                "image_reason": "Полезная архитектурная диаграмма",
            }
        else:
            return {
                "title_ru": "Обычная новость",
                "summary_ru": "Небольшое улучшение библиотеки. Косметические изменения.",
                "takeaways": ["Тезис 1", "Тезис 2", "Тезис 3"],
                "impact_score": 3,
                "impact_reason": "Минорное обновление",
                "hashtags": ["#AI", "#Update", "#Code"],
                "selected_image_index": None,
                "image_reason": None,
            }

    mocker.patch("src.ai.agents.editor.call_llm", side_effect=mock_call_llm)

    mock_state = {
        "viral_tweets": [
            {
                "tweet_id": "1",
                "text_raw": "LowImpact high viral score",
                "viral_score": 80.0,
                "media_urls": ["https://pbs.twimg.com/media/1.jpg"]
            },
            {
                "tweet_id": "2",
                "text_raw": "HighImpact moderate viral score",
                "viral_score": 75.0,
                "media_urls": ["https://pbs.twimg.com/media/2.jpg"]
            }
        ],
        "draft_posts": []
    }

    result = await editor_node(mock_state)
    drafts = result["draft_posts"]

    assert len(drafts) == 2
    # Tweet 2 has combined_score = 75 * 10 = 750
    # Tweet 1 has combined_score = 80 * 3 = 240
    # Drafts should be sorted with Tweet 2 first
    assert drafts[0]["tweet_id"] == "2"
    assert drafts[0]["combined_score"] == 750.0
    assert drafts[0]["ai_data"]["selected_image_index"] == 0

    assert drafts[1]["tweet_id"] == "1"
    assert drafts[1]["combined_score"] == 240.0


@pytest.mark.asyncio
async def test_editor_node_regenerates_failed_drafts(mocker):
    async def mock_call_llm(text, media_urls=None, **kwargs):
        return {
            "title_ru": "Исправленный заголовок",
            "summary_ru": "Новое описание.",
            "takeaways": ["1", "2", "3"],
            "impact_score": 8,
            "impact_reason": "Good",
            "hashtags": ["#AI"],
            "selected_image_index": None,
            "image_reason": None,
        }

    mocker.patch("src.ai.agents.editor.call_llm", side_effect=mock_call_llm)

    mock_state = {
        "viral_tweets": [
            {"tweet_id": "bad_draft_id", "text_raw": "Tweet to be regenerated", "viral_score": 80.0},
            {"tweet_id": "good_draft_id", "text_raw": "Valid tweet", "viral_score": 90.0},
        ],
        "draft_posts": [
            {
                "tweet_id": "bad_draft_id",
                "original_data": {"tweet_id": "bad_draft_id", "viral_score": 80.0},
                "ai_data": {"title_ru": "Слишком длинный заголовок с ошибкой валидации"},
                "combined_score": 80.0,
            },
            {
                "tweet_id": "good_draft_id",
                "original_data": {"tweet_id": "good_draft_id", "viral_score": 90.0},
                "ai_data": {"title_ru": "Хороший заголовок"},
                "combined_score": 900.0,
            }
        ],
        "validation_errors": ["Tweet bad_draft_id: Title exceeds 10 words."]
    }

    result = await editor_node(mock_state)
    drafts = result["draft_posts"]

    assert len(drafts) == 2
    bad_draft = next(d for d in drafts if d["tweet_id"] == "bad_draft_id")
    assert bad_draft["ai_data"]["title_ru"] == "Исправленный заголовок"

