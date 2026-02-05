"""Test case builder node - builds/modifies test cases from user messages."""

from langchain_core.prompts import ChatPromptTemplate
from agent.llm import get_llm
from pydantic import BaseModel, Field
from typing import List, Literal, Optional
from sqlmodel import Session

from db.session import engine
from db import crud
from core.logging import get_logger

logger = get_logger(__name__)


ActionType = Literal[
    "navigate", "click", "type", "fill_form", "select", "hover",
    "press_key", "wait", "wait_for_page", "screenshot", "assert_text",
    "assert_element", "assert_style", "assert_url", "back", "evaluate", "upload", "drag"
]


class TestStepModel(BaseModel):
    """A single test step."""
    action: ActionType = Field(description="The action to perform")
    target: Optional[str] = Field(
        default=None,
        description="Element description for click/type/hover/select/assert_element/assert_style. Must be null for navigate/fill_form/press_key/wait_for_page/screenshot/assert_text/back/evaluate."
    )
    value: Optional[str] = Field(
        default=None,
        description="For navigate: URL path. For type/fill_form: text or JSON. For press_key: key name. For assert_text: expected text. For wait: time in ms. Must be null for click/hover/assert_element/back."
    )
    description: str = Field(description="Human-readable description of this step")


class TestCaseModel(BaseModel):
    """A complete test case."""
    name: str = Field(description="Short, descriptive name for the test case")
    natural_query: str = Field(description="Natural language description of what this test does")
    priority: str = Field(default="medium", description="Priority: low, medium, high, critical")
    tags: List[str] = Field(default_factory=list, description="Tags for categorization")
    steps: List[TestStepModel] = Field(description="Ordered list of test steps")
    fixture_ids: List[int] = Field(
        default_factory=list,
        description="IDs of fixtures to use for setup (e.g., login fixture). If using fixtures, don't duplicate those setup steps."
    )


class BuilderResponse(BaseModel):
    """Response from the builder."""
    test_case: TestCaseModel = Field(description="The updated test case")
    message: Optional[str] = Field(
        default=None,
        description="Optional message to user (clarification question or confirmation)"
    )
    needs_clarification: bool = Field(
        default=False,
        description="True if placeholders were used and user input is needed"
    )


