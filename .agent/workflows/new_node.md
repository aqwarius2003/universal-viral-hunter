# Workflow: Create a new LangGraph Node
Trigger: User asks to create a new agent/node for the pipeline.

## Step 1: State Verification
- Read `src/ai/agents/state.py`.
- Ensure the `AgentState` TypedDict contains the necessary fields for this new node's input and output.
- If fields are missing, prompt the user for permission to update `state.py`.

## Step 2: Test-Driven Development (TDD)
- Create a test file in `tests/` (e.g., `tests/test_scout_node.py`).
- Write a failing test passing a mock `AgentState` to the proposed node function and asserting the expected state modification.

## Step 3: Implementation
- Create the node file in `src/ai/agents/` (e.g., `scout.py`).
- Implement the node strictly as a pure function: `def my_node(state: AgentState) -> dict:`.
- Return only the dictionary of state updates, not the full state.

## Step 4: Verification
- Run `poetry run pytest`.
- Apply Inner Loop (Healer Agent) from `GEMENI.md` if tests fail.

## Step 5: Graph Integration
- Inform the user that the node is ready.
- Ask if it should be wired into the main graph in `src/main.py`.