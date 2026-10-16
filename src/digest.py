import argparse
import asyncio
from datetime import datetime, timezone
import html
import json
import sys
from typing import Any, Dict, List, Optional

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

from src.ai.agents.publisher import format_telegram_post
from src.ai.agents.scout import refresh_tweet_metrics, safe_print
from src.ai.schema import DailyDigestSummary, DigestHighlightItem
from src.analytics.scorer import calculate_viral_score
from src.config import settings, get_app_timezone
from src.db import (
    get_tweets_last_24h,
    get_unposted_tweets,
    init_db,
    is_digest_posted,
    mark_digest_posted,
    update_tweet_metrics,
    update_tweet_status_and_score,
)
from src.telegram.sender import send_photo_to_channel, send_post_to_channel
from src.utils.hashtags import normalize_hashtags, enforce_hashtags_in_post

client: Optional[Any] = None


def get_client() -> Any:
    global client
    if client is None:
        if not settings.ai_api_key or settings.ai_api_key.startswith("your_"):
            raise RuntimeError("AI_API_KEY is not configured. Complete the setup in the web dashboard.")
        client = genai.Client(api_key=settings.ai_api_key)
    return client


try:
    from google.genai.errors import ClientError, APIError
except ImportError:
    class ClientError(Exception):
        code: int = 429
    class APIError(Exception):
        pass

from src.ai.rate_limiter import model_rate_limiter, get_model_interval
from src.ai.agents.editor import is_rate_limit_error


def _parse_digest_json(raw_text: str) -> dict:
    raw_text = raw_text.strip()
    if raw_text.startswith("```json"):
        raw_text = raw_text[7:]
    if raw_text.startswith("```"):
        raw_text = raw_text[3:]
    if raw_text.endswith("```"):
        raw_text = raw_text[:-3]
    return json.loads(raw_text.strip())


async def generate_digest_with_gemini(tweets: List[Dict[str, Any]], max_retries: int = 3) -> DailyDigestSummary:
    """Uses Gemini to synthesize top tweets into a cohesive Russian daily digest with source URLs."""
    current_client = get_client()
    system_prompt = (
        "You are the chief editor of an elite tech and AI news channel. "
        "Your task is to analyze the top viral AI tweets of the last 24 hours "
        "and produce a comprehensive, insightful, and structured daily digest in Russian. "
        "For the main story and each news highlight in key_highlights, you MUST provide the exact source URL "
        "from the provided tweets in main_story_url and source_url. "
        "Focus on genuine technical breakthroughs, developer tools, AI models, and industry impact. "
        "Do not use generic buzzwords or clickbait."
    )

    tweets_context = []
    for idx, t in enumerate(tweets[:8], start=1):
        summary = ""
        if t.get("summary_json"):
            try:
                s_dict = json.loads(t["summary_json"]) if isinstance(t["summary_json"], str) else t["summary_json"]
                summary = s_dict.get("summary_ru", "")
            except Exception:
                pass
        
        entry = (
            f"Tweet #{idx}:\n"
            f"- Source URL: {t.get('tweet_url', '')}\n"
            f"- Author: {t.get('author_name')} (@{t.get('author_handle', '').lstrip('@')})\n"
            f"- Metrics: Likes: {t.get('likes_count', 0)}, Reposts: {t.get('reposts_count', 0)}, Score: {t.get('viral_score', 0)}\n"
            f"- Text: {t.get('text_raw', '')}\n"
        )
        if summary:
            entry += f"- Russian Summary: {summary}\n"
        tweets_context.append(entry)

    prompt = (
        "Here are the top AI tweets from the last 24 hours:\n\n"
        + "\n---\n".join(tweets_context)
        + "\n\nSynthesize this into a structured daily digest matching the schema. "
        + "Ensure every highlight in key_highlights has its corresponding source_url from the tweets above."
    )

    primary_model = settings.primary_model
    fallback_model = settings.fallback_model
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        response_schema=DailyDigestSummary,
        temperature=0.3,
    )

    # Attempt on primary model
    primary_hit_429 = False
    await model_rate_limiter.acquire(primary_model, min_interval=get_model_interval())
    try:
        response = await current_client.aio.models.generate_content(
            model=primary_model,
            contents=prompt,
            config=config,
        )
        parsed_dict = _parse_digest_json(response.text)
        return DailyDigestSummary(**parsed_dict)
    except ClientError as e:
        if is_rate_limit_error(e):
            primary_hit_429 = True
            safe_print(f"⏳ Достигнут лимит 429 на {primary_model} при сборке дайджеста. Пауза 2с перед fallback на {fallback_model}...")
            await asyncio.sleep(2)
        else:
            safe_print(f"⚠️ Ошибка вызова Gemini ({primary_model}) при генерации дайджеста: {e}")
            raise e
    except Exception as e:
        if is_rate_limit_error(e):
            primary_hit_429 = True
            safe_print(f"⏳ Лимит 429 на {primary_model} при сборке дайджеста. Пауза 2с перед fallback на {fallback_model}...")
            await asyncio.sleep(2)
        else:
            safe_print(f"⚠️ Ошибка вызова Gemini ({primary_model}) при генерации дайджеста: {e}")
            raise e

    if primary_hit_429:
        await model_rate_limiter.acquire(fallback_model, min_interval=get_model_interval())
        try:
            safe_print(f"🤖 Генерация дайджеста через резервную модель {fallback_model}...")
            response = await current_client.aio.models.generate_content(
                model=fallback_model,
                contents=prompt,
                config=config,
            )
            parsed_dict = _parse_digest_json(response.text)
            return DailyDigestSummary(**parsed_dict)
        except ClientError as e:
            if is_rate_limit_error(e):
                safe_print(f"⚠️ Резервная модель {fallback_model} также вернула 429 RESOURCE_EXHAUSTED при генерации дайджеста.")
            else:
                safe_print(f"⚠️ Ошибка вызова резервной модели {fallback_model} при генерации дайджеста: {e}")
            raise e
        except Exception as e:
            if is_rate_limit_error(e):
                safe_print(f"⚠️ Резервная модель {fallback_model} также вернула 429 RESOURCE_EXHAUSTED при генерации дайджеста: {e}")
            else:
                safe_print(f"⚠️ Ошибка вызова резервной модели {fallback_model} при генерации дайджеста: {e}")
            raise e


