import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict
from aiohttp import web
from src.version import get_version, get_version_info
from src.ai.agents.editor import get_ai_status as default_get_ai_status
from src.db import (
    init_db,
    get_recent_pipeline_runs,
    get_filtered_tweets,
    export_tweets_data_stream,
    delete_historical_data,
    clear_all_content_data,
)
from src.web.state import STATIC_DIR
from src.web.health import (
    check_twitter_health as default_check_twitter_health,
    check_gemini_health as default_check_gemini_health,
    check_telegram_health as default_check_telegram_health,
    _compute_overall_status,
)


def _resolve_dep(name: str, default: Any) -> Any:
    server = sys.modules.get("src.web.server")
    if server and hasattr(server, name):
        return getattr(server, name)
    return default


export_jobs: Dict[str, Dict[str, Any]] = {}


async def handle_index(request: web.Request) -> web.Response:
    """Serves the main SPA index.html."""
    html_path = STATIC_DIR / "index.html"
    if not html_path.is_file():
        return web.Response(text="index.html not found", status=404)
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    return web.Response(text=content, content_type="text/html", charset="utf-8")


async def handle_favicon(request: web.Request) -> web.Response:
    """Returns a simple modern SVG lightning bolt favicon."""
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="#24a1de"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>'
    return web.Response(text=svg, content_type="image/svg+xml", charset="utf-8")


async def handle_get_version(request: web.Request) -> web.Response:
    """Returns application version information."""
    return web.json_response(get_version_info())


async def handle_get_status(request: web.Request) -> web.Response:
    """Returns real-time status of AI models and rate limiting."""
    status_getter = _resolve_dep("get_ai_status", default_get_ai_status)
    status_info = status_getter()
    return web.json_response(status_info)


