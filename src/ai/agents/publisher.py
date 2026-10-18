import html
import json
import os
from typing import Any, Dict, Optional
from src.ai.agents.state import AgentState
from src.config import settings, is_in_quiet_hours
from src.db import update_tweet_status_and_score, get_active_topics
from src.utils.hashtags import (
    normalize_hashtags,
    ensure_all_hashtags_have_hash,
    enforce_hashtags_in_post,
    extract_trailing_tags_from_summary,
)
from src.telegram.sender import send_post_to_channel, send_photo_to_channel, send_video_to_channel
from src.telegram.moderation_bot import build_admin_keyboard, delete_admin_moderation_message
from src.db import save_tweet_admin_message_id


def format_compact_num(val: Any) -> str:
    """Formats numeric values into concise K/M format (e.g. 1.2K, 324K, 1.5M)."""
    try:
        num = float(val)
    except (ValueError, TypeError):
        return str(val or 0)
    if num >= 1_000_000:
        return f"{num / 1_000_000:.1f}M".replace(".0M", "M")
    elif num >= 1_000:
        return f"{num / 1_000:.1f}K".replace(".0K", "K")
    return str(int(num))


def format_telegram_post(tweet_data: dict, ai_data: dict, is_photo: bool = False, max_caption_len: int = 1024) -> str:
    """
    Formats the JSON data into a clean, modern Telegram message with safe HTML author link.
    Guarantees every hashtag strictly starts with '#' and is formatted properly.
    If is_photo is True, compacts and safely limits the message to fit Telegram's 1024 character caption limit.
    """
    raw_takeaways = ai_data.get("takeaways", [])
    raw_hashtags = (
        ai_data.get("hashtags")
        or ai_data.get("hash_tags")
        or ai_data.get("tags")
        or ai_data.get("keywords")
        or []
    )
    normalized_hashtags = normalize_hashtags(raw_hashtags)

    title_ru = (ai_data.get("title_ru") or "").strip()
    summary_ru = (ai_data.get("summary_ru") or "").strip()

    # Extract any trailing unhashed tags that might be inside summary_ru
    summary_ru, extracted_tags = extract_trailing_tags_from_summary(summary_ru)
    if extracted_tags:
        existing_lower = {h.lower() for h in normalized_hashtags}
        for t in extracted_tags:
            if t.lower() not in existing_lower:
                existing_lower.add(t.lower())
                normalized_hashtags.append(t)

    clean_handle = (tweet_data.get("author_handle") or "").lstrip("@")
    raw_author_name = tweet_data.get("author_name") or clean_handle or "Unknown"
    safe_author_name = html.escape(raw_author_name)
    safe_clean_handle = html.escape(clean_handle)

    # Avoid redundant duplicate names like "Jawwwn (@jawwwn_)"
    if clean_handle and clean_handle.lower() != "unknown":
        if safe_clean_handle.lower() == safe_author_name.lower():
            author_display = f"@{safe_clean_handle}"
        else:
            author_display = safe_author_name
        author_link = f'<a href="https://x.com/{safe_clean_handle}">{author_display}</a>'
    else:
        author_link = safe_author_name

    show_views = getattr(settings, "show_metric_views", True)
    show_reposts = getattr(settings, "show_metric_reposts", True)
    show_likes = getattr(settings, "show_metric_likes", True)
    show_impact = getattr(settings, "show_metric_impact", True)
    show_author = getattr(settings, "show_metric_author", True)
    show_hashtags = getattr(settings, "show_metric_hashtags", True)

    metric_items = []
    if show_views:
        views_str = format_compact_num(tweet_data.get("impressions_count", 0))
        metric_items.append(f"👁 {views_str}")
    if show_reposts:
        reposts_str = format_compact_num(tweet_data.get("reposts_count", 0))
        metric_items.append(f"🔁 {reposts_str}")
    if show_likes:
        likes_str = format_compact_num(tweet_data.get("likes_count", 0))
        metric_items.append(f"❤️ {likes_str}")
    if show_impact:
        viral_score = tweet_data.get("viral_score", 0)
        if isinstance(viral_score, float):
            viral_score_str = f"{viral_score:.1f}"
        else:
            viral_score_str = str(viral_score)
        impact_score = ai_data.get("impact_score", 0)
        metric_items.append(f"⚡ {viral_score_str} · ⭐ {impact_score}/10")

    metrics_line = ("📊 " + " · ".join(metric_items)) if metric_items else ""
    author_line = f"𝕏 {author_link}" if show_author else ""

    max_caption_len = getattr(settings, "telegram_max_caption_len", max_caption_len)
    max_hashtags = getattr(settings, "max_hashtags", 5)
    show_takeaways = getattr(settings, "show_metric_takeaways", False)

    if not is_photo:
        # Full text message formatting
        takeaways_text = "\n".join([f"• {html.escape(t)}" for t in raw_takeaways]) if (show_takeaways and raw_takeaways) else ""
        hashtags_text = " ".join([html.escape(h) for h in normalized_hashtags[:max_hashtags]])
        if hashtags_text:
            hashtags_text = ensure_all_hashtags_have_hash(hashtags_text)

        safe_title = html.escape(title_ru)
        safe_summary = html.escape(summary_ru)

        parts = [f"<b>{safe_title}</b>\n"]
        mid_blocks = []
        if author_line:
            mid_blocks.append(author_line)
        if metrics_line:
            mid_blocks.append(metrics_line)
        if mid_blocks:
            parts.append("\n".join(mid_blocks) + "\n")
        parts.append(safe_summary)
        post = "\n".join(parts)
        if takeaways_text:
            post += f"\n\n💡 Ключевые тезисы:\n{takeaways_text}"
        if hashtags_text and show_hashtags:
            post += f"\n\n{hashtags_text}"
        return enforce_hashtags_in_post(post)

    # --- COMPACT FORMATTING FOR PHOTO/VIDEO CAPTIONS ---
    target_limit = max_caption_len - 24  # Leave safety cushion

    safe_title = html.escape(title_ru)
    compact_takeaways = list(raw_takeaways) if show_takeaways else []
    compact_hashtags = list(normalized_hashtags[:max_hashtags])

    def build_photo_text(summ: str, takeaways_list: list, htags_list: list) -> str:
        s_text = html.escape(summ)
        t_text = "\n".join([f"• {html.escape(t)}" for t in takeaways_list]) if takeaways_list else ""
        h_text = " ".join([html.escape(h) for h in htags_list]) if htags_list else ""
        if h_text:
            h_text = ensure_all_hashtags_have_hash(h_text)
        
        parts = [f"<b>{safe_title}</b>\n"]
        mid_blocks = []
        if author_line:
            mid_blocks.append(author_line)
        if metrics_line:
            mid_blocks.append(metrics_line)
        if mid_blocks:
            parts.append("\n".join(mid_blocks) + "\n")
        parts.append(s_text)
        if t_text:
            parts.append(f"\n💡 Тезисы:\n{t_text}")
        if h_text and show_hashtags:
            parts.append(f"\n{h_text}")
        return "\n".join(parts)

    current_summary = summary_ru
    current_takeaways = compact_takeaways
    current_hashtags = compact_hashtags

    result = build_photo_text(current_summary, current_takeaways, current_hashtags)

    # If exceeding limit, iteratively reduce takeaways
    while len(result) > target_limit and len(current_takeaways) > 1:
        current_takeaways.pop()
        result = build_photo_text(current_summary, current_takeaways, current_hashtags)

    # If still exceeding, drop hashtags
    if len(result) > target_limit and current_hashtags:
        current_hashtags = []
        result = build_photo_text(current_summary, current_takeaways, current_hashtags)

    # If still exceeding, drop takeaways completely
    if len(result) > target_limit and current_takeaways:
        current_takeaways = []
        result = build_photo_text(current_summary, current_takeaways, current_hashtags)

    # If still exceeding, trim summary safely before HTML escaping
    if len(result) > target_limit:
        excess = len(result) - target_limit
        trim_to = max(20, len(current_summary) - excess - 10)
        current_summary = current_summary[:trim_to].rstrip() + "..."
        result = build_photo_text(current_summary, [], [])

    # Final guarantee: ensure result never exceeds max_caption_len (1024)
    while len(result) > max_caption_len and len(current_summary) > 10:
        current_summary = current_summary[:-10].rstrip() + "..."
        result = build_photo_text(current_summary, [], [])

    return enforce_hashtags_in_post(result)