def format_digest_post(digest: Any, date_str: str, header_title: Optional[str] = None) -> str:
    """Formats the DailyDigestSummary into an elegant Telegram HTML post with inline source hyperlinks."""
    if isinstance(digest, str):
        title = header_title or f"⚡ Дайджест {date_str}"
        return f"<b>{html.escape(title)}</b>\n\n{digest}"

    raw_headline = (getattr(digest, "main_headline_ru", None) or "").strip()
    safe_headline = html.escape(raw_headline)
    main_url = getattr(digest, "main_story_url", "")
    if main_url and main_url.strip():
        safe_main_url = html.escape(main_url.strip(), quote=True)
        main_headline_html = f'<a href="{safe_main_url}"><b>{safe_headline}</b></a>'
    else:
        main_headline_html = f'<b>{safe_headline}</b>'

    main_story_summary_ru = html.escape((digest.main_story_summary_ru or "").strip())

    highlights_lines = []
    if digest.key_highlights:
        for h in digest.key_highlights:
            title = html.escape((h.title_ru or "").strip())
            summary = html.escape((h.summary_ru or "").strip())
            url = (h.source_url or "").strip()
            if url:
                safe_url = html.escape(url, quote=True)
                highlights_lines.append(f'• <a href="{safe_url}"><b>{title}</b></a>: {summary}')
            else:
                highlights_lines.append(f'• <b>{title}</b>: {summary}')
    elif digest.key_highlights_ru:
        for h in digest.key_highlights_ru:
            if "<a href=" in h:
                highlights_lines.append(f"• {h}")
            else:
                highlights_lines.append(f"• {html.escape(h)}")

    highlights_text = "\n".join(highlights_lines)
    takeaways_text = "\n".join([f"• {html.escape(t.strip())}" for t in (digest.takeaways_ru or []) if t and t.strip()])
    
    hashtags_list = [html.escape(h) for h in normalize_hashtags(digest.hashtags or [])]
    hashtags_text = " ".join(hashtags_list)

    header = header_title if header_title else f"🌟 <b>ДАЙДЖЕСТ AI-НОВОСТЕЙ ЗА СУТКИ ({date_str})</b>"

    post = f"""{header}

🔥 <b>ГЛАВНАЯ ТЕМА:</b> {main_headline_html}
{main_story_summary_ru}

⚡ <b>ДРУГИЕ ВАЖНЫЕ СОБЫТИЯ:</b>
{highlights_text}

💡 <b>СТРАТЕГИЧЕСКИЕ ИНСАЙТЫ:</b>
{takeaways_text}

{hashtags_text}"""
    return enforce_hashtags_in_post(post)


