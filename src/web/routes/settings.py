import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, Optional
from aiohttp import web
from src.config import settings
from src.auth import mask_secret
from src.db import (
    init_db,
    load_system_settings_into_config,
    save_system_settings_dual,
    get_digest_schedules,
    save_digest_schedule,
    delete_digest_schedule,
    get_topics,
    create_topic,
    update_topic,
    delete_topic,
    is_secret_placeholder_or_empty,
)


async def handle_system_backup(request: web.Request) -> web.Response:
    """Downloads the active SQLite database file as a backup."""
    db_file = Path(settings.db_path)
    if not db_file.is_file():
        return web.json_response({"error": "Файл базы данных не найден"}, status=404)
    return web.FileResponse(
        db_file,
        headers={"Content-Disposition": 'attachment; filename="tweets_backup.sqlite"'}
    )


async def handle_get_settings(request: web.Request) -> web.Response:
    """Returns active settings with sensitive credentials masked."""
    await init_db()
    await load_system_settings_into_config()
    return web.json_response({
        "min_viral_score": settings.min_viral_score,
        "super_viral_threshold": getattr(settings, "super_viral_threshold", 100.0),
        "max_posts_per_run": settings.max_posts_per_run,
        "scheduler_interval_minutes": settings.scheduler_interval_minutes,
        "ai_request_delay_seconds": settings.ai_request_delay_seconds,
        "daily_digest_enabled": settings.daily_digest_enabled,
        "daily_digest_time": settings.daily_digest_time,
        "quiet_hours_enabled": settings.quiet_hours_enabled,
        "quiet_hours_start": settings.quiet_hours_start,
        "quiet_hours_end": settings.quiet_hours_end,
        "digest_pre_sleep_minutes": settings.digest_pre_sleep_minutes,
        "digest_block_interval_minutes": settings.digest_block_interval_minutes,
        "daytime_publish_interval_minutes": settings.daytime_publish_interval_minutes,
        "ai_model_name": settings.ai_model_name,
        "telegram_channel_id": settings.telegram_channel_id or "",
        "ai_api_key": mask_secret(settings.ai_api_key),
        "telegram_bot_token": mask_secret(settings.telegram_bot_token),
        "twitter_username": settings.twitter_username or "",
        "twitter_email": settings.twitter_email or "",
        "twitter_password": mask_secret(settings.twitter_password) if settings.twitter_password else "",
        "twitter_proxy_url": settings.twitter_proxy_url or "",
        "is_configured": settings.is_configured(),
        "x_search_queries": getattr(settings, "x_search_queries", ""),
        "x_relevance_prompt": getattr(settings, "x_relevance_prompt", ""),
        "scout_interval_minutes": getattr(settings, "scout_interval_minutes", None),
        "metric_refresh_hours": getattr(settings, "metric_refresh_hours", 48),
        "metric_refresh_limit": getattr(settings, "metric_refresh_limit", 20),
        "editor_system_prompt": getattr(settings, "editor_system_prompt", ""),
        "editor_prompt_template": getattr(settings, "editor_prompt_template", ""),
        "show_metric_likes": getattr(settings, "show_metric_likes", True),
        "show_metric_reposts": getattr(settings, "show_metric_reposts", True),
        "show_metric_views": getattr(settings, "show_metric_views", True),
        "show_metric_impact": getattr(settings, "show_metric_impact", True),
        "show_metric_author": getattr(settings, "show_metric_author", True),
        "show_metric_hashtags": getattr(settings, "show_metric_hashtags", True),
        "show_metric_source_btn": getattr(settings, "show_metric_source_btn", True),
        "max_digest_items": getattr(settings, "max_digest_items", 10),
        "queue_sort_strategy": getattr(settings, "queue_sort_strategy", "BY_VIRAL_SCORE"),
        "backlog_digest_enabled": getattr(settings, "backlog_digest_enabled", True),
        "backlog_max_chunk_size": getattr(settings, "backlog_max_chunk_size", 6),
        "app_timezone": getattr(settings, "app_timezone", None),
        "video_ttl_days": getattr(settings, "video_ttl_days", 2),
        "video_max_quality": getattr(settings, "video_max_quality", 720),
        "video_max_size_mb": getattr(settings, "video_max_size_mb", 50),
        "telegram_admin_id": getattr(settings, "telegram_admin_id", ""),
        "approval_required": str(getattr(settings, "approval_required", "false")).lower() == "true",
        "show_metric_takeaways": getattr(settings, "show_metric_takeaways", False),
        "max_title_words": getattr(settings, "max_title_words", 25),
        "telegram_max_caption_len": getattr(settings, "telegram_max_caption_len", 1024),
        "telegram_max_text_len": getattr(settings, "telegram_max_text_len", 4096),
        "ai_temperature": getattr(settings, "ai_temperature", 0.3),
        "max_hashtags": getattr(settings, "max_hashtags", 5),
        "scout_search_count": getattr(settings, "scout_search_count", 10),
        "scout_search_mode": getattr(settings, "scout_search_mode", "BOTH"),
    })


