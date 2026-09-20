from typing import Any, Dict
from src.ai.agents.state import AgentState

async def critic_node(state: AgentState) -> Dict[str, Any]:
    """
    Critic Agent: Validates the AI drafts against strict rules.
    If validation fails, adds errors to state to trigger a regeneration loop.
    """
    draft_posts = state.get("draft_posts", [])
    errors = []
    
    for draft in draft_posts:
        ai_data = draft["ai_data"]
        tweet_id = draft["tweet_id"]
        
        # Rule 1: Title max 10 words
        title_words = ai_data.get("title_ru", "").split()
        if len(title_words) > 10:
            errors.append(f"Tweet {tweet_id}: Title exceeds 10 words.")
            
        # Rule 2: Exactly 3 takeaways
        takeaways = ai_data.get("takeaways", [])
        if len(takeaways) != 3:
            errors.append(f"Tweet {tweet_id}: Must have exactly 3 takeaways.")
            
    # LangGraph uses operator.add for Annotated lists, so returning a list appends it
    return {"validation_errors": errors}

def should_publish(state: AgentState) -> str:
    """
    Routing logic: if errors exist, go back to Editor. Otherwise, go to Publisher.
    """
    if state.get("validation_errors"):
        return "editor"
    return "publisher"