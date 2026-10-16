import argparse
import asyncio
from datetime import datetime, timedelta
import sys
from typing import Optional

# Setup Windows console encoding
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from src.config import settings, is_in_quiet_hours, get_app_timezone
from src.db import is_digest_posted, init_db, load_system_settings_into_config, cleanup_expired_posts, get_digest_schedules, get_digest_schedules_due, mark_digest_schedule_run
from src.digest import run_daily_digest, run_morning_digest_parts, run_evening_digest_parts, run_digest_schedule_rule
from src.ai.agents.publisher import publish_single_top_post
from src.main import run_pipeline
from src.web.server import start_web_server


def log_event(message: str) -> None:
    now_str = datetime.now(get_app_timezone()).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] {message}")


def normalize_schedule_times(times_str: Optional[str]) -> list[str]:
    """Parses and normalizes comma-separated schedule times to HH:MM format."""
    if not times_str:
        return []
    result = []
    for item in times_str.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            parts = item.split(":")
            if len(parts) == 2:
                h = int(parts[0])
                m = int(parts[1])
                if 0 <= h < 24 and 0 <= m < 60:
                    result.append(f"{h:02d}:{m:02d}")
        except ValueError:
            continue
    return result


def get_evening_digest_time_str() -> str:
    """Calculates the evening digest start time (quiet_hours_start minus digest_pre_sleep_minutes)."""
    start_str = getattr(settings, "quiet_hours_start", None)
    pre_minutes = getattr(settings, "digest_pre_sleep_minutes", None)
    if not start_str or not pre_minutes:
        return ""
    try:
        sh, sm = map(int, start_str.strip().split(":"))
        dt = datetime.now(get_app_timezone()).replace(hour=sh, minute=sm, second=0, microsecond=0) - timedelta(minutes=pre_minutes)
        return dt.strftime("%H:%M")
    except Exception:
        return ""


async def check_and_publish_scheduled_digests() -> int:
    """Checks for scheduled digests in database that are due for publication and posts them."""
    from src.db import get_due_scheduled_digests, publish_digest
    from src.telegram.sender import send_post_to_channel

    due_digests = await get_due_scheduled_digests()
    published_count = 0
    for d in due_digests:
        did = d["id"]
        text = d.get("formatted_text") or ""
        msg_id = None
        if settings.is_configured() and getattr(settings, "telegram_bot_token", None):
            try:
                msg_id = await send_post_to_channel(text)
            except Exception as e:
                log_event(f"⚠️ Ошибка публикации запланированного дайджеста #{did}: {e}")
        await publish_digest(did, telegram_message_id=msg_id)
        log_event(f"✅ Запланированный дайджест #{did} успешно опубликован.")
        published_count += 1
    return published_count


