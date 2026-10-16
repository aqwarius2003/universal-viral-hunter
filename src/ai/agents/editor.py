import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Union
import aiohttp
try:
    from google import genai
    from google.genai import types
    from google.genai.errors import ClientError, APIError
except ImportError:
    genai = None
    types = None
    class ClientError(Exception):
        code: int = 429
    class APIError(Exception):
        pass

from src.ai.agents.state import AgentState
from src.ai.rate_limiter import model_rate_limiter, get_model_interval
from src.ai.schema import TweetSummary, ImageSelection
from src.config import settings
from src.db import update_tweet_status_and_score, get_active_topics
from src.utils.hashtags import normalize_hashtags
from src.utils.technical_audit import audit_and_prepare_telegram_content

logger = logging.getLogger("viral-hunter.editor")

client: Optional[Any] = None


def get_client() -> Any:
    global client
    if client is None:
        if not settings.ai_api_key or settings.ai_api_key.startswith("your_"):
            raise RuntimeError("AI_API_KEY is not configured. Complete the setup in the web dashboard.")
        client = genai.Client(api_key=settings.ai_api_key)
    return client


def reset_client() -> None:
    """Resets cached Gemini client so newly saved API keys take effect immediately."""
    global client
    client = None


_ai_status: Dict[str, Any] = {
    "status": "pending",
    "message": "Проверка состояния...",
    "model": settings.ai_model_name,
    "waiting_seconds": 0,
    "last_updated": time.time(),
}


def get_ai_status() -> Dict[str, Any]:
    """Returns current Gemini API readiness and rate limit status."""
    _ai_status["model"] = settings.ai_model_name
    _ai_status["primary_model"] = settings.primary_model
    _ai_status["fallback_model"] = settings.fallback_model
    _ai_status["delay_setting"] = settings.ai_request_delay_seconds
    return dict(_ai_status)


def set_ai_status(status: str, message: str, waiting_seconds: int = 0) -> None:
    """Updates global AI API status state."""
    _ai_status["status"] = status
    _ai_status["message"] = message
    _ai_status["waiting_seconds"] = waiting_seconds
    _ai_status["last_updated"] = time.time()


def is_rate_limit_error(e: Exception) -> bool:
    """Checks if an exception indicates HTTP 429 / RESOURCE_EXHAUSTED quota limits."""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if code == 429 or code == "429":
        return True
    resp = getattr(e, "response_json", None)
    if isinstance(resp, dict):
        err_dict = resp.get("error", {})
        if isinstance(err_dict, dict):
            c = err_dict.get("code")
            if c == 429 or c == "429":
                return True
            status = str(err_dict.get("status", "")).upper()
            if "RESOURCE_EXHAUSTED" in status:
                return True
    err_str = str(e).upper()
    return "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "QUOTA" in err_str


