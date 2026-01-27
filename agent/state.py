"""State definitions for the QA Testing Agent."""

from typing import TypedDict, Annotated, Optional, List, Literal
from langgraph.graph.message import add_messages
from langchain_core.messages import AnyMessage


"""
Supported actions mapped to Playwright:

| Action        | Description                   | Parameters                           |
|---------------|-------------------------------|--------------------------------------|
| navigate      | Go to URL                     | value=URL                            |
| click         | Click element                 | target=element description           |
| type          | Type into field               | target=element, value=text           |
| fill_form     | Fill multiple fields          | value=JSON {field: value}            |
| select        | Select dropdown option        | target=element, value=option(s)      |
| hover         | Hover over element            | target=element                       |
| press_key     | Press keyboard key            | value=key name (Enter, Tab, etc)     |
| wait          | Wait for element/text         | value=time(ms) OR target=text/element|
| wait_for_page | Wait for page load            | value=load/domcontentloaded/networkidle|
| screenshot    | Take screenshot               | value=filename (optional)            |
| assert_text   | Verify text visible           | value=expected text                  |
| assert_element| Verify element visible        | target=element                       |
| assert_style  | Verify element CSS style      | target=element, value=JSON           |
| back          | Navigate back                 | -                                    |
| evaluate      | Run JavaScript                | value=JavaScript code                |
| upload        | Upload file                   | value=file path(s)                   |
| drag          | Drag and drop                 | target=start, value=end element      |
"""


class TestStep(TypedDict):
    """A single step in a test plan.

    Maps to Playwright MCP tools for browser automation.
    """
    action: Literal[
        "navigate",       # Go to URL
        "click",          # Click element
        "type",           # Type text into field
        "fill_form",      # Fill multiple form fields at once
        "select",         # Select dropdown option
        "hover",          # Hover over element
        "press_key",      # Press keyboard key
        "wait",           # Wait for time or element
        "wait_for_page",  # Wait for page to finish loading
        "screenshot",     # Take screenshot
        "assert_text",    # Verify text is visible
        "assert_element", # Verify element is visible
        "assert_style",   # Verify element CSS style
        "back",           # Navigate back
        "evaluate",       # Run JavaScript
        "upload",         # Upload file
        "drag",           # Drag and drop
    ]
    target: Optional[str]  # Element description or CSS selector
    value: Optional[str]  # URL, text, time(ms), or other action-specific value
    description: str  # Human-readable step description


class TestPlan(TypedDict):
    """A complete test plan generated from natural language."""
    test_case_id: Optional[str]
    natural_query: str
    steps: List[TestStep]
    expected_outcome: str


class TestResult(TypedDict):
    """Result of executing a single test step."""
    step_number: int
    status: Literal["passed", "failed", "skipped"]
    screenshot: Optional[str]  # Base64 or file path
    error: Optional[str]
    duration_ms: int


class ProjectSettings(TypedDict, total=False):
    """Project configuration and context.

    This bundles all project-related settings that the agent needs:
    - Basic info: id, name, url
    - Config: custom project configuration
    - Context: base_prompt describing app setup, auth flow, etc.
    - Personas and pages are fetched at runtime from DB
    """
    id: str
    name: str
    url: str
    config: dict
    base_prompt: Optional[str]


class AgentState(TypedDict):
    """Main state for the QA Testing Agent."""
    # Conversation
    messages: Annotated[list[AnyMessage], add_messages]

    # Project context (unified settings object)
    project_settings: Optional[ProjectSettings]

    # Legacy fields (kept for backwards compatibility, prefer project_settings)
    project_id: Optional[str]
    project_name: Optional[str]
    project_url: Optional[str]
    project_config: Optional[dict]

    # Intent classification
    intent: Optional[Literal["execute_test", "generate_test_cases", "analyze_results", "manage_project"]]
    confidence: Optional[float]
    extracted_feature: Optional[str]

    # Test planning
    test_plan: Optional[TestPlan]

    # Test execution
    current_step: int
    test_results: List[TestResult]
    browser_state: Optional[dict]  # Current page URL, title, etc.

    # Completion
    test_run_id: Optional[str]
    summary: Optional[str]
    final_status: Optional[Literal["passed", "failed", "cancelled"]]

    # Generated test cases (from generator node)
    generated_test_cases: Optional[List[dict]]
