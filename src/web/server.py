import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional
from aiohttp import web

# Set up Windows console encoding if needed
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from src.ai.agents.publisher import format_telegram_post
from src.utils.hashtags import normalize_hashtags, enforce_hashtags_in_post
from src.config import settings
from src.auth import (
    create_session_token,
    decode_session_token,
    LoginRateLimiter,
    mask_secret,
)
from src.db import (
    auto_schedule_queue,
    cancel_scheduled_post,
    get_published_posts,
    get_queued_posts,
    get_recent_pipeline_runs,
    get_filtered_tweets,
    get_tweet_by_id,
    init_db,
    mark_post_published,
    reorder_queue,
    update_post_content,
    update_post_schedule,
    create_user,
    authenticate_user,
    get_user_by_username,
    get_all_users,
    update_user_password,
    delete_user,
    get_user_count,
    save_system_settings_dual,
    load_system_settings_into_config,
    export_tweets_data_stream,
    delete_historical_data,
    get_staging_tweets,
    create_digest_draft,
    publish_digest,
    get_digests_list,
    get_digest_schedules,
    save_digest_schedule,
    delete_digest_schedule,
    normalize_to_utc_iso,
)
from src.digest import run_daily_digest
from src.main import run_pipeline
from src.version import get_version, get_version_info
from src.utils.technical_audit import audit_and_prepare_telegram_content
from src.telegram.sender import send_photo_to_channel, send_post_to_channel, send_video_to_channel
from src.telegram.moderation_bot import (
    start_moderation_bot,
    stop_moderation_bot,
    delete_admin_moderation_message,
    send_post_card_to_admin,
)
from src.ai.agents.editor import reprocess_tweet_with_editor_feedback
from src.ai.agents.scout import (
    get_twitter_client,
    load_cookies_from_file,
    server_twitter_login,
)

STATIC_DIR = Path(__file__).parent / "static"

login_limiter = LoginRateLimiter(max_attempts=5, window_seconds=300, lockout_seconds=900)
pipeline_lock = asyncio.Lock()
pipeline_runtime = {
    "state": "idle",
    "tweets_found": 0,
    "viral_found": 0,
    "error_message": None,
    "started_at": None,
    "finished_at": None,
}


def get_client_ip(request: web.Request) -> str:
    """Extracts client IP address respecting X-Forwarded-For if present."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote or "127.0.0.1"


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """
    Authentication & Role-Based Access Control (RBAC) middleware.
    Protects API routes, extracts HMAC session tokens, and validates roles.
    """
    path = request.path

    # 1. Public resources (SPA UI, static files, favicon)
    if path in ("/", "/favicon.ico") or path.startswith("/static/"):
        return await handler(request)

    # 2. Public auth endpoints
    public_api_paths = (
        "/api/auth/setup_status",
        "/api/auth/setup",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/me",
    )
    if path in public_api_paths:
        return await handler(request)

    # 3. Global bypass if auth is disabled in settings (e.g. testing)
    if not getattr(settings, "auth_enabled", True):
        request["user"] = {"username": "admin", "role": "admin"}
        return await handler(request)

    # 4. Extract token from Bearer header or session_id cookie
    token = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif "session_id" in request.cookies:
        token = request.cookies.get("session_id")

    payload = decode_session_token(token, settings.secret_key) if token else None
    if not payload:
        await init_db()
        user_count = await get_user_count()
        if user_count == 0:
            return web.json_response(
                {"error": "Setup required", "needs_setup": True},
                status=401,
            )
        return web.json_response({"error": "Unauthorized"}, status=401)

    request["user"] = payload

    # 5. Role-based restrictions (RBAC)
    role = payload.get("role", "editor")
    admin_only_prefixes = (
        "/api/settings",
        "/api/digest-schedules",
        "/api/users",
        "/api/twitter/login",
        "/api/twitter/cookies",
        "/api/twitter/debug-screenshot",
        "/api/system/backup",
    )
    if any(path.startswith(p) for p in admin_only_prefixes):
        if role != "admin":
            return web.json_response(
                {"error": "Forbidden: Requires Admin privileges"},
                status=403,
            )

    return await handler(request)


async def handle_index(request: web.Request) -> web.Response:
    """Serves the main SPA index.html."""
    html_path = STATIC_DIR / "index.html"
    if not html_path.is_file():
        return web.Response(text="index.html not found", status=404)
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    return web.Response(text=content, content_type="text/html", charset="utf-8")


async def handle_favicon(request: web.Request) -> web.Response:
    """Returns a simple modern SVG lightning bolt favicon."""
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="#24a1de"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>'
    return web.Response(text=svg, content_type="image/svg+xml", charset="utf-8")


import time

_failed_summaries: Dict[str, float] = {}  # tweet_id -> last_failed_timestamp
_in_progress_summaries: set = set()

async def _generate_summary_for_tweet(
    p: Dict[str, Any],
    force: bool = False,
    instruction: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Helper to generate and persist summary for a single tweet dictionary."""
    tweet_id = str(p.get("tweet_id"))
    now = time.time()
    
    if not force:
        # If failed recently (< 5 minutes ago), skip to prevent quota exhaustion loops
        last_failed = _failed_summaries.get(tweet_id, 0)
        if now - last_failed < 300:
            return None

    if tweet_id in _in_progress_summaries:
        return None

    _in_progress_summaries.add(tweet_id)
    try:
        from src.ai.agents.editor import call_llm
        from src.db import update_tweet_status_and_score, get_active_topics
        from src.utils.technical_audit import audit_and_prepare_telegram_content

        print(f"🤖 Генерация русского саммари для поста {tweet_id}...")
        media_urls = p.get("media_urls", [])
        video_data = p.get("video_data")
        if video_data and isinstance(video_data, dict) and video_data.get("thumbnail"):
            media_urls = [video_data["thumbnail"]]

        active_topics = await get_active_topics()
        topic = next((t for t in active_topics if t["id"] == p.get("topic_id")), None)

        ai_data = await call_llm(
            text=p.get("text_raw", ""),
            media_urls=media_urls,
            topic=topic,
            editor_instruction=instruction if instruction else None,
            tweet=p,
        )
        if not ai_data:
            print(f"⚠️ Саммари не сформировано для {tweet_id} (квоты 429 исчерпаны).")
            return None
        if "hashtags" in ai_data:
            ai_data["hashtags"] = normalize_hashtags(ai_data["hashtags"])

        # Technical audit check
        is_media = bool(media_urls or video_data)
        audit = audit_and_prepare_telegram_content(
            title=ai_data.get("title_ru", ""),
            summary=ai_data.get("summary_ru", ""),
            takeaways=ai_data.get("takeaways", []),
            hashtags=ai_data.get("hashtags", []),
            is_media=is_media,
            author_handle=p.get("author_handle"),
            tweet_url=p.get("tweet_url"),
        )
        ai_data["title_ru"] = audit["title_ru"]
        ai_data["summary_ru"] = audit["summary_ru"]
        ai_data["takeaways"] = audit["takeaways"]
        ai_data["hashtags"] = audit["hashtags"]
        ai_data["technical_audit"] = {
            "is_valid": audit["is_valid"],
            "char_count": audit["char_count"],
            "max_limit": audit["max_limit"],
            "warnings": audit["warnings"],
        }

        p["summary_data"] = ai_data
        p["impact_score"] = ai_data.get("impact_score", 7)
        summary_json_str = json.dumps(ai_data, ensure_ascii=False)

        new_status = p.get("status", "PROCESSED")
        if getattr(settings, "approval_required", False) and new_status == "PROCESSED":
            new_status = "PENDING_APPROVAL"

        await update_tweet_status_and_score(
            tweet_id=tweet_id,
            score=p.get("viral_score", 0.0),
            is_viral=True,
            status=new_status,
            impact_score=p["impact_score"],
            summary_json=summary_json_str,
        )
        _failed_summaries.pop(tweet_id, None)
        return ai_data
    except Exception as e:
        _failed_summaries[tweet_id] = now
        print(f"⚠️ Ошибка генерации саммари для {tweet_id}: {e}")
        return None
    finally:
        _in_progress_summaries.discard(tweet_id)


