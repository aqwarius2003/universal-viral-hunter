from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
import aiosqlite
from src.config import settings
from src.db.connection import _deserialize_row
from src.db.schema import init_db


async def record_pipeline_run(
    started_at: str,
    finished_at: str,
    duration_sec: float,
    tweets_found: int,
    viral_found: int,
    status: str,
    error_message: Optional[str] = None
) -> None:
    """Records the execution outcome of a pipeline run in the database."""
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            """
            INSERT INTO pipeline_runs (
                started_at, finished_at, duration_sec, tweets_found, viral_found, status, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (started_at, finished_at, duration_sec, tweets_found, viral_found, status, error_message)
        )
        await db.commit()


async def get_recent_pipeline_runs(limit: int = 10) -> List[Dict[str, Any]]:
    """Fetches recent pipeline runs ordered by run id descending."""
    if limit is None or limit <= 0:
        limit = 10
    limit = min(limit, 100)
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, started_at, finished_at, duration_sec, tweets_found, viral_found, status, error_message
            FROM pipeline_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def export_tweets_data_stream(start_date: str, end_date: str):
    """Yields all tweets between start_date and end_date one by one."""
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tweets WHERE created_at >= ? AND created_at <= ?",
            (start_date, end_date)
        ) as cursor:
            async for row in cursor:
                yield _deserialize_row(dict(row))


async def delete_historical_data(end_date: str) -> int:
    """Deletes old unposted tweets created before end_date, preserving POSTED and IGNORED deduplication memory."""
    async with aiosqlite.connect(settings.db_path) as db:
        cur = await db.execute("DELETE FROM tweets WHERE created_at <= ? AND status NOT IN ('POSTED', 'IGNORED')", (end_date,))
        await db.commit()
        # Also clean up pipeline runs, daily digests, etc if they take up space
        await db.execute("DELETE FROM pipeline_runs WHERE started_at <= ?", (end_date,))
        await db.execute("DELETE FROM daily_digests WHERE digest_date <= ?", (end_date[:10],))
        await db.commit()
        return cur.rowcount


async def cleanup_expired_posts(ttl_days: int) -> int:
    """
    Cleans up stale unposted RAW or FAILED tweets older than ttl_days (based on inserted_at).
    CRITICAL: Never deletes POSTED or IGNORED tweets, as they serve as the permanent
    deduplication memory (by tweet_id and video_hash) to prevent duplicate publications.
    """
    threshold = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat()
    async with aiosqlite.connect(settings.db_path) as db:
        # Strictly delete ONLY stale RAW or FAILED tweets by inserted_at, NEVER POSTED or IGNORED
        cur = await db.execute(
            "DELETE FROM tweets WHERE inserted_at <= ? AND status IN ('RAW', 'FAILED')",
            (threshold,)
        )
        await db.commit()
        return cur.rowcount


async def clear_all_content_data() -> Dict[str, int]:
    """
    Clears all content-related data (tweets, digests, pipeline runs) from SQLite,
    freeing database space and resetting tweet deduplication / video hashes.
    
    STRICTLY PRESERVES all configuration and system tables:
    - topics
    - system_settings
    - users
    - digest_schedules
    """
    await init_db()
    counts: Dict[str, int] = {}
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("PRAGMA foreign_keys = OFF;")
        
        tables_to_clear = [
            "digest_posts",
            "digest_items",
            "digests",
            "daily_digests",
            "pipeline_runs",
            "tweets",
        ]
        
        for table in tables_to_clear:
            try:
                cur = await db.execute(f"SELECT COUNT(*) FROM {table}")
                row = await cur.fetchone()
                counts[table] = int(row[0]) if row else 0
                await db.execute(f"DELETE FROM {table}")
            except Exception as e:
                print(f"⚠️ Ошибка очистки таблицы {table}: {e}")
                counts[table] = 0
            
        await db.commit()
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("VACUUM;")
        await db.commit()
        
    return counts