async def check_daily_digest_loop() -> None:
    """Background periodic checker to trigger scheduled digests, Morning ('Пока вы спали') and Evening Digests."""
    executed_slots_today: set = set()
    now = datetime.now(get_app_timezone())
    current_tracked_day = now.strftime("%Y-%m-%d")

    while True:
        try:
            # 0. Check and publish any due scheduled staging digests
            await check_and_publish_scheduled_digests()

            if not settings.is_configured():
                await asyncio.sleep(30)
                continue

            now = datetime.now(get_app_timezone())
            today_str = now.strftime("%Y-%m-%d")
            current_time = now.strftime("%H:%M")
            configured_rules = []

            if today_str != current_tracked_day:
                executed_slots_today.clear()
                current_tracked_day = today_str

            if settings.is_configured():
                configured_rules = await get_digest_schedules()
                due_rules = await get_digest_schedules_due(current_time, today_str)
                for rule in due_rules:
                    await mark_digest_schedule_run(rule["id"], today_str)
                    log_event(f"📰 Запуск дайджеста по правилу #{rule['id']}: {rule['title']}")
                    try:
                        await run_digest_schedule_rule(rule)
                        log_event(f"✅ Дайджест по правилу #{rule['id']} завершен.")
                    except Exception as err:
                        log_event(f"⚠️ Ошибка дайджеста по правилу #{rule['id']}: {err}")

            if not configured_rules and settings.daily_digest_enabled and settings.quiet_hours_end and settings.quiet_hours_start and getattr(settings, "backlog_digest_enabled", True):
                # 1. Morning Digest at quiet_hours_end (default 07:00)
                morning_time = settings.quiet_hours_end
                morning_key = f"{today_str}_morning_{morning_time}"
                if current_time >= morning_time and current_time < "12:00" and morning_key not in executed_slots_today:
                    log_event(f"🌅 Время утреннего дайджеста ({morning_time}) наступило! Запуск сборки 'Пока вы спали'...")
                    try:
                        await run_morning_digest_parts()
                        log_event("✅ Утренний дайджест 'Пока вы спали' завершен.")
                    except Exception as err:
                        log_event(f"⚠️ Ошибка утреннего дайджеста: {err}")
                    executed_slots_today.add(morning_key)

                # 2. Evening Digest at quiet_hours_start minus digest_pre_sleep_minutes (default 22:30)
                evening_time = get_evening_digest_time_str()
                evening_key = f"{today_str}_evening_{evening_time}"
                if current_time >= evening_time and current_time < settings.quiet_hours_start and evening_key not in executed_slots_today:
                    log_event(f"🌙 Время вечернего дайджеста ({evening_time}) наступило! Очистка стека перед сном...")
                    try:
                        await run_evening_digest_parts()
                        log_event("✅ Вечерний дайджест очистки стека завершен.")
                    except Exception as err:
                        log_event(f"⚠️ Ошибка вечернего дайджеста: {err}")
                    executed_slots_today.add(evening_key)

            await asyncio.sleep(30)
        except asyncio.CancelledError:
            break
        except Exception as err:
            log_event(f"⚠️ Ошибка в фоновом таймере дайджеста: {err}")
            await asyncio.sleep(60)


async def daytime_publisher_loop() -> None:
    """Background loop to publish 1 top post every daytime_publish_interval_minutes during daytime."""
    while True:
        try:
            interval = getattr(settings, "daytime_publish_interval_minutes", None)
            if not interval:
                await asyncio.sleep(30)
                continue
            await asyncio.sleep(interval * 60)

            if not settings.is_configured():
                continue

            if settings.quiet_hours_enabled and settings.quiet_hours_start and settings.quiet_hours_end and is_in_quiet_hours():
                continue

            # Don't publish single posts during evening digest window (e.g. 22:30 - 23:00)
            evening_time = get_evening_digest_time_str()
            current_time = datetime.now(get_app_timezone()).strftime("%H:%M")
            if evening_time and settings.quiet_hours_start and current_time >= evening_time and current_time < settings.quiet_hours_start:
                continue

            # Pick and publish #1 top post from queue
            res = await publish_single_top_post()
            if res:
                log_event(f"⚡ Опубликован дневной пост (Топ-1 по виральности): ID {res['tweet_id']}")
            else:
                log_event("ℹ️ Очередь постов пуста. Ожидание следующего сбора Scout...")
        except asyncio.CancelledError:
            break
        except Exception as err:
            log_event(f"⚠️ Ошибка в дневном цикле публикаций: {err}")