async def publisher_node(state: AgentState) -> Dict[str, Any]:
    """
    Publisher Agent: Publishes Top-N drafts to Telegram (with photo if selected),
    marks published as POSTED, and updates remaining candidates as PROCESSED.
    During quiet hours (sleep mode), skips posting and saves all drafts as PROCESSED.
    """
    draft_posts = state.get("draft_posts", [])
    ready_to_publish = []

    if is_in_quiet_hours():
        print("🌙 Режим тихих часов (сна) активен. Автоматическая публикация в Telegram пропущена, посты сохранены в очередь.")
        for draft in draft_posts:
            tweet_data = draft["original_data"]
            ai_data = draft["ai_data"]
            await update_tweet_status_and_score(
                tweet_id=draft["tweet_id"],
                score=tweet_data.get("viral_score", 0.0),
                is_viral=True,
                status="PROCESSED",
                impact_score=ai_data.get("impact_score", 0),
                summary_json=json.dumps(ai_data, ensure_ascii=False),
            )
        return {"ready_to_publish": []}

    # Fetch active topics to get channel routing
    active_topics = await get_active_topics()
    topics_map = {t["id"]: t for t in active_topics}

    # Limit posts to max_posts_per_run
    max_posts = max(1, settings.max_posts_per_run)
    posts_to_publish = draft_posts[:max_posts]
    remaining_posts = draft_posts[max_posts:]

    for draft in posts_to_publish:
        tweet_data = draft["original_data"]
        ai_data = draft["ai_data"]
        tweet_url = tweet_data["tweet_url"]
        media_urls = tweet_data.get("media_urls", [])
        video_data = tweet_data.get("video_data")

        # Topic routing
        topic_id = tweet_data.get("topic_id")
        topic = topics_map.get(topic_id, {})
        target_channel = topic.get("telegram_channel_id") or settings.telegram_channel_id

        # If approval is required, route to admin and use a mock for now
        approval_required = getattr(settings, "approval_required", False)
        if str(approval_required).lower() == "true":
            admin_id = getattr(settings, "telegram_admin_id", None)
            if admin_id:
                target_channel = admin_id
                print(f"⚠️ Режим модерации включен. Пост направляется администратору: {target_channel}")

        # Check for video
        video_local_path = None
        video_thumbnail = None
        if video_data and isinstance(video_data, dict):
            video_local_path = video_data.get("local_path")
            video_thumbnail = video_data.get("thumbnail")

            if video_local_path and not os.path.exists(video_local_path):
                video_local_path = None
            if not video_local_path and video_data.get("original_url"):
                try:
                    from src.utils.video_manager import download_video
                    video_local_path = await download_video(draft["tweet_id"], video_data["original_url"])
                except Exception as dl_err:
                    print(f"⚠️ Ошибка загрузки видео перед публикацией {draft['tweet_id']}: {dl_err}")

        # Check if a valid image index was selected
        selected_idx = ai_data.get("selected_image_index")
        photo_url = None
        if not video_local_path and selected_idx is not None and isinstance(selected_idx, int) and 0 <= selected_idx < len(media_urls):
            photo_url = media_urls[selected_idx]
        elif not video_local_path and media_urls and (selected_idx == 0 or selected_idx is None):
            photo_url = media_urls[0]
        elif not video_local_path and not photo_url and video_thumbnail:
            photo_url = video_thumbnail

        # 1. Format the message (compact if photo_url or video_local_path is present)
        formatted_text = format_telegram_post(tweet_data, ai_data, is_photo=bool(photo_url) or bool(video_local_path))

        # 2. Send to Telegram
        reply_markup = None
        is_approval = str(approval_required).lower() == "true"
        if is_approval and admin_id:
            reply_markup = build_admin_keyboard(draft["tweet_id"], tweet_url)

        sent_msg_id = None
        if video_local_path:
            sent_msg_id = await send_video_to_channel(
                formatted_text, 
                video_local_path, 
                thumbnail_path=video_thumbnail, 
                tweet_url=tweet_url, 
                channel_id_override=target_channel,
                reply_markup_override=reply_markup
            )
        elif photo_url:
            sent_msg_id = await send_photo_to_channel(formatted_text, photo_url, tweet_url, channel_id_override=target_channel, reply_markup_override=reply_markup)
        else:
            sent_msg_id = await send_post_to_channel(formatted_text, tweet_url, channel_id_override=target_channel, reply_markup_override=reply_markup)

        if is_approval and admin_id and sent_msg_id:
            await save_tweet_admin_message_id(draft["tweet_id"], sent_msg_id)
        elif not is_approval:
            # If directly published to channel, ensure any previous admin moderation card is deleted
            await delete_admin_moderation_message(draft["tweet_id"], action="published")
            try:
                from src.utils.video_manager import delete_cached_video
                delete_cached_video(draft["tweet_id"])
            except Exception:
                pass

        # 3. Update DB status. If moderation, mark as STAGING/PENDING, else POSTED
        new_status = "PENDING_APPROVAL" if (is_approval and admin_id) else "POSTED"

        ready_to_publish.append({
            "text": formatted_text,
            "tweet_url": tweet_url,
            "photo_url": photo_url,
            "video_local_path": video_local_path,
        })

        # 3. Update DB status to POSTED
        await update_tweet_status_and_score(
            tweet_id=draft["tweet_id"],
            score=tweet_data.get("viral_score", 0.0),
            is_viral=True,
            status=new_status,
            impact_score=ai_data.get("impact_score", 0),
            summary_json=json.dumps(ai_data, ensure_ascii=False),
        )

    # Save summary and status for remaining unprocessed drafts so they remain available for digest
    for draft in remaining_posts:
        tweet_data = draft["original_data"]
        ai_data = draft["ai_data"]
        await update_tweet_status_and_score(
            tweet_id=draft["tweet_id"],
            score=tweet_data.get("viral_score", 0.0),
            is_viral=True,
            status="PROCESSED",
            impact_score=ai_data.get("impact_score", 0),
            summary_json=json.dumps(ai_data, ensure_ascii=False),
        )

    return {"ready_to_publish": ready_to_publish}


