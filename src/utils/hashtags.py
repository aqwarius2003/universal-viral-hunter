import html
import re
from typing import Any, List, Optional


def normalize_single_hashtag(tag: str) -> Optional[str]:
    """
    Normalizes a single hashtag string:
    - Strips leading '#' and surrounding whitespace.
    - Strips punctuation (commas, semicolons, dots, quotes).
    - Replaces internal spaces and hyphens with '_' so it forms a single valid hashtag.
    - Strips any characters illegal in Telegram hashtags (keeping Unicode letters, digits, and underscores).
    - Prepends '#'.
    """
    if not tag or not isinstance(tag, str):
        return None
    t = tag.strip()
    if not t:
        return None

    # Strip all leading '#' and whitespace
    t = t.lstrip("#").strip()
    if not t:
        return None

    # Strip surrounding punctuation
    t = t.strip(",;:.!?\"'`“”«»[](){}")
    if not t:
        return None

    # Replace internal whitespace and hyphens with '_'
    t = re.sub(r"[\s\-]+", "_", t)

    # Keep only Unicode word characters (letters, digits, underscores)
    t = re.sub(r"[^\w_]", "", t)

    # Strip any dangling underscores from ends
    t = t.strip("_")
    if not t:
        return None

    return f"#{t}"


def _extract_candidates(text: str, is_from_list: bool = False) -> List[str]:
    """
    Extracts raw hashtag candidate strings from text.
    Handles comma/semicolon separation, multiple '#' markers, and whitespace.
    """
    if not text or not isinstance(text, str):
        return []
    trimmed = text.strip()
    if not trimmed:
        return []

    # If text contains commas or semicolons, split by them first
    if "," in trimmed or ";" in trimmed:
        chunks = re.split(r"[,;]+", trimmed)
        res: List[str] = []
        for c in chunks:
            res.extend(_extract_candidates(c, is_from_list=is_from_list))
        return res

    # If text contains multiple hashtags or '#' boundaries (e.g. '#AI #Tech' or '#AI#Tech' or 'AI #Tech')
    if "#" in trimmed:
        parts = [p.strip() for p in re.split(r"(?:^|\s+)#|#", trimmed) if p and p.strip()]
        if len(parts) > 1:
            res: List[str] = []
            for p in parts:
                res.extend(_extract_candidates(p, is_from_list=False))
            return res

    words = [w for w in trimmed.split() if w.strip()]

    # If this is a standalone string (e.g. from user input or DB summary string):
    if not is_from_list:
        return words

    # If a list item has 4 or more space-separated words (e.g. 'DeepSeek ИИ Программирование OpenSource Технологии'),
    # treat each word as an individual tag candidate.
    if len(words) >= 4:
        return words

    # If inside a list item with 1-3 words without delimiters or '#' (e.g. 'кибер безопасность', 'big data analysis'),
    # keep as a single compound tag candidate.
    return [trimmed]


