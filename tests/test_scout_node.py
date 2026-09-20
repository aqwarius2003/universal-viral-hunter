import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from src.ai.agents.scout import (
    extract_tweet_text,
    get_twitter_client,
    load_cookies_from_file,
    parse_tweet_created_at,
    safe_int,
    safe_print,
    scout_node,
)


def test_parse_tweet_created_at_datetime():
    mock_tweet = MagicMock()
    dt = datetime(2026, 3, 18, 12, 0, 0, tzinfo=timezone.utc)
    mock_tweet.created_at_datetime = dt
    result = parse_tweet_created_at(mock_tweet)
    assert result == dt.isoformat()


def test_parse_tweet_created_at_twitter_string():
    mock_tweet = MagicMock()
    mock_tweet.created_at_datetime = None
    mock_tweet.created_at = "Wed Oct 10 20:19:24 +0000 2018"
    result = parse_tweet_created_at(mock_tweet)
    assert "2018-10-10" in result
    assert "+00:00" in result


def test_parse_tweet_created_at_iso_string():
    mock_tweet = MagicMock()
    mock_tweet.created_at_datetime = None
    iso_str = "2026-03-18T15:30:00+00:00"
    mock_tweet.created_at = iso_str
    result = parse_tweet_created_at(mock_tweet)
    assert result == iso_str


def test_parse_tweet_created_at_fallback():
    mock_tweet = MagicMock()
    mock_tweet.created_at_datetime = None
    mock_tweet.created_at = "invalid date string"
    result = parse_tweet_created_at(mock_tweet)
    # Fallback to current UTC datetime
    parsed = datetime.fromisoformat(result)
    assert parsed.tzinfo is not None


def test_load_cookies_from_file_dict(tmp_path):
    p = tmp_path / "cookies.json"
    p.write_text(json.dumps({"auth_token": "tok123", "ct0": "ct456"}), encoding="utf-8")
    assert load_cookies_from_file(p) == {"auth_token": "tok123", "ct0": "ct456"}


def test_load_cookies_from_file_list(tmp_path):
    p = tmp_path / "cookies.json"
    p.write_text(
        json.dumps([
            {"name": "auth_token", "value": "tok_list"},
            {"name": "ct0", "value": "ct0_list"},
            {"name": "twid", "value": "twid_list"},
        ]),
        encoding="utf-8",
    )
    assert load_cookies_from_file(p) == {
        "auth_token": "tok_list",
        "ct0": "ct0_list",
        "twid": "twid_list",
    }


def test_load_cookies_from_file_nonexistent(tmp_path):
    p = tmp_path / "missing.json"
    assert load_cookies_from_file(p) == {}


def test_load_cookies_from_file_corrupt(tmp_path):
    p = tmp_path / "corrupt.json"
    p.write_text("not-a-valid-json", encoding="utf-8")
    assert load_cookies_from_file(p) == {}


def test_extract_tweet_text_full_text():
    mock_tweet = MagicMock()
    mock_tweet.full_text = "Full long text of the tweet"
    mock_tweet.text = "Short text"
    assert extract_tweet_text(mock_tweet) == "Full long text of the tweet"


def test_extract_tweet_text_fallback_to_text():
    mock_tweet = MagicMock()
    mock_tweet.full_text = None
    mock_tweet.text = "Short text"
    assert extract_tweet_text(mock_tweet) == "Short text"


def test_extract_tweet_text_from_dict():
    d = {"full_text": "Dict full text", "text": "Dict text"}
    assert extract_tweet_text(d) == "Dict full text"


@pytest.mark.asyncio
async def test_get_twitter_client_with_settings(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "twitter_auth_token", "test_auth_token")
    monkeypatch.setattr(settings, "twitter_ct0", "test_ct0")

    with patch("src.ai.agents.scout.load_cookies_from_file", return_value={}), \
         patch("src.ai.agents.scout.Client") as mock_client_cls:
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance

        client = await get_twitter_client()
        assert client == mock_instance
        mock_instance.set_cookies.assert_called_once_with({
            "auth_token": "test_auth_token",
            "ct0": "test_ct0",
        })


