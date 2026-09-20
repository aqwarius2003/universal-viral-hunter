import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from aiohttp.test_utils import TestClient, TestServer

from src.config import settings
from src.auth import (
    hash_password,
    verify_password,
    create_session_token,
    decode_session_token,
    LoginRateLimiter,
    mask_secret,
)
from src.db import (
    init_db,
    create_user,
    authenticate_user,
    get_user_by_username,
    get_all_users,
    update_user_password,
    delete_user,
    get_user_count,
    save_system_settings_dual,
    load_system_settings_into_config,
)
from src.web.server import create_app


def test_password_hashing():
    pwd = "superSecretPassword123!"
    pwd_hash, salt = hash_password(pwd)
    assert pwd_hash is not None
    assert salt is not None
    assert len(salt) == 32
    assert verify_password(pwd, salt, pwd_hash) is True
    assert verify_password("wrongPassword", salt, pwd_hash) is False
    assert verify_password(pwd, "wrongSalt", pwd_hash) is False


def test_session_token():
    secret = "test-secret-key-12345"
    token = create_session_token("admin", "admin", secret, max_age_seconds=3600)
    assert token is not None

    payload = decode_session_token(token, secret)
    assert payload is not None
    assert payload["username"] == "admin"
    assert payload["role"] == "admin"

    # Tampered token
    tampered = token[:-4] + "abcd"
    assert decode_session_token(tampered, secret) is None

    # Wrong secret
    assert decode_session_token(token, "wrong-secret") is None

    # Expired token
    expired_token = create_session_token("admin", "admin", secret, max_age_seconds=-10)
    assert decode_session_token(expired_token, secret) is None


def test_rate_limiter():
    limiter = LoginRateLimiter(max_attempts=3, window_seconds=60, lockout_seconds=120)
    ip = "192.168.1.50"

    assert limiter.is_locked(ip) is False
    limiter.record_failure(ip)
    assert limiter.is_locked(ip) is False
    limiter.record_failure(ip)
    assert limiter.is_locked(ip) is False
    limiter.record_failure(ip)
    # 3rd failure locks it
    assert limiter.is_locked(ip) is True
    assert limiter.lockout_remaining(ip) > 0

    # Reset clears lockout
    limiter.reset(ip)
    assert limiter.is_locked(ip) is False


def test_mask_secret():
    assert mask_secret(None) == ""
    assert mask_secret("") == ""
    assert mask_secret("short") == "s***t"
    long_key = "AIzaSyD1234567890abcdefghijklmnop"
    masked = mask_secret(long_key)
    assert masked.startswith("AIzaSyD")
    assert masked.endswith("mnop")
    assert "..." in masked


@pytest.mark.asyncio
async def test_user_db_crud(tmp_path, monkeypatch):
    db_file = tmp_path / "test_users.sqlite"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    await init_db()

    assert await get_user_count() == 0

    # Create admin
    admin = await create_user("admin", "adminPassword123", role="admin")
    assert admin["username"] == "admin"
    assert admin["role"] == "admin"
    assert await get_user_count() == 1

    # Duplicate username check
    with pytest.raises(ValueError, match="уже существует"):
        await create_user("admin", "anotherPassword", role="editor")

    # Short password check
    with pytest.raises(ValueError, match="минимум 8 символов"):
        await create_user("editor1", "short", role="editor")

    # Create editor
    editor = await create_user("editor1", "editorPassword123", role="editor")
    assert editor["role"] == "editor"
    assert await get_user_count() == 2

    # Authenticate
    assert await authenticate_user("admin", "adminPassword123") is not None
    assert await authenticate_user("admin", "wrong") is None
    assert await authenticate_user("nonexistent", "wrong") is None

    # Get all users (no hashes exposed)
    users = await get_all_users()
    assert len(users) == 2
    for u in users:
        assert "password_hash" not in u
        assert "salt" not in u

    # Update password
    assert await update_user_password("editor1", "newEditorPassword123") is True
    assert await authenticate_user("editor1", "newEditorPassword123") is not None
    assert await authenticate_user("editor1", "editorPassword123") is None

    # Delete guard: cannot delete last admin
    with pytest.raises(ValueError, match="единственного администратора"):
        await delete_user("admin")

    # Delete editor
    assert await delete_user("editor1") is True
    assert await get_user_count() == 1


@pytest.mark.asyncio
async def test_dual_write_settings(tmp_path, monkeypatch):
    db_file = tmp_path / "test_dual_settings.sqlite"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    await init_db()

    new_settings = {
        "min_viral_score": 92.5,
        "max_posts_per_run": 3,
        "ai_model_name": "gemini-test",
    }
    await save_system_settings_dual(new_settings)

    assert settings.min_viral_score == 92.5
    assert settings.max_posts_per_run == 3
    assert settings.ai_model_name == "gemini-test"

    # Reset in-memory settings and load from SQLite
    settings.min_viral_score = 50.0
    await load_system_settings_into_config()
    assert settings.min_viral_score == 92.5


