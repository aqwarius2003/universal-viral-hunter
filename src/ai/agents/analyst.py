from typing import Any, Dict
from datetime import datetime, timezone
from src.ai.agents.state import AgentState
from src.analytics.scorer import calculate_viral_score
from src.config import settings
from src.db import update_tweet_status_and_score

async def analyst_node(state: AgentState) -> Dict[str, Any]:
    """
    Analyst Agent: Calculates the Viral Score for raw tweets.
    Filters out non-viral tweets and passes the viral ones forward.
    """
    raw_tweets = state.get("raw_tweets", [])
    viral_tweets = []
    
    # Use timezone-aware UTC datetime for current time
    now = datetime.now(timezone.utc)
    
    for tweet in raw_tweets:
        # 1. Calculate age in hours
        try:
            created_at = datetime.fromisoformat(tweet["created_at"])
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
        except Exception:
            created_at = now

        age_delta = now - created_at
        age_in_hours = max(age_delta.total_seconds() / 3600.0, 1.0)
        
        # 2. Calculate Normalized Viral Score (NVS)
        score = calculate_viral_score(
            likes=tweet.get("likes_count", 0),
            reposts=tweet.get("reposts_count", 0),
            quotes=tweet.get("quotes_count", 0),
            bookmarks=tweet.get("bookmarks_count", 0),
            views=tweet.get("impressions_count", 0),
            age_in_hours=age_in_hours
        )
        
        tweet["viral_score"] = score
        
        # 3. Filter based on threshold and update DB
        if score >= settings.min_viral_score:
            tweet["is_viral"] = True
            tweet["status"] = "PROCESSED"
            viral_tweets.append(tweet)
            await update_tweet_status_and_score(
                tweet["tweet_id"], score, True, "PROCESSED"
            )
        else:
            tweet["is_viral"] = False
            tweet["status"] = "IGNORED"
            await update_tweet_status_and_score(
                tweet["tweet_id"], score, False, "IGNORED"
            )
            
    # Sort viral tweets descending by viral score
    viral_tweets.sort(key=lambda t: t.get("viral_score", 0.0), reverse=True)

    # Return ONLY the updated state field
    return {"viral_tweets": viral_tweets}