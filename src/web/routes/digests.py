from datetime import datetime, timezone
import sys
from typing import Any
from aiohttp import web
import aiosqlite
from src.config import settings
from src.db import (
    init_db,
    get_staging_tweets,
    get_tweet_by_id,
    create_digest_draft,
    publish_digest,
    get_digests_list,
    normalize_to_utc_iso,
)
from src.telegram.sender import send_post_to_channel


def _resolve_dep(name: str, default: Any) -> Any:
    server = sys.modules.get("src.web.server")
    if server and hasattr(server, name):
        return getattr(server, name)
    return default



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
                _send_post = _resolve_dep("send_post_to_channel", send_post_to_channel)
                msg_id = await _send_post(formatted_text)
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


def register_digests_routes(app: web.Application) -> None:
    app.router.add_get("/api/staging/tweets", handle_get_staging_tweets)
    app.router.add_post("/api/staging/digest/preview", handle_staging_digest_preview)
    app.router.add_post("/api/staging/digest/publish", handle_staging_digest_publish)
    app.router.add_post("/api/staging/digest/create", handle_create_manual_digest)
    app.router.add_get("/api/digests", handle_get_digests)
