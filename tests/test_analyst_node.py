import pytest
from datetime import datetime, timezone, timedelta
from src.ai.agents.analyst import analyst_node
from src.config import settings

@pytest.mark.asyncio
async def test_analyst_node_filtering(mocker):
    # Mock the DB update to avoid actual SQLite writes during unit testing
    mocker.patch("src.ai.agents.analyst.update_tweet_status_and_score")
    
    now = datetime.now(timezone.utc)
    recent_time = (now - timedelta(hours=2)).isoformat()
    
    mock_state = {
        "raw_tweets": [
            {
                "tweet_id": "1",
                "created_at": recent_time,
                "likes_count": 10000,
                "reposts_count": 5000,
                "quotes_count": 1000,
                "bookmarks_count": 2000,
                "impressions_count": 500000,
            }, # Should be highly viral
            {
                "tweet_id": "2",
                "created_at": recent_time,
                "likes_count": 5,
                "reposts_count": 0,
                "quotes_count": 0,
                "bookmarks_count": 0,
                "impressions_count": 100,
            } # Should be ignored
        ]
    }
    
    # Run the node
    result = await analyst_node(mock_state)
    
    # Assertions
    assert "viral_tweets" in result
    assert len(result["viral_tweets"]) == 1
    
    viral_tweet = result["viral_tweets"][0]
    assert viral_tweet["tweet_id"] == "1"
    assert viral_tweet["viral_score"] >= settings.min_viral_score


@pytest.mark.asyncio
async def test_analyst_node_sorts_descending(mocker):
    mocker.patch("src.ai.agents.analyst.update_tweet_status_and_score")

    now = datetime.now(timezone.utc)
    recent_time = (now - timedelta(hours=1)).isoformat()

    mock_state = {
        "raw_tweets": [
            {
                "tweet_id": "low_viral",
                "created_at": recent_time,
                "likes_count": 2000,
                "reposts_count": 1000,
                "quotes_count": 200,
                "bookmarks_count": 300,
                "impressions_count": 50000,
            },
            {
                "tweet_id": "high_viral",
                "created_at": recent_time,
                "likes_count": 50000,
                "reposts_count": 20000,
                "quotes_count": 5000,
                "bookmarks_count": 10000,
                "impressions_count": 2000000,
            },
            {
                "tweet_id": "mid_viral",
                "created_at": recent_time,
                "likes_count": 10000,
                "reposts_count": 4000,
                "quotes_count": 800,
                "bookmarks_count": 1500,
                "impressions_count": 200000,
            }
        ]
    }

    result = await analyst_node(mock_state)
    viral = result["viral_tweets"]

    assert len(viral) == 3
    assert viral[0]["tweet_id"] == "high_viral"
    assert viral[1]["tweet_id"] == "mid_viral"
    assert viral[2]["tweet_id"] == "low_viral"
    assert viral[0]["viral_score"] >= viral[1]["viral_score"] >= viral[2]["viral_score"]