"""
Technical Audit Gateway for Viral Hunter.
Ensures every post strictly conforms to Telegram API requirements
BEFORE being displayed to the editor or scheduled for publishing:
- Strictly enforces 1024 character limit for photo/video captions.
- Strictly enforces 4096 character limit for text posts.
- Auto-closes and balances HTML tags (b, i, code, a, blockquote).
- Normalizes hashtags and formatting.
"""
import html
import re
from typing import Any, Dict, List, Optional, Tuple

TELEGRAM_MAX_CAPTION_LEN = 1024
TELEGRAM_MAX_TEXT_LEN = 4096
CAPTION_SAFETY_MARGIN = 20  # Keep within 1004 characters to leave headroom


def balance_and_sanitize_html(text: str) -> str:
    """
    Validates and balances supported Telegram HTML tags.
    Ensures that any opened tag (<b>, <i>, <code>, <a>, <blockquote>)
    is properly closed in reverse order without orphan closing tags.
    Escapes standalone unescaped '<' and '>' characters.
    """
    if not text:
        return ""

    allowed_tags = {"b", "strong", "i", "em", "u", "s", "strike", "del", "code", "pre", "blockquote", "tg-spoiler"}

    # Pattern to match valid HTML tags: <tag>, </tag>, <a href="...">
    tag_pattern = re.compile(r'(</?([a-zA-Z0-9\-_]+)(?:\s+[^>]*)?>)')

    parts = []
    open_stack: List[str] = []
    last_idx = 0

    for match in tag_pattern.finditer(text):
        start, end = match.span()
        tag_full = match.group(1)
        tag_name = match.group(2).lower()
        is_closing = tag_full.startswith("</")

        # Process text before this tag
        raw_before = text[last_idx:start]
        # Escape stray angle brackets in raw text
        raw_escaped = raw_before.replace("<", "&lt;").replace(">", "&gt;")
        parts.append(raw_escaped)
        last_idx = end

        if tag_name == "a":
            if is_closing:
                if "a" in open_stack:
                    # Pop until 'a'
                    while open_stack:
                        top = open_stack.pop()
                        parts.append(f"</{top}>")
                        if top == "a":
                            break
            else:
                parts.append(tag_full)
                open_stack.append("a")
        elif tag_name in allowed_tags:
            if is_closing:
                if tag_name in open_stack:
                    # Close in reverse order
                    while open_stack:
                        top = open_stack.pop()
                        parts.append(f"</{top}>")
                        if top == tag_name:
                            break
            else:
                parts.append(f"<{tag_name}>")
                open_stack.append(tag_name)
        else:
            # Unsupported tag - escape it as plain text
            parts.append(html.escape(tag_full))

    # Remaining text after last tag
    raw_tail = text[last_idx:].replace("<", "&lt;").replace(">", "&gt;")
    parts.append(raw_tail)

    # Close any remaining unclosed tags in LIFO order
    while open_stack:
        unclosed = open_stack.pop()
        parts.append(f"</{unclosed}>")

    return "".join(parts)


def format_hashtags(hashtags: Any) -> List[str]:
    """Ensures hashtags are clean, unique, and strictly start with '#'."""
    if not hashtags:
        return []
    if isinstance(hashtags, str):
        raw_items = hashtags.replace(",", " ").split()
    elif isinstance(hashtags, (list, tuple, set)):
        raw_items = []
        for x in hashtags:
            raw_items.extend(str(x).replace(",", " ").split())
    else:
        return []

    cleaned = []
    for item in raw_items:
        h = str(item).strip()
        if not h:
            continue
        if not h.startswith("#"):
            h = f"#{h}"
        # Keep letters, numbers, underscores
        h = re.sub(r"[^\w#]", "", h)
        if len(h) > 1 and h not in cleaned:
            cleaned.append(h)
    return cleaned


