#!/usr/bin/env python3
"""
Скрипт пакетной перегенерации неопубликованных постов в очереди.
Применяет обновленный системный промпт (и опциональные указания редактора)
ко всем роликам со статусами PROCESSED, SCHEDULED, PENDING_APPROVAL.

Использование:
    python scripts/reprocess_queue.py
    python scripts/reprocess_queue.py --instruction "Без штампов про карму, остроумный тон"
    python scripts/reprocess_queue.py --limit 5
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from src.config import settings
from src.db import (
    init_db,
    load_system_settings_into_config,
    get_queued_posts,
    get_active_topics,
    update_tweet_status_and_score,
)
from src.ai.agents.editor import call_llm
from src.utils.technical_audit import audit_and_prepare_telegram_content


async def reprocess_queue(
    instruction: str = "",
    limit: int = 0,
    delay_seconds: float = 2.0,
    dry_run: bool = False,
) -> None:
    print("🚀 Инициализация базы данных и настроек...")
    await init_db()
    await load_system_settings_into_config()

    posts = await get_queued_posts()
    if not posts:
        print("ℹ️ В очереди нет неопубликованных постов (статусы PROCESSED, SCHEDULED, PENDING_APPROVAL).")
        return

    total = len(posts)
    if limit > 0:
        posts = posts[:limit]
        print(f"📦 Найдено {total} постов в очереди. Будет обработано первых {len(posts)} по лимиту.")
    else:
        print(f"📦 Найдено {total} неопубликованных постов для перегенерации.")

    active_topics = await get_active_topics()
    topics_map = {t["id"]: t for t in active_topics}

    success_count = 0
    fail_count = 0

    print("=" * 60)
    for idx, post in enumerate(posts, 1):
        tweet_id = str(post.get("tweet_id"))
        current_summary = post.get("summary_data") or {}
        if not current_summary and post.get("summary_json"):
            try:
                current_summary = json.loads(post["summary_json"])
            except Exception:
                current_summary = {}

        old_title = current_summary.get("title_ru") or post.get("title") or "—"
        print(f"\n[{idx}/{len(posts)}] Обработка поста {tweet_id}...")
        print(f"  ❌ Было:  «{old_title}»")

        # Media context
        media_urls = post.get("media_urls", [])
        video_data = post.get("video_data")
        if video_data and isinstance(video_data, dict) and video_data.get("thumbnail"):
            media_urls = [video_data["thumbnail"]]

        topic = topics_map.get(post.get("topic_id"))

        if dry_run:
            print("  [DRY-RUN] Пропуск обращения к ИИ.")
            continue

        try:
            ai_data = await call_llm(
                text=post.get("text_raw", ""),
                media_urls=media_urls,
                topic=topic,
                editor_instruction=instruction if instruction else None,
                tweet=post,
            )

            if not ai_data:
                print(f"  ⚠️ ИИ вернул пустой ответ (исчерпан лимит квот 429). Пост пропущен.")
                fail_count += 1
                await asyncio.sleep(delay_seconds)
                continue

            new_title = ai_data.get("title_ru", "—")
            print(f"  ✅ Стало: «{new_title}»")

            # Technical audit check
            is_media = bool(media_urls or video_data)
            audit = audit_and_prepare_telegram_content(
                title=ai_data.get("title_ru", ""),
                summary=ai_data.get("summary_ru", ""),
                takeaways=ai_data.get("takeaways", []),
                hashtags=ai_data.get("hashtags", []),
                is_media=is_media,
                author_handle=post.get("author_handle"),
                tweet_url=post.get("tweet_url"),
            )
            ai_data["title_ru"] = audit["title_ru"]
            ai_data["summary_ru"] = audit["summary_ru"]
            ai_data["takeaways"] = audit["takeaways"]
            ai_data["hashtags"] = audit["hashtags"]
            ai_data["technical_audit"] = {
                "is_valid": audit["is_valid"],
                "char_count": audit["char_count"],
                "max_limit": audit["max_limit"],
                "warnings": audit["warnings"],
            }

            # Update in DB
            new_status = post.get("status", "PROCESSED")
            if getattr(settings, "approval_required", False):
                new_status = "PENDING_APPROVAL"

            await update_tweet_status_and_score(
                tweet_id=tweet_id,
                score=post.get("viral_score", 0.0),
                is_viral=True,
                status=new_status,
                impact_score=ai_data.get("impact_score", 7),
                summary_json=json.dumps(ai_data, ensure_ascii=False),
            )
            success_count += 1

        except Exception as e:
            print(f"  ❌ Ошибка обработки поста {tweet_id}: {e}")
            fail_count += 1

        # Pause to prevent rate limit (RPM)
        if idx < len(posts):
            await asyncio.sleep(delay_seconds)

    print("\n" + "=" * 60)
    print(f"🎉 Готово! Успешно перегенерировано: {success_count}, Ошибок/пропусков: {fail_count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Пакетная перегенерация неопубликованных постов под новый промпт.")
    parser.add_argument(
        "--instruction",
        "-i",
        type=str,
        default="",
        help="Дополнительное указание шеф-редактора (например: 'без штампов про карму, с тонкой иронией')",
    )
    parser.add_argument(
        "--limit",
        "-l",
        type=int,
        default=0,
        help="Ограничить количество обрабатываемых постов (0 = все посты в очереди)",
    )
    parser.add_argument(
        "--delay",
        "-d",
        type=float,
        default=2.0,
        help="Пауза между запросами в секундах для соблюдения квот Gemini (по умолчанию 2.0с)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Только показать посты в очереди без обращения к ИИ и изменения базы",
    )
    args = parser.parse_args()

    asyncio.run(
        reprocess_queue(
            instruction=args.instruction,
            limit=args.limit,
            delay_seconds=args.delay,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()