async def _ensure_russian_summaries(posts: List[Dict[str, Any]]) -> None:
    """Ensures all posts have structured Russian summaries generated by Gemini safely without quota exhaustion."""
    missing = [p for p in posts if not p.get("summary_data") and p.get("text_raw")]
    if not missing:
        return

    # Process at most 2 missing summaries per request to prevent hitting rate limits
    for p in missing[:2]:
        await _generate_summary_for_tweet(p, force=False)
        await asyncio.sleep(0.5)


async def handle_get_queue(request: web.Request) -> web.Response:
    """Returns list of pending and scheduled posts with Russian summaries."""
    await init_db()
    posts = await get_queued_posts()
    await _ensure_russian_summaries(posts)
    return web.json_response(posts)


async def handle_get_published(request: web.Request) -> web.Response:
    """Returns list of published posts with Russian summaries."""
    await init_db()
    limit = int(request.query.get("limit", 50))
    posts = await get_published_posts(limit=limit)
    await _ensure_russian_summaries(posts)
    return web.json_response(posts)


async def handle_update_schedule(request: web.Request) -> web.Response:
    """Updates scheduled_at for a post."""
    tweet_id = request.match_info["id"]
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    scheduled_at = data.get("scheduled_at")
    await init_db()
    await update_post_schedule(tweet_id, scheduled_at)
    return web.json_response({"status": "success", "tweet_id": tweet_id, "scheduled_at": scheduled_at})


async def handle_auto_schedule(request: web.Request) -> web.Response:
    """Automatically assigns spaced publication times for all queued posts."""
    await init_db()
    interval = settings.scheduler_interval_minutes or 120
    tz_offset_minutes = None
    try:
        data = await request.json()
        if isinstance(data, dict):
            tz_offset_minutes = data.get("tz_offset_minutes")
            if "interval_minutes" in data and data["interval_minutes"]:
                interval = int(data["interval_minutes"])
    except Exception:
        pass

    posts = await auto_schedule_queue(
        interval_minutes=interval,
        start_offset_minutes=30,
        tz_offset_minutes=tz_offset_minutes,
    )
    return web.json_response({"status": "success", "count": len(posts), "posts": posts})


