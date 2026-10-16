import pytest
from src.ai.agents.editor import editor_node
from src.ai.agents.critic import critic_node, should_publish

@pytest.mark.asyncio
async def test_editor_node_generates_draft(monkeypatch):
    # Arrange
    async def mock_call_llm(text, media_urls=None, **kwargs):
        return {
            "title_ru": "Тестовый заголовок",
            "summary_ru": "Тестовая выжимка твита.",
            "takeaways": ["Тезис 1", "Тезис 2", "Тезис 3"],
            "impact_score": 8,
            "impact_reason": "Важная новость",
            "hashtags": ["#AI", "#Tech"],
            "selected_image_index": None,
            "image_reason": None,
        }

    monkeypatch.setattr("src.ai.agents.editor.call_llm", mock_call_llm)

    mock_state = {
        "viral_tweets": [
            {"tweet_id": "1001", "text_raw": "Test viral tweet about LLMs", "viral_score": 100.0}
        ],
        "draft_posts": []
    }
    
    # Act
    result = await editor_node(mock_state)
    
    # Assert
    assert "draft_posts" in result
    assert len(result["draft_posts"]) == 1
    
    draft = result["draft_posts"][0]
    assert draft["tweet_id"] == "1001"
    assert "ai_data" in draft
    
    ai_data = draft["ai_data"]
    assert "title_ru" in ai_data
    assert len(ai_data["takeaways"]) == 3

@pytest.mark.asyncio
async def test_critic_node_passes_valid_draft():
    # Arrange
    mock_state = {
        "draft_posts": [
            {
                "tweet_id": "1001",
                "ai_data": {
                    "title_ru": "Важный релиз новой нейросети", # 4 words
                    "takeaways": ["Пункт 1", "Пункт 2", "Пункт 3"] # Exactly 3
                }
            }
        ]
    }
    
    # Act
    result = await critic_node(mock_state)
    
    # Assert
    assert result.get("validation_errors") == []
    assert should_publish(result) == "publisher"

@pytest.mark.asyncio
async def test_critic_node_passes_without_takeaways_when_disabled(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "show_metric_takeaways", False)
    monkeypatch.setattr(settings, "max_title_words", 25)

    mock_state = {
        "draft_posts": [
            {
                "tweet_id": "1002",
                "ai_data": {
                    "title_ru": "💥 РАЗМОТАЛО В ХЛАМ: лобовое на бешеной скорости в центре города",
                    "takeaways": []  # Zero takeaways
                }
            }
        ]
    }
    result = await critic_node(mock_state)
    assert result.get("validation_errors") == []
    assert should_publish(result) == "publisher"

@pytest.mark.asyncio
async def test_critic_node_catches_invalid_draft(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "show_metric_takeaways", True)
    monkeypatch.setattr(settings, "max_title_words", 10)

    # Arrange
    mock_state = {
        "draft_posts": [
            {
                "tweet_id": "1002",
                "ai_data": {
                    # 11 words (violates max 10)
                    "title_ru": "Один два три четыре пять шесть семь восемь девять десять одиннадцать", 
                    # 0 items when show_metric_takeaways is True
                    "takeaways": [] 
                }
            }
        ]
    }
    
    # Act
    result = await critic_node(mock_state)
    errors = result.get("validation_errors", [])
    
    # Assert
    assert len(errors) == 2
    assert "Title exceeds 10 words" in errors[0]
    assert "Must have at least 1 takeaway" in errors[1]
    
    # Test routing logic (Loop trigger)
    mock_state_with_errors = {"validation_errors": errors}
    assert should_publish(mock_state_with_errors) == "editor"