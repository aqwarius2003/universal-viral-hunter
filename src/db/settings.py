from datetime import datetime, timezone
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
from typing import Any, Dict, List, Optional
import aiosqlite
from src.config import settings
from src.db.schema import init_db


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
        target_env = env_path or os.environ.get("ENV_FILE_PATH") or getattr(settings, "env_file_path", ".env")
        is_testing = bool(
            os.environ.get("PYTEST_CURRENT_TEST")
            or os.environ.get("TESTING")
            or "pytest" in sys.modules
        )

        repo_env = (Path(__file__).resolve().parent.parent.parent / ".env").resolve()
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
