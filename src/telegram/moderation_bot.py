import asyncio
import json
import logging
from typing import Any, Dict, Optional
try:
    from aiogram import Bot, Dispatcher, F, types
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
    AIOGRAM_AVAILABLE = True
except ImportError:
    AIOGRAM_AVAILABLE = False
    Bot = None
    class _DummyTypes:
        CallbackQuery = Any
        Message = Any
    types = _DummyTypes()
    DefaultBotProperties = None
    ParseMode = None
    FSMContext = Any
    InlineKeyboardButton = InlineKeyboardMarkup = CallbackQuery = Any
    class _DummyF:
        def __getattr__(self, name):
            return self
        def __call__(self, *args, **kwargs):
            return self
        def __eq__(self, other):
            return self
        def startswith(self, *args, **kwargs):
            return self
    F = _DummyF()
    class State:
        pass
    class StatesGroup:
        pass
    class MemoryStorage:
        pass
    class Dispatcher:
        def __init__(self, *args, **kwargs):
            pass
        def callback_query(self, *args, **kwargs):
            return lambda fn: fn
        def message(self, *args, **kwargs):
            return lambda fn: fn
        async def start_polling(self, *args, **kwargs):
            pass

from src.config import settings
from src.db import (
    get_tweet_by_id,
    update_tweet_status_and_score,
    get_active_topics,
    save_tweet_admin_message_id,
    get_tweet_admin_message_id,
    set_tweet_status,
)
from src.telegram.sender import (
    send_photo_to_channel,
    send_post_to_channel,
    send_video_to_channel,
    normalize_channel_id,
)
from src.utils.technical_audit import audit_and_prepare_telegram_content

logger = logging.getLogger("viral-hunter.moderation")


class ModerationState(StatesGroup):
    waiting_for_comment = State()
    waiting_for_ai_feedback = State()
    waiting_for_manual_caption = State()


dp = Dispatcher(storage=MemoryStorage())
_bot = None
_runner_task = None