def normalize_hashtags(tags: Any) -> List[str]:
    """
    Normalizes a list, string, tuple, set, or dict of hashtags:
    - Ensures every tag starts strictly with '#'.
    - Handles list of strings, set/tuple, dict, or a single string (space-, comma-, or semicolon-separated).
    - If a list item has 1-3 words (compound phrase like 'кибер безопасность', 'big data analysis'),
      replaces spaces with '_' producing '#кибер_безопасность'.
    - If a list item has 4+ space-separated words (e.g. 'DeepSeek ИИ Программирование OpenSource Технологии'),
      or delimiters (',', ';') or '#', splits them into individual tags.
    - If a single string was wrapped in a 1-element list, splits into individual words if multiple words present.
    - If a dict is passed, extracts candidates from 'hashtags', 'tags', or dict values.
    - Removes case-insensitive duplicates while preserving the original order.
    - Guaranteed: every element in returned list starts with '#'.
    """
    if not tags:
        return []

    if isinstance(tags, dict):
        if "hashtags" in tags and tags["hashtags"]:
            return normalize_hashtags(tags["hashtags"])
        if "tags" in tags and tags["tags"]:
            return normalize_hashtags(tags["tags"])
        if "hash_tags" in tags and tags["hash_tags"]:
            return normalize_hashtags(tags["hash_tags"])
        return normalize_hashtags(list(tags.values()))

    raw_candidates: List[str] = []
    if isinstance(tags, str):
        raw_candidates.extend(_extract_candidates(tags, is_from_list=False))
    elif isinstance(tags, (list, tuple, set)):
        items_list = list(tags)
        is_single_item = len(items_list) == 1
        for item in items_list:
            if not isinstance(item, str):
                continue
            words = [w for w in item.strip().split() if w.strip()]
            if is_single_item and len(words) > 1 and not item.strip().startswith("#"):
                raw_candidates.extend(_extract_candidates(item, is_from_list=False))
            elif len(words) >= 4:
                raw_candidates.extend(_extract_candidates(item, is_from_list=False))
            else:
                raw_candidates.extend(_extract_candidates(item, is_from_list=True))
    else:
        return []

    result: List[str] = []
    seen = set()
    for raw in raw_candidates:
        norm = normalize_single_hashtag(raw)
        if norm:
            norm_lower = norm.lower()
            if norm_lower not in seen:
                seen.add(norm_lower)
                result.append(norm)

    return result


def ensure_all_hashtags_have_hash(hashtags_text: str) -> str:
    """
    Guarantees that every token in a hashtags line starts strictly with '#' and is formatted properly.
    E.g. "DeepSeek ИИ Программирование OpenSource Технологии"
    -> "#DeepSeek #ИИ #Программирование #OpenSource #Технологии"
    Handles mixed strings like "#DeepSeek ИИ #Программирование OpenSource #Технологии".
    """
    if not hashtags_text or not isinstance(hashtags_text, str) or not hashtags_text.strip():
        return ""
    normalized = normalize_hashtags(hashtags_text)
    if not normalized:
        return ""
    return " ".join(html.escape(h) for h in normalized)


def enforce_hashtags_in_post(post_text: str) -> str:
    """
    Final defense assertion/regex pass:
    Scans the message. If the message ends with a line or block of hashtags,
    guarantees that every tag in that line starts with '#'.
    If any word in the hashtag block is missing '#', automatically prepends '#'.
    Leaves regular sentences, truncated summaries with ellipsis, links, and bullet points intact.
    """
    if not post_text or not isinstance(post_text, str) or not post_text.strip():
        return post_text

    stripped = post_text.rstrip()
    lines = stripped.split("\n")
    if not lines:
        return post_text

    last_line = lines[-1].strip()
    if not last_line:
        return post_text

    # 1. Must not be HTML tag, bullet, emoji prefix, or heading
    if last_line.startswith(("<", "•", "-", "*", "𝕏", "📊", "💡", "🌟", "🔥", "⚡")):
        return post_text

    # 2. Must not end with sentence punctuation or ellipsis
    if last_line.endswith((".", "!", "?", ":", ";", "…", ",", "'", "\"", "”", "»")):
        return post_text

    # 3. Must not contain HTML tags or HTML entities (like &amp;, &#x27;, &quot;, <...>)
    if re.search(r"&(?:[a-zA-Z]+|#x?[0-9a-fA-F]+);", last_line) or "<" in last_line or ">" in last_line:
        return post_text

    # Check Case A: The line starts with tag prefix like 'Теги:' or 'Хэштеги:' or 'Tags:'
    tag_prefix_match = re.match(r"^(?:теги|хэштеги|tags|hashtags)\s*:\s*(.+)$", last_line, re.IGNORECASE)
    if tag_prefix_match:
        tag_str = tag_prefix_match.group(1).strip()
        fixed_line = ensure_all_hashtags_have_hash(tag_str)
        if fixed_line:
            lines[-1] = fixed_line
            return "\n".join(lines)

    # Check Case B: The line starts with '#' followed by a word character (e.g. #AI, #DeepSeek)
    if re.match(r"^#[\wА-Яа-яA-Za-z]", last_line):
        fixed_line = ensure_all_hashtags_have_hash(last_line)
        if fixed_line and fixed_line != last_line:
            lines[-1] = fixed_line
            return "\n".join(lines)

    # Check Case C: Standalone block of unhashed tags at the very end of post.
    # Must be preceded by an empty line (a separate paragraph at the end),
    # have 1 to 12 words, where each word is an identifier without sentence punctuation.
    if len(lines) >= 2 and lines[-2].strip() == "":
        words = last_line.split()
        if 1 <= len(words) <= 12 and all(re.match(r"^#?[A-Za-zА-Яа-я0-9_\-]+$", w) for w in words):
            # Check that it doesn't contain common lowercase grammatical Russian/English stop words
            stopwords = {
                "и", "в", "во", "на", "с", "со", "по", "из", "к", "ко", "о", "об", "от", "для",
                "не", "что", "это", "как", "за", "до", "the", "a", "an", "is", "are", "of", "to", "in", "and", "or", "for"
            }
            if not any(w.lower() in stopwords for w in words):
                fixed_line = ensure_all_hashtags_have_hash(last_line)
                if fixed_line:
                    lines[-1] = fixed_line
                    return "\n".join(lines)

    return post_text


