from datetime import datetime, timedelta
import json
from typing import Any, Dict, List, Optional
import aiosqlite
from src.config import settings, adjust_for_quiet_hours, get_app_timezone
from src.utils.hashtags import normalize_hashtags
from src.db.connection import _deserialize_row
from src.db.tweets import get_tweet_by_id


async def get_queued_posts(strategy: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetches all pending/scheduled posts ordered by manual queue_order, or automatically by selected strategy."""
    strat = strategy or getattr(settings, "queue_sort_strategy", "BY_VIRAL_SCORE")
    if strat == "CHRONOLOGICAL_DESC":
        auto_order = "created_at DESC, inserted_at DESC"
    elif strat == "CHRONOLOGICAL_ASC":
        auto_order = "created_at ASC, inserted_at ASC"
    elif strat == "RANDOM":
        auto_order = "RANDOM()"
    else:
        auto_order = "(viral_score * CASE WHEN impact_score > 0 THEN impact_score ELSE 7 END) DESC, viral_score DESC, CASE WHEN scheduled_at IS NOT NULL THEN scheduled_at ELSE inserted_at END ASC"

    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""
            SELECT * FROM tweets
            WHERE status IN ('PROCESSED', 'SCHEDULED', 'PROCESSING', 'PENDING_APPROVAL')
            ORDER BY 
                CASE WHEN queue_order > 0 THEN 0 ELSE 1 END,
                queue_order ASC,
                {auto_order}
            """
        ) as cursor:
            rows = await cursor.fetchall()
            return [_deserialize_row(dict(r)) for r in rows]


async def get_unposted_tweets() -> List[Dict[str, Any]]:
    """Fetches all unposted tweets (RAW, PROCESSED, SCHEDULED) ordered by virality score descending."""
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM tweets
            WHERE status IN ('RAW', 'PROCESSED', 'SCHEDULED')
            ORDER BY 
                (viral_score * CASE WHEN impact_score > 0 THEN impact_score ELSE 7 END) DESC,
                viral_score DESC,
                inserted_at ASC
            """
        ) as cursor:
            rows = await cursor.fetchall()
            return [_deserialize_row(dict(r)) for r in rows]


async def get_published_posts(limit: int = 50) -> List[Dict[str, Any]]:
    """Fetches recently published posts ordered by publication time descending."""
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM tweets
            WHERE status = 'POSTED'
            ORDER BY posted_at DESC, inserted_at DESC
            LIMIT ?
            """,
            (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [_deserialize_row(dict(r)) for r in rows]


async def update_post_schedule(tweet_id: str, scheduled_at: Optional[str]) -> None:
    """Updates the scheduled publication date/time for a post."""
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            """
            UPDATE tweets
            SET scheduled_at = ?, status = 'PROCESSED'
            WHERE tweet_id = ?
            """,
            (scheduled_at, tweet_id)
        )
        await db.commit()


async def save_tweet_admin_message_id(tweet_id: str, admin_message_id: Optional[int]) -> None:
    """Saves or clears the Telegram admin message ID for a post."""
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            "UPDATE tweets SET admin_message_id = ? WHERE tweet_id = ?",
            (admin_message_id, tweet_id)
        )
        await db.commit()