async def handle_reorder_queue(request: web.Request) -> web.Response:
    """Updates the queue ordering."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    ordered_ids = data.get("ordered_ids", [])
    if not isinstance(ordered_ids, list):
        return web.json_response({"error": "ordered_ids must be a list"}, status=400)

    await init_db()
    await reorder_queue(ordered_ids)
    return web.json_response({"status": "success", "count": len(ordered_ids)})


async def handle_publish_now(request: web.Request) -> web.Response:
    """Immediately publishes the post to Telegram."""
    tweet_id = request.match_info["id"]
    await init_db()
    tweet = await get_tweet_by_id(tweet_id)
    if not tweet:
        return web.json_response({"error": "Post not found"}, status=404)

    summary_data = tweet.get("summary_data") or {
        "title_ru": tweet.get("text_raw", "")[:60],
        "summary_ru": tweet.get("text_raw", ""),
        "takeaways": ["Ключевая новость из X.com"],
        "hashtags": ["#AI", "#Tech"],
        "impact_score": tweet.get("impact_score", 7),
    }

    # Extract video if present
    video_data = tweet.get("video_data")
    video_local_path = None
    video_thumbnail = None
    if video_data and isinstance(video_data, dict):
        video_local_path = video_data.get("local_path")
        video_thumbnail = video_data.get("thumbnail")

        # Verify video_local_path exists on disk; if missing, re-download if possible
        if video_local_path and not os.path.exists(video_local_path):
            video_local_path = None
        if not video_local_path and video_data.get("original_url"):
            try:
                from src.utils.video_manager import download_video
                video_local_path = await download_video(tweet_id, video_data["original_url"])
            except Exception as dl_err:
                print(f"⚠️ Ошибка загрузки видео перед публикацией {tweet_id}: {dl_err}")

    media_urls = tweet.get("media_urls", [])
    selected_idx = summary_data.get("selected_image_index")
    photo_url = None
    if not video_local_path and selected_idx is not None and isinstance(selected_idx, int) and 0 <= selected_idx < len(media_urls):
        photo_url = media_urls[selected_idx]
    elif not video_local_path and media_urls and (selected_idx == 0 or selected_idx is None):
        photo_url = media_urls[0]
    elif not video_local_path and not photo_url and video_thumbnail:
        photo_url = video_thumbnail

    if isinstance(summary_data, dict):
        raw_h = (
            summary_data.get("hashtags")
            or summary_data.get("hash_tags")
            or summary_data.get("tags")
            or summary_data.get("keywords")
        )
        if raw_h is not None:
            summary_data["hashtags"] = normalize_hashtags(raw_h)

    formatted_text = format_telegram_post(tweet, summary_data, is_photo=bool(photo_url) or bool(video_local_path))
    formatted_text = enforce_hashtags_in_post(formatted_text)
    tweet_url = tweet.get("tweet_url", "https://x.com")

    try:
        if video_local_path:
            await send_video_to_channel(formatted_text, video_local_path, thumbnail_path=video_thumbnail, tweet_url=tweet_url)
        elif photo_url:
            await send_photo_to_channel(formatted_text, photo_url, tweet_url)
        else:
            await send_post_to_channel(formatted_text, tweet_url)

        await mark_post_published(tweet_id)
        # Inbox Zero: delete moderation card from admin's Telegram chat
        try:
            await delete_admin_moderation_message(tweet_id)
        except Exception as e:
            print(f"Failed to delete admin card for {tweet_id}: {e}")

        return web.json_response({"status": "published", "tweet_id": tweet_id})
    except Exception as e:
        error_msg = str(e)
        print(f"❌ Ошибка публикации поста {tweet_id}: {error_msg}")
        return web.json_response({"error": error_msg, "tweet_id": tweet_id}, status=400)


async def handle_edit_post(request: web.Request) -> web.Response:
    """Updates post content with strict technical audit and syncs admin card."""
    tweet_id = request.match_info["id"]
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    title_ru = data.get("title_ru", "").strip()
    summary_ru = data.get("summary_ru", "").strip()
    takeaways = data.get("takeaways", [])
    hashtags = normalize_hashtags(data.get("hashtags", []))
    selected_image_index = data.get("selected_image_index")
    impact_score = data.get("impact_score")

    if not title_ru or not summary_ru:
        return web.json_response({"error": "Title and summary cannot be empty"}, status=400)

    await init_db()
    tweet = await get_tweet_by_id(tweet_id)
    if not tweet:
        return web.json_response({"error": "Post not found"}, status=404)

    # Technical audit before saving
    is_media = bool(tweet.get("media_urls")) or bool(tweet.get("video_data"))
    audit_res = audit_and_prepare_telegram_content(
        title=title_ru,
        summary=summary_ru,
        takeaways=takeaways,
        hashtags=hashtags,
        is_media=is_media,
    )
    title_ru = audit_res["title"]
    summary_ru = audit_res["summary"]
    takeaways = audit_res["takeaways"]
    hashtags = audit_res["hashtags"]

    await update_post_content(
        tweet_id=tweet_id,
        title_ru=title_ru,
        summary_ru=summary_ru,
        takeaways=takeaways,
        hashtags=hashtags,
        selected_image_index=selected_image_index,
        impact_score=impact_score,
    )

    if "scheduled_at" in data:
        scheduled_at = data.get("scheduled_at")
        await update_post_schedule(tweet_id, scheduled_at)

    # If post was in Telegram moderation, refresh the card
    if getattr(settings, "approval_required", False) and getattr(settings, "telegram_admin_id", None):
        updated_tweet = await get_tweet_by_id(tweet_id)
        if updated_tweet:
            try:
                await send_post_card_to_admin(updated_tweet)
            except Exception as e:
                print(f"Error syncing card to admin on edit: {e}")

    return web.json_response({"status": "updated", "tweet_id": tweet_id, "audit": audit_res})


async def handle_reprocess_post(request: web.Request) -> web.Response:
    """Reprocesses a post using AI based on Chief Editor instructions."""
    tweet_id = request.match_info["id"]
    try:
        data = await request.json()
    except Exception:
        data = {}

    editor_instruction = data.get("instruction", "").strip()
    if not editor_instruction:
        return web.json_response({"error": "Инструкция для ИИ не может быть пустой"}, status=400)

    await init_db()
    tweet = await get_tweet_by_id(tweet_id)
    if not tweet:
        return web.json_response({"error": "Post not found"}, status=404)

    try:
        updated_tweet = await reprocess_tweet_with_editor_feedback(tweet_id, editor_instruction)
        if not updated_tweet:
            return web.json_response({"error": "Не удалось переработать пост через ИИ"}, status=500)
        return web.json_response({"status": "success", "tweet": updated_tweet})
    except Exception as e:
        return web.json_response({"error": f"Ошибка при переработке: {str(e)}"}, status=500)


async def handle_delete_post(request: web.Request) -> web.Response:
    """Cancels and archives a post, removing it from admin Telegram."""
    tweet_id = request.match_info["id"]
    await init_db()
    await cancel_scheduled_post(tweet_id)
    try:
        await delete_admin_moderation_message(tweet_id)
    except Exception as e:
        print(f"Failed to delete admin card for {tweet_id}: {e}")
    return web.json_response({"status": "cancelled", "tweet_id": tweet_id})


async def handle_sync_telegram(request: web.Request) -> web.Response:
    """Forces synchronization of all queued unposted posts to the configured Telegram admin."""
    admin_id = getattr(settings, "telegram_admin_id", None)
    if not admin_id:
        return web.json_response(
            {"error": "Telegram Admin ID не настроен. Напишите боту /start в Telegram или укажите ID в настройках."},
            status=400,
        )
    await init_db()
    queued = await get_queued_posts()
    sent_count = 0
    errors = []
    for post in queued:
        try:
            ok = await send_post_card_to_admin(post)
            if ok:
                sent_count += 1
        except Exception as e:
            errors.append(f"Post {post.get('id')}: {str(e)}")

    return web.json_response({
        "status": "success",
        "total_queued": len(queued),
        "synced_count": sent_count,
        "admin_id": str(admin_id),
        "errors": errors if errors else None,
    })


async def handle_get_version(request: web.Request) -> web.Response:
    """Returns application version information."""
    return web.json_response(get_version_info())


from src.ai.agents.editor import get_ai_status, set_ai_status

async def handle_get_status(request: web.Request) -> web.Response:
    """Returns real-time status of AI models and rate limiting."""
    status_info = get_ai_status()
    return web.json_response(status_info)


# --- Authentication Endpoints ---

async def handle_setup_status(request: web.Request) -> web.Response:
    """Checks whether the system requires initial setup (0 users)."""
    await init_db()
    count = await get_user_count()
    return web.json_response({
        "needs_setup": count == 0,
        "user_count": count,
        "is_configured": settings.is_configured(),
    })


async def handle_setup(request: web.Request) -> web.Response:
    """Performs initial admin account creation and optional settings configuration."""
    await init_db()
    count = await get_user_count()
    if count > 0:
        return web.json_response({"error": "Первичная настройка уже завершена"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))

    if len(username) < 3:
        return web.json_response({"error": "Имя пользователя должно содержать минимум 3 символа"}, status=400)
    if len(password) < 8:
        return web.json_response({"error": "Пароль должен содержать минимум 8 символов"}, status=400)

    try:
        user = await create_user(username, password, role="admin")
    except Exception as e:
        return web.json_response({"error": f"Ошибка создания администратора: {e}"}, status=400)

    initial_settings: Dict[str, Any] = {}
    for key in (
        "ai_api_key",
        "ai_model_name",
        "telegram_bot_token",
        "telegram_channel_id",
        "twitter_username",
        "twitter_email",
        "twitter_password",
        "twitter_proxy_url",
    ):
        if key in data and data[key] and not str(data[key]).startswith("***"):
            initial_settings[key] = data[key]

    if initial_settings:
        await save_system_settings_dual(initial_settings)

    token = create_session_token(user["username"], user["role"], settings.secret_key)
    response = web.json_response({
        "status": "setup_completed",
        "user": {"username": user["username"], "role": user["role"]},
        "token": token,
    })
    response.set_cookie(
        "session_id",
        token,
        max_age=14 * 86400,
        httponly=True,
        samesite="Lax",
        path="/"
    )
    return response


async def handle_login(request: web.Request) -> web.Response:
    """Authenticates user credentials with brute-force rate limiting."""
    ip = get_client_ip(request)
    if login_limiter.is_locked(ip):
        rem = login_limiter.lockout_remaining(ip)
        return web.json_response(
            {"error": f"Слишком много неудачных попыток. Попробуйте через {rem} сек."},
            status=429,
        )

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))

    await init_db()
    user = await authenticate_user(username, password)
    if not user:
        login_limiter.record_failure(ip)
        return web.json_response({"error": "Неверный логин или пароль"}, status=401)

    login_limiter.reset(ip)
    token = create_session_token(user["username"], user["role"], settings.secret_key)
    response = web.json_response({
        "status": "authenticated",
        "user": {"id": user["id"], "username": user["username"], "role": user["role"]},
        "token": token,
    })
    response.set_cookie(
        "session_id",
        token,
        max_age=14 * 86400,
        httponly=True,
        samesite="Lax",
        path="/"
    )
    return response


async def handle_logout(request: web.Request) -> web.Response:
    """Terminates active session."""
    response = web.json_response({"status": "logged_out"})
    response.del_cookie("session_id", path="/")
    return response


async def handle_me(request: web.Request) -> web.Response:
    """Returns currently authenticated user profile."""
    token = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif "session_id" in request.cookies:
        token = request.cookies.get("session_id")

    payload = decode_session_token(token, settings.secret_key) if token else None
    if not payload:
        return web.json_response({"authenticated": False, "user": None})

    await init_db()
    db_user = await get_user_by_username(payload["username"])
    if not db_user:
        return web.json_response({"authenticated": False, "user": None})

    return web.json_response({
        "authenticated": True,
        "user": {
            "id": db_user["id"],
            "username": db_user["username"],
            "role": db_user["role"],
            "last_login": db_user.get("last_login"),
        }
    })


# --- User Management Endpoints (Admin Only) ---

async def handle_get_users(request: web.Request) -> web.Response:
    """Returns list of registered users."""
    await init_db()
    users = await get_all_users()
    return web.json_response(users)


async def handle_create_user(request: web.Request) -> web.Response:
    """Creates a new user (Admin or Editor)."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    role = str(data.get("role", "editor")).strip()

    if len(username) < 3:
        return web.json_response({"error": "Имя пользователя должно содержать от 3 символов"}, status=400)
    if len(password) < 8:
        return web.json_response({"error": "Пароль должен содержать от 8 символов"}, status=400)
    if role not in ("admin", "editor"):
        return web.json_response({"error": "Роль должна быть 'admin' или 'editor'"}, status=400)

    await init_db()
    try:
        new_user = await create_user(username, password, role=role)
        return web.json_response(new_user, status=201)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        return web.json_response({"error": f"Ошибка создания пользователя: {e}"}, status=400)


