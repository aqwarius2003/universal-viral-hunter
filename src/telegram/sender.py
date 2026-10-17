import os
from typing import Union, Any, Optional
try:
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, URLInputFile, FSInputFile
except ImportError:
    Bot = None
    DefaultBotProperties = None
    ParseMode = None
    InlineKeyboardMarkup = None
    InlineKeyboardButton = None
    URLInputFile = None
    FSInputFile = None
from src.config import settings
from src.utils.hashtags import enforce_hashtags_in_post

MAX_CAPTION_LENGTH = 1024


def normalize_channel_id(channel_id: Union[str, int, None]) -> Union[str, int]:
    """
    Normalizes telegram channel identifier:
    - If channel starts with @ or is a negative int (e.g. -100...), leaves as is.
    - If channel is a string name without @ and not starting with -, prepends '@'.
    - If channel is a pure positive integer starting with 100 and >= 12 digits, converts to -int (channel missing minus).
    - If channel is a user ID or standard chat ID, returns as positive int.
    """
    if not channel_id:
        raise ValueError("ID Telegram-канала (TELEGRAM_CHANNEL_ID) не задан в конфигурации или настройках.")
    
    raw = str(channel_id).strip()
    if not raw:
        raise ValueError("ID Telegram-канала (TELEGRAM_CHANNEL_ID) пустой.")
    
    if raw.startswith("-"):
        try:
            return int(raw)
        except ValueError:
            return raw
    
    if raw.isdigit():
        val = int(raw)
        if len(raw) >= 12 and raw.startswith("100"):
            return -val
        return val
        
    if not raw.startswith("@"):
        return f"@{raw}"
        
    return raw


def get_friendly_telegram_error(err: Exception) -> str:
    """Translates Telegram API errors into actionable Russian messages."""
    err_str = str(err)
    err_lower = err_str.lower()
    if "chat not found" in err_lower:
        return (
            "Telegram-канал не найден. Проверьте правильность ID или юзернейма канала (например: @my_channel или -1001234567890) "
            "и убедитесь, что бот добавлен в канал и назначен администратором с правом публикации сообщений."
        )
    if "unauthorized" in err_lower or "token is invalid" in err_lower:
        return (
            "Неверный токен Telegram-бота. Проверьте параметр TELEGRAM_BOT_TOKEN в файле .env или во вкладке 'Настройки'."
        )
    if "not enough rights" in err_lower or "have no rights" in err_lower or "bot is not a member" in err_lower or "kicked" in err_lower:
        return (
            "Бот не имеет прав для отправки сообщений в канал. Добавьте бота в канал и выдайте ему права администратора на публикацию."
        )
    if "can't parse entities" in err_lower or "entity" in err_lower:
        return (
            "Ошибка разметки сообщения Telegram (HTML)."
        )
    if "message is too long" in err_lower:
        return (
            "Текст сообщения превышает допустимый лимит Telegram."
        )
    return f"Ошибка отправки в Telegram: {err_str}"