async def system_maintenance_loop() -> None:
    """Background loop to check resource thresholds and perform TTL cleanups."""
    import shutil
    while True:
        try:
            # Check every hour
            await asyncio.sleep(3600)
            
            # 1. TTL Cleanup (e.g. 30 days)
            ttl_days = getattr(settings, "ttl_cleanup_days", 30)
            deleted = await cleanup_expired_posts(ttl_days)
            if deleted > 0:
                log_event(f"🧹 Автоочистка (TTL {ttl_days} дн.): удалено {deleted} старых постов.")
            
            # 2. Resource Monitoring
            emergency = False
            # Check Disk
            import os
            db_dir = os.path.dirname(settings.db_path) or '.'
            total, used, free = shutil.disk_usage(db_dir)
            free_mb = free / (1024 * 1024)
            if free_mb < getattr(settings, "min_free_disk_mb", 500):
                log_event(f"🚨 КРИТИЧЕСКИ МАЛО МЕСТА НА ДИСКЕ: {free_mb:.1f} MB. Запуск экстренной очистки!")
                emergency = True
                
            # Check RAM (Linux /proc/meminfo)
            try:
                with open('/proc/meminfo', 'r') as f:
                    for line in f:
                        if line.startswith('MemAvailable:'):
                            free_ram_mb = int(line.split()[1]) / 1024
                            if free_ram_mb < getattr(settings, "min_free_ram_mb", 150):
                                log_event(f"🚨 КРИТИЧЕСКИ МАЛО RAM: {free_ram_mb:.1f} MB. Запуск экстренной очистки!")
                                emergency = True
                            break
            except Exception:
                pass
                
            if emergency:
                # Emergency aggressive cleanup (14 days)
                deleted_em = await cleanup_expired_posts(14)
                log_event(f"🧹 Экстренная очистка: удалено {deleted_em} постов.")
                
        except asyncio.CancelledError:
            break
        except Exception as err:
            log_event(f"⚠️ Ошибка в цикле обслуживания: {err}")


