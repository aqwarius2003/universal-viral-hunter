import asyncio
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional
from src.config import settings
from src.db import init_db, load_system_settings_into_config, is_secret_placeholder_or_empty
from src.ai.agents.editor import get_ai_status, set_ai_status
from src.ai.agents.scout import get_twitter_client, load_cookies_from_file


def _resolve_dep(name: str, default: Any) -> Any:
    server = sys.modules.get("src.web.server")
    if server and hasattr(server, name):
        return getattr(server, name)
    return default


async def check_twitter_health() -> Dict[str, Any]:
    """
    Checks Twitter / X connection and credentials without throwing unhandled exceptions.
    Returns dict with status ('ok', 'warning', 'error'), message, and details.
    """
    cookies_path = Path("cookies.json")
    cookies_file_exists = cookies_path.is_file()
    _load_cookies = _resolve_dep("load_cookies_from_file", load_cookies_from_file)
    cookies_dict = _load_cookies(cookies_path) if cookies_file_exists else {}

    has_env_token = bool(
        settings.twitter_auth_token
        and len(settings.twitter_auth_token.strip()) > 5
        and "your_" not in settings.twitter_auth_token
    )
    has_env_ct0 = bool(
        settings.twitter_ct0
        and len(settings.twitter_ct0.strip()) > 5
        and "your_" not in settings.twitter_ct0
    )

    auth_token = (settings.twitter_auth_token if has_env_token else None) or cookies_dict.get("auth_token")
    ct0 = (settings.twitter_ct0 if has_env_ct0 else None) or cookies_dict.get("ct0")

    if not auth_token or not ct0 or "your_" in str(auth_token) or "your_" in str(ct0):
        return {
            "name": "X.com (Twitter)",
            "status": "error",
            "message": "Сессия не настроена (отсутствуют cookies.json и TWITTER_AUTH_TOKEN/TWITTER_CT0)",
            "details": {
                "cookies_file": cookies_file_exists,
                "env_token": has_env_token,
                "env_ct0": has_env_ct0,
            },
        }

    source = "cookies.json" if cookies_file_exists and not (has_env_token and has_env_ct0) else ".env"

    try:
        _getter = _resolve_dep("get_twitter_client", get_twitter_client)
        client = await _getter()
        screen_name = None

        use_v11 = False
        if "v11" in getattr(client, "__dict__", {}) or (hasattr(client, "v11") and type(getattr(client, "v11", None)).__name__ == "V11Client"):
            if hasattr(client.v11, "settings") and callable(client.v11.settings):
                use_v11 = True

        if use_v11:
            try:
                res = await asyncio.wait_for(client.v11.settings(), timeout=6.0)
                settings_res = res[0] if isinstance(res, (tuple, list)) else res
                if isinstance(settings_res, dict):
                    screen_name = settings_res.get("screen_name")
            except Exception as v11_err:
                err_str = str(v11_err).lower()
                if any(x in err_str for x in ("unauthorized", "401", "403", "could not authenticate", "expired", "forbidden", "locked", "suspended")):
                    return {
                        "name": "X.com (Twitter)",
                        "status": "error",
                        "message": f"Сессия недействительна: cookies истекли ({v11_err})",
                        "details": {"source": source, "error": str(v11_err)},
                    }
                return {
                    "name": "X.com (Twitter)",
                    "status": "warning",
                    "message": f"Cookies загружены ({source}), но X.com не ответил: {v11_err}",
                    "details": {"source": source, "error": str(v11_err)},
                }
        elif hasattr(client, "user") and callable(client.user):
            # Fallback for mocked clients or clients where user() is defined
            try:
                user = await asyncio.wait_for(client.user(), timeout=6.0)
                if user:
                    screen_name = getattr(user, "screen_name", None) or getattr(user, "name", None)
            except Exception as user_err:
                err_str = str(user_err).lower()
                if any(x in err_str for x in ("unauthorized", "401", "403", "could not authenticate", "expired", "forbidden", "locked", "suspended")):
                    return {
                        "name": "X.com (Twitter)",
                        "status": "error",
                        "message": f"Сессия недействительна: cookies истекли ({user_err})",
                        "details": {"source": source, "error": str(user_err)},
                    }
                return {
                    "name": "X.com (Twitter)",
                    "status": "warning",
                    "message": f"Cookies загружены ({source}), но X.com не ответил: {user_err}",
                    "details": {"source": source, "error": str(user_err)},
                }

        msg = f"Сессия активна ({source} подключен)"
        if screen_name:
            msg = f"Сессия активна (@{screen_name}, {source} подключен)"

        return {
            "name": "X.com (Twitter)",
            "status": "ok",
            "message": msg,
            "details": {
                "source": source,
                "screen_name": screen_name,
            },
        }
    except Exception as e:
        return {
            "name": "X.com (Twitter)",
            "status": "error",
            "message": f"Ошибка инициализации клиента X: {e}",
            "details": {"error": str(e)},
        }