@pytest.mark.asyncio
async def test_get_twitter_client_merging_cookies(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "twitter_auth_token", "env_auth")
    monkeypatch.setattr(settings, "twitter_ct0", None)

    with patch("src.ai.agents.scout.load_cookies_from_file", return_value={"ct0": "file_ct0", "twid": "twid_val"}), \
         patch("src.ai.agents.scout.Client") as mock_client_cls:
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance

        client = await get_twitter_client()
        assert client == mock_instance
        mock_instance.set_cookies.assert_called_once_with({
            "auth_token": "env_auth",
            "ct0": "file_ct0",
            "twid": "twid_val",
        })


@pytest.mark.asyncio
async def test_get_twitter_client_fallback_cookies_json(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "twitter_auth_token", None)
    monkeypatch.setattr(settings, "twitter_ct0", None)

    with patch("src.ai.agents.scout.load_cookies_from_file", return_value={"auth_token": "json_auth_token", "ct0": "json_ct0"}), \
         patch("src.ai.agents.scout.Client") as mock_client_cls:
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance

        client = await get_twitter_client()
        assert client == mock_instance
        mock_instance.set_cookies.assert_called_once_with({
            "auth_token": "json_auth_token",
            "ct0": "json_ct0",
        })


@pytest.mark.asyncio
async def test_get_twitter_client_missing_credentials_raises(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "twitter_auth_token", None)
    monkeypatch.setattr(settings, "twitter_ct0", None)

    with patch("src.ai.agents.scout.load_cookies_from_file", return_value={}):
        with pytest.raises(ValueError, match="TWITTER_AUTH_TOKEN или TWITTER_CT0"):
            await get_twitter_client()


@pytest.mark.asyncio
async def test_scout_node_handles_key_byte_error():
    """Verifies that scout_node handles 'Couldn't get KEY_BYTE indices' gracefully."""
    mock_client = MagicMock()
    mock_client.search_tweet = AsyncMock(
        side_effect=Exception("Couldn't get KEY_BYTE indices")
    )

    with patch("src.ai.agents.scout.get_twitter_client", AsyncMock(return_value=mock_client)):
        result = await scout_node({"raw_tweets": []})
        assert result == {"raw_tweets": []}


@pytest.mark.asyncio
async def test_scout_node_empty_search_results():
    mock_client = MagicMock()
    mock_client.search_tweet = AsyncMock(return_value=[])

    with patch("src.ai.agents.scout.get_twitter_client", AsyncMock(return_value=mock_client)):
        result = await scout_node({"raw_tweets": []})
        assert result == {"raw_tweets": []}


@pytest.mark.asyncio
async def test_scout_node_success():
    mock_tweet = MagicMock()
    mock_tweet.id = 1234567890
    mock_tweet.text = "Groundbreaking AI model released today!"
    # Ensure full_text returns None or str, not a sub-MagicMock
    mock_tweet.full_text = None
    mock_tweet.user.screen_name = "ai_researcher"
    mock_tweet.user.name = "AI Researcher"
    mock_tweet.created_at_datetime = datetime(2026, 3, 18, 10, 0, 0, tzinfo=timezone.utc)
    mock_tweet.favorite_count = 500
    mock_tweet.retweet_count = 120
    mock_tweet.quote_count = 35
    mock_tweet.bookmark_count = 80
    mock_tweet.view_count = 45000

    mock_client = MagicMock()
    mock_client.search_tweet = AsyncMock(return_value=[mock_tweet])

    with patch("src.ai.agents.scout.get_twitter_client", AsyncMock(return_value=mock_client)), \
         patch("src.ai.agents.scout.filter_new_tweets", AsyncMock(side_effect=lambda tweets: tweets)), \
         patch("src.ai.agents.scout.save_raw_tweets", AsyncMock()) as mock_save:

        result = await scout_node({"raw_tweets": []})
        assert len(result["raw_tweets"]) == 1

        saved_tweet = result["raw_tweets"][0]
        assert saved_tweet["tweet_id"] == "1234567890"
        assert saved_tweet["author_handle"] == "@ai_researcher"
        assert saved_tweet["author_name"] == "AI Researcher"
        assert saved_tweet["text_raw"] == "Groundbreaking AI model released today!"
        assert saved_tweet["tweet_url"] == "https://x.com/ai_researcher/status/1234567890"
        assert saved_tweet["likes_count"] == 500
        assert saved_tweet["reposts_count"] == 120
        assert saved_tweet["quotes_count"] == 35
        assert saved_tweet["bookmarks_count"] == 80
        assert saved_tweet["impressions_count"] == 45000
        mock_save.assert_awaited_once()