@pytest.mark.asyncio
async def test_auth_and_rbac_flow(tmp_path, monkeypatch):
    """Full integration test for setup wizard, login, logout, rate limiting, and RBAC."""
    db_file = tmp_path / "test_auth_app.sqlite"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "auth_enabled", True)
    await init_db()

    app = create_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    try:
        # 1. Setup Status initially needs_setup=True
        resp = await client.get("/api/auth/setup_status")
        assert resp.status == 200
        data = await resp.json()
        assert data["needs_setup"] is True
        assert data["user_count"] == 0

        # 2. Unauthenticated request to protected endpoint returns 401 with needs_setup=True
        resp = await client.get("/api/posts/queue")
        assert resp.status == 401
        data = await resp.json()
        assert data.get("needs_setup") is True

        # 3. Setup with invalid body
        resp = await client.post("/api/auth/setup", json={"username": "ad", "password": "123"})
        assert resp.status == 400

        # 4. Valid Setup creates admin
        resp = await client.post("/api/auth/setup", json={
            "username": "admin",
            "password": "adminPassword123!",
            "ai_api_key": "AIzaSyD-valid-test-key",
            "telegram_channel_id": "@test_channel",
        })
        assert resp.status == 200
        setup_data = await resp.json()
        assert setup_data["status"] == "setup_completed"
        assert setup_data["user"]["username"] == "admin"
        assert setup_data["user"]["role"] == "admin"
        admin_token = setup_data["token"]

        # Setup status is now False
        resp = await client.get("/api/auth/setup_status")
        assert resp.status == 200
        assert (await resp.json())["needs_setup"] is False

        # Attempting setup again returns 403
        resp = await client.post("/api/auth/setup", json={"username": "hacker", "password": "hackerPassword123"})
        assert resp.status == 403

        # 5. Rate limiting on login
        for _ in range(5):
            bad_resp = await client.post("/api/auth/login", json={"username": "admin", "password": "wrongPassword"})
            assert bad_resp.status == 401

        # 6th attempt locked out
        locked_resp = await client.post("/api/auth/login", json={"username": "admin", "password": "wrongPassword"})
        assert locked_resp.status == 429
        assert "Слишком много неудачных попыток" in (await locked_resp.json())["error"]

        # 6. Admin accesses settings using Bearer Token
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        resp = await client.get("/api/settings", headers=admin_headers)
        assert resp.status == 200
        settings_data = await resp.json()
        # Secrets must be masked
        assert "AIzaSyD" in settings_data["ai_api_key"]
        assert "..." in settings_data["ai_api_key"]

        # Updating settings without changing masked secret doesn't overwrite it
        resp = await client.post("/api/settings", headers=admin_headers, json={
            "min_viral_score": 88.0,
            "ai_api_key": settings_data["ai_api_key"],  # masked key
        })
        assert resp.status == 200
        assert settings.min_viral_score == 88.0
        assert settings.ai_api_key == "AIzaSyD-valid-test-key"  # original secret kept!

        # 7. Create Editor user as Admin
        resp = await client.post("/api/users", headers=admin_headers, json={
            "username": "editor_bob",
            "password": "editorBobPassword123!",
            "role": "editor",
        })
        assert resp.status == 201
        editor_info = await resp.json()
        assert editor_info["username"] == "editor_bob"
        assert editor_info["role"] == "editor"

        # 8. Create Editor Token
        editor_token = create_session_token("editor_bob", "editor", settings.secret_key)
        editor_headers = {"Authorization": f"Bearer {editor_token}"}

        # Editor CAN access posts queue
        resp = await client.get("/api/posts/queue", headers=editor_headers)
        assert resp.status == 200

        # Editor CANNOT access settings (403)
        resp = await client.get("/api/settings", headers=editor_headers)
        assert resp.status == 403

        # Editor CANNOT access user management (403)
        resp = await client.get("/api/users", headers=editor_headers)
        assert resp.status == 403

        # Editor CANNOT access Twitter server login (403)
        resp = await client.post("/api/twitter/login", headers=editor_headers, json={})
        assert resp.status == 403

        # Editor CANNOT download DB backup (403)
        resp = await client.get("/api/system/backup", headers=editor_headers)
        assert resp.status == 403

        # 9. Health endpoint redaction for Editor
        with patch("src.web.server.check_twitter_health", AsyncMock(return_value={"name": "X", "status": "ok", "message": "OK", "details": {"secret": "token_123"}})), \
             patch("src.web.server.check_gemini_health", AsyncMock(return_value={"name": "Gemini", "status": "ok", "message": "OK", "details": {"api_key": "raw_key"}})), \
             patch("src.web.server.check_telegram_health", AsyncMock(return_value={"name": "TG", "status": "ok", "message": "OK", "details": {"token": "tg_token"}})):

            # As Admin: details present
            admin_health = await client.get("/api/system/health", headers=admin_headers)
            assert admin_health.status == 200
            admin_data = await admin_health.json()
            assert "secret" in admin_data["services"]["twitter"]["details"]

            # As Editor: details redacted to empty dict
            editor_health = await client.get("/api/system/health", headers=editor_headers)
            assert editor_health.status == 200
            editor_data = await editor_health.json()
            assert editor_data["services"]["twitter"]["details"] == {}
            assert editor_data["services"]["gemini"]["details"] == {}
            assert editor_data["services"]["telegram"]["details"] == {}

        # 10. Database Backup for Admin
        resp = await client.get("/api/system/backup", headers=admin_headers)
        assert resp.status == 200
        assert "tweets_backup.sqlite" in resp.headers.get("Content-Disposition", "")

        # 11. Logout
        resp = await client.post("/api/auth/logout")
        assert resp.status == 200

    finally:
        await client.close()