def _normalize_digest_schedule_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    raw_time = str(data.get("time", "")).strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", raw_time):
        raise ValueError("Время должно быть в формате HH:MM")
    title = str(data.get("title", "Дайджест новостей")).strip() or "Дайджест новостей"
    sort_order = str(data.get("sort_order", "BY_VIRAL_SCORE")).strip()
    if sort_order not in ("BY_VIRAL_SCORE", "CHRONOLOGICAL_DESC", "CHRONOLOGICAL_ASC", "RANDOM"):
        raise ValueError("Недопустимый порядок сортировки")
    return {
        "time": raw_time,
        "title": title,
        "max_items_per_post": max(1, min(50, int(data.get("max_items_per_post", 5)))),
        "max_posts": max(1, min(20, int(data.get("max_posts", 1)))),
        "sort_order": sort_order,
        "interval_minutes": max(0, min(1440, int(data.get("interval_minutes", 5)))),
        "enabled": bool(data.get("enabled", True)),
    }


async def handle_get_digest_schedules(request: web.Request) -> web.Response:
    await init_db()
    return web.json_response(await get_digest_schedules())


async def handle_create_digest_schedule(request: web.Request) -> web.Response:
    try:
        payload = _normalize_digest_schedule_payload(await request.json())
        schedule_id = await save_digest_schedule(payload)
        return web.json_response({"status": "created", "id": schedule_id, "schedule": payload}, status=201)
    except (ValueError, TypeError, json.JSONDecodeError) as err:
        return web.json_response({"error": str(err)}, status=400)


async def handle_update_digest_schedule(request: web.Request) -> web.Response:
    try:
        payload = _normalize_digest_schedule_payload(await request.json())
        schedule_id = int(request.match_info["id"])
        await save_digest_schedule(payload, schedule_id=schedule_id)
        return web.json_response({"status": "updated", "id": schedule_id})
    except (ValueError, TypeError, json.JSONDecodeError) as err:
        return web.json_response({"error": str(err)}, status=400)


async def handle_delete_digest_schedule(request: web.Request) -> web.Response:
    await delete_digest_schedule(int(request.match_info["id"]))
    return web.json_response({"status": "deleted"})


