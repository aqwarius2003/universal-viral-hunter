# Спецификация: Архитектурный рефакторинг монолитов (src/db и src/web)

## 1. Введение и цели
Цель данного рефакторинга — разбить два главных монолитных файла проекта:
- `src/db.py` (~1720 строк)
- `src/web/server.py` (~2309 строк)

на компактные, логически изолированные модули, удобные для чтения, тестирования и точечного редактирования AI-агентами, сохранив при этом **100% обратную совместимость** (все 179 тестов проекта должны выполняться без изменения внешних импортов).

## 2. Архитектура src/db/

Вместо монолитного `src/db.py` создается пакет `src/db/`:
- `src/db/connection.py`:
  - `DB_PATH`, `DATABASE_URL`
  - Функция подключения к БД `get_db_connection()`
  - Вспомогательные функции сериализации/десериализации (`_deserialize_row`, `normalize_to_utc_iso`)
- `src/db/schema.py`:
  - `init_db()`
  - Создание DDL таблиц (`tweets`, `topics`, `users`, `system_settings`, `pipeline_runs`, `digest_schedules`, `digest_drafts`)
  - Идемпотентные миграции колонок (`PRAGMA table_info`, `ALTER TABLE ... ADD COLUMN`)
- `src/db/tweets.py`:
  - `save_raw_tweets()`, `get_tweet_by_id()`, `filter_new_tweets()`
  - `update_tweet_status_and_score()`, `update_tweet_metrics()`, `set_tweet_status()`
  - `find_duplicate_video_in_db()`
  - `get_tweets_last_24h()`, `get_recent_active_tweets()`, `get_filtered_tweets()`
- `src/db/posts.py`:
  - `get_queued_posts()`, `get_published_posts()`, `get_unposted_tweets()`
  - `auto_schedule_queue()`, `reorder_queue()`, `update_post_schedule()`, `cancel_scheduled_post()`, `mark_post_published()`
  - `update_post_content()`
  - `save_tweet_admin_message_id()`, `get_tweet_admin_message_id()`
- `src/db/users.py`:
  - `create_user()`, `authenticate_user()`, `get_user_by_username()`, `get_all_users()`, `update_user_password()`, `delete_user()`, `get_user_count()`
- `src/db/settings.py`:
  - `save_system_settings_dual()`, `load_system_settings_into_config()`, `is_secret_placeholder_or_empty()`
  - Управление топиками: `get_topics()`, `get_active_topics()`, `create_topic()`, `update_topic()`, `delete_topic()`
- `src/db/digests.py`:
  - `get_digest_schedules()`, `save_digest_schedule()`, `delete_digest_schedule()`, `mark_digest_schedule_run()`, `get_digest_schedules_due()`, `get_due_scheduled_digests()`
  - `create_digest_draft()`, `publish_digest()`, `get_digests_list()`, `get_staging_tweets()`
  - `mark_digest_posted()`, `is_digest_posted()`
- `src/db/maintenance.py`:
  - `record_pipeline_run()`, `get_recent_pipeline_runs()`
  - `export_tweets_data_stream()`, `delete_historical_data()`, `cleanup_expired_posts()`, `clear_all_content_data()`
- `src/db/__init__.py`:
  - Единая точка входа (фасад), реэкспортирующая все публичные символы, что сохраняет `from src.db import ...` полностью работоспособным.

## 3. Архитектура src/web/

Внутри `src/web/` монолит `server.py` декомпозируется на подсистемы:
- `src/web/state.py`:
  - Глобальное состояние веб-сервера (`pipeline_lock`, `pipeline_runtime`, `login_limiter`, `STATIC_DIR`, `get_client_ip`)
- `src/web/middlewares.py`:
  - `auth_middleware` (валидация JWT, защита защищенных маршрутов, проверка RBAC)
- `src/web/helpers.py`:
  - `_generate_summary_for_tweet()`, `_ensure_russian_summaries()`, `_safe_run_pipeline()`
- `src/web/health.py`:
  - Функции диагностики внешних сервисов: `check_twitter_health()`, `check_gemini_health()`, `check_telegram_health()`
- `src/web/routes/`:
  - `auth.py`: роуты авторизации, настройки первого запуска, управление пользователями
  - `posts.py`: роуты очереди постов, расписания, мгновенной публикации, редактирования и повторной обработки
  - `settings.py`: настройки системы, резервное копирование, расписания дайджестов, топики
  - `pipeline.py`: запуск пайплайна, статус, генерация саммари, пакетная обработка очереди
  - `digests.py`: работа со стейджингом дайджестов, предпросмотр, публикация, ручные дайджесты
  - `twitter.py`: авторизация Twitter через веб, сохранение кук, скриншот отладки
  - `system.py`: статика/главная, статус системы, экспорт данных, очистка истории, сброс контента, healthcheck API
- `src/web/app.py`:
  - Фабрика приложения `create_app()`, хуки запуска `on_app_startup()`, регистрация маршрутов `setup_routes(app)`
  - Запуск веб-сервера `start_web_server()`
- `src/web/server.py`:
  - Фасад для обратной совместимости, реэкспортирующий `create_app`, `start_web_server` и все обработчики `handle_*`, чтобы модули тестов (`test_web_api.py`, `test_monitoring.py`, etc.) работали без изменений.

## 4. Стратегия тестирования и верификации
1. Базовый прогон: все 179 тестов пройдены успешно.
2. Проверка после рефакторинга `src/db`:
   - Запуск `pytest tests/test_db_extensions.py tests/test_reset_content.py tests/test_digest_schedules.py`
   - Полный прогон `pytest`
3. Проверка после рефакторинга `src/web`:
   - Запуск `pytest tests/test_web_api.py tests/test_auth_and_security.py tests/test_monitoring.py tests/test_frontend_static.py`
   - Полный прогон `pytest` (все 179 тестов должны быть зелеными).
4. Проверка запуска сервисов и импортов.