async def handle_get_system_health(request: web.Request) -> web.Response:
    """Returns real-time health status for X.com, Gemini, and Telegram."""
    check_tw = _resolve_dep("check_twitter_health", default_check_twitter_health)
    check_gem = _resolve_dep("check_gemini_health", default_check_gemini_health)
    check_tg = _resolve_dep("check_telegram_health", default_check_telegram_health)

    service_req = request.query.get("service")
    user = request.get("user") or {}
    is_editor = user.get("role") == "editor"

    if service_req in ("twitter", "x"):
        res = await check_tw()
        if is_editor:
            res = {**res, "details": {}}
        return web.json_response({
            "status": res["status"],
            "services": {"twitter": res},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    elif service_req == "gemini":
        res = await check_gem()
        if is_editor:
            res = {**res, "details": {}}
        return web.json_response({
            "status": res["status"],
            "services": {"gemini": res},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    elif service_req == "telegram":
        res = await check_tg()
        if is_editor:
            res = {**res, "details": {}}
        return web.json_response({
            "status": res["status"],
            "services": {"telegram": res},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    results = await asyncio.gather(
        check_tw(),
        check_gem(),
        check_tg(),
        return_exceptions=True,
    )

    twitter_res = results[0] if not isinstance(results[0], Exception) else {
        "name": "X.com (Twitter)", "status": "error", "message": f"Сбой проверки: {results[0]}", "details": {}
    }
    gemini_res = results[1] if not isinstance(results[1], Exception) else {
        "name": "Google Gemini", "status": "error", "message": f"Сбой проверки: {results[1]}", "details": {}
    }
    telegram_res = results[2] if not isinstance(results[2], Exception) else {
        "name": "Telegram", "status": "error", "message": f"Сбой проверки: {results[2]}", "details": {}
    }

    if is_editor:
        twitter_res["details"] = {}
        gemini_res["details"] = {}
        telegram_res["details"] = {}

    services = {
        "twitter": twitter_res,
        "gemini": gemini_res,
        "telegram": telegram_res,
    }
    overall = _compute_overall_status(services)

    return web.json_response({
        "status": overall,
        "services": services,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


async def handle_get_system_history(request: web.Request) -> web.Response:
    """Returns recent pipeline runs (default 10)."""
    await init_db()
    try:
        limit = int(request.query.get("limit", 10))
    except (ValueError, TypeError):
        limit = 10
    limit = max(1, min(limit, 100))
    runs = await get_recent_pipeline_runs(limit=limit)
    return web.json_response(runs)


async def handle_get_filtered_tweets(request: web.Request) -> web.Response:
    """Возвращает твиты, отфильтрованные пайплайном (is_viral=0 или IGNORED)."""
    await init_db()
    try:
        limit = int(request.query.get("limit", 50))
    except (ValueError, TypeError):
        limit = 50
    limit = max(1, min(limit, 500))
    start_date = request.query.get("start_date") or None
    end_date = request.query.get("end_date") or None
    tweets = await get_filtered_tweets(limit=limit, start_date=start_date, end_date=end_date)
    return web.json_response(tweets)


async def _background_export_task(job_id: str, start_date: str, end_date: str) -> None:
    import zipfile
    try:
        import aiofiles
    except ImportError:
        aiofiles = None
    file_path = f"data/export_{job_id}.zip"
    os.makedirs("data", exist_ok=True)
    jsonl_path = f"data/export_{job_id}.jsonl"

    try:
        if aiofiles:
            async with aiofiles.open(jsonl_path, 'w', encoding='utf-8') as f:
                async for row in export_tweets_data_stream(start_date, end_date):
                    await f.write(json.dumps(row, ensure_ascii=False) + "\n")
        else:
            with open(jsonl_path, 'w', encoding='utf-8') as f:
                async for row in export_tweets_data_stream(start_date, end_date):
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")

        def create_zip():
            with zipfile.ZipFile(file_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.write(jsonl_path, arcname=f"export_{start_date[:10]}_to_{end_date[:10]}.jsonl")
            if os.path.exists(jsonl_path):
                os.remove(jsonl_path)

        await asyncio.to_thread(create_zip)
        export_jobs[job_id]["status"] = "completed"
        export_jobs[job_id]["file_path"] = file_path
    except Exception as e:
        export_jobs[job_id]["status"] = "error"
        export_jobs[job_id]["error"] = str(e)
        if os.path.exists(jsonl_path):
            try:
                os.remove(jsonl_path)
            except Exception:
                pass


async def handle_system_export_start(request: web.Request) -> web.Response:
    user = request.get("user")
    if not user or user.get("role") != "admin":
        return web.json_response({"error": "Admin required"}, status=403)

    data = await request.json()
    start_date = data.get("start_date", "1970-01-01T00:00:00")
    end_date = data.get("end_date", "2100-01-01T00:00:00")
    import uuid
    job_id = str(uuid.uuid4())
    export_jobs[job_id] = {"status": "processing"}
    asyncio.create_task(_background_export_task(job_id, start_date, end_date))
    return web.json_response({"status": "processing", "job_id": job_id})


async def handle_system_export_status(request: web.Request) -> web.Response:
    user = request.get("user")
    if not user or user.get("role") != "admin":
        return web.json_response({"error": "Admin required"}, status=403)
    job_id = request.query.get("job_id")
    if not job_id or job_id not in export_jobs:
        return web.json_response({"error": "Not found"}, status=404)
    return web.json_response(export_jobs[job_id])


async def handle_system_export_download(request: web.Request) -> web.Response:
    user = request.get("user")
    if not user or user.get("role") != "admin":
        return web.json_response({"error": "Admin required"}, status=403)
    job_id = request.query.get("job_id")
    if not job_id or job_id not in export_jobs:
        return web.json_response({"error": "Not found"}, status=404)
    job = export_jobs[job_id]
    if job["status"] != "completed":
        return web.json_response({"error": "Not ready"}, status=400)
    return web.FileResponse(Path(job["file_path"]), headers={"Content-Disposition": f'attachment; filename="export_{job_id[:8]}.zip"'})


async def handle_get_history(request: web.Request) -> web.Response:
    """Returns historical pipeline runs."""
    await init_db()
    limit = int(request.query.get("limit", 50))
    runs = await get_recent_pipeline_runs(limit=limit)
    return web.json_response(runs)


async def handle_system_history_delete(request: web.Request) -> web.Response:
    """Deletes historical data up to a given date."""
    user = request.get("user")
    if not user or user.get("role") != "admin":
        return web.json_response({"error": "Admin required"}, status=403)

    data = await request.json()
    end_date = data.get("end_date")
    if not end_date:
        return web.json_response({"error": "end_date is required"}, status=400)

    deleted_count = await delete_historical_data(end_date)
    return web.json_response({"status": "success", "deleted": deleted_count})


async def handle_system_reset_content(request: web.Request) -> web.Response:
    """Cleans all content data (tweets, digests, video cache) while preserving settings, topics, users, schedules."""
    user = request.get("user")
    if not user or user.get("role") != "admin":
        return web.json_response({"error": "Admin required"}, status=403)

    from src.utils.video_manager import clear_all_cached_videos

    db_counts = await clear_all_content_data()
    files_deleted = clear_all_cached_videos()

    return web.json_response({
        "status": "success",
        "cleared_tables": db_counts,
        "cached_videos_deleted": files_deleted,
        "message": "All content and video caches cleared. System settings and topics preserved."
    })


def register_system_routes(app: web.Application) -> None:
    app.router.add_get("/", handle_index)
    app.router.add_get("/favicon.ico", handle_favicon)
    app.router.add_get("/api/version", handle_get_version)
    app.router.add_get("/api/status", handle_get_status)
    app.router.add_get("/api/health", handle_get_system_health)
    app.router.add_get("/api/system/health", handle_get_system_health)
    app.router.add_get("/api/system/history", handle_get_system_history)
    app.router.add_get("/api/history", handle_get_history)
    app.router.add_get("/api/system/filtered_tweets", handle_get_filtered_tweets)
    app.router.add_post("/api/system/export", handle_system_export_start)
    app.router.add_get("/api/system/export/status", handle_system_export_status)
    app.router.add_get("/api/system/export/download", handle_system_export_download)
    app.router.add_delete("/api/system/history", handle_system_history_delete)
    app.router.add_post("/api/system/reset-content", handle_system_reset_content)
