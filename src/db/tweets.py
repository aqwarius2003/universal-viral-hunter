from datetime import datetime, timezone, timedelta
import json
from typing import Any, Dict, List, Optional, Tuple
import aiosqlite
from src.config import settings
from src.db.connection import _deserialize_row


async def find_duplicate_video_in_db(
    video_hash: Optional[str] = None,
    video_url: Optional[str] = None,
    max_distance: int = 5,
    exclude_tweet_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Searches SQLite for any existing tweet with a matching or perceptually identical video hash
    or matching original video URL.
    Checks video URL match first, then exact hash match, then evaluates perceptual Hamming distance.
    """
    if not video_hash and not video_url:
        return None

    from src.utils.video_hasher import is_fingerprint_duplicate

    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row

        # 1. Exact video URL match if available
        if video_url:
            query_url = """
                SELECT tweet_id, tweet_url, status, author_handle, video_hash FROM tweets WHERE video_data LIKE ?
                UNION
                SELECT tweet_id, '' as tweet_url, status, '' as author_handle, video_hash FROM published_video_fingerprints WHERE video_url LIKE ?
            """
            params_url: List[Any] = [f"%{video_url}%", f"%{video_url}%"]
            if exclude_tweet_id:
                query_url = f"SELECT * FROM ({query_url}) WHERE tweet_id != ? LIMIT 1"
                params_url.append(exclude_tweet_id)
            else:
                query_url = f"SELECT * FROM ({query_url}) LIMIT 1"

            async with db.execute(query_url, params_url) as cursor:
                url_row = await cursor.fetchone()
                if url_row:
                    return dict(url_row)

        if not video_hash:
            return None

        # 2. Exact match fast path (indexed)
        query_exact = """
            SELECT tweet_id, tweet_url, status, author_handle, video_hash FROM tweets WHERE video_hash = ?
            UNION
            SELECT tweet_id, '' as tweet_url, status, '' as author_handle, video_hash FROM published_video_fingerprints WHERE video_hash = ?
        """
        params: List[Any] = [video_hash, video_hash]
        if exclude_tweet_id:
            query_exact = f"SELECT * FROM ({query_exact}) WHERE tweet_id != ? LIMIT 1"
            params.append(exclude_tweet_id)
        else:
            query_exact = f"SELECT * FROM ({query_exact}) LIMIT 1"

        async with db.execute(query_exact, params) as cursor:
            exact_row = await cursor.fetchone()
            if exact_row:
                return dict(exact_row)

        # 3. Perceptual match scan across all stored video fingerprints
        query_all = """
            SELECT tweet_id, tweet_url, status, author_handle, video_hash FROM tweets WHERE video_hash IS NOT NULL
            UNION
            SELECT tweet_id, '' as tweet_url, status, '' as author_handle, video_hash FROM published_video_fingerprints WHERE video_hash IS NOT NULL
        """
        params_all: List[Any] = []
        if exclude_tweet_id:
            query_all = f"SELECT * FROM ({query_all}) WHERE tweet_id != ?"
            params_all.append(exclude_tweet_id)

        async with db.execute(query_all, params_all) as cursor:
            rows = await cursor.fetchall()
            for r in rows:
                db_hash = r["video_hash"]
                if db_hash and is_fingerprint_duplicate(video_hash, db_hash, max_distance=max_distance):
                    return dict(r)

    return None


async def filter_new_tweets(tweets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Filters out tweets that already exist in the database by ID or contain duplicate video
    matching any existing tweet (even across different authors / tweet IDs).
    Checks both active tweets and permanent published_video_fingerprints history.
    """
    if not tweets:
        return []

    new_tweets: List[Dict[str, Any]] = []
    seen_batch_hashes: List[Tuple[str, str]] = []  # (video_hash, tweet_id)
    seen_batch_urls: List[Tuple[str, str]] = []  # (video_url, tweet_id)
    from src.utils.video_manager import delete_cached_video
    from src.utils.video_hasher import is_fingerprint_duplicate

    async with aiosqlite.connect(settings.db_path) as db:
        for tweet in tweets:
            tweet_id = tweet.get("tweet_id")

            # 1. Check tweet ID uniqueness in DB and permanent published memory
            async with db.execute(
                "SELECT 1 FROM tweets WHERE tweet_id = ? UNION SELECT 1 FROM published_video_fingerprints WHERE tweet_id = ?",
                (tweet_id, tweet_id)
            ) as cursor:
                exists = await cursor.fetchone()
                if exists:
                    continue

            # 2. Check video fingerprint deduplication
            v_hash = tweet.get("video_hash")
            v_data = tweet.get("video_data") or {}
            orig_v_url = v_data.get("original_url") if isinstance(v_data, dict) else None

            is_video_dup = False
            if v_hash or orig_v_url:
                # Check against existing tweets in DB
                db_dup = await find_duplicate_video_in_db(
                    video_hash=v_hash,
                    video_url=orig_v_url,
                    exclude_tweet_id=tweet_id
                )
                if db_dup:
                    print(
                        f"🚫 [Scout] Дубликат видео отсеян: твит {tweet_id} ({tweet.get('author_handle')}) "
                        f"содержит видео, идентичное {db_dup['tweet_id']} ({db_dup.get('author_handle')}, "
                        f"статус: {db_dup.get('status')}, {db_dup.get('tweet_url')})"
                    )
                    if tweet_id:
                        delete_cached_video(tweet_id)
                    is_video_dup = True
                else:
                    # Check against other tweets in the same incoming batch
                    if orig_v_url:
                        for prev_url, prev_id in seen_batch_urls:
                            if orig_v_url == prev_url:
                                print(
                                    f"🚫 [Scout] Дубликат URL видео в текущей пачке: твит {tweet_id} "
                                    f"дублирует {prev_id}. Пропускаем."
                                )
                                if tweet_id:
                                    delete_cached_video(tweet_id)
                                is_video_dup = True
                                break

                    if not is_video_dup and v_hash:
                        for prev_hash, prev_id in seen_batch_hashes:
                            if is_fingerprint_duplicate(v_hash, prev_hash):
                                print(
                                    f"🚫 [Scout] Дубликат видео в текущей пачке: твит {tweet_id} "
                                    f"дублирует {prev_id}. Пропускаем."
                                )
                                if tweet_id:
                                    delete_cached_video(tweet_id)
                                is_video_dup = True
                                break

            if not is_video_dup:
                if v_hash and tweet_id:
                    seen_batch_hashes.append((v_hash, tweet_id))
                if orig_v_url and tweet_id:
                    seen_batch_urls.append((orig_v_url, tweet_id))
                new_tweets.append(tweet)

    return new_tweets


async def save_raw_tweets(tweets: List[Dict[str, Any]]) -> None:
    """Saves new raw tweets to the database to prevent future duplicates."""
    if not tweets:
        return

    async with aiosqlite.connect(settings.db_path) as db:
        for tweet in tweets:
            media_urls_json = json.dumps(tweet.get("media_urls", []))
            video_data_json = json.dumps(tweet.get("video_data", {})) if tweet.get("video_data") else None
            video_hash = tweet.get("video_hash")
            await db.execute(
                """
                INSERT INTO tweets (
                    tweet_id, author_handle, author_name, text_raw, tweet_url, 
                    created_at, likes_count, reposts_count, quotes_count, 
                    bookmarks_count, impressions_count, viral_score, status, media_urls,
                    topic_id, video_data, video_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tweet["tweet_id"], tweet["author_handle"], tweet.get("author_name", tweet["author_handle"]),
                    tweet["text_raw"], tweet["tweet_url"], tweet["created_at"],
                    tweet.get("likes_count", 0), tweet.get("reposts_count", 0),
                    tweet.get("quotes_count", 0), tweet.get("bookmarks_count", 0),
                    tweet.get("impressions_count", 0), 0.0, 'RAW', media_urls_json,
                    tweet.get("topic_id"), video_data_json, video_hash
                )
            )
        await db.commit()


async def update_tweet_status_and_score(
    tweet_id: str,
    score: float,
    is_viral: bool,
    status: str,
    impact_score: int = 0,
    summary_json: Optional[str] = None
) -> None:
    """Updates the viral score, viral flag, impact score, and processing status of a tweet."""
    async with aiosqlite.connect(settings.db_path) as db:
        posted_clause = ", posted_at = COALESCE(posted_at, CURRENT_TIMESTAMP), scheduled_at = NULL" if status == "POSTED" else ""
        if summary_json is not None:
            await db.execute(
                f"""
                UPDATE tweets 
                SET viral_score = ?, is_viral = ?, status = ?, impact_score = ?, summary_json = ?{posted_clause}
                WHERE tweet_id = ?
                """,
                (score, is_viral, status, impact_score, summary_json, tweet_id)
            )
        else:
            await db.execute(
                f"""
                UPDATE tweets 
                SET viral_score = ?, is_viral = ?, status = ?, impact_score = ?{posted_clause}
                WHERE tweet_id = ?
                """,
                (score, is_viral, status, impact_score, tweet_id)
            )

        if status in ("POSTED", "IGNORED"):
            await db.execute("""
                INSERT INTO published_video_fingerprints (tweet_id, video_hash, status)
                SELECT tweet_id, video_hash, ? FROM tweets WHERE tweet_id = ?
                ON CONFLICT(tweet_id) DO UPDATE SET 
                    status = excluded.status, 
                    video_hash = COALESCE(excluded.video_hash, published_video_fingerprints.video_hash)
            """, (status, tweet_id))

        await db.commit()


async def update_tweet_metrics(
    tweet_id: str,
    likes: int,
    reposts: int,
    quotes: int,
    bookmarks: int,
    views: int,
    viral_score: float,
    is_viral: bool
) -> None:
    """Updates refreshed metrics for a tweet."""
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            """
            UPDATE tweets 
            SET likes_count = ?, reposts_count = ?, quotes_count = ?,
                bookmarks_count = ?, impressions_count = ?, viral_score = ?,
                is_viral = ?
            WHERE tweet_id = ?
            """,
            (likes, reposts, quotes, bookmarks, views, viral_score, is_viral, tweet_id)
        )
        await db.commit()


async def get_tweets_last_24h() -> List[Dict[str, Any]]:
    """Fetches tweets created or inserted in the last 24 hours for daily digest."""
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat()
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM tweets 
            WHERE created_at >= ? OR inserted_at >= datetime('now', '-24 hours')
            ORDER BY viral_score DESC
            """,
            (since,)
        ) as cursor:
            rows = await cursor.fetchall()
            results = []
            for r in rows:
                d = dict(r)
                if isinstance(d.get("media_urls"), str):
                    try:
                        d["media_urls"] = json.loads(d["media_urls"])
                    except Exception:
                        d["media_urls"] = []
                else:
                    d["media_urls"] = d.get("media_urls") or []
                results.append(d)
            return results


async def get_recent_active_tweets(hours: int = 48) -> List[Dict[str, Any]]:
    """Fetches tweets with status in ('RAW', 'PROCESSED') inserted/created within the last `hours` hours for metric refresh."""
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=hours)).isoformat()
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM tweets 
            WHERE status IN ('RAW', 'PROCESSED')
              AND (created_at >= ? OR inserted_at >= datetime('now', ?))
            ORDER BY viral_score DESC
            """,
            (since, f"-{hours} hours")
        ) as cursor:
            rows = await cursor.fetchall()
            return [_deserialize_row(dict(r)) for r in rows]


async def get_tweet_by_id(tweet_id: str) -> Optional[Dict[str, Any]]:
    """Fetches a single tweet record by its tweet_id."""
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tweets WHERE tweet_id = ?", (tweet_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return _deserialize_row(dict(row))
            return None


async def set_tweet_status(tweet_id: str, status: str) -> None:
    """Sets tweet status and tracks permanent fingerprints for POSTED and IGNORED."""
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("UPDATE tweets SET status = ? WHERE tweet_id = ?", (status, tweet_id))
        if status in ("POSTED", "IGNORED"):
            await db.execute("""
                INSERT INTO published_video_fingerprints (tweet_id, video_hash, status)
                SELECT tweet_id, video_hash, ? FROM tweets WHERE tweet_id = ?
                ON CONFLICT(tweet_id) DO UPDATE SET 
                    status = excluded.status, 
                    video_hash = COALESCE(excluded.video_hash, published_video_fingerprints.video_hash)
            """, (status, tweet_id))
        await db.commit()


async def get_filtered_tweets(
    limit: int = 50,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Возвращает твиты, отфильтрованные пайплайном (is_viral=0 или status=IGNORED).
    
    Используется для вкладки «Отфильтрованные твиты» в дашборде, чтобы редакторы
    могли вручную проверить пропущенные публикации (false negatives).
    """
    if limit is None or limit <= 0:
        limit = 50
    limit = min(limit, 500)

    conditions = ["(is_viral = 0 OR status = 'IGNORED')"]
    params: list = []

    if start_date:
        conditions.append("inserted_at >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("inserted_at <= ?")
        params.append(end_date)

    where_clause = " AND ".join(conditions)

    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""
            SELECT
                tweet_id, author_handle, author_name, text_raw, tweet_url,
                created_at, inserted_at, likes_count, reposts_count, quotes_count,
                bookmarks_count, impressions_count, viral_score, status, is_viral,
                media_urls
            FROM tweets
            WHERE {where_clause}
            ORDER BY inserted_at DESC
            LIMIT ?
            """,
            (*params, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            result = []
            for r in rows:
                row = dict(r)
                score = float(row.get("viral_score") or 0.0)
                status_val = row.get("status", "RAW")
                if status_val == "IGNORED":
                    row["rejection_reason"] = "Вручную отклонён или перемещён в архив"
                elif score < 1.0:
                    row["rejection_reason"] = "Нет показателей виральности (viral_score = 0)"
                else:
                    row["rejection_reason"] = f"Низкий вирал-индекс: {score:.1f} (ниже порога)"
                result.append(row)
            return result