def update_env_file(key_values: Dict[str, Any], env_path: Optional[str] = None) -> None:
    """Updates values in .env file safely, guarding existing secrets and isolating tests."""
    target = env_path or os.environ.get("ENV_FILE_PATH") or getattr(settings, "env_file_path", ".env")
    env_p = Path(target).resolve()
    repo_env = (Path(__file__).resolve().parent.parent.parent.parent / ".env").resolve()
    is_testing = bool(
        os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("TESTING")
        or "pytest" in sys.modules
    )

    if is_testing:
        if not os.environ.get("ENV_FILE_PATH") and not env_path:
            return
        if env_p == repo_env:
            return

    if env_p.is_file():
        try:
            import shutil
            shutil.copy2(str(env_p), f"{env_p}.bak")
        except Exception:
            pass

    skip_keys = {"EDITOR_SYSTEM_PROMPT", "EDITOR_PROMPT_TEMPLATE", "X_RELEVANCE_PROMPT"}
    if not env_p.is_file():
        lines = [
            f'{k}="{str(v).replace(chr(34), chr(92) + chr(34)).replace(chr(13), "").replace(chr(10), chr(92) + "n")}"\n'
            for k, v in key_values.items()
            if v is not None and k not in skip_keys and not is_secret_placeholder_or_empty(k, v)
        ]
        with open(env_p, "w", encoding="utf-8") as f:
            f.writelines(lines)
        return

    with open(env_p, "r", encoding="utf-8") as f:
        content = f.read()

    for k, v in key_values.items():
        if v is None or k in skip_keys or is_secret_placeholder_or_empty(k, v):
            continue
        clean_v = str(v).replace('"', '\\"').replace('\r', '').replace('\n', '\\n')
        pattern = re.compile(rf"^{k}=.*$", re.MULTILINE)
        if pattern.search(content):
            content = pattern.sub(f'{k}="{clean_v}"', content)
        else:
            content += f'\n{k}="{clean_v}"'

    with open(env_p, "w", encoding="utf-8") as f:
        f.write(content)