async def get_tweet_admin_message_id(tweet_id: str) -> Optional[int]:
    """Fetches the Telegram admin message ID for a post."""
    async with aiosqlite.connect(settings.db_path) as db:
        async with db.execute(
            "SELECT admin_message_id FROM tweets WHERE tweet_id = ?",
            (tweet_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0] is not None:
                return int(row[0])
            return None


async def auto_schedule_queue(
    interval_minutes: int = 120,
    start_offset_minutes: int = 30,
    tz_offset_minutes: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Automatically assigns spaced scheduled_at times to all queued posts, ranked by virality/impact and skipping quiet hours."""
    if interval_minutes is None or interval_minutes <= 0:
        return []

    queued = await get_queued_posts()
    if not queued:
        return []

    queued.sort(
        key=lambda p: (
            float(p.get("viral_score", 0.0)) * (p.get("impact_score") or 7),
            float(p.get("viral_score", 0.0)),
        ),
        reverse=True,
    )

    tz = get_app_timezone(tz_offset_minutes)
    now = datetime.now(tz).replace(second=0, microsecond=0)
    current_target = now + timedelta(minutes=start_offset_minutes)
    current_target = adjust_for_quiet_hours(current_target)

    async with aiosqlite.connect(settings.db_path) as db:
        for idx, post in enumerate(queued, start=1):
            current_target = adjust_for_quiet_hours(current_target)
            scheduled_iso = current_target.isoformat()
            await db.execute(
                """
                UPDATE tweets
                SET scheduled_at = ?, queue_order = ?, status = 'PROCESSED'
                WHERE tweet_id = ?
                """,
                (scheduled_iso, idx, post["tweet_id"])
            )
            current_target += timedelta(minutes=interval_minutes)
            current_target = adjust_for_quiet_hours(current_target)
        await db.commit()

    return await get_queued_posts()


async def reorder_queue(ordered_ids: List[str]) -> None:
    """Updates queue_order for the given list of tweet_ids (1, 2, 3...)."""
    async with aiosqlite.connect(settings.db_path) as db:
        for idx, tweet_id in enumerate(ordered_ids, start=1):
            await db.execute(
                "UPDATE tweets SET queue_order = ? WHERE tweet_id = ?",
                (idx, tweet_id)
            )
        await db.commit()


async def update_post_content(
    tweet_id: str,
    title_ru: str,
    summary_ru: str,
    takeaways: List[str],
    hashtags: List[str],
    selected_image_index: Optional[int] = None,
    impact_score: Optional[int] = None
) -> None:
    """Updates the edited text, takeaways, hashtags and selected image in summary_json."""
    tweet = await get_tweet_by_id(tweet_id)
    if not tweet:
        return

    summary_data = tweet.get("summary_data") or {}
    summary_data["title_ru"] = title_ru
    summary_data["summary_ru"] = summary_ru
    summary_data["takeaways"] = takeaways
    summary_data["hashtags"] = normalize_hashtags(hashtags)
    if selected_image_index is not None:
        summary_data["selected_image_index"] = selected_image_index
    if impact_score is not None:
        summary_data["impact_score"] = impact_score

    summary_json_str = json.dumps(summary_data, ensure_ascii=False)
    final_impact_score = impact_score if impact_score is not None else tweet.get("impact_score", 0)

    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            """
            UPDATE tweets
            SET summary_json = ?, impact_score = ?
            WHERE tweet_id = ?
            """,
            (summary_json_str, final_impact_score, tweet_id)
        )
        await db.commit()


async def cancel_scheduled_post(tweet_id: str) -> None:
    """Cancels/archives a scheduled post by changing its status to IGNORED."""
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            "UPDATE tweets SET status = 'IGNORED' WHERE tweet_id = ?",
            (tweet_id,)
        )
        await db.execute("""
            INSERT INTO published_video_fingerprints (tweet_id, video_hash, status)
            SELECT tweet_id, video_hash, 'IGNORED' FROM tweets WHERE tweet_id = ?
            ON CONFLICT(tweet_id) DO UPDATE SET status = 'IGNORED'
        """, (tweet_id,))
        await db.commit()


async def mark_post_published(tweet_id: str) -> None:
    """Marks a post as POSTED with current timestamp."""
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            """
            UPDATE tweets
            SET status = 'POSTED', posted_at = CURRENT_TIMESTAMP
            WHERE tweet_id = ?
            """,
            (tweet_id,)
        )
        await db.execute("""
            INSERT INTO published_video_fingerprints (tweet_id, video_hash, status)
            SELECT tweet_id, video_hash, 'POSTED' FROM tweets WHERE tweet_id = ?
            ON CONFLICT(tweet_id) DO UPDATE SET status = 'POSTED'
        """, (tweet_id,))
        await db.commit()
