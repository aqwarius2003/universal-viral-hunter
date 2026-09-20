# Skill: Standard LangGraph Node Template
Description: Use this template whenever generating a new computational node for the LangGraph workflow.

## Template:
```python
from typing import dict, Any
from src.ai.agents.state import AgentState

def sample_node(state: AgentState) -> dict[str, Any]:
    """
    Brief description of what this node does.
    It reads from state and returns a dict with updates.
    """
    # 1. Read necessary data from state
    current_data = state.get("some_key", [])
    
    # 2. Perform business logic
    processed_data = [item for item in current_data if item]
    
    # 3. Return ONLY the state updates (LangGraph merges this automatically)
    return {"some_key": processed_data}