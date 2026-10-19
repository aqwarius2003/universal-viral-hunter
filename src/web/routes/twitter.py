import json
from pathlib import Path
from typing import Dict
from aiohttp import web
from src.config import settings
from src.db import save_system_settings_dual
from src.ai.agents.scout import server_twitter_login


async def handle_twitter_login(request: web.Request) -> web.Response:
    """Performs server-side login to X.com to avoid geo-blocking and generates cookies."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    auth_info_1 = str(data.get("auth_info_1") or data.get("username") or settings.twitter_username or "").strip()
    password = str(data.get("password") or settings.twitter_password or "")
    auth_info_2 = str(data.get("auth_info_2") or data.get("email") or settings.twitter_email or "").strip() or None
    code = str(data.get("code") or "").strip() or None
    proxy_url = data.get("proxy_url")

    if proxy_url:
        settings.twitter_proxy_url = str(proxy_url).strip()
        await save_system_settings_dual({"twitter_proxy_url": settings.twitter_proxy_url})

    if not auth_info_1 or not password:
        return web.json_response({"error": "Необходимо указать логин (username/email) и пароль"}, status=400)

    res = await server_twitter_login(
        auth_info_1=auth_info_1,
        password=password,
        auth_info_2=auth_info_2,
        code=code,
    )

    if res.get("status") in ("ok", "success"):
        updates = {"twitter_username": auth_info_1}
        if auth_info_2:
            updates["twitter_email"] = auth_info_2
        await save_system_settings_dual(updates)
        return web.json_response({**res, "status": "ok"})
    elif res.get("status") in ("code_required", "challenge_required"):
        return web.json_response({**res, "status": "code_required"}, status=200)
    else:
        return web.json_response(res, status=400)


async def handle_save_twitter_cookies(request: web.Request) -> web.Response:
    """Saves user-provided session cookies (auth_token & ct0 or full cookies JSON) directly."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    auth_token = str(data.get("auth_token") or "").strip()
    ct0 = str(data.get("ct0") or "").strip()
    raw_cookies = data.get("cookies")

    cookies_dict: Dict[str, str] = {}
    if isinstance(raw_cookies, dict):
        cookies_dict = {str(k): str(v) for k, v in raw_cookies.items()}
    elif isinstance(raw_cookies, list):
        for item in raw_cookies:
            if isinstance(item, dict) and "name" in item and "value" in item:
                cookies_dict[str(item["name"])] = str(item["value"])

    if auth_token:
        cookies_dict["auth_token"] = auth_token
    if ct0:
        cookies_dict["ct0"] = ct0

    final_auth_token = cookies_dict.get("auth_token")
    final_ct0 = cookies_dict.get("ct0")

    if not final_auth_token or not final_ct0:
        return web.json_response(
            {"error": "Необходимо указать как минимум auth_token и ct0"},
            status=400,
        )

    # Save to data/cookies.json and cookies.json
    target_p = Path(settings.cookies_path)
    target_p.parent.mkdir(parents=True, exist_ok=True)
    with open(target_p, "w", encoding="utf-8") as f:
        json.dump(cookies_dict, f, indent=2)

    root_p = Path("cookies.json")
    with open(root_p, "w", encoding="utf-8") as f:
        json.dump(cookies_dict, f, indent=2)

    # Update runtime settings and persistent .env
    settings.twitter_auth_token = final_auth_token
    settings.twitter_ct0 = final_ct0
    await save_system_settings_dual({
        "twitter_auth_token": final_auth_token,
        "twitter_ct0": final_ct0,
    })

    return web.json_response({
        "status": "ok",
        "message": f"Cookies успешно сохранены в {target_p} и применены в настройках!",
        "has_auth_token": True,
        "has_ct0": True,
    })


async def handle_twitter_debug_screenshot(request: web.Request) -> web.Response:
    """Returns the last login error screenshot if available."""
    target_p = Path(settings.cookies_path).parent / "login_failed.png"
    if not target_p.is_file():
        target_p = Path("data/login_failed.png")
    if not target_p.is_file():
        target_p = Path("login_failed.png")
    if not target_p.is_file():
        return web.json_response({"error": "Скриншот ошибки не найден"}, status=404)
    return web.FileResponse(target_p, headers={"Content-Type": "image/png"})


def register_twitter_routes(app: web.Application) -> None:
    app.router.add_post("/api/twitter/login", handle_twitter_login)
    app.router.add_post("/api/twitter/cookies", handle_save_twitter_cookies)
    app.router.add_get("/api/twitter/debug-screenshot", handle_twitter_debug_screenshot)
