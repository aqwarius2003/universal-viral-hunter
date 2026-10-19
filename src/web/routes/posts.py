import os
import sys
from typing import Any
from aiohttp import web
from src.config import settings
from src.db import (
    init_db,
    get_queued_posts,
    get_published_posts,
    update_post_schedule,
    auto_schedule_queue,
    reorder_queue,
    get_tweet_by_id,
    mark_post_published,
    update_post_content,
    cancel_scheduled_post,
)
from src.web.helpers import _ensure_russian_summaries
from src.ai.agents.publisher import format_telegram_post
from src.utils.hashtags import normalize_hashtags, enforce_hashtags_in_post
from src.telegram.sender import send_photo_to_channel, send_post_to_channel, send_video_to_channel
from src.telegram.moderation_bot import (
    delete_admin_moderation_message,
    send_post_card_to_admin,
)
from src.utils.technical_audit import audit_and_prepare_telegram_content
from src.ai.agents.editor import reprocess_tweet_with_editor_feedback


def _resolve_dep(name: str, default: Any) -> Any:
    server = sys.modules.get("src.web.server")
    if server and hasattr(server, name):
        return getattr(server, name)
    return default


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

    # Pre-publish duplicate video check
    v_hash = tweet.get("video_hash")
    orig_v_url = None
    if isinstance(video_data, dict):
        if not v_hash:
            v_hash = video_data.get("fingerprint")
        orig_v_url = video_data.get("original_url")

    if v_hash or orig_v_url:
        from src.db import find_duplicate_video_in_db, update_tweet_status_and_score
        from src.utils.video_manager import delete_cached_video
        dup = await find_duplicate_video_in_db(
            video_hash=v_hash,
            video_url=orig_v_url,
            exclude_tweet_id=tweet_id
        )
        if dup and dup.get("status") in ("POSTED", "IGNORED"):
            status_text = "опубликовано ранее" if dup.get("status") == "POSTED" else "отклонено/удалено"
            await update_tweet_status_and_score(
                tweet_id=tweet_id,
                score=tweet.get("viral_score", 0.0),
                is_viral=False,
                status="IGNORED",
                impact_score=summary_data.get("impact_score", 0),
            )
            delete_cached_video(tweet_id)
            try:
                await delete_admin_moderation_message(tweet_id, action="deleted")
            except Exception:
                pass
            return web.json_response({
                "error": f"Видео из этого поста уже было {status_text} (твит {dup['tweet_id']}). Пост отменен как дубликат."
            }, status=400)

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
        _send_video = _resolve_dep("send_video_to_channel", send_video_to_channel)
        _send_photo = _resolve_dep("send_photo_to_channel", send_photo_to_channel)
        _send_post = _resolve_dep("send_post_to_channel", send_post_to_channel)
        if video_local_path:
            await _send_video(formatted_text, video_local_path, thumbnail_path=video_thumbnail, tweet_url=tweet_url)
        elif photo_url:
            await _send_photo(formatted_text, photo_url, tweet_url)
        else:
            await _send_post(formatted_text, tweet_url)

        await mark_post_published(tweet_id)
        # Immediate disk cleanup: purge temporary video cache
        try:
            from src.utils.video_manager import delete_cached_video
            delete_cached_video(tweet_id)
        except Exception:
            pass

        # Inbox Zero: delete moderation card from admin's Telegram chat
        try:
            _del_card = _resolve_dep("delete_admin_moderation_message", delete_admin_moderation_message)
            await _del_card(tweet_id, action="published")
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
                _send_card = _resolve_dep("send_post_card_to_admin", send_post_card_to_admin)
                await _send_card(updated_tweet)
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
        _reprocess = _resolve_dep("reprocess_tweet_with_editor_feedback", reprocess_tweet_with_editor_feedback)
        updated_tweet = await _reprocess(tweet_id, editor_instruction)
        if not updated_tweet:
            return web.json_response({"error": "Не удалось переработать пост через ИИ"}, status=500)
        return web.json_response({"status": "success", "tweet": updated_tweet})
    except Exception as e:
        return web.json_response({"error": f"Ошибка при переработке: {str(e)}"}, status=500)


async def handle_delete_post(request: web.Request) -> web.Response:
    """Cancels and archives a post, removing it from admin Telegram and purging cached media."""
    tweet_id = request.match_info["id"]
    await init_db()
    await cancel_scheduled_post(tweet_id)
    try:
        from src.utils.video_manager import delete_cached_video
        delete_cached_video(tweet_id)
    except Exception as ve:
        print(f"Failed to delete cached video for {tweet_id}: {ve}")
    try:
        _del_card = _resolve_dep("delete_admin_moderation_message", delete_admin_moderation_message)
        await _del_card(tweet_id, action="deleted")
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
    _send_card = _resolve_dep("send_post_card_to_admin", send_post_card_to_admin)
    for post in queued:
        try:
            ok = await _send_card(post)
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


def register_posts_routes(app: web.Application) -> None:
    app.router.add_get("/api/posts/queue", handle_get_queue)
    app.router.add_get("/api/posts/published", handle_get_published)
    app.router.add_post("/api/posts/{id}/schedule", handle_update_schedule)
    app.router.add_post("/api/posts/auto_schedule", handle_auto_schedule)
    app.router.add_post("/api/posts/reorder", handle_reorder_queue)
    app.router.add_post("/api/posts/{id}/publish_now", handle_publish_now)
    app.router.add_post("/api/posts/{id}/publish", handle_publish_now)
    app.router.add_post("/api/posts/{id}/edit", handle_edit_post)
    app.router.add_post("/api/posts/{id}/reprocess", handle_reprocess_post)
    app.router.add_delete("/api/posts/{id}", handle_delete_post)
    app.router.add_post("/api/telegram/sync", handle_sync_telegram)