@pytest.mark.asyncio
async def test_scout_node_handles_malformed_tweet():
    """Verifies that tweets missing user or text are handled without crashing."""
    valid_tweet = MagicMock()
    valid_tweet.id = 999
    valid_tweet.text = "Valid tweet text"
    valid_tweet.full_text = None
    valid_tweet.user = None
    valid_tweet.created_at_datetime = None
    valid_tweet.created_at = "Wed Oct 10 20:19:24 +0000 2018"
    valid_tweet.favorite_count = None
    valid_tweet.retweet_count = None
    valid_tweet.quote_count = None
    valid_tweet.bookmark_count = None
    valid_tweet.view_count = None
    valid_tweet.views_count = 100

    empty_tweet = MagicMock()
    empty_tweet.id = None
    empty_tweet.text = None
    empty_tweet.full_text = None

    mock_client = MagicMock()
    mock_client.search_tweet = AsyncMock(return_value=[valid_tweet, empty_tweet])

    with patch("src.ai.agents.scout.get_twitter_client", AsyncMock(return_value=mock_client)), \
         patch("src.ai.agents.scout.filter_new_tweets", AsyncMock(side_effect=lambda tweets: tweets)), \
         patch("src.ai.agents.scout.save_raw_tweets", AsyncMock()):

        result = await scout_node({"raw_tweets": []})
        assert len(result["raw_tweets"]) == 1

        tweet = result["raw_tweets"][0]
        assert tweet["tweet_id"] == "999"
        assert tweet["author_handle"] == "@unknown"
        assert tweet["author_name"] == "Unknown"
        assert tweet["tweet_url"] == "https://x.com/i/status/999"
        assert tweet["likes_count"] == 0
        assert tweet["impressions_count"] == 100


def test_safe_int():
    assert safe_int(123) == 123
    assert safe_int("456") == 456
    assert safe_int("0") == 0
    assert safe_int(None) == 0
    assert safe_int(None, default=10) == 10
    assert safe_int("invalid") == 0
    assert safe_int("invalid", default=5) == 5
    assert safe_int(12.7) == 12


def test_safe_print(capsys):
    safe_print("Hello world")
    captured = capsys.readouterr()
    assert "Hello world" in captured.out


@pytest.mark.asyncio
async def test_scout_node_string_view_count():
    """Verifies that string view_count from Twitter API is converted to int."""
    mock_tweet = MagicMock()
    mock_tweet.id = 12345
    mock_tweet.text = "AI tweet"
    mock_tweet.full_text = None
    mock_tweet.user.screen_name = "testuser"
    mock_tweet.user.name = "Test User"
    mock_tweet.created_at_datetime = None
    mock_tweet.created_at = "Wed Oct 10 20:19:24 +0000 2018"
    mock_tweet.favorite_count = "250"
    mock_tweet.retweet_count = "50"
    mock_tweet.quote_count = "10"
    mock_tweet.bookmark_count = "20"
    mock_tweet.view_count = "35000000"
    mock_tweet.views_count = None

    mock_client = MagicMock()
    mock_client.search_tweet = AsyncMock(return_value=[mock_tweet])

    with patch("src.ai.agents.scout.get_twitter_client", AsyncMock(return_value=mock_client)), \
         patch("src.ai.agents.scout.filter_new_tweets", AsyncMock(side_effect=lambda tweets: tweets)), \
         patch("src.ai.agents.scout.save_raw_tweets", AsyncMock()):

        result = await scout_node({"raw_tweets": []})
        assert len(result["raw_tweets"]) == 1
        tweet = result["raw_tweets"][0]
        assert tweet["likes_count"] == 250
        assert isinstance(tweet["likes_count"], int)
        assert tweet["impressions_count"] == 35000000
        assert isinstance(tweet["impressions_count"], int)


