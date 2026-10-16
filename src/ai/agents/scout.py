import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Set up Windows console encoding if needed
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# twifork (PyPI >= 2.4.0) provides the 'twikit' namespace with the fix for X.com KEY_BYTE indices.
# Both twifork and unclecode/twikit install as the 'twikit' package.
try:
    from twikit import Client
except ImportError:
    try:
        from twifork import Client
    except ImportError:
        Client = None

from src.ai.agents.state import AgentState
from src.config import settings
from src.db import filter_new_tweets, save_raw_tweets, get_active_topics
from src.utils.video_manager import download_video, select_best_variant, get_max_quality


def safe_print(message: str) -> None:
    """Safely prints message handling Windows console encoding issues."""
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        try:
            print(message.encode(encoding, errors="replace").decode(encoding))
        except Exception:
            print(message.encode("ascii", errors="backslashreplace").decode("ascii"))


def safe_int(value: Any, default: int = 0) -> int:
    """Safely converts string, numeric, or None values to int."""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def extract_tweet_text(tweet: Any) -> str:
    """Extracts text from a tweet object or dict, preferring full_text if it is a string."""
    full_text = (
        getattr(tweet, "full_text", None)
        if not isinstance(tweet, dict)
        else tweet.get("full_text")
    )
    if isinstance(full_text, str) and full_text.strip():
        return full_text

    text = (
        getattr(tweet, "text", None)
        if not isinstance(tweet, dict)
        else tweet.get("text")
    )
    if isinstance(text, str):
        return text
    return ""


def parse_tweet_created_at(tweet: Any) -> str:
    """Parses tweet created_at into an ISO 8601 string."""
    dt = getattr(tweet, "created_at_datetime", None)
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()

    raw_created = getattr(tweet, "created_at", None)
    if isinstance(raw_created, datetime):
        if raw_created.tzinfo is None:
            raw_created = raw_created.replace(tzinfo=timezone.utc)
        return raw_created.isoformat()

    if isinstance(raw_created, str):
        # Try ISO format
        try:
            parsed = datetime.fromisoformat(raw_created)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.isoformat()
        except (ValueError, TypeError):
            pass

        # Try Twitter HTTP date format: 'Wed Oct 10 20:19:24 +0000 2018'
        try:
            parsed = datetime.strptime(raw_created, "%a %b %d %H:%M:%S %z %Y")
            return parsed.isoformat()
        except (ValueError, TypeError):
            pass

    return datetime.now(timezone.utc).isoformat()


def extract_media_urls(tweet: Any) -> List[str]:
    """Extracts photo media URLs from a tweet object or dict."""
    media_list = (
        getattr(tweet, "media", None)
        or getattr(tweet, "photos", None)
        if not isinstance(tweet, dict)
        else (tweet.get("media") or tweet.get("photos"))
    )
    if not media_list or not isinstance(media_list, list):
        return []

    media_urls: List[str] = []
    for item in media_list:
        if isinstance(item, dict):
            media_type = item.get("type", "")
            if media_type in ("photo", ""):
                url = item.get("media_url_https") or item.get("media_url") or item.get("url")
                if url and isinstance(url, str) and url.startswith("http"):
                    media_urls.append(url)
        else:
            media_type = getattr(item, "type", "")
            if media_type in ("photo", ""):
                url = (
                    getattr(item, "media_url_https", None)
                    or getattr(item, "media_url", None)
                    or getattr(item, "url", None)
                )
                if url and isinstance(url, str) and url.startswith("http"):
                    media_urls.append(url)
    return media_urls

