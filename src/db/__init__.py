"""
Database package for Universal Viral Hunter.
Provides modular submodules for schema, connection, tweets, posts, users, settings,
digests, and maintenance, while exposing all public interfaces at package level
for 100% backward compatibility.
"""

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
from src.db.settings import (
    get_active_topics,
    get_topics,
    create_topic,
    update_topic,
    delete_topic,
    is_secret_placeholder_or_empty,
    save_system_settings_dual,
    load_system_settings_into_config,
)
from src.db.digests import (
    get_digest_schedules,
    save_digest_schedule,
    delete_digest_schedule,
    mark_digest_schedule_run,
    get_digest_schedules_due,
    mark_digest_posted,
    is_digest_posted,
    get_staging_tweets,
    create_digest_draft,
    publish_digest,
    get_digests_list,
    get_due_scheduled_digests,
)
from src.db.maintenance import (
    record_pipeline_run,
    get_recent_pipeline_runs,
    export_tweets_data_stream,
    delete_historical_data,
    cleanup_expired_posts,
    clear_all_content_data,
)

__all__ = [
    # connection
    "get_db_connection",
    "get_db_path",
    "_deserialize_row",
    "normalize_to_utc_iso",
    # schema
    "init_db",
    # tweets
    "find_duplicate_video_in_db",
    "filter_new_tweets",
    "save_raw_tweets",
    "update_tweet_status_and_score",
    "update_tweet_metrics",
    "get_tweets_last_24h",
    "get_recent_active_tweets",
    "get_tweet_by_id",
    "set_tweet_status",
    "get_filtered_tweets",
    # posts
    "get_queued_posts",
    "get_unposted_tweets",
    "get_published_posts",
    "update_post_schedule",
    "save_tweet_admin_message_id",
    "get_tweet_admin_message_id",
    "auto_schedule_queue",
    "reorder_queue",
    "update_post_content",
    "cancel_scheduled_post",
    "mark_post_published",
    # users
    "create_user",
    "authenticate_user",
    "get_user_by_username",
    "get_all_users",
    "update_user_password",
    "delete_user",
    "get_user_count",
    # settings & topics
    "get_active_topics",
    "get_topics",
    "create_topic",
    "update_topic",
    "delete_topic",
    "is_secret_placeholder_or_empty",
    "save_system_settings_dual",
    "load_system_settings_into_config",
    # digests
    "get_digest_schedules",
    "save_digest_schedule",
    "delete_digest_schedule",
    "mark_digest_schedule_run",
    "get_digest_schedules_due",
    "mark_digest_posted",
    "is_digest_posted",
    "get_staging_tweets",
    "create_digest_draft",
    "publish_digest",
    "get_digests_list",
    "get_due_scheduled_digests",
    # maintenance
    "record_pipeline_run",
    "get_recent_pipeline_runs",
    "export_tweets_data_stream",
    "delete_historical_data",
    "cleanup_expired_posts",
    "clear_all_content_data",
]