@pytest.mark.asyncio
async def test_scout_node_retries_with_refresh_transaction():
    """Verifies that scout_node calls refresh_transaction and retries when search fails once."""
    mock_tweet = MagicMock()
    mock_tweet.id = 555
    mock_tweet.text = "Recovered AI tweet"
    mock_tweet.full_text = None
    mock_tweet.user.screen_name = "recovered"
    mock_tweet.user.name = "Recovered"
    mock_tweet.created_at_datetime = None
    mock_tweet.created_at = "Wed Oct 10 20:19:24 +0000 2018"
    mock_tweet.favorite_count = 10
    mock_tweet.retweet_count = 5
    mock_tweet.quote_count = 1
    mock_tweet.bookmark_count = 2
    mock_tweet.view_count = 1000

    mock_client = MagicMock()
    mock_client.search_tweet = AsyncMock(side_effect=[
        Exception("Couldn't get KEY_BYTE indices"),
        [mock_tweet]
    ])
    mock_client.refresh_transaction = MagicMock()

    with patch("src.ai.agents.scout.get_twitter_client", AsyncMock(return_value=mock_client)), \
         patch("src.ai.agents.scout.filter_new_tweets", AsyncMock(side_effect=lambda tweets: tweets)), \
         patch("src.ai.agents.scout.save_raw_tweets", AsyncMock()):

        result = await scout_node({"raw_tweets": []})
        assert len(result["raw_tweets"]) == 1
        mock_client.refresh_transaction.assert_called_once()
        assert mock_client.search_tweet.await_count == 2


@pytest.mark.asyncio
async def test_scout_node_in_batch_deduplication():
    mock_tweet = MagicMock()
    mock_tweet.id = 777
    mock_tweet.text = "Duplicate tweet"
    mock_tweet.full_text = None
    mock_tweet.user.screen_name = "user7"
    mock_tweet.user.name = "User 7"
    mock_tweet.created_at_datetime = None
    mock_tweet.created_at = "Wed Oct 10 20:19:24 +0000 2018"
    mock_tweet.favorite_count = 1
    mock_tweet.retweet_count = 1
    mock_tweet.quote_count = 1
    mock_tweet.bookmark_count = 1
    mock_tweet.view_count = 100

    # Same tweet returned twice in search results
    mock_client = MagicMock()
    mock_client.search_tweet = AsyncMock(return_value=[mock_tweet, mock_tweet])

    with patch("src.ai.agents.scout.get_twitter_client", AsyncMock(return_value=mock_client)), \
         patch("src.ai.agents.scout.filter_new_tweets", AsyncMock(side_effect=lambda tweets: tweets)), \
         patch("src.ai.agents.scout.save_raw_tweets", AsyncMock()) as mock_save:

        result = await scout_node({"raw_tweets": []})
        assert len(result["raw_tweets"]) == 1
        assert result["raw_tweets"][0]["tweet_id"] == "777"


@pytest.mark.asyncio
async def test_scout_node_retry_failure_is_handled():
    mock_client = MagicMock()
    mock_client.search_tweet = AsyncMock(side_effect=[
        Exception("First transaction error"),
        Exception("Second failure after refresh")
    ])
    mock_client.refresh_transaction = MagicMock()

    with patch("src.ai.agents.scout.get_twitter_client", AsyncMock(return_value=mock_client)):
        result = await scout_node({"raw_tweets": []})
        assert result == {"raw_tweets": []}
        mock_client.refresh_transaction.assert_called_once()


