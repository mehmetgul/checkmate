"""Report generation node - summarizes test results."""

from langchain_core.prompts import ChatPromptTemplate
from agent.llm import get_llm
from langchain_core.messages import AIMessage

from agent.state import AgentState


REPORTER_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """You are a QA test reporter. Summarize the test execution results in a clear, concise way.

Test Plan: {test_plan}
Results: {results}

Provide:
1. Overall status (PASSED/FAILED)
2. Summary of what was tested
3. Step-by-step results
4. Any errors encountered
5. Recommendations if the test failed

Be conversational and helpful."""),
    ("human", "Generate the test report.")
])


async def generate_report(state: AgentState) -> dict:
    """Generate a summary report of test execution."""
    model = get_llm("fast")

    test_plan = state.get("test_plan", {})
    test_results = state.get("test_results", [])

    # Calculate overall status
    failed_steps = [r for r in test_results if r.get("status") == "failed"]
    overall_status = "failed" if failed_steps else "passed"

    # Format results for the prompt
    formatted_results = []
    for result in test_results:
        step_num = result.get("step_number", 0)
        step_info = test_plan.get("steps", [])[step_num] if step_num < len(test_plan.get("steps", [])) else {}
        formatted_results.append({
            "step": step_num + 1,
            "action": step_info.get("action", "unknown"),
            "description": step_info.get("description", ""),
            "status": result.get("status", "unknown"),
            "duration_ms": result.get("duration_ms", 0),
            "error": result.get("error"),
        })

    chain = REPORTER_PROMPT | model

    response = await chain.ainvoke({
        "test_plan": {
            "query": test_plan.get("natural_query", ""),
            "expected_outcome": test_plan.get("expected_outcome", ""),
            "total_steps": len(test_plan.get("steps", [])),
        },
        "results": formatted_results,
    })

    return {
        "messages": [AIMessage(content=response.content)],
        "summary": response.content,
        "final_status": overall_status,
    }
