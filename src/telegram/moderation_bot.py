import asyncio
import json
from typing import Any, Dict
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery

from src.config import settings
from src.db import get_tweet_by_id, update_tweet_status_and_score, get_active_topics
from src.telegram.sender import send_photo_to_channel, send_post_to_channel, send_video_to_channel

class CommentState(StatesGroup):
    waiting_for_comment = State()

dp = Dispatcher(storage=MemoryStorage())
_bot = None
_runner_task = None

def get_bot():
    global _bot
    if not settings.telegram_bot_token or "your_bot_token" in settings.telegram_bot_token:
        return None
    if _bot is None:
        _bot = Bot(token=settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return _bot

async def start_moderation_bot():
    global _runner_task
    bot = get_bot()
    if not bot:
        print("⚠️ Telegram bot token not set. Moderation bot will not start.")
        return
    print("🤖 Starting Telegram Moderation Bot...")
    _runner_task = asyncio.create_task(dp.start_polling(bot))

async def stop_moderation_bot():
    global _runner_task
    if _runner_task:
        _runner_task.cancel()

def build_admin_keyboard(tweet_id: str, tweet_url: str) -> InlineKeyboardMarkup:
    """Builds inline keyboard for the admin review."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"pub:{tweet_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rej:{tweet_id}")
            ],
            [
                InlineKeyboardButton(text="📝 С комментом", callback_data=f"cmt:{tweet_id}")
            ],
            [
                InlineKeyboardButton(text="🔗 Оригинал", url=tweet_url)
            ]
        ]
    )

async def _publish_approved_post(tweet_id: str, custom_comment: str = None, bot: Bot = None, callback: CallbackQuery = None):
    tweet = await get_tweet_by_id(tweet_id)
    if not tweet:
        return "Твит не найден в базе данных."
    
    if tweet.get("status") in ("POSTED", "REJECTED"):
        return f"Этот пост уже обработан (Статус: {tweet.get('status')})"

    ai_data_raw = tweet.get("summary_json") or "{}"
    try:
        ai_data = json.loads(ai_data_raw)
    except:
        ai_data = {}

    media_urls = tweet.get("media_urls", "[]")
    if isinstance(media_urls, str):
        try:
            media_urls = json.loads(media_urls)
        except:
            media_urls = []

    video_data = tweet.get("video_data")

    # If custom comment is provided, prepend it to the text
    if custom_comment:
        original_text = ai_data.get("summary_ru", "")
        ai_data["summary_ru"] = f"🗣 <b>Комментарий:</b>\n{custom_comment}\n\n{original_text}"

    # Topic routing
    topic_id = tweet.get("topic_id")
    target_channel = settings.telegram_channel_id
    if topic_id:
        active_topics = await get_active_topics()
        topic = next((t for t in active_topics if t["id"] == topic_id), None)
        if topic and topic.get("telegram_channel_id"):
            target_channel = topic["telegram_channel_id"]

    video_local_path = None
    video_thumbnail = None
    if video_data and isinstance(video_data, dict):
        video_local_path = video_data.get("local_path")
        video_thumbnail = video_data.get("thumbnail")

    selected_idx = ai_data.get("selected_image_index")
    photo_url = None
    if not video_local_path and selected_idx is not None and isinstance(selected_idx, int) and 0 <= selected_idx < len(media_urls):
        photo_url = media_urls[selected_idx]

    from src.ai.agents.publisher import format_telegram_post
    formatted_text = format_telegram_post(tweet, ai_data, is_photo=bool(photo_url) or bool(video_local_path))
    tweet_url = tweet.get("tweet_url")

    # Send natively to the channel
    try:
        if video_local_path:
            await send_video_to_channel(formatted_text, video_local_path, thumbnail_path=video_thumbnail, tweet_url=tweet_url, channel_id_override=target_channel)
        elif photo_url:
            await send_photo_to_channel(formatted_text, photo_url, tweet_url, channel_id_override=target_channel)
        else:
            await send_post_to_channel(formatted_text, tweet_url, channel_id_override=target_channel)
    except Exception as e:
        return f"Ошибка публикации: {str(e)}"

    # Update DB
    await update_tweet_status_and_score(
        tweet_id=tweet_id,
        score=tweet.get("viral_score", 0.0),
        is_viral=True,
        status="POSTED",
        impact_score=ai_data.get("impact_score", 0),
        summary_json=json.dumps(ai_data, ensure_ascii=False)
    )
    return "✅ Пост успешно опубликован!"

@dp.callback_query(F.data.startswith("pub:"))
async def handle_publish(callback: types.CallbackQuery):
    tweet_id = callback.data.split(":")[1]
    res = await _publish_approved_post(tweet_id, bot=callback.bot, callback=callback)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(res)
    await callback.answer()

@dp.callback_query(F.data.startswith("rej:"))
async def handle_reject(callback: types.CallbackQuery):
    tweet_id = callback.data.split(":")[1]
    tweet = await get_tweet_by_id(tweet_id)
    if tweet:
        ai_data_raw = tweet.get("summary_json") or "{}"
        try:
            ai_data = json.loads(ai_data_raw)
        except:
            ai_data = {}
        await update_tweet_status_and_score(
            tweet_id=tweet_id,
            score=tweet.get("viral_score", 0.0),
            is_viral=True,
            status="REJECTED",
            impact_score=ai_data.get("impact_score", 0),
            summary_json=json.dumps(ai_data, ensure_ascii=False)
        )
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply("❌ Пост отклонен.")
    else:
        await callback.answer("Твит не найден.", show_alert=True)
    await callback.answer()

@dp.callback_query(F.data.startswith("cmt:"))
async def handle_comment(callback: types.CallbackQuery, state: FSMContext):
    tweet_id = callback.data.split(":")[1]
    await state.update_data(tweet_id=tweet_id)
    await state.set_state(CommentState.waiting_for_comment)
    await callback.message.reply("Отправьте ваш комментарий текстом (или нажмите /cancel):")
    await callback.answer()

@dp.message(CommentState.waiting_for_comment)
async def process_comment(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.reply("Отменено.")
        return
        
    data = await state.get_data()
    tweet_id = data.get("tweet_id")
    custom_comment = message.text
    
    await state.clear()
    res = await _publish_approved_post(tweet_id, custom_comment=custom_comment, bot=message.bot)
    await message.reply(res)