def extract_trailing_tags_from_summary(summary: str) -> tuple[str, List[str]]:
    """
    If the summary ends with a line of hashtags or tag keywords (e.g. 'Теги: DeepSeek ИИ'
    or 'DeepSeek ИИ Программирование OpenSource Технологии'), strips that line from summary
    and returns (clean_summary, list_of_normalized_tags).
    """
    if not summary or not isinstance(summary, str) or not summary.strip():
        return summary, []

    lines = summary.strip().split("\n")
    last_line = lines[-1].strip()
    if not last_line:
        return summary, []

    # Check if last_line starts with 'Теги:' or 'Хэштеги:' or 'Tags:'
    tag_prefix_match = re.match(r"^(?:теги|хэштеги|tags|hashtags)\s*:\s*(.+)$", last_line, re.IGNORECASE)
    if tag_prefix_match:
        tag_str = tag_prefix_match.group(1).strip()
        tags = normalize_hashtags(tag_str)
        if tags:
            clean_summary = "\n".join(lines[:-1]).rstrip()
            return clean_summary, tags

    # Check if last_line starts with '#' and a word char
    if re.match(r"^#[\wА-Яа-яA-Za-z]", last_line):
        tags = normalize_hashtags(last_line)
        if tags:
            clean_summary = "\n".join(lines[:-1]).rstrip()
            return clean_summary, tags

    # Check if last_line is a standalone line of capitalized words without punctuation at the end,
    # separated from the rest of the text by an empty line
    if len(lines) >= 2 and lines[-2].strip() == "":
        if not last_line.endswith((".", "!", "?", ":", ";", "…", ",", "'", "\"")):
            words = last_line.split()
            if 2 <= len(words) <= 12 and all(re.match(r"^#?[A-Za-zА-Яа-я0-9_\-]+$", w) for w in words):
                stopwords = {
                    "и", "в", "во", "на", "с", "со", "по", "из", "к", "ко", "о", "об", "от", "для",
                    "не", "что", "это", "как", "за", "до", "the", "a", "an", "is", "are", "of", "to", "in", "and", "or", "for"
                }
                if not any(w.lower() in stopwords for w in words):
                    tags = normalize_hashtags(last_line)
                    if tags and len(tags) >= 2:
                        clean_summary = "\n".join(lines[:-1]).rstrip()
                        return clean_summary, tags

    return summary, []


def format_hashtags_line(tags: Any, max_count: Optional[int] = None) -> str:
    """
    Normalizes hashtags, safely escapes HTML characters, and joins them with spaces.
    If max_count is specified, truncates to at most max_count hashtags.
    """
    normalized = normalize_hashtags(tags)
    if max_count is not None and max_count > 0:
        normalized = normalized[:max_count]
    return " ".join(html.escape(h) for h in normalized)
