import pytest
import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch
from src.telegram.moderation_bot import send_post_card_to_admin, handle_sync_command, build_admin_keyboard
from src.config import settings

@pytest.mark.asyncio
async def test_send_post_card_empty_admin(monkeypatch):
    monkeypatch.setattr(settings, "telegram_admin_id", None)
    with pytest.raises(ValueError, match="ID администратора Telegram"):
        await send_post_card_to_admin({"tweet_id": "123"})

@pytest.mark.asyncio
async def test_send_post_card_valid(monkeypatch):
    monkeypatch.setattr(settings, "telegram_admin_id", "123456789")
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
    
    mock_bot = MagicMock()
    mock_bot.delete_message = AsyncMock()
    mock_bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
    mock_bot.session = MagicMock()
    mock_bot.session.close = AsyncMock()
    
    with patch("src.telegram.moderation_bot.get_bot", return_value=mock_bot):
        with patch("src.telegram.sender.Bot", return_value=mock_bot):
            with patch("src.telegram.moderation_bot.save_tweet_admin_message_id", new_callable=AsyncMock):
                tweet = {
                    "tweet_id": "1001",
                    "author_handle": "testuser",
                    "author_name": "Test User",
                    "text_raw": "Original tweet content",
                    "tweet_url": "https://x.com/testuser/status/1001",
                    "summary_json": json.dumps({
                        "title_ru": "Заголовок новости",
                        "summary_ru": "Краткое содержание новости",
                        "hashtags": ["#AI", "#Tech"]
                    })
                }
                msg_id = await send_post_card_to_admin(tweet)
                assert msg_id == 999

@pytest.mark.asyncio
async def test_send_post_card_with_local_video_and_thumbnail(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "telegram_admin_id", "123456789")
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")

    v_file = tmp_path / "test.mp4"
    v_file.write_bytes(b"fake video data")
    t_file = tmp_path / "test_thumb.jpg"
    t_file.write_bytes(b"fake thumb data")

    mock_bot = MagicMock()
    mock_bot.delete_message = AsyncMock()
    mock_bot.send_video = AsyncMock(return_value=MagicMock(message_id=888))
    mock_bot.session = MagicMock()
    mock_bot.session.close = AsyncMock()

    with patch("src.telegram.moderation_bot.get_bot", return_value=mock_bot):
        with patch("src.telegram.sender.Bot", return_value=mock_bot):
            with patch("src.telegram.moderation_bot.save_tweet_admin_message_id", new_callable=AsyncMock):
                tweet = {
                    "tweet_id": "1002",
                    "author_handle": "videouser",
                    "author_name": "Video User",
                    "text_raw": "Video tweet",
                    "tweet_url": "https://x.com/videouser/status/1002",
                    "video_data": json.dumps({
                        "local_path": str(v_file),
                        "thumbnail": str(t_file),
                    }),
                    "summary_json": json.dumps({
                        "title_ru": "Видео заголовок",
                        "summary_ru": "Описание видео",
                        "hashtags": ["#Video"]
                    })
                }
                msg_id = await send_post_card_to_admin(tweet)
                assert msg_id == 888

@pytest.mark.asyncio
async def test_send_post_card_photo_with_local_path(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "telegram_admin_id", "123456789")
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")

    t_file = tmp_path / "test_thumb.jpg"
    t_file.write_bytes(b"fake thumb data")

    mock_bot = MagicMock()
    mock_bot.delete_message = AsyncMock()
    mock_bot.send_photo = AsyncMock(return_value=MagicMock(message_id=777))
    mock_bot.session = MagicMock()
    mock_bot.session.close = AsyncMock()

    with patch("src.telegram.moderation_bot.get_bot", return_value=mock_bot):
        with patch("src.telegram.sender.Bot", return_value=mock_bot):
            with patch("src.telegram.moderation_bot.save_tweet_admin_message_id", new_callable=AsyncMock):
                tweet = {
                    "tweet_id": "1003",
                    "author_handle": "photouser",
                    "tweet_url": "https://x.com/photouser/status/1003",
                    "video_data": json.dumps({
                        "local_path": "non_existent.mp4",
                        "thumbnail": str(t_file),
                    }),
                    "summary_json": json.dumps({
                        "title_ru": "Фото заголовок",
                        "summary_ru": "Описание фото",
                    })
                }
                msg_id = await send_post_card_to_admin(tweet)
                assert msg_id == 777
                assert mock_bot.send_photo.called

def test_build_admin_keyboard_empty_url():
    kb = build_admin_keyboard("1004", tweet_url="")
    all_buttons = [btn for row in kb.inline_keyboard for btn in row]
    # Verify no button with empty url exists
    for b in all_buttons:
        if b.url is not None:
            assert b.url.startswith("http")

@pytest.mark.asyncio
async def test_handle_sync_command(monkeypatch):
    monkeypatch.setattr(settings, "telegram_admin_id", "555")
    mock_message = MagicMock()
    mock_message.from_user.id = 555
    mock_message.reply = AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))
    
    queued_posts = [
        {"tweet_id": "t1", "text_raw": "post 1", "tweet_url": "https://x.com/1"},
        {"tweet_id": "t2", "text_raw": "post 2", "tweet_url": "https://x.com/2"},
    ]
    
    with patch("src.db.get_queued_posts", new_callable=AsyncMock, return_value=queued_posts):
        with patch("src.telegram.moderation_bot.send_post_card_to_admin", new_callable=AsyncMock, side_effect=[101, 102]):
            await handle_sync_command(mock_message)
            assert mock_message.reply.called
            progress_msg = mock_message.reply.return_value
            assert progress_msg.edit_text.called
            last_text = progress_msg.edit_text.call_args[0][0]
            assert "Всего в очереди: <b>2</b>" in last_text
            assert "Карточек отправлено админу: <b>2</b>" in last_text
