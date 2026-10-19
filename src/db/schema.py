import os
from datetime import datetime, timezone
import aiosqlite
from src.config import settings


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
                status TEXT CHECK(status IN ('RAW', 'PROCESSED', 'SCHEDULED', 'POSTED', 'FAILED', 'IGNORED', 'IN_DIGEST', 'PROCESSING', 'PENDING_APPROVAL')) DEFAULT 'RAW',
                scheduled_at TIMESTAMP,
                queue_order INTEGER DEFAULT 0,
                posted_at TIMESTAMP,
                inserted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                digest_id INTEGER,
                topic_id INTEGER,
                video_data TEXT,
                video_hash TEXT,
                admin_message_id INTEGER
            );
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                search_queries TEXT NOT NULL,
                media_type TEXT CHECK(media_type IN ('video', 'photo', 'photo_video', 'any')) DEFAULT 'any',
                telegram_channel_id TEXT,
                system_prompt TEXT,
                prompt_template TEXT,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS digest_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT NOT NULL,
                title TEXT NOT NULL,
                max_items_per_post INTEGER NOT NULL DEFAULT 5,
                max_posts INTEGER NOT NULL DEFAULT 1,
                sort_order TEXT NOT NULL DEFAULT 'BY_VIRAL_SCORE',
                interval_minutes INTEGER NOT NULL DEFAULT 5,
                enabled BOOLEAN NOT NULL DEFAULT 1,
                last_run_date TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                if "topic_id" not in cols:
                    await db.execute("ALTER TABLE tweets ADD COLUMN topic_id INTEGER REFERENCES topics(id)")
                if "video_data" not in cols:
                    await db.execute("ALTER TABLE tweets ADD COLUMN video_data TEXT")
                if "video_hash" not in cols:
                    await db.execute("ALTER TABLE tweets ADD COLUMN video_hash TEXT")
                if "admin_message_id" not in cols:
                    await db.execute("ALTER TABLE tweets ADD COLUMN admin_message_id INTEGER")

        # Migrate legacy tweets CHECK constraint if needed (adds PROCESSING and PENDING_APPROVAL)
        async with db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tweets'") as cursor:
            tbl_row = await cursor.fetchone()
            if tbl_row and tbl_row[0] and "PROCESSING" not in tbl_row[0]:
                await db.execute("PRAGMA foreign_keys=OFF")
                await db.execute("ALTER TABLE tweets RENAME TO tweets_legacy_migration")
                await db.execute("""
                    CREATE TABLE tweets (
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
                        status TEXT CHECK(status IN ('RAW', 'PROCESSED', 'SCHEDULED', 'POSTED', 'FAILED', 'IGNORED', 'IN_DIGEST', 'PROCESSING', 'PENDING_APPROVAL')) DEFAULT 'RAW',
                        scheduled_at TIMESTAMP,
                        queue_order INTEGER DEFAULT 0,
                        posted_at TIMESTAMP,
                        inserted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        digest_id INTEGER,
                        topic_id INTEGER,
                        video_data TEXT,
                        video_hash TEXT,
                        admin_message_id INTEGER
                    )
                """)
                async with db.execute("PRAGMA table_info(tweets_legacy_migration)") as c_cursor:
                    legacy_cols = [r[1] for r in await c_cursor.fetchall()]
                target_cols = [
                    "tweet_id", "author_handle", "author_name", "text_raw", "tweet_url",
                    "created_at", "likes_count", "reposts_count", "quotes_count", "bookmarks_count",
                    "impressions_count", "viral_score", "impact_score", "is_viral", "media_urls",
                    "summary_json", "status", "scheduled_at", "queue_order", "posted_at",
                    "inserted_at", "digest_id", "topic_id", "video_data", "video_hash", "admin_message_id"
                ]
                common_cols = [col for col in target_cols if col in legacy_cols]
                cols_str = ", ".join(common_cols)
                await db.execute(f"INSERT INTO tweets ({cols_str}) SELECT {cols_str} FROM tweets_legacy_migration")
                await db.execute("DROP TABLE tweets_legacy_migration")
                await db.execute("PRAGMA foreign_keys=ON")
                await db.commit()

        # 2. Create indexes
        await db.execute("CREATE INDEX IF NOT EXISTS idx_viral_status ON tweets(is_viral, status);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON tweets(created_at);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_queue_order ON tweets(queue_order, scheduled_at);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tweets_video_hash ON tweets(video_hash);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_runs_id ON pipeline_runs(id DESC);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_digest_items_digest ON digest_items(digest_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_digest_items_tweet ON digest_items(tweet_id);")

        # 2b. Permanent deduplication memory table (never purged by maintenance/TTL)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS published_video_fingerprints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tweet_id TEXT NOT NULL UNIQUE,
                video_hash TEXT,
                video_url TEXT,
                status TEXT NOT NULL,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_pvf_tweet_id ON published_video_fingerprints(tweet_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_pvf_video_hash ON published_video_fingerprints(video_hash);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_pvf_video_url ON published_video_fingerprints(video_url);")

        # Backfill existing POSTED and IGNORED tweets into permanent memory
        await db.execute("""
            INSERT OR IGNORE INTO published_video_fingerprints (tweet_id, video_hash, status)
            SELECT tweet_id, video_hash, status FROM tweets WHERE status IN ('POSTED', 'IGNORED')
        """)

        # 3. Seed Zero-State / Default presets if not already present
        now_iso = datetime.now(timezone.utc).isoformat()
        default_presets = {
            "editor_system_prompt": "You are an expert AI news editor and technical analyst. Create a strict JSON output matching the schema. Output all JSON string values strictly in Russian. All hashtags must strictly start with '#' (e.g. #ИИ #Нейросети #Кибербезопасность). No emojis in title or takeaways. No conversational filler.",
            "editor_prompt_template": "Analyze this tweet: {text}\nAuthor: {author}\nDate: {date}\nMedia count: {media_count}\n\nGenerate Russian title, summary, 3 key takeaways, impact score (1-10), impact reason, and hashtags (starting with #).",
            "x_relevance_prompt": "Оцени релевантность твита для IT-специалистов. Ответь только JSON: {\"relevant\": true, \"reason\": \"...\"}",
            "x_search_queries": "min_faves:50, lang:en",
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
            "video_ttl_days": "2",
            "video_max_quality": "720",
            "video_max_size_mb": "50",
            "telegram_admin_id": "",
            "approval_required": "false",
            "show_metric_takeaways": "false",
            "max_title_words": "25",
            "telegram_max_caption_len": "1024",
            "telegram_max_text_len": "4096",
            "ai_temperature": "0.3",
            "max_hashtags": "5",
            "scout_search_count": "10",
            "scout_search_mode": "BOTH",
        }
        for k, v in default_presets.items():
            await db.execute(
                "INSERT OR IGNORE INTO system_settings (key, value, updated_at) VALUES (?, ?, ?)",
                (k, v, now_iso)
            )

        async with db.execute("SELECT COUNT(*) FROM digest_schedules") as cursor:
            schedule_count = (await cursor.fetchone())[0]
        if schedule_count == 0:
            async with db.execute("SELECT value FROM system_settings WHERE key = 'daily_digest_time'") as cursor:
                legacy_row = await cursor.fetchone()
            legacy_value = legacy_row[0] if legacy_row and legacy_row[0] else getattr(settings, "daily_digest_time", None)
            legacy_times = str(legacy_value).split(",") if legacy_value else []
            for raw_time in legacy_times:
                value = raw_time.strip()
                if value and len(value) == 5 and value[2] == ":":
                    await db.execute(
                        """
                        INSERT INTO digest_schedules
                        (time, title, max_items_per_post, max_posts, sort_order, interval_minutes)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (value, "Дайджест новостей", 5, 1, "BY_VIRAL_SCORE", 5),
                    )

        await db.commit()