def get_bot() -> Optional[Bot]:
    global _bot
    if not settings.telegram_bot_token or "your_bot_token" in settings.telegram_bot_token:
        return None
    if _bot is None:
        _bot = Bot(token=settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return _bot


async def start_moderation_bot() -> None:
    global _runner_task
    bot = get_bot()
    if not bot:
        print("⚠️ Telegram bot token not set. Moderation bot will not start.")
        return
    print("🤖 Starting Telegram Moderation Bot...")
    _runner_task = asyncio.create_task(dp.start_polling(bot))


async def stop_moderation_bot() -> None:
    global _runner_task
    if _runner_task:
        _runner_task.cancel()


def build_admin_keyboard(tweet_id: str, tweet_url: str) -> InlineKeyboardMarkup:
    """Builds interactive inline keyboard for the admin review."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"pub:{tweet_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rej:{tweet_id}"),
            ],
            [
                InlineKeyboardButton(text="🔄 На доработку ИИ", callback_data=f"rew:{tweet_id}"),
                InlineKeyboardButton(text="✏️ Править подпись", callback_data=f"edit_txt:{tweet_id}"),
            ],
            [
                InlineKeyboardButton(text="📝 С комментом", callback_data=f"cmt:{tweet_id}"),
                InlineKeyboardButton(text="🔗 Оригинал в X", url=tweet_url),
            ],
        ]
    )


async def delete_admin_moderation_message(tweet_id: str) -> None:
    """
    Deletes the moderation card from the admin chat when published from Web UI, cron, or Telegram.
    Ensures admin's chat strictly mirrors the unposted queue ('Inbox Zero').
    """
    admin_id = getattr(settings, "telegram_admin_id", None)
    if not admin_id:
        return
    admin_msg_id = await get_tweet_admin_message_id(tweet_id)
    if not admin_msg_id:
        return

    bot = get_bot()
    if not bot:
        return

    try:
        chat_target = normalize_channel_id(admin_id)
        await bot.delete_message(chat_id=chat_target, message_id=admin_msg_id)
    except Exception as e:
        logger.debug(f"Could not delete admin message {admin_msg_id}: {e}")
        try:
            # If deletion is impossible (e.g. > 48h), edit into a clean published marker
            await bot.edit_message_caption(
                chat_id=normalize_channel_id(admin_id),
                message_id=admin_msg_id,
                caption="✅ <b>Опубликовано в канал.</b>",
                reply_markup=None,
            )
        except Exception:
            pass
    finally:
        await save_tweet_admin_message_id(tweet_id, None)


async def send_post_card_to_admin(tweet: Dict[str, Any]) -> Optional[int]:
    """
    Sends or re-sends an audited post card with media and approval buttons to admin.
    Saves the new admin_message_id to DB for 1:1 synchronization.
    """
    admin_id = getattr(settings, "telegram_admin_id", None)
    if not admin_id:
        return None

    bot = get_bot()
    if not bot:
        return None

    # Remove previous card if any (ensures no duplicate cards)
    try:
        await delete_admin_moderation_message(tweet["tweet_id"])
    except Exception:
        pass

    ai_data_raw = tweet.get("summary_json") or "{}"
    try:
        ai_data = json.loads(ai_data_raw)
    except Exception:
        ai_data = {}

    media_urls = tweet.get("media_urls", [])
    if isinstance(media_urls, str):
        try:
            media_urls = json.loads(media_urls)
        except Exception:
            media_urls = []

    video_data = tweet.get("video_data")
    if isinstance(video_data, str):
        try:
            video_data = json.loads(video_data)
        except Exception:
            video_data = None

    video_local_path = None
    video_thumbnail = None
    if video_data and isinstance(video_data, dict):
        video_local_path = video_data.get("local_path")
        video_thumbnail = video_data.get("thumbnail")

    selected_idx = ai_data.get("selected_image_index")
    photo_url = None
    if not video_local_path and selected_idx is not None and isinstance(selected_idx, int) and 0 <= selected_idx < len(media_urls):
        photo_url = media_urls[selected_idx]
    elif not video_local_path and media_urls:
        photo_url = media_urls[0]

    from src.ai.agents.publisher import format_telegram_post
    formatted_text = format_telegram_post(tweet, ai_data, is_photo=bool(photo_url) or bool(video_local_path))
    tweet_url = tweet.get("tweet_url", "")
    reply_markup = build_admin_keyboard(tweet["tweet_id"], tweet_url)

    sent_msg_id = None
    target_channel = normalize_channel_id(admin_id)

    try:
        if video_local_path:
            sent_msg_id = await send_video_to_channel(
                formatted_text,
                video_local_path,
                thumbnail_path=video_thumbnail,
                tweet_url=tweet_url,
                channel_id_override=target_channel,
                reply_markup_override=reply_markup,
            )
        elif photo_url:
            sent_msg_id = await send_photo_to_channel(
                formatted_text,
                photo_url,
                tweet_url,
                channel_id_override=target_channel,
                reply_markup_override=reply_markup,
            )
        else:
            sent_msg_id = await send_post_to_channel(
                formatted_text,
                tweet_url,
                channel_id_override=target_channel,
                reply_markup_override=reply_markup,
            )

        if sent_msg_id:
            await save_tweet_admin_message_id(tweet["tweet_id"], sent_msg_id)
        return sent_msg_id
    except Exception as e:
        logger.error(f"Error sending post card to admin: {e}")
        return None


async def _publish_approved_post(tweet_id: str, custom_comment: str = None, bot: Bot = None, callback: CallbackQuery = None):
    tweet = await get_tweet_by_id(tweet_id)
    if not tweet:
        return "Твит не найден в базе данных."

    if tweet.get("status") == "POSTED":
        return "Этот пост уже опубликован!"

    ai_data_raw = tweet.get("summary_json") or "{}"
    try:
        ai_data = json.loads(ai_data_raw)
    except Exception:
        ai_data = {}

    media_urls = tweet.get("media_urls", [])
    if isinstance(media_urls, str):
        try:
            media_urls = json.loads(media_urls)
        except Exception:
            media_urls = []

    video_data = tweet.get("video_data")
    if isinstance(video_data, str):
        try:
            video_data = json.loads(video_data)
        except Exception:
            video_data = None

    if custom_comment:
        original_text = ai_data.get("summary_ru", "")
        ai_data["summary_ru"] = f"🗣 <b>Комментарий:</b>\n{custom_comment}\n\n{original_text}"

    # Topic routing for channel destination
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
    elif not video_local_path and media_urls:
        photo_url = media_urls[0]

    from src.ai.agents.publisher import format_telegram_post
    formatted_text = format_telegram_post(tweet, ai_data, is_photo=bool(photo_url) or bool(video_local_path))
    tweet_url = tweet.get("tweet_url")

    # Send natively to public channel
    try:
        if video_local_path:
            await send_video_to_channel(formatted_text, video_local_path, thumbnail_path=video_thumbnail, tweet_url=tweet_url, channel_id_override=target_channel)
        elif photo_url:
            await send_photo_to_channel(formatted_text, photo_url, tweet_url, channel_id_override=target_channel)
        else:
            await send_post_to_channel(formatted_text, tweet_url, channel_id_override=target_channel)
    except Exception as e:
        return f"Ошибка публикации в канал: {str(e)}"

    # Update DB to POSTED
    await update_tweet_status_and_score(
        tweet_id=tweet_id,
        score=tweet.get("viral_score", 0.0),
        is_viral=True,
        status="POSTED",
        impact_score=ai_data.get("impact_score", 0),
        summary_json=json.dumps(ai_data, ensure_ascii=False)
    )

    # Automatically delete card from admin's moderation chat (Inbox Zero!)
    await delete_admin_moderation_message(tweet_id)
    return "✅ Пост успешно опубликован в канал!"


@dp.callback_query(F.data.startswith("pub:"))
async def handle_publish(callback: types.CallbackQuery):
    tweet_id = callback.data.split(":")[1]
    res = await _publish_approved_post(tweet_id, bot=callback.bot, callback=callback)
    try:
        # Delete the moderation message from chat so it disappears
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(res)
    await callback.answer()


@dp.callback_query(F.data.startswith("rej:"))
async def handle_reject(callback: types.CallbackQuery):
    tweet_id = callback.data.split(":")[1]
    tweet = await get_tweet_by_id(tweet_id)
    if tweet:
        ai_data_raw = tweet.get("summary_json") or "{}"
        try:
            ai_data = json.loads(ai_data_raw)
        except Exception:
            ai_data = {}
        await update_tweet_status_and_score(
            tweet_id=tweet_id,
            score=tweet.get("viral_score", 0.0),
            is_viral=True,
            status="REJECTED",
            impact_score=ai_data.get("impact_score", 0),
            summary_json=json.dumps(ai_data, ensure_ascii=False)
        )
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer("❌ Пост отклонен и удален из очереди.")
    else:
        await callback.answer("Твит не найден.", show_alert=True)
    await callback.answer()


@dp.callback_query(F.data.startswith("cmt:"))
async def handle_comment(callback: types.CallbackQuery, state: FSMContext):
    tweet_id = callback.data.split(":")[1]
    await state.update_data(tweet_id=tweet_id, orig_message_id=callback.message.message_id)
    await state.set_state(ModerationState.waiting_for_comment)
    await callback.message.reply("Отправьте ваш комментарий текстом (он добавится к посту перед публикацией) или нажмите /cancel:")
    await callback.answer()


@dp.message(ModerationState.waiting_for_comment)
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


@dp.callback_query(F.data.startswith("rew:"))
async def handle_rewrite_request(callback: types.CallbackQuery, state: FSMContext):
    """Admin clicks 'На доработку ИИ'."""
    tweet_id = callback.data.split(":")[1]
    await state.update_data(tweet_id=tweet_id, orig_message_id=callback.message.message_id)
    await state.set_state(ModerationState.waiting_for_ai_feedback)
    await callback.message.reply(
        "✍️ <b>Напишите указания для ИИ</b> (например: <i>«будь милее»</i>, <i>«без жести»</i>, <i>«сделай кликбейтнее»</i>) или отправьте /cancel:"
    )
    await callback.answer()


@dp.message(ModerationState.waiting_for_ai_feedback)
async def process_ai_rewrite_feedback(message: types.Message, state: FSMContext):
    """Processes editor's prompt instruction for AI rework."""
    if message.text == "/cancel":
        await state.clear()
        await message.reply("Отменено.")
        return

    data = await state.get_data()
    tweet_id = data.get("tweet_id")
    orig_msg_id = data.get("orig_message_id")
    feedback = message.text.strip()
    await state.clear()

    # 1. Update UI & chat status to PROCESSING
    status_msg = await message.reply(f"⏳ <b>ИИ перерабатывает пост...</b>\nПожелание: <i>«{feedback}»</i>")

    try:
        from src.ai.agents.editor import reprocess_tweet_with_editor_feedback
        rework_res = await reprocess_tweet_with_editor_feedback(tweet_id, feedback)

        # 2. Delete original message from chat to keep 1:1 clean queue
        if orig_msg_id:
            try:
                await message.bot.delete_message(chat_id=message.chat.id, message_id=orig_msg_id)
            except Exception:
                pass

        try:
            await status_msg.delete()
        except Exception:
            pass

        # 3. Re-send fresh post card (strictly audited) with active approval buttons
        updated_tweet = await get_tweet_by_id(tweet_id)
        if updated_tweet:
            await send_post_card_to_admin(updated_tweet)
            audit_info = rework_res.get("audit", {})
            char_count = audit_info.get("char_count", 0)
            max_limit = audit_info.get("max_limit", 1024)
            await message.answer(f"✅ <b>Пост переработан ИИ и прошел Технический аудит!</b> ({char_count}/{max_limit} симв.)\nПроверьте карточку выше.")
    except Exception as e:
        logger.error(f"Error during AI rework: {e}")
        await message.reply(f"❌ Ошибка доработки ИИ: {e}")


@dp.callback_query(F.data.startswith("edit_txt:"))
async def handle_manual_edit_request(callback: types.CallbackQuery, state: FSMContext):
    """Admin clicks 'Править подпись' (direct edit without AI)."""
    tweet_id = callback.data.split(":")[1]
    await state.update_data(tweet_id=tweet_id, orig_message_id=callback.message.message_id)
    await state.set_state(ModerationState.waiting_for_manual_caption)
    await callback.message.reply(
        "✏️ <b>Отправьте новый текст подписи</b> к посту (без вызова ИИ).\n"
        "Текст пройдет Технический аудит (до 1024 символов), обновится в админке, и появится кнопка «Опубликовать». Или отправьте /cancel:"
    )
    await callback.answer()


@dp.message(ModerationState.waiting_for_manual_caption)
async def process_manual_caption_edit(message: types.Message, state: FSMContext):
    """Processes direct manual edit by editor, audits it, saves to DB, and updates card."""
    if message.text == "/cancel":
        await state.clear()
        await message.reply("Отменено.")
        return

    data = await state.get_data()
    tweet_id = data.get("tweet_id")
    orig_msg_id = data.get("orig_message_id")
    new_text = message.text.strip()
    await state.clear()

    tweet = await get_tweet_by_id(tweet_id)
    if not tweet:
        await message.reply("❌ Твит не найден в базе данных.")
        return

    ai_data_raw = tweet.get("summary_json") or "{}"
    try:
        ai_data = json.loads(ai_data_raw)
    except Exception:
        ai_data = {}

    media_urls = tweet.get("media_urls", [])
    video_data = tweet.get("video_data")
    is_media = bool(media_urls or video_data)

    # 1. Technical Audit on editor's manual text
    audit_res = audit_and_prepare_telegram_content(
        title=ai_data.get("title_ru", ""),
        summary=new_text,
        takeaways=ai_data.get("takeaways", []),
        hashtags=ai_data.get("hashtags", []),
        is_media=is_media,
        author_handle=tweet.get("author_handle"),
        tweet_url=tweet.get("tweet_url"),
    )

    ai_data["summary_ru"] = audit_res["summary_ru"]
    ai_data["takeaways"] = audit_res["takeaways"]
    ai_data["hashtags"] = audit_res["hashtags"]
    ai_data["technical_audit"] = {
        "is_valid": audit_res["is_valid"],
        "char_count": audit_res["char_count"],
        "max_limit": audit_res["max_limit"],
        "warnings": audit_res["warnings"],
    }

    # 2. Save directly to DB -> status PENDING_APPROVAL
    new_status = "PENDING_APPROVAL" if getattr(settings, "approval_required", False) else "PROCESSED"
    await update_tweet_status_and_score(
        tweet_id=tweet_id,
        score=tweet.get("viral_score", 0.0),
        is_viral=True,
        status=new_status,
        impact_score=ai_data.get("impact_score", 0),
        summary_json=json.dumps(ai_data, ensure_ascii=False),
    )

    # 3. Delete old card and send updated card with active 'Опубликовать' button
    if orig_msg_id:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=orig_msg_id)
        except Exception:
            pass

    updated_tweet = await get_tweet_by_id(tweet_id)
    if updated_tweet:
        await send_post_card_to_admin(updated_tweet)
        char_count = audit_res.get("char_count", 0)
        max_limit = audit_res.get("max_limit", 1024)
        await message.answer(f"✅ <b>Подпись обновлена и прошла Технический аудит!</b> ({char_count}/{max_limit} симв.)\nПост обновлен в админке. Нажмите «Опубликовать», когда будете готовы.")


