from pathlib import Path
from aiohttp import web
from src.web.state import STATIC_DIR

from src.web.routes.auth import (
    handle_setup_status,
    handle_setup,
    handle_login,
    handle_logout,
    handle_me,
    handle_get_users,
    handle_create_user,
    handle_update_user_password,
    handle_delete_user,
    register_auth_routes,
)
from src.web.routes.posts import (
    handle_get_queue,
    handle_get_published,
    handle_update_schedule,
    handle_auto_schedule,
    handle_reorder_queue,
    handle_publish_now,
    handle_edit_post,
    handle_reprocess_post,
    handle_delete_post,
    handle_sync_telegram,
    register_posts_routes,
)
from src.web.routes.settings import (
    handle_system_backup,
    handle_get_settings,
    handle_update_settings,
    handle_get_digest_schedules,
    handle_create_digest_schedule,
    handle_update_digest_schedule,
    handle_delete_digest_schedule,
    handle_get_topics,
    handle_create_topic,
    handle_update_topic,
    handle_delete_topic,
    update_env_file,
    register_settings_routes,
)
from src.web.routes.pipeline import (
    handle_run_pipeline,
    handle_pipeline_status,
    handle_run_digest,
    handle_generate_summary,
    handle_batch_reprocess_queue,
    register_pipeline_routes,
)
from src.web.routes.digests import (
    handle_get_staging_tweets,
    handle_staging_digest_preview,
    handle_staging_digest_publish,
    handle_create_manual_digest,
    handle_get_digests,
    register_digests_routes,
)
from src.web.routes.twitter import (
    handle_twitter_login,
    handle_save_twitter_cookies,
    handle_twitter_debug_screenshot,
    register_twitter_routes,
)
from src.web.routes.system import (
    handle_index,
    handle_favicon,
    handle_get_version,
    handle_get_status,
    handle_get_system_health,
    handle_get_system_history,
    handle_get_filtered_tweets,
    handle_system_export_start,
    handle_system_export_status,
    handle_system_export_download,
    handle_get_history,
    handle_system_history_delete,
    handle_system_reset_content,
    export_jobs,
    register_system_routes,
)


def register_routes(app: web.Application) -> None:
    """Registers all API, system, digest, twitter, post, settings and static routes to the aiohttp app."""
    register_system_routes(app)
    register_auth_routes(app)
    register_posts_routes(app)
    register_settings_routes(app)
    register_pipeline_routes(app)
    register_digests_routes(app)
    register_twitter_routes(app)

    if STATIC_DIR.exists():
        app.router.add_static("/static", path=STATIC_DIR, name="static")

    video_dir = Path("data/video-temp")
    video_dir.mkdir(parents=True, exist_ok=True)
    app.router.add_static("/videos", path=video_dir, name="videos")


__all__ = [
    "register_routes",
    "register_auth_routes",
    "register_posts_routes",
    "register_settings_routes",
    "register_pipeline_routes",
    "register_digests_routes",
    "register_twitter_routes",
    "register_system_routes",
    "handle_setup_status",
    "handle_setup",
    "handle_login",
    "handle_logout",
    "handle_me",
    "handle_get_users",
    "handle_create_user",
    "handle_update_user_password",
    "handle_delete_user",
    "handle_get_queue",
    "handle_get_published",
    "handle_update_schedule",
    "handle_auto_schedule",
    "handle_reorder_queue",
    "handle_publish_now",
    "handle_edit_post",
    "handle_reprocess_post",
    "handle_delete_post",
    "handle_sync_telegram",
    "handle_system_backup",
    "handle_get_settings",
    "handle_update_settings",
    "handle_get_digest_schedules",
    "handle_create_digest_schedule",
    "handle_update_digest_schedule",
    "handle_delete_digest_schedule",
    "handle_get_topics",
    "handle_create_topic",
    "handle_update_topic",
    "handle_delete_topic",
    "update_env_file",
    "handle_run_pipeline",
    "handle_pipeline_status",
    "handle_run_digest",
    "handle_generate_summary",
    "handle_batch_reprocess_queue",
    "handle_get_staging_tweets",
    "handle_staging_digest_preview",
    "handle_staging_digest_publish",
    "handle_create_manual_digest",
    "handle_get_digests",
    "handle_twitter_login",
    "handle_save_twitter_cookies",
    "handle_twitter_debug_screenshot",
    "handle_index",
    "handle_favicon",
    "handle_get_version",
    "handle_get_status",
    "handle_get_system_health",
    "handle_get_system_history",
    "handle_get_filtered_tweets",
    "handle_system_export_start",
    "handle_system_export_status",
    "handle_system_export_download",
    "handle_get_history",
    "handle_system_history_delete",
    "handle_system_reset_content",
    "export_jobs",
]
