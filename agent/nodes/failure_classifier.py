"""Failure classification for intelligent test retry."""

import base64
from typing import Literal, Optional

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from agent.llm import get_llm
from core.logging import get_logger

logger = get_logger(__name__)


FailureCategory = Literal[
    "network_error",      # Retryable: connection timeout, DNS failure, ECONNREFUSED
    "timeout",            # Retryable: page load timeout, element wait timeout
    "element_timing",     # Retryable: element not found (timing issue)
    "authentication_failure",  # NOT retryable: invalid credentials, login rejected
    "assertion_failure",       # NOT retryable: expected text not found
    "validation_error",        # NOT retryable: form validation errors
    "application_error",       # NOT retryable: 500 errors, app crashes
    "unknown",                 # Uncertain - default to not retryable
]

# Categories that should be retried
RETRYABLE_CATEGORIES: set[FailureCategory] = {
    "network_error",
    "timeout",
    "element_timing",
}


class FailureClassification(BaseModel):
    """Result of failure classification."""
    is_retryable: bool = Field(
        description="Whether this failure should be retried"
    )
    failure_category: FailureCategory = Field(
        description="The category of failure"
    )
    confidence: float = Field(
        description="Confidence score from 0.0 to 1.0",
        ge=0.0,
        le=1.0
    )
    reasoning: str = Field(
        description="Brief explanation of the classification"
    )


CLASSIFIER_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """You are a test failure classifier for a browser automation system.
Analyze the test step failure and determine if it should be retried.

## Failure Categories

RETRYABLE failures (transient issues that may succeed on retry):
- network_error: Connection timeouts, DNS failures, ECONNREFUSED, network unreachable
- timeout: Page load timeout, element wait timeout, script timeout
- element_timing: "Element not found" when it's likely a timing issue (element loading)

NOT RETRYABLE failures (actual test failures or app issues):
- authentication_failure: Invalid credentials, login rejected, session expired
- assertion_failure: Expected text/element not found (actual test assertion failed)
- validation_error: Form validation errors shown to user (expected behavior)
- application_error: 500 errors, server crashes, application exceptions
- unknown: Cannot determine - default to NOT retryable

## Decision Guidelines

1. If the error mentions "timeout", "timed out", "waiting for" - likely retryable (timeout or element_timing)
2. If the error mentions "not found", "unable to locate" - check context:
   - If it's about an element on an interactive page - likely element_timing (retryable)
   - If it's an assertion check - assertion_failure (not retryable)
3. If the error mentions connection, network, refused - network_error (retryable)
4. If the error mentions credentials, password, login failed - authentication_failure (not retryable)
5. If the screenshot shows an error message or validation error - likely not retryable
6. If the screenshot shows a blank/loading page - likely retryable (timing)

Be conservative: if uncertain, classify as "unknown" with is_retryable=false."""),
    ("human", """Classify this test step failure:

Action: {action}
Target: {target}
Value: {value}
Error: {error_message}

{screenshot_note}

Determine if this failure should be retried.""")
])


async def classify_failure(
    action: str,
    target: Optional[str],
    value: Optional[str],
    error_message: str,
    screenshot_b64: Optional[str] = None,
) -> FailureClassification:
    """Classify a test step failure to determine if it should be retried.

    Args:
        action: The action that failed (e.g., "click", "type")
        target: The target element (if any)
        value: The action value (if any)
        error_message: The error message from the failure
        screenshot_b64: Optional base64-encoded screenshot of the failure

    Returns:
        FailureClassification with is_retryable, category, confidence, and reasoning
    """
    model = get_llm("fast")

    # Prepare the prompt
    screenshot_note = ""
    if screenshot_b64:
        screenshot_note = "A screenshot of the failure is attached for analysis."

    # Build messages
    messages = []

    # Format the prompt
    prompt_messages = CLASSIFIER_PROMPT.format_messages(
        action=action,
        target=target or "(none)",
        value=value or "(none)",
        error_message=error_message,
        screenshot_note=screenshot_note,
    )

    # If we have a screenshot, use vision capability
    if screenshot_b64:
        # Convert to vision message format
        system_msg = prompt_messages[0]
        human_content = prompt_messages[1].content

        messages = [
            system_msg,
            HumanMessage(content=[
                {"type": "text", "text": human_content},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{screenshot_b64}",
                        "detail": "low",  # Use low detail for faster processing
                    },
                },
            ]),
        ]
    else:
        messages = prompt_messages

    # Get structured output
    structured_model = model.with_structured_output(FailureClassification)

    try:
        result = await structured_model.ainvoke(messages)
        logger.info(
            f"Failure classified: category={result.failure_category}, "
            f"retryable={result.is_retryable}, confidence={result.confidence:.2f}"
        )
        return result
    except Exception as e:
        logger.error(f"Failed to classify failure: {e}")
        # Return conservative default
        return FailureClassification(
            is_retryable=False,
            failure_category="unknown",
            confidence=0.0,
            reasoning=f"Classification failed: {str(e)}",
        )


def is_retryable_category(category: FailureCategory) -> bool:
    """Check if a failure category is retryable."""
    return category in RETRYABLE_CATEGORIES