async def handle_update_user_password(request: web.Request) -> web.Response:
    """Updates password for a user."""
    target_username = request.match_info["username"]
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    new_password = str(data.get("password", ""))
    if len(new_password) < 8:
        return web.json_response({"error": "Пароль должен содержать минимум 8 символов"}, status=400)

    await init_db()
    try:
        success = await update_user_password(target_username, new_password)
        if not success:
            return web.json_response({"error": "Пользователь не найден"}, status=404)
        return web.json_response({"status": "password_updated", "username": target_username})
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)


async def handle_delete_user(request: web.Request) -> web.Response:
    """Deletes a user, guarding the last remaining admin."""
    target_username = request.match_info["username"]
    current_user = request.get("user", {})
    if current_user.get("username") == target_username:
        return web.json_response({"error": "Нельзя удалить текущего авторизованного пользователя"}, status=400)

    await init_db()
    try:
        success = await delete_user(target_username)
        if not success:
            return web.json_response({"error": "Пользователь не найден"}, status=404)
        return web.json_response({"status": "deleted", "username": target_username})
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)


# --- Twitter Login & Database Backup Endpoints ---

async def handle_twitter_login(request: web.Request) -> web.Response:
    """Performs server-side login to X.com to avoid geo-blocking and generates cookies."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    auth_info_1 = str(data.get("auth_info_1") or data.get("username") or settings.twitter_username or "").strip()
    password = str(data.get("password") or settings.twitter_password or "")
    auth_info_2 = str(data.get("auth_info_2") or data.get("email") or settings.twitter_email or "").strip() or None
    code = str(data.get("code") or "").strip() or None
    proxy_url = data.get("proxy_url")

    if proxy_url:
        settings.twitter_proxy_url = str(proxy_url).strip()
        await save_system_settings_dual({"twitter_proxy_url": settings.twitter_proxy_url})

    if not auth_info_1 or not password:
        return web.json_response({"error": "Необходимо указать логин (username/email) и пароль"}, status=400)

    res = await server_twitter_login(
        auth_info_1=auth_info_1,
        password=password,
        auth_info_2=auth_info_2,
        code=code,
    )

    if res.get("status") in ("ok", "success"):
        updates = {"twitter_username": auth_info_1}
        if auth_info_2:
            updates["twitter_email"] = auth_info_2
        await save_system_settings_dual(updates)
        return web.json_response({**res, "status": "ok"})
    elif res.get("status") in ("code_required", "challenge_required"):
        return web.json_response({**res, "status": "code_required"}, status=200)
    else:
        return web.json_response(res, status=400)


async def handle_save_twitter_cookies(request: web.Request) -> web.Response:
    """Saves user-provided session cookies (auth_token & ct0 or full cookies JSON) directly."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    auth_token = str(data.get("auth_token") or "").strip()
    ct0 = str(data.get("ct0") or "").strip()
    raw_cookies = data.get("cookies")

    cookies_dict: Dict[str, str] = {}
    if isinstance(raw_cookies, dict):
        cookies_dict = {str(k): str(v) for k, v in raw_cookies.items()}
    elif isinstance(raw_cookies, list):
        for item in raw_cookies:
            if isinstance(item, dict) and "name" in item and "value" in item:
                cookies_dict[str(item["name"])] = str(item["value"])

    if auth_token:
        cookies_dict["auth_token"] = auth_token
    if ct0:
        cookies_dict["ct0"] = ct0

    final_auth_token = cookies_dict.get("auth_token")
    final_ct0 = cookies_dict.get("ct0")

    if not final_auth_token or not final_ct0:
        return web.json_response(
            {"error": "Необходимо указать как минимум auth_token и ct0"},
            status=400,
        )

    # Save to data/cookies.json and cookies.json
    target_p = Path(settings.cookies_path)
    target_p.parent.mkdir(parents=True, exist_ok=True)
    with open(target_p, "w", encoding="utf-8") as f:
        json.dump(cookies_dict, f, indent=2)

    root_p = Path("cookies.json")
    with open(root_p, "w", encoding="utf-8") as f:
        json.dump(cookies_dict, f, indent=2)

    # Update runtime settings and persistent .env
    settings.twitter_auth_token = final_auth_token
    settings.twitter_ct0 = final_ct0
    await save_system_settings_dual({
        "twitter_auth_token": final_auth_token,
        "twitter_ct0": final_ct0,
    })

    return web.json_response({
        "status": "ok",
        "message": f"Cookies успешно сохранены в {target_p} и применены в настройках!",
        "has_auth_token": True,
        "has_ct0": True,
    })