async def check_gemini_health() -> Dict[str, Any]:
    """
    Checks Google Gemini API readiness, API key, active model, and rate limit status.
    """
    await init_db()
    await load_system_settings_into_config()
    key = settings.ai_api_key
    if not key or is_secret_placeholder_or_empty("ai_api_key", key) or len(key.strip()) < 8:
        return {
            "name": "Google Gemini",
            "status": "error",
            "message": "AI_API_KEY не задан или содержит шаблонное значение в .env",
            "details": {"model": settings.ai_model_name, "key_present": False},
        }

    status_getter = _resolve_dep("get_ai_status", get_ai_status)
    status_setter = _resolve_dep("set_ai_status", set_ai_status)

    ai_stat = status_getter()
    current_status = ai_stat.get("status", "ok")
    model = settings.ai_model_name or "gemini-3.5-flash-lite"
    delay = settings.ai_request_delay_seconds

    model_details = {
        "model": model,
        "primary_model": settings.primary_model,
        "fallback_model": settings.fallback_model,
        "delay_setting": delay,
        "ai_status": ai_stat,
    }

    if current_status == "error":
        return {
            "name": "Google Gemini",
            "status": "error",
            "message": f"Ошибка Gemini API: {ai_stat.get('message', 'Неизвестная ошибка')}",
            "details": model_details,
        }
    elif current_status == "waiting":
        return {
            "name": "Google Gemini",
            "status": "warning",
            "message": f"Gemini {model}: ожидание лимитов ({ai_stat.get('waiting_seconds', 0)}с)",
            "details": model_details,
        }
    else:
        if current_status == "pending":
            status_setter("ok", "Готов к работе")
            ai_stat = status_getter()
            model_details["ai_status"] = ai_stat
        return {
            "name": "Google Gemini",
            "status": "ok",
            "message": f"Gemini {model} готов",
            "details": model_details,
        }


async def check_telegram_health() -> Dict[str, Any]:
    """
    Checks Telegram Bot token, connectivity via bot.get_me(), and channel permissions via bot.get_chat_administrators().
    """
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from src.telegram.sender import normalize_channel_id, get_friendly_telegram_error

    bot_token = settings.telegram_bot_token
    channel_id_raw = settings.telegram_channel_id

    if not bot_token or "your_bot_token" in bot_token or len(bot_token.strip()) < 10:
        return {
            "name": "Telegram",
            "status": "error",
            "message": "TELEGRAM_BOT_TOKEN не задан в .env или настройках",
            "details": {"bot_token_present": False},
        }

    bot = Bot(token=bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me = await asyncio.wait_for(bot.get_me(), timeout=6.0)
        bot_username = me.username or str(me.id)

        if not channel_id_raw or "your_channel" in channel_id_raw or not str(channel_id_raw).strip():
            return {
                "name": "Telegram",
                "status": "warning",
                "message": f"Бот @{bot_username} активен, но TELEGRAM_CHANNEL_ID не задан в .env или настройках",
                "details": {
                    "bot_username": bot_username,
                    "bot_id": me.id,
                    "channel_id_present": False,
                },
            }

        try:
            norm_channel = normalize_channel_id(channel_id_raw)
            chat = await asyncio.wait_for(bot.get_chat(norm_channel), timeout=6.0)
            chat_title = chat.title or str(channel_id_raw)

            admins = await asyncio.wait_for(bot.get_chat_administrators(norm_channel), timeout=6.0)
            bot_admin = next(
                (admin for admin in admins if getattr(admin, "user", None) and admin.user.id == me.id),
                None
            )

            if bot_admin is not None:
                is_owner = getattr(bot_admin, "status", "") == "creator"
                can_post = getattr(bot_admin, "can_post_messages", True)
                if can_post is None:
                    can_post = True

                if not is_owner and not can_post:
                    return {
                        "name": "Telegram",
                        "status": "warning",
                        "message": f"Бот @{bot_username} добавлен в канал {chat_title}, но не имеет права на публикацию сообщений",
                        "details": {
                            "bot_username": bot_username,
                            "bot_id": me.id,
                            "channel": chat_title,
                            "is_admin": True,
                            "can_post": False,
                        },
                    }

                return {
                    "name": "Telegram",
                    "status": "ok",
                    "message": f"Бот @{bot_username} — администратор канала {chat_title}",
                    "details": {
                        "bot_username": bot_username,
                        "bot_id": me.id,
                        "channel": chat_title,
                        "is_admin": True,
                        "can_post": True,
                    },
                }
            else:
                return {
                    "name": "Telegram",
                    "status": "warning",
                    "message": f"Бот @{bot_username} найден, но не является администратором канала {chat_title}",
                    "details": {
                        "bot_username": bot_username,
                        "bot_id": me.id,
                        "channel": chat_title,
                        "is_admin": False,
                    },
                }
        except Exception as chat_err:
            friendly_err = get_friendly_telegram_error(chat_err)
            return {
                "name": "Telegram",
                "status": "warning",
                "message": f"Бот @{bot_username} активен, но канал недоступен: {friendly_err}",
                "details": {
                    "bot_username": bot_username,
                    "bot_id": me.id,
                    "channel_error": str(chat_err),
                },
            }
    except Exception as e:
        friendly_err = get_friendly_telegram_error(e)
        return {
            "name": "Telegram",
            "status": "error",
            "message": friendly_err,
            "details": {"error": str(e)},
        }
    finally:
        await bot.session.close()


def _compute_overall_status(services: Dict[str, Dict[str, Any]]) -> str:
    """Computes overall service health status ('ok', 'warning', 'error')."""
    statuses = [s.get("status") for s in services.values()]
    if any(st == "error" for st in statuses):
        return "error"
    if any(st == "warning" for st in statuses):
        return "warning"
    return "ok"
