"""LangGraph StateGraph for the QA Testing Agent."""

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from agent.state import AgentState
from agent.nodes.classifier import classify_intent
from agent.nodes.planner import plan_test
from agent.nodes.executor import execute_step
from agent.nodes.reporter import generate_report
from agent.nodes.generator import generate_test_cases


def route_intent(state: AgentState) -> str:
    """Route based on detected intent."""
    intent = state.get("intent")

    if intent == "generate_test_cases":
        return "generator"
    elif intent == "execute_test":
        return "planner"
    elif intent == "analyze_results":
        return "reporter"
    else:
        # Default to planner for execute intent
        return "planner"


def should_execute_or_clarify(state: AgentState) -> str:
    """Check if we need clarification before executing."""
    test_plan = state.get("test_plan", {})

    # Check for placeholders in the test plan (indicates missing info)
    steps = test_plan.get("steps", [])
    for step in steps:
        target = step.get("target") or ""
        value = step.get("value") or ""
        description = step.get("description") or ""
        # Look for placeholder patterns like {BUTTON_NAME}
        if "{" in target or "{" in value or "{" in description:
            return "end"  # Skip execution, return to user for clarification

    return "executor"


def should_continue_execution(state: AgentState) -> str:
    """Check if there are more steps to execute."""
    test_plan = state.get("test_plan")
    current_step = state.get("current_step", 0)

    if test_plan and current_step < len(test_plan.get("steps", [])):
        return "executor"
    else:
        return "reporter"


# Build the graph
builder = StateGraph(AgentState)

# Add nodes
builder.add_node("classifier", classify_intent)
builder.add_node("planner", plan_test)
builder.add_node("executor", execute_step)
builder.add_node("reporter", generate_report)
builder.add_node("generator", generate_test_cases)

# Add edges
builder.add_edge(START, "classifier")
builder.add_conditional_edges("classifier", route_intent)
builder.add_conditional_edges("planner", should_execute_or_clarify, {
    "executor": "executor",
    "end": END,
})
builder.add_conditional_edges("executor", should_continue_execution)
builder.add_edge("generator", END)
builder.add_edge("reporter", END)

# Compile with checkpointer for persistence
checkpointer = MemorySaver()
graph = builder.compile(checkpointer=checkpointer)