async def build_and_send_digest_chunks(
    tweets: List[Dict[str, Any]],
    title_prefix: str,
    date_display_str: str,
    chunk_size: Optional[int] = None,
    is_test: bool = False,
    max_posts: Optional[int] = None,
    interval_minutes: Optional[int] = None,
) -> List[str]:
    """Splits tweets into chunks, generates Gemini digests for each chunk, and sends them sequentially to Telegram."""
    if not tweets:
        return []

    actual_chunk_size = chunk_size if (chunk_size is not None and chunk_size > 0) else getattr(settings, "backlog_max_chunk_size", 6) or 6
    chunks = [tweets[i:i + actual_chunk_size] for i in range(0, len(tweets), actual_chunk_size)]
    if max_posts is not None and max_posts > 0:
        chunks = chunks[:max_posts]
    total_parts = len(chunks)
    published_posts = []

    for idx, chunk in enumerate(chunks, start=1):
        if total_parts > 1:
            header_title = f"{title_prefix} (Часть {idx} из {total_parts})"
        else:
            header_title = f"{title_prefix}"

        safe_print(f"🤖 Генерация {title_prefix} (Блок {idx}/{total_parts}, {len(chunk)} твитов)...")
        try:
            digest_summary = await generate_digest_with_gemini(chunk)
        except Exception as gemini_err:
            safe_print(f"⚠️ Ошибка Gemini при сборке блока {idx}: {gemini_err}. Использован резервный шаблон.")
            top = chunk[0]
            digest_summary = DailyDigestSummary(
                main_headline_ru=f"Главное событие: @{top.get('author_handle', '').lstrip('@') or 'AI'}",
                main_story_summary_ru=top.get('text_raw', '')[:200],
                main_story_url=top.get('tweet_url', ''),
                key_highlights=[
                    DigestHighlightItem(
                        title_ru=t.get('text_raw', '')[:50],
                        summary_ru=t.get('text_raw', '')[:150],
                        source_url=t.get('tweet_url', '')
                    ) for t in chunk[1:5]
                ],
                takeaways_ru=["Развитие AI экосистемы", "Интеграция новых решений"],
                hashtags=["#AIDigest", "#TechNews"]
            )

        formatted_post = format_digest_post(digest_summary, date_display_str, header_title=header_title)
        published_posts.append(formatted_post)

        safe_print("\n" + "=" * 50)
        safe_print(formatted_post)
        safe_print("=" * 50 + "\n")

        if not is_test:
            safe_print(f"📤 Отправка блока {idx}/{total_parts} в Telegram...")
            await send_post_to_channel(formatted_post, tweet_url=None)
            for tweet in chunk:
                await update_tweet_status_and_score(
                    tweet_id=tweet["tweet_id"],
                    score=tweet.get("viral_score", 0.0),
                    is_viral=tweet.get("is_viral", False),
                    status="POSTED",
                    impact_score=tweet.get("impact_score", 0),
                    summary_json=tweet.get("summary_json") if isinstance(tweet.get("summary_json"), str) else json.dumps(tweet.get("summary_json"), ensure_ascii=False) if tweet.get("summary_json") else None
                )
            safe_print(f"✅ Блок {idx}/{total_parts} опубликован. {len(chunk)} постов отмечены как POSTED.")
            if idx < total_parts:
                delay_minutes = interval_minutes if interval_minutes is not None else settings.digest_block_interval_minutes
                delay_minutes = delay_minutes or 5
                interval_sec = delay_minutes * 60
                safe_print(f"⏳ Пауза {delay_minutes} мин. перед отправкой следующего блока...")
                await asyncio.sleep(interval_sec)
        else:
            safe_print(f"🧪 Тестовый режим: блок {idx}/{total_parts} сформирован, отправка пропущена.")
            for tweet in chunk:
                tweet["status"] = "POSTED"

    return published_posts


