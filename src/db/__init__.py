import json
import os
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import aiosqlite
from src.config import settings, adjust_for_quiet_hours, get_app_timezone
from src.auth import hash_password, verify_password
from src.utils.hashtags import normalize_hashtags

from src.db.connection import (
    get_db_connection,
    get_db_path,
    _deserialize_row,
    normalize_to_utc_iso,
)
from src.db.schema import init_db
from src.db.tweets import (
    find_duplicate_video_in_db,
    filter_new_tweets,
    save_raw_tweets,
    update_tweet_status_and_score,
    update_tweet_metrics,
    get_tweets_last_24h,
    get_recent_active_tweets,
    get_tweet_by_id,
    set_tweet_status,
    get_filtered_tweets,
)
from src.db.posts import (
    get_queued_posts,
    get_unposted_tweets,
    get_published_posts,
    update_post_schedule,
    save_tweet_admin_message_id,
    get_tweet_admin_message_id,
    auto_schedule_queue,
    reorder_queue,
    update_post_content,
    cancel_scheduled_post,
    mark_post_published,
)
from src.db.users import (
    create_user,
    authenticate_user,
    get_user_by_username,
    get_all_users,
    update_user_password,
    delete_user,
    get_user_count,
)


async def get_digest_schedules() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM digest_schedules ORDER BY time, id") as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def save_digest_schedule(schedule: Dict[str, Any], schedule_id: Optional[int] = None) -> int:
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
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("DELETE FROM digest_schedules WHERE id = ?", (schedule_id,))
        await db.commit()


async def mark_digest_schedule_run(schedule_id: int, run_date: str) -> None:
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            "UPDATE digest_schedules SET last_run_date=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (run_date, schedule_id),
        )
        await db.commit()


async def get_digest_schedules_due(time_value: str, run_date: str) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM digest_schedules
            WHERE enabled = 1 AND time = ? AND (last_run_date IS NULL OR last_run_date <> ?)
            ORDER BY id""",
            (time_value, run_date),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

async def get_active_topics() -> List[Dict[str, Any]]:
    """Fetches all active topics."""
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM topics WHERE is_active = 1 ORDER BY id") as cursor:
            return [dict(row) for row in await cursor.fetchall()]

async def get_topics() -> List[Dict[str, Any]]:
    """Fetches all topics."""
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM topics ORDER BY id") as cursor:
            return [dict(row) for row in await cursor.fetchall()]

async def create_topic(topic: Dict[str, Any]) -> int:
    """Creates a new topic."""
    async with aiosqlite.connect(settings.db_path) as db:
        async with db.execute(
            """INSERT INTO topics (name, search_queries, media_type, telegram_channel_id, system_prompt, prompt_template, is_active)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                topic.get("name", "New Topic"),
                topic.get("search_queries", ""),
                topic.get("media_type", "any"),
                topic.get("telegram_channel_id", ""),
                topic.get("system_prompt", ""),
                topic.get("prompt_template", ""),
                topic.get("is_active", 1)
            )
        ) as cursor:
            await db.commit()
            return cursor.lastrowid

async def update_topic(topic_id: int, topic: Dict[str, Any]) -> None:
    """Updates an existing topic."""
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            """UPDATE topics SET 
                name = ?, search_queries = ?, media_type = ?, telegram_channel_id = ?, 
                system_prompt = ?, prompt_template = ?, is_active = ?
               WHERE id = ?""",
            (
                topic.get("name", ""),
                topic.get("search_queries", ""),
                topic.get("media_type", "any"),
                topic.get("telegram_channel_id", ""),
                topic.get("system_prompt", ""),
                topic.get("prompt_template", ""),
                topic.get("is_active", 1),
                topic_id
            )
        )
        await db.commit()

async def delete_topic(topic_id: int) -> None:
    """Deletes a topic."""
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("DELETE FROM topics WHERE id = ?", (topic_id,))
        await db.commit()


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




def is_secret_placeholder_or_empty(key: str, val: Any) -> bool:
    """Returns True if the value is empty, masked, or a template placeholder."""
    if val is None:
        return True
    s = str(val).strip()
    if not s or s.lower() in ("null", "undefined", "none"):
        return True
    if any(m in s for m in ("*", "...", "•••")):
        return True
    s_low = s.lower()
    if s_low.startswith("your_") or s_low.startswith("your-") or "change-me" in s_low or "your_gemini" in s_low or "your_api" in s_low:
        return True
    return False


