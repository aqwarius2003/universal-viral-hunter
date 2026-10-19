from typing import Any, Dict
from aiohttp import web
from src.config import settings
from src.auth import (
    create_session_token,
    decode_session_token,
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
)
from src.web.state import login_limiter, get_client_ip


async def handle_setup_status(request: web.Request) -> web.Response:
    """Checks whether the system requires initial setup (0 users)."""
    await init_db()
    count = await get_user_count()
    return web.json_response({
        "needs_setup": count == 0,
        "user_count": count,
        "is_configured": settings.is_configured(),
    })


async def handle_setup(request: web.Request) -> web.Response:
    """Performs initial admin account creation and optional settings configuration."""
    await init_db()
    count = await get_user_count()
    if count > 0:
        return web.json_response({"error": "Первичная настройка уже завершена"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))

    if len(username) < 3:
        return web.json_response({"error": "Имя пользователя должно содержать минимум 3 символа"}, status=400)
    if len(password) < 8:
        return web.json_response({"error": "Пароль должен содержать минимум 8 символов"}, status=400)

    try:
        user = await create_user(username, password, role="admin")
    except Exception as e:
        return web.json_response({"error": f"Ошибка создания администратора: {e}"}, status=400)

    initial_settings: Dict[str, Any] = {}
    for key in (
        "ai_api_key",
        "ai_model_name",
        "telegram_bot_token",
        "telegram_channel_id",
        "twitter_username",
        "twitter_email",
        "twitter_password",
        "twitter_proxy_url",
    ):
        if key in data and data[key] and not str(data[key]).startswith("***"):
            initial_settings[key] = data[key]

    if initial_settings:
        await save_system_settings_dual(initial_settings)

    token = create_session_token(user["username"], user["role"], settings.secret_key)
    response = web.json_response({
        "status": "setup_completed",
        "user": {"username": user["username"], "role": user["role"]},
        "token": token,
    })
    response.set_cookie(
        "session_id",
        token,
        max_age=14 * 86400,
        httponly=True,
        samesite="Lax",
        path="/"
    )
    return response


async def handle_login(request: web.Request) -> web.Response:
    """Authenticates user credentials with brute-force rate limiting."""
    ip = get_client_ip(request)
    if login_limiter.is_locked(ip):
        rem = login_limiter.lockout_remaining(ip)
        return web.json_response(
            {"error": f"Слишком много неудачных попыток. Попробуйте через {rem} сек."},
            status=429,
        )

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))

    await init_db()
    user = await authenticate_user(username, password)
    if not user:
        login_limiter.record_failure(ip)
        return web.json_response({"error": "Неверный логин или пароль"}, status=401)

    login_limiter.reset(ip)
    token = create_session_token(user["username"], user["role"], settings.secret_key)
    response = web.json_response({
        "status": "authenticated",
        "user": {"id": user["id"], "username": user["username"], "role": user["role"]},
        "token": token,
    })
    response.set_cookie(
        "session_id",
        token,
        max_age=14 * 86400,
        httponly=True,
        samesite="Lax",
        path="/"
    )
    return response


async def handle_logout(request: web.Request) -> web.Response:
    """Terminates active session."""
    response = web.json_response({"status": "logged_out"})
    response.del_cookie("session_id", path="/")
    return response


async def handle_me(request: web.Request) -> web.Response:
    """Returns currently authenticated user profile."""
    token = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif "session_id" in request.cookies:
        token = request.cookies.get("session_id")

    payload = decode_session_token(token, settings.secret_key) if token else None
    if not payload:
        return web.json_response({"authenticated": False, "user": None})

    await init_db()
    db_user = await get_user_by_username(payload["username"])
    if not db_user:
        return web.json_response({"authenticated": False, "user": None})

    return web.json_response({
        "authenticated": True,
        "user": {
            "id": db_user["id"],
            "username": db_user["username"],
            "role": db_user["role"],
            "last_login": db_user.get("last_login"),
        }
    })


async def handle_get_users(request: web.Request) -> web.Response:
    """Returns list of registered users."""
    await init_db()
    users = await get_all_users()
    return web.json_response(users)


async def handle_create_user(request: web.Request) -> web.Response:
    """Creates a new user (Admin or Editor)."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    role = str(data.get("role", "editor")).strip()

    if len(username) < 3:
        return web.json_response({"error": "Имя пользователя должно содержать от 3 символов"}, status=400)
    if len(password) < 8:
        return web.json_response({"error": "Пароль должен содержать от 8 символов"}, status=400)
    if role not in ("admin", "editor"):
        return web.json_response({"error": "Роль должна быть 'admin' или 'editor'"}, status=400)

    await init_db()
    try:
        new_user = await create_user(username, password, role=role)
        return web.json_response(new_user, status=201)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        return web.json_response({"error": f"Ошибка создания пользователя: {e}"}, status=400)


async def handle_update_user_password(request: web.Request) -> web.Response:
    """Updates password for a user."""
    target_username = request.match_info["username"]
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    new_password = str(data.get("password", ""))
    if len(new_password) < 8:
        return web.json_response({"error": "Пароль должен содержать минимум 8 символов"}, status=400)

    await init_db()
    try:
        success = await update_user_password(target_username, new_password)
        if not success:
            return web.json_response({"error": "Пользователь не найден"}, status=404)
        return web.json_response({"status": "password_updated", "username": target_username})
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)


async def handle_delete_user(request: web.Request) -> web.Response:
    """Deletes a user, guarding the last remaining admin."""
    target_username = request.match_info["username"]
    current_user = request.get("user", {})
    if current_user.get("username") == target_username:
        return web.json_response({"error": "Нельзя удалить текущего авторизованного пользователя"}, status=400)

    await init_db()
    try:
        success = await delete_user(target_username)
        if not success:
            return web.json_response({"error": "Пользователь не найден"}, status=404)
        return web.json_response({"status": "deleted", "username": target_username})
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)


def register_auth_routes(app: web.Application) -> None:
    app.router.add_get("/api/auth/setup_status", handle_setup_status)
    app.router.add_post("/api/auth/setup", handle_setup)
    app.router.add_post("/api/auth/login", handle_login)
    app.router.add_post("/api/auth/logout", handle_logout)
    app.router.add_get("/api/auth/me", handle_me)
    app.router.add_get("/api/users", handle_get_users)
    app.router.add_post("/api/users", handle_create_user)
    app.router.add_post("/api/users/{username}/password", handle_update_user_password)
    app.router.add_delete("/api/users/{username}", handle_delete_user)
