import json
import os
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import aiosqlite
from src.config import settings, adjust_for_quiet_hours, get_app_timezone
from src.auth import hash_password, verify_password
from src.utils.hashtags import normalize_hashtags

async def init_db() -> None:
    """Initializes the SQLite database with the required schema and migrations."""
    db_dir = os.path.dirname(settings.db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tweets (
                tweet_id TEXT PRIMARY KEY,
                author_handle TEXT NOT NULL,
                author_name TEXT,
                text_raw TEXT NOT NULL,
                tweet_url TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                likes_count INTEGER DEFAULT 0,
                reposts_count INTEGER DEFAULT 0,
                quotes_count INTEGER DEFAULT 0,
                bookmarks_count INTEGER DEFAULT 0,
                impressions_count INTEGER DEFAULT 0,
                viral_score REAL NOT NULL,
                impact_score INTEGER DEFAULT 0,
                is_viral BOOLEAN DEFAULT 0,
                media_urls TEXT DEFAULT '[]',
                summary_json TEXT,
                status TEXT CHECK(status IN ('RAW', 'PROCESSED', 'SCHEDULED', 'POSTED', 'FAILED', 'IGNORED', 'IN_DIGEST')) DEFAULT 'RAW',
                scheduled_at TIMESTAMP,
                queue_order INTEGER DEFAULT 0,
                posted_at TIMESTAMP,
                inserted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                digest_id INTEGER
            );
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_digests (
                digest_date TEXT PRIMARY KEY,
                tweet_count INTEGER DEFAULT 0,
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS digests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                formatted_text TEXT,
                status TEXT CHECK(status IN ('DRAFT', 'SCHEDULED', 'POSTED')) DEFAULT 'DRAFT',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                scheduled_at TIMESTAMP,
                posted_at TIMESTAMP,
                telegram_message_id INTEGER
            );
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS digest_posts (
                digest_id INTEGER,
                tweet_id TEXT,
                order_index INTEGER DEFAULT 0,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (digest_id, tweet_id),
                FOREIGN KEY (digest_id) REFERENCES digests(id) ON DELETE CASCADE,
                FOREIGN KEY (tweet_id) REFERENCES tweets(tweet_id) ON DELETE CASCADE
            );
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS digest_items (
                digest_id INTEGER NOT NULL,
                tweet_id TEXT NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (digest_id, tweet_id),
                FOREIGN KEY (digest_id) REFERENCES digests(id) ON DELETE CASCADE,
                FOREIGN KEY (tweet_id) REFERENCES tweets(tweet_id) ON DELETE CASCADE
            );
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                duration_sec REAL NOT NULL,
                tweets_found INTEGER DEFAULT 0,
                viral_found INTEGER DEFAULT 0,
                status TEXT NOT NULL,
                error_message TEXT
            );
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'editor')),
                created_at TIMESTAMP NOT NULL,
                last_login TIMESTAMP
            );
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL
            );
        """)
        # 1. Schema migration check for existing tables (must run before creating indexes on new columns)
        async with db.execute("PRAGMA table_info(tweets)") as cursor:
            cols = [row[1] for row in await cursor.fetchall()]
            if cols:
                if "media_urls" not in cols:
                    await db.execute("ALTER TABLE tweets ADD COLUMN media_urls TEXT DEFAULT '[]'")
                if "impact_score" not in cols:
                    await db.execute("ALTER TABLE tweets ADD COLUMN impact_score INTEGER DEFAULT 0")
                if "scheduled_at" not in cols:
                    await db.execute("ALTER TABLE tweets ADD COLUMN scheduled_at TIMESTAMP")
                if "queue_order" not in cols:
                    await db.execute("ALTER TABLE tweets ADD COLUMN queue_order INTEGER DEFAULT 0")
                if "digest_id" not in cols:
                    await db.execute("ALTER TABLE tweets ADD COLUMN digest_id INTEGER")

        # 2. Create indexes
        await db.execute("CREATE INDEX IF NOT EXISTS idx_viral_status ON tweets(is_viral, status);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON tweets(created_at);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_queue_order ON tweets(queue_order, scheduled_at);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_runs_id ON pipeline_runs(id DESC);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_digest_items_digest ON digest_items(digest_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_digest_items_tweet ON digest_items(tweet_id);")

        # 3. Seed Zero-State / Default presets if not already present
        now_iso = datetime.now(timezone.utc).isoformat()
        default_presets = {
            "editor_system_prompt": "You are an expert AI news editor and technical analyst. Create a strict JSON output matching the schema. Output all JSON string values strictly in Russian. All hashtags must strictly start with '#' (e.g. #ИИ #Нейросети #Кибербезопасность). No emojis in title or takeaways. No conversational filler.",
            "editor_prompt_template": "Analyze this tweet: {text}\nAuthor: {author}\nDate: {date}\nMedia count: {media_count}\n\nGenerate Russian title, summary, 3 key takeaways, impact score (1-10), impact reason, and hashtags (starting with #).",
            "x_relevance_prompt": "Оцени релевантность твита для IT-специалистов. Ответь только JSON: {\"relevant\": true, \"reason\": \"...\"}",
            "x_search_queries": "min_faves:50, lang:en",
            "scout_interval_minutes": "15",
            "max_digest_items": "10",
            "queue_sort_strategy": "BY_VIRAL_SCORE",
            "show_metric_likes": "true",
            "show_metric_reposts": "true",
            "show_metric_views": "true",
            "show_metric_impact": "true",
            "show_metric_author": "true",
            "show_metric_hashtags": "true",
            "show_metric_source_btn": "true",
            "backlog_digest_enabled": "true",
            "backlog_max_chunk_size": "6",
        }
        for k, v in default_presets.items():
            await db.execute(
                "INSERT OR IGNORE INTO system_settings (key, value, updated_at) VALUES (?, ?, ?)",
                (k, v, now_iso)
            )

        await db.commit()

async def filter_new_tweets(tweets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filters out tweets that already exist in the database."""
    if not tweets:
        return []
    
    new_tweets = []
    async with aiosqlite.connect(settings.db_path) as db:
        for tweet in tweets:
            async with db.execute(
                "SELECT 1 FROM tweets WHERE tweet_id = ?", (tweet["tweet_id"],)
            ) as cursor:
                exists = await cursor.fetchone()
                if not exists:
                    new_tweets.append(tweet)
    return new_tweets