async def save_system_settings_dual(settings_dict: Any, env_path: Optional[str] = None) -> None:
    """
    Dual-writes settings to SQLite system_settings table and .env file,
    and updates the active in-memory settings object.
    Protects against test overwrites, template placeholders, and keeps backups.
    Supports either a dict of updates or (key, value) arguments.
    """
    if isinstance(settings_dict, str):
        settings_dict = {settings_dict: env_path}
        env_path = None

    from pathlib import Path
    now_iso = datetime.now(timezone.utc).isoformat()
    env_updates: Dict[str, str] = {}

    secret_field_names = {"ai_api_key", "telegram_bot_token", "twitter_password", "secret_key"}

    await init_db()
    async with aiosqlite.connect(settings.db_path) as db:
        for key, val in settings_dict.items():
            if val is None:
                await db.execute("DELETE FROM system_settings WHERE key = ?", (key.lower(),))
                if hasattr(settings, key.lower()):
                    setattr(settings, key.lower(), None)
                continue
            str_val = str(val).strip()
            # Guard: ignore masked or placeholder values for secret fields
            is_secret = key.lower() in secret_field_names or any(s in key.lower() for s in ("secret", "token", "password")) or key.lower().endswith("_key")
            if is_secret and is_secret_placeholder_or_empty(key, str_val):
                continue

            await db.execute(
                """
                INSERT INTO system_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key.lower(), str_val, now_iso)
            )
            env_key = key.upper()
            if key.lower() not in {"editor_system_prompt", "editor_prompt_template", "x_relevance_prompt"}:
                env_updates[env_key] = str_val
            attr_name = key.lower()
            if hasattr(settings, attr_name):
                optional_bool_keys = {"daily_digest_enabled", "quiet_hours_enabled"}
                optional_int_keys = {
                    "scheduler_interval_minutes",
                    "digest_pre_sleep_minutes",
                    "digest_block_interval_minutes",
                    "daytime_publish_interval_minutes",
                    "scout_interval_minutes",
                }
                if attr_name in optional_bool_keys:
                    field_type = bool
                elif attr_name in optional_int_keys:
                    field_type = int
                else:
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
        import re

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
        skip_keys_upper = {"EDITOR_SYSTEM_PROMPT", "EDITOR_PROMPT_TEMPLATE", "X_RELEVANCE_PROMPT"}
        valid_key_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

        if os.path.exists(target_env):
            with open(target_env, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        env_lines.append(line)
                        continue

                    if "=" in stripped:
                        k, existing_val = stripped.split("=", 1)
                        k = k.strip()
                        if k in skip_keys_upper:
                            continue  # Prompts are managed in SQLite table system_settings
                        if not valid_key_re.match(k):
                            env_lines.append(f"# [invalid] {line}")
                            continue

                        existing_val = existing_val.strip().strip('"').strip("'")
                        if k in env_updates:
                            new_val = env_updates[k]
                            # Guard: never overwrite existing non-empty secret with empty or template placeholder
                            if k.lower() in secret_field_names and existing_val and not is_secret_placeholder_or_empty(k, existing_val) and is_secret_placeholder_or_empty(k, new_val):
                                env_lines.append(line)
                            else:
                                val_to_write = new_val.replace('"', '\\"').replace('\r', '').replace('\n', '\\n')
                                env_lines.append(f'{k}="{val_to_write}"\n')
                            existing_keys.add(k)
                        else:
                            env_lines.append(line)
                    else:
                        # Orphan line without '=' - comment it out so Docker Compose won't fail
                        env_lines.append(f"# [orphan] {line}")

        for k, v in env_updates.items():
            if k not in existing_keys and k not in skip_keys_upper:
                if k.lower() in secret_field_names and is_secret_placeholder_or_empty(k, v):
                    continue
                val_to_write = v.replace('"', '\\"').replace('\r', '').replace('\n', '\\n')
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
                    if key in {"ai_api_key", "telegram_bot_token", "twitter_password", "secret_key"} and is_secret_placeholder_or_empty(key, val):
                        continue
                    if hasattr(settings, key):
                        attr_val = getattr(settings, key)
                        optional_bool_keys = {"daily_digest_enabled", "quiet_hours_enabled"}
                        optional_int_keys = {
                            "scheduler_interval_minutes",
                            "digest_pre_sleep_minutes",
                            "digest_block_interval_minutes",
                            "daytime_publish_interval_minutes",
                            "scout_interval_minutes",
                        }
                        if key in optional_bool_keys:
                            field_type = bool
                        elif key in optional_int_keys:
                            field_type = int
                        else:
                            field_type = type(attr_val) if attr_val is not None else str
                        try:
                            if val is None:
                                setattr(settings, key, None)
                            elif field_type == bool:
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
            if settings.secret_key == "viral-hunter-secret-key-change-me":
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