def test_extract_media_urls_from_dict_and_object():
    from src.ai.agents.scout import extract_media_urls

    # 1. From dict with media list
    dict_tweet = {
        "media": [
            {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/photo1.jpg"},
            {"type": "video", "media_url_https": "https://pbs.twimg.com/media/video.mp4"}
        ]
    }
    assert extract_media_urls(dict_tweet) == ["https://pbs.twimg.com/media/photo1.jpg"]

    # 2. From object with photos attribute
    class PhotoObj:
        type = "photo"
        media_url_https = "https://pbs.twimg.com/media/photo2.jpg"

    class TweetObj:
        photos = [PhotoObj()]

    assert extract_media_urls(TweetObj()) == ["https://pbs.twimg.com/media/photo2.jpg"]


@pytest.mark.asyncio
async def test_refresh_tweet_metrics(mocker):
    from src.ai.agents.scout import refresh_tweet_metrics

    mock_tweet = MagicMock()
    mock_tweet.favorite_count = 100
    mock_tweet.retweet_count = 25
    mock_tweet.quote_count = 5
    mock_tweet.bookmark_count = 10
    mock_tweet.view_count = 5000

    mock_client = MagicMock()
    mock_client.get_tweet_by_id = AsyncMock(return_value=mock_tweet)

    with patch("src.ai.agents.scout.get_twitter_client", AsyncMock(return_value=mock_client)):
        res = await refresh_tweet_metrics(["999"])
        assert len(res) == 1
        assert res[0]["tweet_id"] == "999"
        assert res[0]["likes_count"] == 100
        assert res[0]["impressions_count"] == 5000


@pytest.mark.asyncio
async def test_revalidate_recent_tweets_metrics(mocker):
    from src.ai.agents.scout import revalidate_recent_tweets_metrics

    mock_tweets = [
        {
            "tweet_id": "rev_1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "likes_count": 10,
            "reposts_count": 2,
            "quotes_count": 0,
            "bookmarks_count": 0,
            "impressions_count": 1000,
            "viral_score": 10.0,
            "is_viral": False,
        }
    ]

    mock_updated_metrics = [
        {
            "tweet_id": "rev_1",
            "likes_count": 5000,
            "reposts_count": 1200,
            "quotes_count": 300,
            "bookmarks_count": 800,
            "impressions_count": 250000,
        }
    ]

    with patch("src.db.get_recent_active_tweets", AsyncMock(return_value=mock_tweets)), \
         patch("src.ai.agents.scout.refresh_tweet_metrics", AsyncMock(return_value=mock_updated_metrics)), \
         patch("src.db.update_tweet_metrics", AsyncMock()) as mock_update_db:

        updated_count = await revalidate_recent_tweets_metrics(hours=48)
        assert updated_count == 1
        mock_update_db.assert_awaited_once()
        call_kwargs = mock_update_db.call_args.kwargs
        assert call_kwargs["tweet_id"] == "rev_1"
        assert call_kwargs["likes"] == 5000
        assert call_kwargs["viral_score"] > 50.0
        assert call_kwargs["is_viral"] is True


@pytest.mark.asyncio
async def test_scout_node_multi_query_search(monkeypatch, mocker):
    """Verifies that scout_node executes searches across all queries specified in settings."""
    from src.config import settings

    monkeypatch.setattr(settings, "x_search_queries", "AI agents lang:en\n#LLM min_faves:50\n(machine learning) lang:en")
    mock_client = mocker.MagicMock()
    mock_client.search_tweet = AsyncMock(return_value=[])

    mocker.patch("src.ai.agents.scout.get_twitter_client", AsyncMock(return_value=mock_client))
    mocker.patch("src.ai.agents.scout.filter_new_tweets", AsyncMock(return_value=[]))
    mocker.patch("src.ai.agents.scout.save_raw_tweets", AsyncMock())

    res = await scout_node({})
    assert res == {"raw_tweets": []}
    # All 3 queries must have been queried
    searched_queries = [call.args[0] for call in mock_client.search_tweet.call_args_list]
    assert "AI agents lang:en" in searched_queries
    assert "#LLM min_faves:50" in searched_queries
    assert "(machine learning) lang:en" in searched_queries