async def save_raw_tweets(tweets: List[Dict[str, Any]]) -> None:
    """Saves new raw tweets to the database to prevent future duplicates."""
    if not tweets:
        return

    async with aiosqlite.connect(settings.db_path) as db:
        for tweet in tweets:
            media_urls_json = json.dumps(tweet.get("media_urls", []))
            await db.execute(
                """
                INSERT INTO tweets (
                    tweet_id, author_handle, author_name, text_raw, tweet_url, 
                    created_at, likes_count, reposts_count, quotes_count, 
                    bookmarks_count, impressions_count, viral_score, status, media_urls
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tweet["tweet_id"], tweet["author_handle"], tweet.get("author_name", tweet["author_handle"]),
                    tweet["text_raw"], tweet["tweet_url"], tweet["created_at"],
                    tweet.get("likes_count", 0), tweet.get("reposts_count", 0),
                    tweet.get("quotes_count", 0), tweet.get("bookmarks_count", 0),
                    tweet.get("impressions_count", 0), 0.0, 'RAW', media_urls_json
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

def _deserialize_row(row_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Helper to deserialize media_urls and summary_json into python objects."""
    d = dict(row_dict)
    if isinstance(d.get("media_urls"), str):
        try:
            d["media_urls"] = json.loads(d["media_urls"])
        except Exception:
            d["media_urls"] = []
    else:
        d["media_urls"] = d.get("media_urls") or []

    if isinstance(d.get("summary_json"), str) and d["summary_json"].strip():
        try:
            d["summary_data"] = json.loads(d["summary_json"])
            if isinstance(d["summary_data"], dict):
                raw_h = (
                    d["summary_data"].get("hashtags")
                    or d["summary_data"].get("hash_tags")
                    or d["summary_data"].get("tags")
                    or d["summary_data"].get("keywords")
                )
                if raw_h is not None:
                    d["summary_data"]["hashtags"] = normalize_hashtags(raw_h)
        except Exception:
            d["summary_data"] = None
    else:
        d["summary_data"] = None
    return d


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
            WHERE status IN ('PROCESSED', 'SCHEDULED')
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


async def auto_schedule_queue(
    interval_minutes: int = 120,
    start_offset_minutes: int = 30,
    tz_offset_minutes: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Automatically assigns spaced scheduled_at times to all queued posts, ranked by virality/impact and skipping quiet hours."""
    queued = await get_queued_posts()
    if not queued:
        return []

    # Sort queued posts by combined virality score descending
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
        await db.commit()


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


async def create_user(username: str, password: str, role: str = "editor") -> Dict[str, Any]:
    """Creates a new user with PBKDF2 salt and hash."""
    username = username.strip()
    if not username:
        raise ValueError("Имя пользователя не может быть пустым")
    if len(password) < 8:
        raise ValueError("Пароль должен содержать минимум 8 символов")
    if role not in ("admin", "editor"):
        raise ValueError("Роль пользователя должна быть 'admin' или 'editor'")

    pwd_hash, salt = hash_password(password)
    now_iso = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(settings.db_path) as db:
        try:
            await db.execute(
                """
                INSERT INTO users (username, password_hash, salt, role, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (username, pwd_hash, salt, role, now_iso)
            )
            await db.commit()
        except Exception as err:
            if "UNIQUE" in str(err) or "constraint failed" in str(err).lower():
                raise ValueError(f"Пользователь с именем '{username}' уже существует")
            raise

    return {"username": username, "role": role, "created_at": now_iso}


async def authenticate_user(username: str, password: str) -> Optional[Dict[str, Any]]:
    """Authenticates user credentials and returns user profile if valid."""
    username = username.strip()
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, username, password_hash, salt, role FROM users WHERE username = ?",
            (username,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None

            user = dict(row)
            if verify_password(password, user["salt"], user["password_hash"]):
                now_iso = datetime.now(timezone.utc).isoformat()
                await db.execute("UPDATE users SET last_login = ? WHERE id = ?", (now_iso, user["id"]))
                await db.commit()
                return {"id": user["id"], "username": user["username"], "role": user["role"]}
            return None


async def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    """Fetches user public profile by username."""
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, username, role, created_at, last_login FROM users WHERE username = ?",
            (username.strip(),)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_all_users() -> List[Dict[str, Any]]:
    """Fetches list of all users without sensitive password hashes."""
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, username, role, created_at, last_login FROM users ORDER BY id ASC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def update_user_password(username: str, new_password: str) -> bool:
    """Updates password for a user."""
    if len(new_password) < 8:
        raise ValueError("Пароль должен содержать минимум 8 символов")
    pwd_hash, salt = hash_password(new_password)
    async with aiosqlite.connect(settings.db_path) as db:
        cur = await db.execute(
            "UPDATE users SET password_hash = ?, salt = ? WHERE username = ?",
            (pwd_hash, salt, username.strip())
        )
        await db.commit()
        return cur.rowcount > 0


async def delete_user(username: str) -> bool:
    """Deletes a user, ensuring at least one admin remains."""
    username = username.strip()
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT role FROM users WHERE username = ?", (username,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False
            if row["role"] == "admin":
                async with db.execute("SELECT COUNT(*) as cnt FROM users WHERE role = 'admin'") as admin_cursor:
                    admin_row = await admin_cursor.fetchone()
                    if admin_row and admin_row["cnt"] <= 1:
                        raise ValueError("Нельзя удалить единственного администратора системы")

        cur = await db.execute("DELETE FROM users WHERE username = ?", (username,))
        await db.commit()
        return cur.rowcount > 0


async def get_user_count() -> int:
    """Returns total number of registered users."""
    async with aiosqlite.connect(settings.db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


def is_secret_placeholder_or_empty(key: str, val: Any) -> bool:
    """Returns True if the value is empty, masked, or a template placeholder."""
    if val is None:
        return True
    s = str(val).strip()
    if not s or s.lower() in ("null", "undefined", "none"):
        return True
    if any(m in s for m in ("*", "...", "•••")):
        return True
    if s.startswith("your_") or s.startswith("your-") or "change-me" in s.lower():
        return True
    return False


async def save_system_settings_dual(settings_dict: Dict[str, Any], env_path: Optional[str] = None) -> None:
    """
    Dual-writes settings to SQLite system_settings table and .env file,
    and updates the active in-memory settings object.
    Protects against test overwrites, template placeholders, and keeps backups.
    """
    from pathlib import Path
    now_iso = datetime.now(timezone.utc).isoformat()
    env_updates: Dict[str, str] = {}

    async with aiosqlite.connect(settings.db_path) as db:
        for key, val in settings_dict.items():
            if val is None:
                continue
            str_val = str(val).strip()
            # Guard: ignore masked values so secrets aren't overwritten by asterisks or ellipses
            if is_secret_placeholder_or_empty(key, str_val):
                # Check if an existing valid secret exists in SQLite
                async with db.execute("SELECT value FROM system_settings WHERE key = ?", (key.lower(),)) as cur:
                    row = await cur.fetchone()
                    if row and row[0] and not is_secret_placeholder_or_empty(key, row[0]):
                        continue  # Never overwrite an existing valid setting with empty/placeholder/mask

            await db.execute(
                """
                INSERT INTO system_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key.lower(), str_val, now_iso)
            )
            env_key = key.upper()
            env_updates[env_key] = str_val
            attr_name = key.lower()
            if hasattr(settings, attr_name):
                field_type = type(getattr(settings, attr_name))
                try:
                    if field_type == bool:
                        setattr(settings, attr_name, str_val.lower() in ("true", "1", "yes"))
                    elif field_type == int:
                        setattr(settings, attr_name, int(str_val))
                    elif field_type == float:
                        setattr(settings, attr_name, float(str_val))
                    else:
                        setattr(settings, attr_name, str_val)
                except Exception:
                    setattr(settings, attr_name, str_val)
        await db.commit()

    # Sync to .env file safely
    try:
        from pathlib import Path
        import sys

        target_env = env_path or os.environ.get("ENV_FILE_PATH") or getattr(settings, "env_file_path", ".env")
        is_testing = bool(
            os.environ.get("PYTEST_CURRENT_TEST")
            or os.environ.get("TESTING")
            or "pytest" in sys.modules
        )

        repo_env = (Path(__file__).resolve().parent.parent / ".env").resolve()
        target_env_path = Path(target_env).resolve()

        # Never touch the root .env file if running in pytest without explicit isolated ENV_FILE_PATH
        if is_testing:
            if not os.environ.get("ENV_FILE_PATH") and not env_path:
                return
            if target_env_path == repo_env:
                return

        # Automatic backup before mutating .env
        if os.path.exists(target_env):
            try:
                import shutil
                shutil.copy2(target_env, f"{target_env}.bak")
            except Exception:
                pass

        env_lines = []
        existing_keys = set()
        if os.path.exists(target_env):
            with open(target_env, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#") and "=" in stripped:
                        k, existing_val = stripped.split("=", 1)
                        k = k.strip()
                        existing_val = existing_val.strip().strip('"').strip("'")
                        if k in env_updates:
                            new_val = env_updates[k]
                            # Guard: never overwrite existing non-empty secret with empty or template placeholder
                            if existing_val and not is_secret_placeholder_or_empty(k, existing_val) and is_secret_placeholder_or_empty(k, new_val):
                                env_lines.append(line)
                            else:
                                val_to_write = new_val.replace('"', '\\"')
                                env_lines.append(f'{k}="{val_to_write}"\n')
                            existing_keys.add(k)
                        else:
                            env_lines.append(line)
                    else:
                        env_lines.append(line)

        for k, v in env_updates.items():
            if k not in existing_keys and v and not is_secret_placeholder_or_empty(k, v):
                val_to_write = v.replace('"', '\\"')
                env_lines.append(f'{k}="{val_to_write}"\n')

        with open(target_env, "w", encoding="utf-8") as f:
            f.writelines(env_lines)
    except Exception as e:
        print(f"⚠️ Ошибка синхронизации .env файла: {e}")


async def load_system_settings_into_config() -> None:
    """Loads settings stored in SQLite system_settings into the settings object in memory."""
    import secrets
    try:
        async with aiosqlite.connect(settings.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT key, value FROM system_settings") as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    key = row["key"].lower()
                    val = row["value"]
                    if hasattr(settings, key):
                        attr_val = getattr(settings, key)
                        field_type = type(attr_val) if attr_val is not None else str
                        try:
                            if field_type == bool:
                                setattr(settings, key, val.lower() in ("true", "1", "yes"))
                            elif field_type == int:
                                setattr(settings, key, int(val))
                            elif field_type == float:
                                setattr(settings, key, float(val))
                            else:
                                setattr(settings, key, val)
                        except Exception:
                            setattr(settings, key, val)

            # Ensure persistent cryptographically strong secret_key if default is still used
            if settings.secret_key == "ai-viral-hunter-secret-key-change-me":
                async with db.execute("SELECT value FROM system_settings WHERE key = 'secret_key'") as cur:
                    row = await cur.fetchone()
                    if row and row["value"]:
                        settings.secret_key = row["value"]
                    else:
                        new_secret = secrets.token_hex(32)
                        settings.secret_key = new_secret
                        now_iso = datetime.now(timezone.utc).isoformat()
                        await db.execute(
                            "INSERT INTO system_settings (key, value, updated_at) VALUES ('secret_key', ?, ?)",
                            (new_secret, now_iso)
                        )
                        await db.commit()
    except Exception as e:
        print(f"⚠️ load_system_settings_into_config: {e}")

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
    """Deletes all tweets created before end_date."""
    async with aiosqlite.connect(settings.db_path) as db:
        cur = await db.execute("DELETE FROM tweets WHERE created_at <= ?", (end_date,))
        await db.commit()
        # Also clean up pipeline runs, daily digests, etc if they take up space
        await db.execute("DELETE FROM pipeline_runs WHERE started_at <= ?", (end_date,))
        await db.execute("DELETE FROM daily_digests WHERE digest_date <= ?", (end_date[:10],))
        await db.commit()
        return cur.rowcount

async def cleanup_expired_posts(ttl_days: int) -> int:
    """Deletes old POSTED, IGNORED, FAILED, and old RAW posts older than ttl_days to free disk space."""
    threshold = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat()
    async with aiosqlite.connect(settings.db_path) as db:
        # We can delete all tweets that are older than the threshold, regardless of status,
        # but to be safe, maybe only delete those that are already POSTED or IGNORED or RAW.
        cur = await db.execute(
            "DELETE FROM tweets WHERE created_at <= ? AND status IN ('POSTED', 'IGNORED', 'FAILED', 'RAW')",
            (threshold,)
        )
        await db.commit()
        return cur.rowcount

def normalize_to_utc_iso(dt_input: Any = None) -> str:
    """Normalizes any datetime or ISO string to strict UTC ISO-8601 string ending with 'Z'."""
    if dt_input is None:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(dt_input, datetime):
        if dt_input.tzinfo is None:
            dt = dt_input.replace(tzinfo=timezone.utc)
        else:
            dt = dt_input.astimezone(timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    if isinstance(dt_input, (int, float)):
        dt = datetime.fromtimestamp(dt_input, tz=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    if isinstance(dt_input, str):
        s = dt_input.strip()
        if not s:
            return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt.isoformat().replace("+00:00", "Z")
        except Exception:
            return s
    return str(dt_input)

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