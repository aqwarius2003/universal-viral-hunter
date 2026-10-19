from typing import Any, Dict, List, Optional
import aiosqlite
from src.config import settings
from src.db.connection import _deserialize_row, normalize_to_utc_iso


async def get_digest_schedules() -> List[Dict[str, Any]]:
    """Fetches all digest schedules ordered by time and id."""
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM digest_schedules ORDER BY time, id") as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def save_digest_schedule(schedule: Dict[str, Any], schedule_id: Optional[int] = None) -> int:
    """Inserts or updates a digest schedule."""
    fields = (
        schedule["time"], schedule["title"], schedule["max_items_per_post"],
        schedule["max_posts"], schedule["sort_order"], schedule["interval_minutes"],
        int(schedule.get("enabled", True)),
    )
    async with aiosqlite.connect(settings.db_path) as db:
        if schedule_id is None:
            cursor = await db.execute(
                """INSERT INTO digest_schedules
                (time, title, max_items_per_post, max_posts, sort_order, interval_minutes, enabled, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""", fields
            )
            result_id = cursor.lastrowid
        else:
            await db.execute(
                """UPDATE digest_schedules SET time=?, title=?, max_items_per_post=?, max_posts=?,
                sort_order=?, interval_minutes=?, enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (*fields, schedule_id),
            )
            result_id = schedule_id
        await db.commit()
    return int(result_id)


async def delete_digest_schedule(schedule_id: int) -> None:
    """Deletes a digest schedule by its id."""
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("DELETE FROM digest_schedules WHERE id = ?", (schedule_id,))
        await db.commit()


async def mark_digest_schedule_run(schedule_id: int, run_date: str) -> None:
    """Marks a digest schedule as run on a specific date."""
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            "UPDATE digest_schedules SET last_run_date=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (run_date, schedule_id),
        )
        await db.commit()


async def get_digest_schedules_due(time_value: str, run_date: str) -> List[Dict[str, Any]]:
    """Fetches enabled digest schedules matching time_value that have not run on run_date."""
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM digest_schedules
            WHERE enabled = 1 AND time = ? AND (last_run_date IS NULL OR last_run_date <> ?)
            ORDER BY id""",
            (time_value, run_date),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def mark_digest_posted(digest_date: str, tweet_count: int = 0) -> None:
    """Marks a daily digest as posted to avoid duplicate runs."""
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO daily_digests (digest_date, tweet_count, posted_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            """,
            (digest_date, tweet_count)
        )
        await db.commit()


async def is_digest_posted(digest_date: str) -> bool:
    """Checks if a daily digest for the given date was already posted."""
    async with aiosqlite.connect(settings.db_path) as db:
        async with db.execute(
            "SELECT 1 FROM daily_digests WHERE digest_date = ?", (digest_date,)
        ) as cursor:
            return (await cursor.fetchone()) is not None


async def get_staging_tweets(strategy: str = "BY_VIRAL_SCORE", limit: int = 50) -> List[Dict[str, Any]]:
    """Fetches tweets for the staging zone based on the specified sorting strategy."""
    order_clause = "viral_score DESC"
    if strategy == "CHRONOLOGICAL_DESC":
        order_clause = "created_at DESC"
    elif strategy == "CHRONOLOGICAL_ASC":
        order_clause = "created_at ASC"
    elif strategy == "RANDOM":
        order_clause = "RANDOM()"
        
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""
            SELECT * FROM tweets
            WHERE status IN ('RAW', 'PROCESSED', 'SCHEDULED') 
            ORDER BY {order_clause}
            LIMIT ?
            """,
            (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [_deserialize_row(dict(r)) for r in rows]


async def create_digest_draft(title: str, formatted_text: str, tweet_ids: List[str]) -> int:
    """Creates a digest and associates it with tweets in digest_items with UTC ISO-8601 timestamps."""
    now_iso = normalize_to_utc_iso()
    async with aiosqlite.connect(settings.db_path) as db:
        cur = await db.execute(
            "INSERT INTO digests (title, formatted_text, status, created_at) VALUES (?, ?, 'DRAFT', ?)",
            (title, formatted_text, now_iso)
        )
        digest_id = cur.lastrowid
        
        for idx, tid in enumerate(tweet_ids):
            await db.execute(
                "INSERT OR REPLACE INTO digest_items (digest_id, tweet_id, added_at) VALUES (?, ?, ?)",
                (digest_id, tid, now_iso)
            )
            await db.execute(
                "INSERT OR REPLACE INTO digest_posts (digest_id, tweet_id, order_index, added_at) VALUES (?, ?, ?, ?)",
                (digest_id, tid, idx, now_iso)
            )
            await db.execute(
                "UPDATE tweets SET status = 'IN_DIGEST', digest_id = ? WHERE tweet_id = ?",
                (digest_id, tid)
            )
        await db.commit()
        return digest_id


async def publish_digest(digest_id: int, telegram_message_id: Optional[int] = None) -> bool:
    """Marks a digest as POSTED with UTC ISO-8601 timestamp, updates all linked tweets to POSTED."""
    now_iso = normalize_to_utc_iso()
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            """
            UPDATE digests 
            SET status = 'POSTED', posted_at = ?, telegram_message_id = ? 
            WHERE id = ?
            """,
            (now_iso, telegram_message_id, digest_id)
        )
        await db.execute(
            """
            UPDATE tweets 
            SET status = 'POSTED', posted_at = ? 
            WHERE tweet_id IN (SELECT tweet_id FROM digest_items WHERE digest_id = ?)
               OR digest_id = ?
            """,
            (now_iso, digest_id, digest_id)
        )
        await db.commit()
        return True


async def get_digests_list(limit: int = 50) -> List[Dict[str, Any]]:
    """Returns digests ordered by id DESC with their associated tweets from digest_items."""
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM digests ORDER BY id DESC LIMIT ?",
            (limit,)
        ) as cursor:
            digest_rows = await cursor.fetchall()
            digests = [dict(r) for r in digest_rows]

        for d in digests:
            did = d["id"]
            async with db.execute(
                """
                SELECT t.*, di.added_at as item_added_at
                FROM digest_items di
                JOIN tweets t ON di.tweet_id = t.tweet_id
                WHERE di.digest_id = ?
                ORDER BY di.added_at ASC
                """,
                (did,)
            ) as item_cursor:
                tweet_rows = await item_cursor.fetchall()
                if not tweet_rows:
                    async with db.execute(
                        """
                        SELECT t.*, dp.order_index
                        FROM digest_posts dp
                        JOIN tweets t ON dp.tweet_id = t.tweet_id
                        WHERE dp.digest_id = ?
                        ORDER BY dp.order_index ASC
                        """,
                        (did,)
                    ) as dp_cursor:
                        tweet_rows = await dp_cursor.fetchall()
                d["tweets"] = [_deserialize_row(dict(tr)) for tr in tweet_rows]
                d["tweet_count"] = len(d["tweets"])
        return digests


async def get_due_scheduled_digests() -> List[Dict[str, Any]]:
    """Fetches all scheduled digests that are due for publication."""
    now_iso = normalize_to_utc_iso()
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM digests 
            WHERE status = 'SCHEDULED' AND scheduled_at IS NOT NULL AND scheduled_at <= ?
            ORDER BY scheduled_at ASC
            """,
            (now_iso,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