async def run_scheduler(interval_minutes: int, run_immediately: bool = True, enable_dashboard: bool = True) -> None:
    """Runs the Viral Hunter pipeline periodically every interval_minutes minutes."""
    log_event("🚀 Viral Hunter Scheduler запущен!")
    if interval_minutes:
        log_event(f"⏱️ Интервал проверки Scout: {interval_minutes} мин. ({interval_minutes / 60:.1f} ч.)")
    if settings.daytime_publish_interval_minutes:
        log_event(f"⏱️ Интервал дневных публикаций: {settings.daytime_publish_interval_minutes} мин.")
    if settings.daily_digest_enabled and settings.quiet_hours_end:
        log_event(f"📰 Утренний дайджест: {settings.quiet_hours_end} | Вечерний дайджест: {get_evening_digest_time_str()}")
    if settings.quiet_hours_enabled and settings.quiet_hours_start and settings.quiet_hours_end:
        log_event(f"🌙 Режим сна (тихие часы) включен: с {settings.quiet_hours_start} до {settings.quiet_hours_end}")

    await init_db()
    await load_system_settings_into_config()

    if enable_dashboard:
        try:
            await start_web_server(host="0.0.0.0", port=8000)
        except Exception as web_err:
            log_event(f"⚠️ Не удалось запустить веб-дашборд на порту 8000: {web_err}")

    log_event("Нажмите Ctrl+C для остановки.")
    print("-" * 60)

    is_first_run = True
    digest_task = asyncio.create_task(check_daily_digest_loop())
    publisher_task = asyncio.create_task(daytime_publisher_loop())
    maintenance_task = asyncio.create_task(system_maintenance_loop())

    try:
        while True:
            try:
                if not settings.is_configured():
                    log_event("⏳ Система ожидает первичной настройки API-ключей в веб-дашборде (http://localhost:8000)...")
                    await asyncio.sleep(20)
                    continue

                if interval_minutes and (not is_first_run or run_immediately):
                    if is_in_quiet_hours():
                        log_event(f"🌙 Сейчас период тихих часов ({settings.quiet_hours_start} - {settings.quiet_hours_end}). Посты будут сохранены в очередь без публикации.")
                    log_event("▶️ Запуск итерации сбора и обработки постов (Scout)...")
                    start_time = datetime.now(get_app_timezone())

                    try:
                        try:
                            from src.web.server import update_pipeline_runtime
                            update_pipeline_runtime("running")
                        except Exception:
                            pass
                        res = await run_pipeline()
                        elapsed = (datetime.now(get_app_timezone()) - start_time).total_seconds()
                        log_event(f"✅ Итерация успешно завершена за {elapsed:.1f} сек.")
                        try:
                            from src.web.server import update_pipeline_runtime
                            update_pipeline_runtime(
                                state="success",
                                tweets_found=len(res.get("raw_tweets", [])) if isinstance(res, dict) else 0,
                                viral_found=len(res.get("viral_tweets", [])) if isinstance(res, dict) else 0,
                            )
                        except Exception:
                            pass
                    except Exception as pipeline_err:
                        log_event(f"⚠️ Ошибка выполнения пайплайна: {pipeline_err}")
                        try:
                            from src.web.server import update_pipeline_runtime
                            update_pipeline_runtime(state="error", error_message=str(pipeline_err))
                        except Exception:
                            pass

                is_first_run = False

                if interval_minutes:
                    next_run = datetime.now(get_app_timezone()) + timedelta(minutes=interval_minutes)
                    log_event(f"💤 Ожидание следующего сбора Scout в {next_run.strftime('%H:%M:%S')} (через {interval_minutes} мин.)...\n")
                    await asyncio.sleep(interval_minutes * 60)
                else:
                    await asyncio.sleep(30)

            except asyncio.CancelledError:
                log_event("🛑 Получен сигнал остановки. Завершение работы планировщика...")
                break
            except KeyboardInterrupt:
                log_event("🛑 Остановка по Ctrl+C...")
                break
            except Exception as loop_err:
                log_event(f"❌ Непредвиденная ошибка в цикле планировщика: {loop_err}")
                log_event("Повторная попытка через 60 секунд...")
                await asyncio.sleep(60)
    finally:
        digest_task.cancel()
        publisher_task.cancel()
        maintenance_task.cancel()
        try:
            await digest_task
        except asyncio.CancelledError:
            pass
        try:
            await publisher_task
        except asyncio.CancelledError:
            pass
        try:
            await maintenance_task
        except asyncio.CancelledError:
            pass



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Периодический планировщик для Viral Hunter."
    )
    parser.add_argument(
        "--minutes",
        "-m",
        type=int,
        default=None,
        help=f"Интервал в минутах (по умолчанию: {settings.scheduler_interval_minutes} мин.)"
    )
    parser.add_argument(
        "--hours",
        "-H",
        type=float,
        default=None,
        help="Интервал в часах (например: 2 или 1.5)"
    )
    parser.add_argument(
        "--skip-initial",
        action="store_true",
        help="Не запускать пайплайн сразу при старте, а подождать первый интервал"
    )
    parser.add_argument(
        "--digest-now",
        action="store_true",
        help="Немедленно запустить генерацию вечернего дайджеста"
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Отключить запуск веб-дашборда"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.digest_now:
        manual_slot = f"manual_{datetime.now(get_app_timezone()).strftime('%Y-%m-%d_%H%M%S')}"
        asyncio.run(run_daily_digest(slot_id=manual_slot, is_test=False))
        return

    if args.hours is not None:
        interval_minutes = int(args.hours * 60)
    elif args.minutes is not None:
        interval_minutes = args.minutes
    else:
        interval_minutes = settings.scheduler_interval_minutes

    if interval_minutes is not None and interval_minutes <= 0:
        print("❌ Ошибка: интервал должен быть больше 0 минут.")
        sys.exit(1)

    try:
        asyncio.run(
            run_scheduler(
                interval_minutes=interval_minutes,
                run_immediately=not args.skip_initial,
                enable_dashboard=not args.no_dashboard
            )
        )
    except (KeyboardInterrupt, SystemExit):
        print("\nПланировщик остановлен.")


if __name__ == "__main__":
    main()
