"""Test case generation node - generates test cases from natural language."""

from langchain_core.prompts import ChatPromptTemplate
from agent.llm import get_llm
from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field
from typing import List, Optional

from agent.state import AgentState


class GeneratedTestCase(BaseModel):
    """A generated test case."""
    name: str = Field(description="Short, descriptive name for the test case")
    natural_query: str = Field(description="Natural language description of what to test")
    priority: str = Field(description="Priority level: low, medium, high, critical")
    tags: List[str] = Field(description="Tags for categorization")


class GeneratedTestCases(BaseModel):
    """Collection of generated test cases."""
    test_cases: List[GeneratedTestCase] = Field(description="List of generated test cases")
    summary: str = Field(description="Summary of what test cases were generated")


GENERATOR_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """You are a QA test case generator. Based on the user's request,
generate a comprehensive set of test cases.

Project: {project_name}
URL: {project_url}
Feature/Area: {feature}

Generate test cases that:
1. Cover the main happy path
2. Include edge cases and error scenarios
3. Test boundary conditions where applicable
4. Consider accessibility and usability
5. Include both positive and negative tests

Each test case should have:
- A clear, descriptive name
- A natural language query that can be executed by the testing agent
- Appropriate priority (critical for core functionality, high for important features, medium for nice-to-haves, low for edge cases)
- Relevant tags for organization"""),
    ("human", "{request}")
])


async def generate_test_cases(state: AgentState) -> dict:
    """Generate test cases from natural language request."""
    model = get_llm("default")
    structured_model = model.with_structured_output(GeneratedTestCases)

    messages = state.get("messages", [])
    last_message = messages[-1].content if messages else ""

    # Get project info from project_settings or legacy fields
    settings = state.get("project_settings") or {}
    project_name = settings.get("name") or state.get("project_name", "Unknown")
    project_url = settings.get("url") or state.get("project_url", "")

    chain = GENERATOR_PROMPT | structured_model

    result = await chain.ainvoke({
        "project_name": project_name,
        "project_url": project_url,
        "feature": state.get("extracted_feature", "general"),
        "request": last_message,
    })

    # Format response message
    response_parts = [f"Generated {len(result.test_cases)} test cases:\n"]

    for i, tc in enumerate(result.test_cases, 1):
        response_parts.append(
            f"\n{i}. **{tc.name}** [{tc.priority.upper()}]\n"
            f"   Query: \"{tc.natural_query}\"\n"
            f"   Tags: {', '.join(tc.tags)}"
        )

    response_parts.append(f"\n\n{result.summary}")

    return {
        "messages": [AIMessage(content="\n".join(response_parts))],
        "summary": result.summary,
        "generated_test_cases": [tc.model_dump() for tc in result.test_cases],
    }
