from datetime import datetime, timedelta, timezone
from typing import Optional, Any
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    ai_api_key: Optional[str] = None
    ai_model_name: str = "gemini-3.5-flash-lite, gemini-3.1-flash-lite"
    ai_fallback_model_name: str = "gemini-3.1-flash-lite"
    telegram_bot_token: Optional[str] = None
    telegram_channel_id: Optional[str] = None
    telegram_admin_id: Optional[str] = None
    approval_required: bool = False
    min_viral_score: float = 75.0
    super_viral_threshold: float = 100.0
    db_path: str = "tweets.sqlite"
    
    # Twitter / X credentials & proxy
    twitter_username: Optional[str] = None
    twitter_email: Optional[str] = None
    twitter_password: Optional[str] = None
    twitter_auth_token: Optional[str] = None
    twitter_ct0: Optional[str] = None
    twitter_proxy_url: Optional[str] = None
    cookies_path: str = "data/cookies.json"

    # Security & Sessions
    secret_key: str = "viral-hunter-secret-key-change-me"
    auth_enabled: bool = True

    # Scheduler, AI Rate Limit & Publishing limits
    ai_request_delay_seconds: int = 4
    scheduler_interval_minutes: Optional[int] = None
    max_posts_per_run: int = 1
    daily_digest_enabled: Optional[bool] = None
    daily_digest_time: Optional[str] = None

    # Quiet hours / Night sleep mode & Digest intervals
    quiet_hours_enabled: Optional[bool] = None
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None
    digest_pre_sleep_minutes: Optional[int] = None
    digest_block_interval_minutes: Optional[int] = None
    daytime_publish_interval_minutes: Optional[int] = None
    app_timezone: Optional[str] = None

    # Dynamic Parsing Settings (Zero-State/Presets)
    x_search_queries: str = "min_faves:50, lang:en"
    x_relevance_prompt: str = "Оцени релевантность твита для IT-специалистов. Ответь только JSON: {\"relevant\": true, \"reason\": \"...\"}"
    scout_interval_minutes: Optional[int] = None
    metric_refresh_hours: int = 48
    metric_refresh_limit: int = 20
    editor_system_prompt: str = "You are an expert content editor and media analyst. Create a strict JSON output matching the schema. Output all JSON string values strictly in Russian. All hashtags must strictly start with '#' (e.g. #Новости #Видео #Происшествия). No conversational filler."
    editor_prompt_template: str = "Analyze this tweet: {text}\nAuthor: {author}\nDate: {date}\nMedia count: {media_count}\n\nGenerate Russian title, summary, key takeaways (or empty list if not needed), impact score (1-10), impact reason, and hashtags (starting with #)."
    max_digest_items: int = 10
    queue_sort_strategy: str = "BY_VIRAL_SCORE"
    backlog_digest_enabled: bool = True
    backlog_max_chunk_size: int = 6

    # Web Panel UI Flags (Telegram Metrics & Elements)
    show_metric_likes: bool = True
    show_metric_reposts: bool = True
    show_metric_views: bool = True
    show_metric_impact: bool = True
    show_metric_author: bool = True
    show_metric_hashtags: bool = True
    show_metric_source_btn: bool = True
    show_metric_takeaways: bool = False

    # Advanced Customizable Limits & AI Parameters
    max_title_words: int = 25
    telegram_max_caption_len: int = 1024
    telegram_max_text_len: int = 4096
    ai_temperature: float = 0.3
    max_hashtags: int = 5
    scout_search_count: int = 10

    @property
    def primary_model(self) -> str:
        """Returns primary text model parsed from ai_model_name or default gemini-3.5-flash-lite."""
        if self.ai_model_name:
            parts = [p.strip() for p in str(self.ai_model_name).split(",") if p.strip()]
            if parts:
                return parts[0]
        return "gemini-3.5-flash-lite"

    @property
    def fallback_model(self) -> str:
        """Returns fallback/multimodal model parsed from ai_model_name or default gemini-3.1-flash-lite."""
        if self.ai_model_name:
            parts = [p.strip() for p in str(self.ai_model_name).split(",") if p.strip()]
            if len(parts) >= 2:
                return parts[1]
            if len(parts) == 1 and parts[0] == "gemini-3.1-flash-lite":
                return "gemini-3.5-flash-lite"
        return self.ai_fallback_model_name or "gemini-3.1-flash-lite"

    def is_configured(self) -> bool:
        """Checks if the minimal required API keys are configured."""
        has_ai = bool(self.ai_api_key and self.ai_api_key.strip() and not self.ai_api_key.startswith("your_"))
        has_tg = bool(self.telegram_bot_token and self.telegram_bot_token.strip() and not self.telegram_bot_token.startswith("your_"))
        has_channel = bool(self.telegram_channel_id and self.telegram_channel_id.strip() and not self.telegram_channel_id.startswith("your_"))
        return has_ai and has_tg and has_channel

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()


