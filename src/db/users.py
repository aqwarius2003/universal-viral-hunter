from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import aiosqlite
from src.config import settings
from src.auth import hash_password, verify_password


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
