from aiohttp import web
from src.auth import decode_session_token
from src.config import settings
from src.db import init_db, get_user_count


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """
    Authentication & Role-Based Access Control (RBAC) middleware.
    Protects API routes, extracts HMAC session tokens, and validates roles.
    """
    path = request.path

    # 1. Public resources (SPA UI, static files, favicon)
    if path in ("/", "/favicon.ico") or path.startswith("/static/"):
        return await handler(request)

    # 2. Public auth endpoints
    public_api_paths = (
        "/api/auth/setup_status",
        "/api/auth/setup",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/me",
    )
    if path in public_api_paths:
        return await handler(request)

    # 3. Global bypass if auth is disabled in settings (e.g. testing)
    if not getattr(settings, "auth_enabled", True):
        request["user"] = {"username": "admin", "role": "admin"}
        return await handler(request)

    # 4. Extract token from Bearer header or session_id cookie
    token = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif "session_id" in request.cookies:
        token = request.cookies.get("session_id")

    payload = decode_session_token(token, settings.secret_key) if token else None
    if not payload:
        await init_db()
        user_count = await get_user_count()
        if user_count == 0:
            return web.json_response(
                {"error": "Setup required", "needs_setup": True},
                status=401,
            )
        return web.json_response({"error": "Unauthorized"}, status=401)

    request["user"] = payload

    # 5. Role-based restrictions (RBAC)
    role = payload.get("role", "editor")
    admin_only_prefixes = (
        "/api/settings",
        "/api/digest-schedules",
        "/api/users",
        "/api/twitter/login",
        "/api/twitter/cookies",
        "/api/twitter/debug-screenshot",
        "/api/system/backup",
    )
    if any(path.startswith(p) for p in admin_only_prefixes):
        if role != "admin":
            return web.json_response(
                {"error": "Forbidden: Requires Admin privileges"},
                status=403,
            )

    return await handler(request)