async def handle_twitter_debug_screenshot(request: web.Request) -> web.Response:
    """Returns the last login error screenshot if available."""
    target_p = Path(settings.cookies_path).parent / "login_failed.png"
    if not target_p.is_file():
        target_p = Path("data/login_failed.png")
    if not target_p.is_file():
        target_p = Path("login_failed.png")
    if not target_p.is_file():
        return web.json_response({"error": "Скриншот ошибки не найден"}, status=404)
    return web.FileResponse(target_p, headers={"Content-Type": "image/png"})


async def handle_system_backup(request: web.Request) -> web.Response:
    """Downloads the active SQLite database file as a backup."""
    db_file = Path(settings.db_path)
    if not db_file.is_file():
        return web.json_response({"error": "Файл базы данных не найден"}, status=404)
    return web.FileResponse(
        db_file,
        headers={"Content-Disposition": 'attachment; filename="tweets_backup.sqlite"'}
    )


# --- Settings Endpoints (Admin Only) ---

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
    from src.db import is_secret_placeholder_or_empty
    target = env_path or os.environ.get("ENV_FILE_PATH") or getattr(settings, "env_file_path", ".env")
    env_p = Path(target).resolve()
    repo_env = (Path(__file__).resolve().parent.parent / ".env").resolve()
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


def update_pipeline_runtime(
    state: str,
    tweets_found: int = 0,
    viral_found: int = 0,
    error_message: Optional[str] = None,
) -> None:
    """Updates runtime state of the pipeline so dashboard UI status reflects reality."""
    pipeline_runtime["state"] = state
    pipeline_runtime["tweets_found"] = tweets_found
    pipeline_runtime["viral_found"] = viral_found
    pipeline_runtime["error_message"] = error_message
    if state == "running":
        pipeline_runtime["started_at"] = datetime.now(timezone.utc).isoformat()
        pipeline_runtime["finished_at"] = None
    elif state in ("success", "error", "idle"):
        pipeline_runtime["finished_at"] = datetime.now(timezone.utc).isoformat()


async def _safe_run_pipeline() -> None:
    update_pipeline_runtime("running")
    try:
        result = await run_pipeline()
        update_pipeline_runtime(
            state="success",
            tweets_found=len(result.get("raw_tweets", [])),
            viral_found=len(result.get("viral_tweets", [])),
        )
    except Exception as e:
        update_pipeline_runtime(state="error", error_message=str(e))
        print(f"⚠️ Фоновый запуск пайплайна завершился с ошибкой: {e}")


async def handle_run_pipeline(request: web.Request) -> web.Response:
    """Triggers an on-demand background pipeline run."""
    if pipeline_lock.locked() or pipeline_runtime["state"] == "running":
        return web.json_response(
            {"status": "pipeline_running", "message": "Pipeline уже выполняется."},
            status=409,
        )

    pipeline_runtime["state"] = "running"
    pipeline_runtime["started_at"] = datetime.now(timezone.utc).isoformat()
    pipeline_runtime["finished_at"] = None
    pipeline_runtime["error_message"] = None

    async def run_locked_pipeline() -> None:
        async with pipeline_lock:
            await _safe_run_pipeline()

    asyncio.create_task(run_locked_pipeline())
    return web.json_response({"status": "pipeline_started", "message": "Сбор и обработка запущены в фоне."})


async def handle_pipeline_status(request: web.Request) -> web.Response:
    """Returns the current runtime state of the manually triggered pipeline."""
    return web.json_response(pipeline_runtime)


async def handle_run_digest(request: web.Request) -> web.Response:
    """Triggers an on-demand daily digest run."""
    manual_slot = f"manual_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}"
    asyncio.create_task(run_daily_digest(slot_id=manual_slot, is_test=False))
    return web.json_response({"status": "digest_started", "message": "Генерация дайджеста запущена в фоне."})