async def handle_update_settings(request: web.Request) -> web.Response:
    """Saves updated settings to SQLite and .env and updates runtime object."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    db_updates: Dict[str, Any] = {}

    def is_unmasked(val: Any) -> bool:
        if not val:
            return False
        s = str(val).strip()
        if not s or "*" in s or "..." in s or "•••" in s or s.lower().startswith("your_"):
            return False
        return True

    if "min_viral_score" in data and data["min_viral_score"] not in (None, ""):
        try:
            db_updates["min_viral_score"] = float(data["min_viral_score"])
        except (ValueError, TypeError):
            pass

    if "super_viral_threshold" in data and data["super_viral_threshold"] not in (None, ""):
        try:
            db_updates["super_viral_threshold"] = float(data["super_viral_threshold"])
        except (ValueError, TypeError):
            pass

    if "max_posts_per_run" in data and data["max_posts_per_run"] not in (None, ""):
        try:
            db_updates["max_posts_per_run"] = int(data["max_posts_per_run"])
        except (ValueError, TypeError):
            pass

    if "scheduler_interval_minutes" in data:
        value = data["scheduler_interval_minutes"]
        if value not in (None, ""):
            try:
                db_updates["scheduler_interval_minutes"] = int(value)
            except (ValueError, TypeError):
                pass

    if "ai_request_delay_seconds" in data and data["ai_request_delay_seconds"] not in (None, ""):
        try:
            db_updates["ai_request_delay_seconds"] = max(0, int(data["ai_request_delay_seconds"]))
        except (ValueError, TypeError):
            pass

    if "daily_digest_enabled" in data and data["daily_digest_enabled"] is not None:
        db_updates["daily_digest_enabled"] = bool(data["daily_digest_enabled"])

    if "daily_digest_time" in data:
        raw_digest_time = str(data["daily_digest_time"] or "").strip()
        from src.scheduler import normalize_schedule_times
        cleaned_list = normalize_schedule_times(raw_digest_time)
        cleaned_times = ", ".join(cleaned_list)
        db_updates["daily_digest_time"] = cleaned_times or None

    def normalize_time(t_str: str) -> str:
        t_str = t_str.strip().lower()
        if not t_str:
            return ""
        is_pm = "pm" in t_str
        is_am = "am" in t_str
        t_str = t_str.replace("am", "").replace("pm", "").strip()
        parts = t_str.split(":")
        try:
            h = int(parts[0])
            m = int(parts[1]) if len(parts) > 1 else 0
            if is_pm and h < 12:
                h += 12
            if is_am and h == 12:
                h = 0
            return f"{h:02d}:{m:02d}"
        except Exception:
            return ""

    if "quiet_hours_enabled" in data and data["quiet_hours_enabled"] is not None:
        db_updates["quiet_hours_enabled"] = bool(data["quiet_hours_enabled"])

    if "quiet_hours_start" in data:
        qh_start = normalize_time(str(data["quiet_hours_start"] or ""))
        db_updates["quiet_hours_start"] = qh_start or None

    if "quiet_hours_end" in data:
        qh_end = normalize_time(str(data["quiet_hours_end"] or ""))
        db_updates["quiet_hours_end"] = qh_end or None

    if "digest_pre_sleep_minutes" in data and data["digest_pre_sleep_minutes"] not in (None, ""):
        try:
            db_updates["digest_pre_sleep_minutes"] = max(1, int(data["digest_pre_sleep_minutes"]))
        except (ValueError, TypeError):
            pass

    if "digest_block_interval_minutes" in data and data["digest_block_interval_minutes"] not in (None, ""):
        try:
            db_updates["digest_block_interval_minutes"] = max(1, int(data["digest_block_interval_minutes"]))
        except (ValueError, TypeError):
            pass

    if "daytime_publish_interval_minutes" in data and data["daytime_publish_interval_minutes"] not in (None, ""):
        try:
            db_updates["daytime_publish_interval_minutes"] = max(1, int(data["daytime_publish_interval_minutes"]))
        except (ValueError, TypeError):
            pass

    if "ai_model_name" in data:
        model = str(data["ai_model_name"] or "").strip()
        if model:
            db_updates["ai_model_name"] = model
        else:
            db_updates["ai_model_name"] = "gemini-3.5-flash-lite, gemini-3.1-flash-lite"

    if "telegram_channel_id" in data:
        channel = str(data["telegram_channel_id"] or "").strip()
        if channel and not channel.startswith("@") and not channel.startswith("-") and not channel.isdigit():
            channel = f"@{channel}"
        db_updates["telegram_channel_id"] = channel

    # Sensitive fields - only update if provided and not masked
    if "ai_api_key" in data and is_unmasked(data["ai_api_key"]):
        db_updates["ai_api_key"] = str(data["ai_api_key"]).strip()
        try:
            from src.ai.agents.editor import reset_client
            reset_client()
        except Exception:
            pass

    if "telegram_bot_token" in data and is_unmasked(data["telegram_bot_token"]):
        db_updates["telegram_bot_token"] = str(data["telegram_bot_token"]).strip()

    if "twitter_username" in data:
        db_updates["twitter_username"] = str(data["twitter_username"]).strip()

    if "twitter_email" in data:
        db_updates["twitter_email"] = str(data["twitter_email"]).strip()

    if "twitter_password" in data and is_unmasked(data["twitter_password"]):
        db_updates["twitter_password"] = str(data["twitter_password"]).strip()

    if "twitter_proxy_url" in data:
        db_updates["twitter_proxy_url"] = str(data["twitter_proxy_url"]).strip()

    for string_setting in ["x_search_queries", "x_relevance_prompt", "editor_system_prompt", "editor_prompt_template", "app_timezone"]:
        if string_setting in data:
            db_updates[string_setting] = str(data[string_setting]).strip()

    if "scout_interval_minutes" in data:
        value = data["scout_interval_minutes"]
        if value not in (None, ""):
            db_updates["scout_interval_minutes"] = max(1, int(value))

    if "metric_refresh_hours" in data:
        db_updates["metric_refresh_hours"] = max(1, min(720, int(data["metric_refresh_hours"])))

    if "metric_refresh_limit" in data:
        db_updates["metric_refresh_limit"] = max(1, min(500, int(data["metric_refresh_limit"])))

    if "max_digest_items" in data:
        db_updates["max_digest_items"] = max(1, int(data["max_digest_items"]))

    if "queue_sort_strategy" in data:
        strat = str(data["queue_sort_strategy"]).strip()
        if strat in ("BY_VIRAL_SCORE", "CHRONOLOGICAL_DESC", "CHRONOLOGICAL_ASC", "RANDOM"):
            db_updates["queue_sort_strategy"] = strat

    if "backlog_digest_enabled" in data:
        db_updates["backlog_digest_enabled"] = bool(data["backlog_digest_enabled"])

    if "backlog_max_chunk_size" in data:
        db_updates["backlog_max_chunk_size"] = max(1, int(data["backlog_max_chunk_size"]))

    for bool_setting in ["show_metric_likes", "show_metric_reposts", "show_metric_views", "show_metric_impact", "show_metric_author", "show_metric_hashtags", "show_metric_source_btn", "approval_required", "show_metric_takeaways"]:
        if bool_setting in data:
            db_updates[bool_setting] = str(data[bool_setting]).lower() == "true"

    if "max_title_words" in data and data["max_title_words"] not in (None, ""):
        try:
            db_updates["max_title_words"] = max(3, min(100, int(data["max_title_words"])))
        except (ValueError, TypeError):
            pass

    if "telegram_max_caption_len" in data and data["telegram_max_caption_len"] not in (None, ""):
        try:
            db_updates["telegram_max_caption_len"] = max(200, min(10000, int(data["telegram_max_caption_len"])))
        except (ValueError, TypeError):
            pass

    if "telegram_max_text_len" in data and data["telegram_max_text_len"] not in (None, ""):
        try:
            db_updates["telegram_max_text_len"] = max(500, min(20000, int(data["telegram_max_text_len"])))
        except (ValueError, TypeError):
            pass

    if "ai_temperature" in data and data["ai_temperature"] not in (None, ""):
        try:
            db_updates["ai_temperature"] = max(0.0, min(1.0, float(data["ai_temperature"])))
        except (ValueError, TypeError):
            pass

    if "max_hashtags" in data and data["max_hashtags"] not in (None, ""):
        try:
            db_updates["max_hashtags"] = max(0, min(50, int(data["max_hashtags"])))
        except (ValueError, TypeError):
            pass

    if "scout_search_count" in data and data["scout_search_count"] not in (None, ""):
        try:
            db_updates["scout_search_count"] = max(1, min(100, int(data["scout_search_count"])))
        except (ValueError, TypeError):
            pass

    if "scout_search_mode" in data and data["scout_search_mode"] not in (None, ""):
        mode = str(data["scout_search_mode"]).strip().upper()
        if mode in ("BOTH", "LATEST", "TOP"):
            db_updates["scout_search_mode"] = mode

    if "video_ttl_days" in data:
        db_updates["video_ttl_days"] = int(data["video_ttl_days"])
    if "video_max_quality" in data:
        db_updates["video_max_quality"] = int(data["video_max_quality"])
    if "video_max_size_mb" in data:
        db_updates["video_max_size_mb"] = int(data["video_max_size_mb"])
    if "telegram_admin_id" in data:
        db_updates["telegram_admin_id"] = str(data["telegram_admin_id"]).strip()

    if db_updates:
        await init_db()
        await save_system_settings_dual(db_updates)

    return web.json_response({
        "status": "saved",
        "settings": {
            "min_viral_score": settings.min_viral_score,
            "super_viral_threshold": getattr(settings, "super_viral_threshold", 100.0),
            "max_posts_per_run": settings.max_posts_per_run,
            "scheduler_interval_minutes": settings.scheduler_interval_minutes,
            "ai_request_delay_seconds": settings.ai_request_delay_seconds,
            "daily_digest_enabled": settings.daily_digest_enabled,
            "daily_digest_time": settings.daily_digest_time,
            "quiet_hours_enabled": settings.quiet_hours_enabled,
            "quiet_hours_start": settings.quiet_hours_start,
            "quiet_hours_end": settings.quiet_hours_end,
            "ai_model_name": settings.ai_model_name,
            "telegram_channel_id": settings.telegram_channel_id,
            "twitter_username": settings.twitter_username,
            "twitter_email": settings.twitter_email,
            "twitter_proxy_url": settings.twitter_proxy_url,
            "is_configured": settings.is_configured(),
            "x_search_queries": getattr(settings, "x_search_queries", ""),
            "x_relevance_prompt": getattr(settings, "x_relevance_prompt", ""),
            "scout_interval_minutes": getattr(settings, "scout_interval_minutes", 15),
            "metric_refresh_hours": getattr(settings, "metric_refresh_hours", 48),
            "metric_refresh_limit": getattr(settings, "metric_refresh_limit", 20),
            "editor_system_prompt": getattr(settings, "editor_system_prompt", ""),
            "editor_prompt_template": getattr(settings, "editor_prompt_template", ""),
            "show_metric_likes": getattr(settings, "show_metric_likes", True),
            "show_metric_reposts": getattr(settings, "show_metric_reposts", True),
            "show_metric_views": getattr(settings, "show_metric_views", True),
            "show_metric_impact": getattr(settings, "show_metric_impact", True),
            "show_metric_author": getattr(settings, "show_metric_author", True),
            "show_metric_hashtags": getattr(settings, "show_metric_hashtags", True),
            "show_metric_source_btn": getattr(settings, "show_metric_source_btn", True),
            "max_digest_items": getattr(settings, "max_digest_items", 10),
            "queue_sort_strategy": getattr(settings, "queue_sort_strategy", "BY_VIRAL_SCORE"),
            "backlog_digest_enabled": getattr(settings, "backlog_digest_enabled", True),
            "backlog_max_chunk_size": getattr(settings, "backlog_max_chunk_size", 6),
            "app_timezone": getattr(settings, "app_timezone", None),
            "show_metric_takeaways": getattr(settings, "show_metric_takeaways", False),
            "max_title_words": getattr(settings, "max_title_words", 25),
            "telegram_max_caption_len": getattr(settings, "telegram_max_caption_len", 1024),
            "telegram_max_text_len": getattr(settings, "telegram_max_text_len", 4096),
            "ai_temperature": getattr(settings, "ai_temperature", 0.3),
            "max_hashtags": getattr(settings, "max_hashtags", 5),
            "scout_search_count": getattr(settings, "scout_search_count", 10),
        }
    })


async def handle_get_topics(request: web.Request) -> web.Response:
    topics = await get_topics()
    return web.json_response(topics)


async def handle_create_topic(request: web.Request) -> web.Response:
    data = await request.json()
    new_id = await create_topic(data)
    return web.json_response({"id": new_id, "status": "success"})


async def handle_update_topic(request: web.Request) -> web.Response:
    topic_id = int(request.match_info['id'])
    data = await request.json()
    await update_topic(topic_id, data)
    return web.json_response({"status": "success"})


async def handle_delete_topic(request: web.Request) -> web.Response:
    topic_id = int(request.match_info['id'])
    await delete_topic(topic_id)
    return web.json_response({"status": "success"})


def register_settings_routes(app: web.Application) -> None:
    app.router.add_get("/api/system/backup", handle_system_backup)
    app.router.add_get("/api/settings", handle_get_settings)
    app.router.add_post("/api/settings", handle_update_settings)
    app.router.add_get("/api/digest-schedules", handle_get_digest_schedules)
    app.router.add_post("/api/digest-schedules", handle_create_digest_schedule)
    app.router.add_put("/api/digest-schedules/{id}", handle_update_digest_schedule)
    app.router.add_delete("/api/digest-schedules/{id}", handle_delete_digest_schedule)
    app.router.add_get("/api/topics", handle_get_topics)
    app.router.add_post("/api/topics", handle_create_topic)
    app.router.add_put("/api/topics/{id}", handle_update_topic)
    app.router.add_delete("/api/topics/{id}", handle_delete_topic)
