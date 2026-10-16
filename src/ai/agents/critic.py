from typing import Any, Dict
from src.ai.agents.state import AgentState
from src.config import settings

async def critic_node(state: AgentState) -> Dict[str, Any]:
    """
    Critic Agent: Validates the AI drafts against configurable rules.
    If validation fails, adds errors to state to trigger a regeneration loop.
    """
    draft_posts = state.get("draft_posts", [])
    errors = []
    
    max_title_words = getattr(settings, "max_title_words", 25)
    show_takeaways = getattr(settings, "show_metric_takeaways", False)

    for draft in draft_posts:
        ai_data = draft["ai_data"]
        tweet_id = draft["tweet_id"]
        
        # Rule 1: Title max words (configurable, default 25)
        title_words = ai_data.get("title_ru", "").split()
        if len(title_words) > max_title_words:
            errors.append(f"Tweet {tweet_id}: Title exceeds {max_title_words} words ({len(title_words)} words).")
            
        # Rule 2: Takeaways check (only if enabled in settings)
        if show_takeaways:
            takeaways = ai_data.get("takeaways", [])
            if not isinstance(takeaways, list) or len(takeaways) == 0:
                errors.append(f"Tweet {tweet_id}: Must have at least 1 takeaway when takeaways are enabled.")
            
    # LangGraph uses operator.add for Annotated lists, so returning a list appends it
    return {"validation_errors": errors}

def should_publish(state: AgentState) -> str:
    """
    Routing logic: if errors exist, go back to Editor. Otherwise, go to Publisher.
    """
    if state.get("validation_errors"):
        return "editor"
    return "publisher"