BUILDER_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """You are a QA test case builder. Your job is to build or modify a test case based on user messages.

Project: {project_name}
Project URL: {base_url}

{app_context}

{personas_and_pages}

{fixtures_context}

## Previous User Messages (intent history)
{previous_messages}

## Original Test Case (what was loaded from database)
{original_test_case}

## Current Test Case State (including any manual edits by user)
{current_test_case}

## CRITICAL: JSON Output Format Examples
When outputting steps, use this exact JSON structure:

Navigate example (target MUST be null):
{{"action": "navigate", "target": null, "value": "/login", "description": "Go to login page"}}

Click example (value MUST be null):
{{"action": "click", "target": "Login button", "value": null, "description": "Click the login button"}}

Type example:
{{"action": "type", "target": "Email input", "value": "test@example.com", "description": "Enter email"}}

Assert URL example (target MUST be null):
{{"action": "assert_url", "target": null, "value": ".*dashboard.*", "description": "Verify URL contains 'dashboard'"}}

WRONG - Never do this:
{{"action": "navigate", "target": "/login", "value": null}}  <-- URL must be in value, not target!

## Available Actions (mapped to Playwright)
- navigate: Go to URL (target = null, value = URL path like "/login")
- click: Click element (target = element description like "Login button", value = null)
- type: Type into single field (target = field description, value = text to type)
- fill_form: Fill multiple fields at once (target = null, value = JSON like '{{"email": "test@example.com", "password": "pass123"}}')
- select: Select dropdown option (target = dropdown description, value = option to select)
- hover: Hover over element (target = element description, value = null)
- press_key: Press keyboard key (target = null, value = key name like "Enter", "Tab", "Escape")
- wait: Wait for element/text to appear (target = text/element to wait for, OR target = null with value = time in ms)
- wait_for_page: Wait for page to finish loading (target = null, value = "load", "domcontentloaded", or "networkidle")
- screenshot: Capture screenshot (target = null, value = optional filename or null)
- assert_text: Verify text is visible (target = null, value = expected text)
- assert_element: Verify element exists (target = element description, value = null)
- assert_style: Verify element CSS style (target = element description, value = JSON like '{{{{"property": "background-color", "expected": "grey"}}}}')
- assert_url: Verify URL matches regex (target = null, value = regex pattern like ".*dashboard.*" to check if URL contains "dashboard")
- back: Navigate back (target = null, value = null)
- evaluate: Run JavaScript (target = null, value = JS code)
- upload: Upload file (target = file input element, value = file path)
- drag: Drag and drop (target = source element, value = destination element)

## Guidelines
1. Use descriptive element names - Playwright uses accessibility tree, not CSS selectors
2. Prefer fill_form for login/signup forms over multiple type actions
3. Use wait_for_page after clicks that trigger page navigation or redirects (use the project's default page load event from Project Settings above)
4. Use wait for element/text to appear after dynamic content loads
5. Use assert_text or assert_element to verify success
6. Use assert_url to verify navigation to correct page or URL pattern. When user asks to "check if URL contains X", use pattern ".*X.*" (not the full URL). Examples: ".*dashboard.*", ".*login.*", ".*exampl.*"
7. End with a screenshot to capture final state
7. IMPORTANT: When personas are available and user mentions a persona name (like "admin", "client"), use the template variables like {{{{admin.username}}}} and {{{{admin.password}}}} - do NOT make up fake credentials
8. IMPORTANT: When pages are available and user mentions a page name, use the template variable like {{{{login}}}} for navigation
9. IMPORTANT: Preserve existing steps when adding new ones (unless user asks to remove/replace)
10. If specific details are missing and NO matching persona/page exists, use placeholder like {{{{BUTTON_NAME}}}} and set needs_clarification=true
11. Update the test case name and natural_query to reflect all the steps
12. FIXTURES: If your test needs login/auth, use the login fixture (add ID to fixture_ids) and start your steps AFTER login (don't write login steps yourself)

Based on the current message, update the test case appropriately."""),
    ("human", "{current_message}")
])


def build_app_context(project_id: Optional[int]) -> str:
    """Build app context from project's base_prompt and settings."""
    if not project_id:
        return ""

    context_parts = []

    with Session(engine) as session:
        project = crud.get_project(session, project_id)
        if project:
            # Add page load state setting
            page_load_state = project.page_load_state or "load"
            context_parts.append(f"## Project Settings\n- Default page load event: {page_load_state} (use this value for wait_for_page actions unless user specifies otherwise)")

            # Add user-provided app context
            if project.base_prompt:
                context_parts.append(f"\n## App Context (provided by user)\n{project.base_prompt}")

    return "\n".join(context_parts) if context_parts else ""


def build_personas_and_pages_context(project_id: Optional[int]) -> str:
    """Build context about available personas and pages for the project."""
    if not project_id:
        return "No personas or pages configured."

    context_parts = []

    with Session(engine) as session:
        personas = crud.get_personas_by_project(session, project_id)
        pages = crud.get_pages_by_project(session, project_id)

    if personas:
        context_parts.append("## Available Personas (USE THESE for login credentials)")
        for p in personas:
            desc = f" - {p.description}" if p.description else ""
            context_parts.append(f"  - '{p.name}': Use {{{{{p.name}.username}}}} and {{{{{p.name}.password}}}}{desc}")

    if pages:
        context_parts.append("\n## Available Pages (USE THESE for navigation)")
        for p in pages:
            desc = f" - {p.description}" if p.description else ""
            context_parts.append(f"  - '{p.name}': Use {{{{{p.name}}}}} (resolves to '{p.path}'){desc}")

    if not context_parts:
        return "No personas or pages configured for this project."

    return "\n".join(context_parts)