def parse_json_safely(text: str) -> dict:
    """Parses JSON response, safely stripping markdown code fences if present."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


async def download_image(session: aiohttp.ClientSession, url: str) -> Optional[tuple[bytes, str]]:
    """Downloads an image from URL or reads from local file and returns (bytes, mime_type) or None."""
    if not url:
        return None
    # Check for local file path (e.g. video thumbnails)
    if isinstance(url, str) and (os.path.exists(url) or (not url.startswith("http://") and not url.startswith("https://"))):
        if os.path.exists(url):
            try:
                with open(url, "rb") as f:
                    data = f.read()
                mime = "image/png" if url.lower().endswith(".png") else "image/jpeg"
                return data, mime
            except Exception as err:
                print(f"⚠️ Ошибка чтения локального файла {url}: {err}")
        return None
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.read()
                content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
                if not content_type.startswith("image/"):
                    content_type = "image/jpeg"
                return data, content_type
    except Exception as err:
        print(f"⚠️ Ошибка загрузки изображения {url}: {err}")
    return None


async def select_key_image(
    text: str,
    media_urls: List[str],
    model: Optional[str] = None,
) -> tuple[Optional[int], Optional[str]]:
    """
    Selects the most informative image from attached media URLs using the dedicated multimodal model.
    Isolates image evaluation to avoid consuming the primary text model quota.
    """
    if not media_urls:
        return None, None

    if len(media_urls) == 1:
        return 0, "Единственное прикрепленное изображение"

    target_model = model or settings.fallback_model

    async with aiohttp.ClientSession() as session:
        tasks = [download_image(session, url) for url in media_urls[:4]]
        downloaded = await asyncio.gather(*tasks)

    image_parts = []
    valid_indices = []
    for idx, res in enumerate(downloaded):
        if res is not None:
            data, mime = res
            part = types.Part.from_bytes(data=data, mime_type=mime)
            image_parts.append(part)
            valid_indices.append(idx)

    if not valid_indices:
        return None, "Не удалось загрузить изображения"

    if len(valid_indices) == 1:
        return valid_indices[0], "Единственное успешно загруженное изображение"

    prompt_text = (
        f"Context tweet: {text}\n\n"
        f"There are {len(image_parts)} images attached (original indices: {valid_indices}). "
        "Carefully evaluate them and select the single most informative image (e.g. architecture diagram, "
        "benchmark graph, table, code sample). If one is informative, set `selected_image_index` to its original index "
        "and provide `image_reason` in Russian. If none are informative, set `selected_image_index` to null."
    )
    contents = list(image_parts)
    contents.append(prompt_text)

    await model_rate_limiter.acquire(target_model, min_interval=get_model_interval())

    try:
        current_client = get_client()
        set_ai_status("busy", f"Выбор ключевой картинки моделью {target_model}...")
        response = await current_client.aio.models.generate_content(
            model=target_model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=(
                    "You are an expert visual editor. "
                    "Analyze attached tweet images and select the most informative one. "
                    "Return strict JSON matching the schema with selected_image_index and image_reason in Russian."
                ),
                response_mime_type="application/json",
                response_schema=ImageSelection,
                temperature=0.1,
            ),
        )
        parsed = parse_json_safely(response.text)
        sel_idx = parsed.get("selected_image_index")
        sel_reason = parsed.get("image_reason")
        if sel_idx is not None and sel_idx not in valid_indices:
            sel_idx = valid_indices[0]
        set_ai_status("ok", f"Готов к работе ({settings.primary_model})")
        return sel_idx, sel_reason
    except ClientError as e:
        logger.warning(f"⚠️ ClientError при анализе картинок на {target_model}: {e}")
        return valid_indices[0], "Выбрано первое изображение (ошибка ИИ при выборе)"
    except Exception as e:
        logger.warning(f"⚠️ Ошибка при анализе картинок на {target_model}: {e}")
        return valid_indices[0], "Выбрано первое изображение"


async def call_llm(
    text: Optional[Union[str, Dict[str, Any]]] = None,
    media_urls: Optional[List[str]] = None,
    max_retries: int = 1,
    topic: Optional[Dict[str, Any]] = None,
    editor_instruction: Optional[str] = None,
    **kwargs: Any,
) -> Optional[dict]:
    """
    Two-model architecture with automatic fallback:
    1. Key image selection (if multiple images) routed isolated to fallback model (gemini-3.1-flash-lite).
    2. Text analysis and TweetSummary routed by default to primary model (gemini-3.5-flash-lite).
    3. On HTTP 429 RESOURCE_EXHAUSTED on primary model, automatically pauses 2s and retries on fallback model.
    4. If fallback model also returns 429, logs warning and returns None (skipping post without crashing).
    """
    tweet = kwargs.get("tweet")
    if isinstance(text, dict):
        tweet = text
        text = tweet.get("text_raw", "")
    elif text is not None and tweet is None:
        tweet = {"text_raw": text}
    elif tweet is not None and text is None:
        text = tweet.get("text_raw", "")
    elif tweet is None and text is None:
        tweet = {"text_raw": ""}
        text = ""

    primary_model = settings.primary_model
    fallback_model = settings.fallback_model

    # Step 1: Multimodal image selection (isolated to fallback model if multiple images)
    selected_img_idx: Optional[int] = None
    selected_img_reason: Optional[str] = None
    if media_urls:
        if len(media_urls) > 1:
            selected_img_idx, selected_img_reason = await select_key_image(
                text=text,
                media_urls=media_urls,
                model=fallback_model,
            )
        elif len(media_urls) == 1:
            selected_img_idx = 0
            selected_img_reason = "Единственное прикрепленное изображение/видео"

    # Step 2: Prepare pure text prompt for TweetSummary
    system_prompt = (topic.get("system_prompt") if topic and topic.get("system_prompt") else None) or settings.editor_system_prompt
    prompt_template = (topic.get("prompt_template") if topic and topic.get("prompt_template") else None) or settings.editor_prompt_template

    if not system_prompt or not system_prompt.strip():
        system_prompt = (
            "You are an expert news editor and media analyst. "
            "Create a strict JSON output matching the schema. "
            "Output all JSON string values strictly in Russian. "
            "All hashtags must strictly start with '#' (e.g. #Новости #Видео #Происшествия). "
            "No emojis in title or takeaways. No conversational filler."
        )
    if not prompt_template or not prompt_template.strip():
        prompt_template = (
            "Analyze this tweet: {text}\nAuthor: {author}\nDate: {date}\nMedia count: {media_count}\n\n"
            "Generate Russian title, summary, 3 key takeaways, impact score (1-10), impact reason, and hashtags (starting with #)."
        )
    
    author = tweet.get("author_handle", "") or tweet.get("author_name", "") or "Unknown"
    date = tweet.get("created_at", "")
    media_count = len(media_urls) if media_urls else 0
    try:
        prompt_text = prompt_template.format(
            text=text,
            author=author,
            date=date,
            media_count=media_count
        )
    except Exception:
        prompt_text = f"Analyze this tweet: {text}\nAuthor: {author}\nDate: {date}\nMedia count: {media_count}"

    if editor_instruction and str(editor_instruction).strip():
        instruction_block = (
            f"\n\n🔥 КРИТИЧЕСКИ ВАЖНОЕ УКАЗАНИЕ ГЛАВНОГО РЕДАКТОРА:\n"
            f"«{str(editor_instruction).strip()}»\n"
            f"Обязательно строго перепиши заголовок, суть и тональность с учетом этого требования!"
        )
        prompt_text += instruction_block

    contents = [prompt_text]
    
    # If media contains exactly one image (or thumbnail), and no selection was needed, pass it to the text model for multimodal context
    if media_urls and len(media_urls) == 1:
        try:
            async with aiohttp.ClientSession() as session:
                img_data = await download_image(session, media_urls[0])
                if img_data:
                    data, mime = img_data
                    contents.append(types.Part.from_bytes(data=data, mime_type=mime))
        except Exception as e:
            logger.warning(f"Failed to append image to text model prompt: {e}")

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        response_schema=TweetSummary,
        temperature=0.2,
    )

    current_client = get_client()

    # Step 3: Attempt 1 - Primary Text Model
    primary_429 = False
    await model_rate_limiter.acquire(primary_model, min_interval=get_model_interval())
    try:
        set_ai_status("busy", f"Обработка запроса моделью {primary_model}...")
        response = await current_client.aio.models.generate_content(
            model=primary_model,
            contents=contents,
            config=config,
        )
        parsed_dict = parse_json_safely(response.text)
        if "hashtags" in parsed_dict:
            parsed_dict["hashtags"] = normalize_hashtags(parsed_dict["hashtags"])
        parsed_dict["selected_image_index"] = selected_img_idx
        parsed_dict["image_reason"] = selected_img_reason

        # Run strict Technical Audit BEFORE returning
        is_media = bool(media_urls or (tweet and tweet.get("video_data")))
        audit = audit_and_prepare_telegram_content(
            title=parsed_dict.get("title_ru", ""),
            summary=parsed_dict.get("summary_ru", ""),
            takeaways=parsed_dict.get("takeaways", []),
            hashtags=parsed_dict.get("hashtags", []),
            is_media=is_media,
            author_handle=tweet.get("author_handle") if tweet else None,
            tweet_url=tweet.get("tweet_url") if tweet else None,
        )
        parsed_dict["title_ru"] = audit["title_ru"]
        parsed_dict["summary_ru"] = audit["summary_ru"]
        parsed_dict["takeaways"] = audit["takeaways"]
        parsed_dict["hashtags"] = audit["hashtags"]
        parsed_dict["formatted_text"] = audit["formatted_text"]
        parsed_dict["technical_audit"] = {
            "is_valid": audit["is_valid"],
            "char_count": audit["char_count"],
            "max_limit": audit["max_limit"],
            "warnings": audit["warnings"],
        }
        set_ai_status("ok", f"Готов к работе ({primary_model})")
        return parsed_dict
    except ClientError as e:
        if is_rate_limit_error(e):
            primary_429 = True
            msg = f"⏳ Достигнут лимит 429 RESOURCE_EXHAUSTED на {primary_model}. Пауза 2с перед fallback на {fallback_model}..."
            print(msg)
            logger.warning(msg)
            set_ai_status("waiting", msg, waiting_seconds=2)
            await asyncio.sleep(2)
        else:
            set_ai_status("error", f"Ошибка Gemini API ({primary_model}): {e}")
            raise
    except Exception as e:
        if is_rate_limit_error(e):
            primary_429 = True
            msg = f"⏳ Лимит 429 на {primary_model}: {e}. Пауза 2с перед fallback на {fallback_model}..."
            print(msg)
            logger.warning(msg)
            set_ai_status("waiting", msg, waiting_seconds=2)
            await asyncio.sleep(2)
        else:
            set_ai_status("error", f"Ошибка Gemini API ({primary_model}): {e}")
            raise

    # Step 4: Attempt 2 - Fallback Model
    if primary_429:
        await model_rate_limiter.acquire(fallback_model, min_interval=get_model_interval())
        try:
            set_ai_status("busy", f"Обработка запроса резервной моделью {fallback_model}...")
            response = await current_client.aio.models.generate_content(
                model=fallback_model,
                contents=contents,
                config=config,
            )
            parsed_dict = parse_json_safely(response.text)
            if "hashtags" in parsed_dict:
                parsed_dict["hashtags"] = normalize_hashtags(parsed_dict["hashtags"])
            parsed_dict["selected_image_index"] = selected_img_idx
            parsed_dict["image_reason"] = selected_img_reason

            # Run strict Technical Audit BEFORE returning
            is_media = bool(media_urls or (tweet and tweet.get("video_data")))
            audit = audit_and_prepare_telegram_content(
                title=parsed_dict.get("title_ru", ""),
                summary=parsed_dict.get("summary_ru", ""),
                takeaways=parsed_dict.get("takeaways", []),
                hashtags=parsed_dict.get("hashtags", []),
                is_media=is_media,
                author_handle=tweet.get("author_handle") if tweet else None,
                tweet_url=tweet.get("tweet_url") if tweet else None,
            )
            parsed_dict["title_ru"] = audit["title_ru"]
            parsed_dict["summary_ru"] = audit["summary_ru"]
            parsed_dict["takeaways"] = audit["takeaways"]
            parsed_dict["hashtags"] = audit["hashtags"]
            parsed_dict["formatted_text"] = audit["formatted_text"]
            parsed_dict["technical_audit"] = {
                "is_valid": audit["is_valid"],
                "char_count": audit["char_count"],
                "max_limit": audit["max_limit"],
                "warnings": audit["warnings"],
            }
            set_ai_status("ok", f"Готов к работе ({fallback_model})")
            return parsed_dict
        except ClientError as e:
            if is_rate_limit_error(e):
                warn_msg = f"⚠️ Резервная модель {fallback_model} также вернула 429 RESOURCE_EXHAUSTED. Пропуск поста."
                print(warn_msg)
                logger.warning(warn_msg)
                set_ai_status("waiting", warn_msg, waiting_seconds=4)
                return None
            else:
                set_ai_status("error", f"Ошибка резервной модели ({fallback_model}): {e}")
                raise
        except Exception as e:
            if is_rate_limit_error(e):
                warn_msg = f"⚠️ Резервная модель {fallback_model} также вернула 429 RESOURCE_EXHAUSTED: {e}. Пропуск поста."
                print(warn_msg)
                logger.warning(warn_msg)
                set_ai_status("waiting", warn_msg, waiting_seconds=4)
                return None
            else:
                set_ai_status("error", f"Ошибка резервной модели ({fallback_model}): {e}")
                raise

    return None


async def editor_node(state: AgentState) -> Dict[str, Any]:
    """
    Editor Agent: Generates Russian summaries and evaluates images for viral tweets using Gemini.
    Sorts drafts in descending order of combined_score (viral_score * impact_score).
    Skips posts without crashing if quotas are exhausted.
    """
    viral_tweets = state.get("viral_tweets", [])
    draft_posts = state.get("draft_posts", [])
    validation_errors = state.get("validation_errors", [])

    # Identify tweet IDs that failed critic validation
    failed_tweet_ids = set()
    for err in validation_errors:
        if err.startswith("Tweet "):
            parts = err.split(":")
            if parts:
                tid = parts[0].replace("Tweet ", "").strip()
                failed_tweet_ids.add(tid)

    # Keep only valid existing drafts
    kept_drafts = [d for d in draft_posts if d["tweet_id"] not in failed_tweet_ids]
    processed_ids = {draft["tweet_id"] for draft in kept_drafts}
    new_drafts = []

    active_topics = await get_active_topics()
    topics_map = {t["id"]: t for t in active_topics}

    for idx, tweet in enumerate(viral_tweets):
        if tweet["tweet_id"] in processed_ids:
            continue

        print(f"🤖 Отправляю твит {tweet['tweet_id']} на анализ в Gemini ({settings.primary_model})...")
        media_urls = tweet.get("media_urls", [])
        
        # If it's a video, use the thumbnail for multimodal context
        video_data = tweet.get("video_data")
        if video_data and isinstance(video_data, dict):
            thumbnail = video_data.get("thumbnail")
            if thumbnail:
                media_urls = [thumbnail]

        topic = topics_map.get(tweet.get("topic_id"))
        ai_response_dict = await call_llm(tweet.get("text_raw", ""), media_urls=media_urls, topic=topic)

        if ai_response_dict is None:
            warn_msg = f"⚠️ Пропуск поста {tweet['tweet_id']} без падения скрипта (квоты 429 исчерпаны на обеих моделях)."
            print(warn_msg)
            logger.warning(warn_msg)
            continue

        if "hashtags" in ai_response_dict:
            ai_response_dict["hashtags"] = normalize_hashtags(ai_response_dict["hashtags"])

        impact_score = ai_response_dict.get("impact_score", 1)
        viral_score = tweet.get("viral_score", 0.0)
        combined_score = viral_score * impact_score

        # Persist generated Russian summary into DB immediately
        await update_tweet_status_and_score(
            tweet_id=tweet["tweet_id"],
            score=viral_score,
            is_viral=True,
            status="PROCESSED",
            impact_score=impact_score,
            summary_json=json.dumps(ai_response_dict, ensure_ascii=False),
        )

        draft = {
            "tweet_id": tweet["tweet_id"],
            "original_data": tweet,
            "ai_data": ai_response_dict,
            "combined_score": combined_score,
        }
        new_drafts.append(draft)

    all_drafts = list(kept_drafts) + new_drafts

    # Sort all drafts by combined_score descending
    all_drafts.sort(
        key=lambda d: d.get(
            "combined_score",
            d.get("original_data", {}).get("viral_score", 0.0)
            * d.get("ai_data", {}).get("impact_score", 1),
        ),
        reverse=True,
    )

    return {"draft_posts": all_drafts}


async def reprocess_tweet_with_editor_feedback(
    tweet_id: str,
    feedback: str,
) -> Dict[str, Any]:
    """
    Reworks an existing tweet with chief editor instructions (e.g. 'будь милее', 'без жести').
    Sets status to 'PROCESSING' during work, executes Technical Audit BEFORE saving to DB,
    and returns audited data with status 'PENDING_APPROVAL' / 'PROCESSED'.
    """
    from src.db import get_tweet_by_id, set_tweet_status, update_tweet_status_and_score, get_active_topics

    # 1. Mark as PROCESSING in DB so UI immediately reflects processing state
    await set_tweet_status(tweet_id, "PROCESSING")

    try:
        tweet = await get_tweet_by_id(tweet_id)
        if not tweet:
            raise ValueError(f"Твит {tweet_id} не найден в базе данных.")

        media_urls = tweet.get("media_urls", [])
        video_data = tweet.get("video_data")
        if video_data and isinstance(video_data, dict) and video_data.get("thumbnail"):
            media_urls = [video_data["thumbnail"]]

        active_topics = await get_active_topics()
        topic = next((t for t in active_topics if t["id"] == tweet.get("topic_id")), None)

        # 2. Call LLM with editor instruction (technical audit runs automatically inside call_llm)
        ai_data = await call_llm(
            text=tweet.get("text_raw", ""),
            media_urls=media_urls,
            topic=topic,
            editor_instruction=feedback,
            tweet=tweet,
        )
        if not ai_data:
            raise RuntimeError("ИИ временно недоступен (лимит квот исчерпан или модель вернула пустой ответ).")

        # 3. Technical Audit is guaranteed, re-verify
        is_media = bool(media_urls or video_data)
        audit_res = audit_and_prepare_telegram_content(
            title=ai_data.get("title_ru", ""),
            summary=ai_data.get("summary_ru", ""),
            takeaways=ai_data.get("takeaways", []),
            hashtags=ai_data.get("hashtags", []),
            is_media=is_media,
            author_handle=tweet.get("author_handle"),
            tweet_url=tweet.get("tweet_url"),
        )
        ai_data["title_ru"] = audit_res["title_ru"]
        ai_data["summary_ru"] = audit_res["summary_ru"]
        ai_data["takeaways"] = audit_res["takeaways"]
        ai_data["hashtags"] = audit_res["hashtags"]
        ai_data["technical_audit"] = {
            "is_valid": audit_res["is_valid"],
            "char_count": audit_res["char_count"],
            "max_limit": audit_res["max_limit"],
            "warnings": audit_res["warnings"],
        }

        # 4. Save to DB with PENDING_APPROVAL status (or PROCESSED)
        new_status = "PENDING_APPROVAL" if getattr(settings, "approval_required", False) else "PROCESSED"
        await update_tweet_status_and_score(
            tweet_id=tweet_id,
            score=tweet.get("viral_score", 0.0),
            is_viral=True,
            status=new_status,
            impact_score=ai_data.get("impact_score", 0),
            summary_json=json.dumps(ai_data, ensure_ascii=False),
        )

        updated_tweet = await get_tweet_by_id(tweet_id)
        if updated_tweet:
            updated_tweet["audit"] = audit_res
            updated_tweet["ai_data"] = ai_data
            return updated_tweet

        return {
            "tweet_id": tweet_id,
            "status": new_status,
            "summary_data": ai_data,
            "ai_data": ai_data,
            "audit": audit_res,
        }
    except Exception as err:
        try:
            await set_tweet_status(tweet_id, "PROCESSED")
        except Exception:
            pass
        raise err