# ===========================================================================
# Admin Commands (/start, /sync, /status, /cancel)
# ===========================================================================

@dp.message(F.text.startswith("/start"))
async def handle_start_command(message: types.Message):
    user_id = message.from_user.id
    current_admin_id = getattr(settings, "telegram_admin_id", None)
    approval_required = getattr(settings, "approval_required", False)

    is_admin = False
    if current_admin_id and str(current_admin_id).strip() == str(user_id):
        is_admin = True
    elif not current_admin_id:
        # Automatically bind this user as admin if none is configured
        settings.telegram_admin_id = str(user_id)
        settings.approval_required = True
        try:
            from src.db import save_system_settings_dual
            await save_system_settings_dual("telegram_admin_id", str(user_id))
            await save_system_settings_dual("approval_required", "true")
        except Exception:
            pass
        is_admin = True
        current_admin_id = str(user_id)
        approval_required = True

    admin_str = f"<code>{current_admin_id}</code>" if current_admin_id else "<i>не задан</i>"
    approval_str = "<b>ВКЛЮЧЕНО ✅</b>" if str(approval_required).lower() in ("true", "1") else "<b>ВЫКЛЮЧЕНО ⚪</b>"

    text = (
        f"👋 <b>Центр модерации Viral Hunter</b>\n\n"
        f"👤 <b>Ваш Telegram ID:</b> <code>{user_id}</code>\n"
        f"⚙️ <b>ID администратора в системе:</b> {admin_str}\n"
        f"🛡 <b>Согласование постов:</b> {approval_str}\n\n"
    )

    if is_admin:
        text += (
            f"✅ <b>Вы авторизованы как Администратор / Главный Редактор!</b>\n\n"
            f"📋 <b>Команды:</b>\n"
            f"• <b>/sync</b> — 🔄 <b>Принудительно прислать все посты из очереди</b> на согласование\n"
            f"• <b>/status</b> — 📊 Проверить состояние очереди и системы\n"
            f"• <b>/cancel</b> — ❌ Сбросить текущее действие (правку/комментарий)\n\n"
            f"💡 <i>Отправьте <b>/sync</b> прямо сейчас, чтобы получить все карточки, ожидающие публикации!</i>"
        )
    else:
        text += (
            f"⚠️ <b>Внимание:</b> ваш ID (<code>{user_id}</code>) не совпадает с администратором системы.\n\n"
            f"Чтобы получать посты на модерацию:\n"
            f"1. Откройте веб-панель Viral Hunter\n"
            f"2. Перейдите в раздел <b>Настройки</b>\n"
            f"3. Укажите в поле <b>Telegram ID администратора:</b> <code>{user_id}</code>\n"
            f"4. Включите переключатель <b>«Требовать согласование перед публикацией»</b>\n"
            f"5. Сохраните настройки и отправьте <b>/sync</b>"
        )

    await message.reply(text)


