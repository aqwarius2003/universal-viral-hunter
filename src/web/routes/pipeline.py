import asyncio
from datetime import datetime, timezone
import sys
from typing import Any
from aiohttp import web
from src.config import settings
from src.db import init_db, get_tweet_by_id, get_queued_posts
from src.digest import run_daily_digest
from src.web.state import pipeline_lock, pipeline_runtime
from src.web.helpers import _safe_run_pipeline, _generate_summary_for_tweet


def _resolve_dep(name: str, default: Any) -> Any:
    server = sys.modules.get("src.web.server")
    if server and hasattr(server, name):
        return getattr(server, name)
    return default



async def handle_run_pipeline(request: web.Request) -> web.Response:
    """Triggers an on-demand background pipeline run."""
    if pipeline_lock.locked() or pipeline_runtime["state"] == "running":
        return web.json_response(
            {"status": "pipeline_running", "message": "Pipeline уже выполняется."},
            status=409,
        )

    pipeline_runtime["state"] = "running"
    pipeline_runtime["started_at"] = datetime.now(timezone.utc).isoformat()
    pipeline_runtime["finished_at"] = None
    pipeline_runtime["error_message"] = None

    async def run_locked_pipeline() -> None:
        async with pipeline_lock:
            await _safe_run_pipeline()

    asyncio.create_task(run_locked_pipeline())
    return web.json_response({"status": "pipeline_started", "message": "Сбор и обработка запущены в фоне."})


async def handle_pipeline_status(request: web.Request) -> web.Response:
    """Returns the current runtime state of the manually triggered pipeline."""
    return web.json_response(pipeline_runtime)


async def handle_run_digest(request: web.Request) -> web.Response:
    """Triggers an on-demand daily digest run."""
    manual_slot = f"manual_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}"
    asyncio.create_task(run_daily_digest(slot_id=manual_slot, is_test=False))
    return web.json_response({"status": "digest_started", "message": "Генерация дайджеста запущена в фоне."})


async def handle_generate_summary(request: web.Request) -> web.Response:
    """Manually triggers Russian summary generation with Gemini for a specific post."""
    tweet_id = request.match_info["id"]
    await init_db()
    tweet = await get_tweet_by_id(tweet_id)
    if not tweet:
        return web.json_response({"error": "Post not found"}, status=404)

    instruction = None
    if request.can_read_body:
        try:
            data = await request.json()
            if isinstance(data, dict):
                instruction = data.get("instruction")
        except Exception:
            pass

    _gen_sum = _resolve_dep("_generate_summary_for_tweet", _generate_summary_for_tweet)
    summary_data = await _gen_sum(tweet, force=True, instruction=instruction)
    if summary_data:
        return web.json_response({"status": "success", "summary_data": summary_data, "impact_score": tweet.get("impact_score", 7)})
    else:
        return web.json_response({"error": "Failed to generate summary with Gemini (quota or network error). Please try again in a few moments."}, status=500)


async def handle_batch_reprocess_queue(request: web.Request) -> web.Response:
    """Batch re-generates summaries and titles for all queued/unpublished posts."""
    await init_db()
    try:
        data = await request.json()
    except Exception:
        data = {}

    instruction = data.get("instruction", "").strip() if isinstance(data, dict) else ""
    posts = await get_queued_posts()
    if not posts:
        return web.json_response({"status": "success", "total": 0, "reprocessed": 0, "errors": 0, "message": "Очередь пуста"})

    reprocessed = 0
    errors = 0
    delay = float(getattr(settings, "ai_request_delay_seconds", 1.5))
    _gen_sum = _resolve_dep("_generate_summary_for_tweet", _generate_summary_for_tweet)

    for p in posts:
        try:
            res = await _gen_sum(p, force=True, instruction=instruction)
            if res:
                reprocessed += 1
            else:
                errors += 1
            await asyncio.sleep(delay)
        except Exception as e:
            errors += 1
            print(f"Error in batch reprocessing post {p.get('tweet_id')}: {e}")

    return web.json_response({
        "status": "success",
        "total": len(posts),
        "reprocessed": reprocessed,
        "errors": errors,
    })


def register_pipeline_routes(app: web.Application) -> None:
    app.router.add_post("/api/pipeline/run", handle_run_pipeline)
    app.router.add_post("/api/scout/trigger", handle_run_pipeline)
    app.router.add_post("/api/scout/run", handle_run_pipeline)
    app.router.add_get("/api/pipeline/status", handle_pipeline_status)
    app.router.add_post("/api/digest/run", handle_run_digest)
    app.router.add_post("/api/posts/{id}/generate_summary", handle_generate_summary)
    app.router.add_post("/api/queue/batch-reprocess", handle_batch_reprocess_queue)