def audit_and_prepare_telegram_content(
    title: str,
    summary: str,
    takeaways: Optional[List[str]] = None,
    hashtags: Optional[List[str]] = None,
    is_media: bool = False,
    author_handle: Optional[str] = None,
    tweet_url: Optional[str] = None,
    metrics_str: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Performs full technical audit on post content.
    Guarantees that resulting content fits Telegram limits:
    - is_media=True -> <= 1024 characters (photo/video caption)
    - is_media=False -> <= 4096 characters (text message)
    Safely compacts content if required, preserving valid HTML tags.
    """
    clean_title = (title or "").strip()
    clean_summary = (summary or "").strip()
    clean_takeaways = [t.strip() for t in (takeaways or []) if str(t).strip()]
    clean_hashtags = format_hashtags(hashtags)
    warnings: List[str] = []

    try:
        from src.config import settings
        max_caption_setting = getattr(settings, "telegram_max_caption_len", TELEGRAM_MAX_CAPTION_LEN)
        max_text_setting = getattr(settings, "telegram_max_text_len", TELEGRAM_MAX_TEXT_LEN)
        show_takeaways = getattr(settings, "show_metric_takeaways", False)
        max_hashtags_setting = getattr(settings, "max_hashtags", 5)
    except Exception:
        max_caption_setting = TELEGRAM_MAX_CAPTION_LEN
        max_text_setting = TELEGRAM_MAX_TEXT_LEN
        show_takeaways = False
        max_hashtags_setting = 5

    if not show_takeaways:
        clean_takeaways = []
    if max_hashtags_setting and len(clean_hashtags) > max_hashtags_setting:
        clean_hashtags = clean_hashtags[:max_hashtags_setting]

    max_allowed = max_caption_setting if is_media else max_text_setting
    target_limit = (max_allowed - CAPTION_SAFETY_MARGIN) if is_media else max_allowed

    def build_candidate(s_text: str, t_list: List[str], h_list: List[str]) -> str:
        parts = []
        if clean_title:
            parts.append(f"<b>{clean_title}</b>\n")

        mid = []
        if author_handle:
            clean_handle = author_handle.lstrip("@")
            url = tweet_url or f"https://x.com/{clean_handle}"
            mid.append(f'𝕏 <a href="{url}">@{clean_handle}</a>')
        if metrics_str:
            mid.append(metrics_str)
        if mid:
            parts.append("\n".join(mid) + "\n")

        parts.append(s_text)

        if t_list:
            t_bullets = "\n".join([f"• {t}" for t in t_list])
            parts.append(f"\n💡 <b>Тезисы:</b>\n{t_bullets}")

        if h_list:
            h_line = " ".join(h_list)
            parts.append(f"\n{h_line}")

        raw_post = "\n".join(parts)
        return balance_and_sanitize_html(raw_post)

    candidate = build_candidate(clean_summary, clean_takeaways, clean_hashtags)

    # If already fits within limit, we are done
    if len(candidate) <= target_limit:
        return {
            "is_valid": True,
            "formatted_text": candidate,
            "title": clean_title,
            "summary": clean_summary,
            "title_ru": clean_title,
            "summary_ru": clean_summary,
            "takeaways": clean_takeaways,
            "hashtags": clean_hashtags,
            "char_count": len(candidate),
            "max_limit": max_allowed,
            "is_media": is_media,
            "warnings": warnings,
        }

    # Content exceeds limit -> perform smart adaptive compaction
    warnings.append(f"Исходная длина ({len(candidate)} симв.) превысила лимит {target_limit}. Проведена оптимизация.")

    # Step 1: Reduce takeaways to at most 2, then 1, then 0
    adj_takeaways = list(clean_takeaways)
    while len(adj_takeaways) > 0 and len(candidate) > target_limit:
        adj_takeaways.pop()
        candidate = build_candidate(clean_summary, adj_takeaways, clean_hashtags)

    # Step 2: Reduce hashtags to top 3 if still exceeding
    adj_hashtags = list(clean_hashtags)
    if len(candidate) > target_limit and len(adj_hashtags) > 3:
        adj_hashtags = adj_hashtags[:3]
        candidate = build_candidate(clean_summary, adj_takeaways, adj_hashtags)

    # Step 3: Trim summary if still exceeding
    adj_summary = clean_summary
    if len(candidate) > target_limit:
        overage = len(candidate) - target_limit + 10
        trim_len = max(50, len(adj_summary) - overage)
        adj_summary = adj_summary[:trim_len].rstrip() + "..."
        candidate = build_candidate(adj_summary, adj_takeaways, adj_hashtags)

    return {
        "is_valid": True,
        "formatted_text": candidate,
        "title": clean_title,
        "summary": adj_summary,
        "title_ru": clean_title,
        "summary_ru": adj_summary,
        "takeaways": adj_takeaways,
        "hashtags": adj_hashtags,
        "char_count": len(candidate),
        "max_limit": max_allowed,
        "is_media": is_media,
        "warnings": warnings,
    }