def extract_video_info(tweet: Any) -> Optional[Dict[str, Any]]:
    """Extracts video metadata from a tweet."""
    media_list = (
        getattr(tweet, "extended_entities", {}).get("media", [])
        if isinstance(getattr(tweet, "extended_entities", None), dict)
        else (
            getattr(tweet, "media", [])
            if not isinstance(tweet, dict)
            else tweet.get("extended_entities", {}).get("media", []) or tweet.get("media", [])
        )
    )
    if not media_list:
        return None

    for item in media_list:
        if isinstance(item, dict):
            media_type = item.get("type", "")
            if media_type in ("video", "animated_gif"):
                video_info = item.get("video_info", {})
                variants = video_info.get("variants", [])
                if variants:
                    thumbnail = item.get("media_url_https") or item.get("media_url")
                    return {
                        "type": media_type,
                        "variants": variants,
                        "thumbnail": thumbnail,
                        "duration": video_info.get("duration_millis", 0)
                    }
        else:
            media_type = getattr(item, "type", "")
            if media_type in ("video", "animated_gif"):
                video_info = getattr(item, "video_info", {})
                if not isinstance(video_info, dict):
                    video_info = getattr(video_info, "__dict__", {}) if video_info else {}
                variants = video_info.get("variants", [])
                
                # Check for item.video_info.variants if video_info is an object
                if not variants and hasattr(item, 'video_info'):
                    try:
                        variants = getattr(item.video_info, "variants", [])
                        # Sometimes variants are objects, we need them as dicts
                        variants = [v.__dict__ if not isinstance(v, dict) else v for v in variants]
                    except:
                        pass
                        
                if variants:
                    thumbnail = getattr(item, "media_url_https", None) or getattr(item, "media_url", None)
                    return {
                        "type": media_type,
                        "variants": variants,
                        "thumbnail": thumbnail,
                        "duration": video_info.get("duration_millis", 0)
                    }
    return None


def load_cookies_from_file(file_path: Path) -> Dict[str, str]:
    """
    Reads cookies from a file supporting both dict mapping:
    {"auth_token": "...", "ct0": "..."}
    and list-of-objects format from browser export tools:
    [{"name": "auth_token", "value": "..."}, ...]
    """
    if not file_path.is_file():
        return {}

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
            elif isinstance(data, list):
                result: Dict[str, str] = {}
                for item in data:
                    if isinstance(item, dict) and "name" in item and "value" in item:
                        result[str(item["name"])] = str(item["value"])
                return result
    except Exception as err:
        safe_print(f"⚠️ Не удалось прочитать {file_path}: {err}")
    return {}


def get_cookies_path() -> Path:
    """Returns the effective cookies path, preferring data/cookies.json if present."""
    p = Path(settings.cookies_path)
    if p.is_file():
        return p
    root_p = Path("cookies.json")
    if root_p.is_file():
        return root_p
    return p


async def get_twitter_client() -> Client:
    """
    Initializes and authenticates the Twitter/X client using cookies.
    Reads credentials from settings (.env) or cookies.json / data/cookies.json.
    """
    proxy = settings.twitter_proxy_url if settings.twitter_proxy_url and settings.twitter_proxy_url.strip() else None
    client = Client(language="en-US", proxy=proxy)

    # Start with cookies from persistent file if available
    cookies_path = get_cookies_path()
    cookies_dict = load_cookies_from_file(cookies_path)
    if not cookies_dict and cookies_path != Path("cookies.json"):
        cookies_dict = load_cookies_from_file(Path("cookies.json"))

    # Environment variables take precedence for auth_token and ct0
    if settings.twitter_auth_token:
        cookies_dict["auth_token"] = settings.twitter_auth_token
    if settings.twitter_ct0:
        cookies_dict["ct0"] = settings.twitter_ct0

    auth_token = cookies_dict.get("auth_token")
    ct0 = cookies_dict.get("ct0")

    if not auth_token or not ct0:
        raise ValueError(
            "TWITTER_AUTH_TOKEN или TWITTER_CT0 не заданы в .env или cookies.json"
        )

    client.set_cookies(cookies_dict)
    return client


