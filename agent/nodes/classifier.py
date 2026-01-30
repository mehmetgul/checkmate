"""Intent classification node."""

from langchain_core.prompts import ChatPromptTemplate
from agent.llm import get_llm
from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field
from typing import Literal, Optional

from agent.state import AgentState
from core.logging import get_logger

logger = get_logger(__name__)


class IntentClassification(BaseModel):
    """Structured output for intent classification."""
    intent: Literal["execute_test", "generate_test_cases", "analyze_results", "manage_project"] = Field(
        description="The detected intent from the user message"
    )
    confidence: float = Field(
        description="Confidence score from 0.0 to 1.0",
        ge=0.0,
        le=1.0
    )
    extracted_feature: Optional[str] = Field(
        default=None,
        description="The feature or area being referenced (e.g., 'login', 'checkout', 'navigation')"
    )


CLASSIFIER_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """You are an intent classifier for a QA testing agent.
Analyze the user's message and determine their intent.

Possible intents:
1. execute_test - User wants to run a test (e.g., "is login working?", "test the checkout", "check if signup validates email")
2. generate_test_cases - User wants to create test cases (e.g., "generate tests for the checkout flow", "create test cases for login")
3. analyze_results - User wants to understand past results (e.g., "why did the last test fail?", "show me the test history")
4. manage_project - User wants to configure or manage (e.g., "add a new project", "update the base URL")

Project context:
- Name: {project_name}
- URL: {project_url}

Extract the feature/area being referenced if mentioned (e.g., login, checkout, signup, navigation)."""),
    ("human", "{message}")
])


async def classify_intent(state: AgentState) -> dict:
    """Classify the user's intent from their message."""
    model = get_llm("fast")
    structured_model = model.with_structured_output(IntentClassification)

    messages = state.get("messages", [])
    last_message = messages[-1].content if messages else ""

    # Get project info from project_settings or legacy fields
    settings = state.get("project_settings") or {}
    project_name = settings.get("name") or state.get("project_name", "Unknown")
    project_url = settings.get("url") or state.get("project_url", "")

    chain = CLASSIFIER_PROMPT | structured_model

    result = await chain.ainvoke({
        "project_name": project_name,
        "project_url": project_url,
        "message": last_message
    })

    logger.info(f"Classified intent: {result.intent} (confidence={result.confidence:.2f}, feature={result.extracted_feature})")

    return {
        "intent": result.intent,
        "confidence": result.confidence,
        "extracted_feature": result.extracted_feature,
    }
