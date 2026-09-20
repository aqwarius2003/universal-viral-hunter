import math

def calculate_viral_score(
    likes: int,
    reposts: int,
    quotes: int,
    bookmarks: int,
    views: int,
    age_in_hours: float,
) -> float:
    """
    Calculates the Normalized Viral Score (NVS) based on engagement metrics and age.
    """
    try:
        safe_views = max(int(views or 0), 100)
    except (ValueError, TypeError):
        safe_views = 100

    try:
        safe_age = max(float(age_in_hours or 1.0), 1.0)
    except (ValueError, TypeError):
        safe_age = 1.0

    try:
        l = int(likes or 0)
        r = int(reposts or 0)
        q = int(quotes or 0)
        b = int(bookmarks or 0)
    except (ValueError, TypeError):
        l = r = q = b = 0

    raw_score = (
        (l * 1.0) +
        (r * 2.5) +
        (q * 3.0) +
        (b * 3.5)
    ) / math.log10(safe_views)

    decay_factor = 1.0 / math.sqrt(safe_age)
    
    normalized_score = raw_score * decay_factor
    return round(normalized_score, 2)