async def server_twitter_login(
    auth_info_1: str,
    password: str,
    auth_info_2: Optional[str] = None,
    code: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Performs server-side login to Twitter/X using twifork.
    Runs from the server's current IP address (e.g. Netherlands VPS).
    Saves resulting cookies into settings.cookies_path (data/cookies.json).
    """
    proxy = settings.twitter_proxy_url if settings.twitter_proxy_url and settings.twitter_proxy_url.strip() else None
    client = Client(language="en-US", proxy=proxy)

    target_cookies_path = Path(settings.cookies_path)
    target_cookies_path.parent.mkdir(parents=True, exist_ok=True)

    async def code_cb(hint: str = "") -> str:
        if code:
            return code.strip()
        raise ValueError("CONFIRMATION_CODE_REQUIRED")

    try:
        await client.login(
            auth_info_1=auth_info_1.strip(),
            auth_info_2=auth_info_2.strip() if auth_info_2 else None,
            password=password,
            cookies_file=str(target_cookies_path),
            code_callback=code_cb if code else None,
        )

        saved_cookies = load_cookies_from_file(target_cookies_path)
        if not saved_cookies:
            try:
                raw_cookies = client.get_cookies()
                if raw_cookies:
                    saved_cookies = raw_cookies
                    with open(target_cookies_path, "w", encoding="utf-8") as f:
                        json.dump(saved_cookies, f, indent=2)
            except Exception:
                pass

        if "auth_token" in saved_cookies:
            settings.twitter_auth_token = saved_cookies["auth_token"]
        if "ct0" in saved_cookies:
            settings.twitter_ct0 = saved_cookies["ct0"]

        return {
            "status": "success",
            "message": "Успешная авторизация в X.com с сервера",
            "cookies_saved": str(target_cookies_path),
            "has_auth_token": bool(saved_cookies.get("auth_token")),
            "has_ct0": bool(saved_cookies.get("ct0")),
        }
    except Exception as e:
        err_msg = str(e)
        if any(x in err_msg for x in ("366", "LoginFlow", "castle_token", "not accessible")):
            safe_print("⚠️ HTTP-вход X.com не поддерживается (LoginFlow 366). Выполняем авторизацию через браузер (Playwright)...")
            try:
                from src.browser_login import playwright_twitter_login
                pw_res = await playwright_twitter_login(
                    auth_info_1=auth_info_1,
                    password=password,
                    auth_info_2=auth_info_2,
                    code=code,
                    proxy_url=proxy,
                    headless=True,
                    target_cookies_path=target_cookies_path,
                )
                if pw_res.get("status") in ("ok", "success"):
                    saved_cookies = load_cookies_from_file(target_cookies_path)
                    if "auth_token" in saved_cookies:
                        settings.twitter_auth_token = saved_cookies["auth_token"]
                    if "ct0" in saved_cookies:
                        settings.twitter_ct0 = saved_cookies["ct0"]
                    return {
                        "status": "success",
                        "message": "Успешная авторизация в X.com через браузер (Playwright) с IP сервера",
                        "cookies_saved": str(target_cookies_path),
                        "has_auth_token": bool(saved_cookies.get("auth_token")),
                        "has_ct0": bool(saved_cookies.get("ct0")),
                    }
                elif pw_res.get("status") == "code_required":
                    return {
                        "status": "challenge_required",
                        "message": pw_res.get("message", "X.com требует 2FA код подтверждения"),
                    }
                else:
                    return {
                        "status": "error",
                        "message": f"Ошибка браузерной авторизации в X.com: {pw_res.get('error', 'Неизвестная ошибка')}",
                    }
            except Exception as pw_err:
                return {
                    "status": "error",
                    "message": f"Ошибка браузерного входа (Playwright): {pw_err}",
                }

        if "CONFIRMATION_CODE_REQUIRED" in err_msg or "code_callback" in err_msg or "confirmation_code" in err_msg.lower() or "challenge" in err_msg.lower():
            return {
                "status": "challenge_required",
                "message": "X.com требует код подтверждения (отправлен на вашу почту или телефон)",
            }
        return {
            "status": "error",
            "message": f"Ошибка авторизации в X.com: {err_msg}",
        }


async def check_tweet_relevance(text: str, relevance_prompt: Optional[str] = None) -> bool:
    """Verifies tweet relevance with Gemini LLM using custom x_relevance_prompt."""
    prompt_tpl = relevance_prompt or getattr(settings, "x_relevance_prompt", "")
    if not prompt_tpl or not prompt_tpl.strip():
        return True

    # In automated test suite, avoid unmocked external Gemini calls unless explicitly enabled
    if os.environ.get("TESTING") == "true" and not os.environ.get("LIVE_AI_TEST"):
        return True

    if not settings.ai_api_key or settings.ai_api_key.startswith("your_"):
        return True

    try:
        from google import genai
        from google.genai import types
        from src.ai.rate_limiter import model_rate_limiter, get_model_interval
        import re

        client = genai.Client(api_key=settings.ai_api_key)
        await model_rate_limiter.acquire(settings.primary_model, min_interval=get_model_interval())
        prompt = f"{prompt_tpl}\n\nТвит: {text}"
        resp = await client.aio.models.generate_content(
            model=settings.primary_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1
            )
        )
        cleaned = (resp.text or "").strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        parsed = json.loads(cleaned)
        return parsed.get("relevant", True) is not False
    except Exception as e:
        safe_print(f"⚠️ Ошибка проверки релевантности твита: {e}")
        return True


async def scout_node(state: AgentState) -> Dict[str, Any]:
    """Scout Agent: Collects live viral AI tweets from X.com using session cookies for active topics."""
    
    # Pre-flight validation
    if os.environ.get("TESTING") != "true" and (not settings.ai_api_key or settings.ai_api_key.startswith("your_")):
        error_msg = "Google Gemini API ключ не настроен или недействителен (AI_API_KEY)."
        safe_print(f"❌ {error_msg}")
        raise ValueError(error_msg)

    active_topics = await get_active_topics()
    
    # Fallback if no topics exist (backward compatibility)
    if not active_topics:
        raw_queries = getattr(settings, "x_search_queries", "") or ""
        qlist = [q.strip() for q in raw_queries.splitlines() if q.strip()]
        if not qlist:
            qlist = ["(AI OR LLM OR 'machine learning') lang:en min_faves:50 -is:retweet"]
        active_topics = [{"id": None, "name": "Default", "search_queries": "\n".join(qlist), "media_type": "any"}]

    client = await get_twitter_client()
    fetched_tweets: List[Dict[str, Any]] = []
    seen_ids = set()

    max_quality = get_max_quality()

    for topic in active_topics:
        topic_id = topic.get("id")
        topic_name = topic.get("name")
        media_type = topic.get("media_type", "any")
        
        raw_queries = topic.get("search_queries", "")
        query_list = [q.strip() for q in raw_queries.splitlines() if q.strip()]
        
        for base_query in query_list:
            # Append media filters based on topic configuration
            query = base_query
            if media_type == "video":
                query += " filter:videos"
            elif media_type == "photo":
                query += " filter:images"
            elif media_type == "photo_video":
                query += " (filter:images OR filter:videos)"

            safe_print(f"🔍 Scout: Поиск по топику '{topic_name}' -> {query}")
            
            search_results = []
            try:
                res = await client.search_tweet(query, product="Top", count=10)
                if not res:
                    res = await client.search_tweet(query, product="Latest", count=10)
                if res:
                    search_results.extend(res)
            except Exception as search_err:
                if hasattr(client, "refresh_transaction") and callable(client.refresh_transaction):
                    try:
                        client.refresh_transaction()
                        res = await client.search_tweet(query, product="Top", count=10)
                        if not res:
                            res = await client.search_tweet(query, product="Latest", count=10)
                        if res:
                            search_results.extend(res)
                    except Exception as retry_err:
                        safe_print(f"❌ Ошибка поиска X.com: {retry_err}")
                else:
                    safe_print(f"❌ Ошибка поиска X.com: {search_err}")

            for tweet in search_results:
                if not tweet:
                    continue

                tweet_id = getattr(tweet, "id", None) if not isinstance(tweet, dict) else tweet.get("id")
                text = extract_tweet_text(tweet)
                if not tweet_id or not text:
                    continue

                tweet_id_str = str(tweet_id)
                if tweet_id_str in seen_ids:
                    continue
                seen_ids.add(tweet_id_str)

                user = getattr(tweet, "user", None) if not isinstance(tweet, dict) else tweet.get("user")
                screen_name = getattr(user, "screen_name", "") if user else ""
                author_handle = f"@{screen_name}" if screen_name else "@unknown"
                author_name = getattr(user, "name", "") if user else screen_name or "Unknown"
                tweet_url = f"https://x.com/{screen_name}/status/{tweet_id}" if screen_name else f"https://x.com/i/status/{tweet_id}"
                
                created_at = parse_tweet_created_at(tweet)
                
                likes_count = safe_int(getattr(tweet, "favorite_count", 0) if not isinstance(tweet, dict) else tweet.get("favorite_count", 0))
                reposts_count = safe_int(getattr(tweet, "retweet_count", 0) if not isinstance(tweet, dict) else tweet.get("retweet_count", 0))
                quotes_count = safe_int(getattr(tweet, "quote_count", 0) if not isinstance(tweet, dict) else tweet.get("quote_count", 0))
                bookmarks_count = safe_int(getattr(tweet, "bookmark_count", 0) if not isinstance(tweet, dict) else tweet.get("bookmark_count", 0))
                impressions_count = safe_int((getattr(tweet, "view_count", None) if not isinstance(tweet, dict) else tweet.get("view_count", None)) or (getattr(tweet, "views_count", None) if not isinstance(tweet, dict) else tweet.get("views_count", None)) or 0)

                media_urls = extract_media_urls(tweet)
                video_info = extract_video_info(tweet)
                
                video_data = None
                if video_info and video_info.get("variants"):
                    best_variant = select_best_variant(video_info["variants"], max_quality)
                    if best_variant:
                        local_path = await download_video(tweet_id_str, best_variant)
                        video_data = {
                            "original_url": best_variant,
                            "thumbnail": video_info.get("thumbnail"),
                            "duration": video_info.get("duration"),
                            "local_path": local_path
                        }

                fetched_tweets.append({
                    "tweet_id": tweet_id_str,
                    "author_handle": author_handle,
                    "author_name": author_name,
                    "text_raw": text,
                    "tweet_url": tweet_url,
                    "created_at": created_at,
                    "likes_count": likes_count,
                    "reposts_count": reposts_count,
                    "quotes_count": quotes_count,
                    "bookmarks_count": bookmarks_count,
                    "impressions_count": impressions_count,
                    "media_urls": media_urls,
                    "topic_id": topic_id,
                    "video_data": video_data,
                })

    if os.environ.get("TESTING") != "true":
        try:
            await revalidate_recent_tweets_metrics()
        except Exception as reval_err:
            safe_print(f"⚠️ Ошибка ревалидации метрик в scout_node: {reval_err}")

    if not fetched_tweets:
        return {"raw_tweets": []}

    new_tweets = await filter_new_tweets(fetched_tweets)
    safe_print(f"✨ Новых постов после дедупликации: {len(new_tweets)}")

    relevant_tweets = []
    if new_tweets:
        topics_by_id = {t["id"]: t for t in active_topics if t.get("id")}
        for tweet in new_tweets:
            t_id = tweet.get("topic_id")
            topic = topics_by_id.get(t_id) if t_id else None
            # If the tweet belongs to a custom topic (multi-stream), it is already targeted by topic search queries.
            # Only apply global relevance check if tweet has no topic or if global relevance prompt is non-empty and there's no custom topic.
            if not getattr(settings, "x_relevance_prompt", "") or not settings.x_relevance_prompt.strip():
                is_rel = True
            elif topic and topic.get("name") and topic.get("name") != "Default":
                is_rel = True
            else:
                is_rel = await check_tweet_relevance(tweet.get("text_raw", ""), settings.x_relevance_prompt)
            if is_rel:
                relevant_tweets.append(tweet)
            else:
                safe_print(f"ℹ️ Твит {tweet['tweet_id']} отклонен AI-фильтром релевантности.")

    await save_raw_tweets(relevant_tweets)
    return {"raw_tweets": relevant_tweets}


async def refresh_tweet_metrics(tweet_ids: List[str]) -> List[Dict[str, Any]]:
    """
    Refreshes metrics for a list of tweet IDs directly from X.com.
    Returns a list of dicts with updated counts.
    """
    if not tweet_ids:
        return []

    try:
        client = await get_twitter_client()
    except Exception as err:
        safe_print(f"⚠️ Scout refresh_tweet_metrics: не удалось подключиться к X: {err}")
        return []

    updated_metrics: List[Dict[str, Any]] = []
    for tid in tweet_ids:
        try:
            tweet = await client.get_tweet_by_id(tid)
            if not tweet:
                continue

            likes = safe_int(
                getattr(tweet, "favorite_count", 0)
                if not isinstance(tweet, dict)
                else tweet.get("favorite_count", 0)
            )
            reposts = safe_int(
                getattr(tweet, "retweet_count", 0)
                if not isinstance(tweet, dict)
                else tweet.get("retweet_count", 0)
            )
            quotes = safe_int(
                getattr(tweet, "quote_count", 0)
                if not isinstance(tweet, dict)
                else tweet.get("quote_count", 0)
            )
            bookmarks = safe_int(
                getattr(tweet, "bookmark_count", 0)
                if not isinstance(tweet, dict)
                else tweet.get("bookmark_count", 0)
            )
            impressions = safe_int(
                (
                    getattr(tweet, "view_count", None)
                    if not isinstance(tweet, dict)
                    else tweet.get("view_count", None)
                )
                or (
                    getattr(tweet, "views_count", None)
                    if not isinstance(tweet, dict)
                    else tweet.get("views_count", None)
                )
                or 0
            )

            updated_metrics.append({
                "tweet_id": str(tid),
                "likes_count": likes,
                "reposts_count": reposts,
                "quotes_count": quotes,
                "bookmarks_count": bookmarks,
                "impressions_count": impressions,
            })
        except Exception as err:
            safe_print(f"⚠️ Ошибка обновления метрик для твита {tid}: {err}")

    return updated_metrics


async def revalidate_recent_tweets_metrics(hours: Optional[int] = None) -> int:
    """
    Revalidates and updates engagement metrics and viral score for recent tweets in the database.
    Checks for metric surges in X and recalculates viral scores.
    """
    try:
        from src.db import get_recent_active_tweets, update_tweet_metrics
        from src.analytics.scorer import calculate_viral_score

        refresh_hours = max(1, int(hours or getattr(settings, "metric_refresh_hours", 48)))
        recent_tweets = await get_recent_active_tweets(hours=refresh_hours)
        if not recent_tweets:
            return 0

        refresh_limit = max(1, int(getattr(settings, "metric_refresh_limit", 20) or 20))
        tweet_ids = [t["tweet_id"] for t in recent_tweets[:refresh_limit]]
        updated_metrics = await refresh_tweet_metrics(tweet_ids)
        if not updated_metrics:
            return 0

        now = datetime.now(timezone.utc)
        metrics_by_id = {m["tweet_id"]: m for m in updated_metrics}
        updated_count = 0

        for tweet in recent_tweets:
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
                new_viral_score = calculate_viral_score(
                    likes=m["likes_count"],
                    reposts=m["reposts_count"],
                    quotes=m["quotes_count"],
                    bookmarks=m["bookmarks_count"],
                    views=m["impressions_count"],
                    age_in_hours=age_hours,
                )
                is_viral = new_viral_score >= settings.min_viral_score

                await update_tweet_metrics(
                    tweet_id=tid,
                    likes=m["likes_count"],
                    reposts=m["reposts_count"],
                    quotes=m["quotes_count"],
                    bookmarks=m["bookmarks_count"],
                    views=m["impressions_count"],
                    viral_score=new_viral_score,
                    is_viral=is_viral,
                )
                updated_count += 1

        if updated_count > 0:
            safe_print(f"🔄 Scout обновил метрики и виральность для {updated_count} постов из БД.")
        return updated_count
    except Exception as err:
        safe_print(f"⚠️ Ошибка при ревалидации метрик недавних твитов: {err}")
        return 0