async def run_digest_schedule_rule(rule: Dict[str, Any], is_test: bool = False) -> List[str]:
    """Исполняет одно структурированное правило, оставляя переполнение в очереди."""
    await init_db()
    tweets = await get_unposted_tweets()
    sort_order = rule.get("sort_order", "BY_VIRAL_SCORE")
    if sort_order == "CHRONOLOGICAL_DESC":
        tweets.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    elif sort_order == "CHRONOLOGICAL_ASC":
        tweets.sort(key=lambda item: item.get("created_at", ""))
    elif sort_order == "RANDOM":
        import random
        random.shuffle(tweets)
    else:
        tweets.sort(key=lambda item: float(item.get("viral_score", 0) or 0), reverse=True)

    max_items = max(1, int(rule.get("max_items_per_post", 5)))
    max_posts = max(1, int(rule.get("max_posts", 1)))
    capacity = max_items * max_posts
    selected = tweets[:capacity]
    if not selected:
        safe_print(f"ℹ️ Для правила «{rule.get('title', 'Дайджест')}» нет постов в очереди.")
        return []

    title = str(rule.get("title") or "Дайджест новостей").strip()
    date_display = datetime.now(get_app_timezone()).strftime("%d.%m.%Y")
    return await build_and_send_digest_chunks(
        tweets=selected,
        title_prefix=title,
        date_display_str=date_display,
        chunk_size=max_items,
        max_posts=max_posts,
        interval_minutes=max(0, int(rule.get("interval_minutes", 5))),
        is_test=is_test,
    )


async def run_morning_digest_parts(is_test: bool = False) -> List[str]:
    """Generates and publishes morning digest ('Пока вы спали') in multi-part blocks."""
    await init_db()
    if not getattr(settings, "backlog_digest_enabled", True):
        safe_print("ℹ️ Backlog digest отключен в настройках.")
        return []

    date_display_str = datetime.now(get_app_timezone()).strftime("%d.%m.%Y")
    tweets = await get_unposted_tweets()

    if not tweets:
        safe_print("ℹ️ Нет невышедших ночных новостей для утреннего дайджеста.")
        return []

    safe_print(f"🌅 Запуск утреннего дайджеста «ПОКА ВЫ СПАЛИ» ({len(tweets)} постов в очереди)...")
    return await build_and_send_digest_chunks(
        tweets=tweets,
        title_prefix="🌅 ДАЙДЖЕСТ «ПОКА ВЫ СПАЛИ»",
        date_display_str=date_display_str,
        is_test=is_test
    )


async def run_evening_digest_parts(is_test: bool = False) -> List[str]:
    """Generates and publishes evening digest in multi-part blocks to clear the queue before sleep."""
    await init_db()
    if not getattr(settings, "backlog_digest_enabled", True):
        safe_print("ℹ️ Backlog digest отключен в настройках.")
        return []

    date_display_str = datetime.now(get_app_timezone()).strftime("%d.%m.%Y")
    tweets = await get_unposted_tweets()

    if not tweets:
        safe_print("ℹ️ Стек постов перед сном пуст. Вечерний дайджест не требуется.")
        return []

    safe_print(f"🌙 Запуск вечернего дайджеста для очистки стека ({len(tweets)} постов в очереди)...")
    return await build_and_send_digest_chunks(
        tweets=tweets,
        title_prefix="🌙 ВЕЧЕРНИЙ ДАЙДЖЕСТ",
        date_display_str=date_display_str,
        is_test=is_test
    )