@dp.message(F.text.startswith("/sync"))
async def handle_sync_command(message: types.Message):
    user_id = message.from_user.id
    current_admin_id = getattr(settings, "telegram_admin_id", None)

    # If not set, bind this user
    if not current_admin_id:
        settings.telegram_admin_id = str(user_id)
        settings.approval_required = True
        try:
            from src.db import save_system_settings_dual
            await save_system_settings_dual("telegram_admin_id", str(user_id))
            await save_system_settings_dual("approval_required", "true")
        except Exception:
            pass
        current_admin_id = str(user_id)

    if str(current_admin_id).strip() != str(user_id):
        await message.reply(
            f"⛔ <b>Доступ ограничен.</b>\n"
            f"Ваш ID (<code>{user_id}</code>) не совпадает с ID администратора (<code>{current_admin_id}</code>).\n"
            f"Укажите ваш ID <code>{user_id}</code> в настройках веб-панели."
        )
        return

    progress_msg = await message.reply("⏳ <b>Запускаю синхронизацию очереди...</b>\nИщу неопубликованные посты...")

    try:
        from src.db import get_queued_posts
        queued = await get_queued_posts()
        if not queued:
            await progress_msg.edit_text(
                "📭 <b>В очереди нет постов для публикации.</b>\n"
                "Запустите сбор твитов агентом Scout в веб-панели управления, чтобы наполнить очередь свежим контентом."
            )
            return

        sent_count = 0
        failed_count = 0
        for post in queued:
            try:
                msg_id = await send_post_card_to_admin(post)
                if msg_id:
                    sent_count += 1
                else:
                    failed_count += 1
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.error(f"Error syncing post {post.get('tweet_id')}: {e}")
                failed_count += 1

        res_text = (
            f"✅ <b>Синхронизация завершена!</b>\n\n"
            f"📥 Всего в очереди: <b>{len(queued)}</b>\n"
            f"📬 Карточек отправлено админу: <b>{sent_count}</b>\n"
        )
        if failed_count > 0:
            res_text += f"⚠️ Ошибок отправки: {failed_count}\n"
        res_text += f"\nКарточки выше готовы к модерации и публикации."
        await progress_msg.edit_text(res_text)
    except Exception as e:
        logger.error(f"Sync command error: {e}")
        await progress_msg.edit_text(f"❌ Ошибка синхронизации: {e}")


@dp.message(F.text.startswith("/status"))
async def handle_status_command(message: types.Message):
    from src.db import get_queued_posts, get_published_posts
    from src.ai.agents.editor import get_ai_status

    queued = await get_queued_posts()
    published = await get_published_posts(limit=10)
    ai_status = get_ai_status()

    ai_state = ai_status.get("status", "unknown")
    ai_msg = ai_status.get("message", "Нет данных")

    text = (
        f"📊 <b>Статус системы Viral Hunter</b>\n\n"
        f"📬 Постов в очереди (ожидают): <b>{len(queued)}</b>\n"
        f"🚀 Опубликовано (последних): <b>{len(published)}</b>\n"
        f"🤖 Состояние ИИ: <b>{ai_state.upper()}</b> ({ai_msg})\n"
        f"🕒 Серверное время: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>\n\n"
        f"Отправьте /sync для синхронизации очереди."
    )
    await message.reply(text)


@dp.message(F.text.startswith("/cancel"))
async def handle_cancel_command(message: types.Message, state: FSMContext):
    await state.clear()
    await message.reply("Действие отменено.")