async def handle_generate_summary(request: web.Request) -> web.Response:
    """Manually triggers Russian summary generation with Gemini for a specific post."""
    tweet_id = request.match_info["id"]
    await init_db()
    tweet = await get_tweet_by_id(tweet_id)
    if not tweet:
        return web.json_response({"error": "Post not found"}, status=404)

    instruction = None
    if request.can_read_body:
        try:
            data = await request.json()
            if isinstance(data, dict):
                instruction = data.get("instruction")
        except Exception:
            pass

    summary_data = await _generate_summary_for_tweet(tweet, force=True, instruction=instruction)
    if summary_data:
        return web.json_response({"status": "success", "summary_data": summary_data, "impact_score": tweet.get("impact_score", 7)})
    else:
        return web.json_response({"error": "Failed to generate summary with Gemini (quota or network error). Please try again in a few moments."}, status=500)


async def handle_batch_reprocess_queue(request: web.Request) -> web.Response:
    """Batch re-generates summaries and titles for all queued/unpublished posts."""
    await init_db()
    try:
        data = await request.json()
    except Exception:
        data = {}

    instruction = data.get("instruction", "").strip() if isinstance(data, dict) else ""
    posts = await get_queued_posts()
    if not posts:
        return web.json_response({"status": "success", "total": 0, "reprocessed": 0, "errors": 0, "message": "Очередь пуста"})

    reprocessed = 0
    errors = 0
    delay = float(getattr(settings, "ai_request_delay_seconds", 1.5))

    for p in posts:
        try:
            res = await _generate_summary_for_tweet(p, force=True, instruction=instruction)
            if res:
                reprocessed += 1
            else:
                errors += 1
            await asyncio.sleep(delay)
        except Exception as e:
            errors += 1
            print(f"Error in batch reprocessing post {p.get('tweet_id')}: {e}")

    return web.json_response({
        "status": "success",
        "total": len(posts),
        "reprocessed": reprocessed,
        "errors": errors,
    })


async def check_twitter_health() -> Dict[str, Any]:
    """
    Checks Twitter / X connection and credentials without throwing unhandled exceptions.
    Returns dict with status ('ok', 'warning', 'error'), message, and details.
    """
    cookies_path = Path("cookies.json")
    cookies_file_exists = cookies_path.is_file()
    cookies_dict = load_cookies_from_file(cookies_path) if cookies_file_exists else {}

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
        client = await get_twitter_client()
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
    from src.db import is_secret_placeholder_or_empty
    if not key or is_secret_placeholder_or_empty("ai_api_key", key) or len(key.strip()) < 8:
        return {
            "name": "Google Gemini",
            "status": "error",
            "message": "AI_API_KEY не задан или содержит шаблонное значение в .env",
            "details": {"model": settings.ai_model_name, "key_present": False},
        }

    ai_stat = get_ai_status()
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
            set_ai_status("ok", "Готов к работе")
            ai_stat = get_ai_status()
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
    statuses = [s.get("status") for s in services.values()]
    if any(st == "error" for st in statuses):
        return "error"
    if any(st == "warning" for st in statuses):
        return "warning"
    return "ok"