def get_app_timezone(tz_offset_minutes: Optional[int] = None) -> Any:
    """Returns the target application timezone based on client offset, settings or host local timezone."""
    if tz_offset_minutes is not None:
        try:
            return timezone(timedelta(minutes=-int(tz_offset_minutes)))
        except Exception:
            pass

    app_tz = getattr(settings, "app_timezone", None)
    if app_tz:
        tz_name = str(app_tz).strip()
        if tz_name.lower() not in ("auto", "local", ""):
            import re
            offset_match = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?", tz_name, re.IGNORECASE)
            if offset_match:
                hours = int(offset_match.group(2))
                minutes = int(offset_match.group(3) or 0)
                if hours <= 14 and minutes < 60 and not (hours == 14 and minutes > 0):
                    total_minutes = hours * 60 + minutes
                    sign = 1 if offset_match.group(1) == "+" else -1
                    return timezone(sign * timedelta(minutes=total_minutes))
            try:
                import zoneinfo
                return zoneinfo.ZoneInfo(tz_name)
            except Exception:
                try:
                    if tz_name.startswith(("+", "-")) and ":" in tz_name:
                        sign = 1 if tz_name[0] == "+" else -1
                        h, m = map(int, tz_name[1:].split(":"))
                        return timezone(sign * timedelta(hours=h, minutes=m))
                except Exception:
                    pass

    local_tz = datetime.now().astimezone().tzinfo
    return local_tz if local_tz is not None else timezone.utc


def is_in_quiet_hours(
    dt: Optional[datetime] = None,
    enabled: Optional[bool] = None,
    start_str: Optional[str] = None,
    end_str: Optional[str] = None,
    tz_offset_minutes: Optional[int] = None,
) -> bool:
    """Checks if dt (or current local time) falls within the quiet hours window."""
    is_enabled = settings.quiet_hours_enabled if enabled is None else enabled
    if not is_enabled:
        return False
    if dt is None:
        tz = get_app_timezone(tz_offset_minutes)
        dt = datetime.now(tz)

    start_str = start_str or settings.quiet_hours_start
    end_str = end_str or settings.quiet_hours_end

    try:
        sh, sm = map(int, start_str.strip().split(":"))
        eh, em = map(int, end_str.strip().split(":"))
    except Exception:
        sh, sm = 22, 0
        eh, em = 7, 0

    current_minutes = dt.hour * 60 + dt.minute
    start_minutes = sh * 60 + sm
    end_minutes = eh * 60 + em

    if start_minutes == end_minutes:
        return False
    elif start_minutes > end_minutes:
        # Crosses midnight: e.g. 22:00 to 07:00
        return current_minutes >= start_minutes or current_minutes < end_minutes
    else:
        # Same day: e.g. 01:00 to 06:00
        return start_minutes <= current_minutes < end_minutes


def adjust_for_quiet_hours(
    dt: datetime,
    enabled: Optional[bool] = None,
    start_str: Optional[str] = None,
    end_str: Optional[str] = None,
) -> datetime:
    """If dt falls into quiet hours, shifts dt forward to the end of quiet hours."""
    is_enabled = settings.quiet_hours_enabled if enabled is None else enabled
    if not is_enabled:
        return dt
    start_str = start_str or settings.quiet_hours_start
    end_str = end_str or settings.quiet_hours_end

    try:
        sh, sm = map(int, start_str.strip().split(":"))
        eh, em = map(int, end_str.strip().split(":"))
    except Exception:
        sh, sm = 22, 0
        eh, em = 7, 0

    current_minutes = dt.hour * 60 + dt.minute
    start_minutes = sh * 60 + sm
    end_minutes = eh * 60 + em

    if start_minutes == end_minutes:
        return dt

    if start_minutes > end_minutes:
        # Crosses midnight: e.g. 22:00 to 07:00
        if current_minutes >= start_minutes:
            next_day = dt + timedelta(days=1)
            return next_day.replace(hour=eh, minute=em, second=0, microsecond=0)
        elif current_minutes < end_minutes:
            return dt.replace(hour=eh, minute=em, second=0, microsecond=0)
    else:
        # Same day: e.g. 01:00 to 06:00
        if start_minutes <= current_minutes < end_minutes:
            return dt.replace(hour=eh, minute=em, second=0, microsecond=0)

    return dt