def build_fixtures_context(project_id: Optional[int]) -> str:
    """Build context about available fixtures for the project."""
    if not project_id:
        return ""

    with Session(engine) as session:
        fixtures = crud.get_fixtures_by_project(session, project_id)

    if not fixtures:
        return ""

    context_parts = ["## Available Fixtures (reusable setup sequences)"]

    for f in fixtures:
        steps = f.get_setup_steps()
        # Summarize what the fixture does
        step_actions = [s.get("action", "") for s in steps[:3]]
        step_summary = ", ".join(step_actions)
        if len(steps) > 3:
            step_summary += f", ... ({len(steps)} steps total)"

        context_parts.append(f"  - Fixture ID {f.id}: '{f.name}'")
        if f.description:
            context_parts.append(f"    Description: {f.description}")
        context_parts.append(f"    Steps: {step_summary}")

    context_parts.append("")
    context_parts.append("FIXTURE RULES:")
    context_parts.append("1. If your test needs login/authentication, ALWAYS use the login fixture (add its ID to fixture_ids)")
    context_parts.append("2. When you use a fixture, your test steps start AFTER the fixture completes")
    context_parts.append("3. Do NOT write login/setup steps if you're using a fixture that already does that")
    context_parts.append("")
    context_parts.append("Example: Test 'verify dashboard shows user name' with login fixture:")
    context_parts.append("  - fixture_ids: [1]  (the login fixture)")
    context_parts.append("  - steps: navigate to /dashboard, assert_text 'Welcome'  (NO login steps needed)")

    return "\n".join(context_parts)


async def build_test_case(
    current_message: str,
    previous_messages: List[str],
    current_test_case: Optional[dict],
    project_name: str,
    base_url: str,
    project_id: Optional[int] = None,
) -> BuilderResponse:
    """Build or modify a test case based on user messages."""
    logger.info(f"Building test case: {current_message[:100]}{'...' if len(current_message) > 100 else ''}")

    model = get_llm("default")
    structured_model = model.with_structured_output(BuilderResponse)

    # Build app context from base_prompt
    app_context = build_app_context(project_id)

    # Build personas and pages context
    personas_and_pages = build_personas_and_pages_context(project_id)

    # Build fixtures context
    fixtures_context = build_fixtures_context(project_id)

    # Format previous messages
    if previous_messages:
        prev_msgs_formatted = "\n".join(
            f"{i+1}. \"{msg}\"" for i, msg in enumerate(previous_messages)
        )
    else:
        prev_msgs_formatted = "(No previous messages - this is the first request)"

    # Format original test case (what was loaded from database)
    original_tc_formatted = "(No original - this is a new test case)"
    if current_test_case and current_test_case.get("original_steps"):
        orig_parts = ["Steps as originally loaded:"]
        for i, step in enumerate(current_test_case.get("original_steps", []), 1):
            orig_parts.append(
                f"  {i}. {step.get('action')}: {step.get('description')} "
                f"(target={step.get('target')}, value={step.get('value')})"
            )
        original_tc_formatted = "\n".join(orig_parts)

    # Format current test case state
    if current_test_case and current_test_case.get("steps"):
        tc_parts = [
            f"Name: {current_test_case.get('name', 'Untitled')}",
            f"Query: {current_test_case.get('natural_query', '')}",
            f"Priority: {current_test_case.get('priority', 'medium')}",
            f"Tags: {', '.join(current_test_case.get('tags', []))}",
            "Steps:"
        ]
        for i, step in enumerate(current_test_case.get("steps", []), 1):
            tc_parts.append(
                f"  {i}. {step.get('action')}: {step.get('description')} "
                f"(target={step.get('target')}, value={step.get('value')})"
            )
        current_tc_formatted = "\n".join(tc_parts)
    else:
        current_tc_formatted = "(Empty - no test case yet, create a new one)"

    chain = BUILDER_PROMPT | structured_model

    result = await chain.ainvoke({
        "project_name": project_name,
        "base_url": base_url,
        "app_context": app_context,
        "personas_and_pages": personas_and_pages,
        "fixtures_context": fixtures_context,
        "previous_messages": prev_msgs_formatted,
        "original_test_case": original_tc_formatted,
        "current_test_case": current_tc_formatted,
        "current_message": current_message,
    })

    step_count = len(result.test_case.steps) if result.test_case else 0
    logger.info(f"Built test case '{result.test_case.name}' with {step_count} steps")

    return result