async def publish_single_top_post(is_test: bool = False) -> Optional[Dict[str, Any]]:
    """
    Fetches queued posts sorted by virality score, picks the #1 top post,
    formats it and publishes it to Telegram.
    Returns the published post dict or None if queue is empty.
    """
    from src.db import get_queued_posts, normalize_to_utc_iso
    queued = await get_queued_posts()
    if not queued:
        return None

    now_iso = normalize_to_utc_iso()
    eligible = [p for p in queued if not p.get("scheduled_at") or p.get("scheduled_at") <= now_iso]
    if not eligible:
        return None

    due_scheduled = [p for p in eligible if p.get("scheduled_at")]
    top_draft = due_scheduled[0] if due_scheduled else eligible[0]
    tweet_url = top_draft["tweet_url"]
    media_urls = top_draft.get("media_urls", [])

    summary_data = None
    if top_draft.get("summary_json"):
        try:
            summary_data = json.loads(top_draft["summary_json"]) if isinstance(top_draft["summary_json"], str) else top_draft["summary_json"]
        except Exception:
            pass

    if not summary_data or not summary_data.get("title_ru"):
        summary_data = {
            "title_ru": top_draft.get("text_raw", "")[:60],
            "summary_ru": top_draft.get("text_raw", ""),
            "takeaways": ["Развитие AI"],
            "hashtags": ["#AI", "#TechNews"],
            "impact_score": top_draft.get("impact_score", 7) or 7,
        }

    video_data = top_draft.get("video_data")
    video_local_path = None
    video_thumbnail = None
    if video_data and isinstance(video_data, dict):
        video_local_path = video_data.get("local_path")
        video_thumbnail = video_data.get("thumbnail")

        if video_local_path and not os.path.exists(video_local_path):
            video_local_path = None
        if not video_local_path and video_data.get("original_url"):
            try:
                from src.utils.video_manager import download_video
                video_local_path = await download_video(top_draft["tweet_id"], video_data["original_url"])
            except Exception as dl_err:
                print(f"⚠️ Ошибка загрузки видео перед публикацией {top_draft['tweet_id']}: {dl_err}")

    selected_idx = summary_data.get("selected_image_index")
    photo_url = None
    if not video_local_path and selected_idx is not None and isinstance(selected_idx, int) and 0 <= selected_idx < len(media_urls):
        photo_url = media_urls[selected_idx]
    elif not video_local_path and media_urls and (selected_idx == 0 or selected_idx is None):
        photo_url = media_urls[0]
    elif not video_local_path and not photo_url and video_thumbnail:
        photo_url = video_thumbnail

    formatted_text = format_telegram_post(top_draft, summary_data, is_photo=bool(photo_url) or bool(video_local_path))

    if not is_test:
        if video_local_path:
            await send_video_to_channel(formatted_text, video_local_path, thumbnail_path=video_thumbnail, tweet_url=tweet_url)
        elif photo_url:
            await send_photo_to_channel(formatted_text, photo_url, tweet_url)
        else:
            await send_post_to_channel(formatted_text, tweet_url)

        await update_tweet_status_and_score(
            tweet_id=top_draft["tweet_id"],
            score=top_draft.get("viral_score", 0.0),
            is_viral=True,
            status="POSTED",
            impact_score=summary_data.get("impact_score", 0),
            summary_json=json.dumps(summary_data, ensure_ascii=False),
        )
        try:
            await delete_admin_moderation_message(top_draft["tweet_id"], action="published")
        except Exception as del_err:
            pass
        try:
            from src.utils.video_manager import delete_cached_video
            delete_cached_video(top_draft["tweet_id"])
        except Exception:
            pass
    else:
        top_draft["status"] = "POSTED"

    return {
        "tweet_id": top_draft["tweet_id"],
        "text": formatted_text,
        "tweet_url": tweet_url,
        "photo_url": photo_url,
        "video_local_path": video_local_path,
    }