async def handle_get_system_health(request: web.Request) -> web.Response:
    """Returns real-time health status for X.com, Gemini, and Telegram."""
    service_req = request.query.get("service")
    user = request.get("user") or {}
    is_editor = user.get("role") == "editor"

    if service_req in ("twitter", "x"):
        res = await check_twitter_health()
        if is_editor:
            res = {**res, "details": {}}
        return web.json_response({
            "status": res["status"],
            "services": {"twitter": res},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    elif service_req == "gemini":
        res = await check_gemini_health()
        if is_editor:
            res = {**res, "details": {}}
        return web.json_response({
            "status": res["status"],
            "services": {"gemini": res},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    elif service_req == "telegram":
        res = await check_telegram_health()
        if is_editor:
            res = {**res, "details": {}}
        return web.json_response({
            "status": res["status"],
            "services": {"telegram": res},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    results = await asyncio.gather(
        check_twitter_health(),
        check_gemini_health(),
        check_telegram_health(),
        return_exceptions=True,
    )

    twitter_res = results[0] if not isinstance(results[0], Exception) else {
        "name": "X.com (Twitter)", "status": "error", "message": f"Сбой проверки: {results[0]}", "details": {}
    }
    gemini_res = results[1] if not isinstance(results[1], Exception) else {
        "name": "Google Gemini", "status": "error", "message": f"Сбой проверки: {results[1]}", "details": {}
    }
    telegram_res = results[2] if not isinstance(results[2], Exception) else {
        "name": "Telegram", "status": "error", "message": f"Сбой проверки: {results[2]}", "details": {}
    }

    if is_editor:
        twitter_res["details"] = {}
        gemini_res["details"] = {}
        telegram_res["details"] = {}

    services = {
        "twitter": twitter_res,
        "gemini": gemini_res,
        "telegram": telegram_res,
    }
    overall = _compute_overall_status(services)

    return web.json_response({
        "status": overall,
        "services": services,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


async def handle_get_system_history(request: web.Request) -> web.Response:
    """Returns recent pipeline runs (default 10)."""
    await init_db()
    try:
        limit = int(request.query.get("limit", 10))
    except (ValueError, TypeError):
        limit = 10
    limit = max(1, min(limit, 100))
    runs = await get_recent_pipeline_runs(limit=limit)
    return web.json_response(runs)


async def handle_get_filtered_tweets(request: web.Request) -> web.Response:
    """Возвращает твиты, отфильтрованные пайплайном (is_viral=0 или IGNORED).
    
    Параметры запроса:
      - limit (int, default=50): максимальное число записей
      - start_date (str, опционально): нижняя граница inserted_at (ISO 8601)
      - end_date (str, опционально): верхняя граница inserted_at (ISO 8601)
    """
    await init_db()
    try:
        limit = int(request.query.get("limit", 50))
    except (ValueError, TypeError):
        limit = 50
    limit = max(1, min(limit, 500))
    start_date = request.query.get("start_date") or None
    end_date = request.query.get("end_date") or None
    tweets = await get_filtered_tweets(limit=limit, start_date=start_date, end_date=end_date)
    return web.json_response(tweets)


export_jobs: Dict[str, Dict[str, Any]] = {}

async def _background_export_task(job_id: str, start_date: str, end_date: str):
    import uuid, zipfile, os, json, shutil
    try:
        import aiofiles
    except ImportError:
        aiofiles = None
    file_path = f"data/export_{job_id}.zip"
    os.makedirs("data", exist_ok=True)
    jsonl_path = f"data/export_{job_id}.jsonl"
    
    try:
        # Write to JSONL
        if aiofiles:
            async with aiofiles.open(jsonl_path, 'w', encoding='utf-8') as f:
                async for row in export_tweets_data_stream(start_date, end_date):
                    await f.write(json.dumps(row, ensure_ascii=False) + "\n")
        else:
            with open(jsonl_path, 'w', encoding='utf-8') as f:
                async for row in export_tweets_data_stream(start_date, end_date):
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    
        def create_zip():
            with zipfile.ZipFile(file_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.write(jsonl_path, arcname=f"export_{start_date[:10]}_to_{end_date[:10]}.jsonl")
            if os.path.exists(jsonl_path):
                os.remove(jsonl_path)
                
        await asyncio.to_thread(create_zip)
        export_jobs[job_id]["status"] = "completed"
        export_jobs[job_id]["file_path"] = file_path
    except Exception as e:
        export_jobs[job_id]["status"] = "error"
        export_jobs[job_id]["error"] = str(e)
        if os.path.exists(jsonl_path):
            try: os.remove(jsonl_path)
            except: pass

async def handle_system_export_start(request: web.Request) -> web.Response:
    user = request.get("user")
    if not user or user.get("role") != "admin":
        return web.json_response({"error": "Admin required"}, status=403)
        
    data = await request.json()
    start_date = data.get("start_date", "1970-01-01T00:00:00")
    end_date = data.get("end_date", "2100-01-01T00:00:00")
    import uuid
    job_id = str(uuid.uuid4())
    export_jobs[job_id] = {"status": "processing"}
    asyncio.create_task(_background_export_task(job_id, start_date, end_date))
    return web.json_response({"status": "processing", "job_id": job_id})

async def handle_system_export_status(request: web.Request) -> web.Response:
    user = request.get("user")
    if not user or user.get("role") != "admin":
        return web.json_response({"error": "Admin required"}, status=403)
    job_id = request.query.get("job_id")
    if not job_id or job_id not in export_jobs:
        return web.json_response({"error": "Not found"}, status=404)
    return web.json_response(export_jobs[job_id])

async def handle_system_export_download(request: web.Request) -> web.Response:
    user = request.get("user")
    if not user or user.get("role") != "admin":
        return web.json_response({"error": "Admin required"}, status=403)
    job_id = request.query.get("job_id")
    if not job_id or job_id not in export_jobs:
        return web.json_response({"error": "Not found"}, status=404)
    job = export_jobs[job_id]
    if job["status"] != "completed":
        return web.json_response({"error": "Not ready"}, status=400)
    return web.FileResponse(Path(job["file_path"]), headers={"Content-Disposition": f'attachment; filename="export_{job_id[:8]}.zip"'})


async def handle_get_history(request: web.Request) -> web.Response:
    """Returns historical pipeline runs."""
    await init_db()
    limit = int(request.query.get("limit", 50))
    from src.db import get_recent_pipeline_runs
    runs = await get_recent_pipeline_runs(limit=limit)
    return web.json_response(runs)


async def handle_system_history_delete(request: web.Request) -> web.Response:
    """Deletes historical data up to a given date."""
    user = request.get("user")
    if not user or user.get("role") != "admin":
        return web.json_response({"error": "Admin required"}, status=403)
        
    data = await request.json()
    end_date = data.get("end_date")
    if not end_date:
        return web.json_response({"error": "end_date is required"}, status=400)
        
    deleted_count = await delete_historical_data(end_date)
    return web.json_response({"status": "success", "deleted": deleted_count})


async def handle_get_topics(request: web.Request) -> web.Response:
    from src.db import get_topics
    topics = await get_topics()
    return web.json_response(topics)

async def handle_create_topic(request: web.Request) -> web.Response:
    from src.db import create_topic
    data = await request.json()
    new_id = await create_topic(data)
    return web.json_response({"id": new_id, "status": "success"})

async def handle_update_topic(request: web.Request) -> web.Response:
    from src.db import update_topic
    topic_id = int(request.match_info['id'])
    data = await request.json()
    await update_topic(topic_id, data)
    return web.json_response({"status": "success"})

async def handle_delete_topic(request: web.Request) -> web.Response:
    from src.db import delete_topic
    topic_id = int(request.match_info['id'])
    await delete_topic(topic_id)
    return web.json_response({"status": "success"})

async def on_app_startup(app: web.Application) -> None:
    await init_db()
    await load_system_settings_into_config()


def create_app() -> web.Application:
    """Creates and configures the web application with authentication middleware."""
    app = web.Application(middlewares=[auth_middleware])
    app.on_startup.append(on_app_startup)

    # CORS / Static & Routes
    app.router.add_get("/", handle_index)
    app.router.add_get("/favicon.ico", handle_favicon)

    # Auth & RBAC
    app.router.add_get("/api/auth/setup_status", handle_setup_status)
    app.router.add_post("/api/auth/setup", handle_setup)
    app.router.add_post("/api/auth/login", handle_login)
    app.router.add_post("/api/auth/logout", handle_logout)
    app.router.add_get("/api/auth/me", handle_me)

    # User Management (Admin only)
    app.router.add_get("/api/users", handle_get_users)
    app.router.add_post("/api/users", handle_create_user)
    app.router.add_post("/api/users/{username}/password", handle_update_user_password)
    app.router.add_delete("/api/users/{username}", handle_delete_user)

    # Server-Side Twitter Login & Database Backup (Admin only)
    app.router.add_post("/api/twitter/login", handle_twitter_login)
    app.router.add_post("/api/twitter/cookies", handle_save_twitter_cookies)
    app.router.add_get("/api/twitter/debug-screenshot", handle_twitter_debug_screenshot)
    app.router.add_get("/api/history", handle_get_history)

    # --- Staging & Manual Digest Endpoints ---
    async def handle_get_staging_tweets(request: web.Request) -> web.Response:
        await init_db()
        strategy = request.query.get("strategy", getattr(settings, "queue_sort_strategy", "BY_VIRAL_SCORE"))
        limit = int(request.query.get("limit", 50))
        tweets = await get_staging_tweets(strategy=strategy, limit=limit)
        return web.json_response(tweets)

    async def handle_staging_digest_preview(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        tweet_ids = data.get("tweet_ids", [])
        if not tweet_ids:
            return web.json_response({"error": "No tweets selected"}, status=400)

        max_items = getattr(settings, "max_digest_items", 10)
        if len(tweet_ids) > max_items:
            return web.json_response({"error": f"Превышен лимит постов для контекста (макс {max_items})"}, status=400)

        await init_db()
        tweets = []
        for tid in tweet_ids:
            t = await get_tweet_by_id(tid)
            if t:
                tweets.append(t)

        from src.digest import generate_digest_with_gemini, format_digest_post
        digest_summary = await generate_digest_with_gemini(tweets)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        formatted = format_digest_post(digest_summary, date_str, header_title=data.get("title", ""))
        return web.json_response({"status": "success", "formatted_text": formatted, "tweets_count": len(tweets)})

    async def handle_staging_digest_publish(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        tweet_ids = data.get("tweet_ids", [])
        if not tweet_ids:
            return web.json_response({"error": "No tweets selected"}, status=400)

        max_items = getattr(settings, "max_digest_items", 10)
        if len(tweet_ids) > max_items:
            return web.json_response({"error": f"Превышен лимит постов для контекста (макс {max_items})"}, status=400)

        await init_db()
        title = data.get("title") or f"Дайджест {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        formatted_text = data.get("formatted_text")
        if not formatted_text:
            tweets = []
            for tid in tweet_ids:
                t = await get_tweet_by_id(tid)
                if t:
                    tweets.append(t)
            from src.digest import generate_digest_with_gemini, format_digest_post
            digest_summary = await generate_digest_with_gemini(tweets)
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            formatted_text = format_digest_post(digest_summary, date_str, header_title=title)

        digest_id = await create_digest_draft(title, formatted_text, tweet_ids)
        scheduled_at = data.get("scheduled_at")
        if scheduled_at:
            sched_utc = normalize_to_utc_iso(scheduled_at)
            import aiosqlite
            async with aiosqlite.connect(settings.db_path) as db:
                await db.execute(
                    "UPDATE digests SET status = 'SCHEDULED', scheduled_at = ? WHERE id = ?",
                    (sched_utc, digest_id)
                )
                await db.commit()
            return web.json_response({"status": "scheduled", "digest_id": digest_id, "scheduled_at": sched_utc})
        else:
            msg_id = None
            if settings.is_configured() and getattr(settings, "telegram_bot_token", None):
                try:
                    msg_id = await send_post_to_channel(formatted_text)
                except Exception as e:
                    print(f"⚠️ Ошибка отправки дайджеста в Telegram: {e}")
            await publish_digest(digest_id, telegram_message_id=msg_id)
            return web.json_response({"status": "published", "digest_id": digest_id})

    async def handle_create_manual_digest(request: web.Request) -> web.Response:
        return await handle_staging_digest_publish(request)

    async def handle_get_digests(request: web.Request) -> web.Response:
        await init_db()
        limit = int(request.query.get("limit", 50))
        digests = await get_digests_list(limit=limit)
        return web.json_response(digests)

    app.router.add_get("/api/staging/tweets", handle_get_staging_tweets)
    app.router.add_post("/api/staging/digest/preview", handle_staging_digest_preview)
    app.router.add_post("/api/staging/digest/publish", handle_staging_digest_publish)
    app.router.add_post("/api/staging/digest/create", handle_create_manual_digest)
    app.router.add_get("/api/digests", handle_get_digests)
    app.router.add_get("/api/digest-schedules", handle_get_digest_schedules)
    app.router.add_post("/api/digest-schedules", handle_create_digest_schedule)
    app.router.add_put("/api/digest-schedules/{id}", handle_update_digest_schedule)
    app.router.add_delete("/api/digest-schedules/{id}", handle_delete_digest_schedule)

    app.router.add_get("/api/system/backup", handle_system_backup)
    app.router.add_post("/api/system/export", handle_system_export_start)
    app.router.add_get("/api/system/export/status", handle_system_export_status)
    app.router.add_get("/api/system/export/download", handle_system_export_download)
    app.router.add_delete("/api/system/history", handle_system_history_delete)

    # Posts & Workflow
    app.router.add_get("/api/posts/queue", handle_get_queue)
    app.router.add_get("/api/posts/published", handle_get_published)
    app.router.add_post("/api/posts/{id}/schedule", handle_update_schedule)
    app.router.add_post("/api/posts/auto_schedule", handle_auto_schedule)
    app.router.add_post("/api/posts/reorder", handle_reorder_queue)
    app.router.add_post("/api/queue/batch-reprocess", handle_batch_reprocess_queue)
    app.router.add_post("/api/posts/{id}/publish_now", handle_publish_now)
    app.router.add_post("/api/posts/{id}/edit", handle_edit_post)
    app.router.add_post("/api/posts/{id}/reprocess", handle_reprocess_post)
    app.router.add_post("/api/posts/{id}/generate_summary", handle_generate_summary)
    app.router.add_delete("/api/posts/{id}", handle_delete_post)
    app.router.add_post("/api/telegram/sync", handle_sync_telegram)

    # Version & Status
    app.router.add_get("/api/version", handle_get_version)
    app.router.add_get("/api/status", handle_get_status)
    app.router.add_get("/api/settings", handle_get_settings)
    app.router.add_post("/api/settings", handle_update_settings)
    app.router.add_post("/api/pipeline/run", handle_run_pipeline)
    app.router.add_get("/api/pipeline/status", handle_pipeline_status)
    app.router.add_post("/api/digest/run", handle_run_digest)
    app.router.add_get("/api/system/health", handle_get_system_health)
    app.router.add_get("/api/system/history", handle_get_system_history)
    app.router.add_get("/api/system/filtered_tweets", handle_get_filtered_tweets)

    app.router.add_get("/api/topics", handle_get_topics)
    app.router.add_post("/api/topics", handle_create_topic)
    app.router.add_put("/api/topics/{id}", handle_update_topic)
    app.router.add_delete("/api/topics/{id}", handle_delete_topic)

    if STATIC_DIR.exists():
        app.router.add_static("/static", path=STATIC_DIR, name="static")

    video_dir = Path("data/video-temp")
    if video_dir.exists() or not video_dir.exists():
        video_dir.mkdir(parents=True, exist_ok=True)
        app.router.add_static("/videos", path=video_dir, name="videos")

    return app


async def start_web_server(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Starts the web dashboard server."""
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    try:
        await start_moderation_bot()
    except Exception as e:
        print(f"⚠️ Не удалось запустить Telegram-бота модерации: {e}")
    print(f"🌐 Дашборд Viral Hunter запущен на http://localhost:{port} (host: {host})")


def main() -> None:
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")
    app = create_app()
    print(f"🚀 Запуск веб-дашборда на http://localhost:{port}")
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()