async def send_post_to_channel(text: str, tweet_url: Optional[str] = None, channel_id_override: Optional[str] = None, reply_markup_override: Optional[InlineKeyboardMarkup] = None) -> Optional[int]:
    """
    Sends a formatted text message with an optional inline button to the configured Telegram channel.
    Returns the Telegram message_id if sent successfully.
    """
    text = enforce_hashtags_in_post(text)
    if not settings.telegram_bot_token or "your_bot_token" in settings.telegram_bot_token:
        raise ValueError("Токен Telegram-бота (TELEGRAM_BOT_TOKEN) не задан. Укажите действующий токен бота в настройках.")

    channel_id = normalize_channel_id(channel_id_override or settings.telegram_channel_id)
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    keyboard = reply_markup_override
    if not keyboard and tweet_url and getattr(settings, "show_metric_source_btn", True):
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔗 Читать оригинал в X", url=tweet_url)]
            ]
        )

    try:
        sent_msg = await bot.send_message(
            chat_id=channel_id,
            text=text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        return getattr(sent_msg, "message_id", None)
    except Exception as e:
        friendly = get_friendly_telegram_error(e)
        raise RuntimeError(friendly) from e
    finally:
        await bot.session.close()


async def send_photo_to_channel(text: str, photo_url: str, tweet_url: Optional[str] = None, channel_id_override: Optional[str] = None, reply_markup_override: Optional[InlineKeyboardMarkup] = None) -> Optional[int]:
    """
    Sends a photo with caption to the configured Telegram channel.
    If the text exceeds Telegram's 1024-character caption limit, sends the photo
    and follows up with the full text message to preserve formatting and links.
    Returns the Telegram message_id if sent successfully.
    """
    text = enforce_hashtags_in_post(text)
    if not settings.telegram_bot_token or "your_bot_token" in settings.telegram_bot_token:
        raise ValueError("Токен Telegram-бота (TELEGRAM_BOT_TOKEN) не задан. Укажите действующий токен бота в настройках.")

    channel_id = normalize_channel_id(channel_id_override or settings.telegram_channel_id)
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    keyboard = reply_markup_override
    if not keyboard and tweet_url and getattr(settings, "show_metric_source_btn", True):
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔗 Читать оригинал в X", url=tweet_url)]
            ]
        )

    try:
        if isinstance(photo_url, str):
            if photo_url.startswith("http"):
                photo = URLInputFile(photo_url)
            elif os.path.isfile(photo_url):
                photo = FSInputFile(photo_url)
            else:
                photo = None
        else:
            photo = photo_url

        if not photo:
            sent_msg = await bot.send_message(
                chat_id=channel_id,
                text=text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            return getattr(sent_msg, "message_id", None)

        max_caption = getattr(settings, "telegram_max_caption_len", MAX_CAPTION_LENGTH)
        if len(text) <= max_caption:
            sent_msg = await bot.send_photo(
                chat_id=channel_id,
                photo=photo,
                caption=text,
                reply_markup=keyboard,
            )
            return getattr(sent_msg, "message_id", None)
        else:
            # When caption exceeds limit, send photo first, then full post
            await bot.send_photo(
                chat_id=channel_id,
                photo=photo,
            )
            sent_msg = await bot.send_message(
                chat_id=channel_id,
                text=text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            return getattr(sent_msg, "message_id", None)
    except Exception as photo_err:
        err_lower = str(photo_err).lower()
        if "chat not found" in err_lower or "unauthorized" in err_lower or "rights" in err_lower:
            friendly = get_friendly_telegram_error(photo_err)
            raise RuntimeError(friendly) from photo_err

        print(f"⚠️ Ошибка отправки фото в Telegram: {photo_err}. Отправка текстовым постом.")
        try:
            sent_msg = await bot.send_message(
                chat_id=channel_id,
                text=text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            return getattr(sent_msg, "message_id", None)
        except Exception as text_err:
            friendly = get_friendly_telegram_error(text_err)
            raise RuntimeError(friendly) from text_err
    finally:
        await bot.session.close()


async def send_video_to_channel(text: str, video_path: str, thumbnail_path: Optional[str] = None, tweet_url: Optional[str] = None, channel_id_override: Optional[str] = None, reply_markup_override: Optional[InlineKeyboardMarkup] = None) -> Optional[int]:
    """
    Sends a video with caption to the configured Telegram channel.
    If the text exceeds Telegram's 1024-character caption limit, sends the video
    and follows up with the full text message.
    """
    text = enforce_hashtags_in_post(text)
    if not settings.telegram_bot_token or "your_bot_token" in settings.telegram_bot_token:
        raise ValueError("Токен Telegram-бота (TELEGRAM_BOT_TOKEN) не задан.")

    channel_id = normalize_channel_id(channel_id_override or settings.telegram_channel_id)
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    keyboard = reply_markup_override
    if not keyboard and tweet_url and getattr(settings, "show_metric_source_btn", True):
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔗 Читать оригинал в X", url=tweet_url)]]
        )

    max_caption = getattr(settings, "telegram_max_caption_len", MAX_CAPTION_LENGTH)
    try:
        video = FSInputFile(video_path) if not video_path.startswith("http") else URLInputFile(video_path)
        thumb = None
        if thumbnail_path:
            if not thumbnail_path.startswith("http"):
                if os.path.isfile(thumbnail_path):
                    thumb = FSInputFile(thumbnail_path)
            else:
                thumb = URLInputFile(thumbnail_path)
        
        if len(text) <= max_caption:
            sent_msg = await bot.send_video(
                chat_id=channel_id,
                video=video,
                thumbnail=thumb,
                caption=text,
                reply_markup=keyboard,
                supports_streaming=True
            )
            return getattr(sent_msg, "message_id", None)
        else:
            await bot.send_video(
                chat_id=channel_id,
                video=video,
                thumbnail=thumb,
                supports_streaming=True
            )
            sent_msg = await bot.send_message(
                chat_id=channel_id,
                text=text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            return getattr(sent_msg, "message_id", None)
    except Exception as e:
        err_lower = str(e).lower()
        if "chat not found" in err_lower or "unauthorized" in err_lower or "rights" in err_lower or "kicked" in err_lower:
            friendly = get_friendly_telegram_error(e)
            raise RuntimeError(friendly) from e

        # If thumbnail caused failure, retry without thumbnail
        if thumb is not None:
            try:
                print(f"⚠️ Ошибка отправки видео с миниатюрой ({e}), повторная попытка без миниатюры...")
                if len(text) <= max_caption:
                    sent_msg = await bot.send_video(
                        chat_id=channel_id,
                        video=video,
                        thumbnail=None,
                        caption=text,
                        reply_markup=keyboard,
                        supports_streaming=True
                    )
                    return getattr(sent_msg, "message_id", None)
                else:
                    await bot.send_video(
                        chat_id=channel_id,
                        video=video,
                        thumbnail=None,
                        supports_streaming=True
                    )
                    sent_msg = await bot.send_message(
                        chat_id=channel_id,
                        text=text,
                        reply_markup=keyboard,
                        disable_web_page_preview=True,
                    )
                    return getattr(sent_msg, "message_id", None)
            except Exception as retry_err:
                e = retry_err
                err_lower = str(e).lower()
                if "chat not found" in err_lower or "unauthorized" in err_lower or "rights" in err_lower:
                    friendly = get_friendly_telegram_error(e)
                    raise RuntimeError(friendly) from e

        # Fallback if video itself fails (e.g. >50MB limit or format issue): send as photo or text
        print(f"⚠️ Ошибка отправки видео в Telegram: {e}. Попытка резервной отправки фото/текстом.")
        try:
            if thumbnail_path:
                photo = URLInputFile(thumbnail_path) if thumbnail_path.startswith("http") else (FSInputFile(thumbnail_path) if os.path.isfile(thumbnail_path) else None)
                if photo:
                    if len(text) <= max_caption:
                        sent_msg = await bot.send_photo(
                            chat_id=channel_id,
                            photo=photo,
                            caption=text,
                            reply_markup=keyboard,
                        )
                        return getattr(sent_msg, "message_id", None)
                    else:
                        await bot.send_photo(chat_id=channel_id, photo=photo)
                        sent_msg = await bot.send_message(
                            chat_id=channel_id,
                            text=text,
                            reply_markup=keyboard,
                            disable_web_page_preview=True,
                        )
                        return getattr(sent_msg, "message_id", None)
            # Text fallback
            sent_msg = await bot.send_message(
                chat_id=channel_id,
                text=text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            return getattr(sent_msg, "message_id", None)
        except Exception as fallback_err:
            friendly = get_friendly_telegram_error(fallback_err)
            raise RuntimeError(friendly) from fallback_err
    finally:
        await bot.session.close()