async def run_daily_digest(slot_id: Optional[str] = None, is_test: bool = False) -> Optional[str]:
    """
    Collects tweets from the last 24 hours, revalidates their metrics,
    checks for super-viral candidates to publish as standalone posts,
    generates a Gemini digest with inline source links, and publishes to Telegram.
    """
    await init_db()
    today_str = datetime.now(get_app_timezone()).strftime("%Y-%m-%d")
    date_display_str = datetime.now(get_app_timezone()).strftime("%d.%m.%Y")
    digest_key = slot_id if slot_id else today_str

    if not is_test and await is_digest_posted(digest_key):
        safe_print(f"ℹ️ Дайджест ({digest_key}) уже был опубликован. Пропускаем.")
        return None

    safe_print("📊 Сбор твитов за последние 24 часа для дайджеста...")
    tweets = await get_tweets_last_24h()

    if not tweets:
        safe_print("ℹ️ Нет твитов за последние 24 часа для формирования дайджеста.")
        return None

    safe_print(f"📥 Найдено {len(tweets)} твитов за 24 часа. Запуск ревалидации метрик...")

    # Revalidate metrics for top candidates
    candidate_ids = [t["tweet_id"] for t in tweets[:15]]
    try:
        updated_metrics = await refresh_tweet_metrics(candidate_ids)
        now = datetime.now(timezone.utc)
        
        metrics_by_id = {m["tweet_id"]: m for m in updated_metrics}
        for tweet in tweets:
            tid = tweet["tweet_id"]
            if tid in metrics_by_id:
                m = metrics_by_id[tid]
                try:
                    created_at = datetime.fromisoformat(tweet["created_at"])
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                except Exception:
                    created_at = now

                age_hours = max((now - created_at).total_seconds() / 3600.0, 1.0)
                new_score = calculate_viral_score(
                    likes=m["likes_count"],
                    reposts=m["reposts_count"],
                    quotes=m["quotes_count"],
                    bookmarks=m["bookmarks_count"],
                    views=m["impressions_count"],
                    age_in_hours=age_hours,
                )
                tweet["likes_count"] = m["likes_count"]
                tweet["reposts_count"] = m["reposts_count"]
                tweet["quotes_count"] = m["quotes_count"]
                tweet["bookmarks_count"] = m["bookmarks_count"]
                tweet["impressions_count"] = m["impressions_count"]
                tweet["viral_score"] = new_score
                tweet["is_viral"] = new_score >= settings.min_viral_score

                await update_tweet_metrics(
                    tweet_id=tid,
                    likes=m["likes_count"],
                    reposts=m["reposts_count"],
                    quotes=m["quotes_count"],
                    bookmarks=m["bookmarks_count"],
                    views=m["impressions_count"],
                    viral_score=new_score,
                    is_viral=tweet["is_viral"],
                )
    except Exception as err:
        safe_print(f"⚠️ Ошибка ревалидации метрик: {err}. Используем текущие данные.")

    # Sort tweets descending by viral_score
    tweets.sort(key=lambda t: t.get("viral_score", 0.0), reverse=True)

    # Check for super-viral candidates to publish as standalone posts
    super_threshold = getattr(settings, "super_viral_threshold", 100.0)
    super_candidates = [
        t for t in tweets
        if t.get("status") != "POSTED" and t.get("viral_score", 0.0) >= super_threshold
    ]

    if super_candidates:
        super_tweet = super_candidates[0]
        safe_print(f"🚀 Обнаружена супер-виральная новость ({super_tweet['tweet_id']}, score={super_tweet.get('viral_score', 0):.1f})!")
        
        summary_data = None
        if super_tweet.get("summary_json"):
            try:
                summary_data = json.loads(super_tweet["summary_json"]) if isinstance(super_tweet["summary_json"], str) else super_tweet["summary_json"]
            except Exception:
                pass

        if not summary_data or not summary_data.get("title_ru"):
            try:
                from src.ai.agents.editor import call_llm
                summary_data = await call_llm(super_tweet.get("text_raw", ""), media_urls=super_tweet.get("media_urls", []))
            except Exception as err:
                safe_print(f"⚠️ Ошибка генерации саммари для супер-вирального поста: {err}")
                summary_data = None

            if not summary_data or not summary_data.get("title_ru"):
                summary_data = {
                    "title_ru": super_tweet.get("text_raw", "")[:60],
                    "summary_ru": super_tweet.get("text_raw", ""),
                    "takeaways": ["Ключевой AI-прорыв дня"],
                    "hashtags": ["#AI", "#BreakingNews"],
                    "impact_score": 10,
                }

        media_urls = super_tweet.get("media_urls", [])
        selected_idx = summary_data.get("selected_image_index") if summary_data else None
        photo_url = None
        if selected_idx is not None and isinstance(selected_idx, int) and 0 <= selected_idx < len(media_urls):
            photo_url = media_urls[selected_idx]
        elif media_urls and (selected_idx == 0 or selected_idx is None):
            photo_url = media_urls[0]

        formatted_standalone = format_telegram_post(super_tweet, summary_data, is_photo=bool(photo_url))
        super_tweet_url = super_tweet.get("tweet_url", "https://x.com")

        if not is_test:
            safe_print(f"📤 Отправка супер-вирального поста {super_tweet['tweet_id']} в Telegram...")
            if photo_url:
                await send_photo_to_channel(formatted_standalone, photo_url, super_tweet_url)
            else:
                await send_post_to_channel(formatted_standalone, super_tweet_url)

            await update_tweet_status_and_score(
                tweet_id=super_tweet["tweet_id"],
                score=super_tweet.get("viral_score", 0.0),
                is_viral=True,
                status="POSTED",
                impact_score=summary_data.get("impact_score", 10),
                summary_json=json.dumps(summary_data, ensure_ascii=False),
            )
            super_tweet["status"] = "POSTED"
            safe_print("✅ Супер-виральная новость успешно опубликована отдельным постом.")
        else:
            safe_print("🧪 Тестовый режим: супер-виральный пост определен, отправка пропущена.")
            super_tweet["status"] = "POSTED"

        # Keep remaining tweets for the digest, or fallback to all if only 1 was available
        remaining_for_digest = [t for t in tweets if t["tweet_id"] != super_tweet["tweet_id"]]
        if remaining_for_digest:
            tweets = remaining_for_digest

    safe_print("🤖 Генерация дайджеста через Gemini...")
    try:
        digest_summary = await generate_digest_with_gemini(tweets)
    except Exception as gemini_err:
        safe_print(f"⚠️ Ошибка генерации дайджеста через Gemini: {gemini_err}. Создание резервного дайджеста...")
        top_tweet = tweets[0]
        top_summary = ""
        if top_tweet.get("summary_json"):
            try:
                s_dict = json.loads(top_tweet["summary_json"]) if isinstance(top_tweet["summary_json"], str) else top_tweet["summary_json"]
                top_summary = s_dict.get("summary_ru", top_tweet.get("text_raw", ""))
            except Exception:
                top_summary = top_tweet.get("text_raw", "")
        else:
            top_summary = top_tweet.get("text_raw", "")

        highlights = []
        for t in tweets[1:5]:
            t_summary = ""
            t_title = t.get("text_raw", "")[:50]
            if t.get("summary_json"):
                try:
                    s_dict = json.loads(t["summary_json"]) if isinstance(t["summary_json"], str) else t["summary_json"]
                    t_title = s_dict.get("title_ru", t_title)
                    t_summary = s_dict.get("summary_ru", t.get("text_raw", ""))
                except Exception:
                    t_summary = t.get("text_raw", "")
            else:
                t_summary = t.get("text_raw", "")

            highlights.append(
                DigestHighlightItem(
                    title_ru=t_title,
                    summary_ru=t_summary,
                    source_url=t.get("tweet_url", ""),
                )
            )

        digest_summary = DailyDigestSummary(
            main_headline_ru=f"Главное событие дня от @{top_tweet.get('author_handle', '').lstrip('@') or 'AI'}",
            main_story_summary_ru=top_summary,
            main_story_url=top_tweet.get("tweet_url", ""),
            key_highlights=highlights if highlights else None,
            takeaways_ru=["Активное развитие открытых моделей", "Интеграция новых AI-инструментов"],
            hashtags=["#AIDigest", "#AI", "#TechNews"],
        )

    formatted_digest = format_digest_post(digest_summary, date_display_str)

    safe_print("\n" + "=" * 50)
    safe_print(formatted_digest)
    safe_print("=" * 50 + "\n")

    if not is_test:
        safe_print("📤 Отправка дайджеста в Telegram-канал...")
        # Send digest post without bottom button — links are embedded in keywords/headlines
        await send_post_to_channel(formatted_digest, tweet_url=None)
        await mark_digest_posted(digest_key, tweet_count=len(tweets))
        
        for tweet in tweets:
            if super_candidates and tweet["tweet_id"] == super_tweet["tweet_id"]:
                continue
            await update_tweet_status_and_score(
                tweet_id=tweet["tweet_id"],
                score=tweet.get("viral_score", 0.0),
                is_viral=tweet.get("is_viral", False),
                status="POSTED",
                impact_score=tweet.get("impact_score", 0),
                summary_json=tweet.get("summary_json") if isinstance(tweet.get("summary_json"), str) else json.dumps(tweet.get("summary_json"), ensure_ascii=False) if tweet.get("summary_json") else None
            )

        safe_print("✅ Дайджест успешно опубликован и зафиксирован в БД.")
    else:
        safe_print("🧪 Тестовый режим: отправка в Telegram пропущена.")

    return formatted_digest


def main() -> None:
    parser = argparse.ArgumentParser(description="Сборка и публикация AI дайджеста.")
    parser.add_argument("--test", action="store_true", help="Запуск в тестовом режиме без публикации в Telegram")
    parser.add_argument("--slot", type=str, default=None, help="Идентификатор слота дайджеста (например, 2026-09-13_14:00)")
    args = parser.parse_args()

    asyncio.run(run_daily_digest(slot_id=args.slot, is_test=args.test))


if __name__ == "__main__":
    main()
