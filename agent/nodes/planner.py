"""Test planning node - converts natural language to test steps."""

from langchain_core.prompts import ChatPromptTemplate
from agent.llm import get_llm
from pydantic import BaseModel, Field
from typing import List, Literal, Optional

from agent.state import AgentState, TestPlan, TestStep
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

# Keep list for runtime validation if needed
VALID_ACTIONS = list(ActionType.__args__)


class TestStepModel(BaseModel):
    """A single test step mapped to Playwright MCP tools."""
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


class TestPlanModel(BaseModel):
    """A complete test plan."""
    steps: List[TestStepModel] = Field(description="Ordered list of test steps")
    expected_outcome: str = Field(description="What success looks like for this test")
    fixture_ids: List[int] = Field(
        default=[],
        description="IDs of fixtures to use for setup (e.g., login fixture). If using fixtures, don't duplicate those setup steps."
    )
    needs_clarification: bool = Field(
        default=False,
        description="True if placeholders were used and user input is needed"
    )
    clarification_questions: Optional[List[str]] = Field(
        default=None,
        description="Questions to ask user if needs_clarification is true (e.g., 'What is the exact button text?')"
    )


PLANNER_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """You are a QA test planner. Given a natural language test request,
create a detailed test plan with browser actions that map to Playwright MCP tools.

Project URL: {base_url}
Feature to test: {feature}

{app_context}

{conversation_context}

{personas_and_pages}

{fixtures_context}

CRITICAL: JSON Output Format Examples
When outputting steps, use this exact JSON structure:

Navigate example (target MUST be null):
{{"action": "navigate", "target": null, "value": "/login", "description": "Go to login page"}}

Click example (value MUST be null):
{{"action": "click", "target": "Login button", "value": null, "description": "Click the login button"}}

Type example:
{{"action": "type", "target": "Email input", "value": "test@example.com", "description": "Enter email"}}

Assert URL example (target MUST be null):
{{"action": "assert_url", "target": null, "value": ".*dashboard.*", "description": "Verify URL contains dashboard"}}

WRONG - Never do this:
{{"action": "navigate", "target": "/login", "value": null}}  <-- URL must be in value, not target!

Available actions (mapped to Playwright):
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

Guidelines:
1. Use descriptive element names - Playwright uses accessibility tree, not CSS selectors
2. Prefer fill_form for login/signup forms over multiple type actions
3. Use wait_for_page after clicks that trigger page navigation or redirects (use the project's default page load event from Project Settings above)
4. Use wait for element/text to appear after dynamic content loads
5. Use assert_text or assert_element to verify success
6. Use assert_url to verify navigation to correct page or URL pattern. When user asks to "check if URL contains X", use pattern ".*X.*" (not the full URL). Examples: ".*dashboard.*", ".*login.*", ".*exampl.*"
7. End with a screenshot to capture final state
8. IMPORTANT: When personas are available and user mentions logging in "as admin" or similar, use the persona template variables like {{{{admin.username}}}} and {{{{admin.password}}}} in fill_form or type actions
9. IMPORTANT: When pages are available and user mentions a page name (like "login page"), use the page template variable like {{{{login}}}} for the navigate action value
10. IMPORTANT: If this is a follow-up request, MERGE with the previous plan - keep existing steps and add/modify as needed
11. For style checks (colors, sizes), use assert_style with the CSS property and expected value
12. If specific details are missing and NO matching persona/page exists, use placeholder like {{BUTTON_NAME}} and set needs_clarification=true
13. FIXTURES: If your test needs login/auth, use the login fixture (add ID to fixture_ids) and start your steps AFTER login (don't write login steps yourself)"""),
    ("human", "{query}")
])


def build_conversation_context(messages: list, previous_plan: Optional[dict]) -> str:
    """Build context from conversation history and previous test plan."""
    context_parts = []

    # Include previous messages (excluding the latest one which is the current query)
    if len(messages) > 1:
        context_parts.append("Previous conversation:")
        for msg in messages[:-1]:
            role = "User" if msg.type == "human" else "Assistant"
            # Truncate long messages
            content = msg.content[:500] + "..." if len(msg.content) > 500 else msg.content
            context_parts.append(f"  {role}: {content}")

    # Include previous test plan if exists
    if previous_plan:
        context_parts.append("\nPrevious test plan (IMPORTANT - extend/modify this, don't replace):")
        for i, step in enumerate(previous_plan.get("steps", []), 1):
            context_parts.append(f"  {i}. {step.get('action')}: {step.get('description')}")

    return "\n".join(context_parts) if context_parts else "No previous context."


def build_app_context(project_id: Optional[str]) -> str:
    """Build app context from project's base_prompt and settings."""
    if not project_id:
        return ""

    try:
        pid = int(project_id)
    except (ValueError, TypeError):
        return ""

    context_parts = []

    with Session(engine) as session:
        project = crud.get_project(session, pid)
        if project:
            # Add page load state setting
            page_load_state = project.page_load_state or "load"
            context_parts.append(f"## Project Settings\n- Default page load event: {page_load_state} (use this value for wait_for_page actions unless user specifies otherwise)")

            # Add user-provided app context
            if project.base_prompt:
                context_parts.append(f"\n## App Context (provided by user)\n{project.base_prompt}")

    return "\n".join(context_parts) if context_parts else ""


def build_personas_and_pages_context(project_id: Optional[str]) -> str:
    """Build context about available personas and pages for the project."""
    if not project_id:
        return "No project context available."

    try:
        pid = int(project_id)
    except (ValueError, TypeError):
        return "No project context available."

    context_parts = []

    with Session(engine) as session:
        personas = crud.get_personas_by_project(session, pid)
        pages = crud.get_pages_by_project(session, pid)

    if personas:
        context_parts.append("Available Personas (use these for login/authentication):")
        for p in personas:
            desc = f" - {p.description}" if p.description else ""
            # Use double braces to escape in f-string: {{ becomes {, }} becomes }
            context_parts.append(f"  - '{p.name}': Use {{{{{p.name}.username}}}} for username, {{{{{p.name}.password}}}} for password{desc}")

    if pages:
        context_parts.append("\nAvailable Pages (use these for navigation):")
        for p in pages:
            desc = f" - {p.description}" if p.description else ""
            context_parts.append(f"  - '{p.name}': Use {{{{{p.name}}}}} which resolves to '{p.path}'{desc}")

    if not context_parts:
        return "No personas or pages configured for this project."

    return "\n".join(context_parts)


def build_fixtures_context(project_id: Optional[str]) -> str:
    """Build context about available fixtures for the project."""
    if not project_id:
        return ""

    try:
        pid = int(project_id)
    except (ValueError, TypeError):
        return ""

    with Session(engine) as session:
        fixtures = crud.get_fixtures_by_project(session, pid)

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


async def plan_test(state: AgentState) -> dict:
    """Convert natural language query to a test plan."""
    from langchain_core.messages import AIMessage

    model = get_llm("default")
    structured_model = model.with_structured_output(TestPlanModel)

    messages = state.get("messages", [])
    last_message = messages[-1].content if messages else ""
    previous_plan = state.get("test_plan")

    logger.info(f"Planning test: {last_message[:100]}{'...' if len(last_message) > 100 else ''}")

    # Get project info from project_settings or legacy fields
    settings = state.get("project_settings") or {}
    project_id = settings.get("id") or state.get("project_id")
    project_url = settings.get("url") or state.get("project_url", "")

    # Build conversation context
    conversation_context = build_conversation_context(messages, previous_plan)

    # Build app context from base_prompt
    app_context = build_app_context(project_id)

    # Build personas and pages context
    personas_and_pages = build_personas_and_pages_context(project_id)

    # Build fixtures context (skip when generating fixtures to avoid circular dependencies)
    if state.get("skip_fixtures_context"):
        fixtures_context = ""
    else:
        fixtures_context = build_fixtures_context(project_id)

    chain = PLANNER_PROMPT | structured_model

    result = await chain.ainvoke({
        "base_url": project_url,
        "feature": state.get("extracted_feature", "general"),
        "app_context": app_context,
        "conversation_context": conversation_context,
        "personas_and_pages": personas_and_pages,
        "fixtures_context": fixtures_context,
        "query": last_message
    })

    # Convert to TestPlan format
    test_plan: TestPlan = {
        "test_case_id": None,
        "natural_query": last_message,
        "steps": [
            {
                "action": step.action,
                "target": step.target,
                "value": step.value,
                "description": step.description,
            }
            for step in result.steps
        ],
        "expected_outcome": result.expected_outcome,
        "fixture_ids": result.fixture_ids,
    }

    logger.info(f"Generated test plan with {len(result.steps)} steps, fixtures: {result.fixture_ids}")

    # Get available personas and pages to filter valid template variables
    valid_templates = set()
    if project_id:
        try:
            pid = int(project_id)
            with Session(engine) as session:
                personas = crud.get_personas_by_project(session, pid)
                pages = crud.get_pages_by_project(session, pid)
                for p in personas:
                    valid_templates.add(f"{p.name}.username")
                    valid_templates.add(f"{p.name}.password")
                for p in pages:
                    valid_templates.add(p.name)
        except (ValueError, TypeError):
            pass

    # Check for placeholders in the plan (excluding valid persona/page templates)
    placeholders_found = []
    import re
    for step in result.steps:
        for field in [step.target, step.value, step.description]:
            if field and "{{" in field:
                # Extract placeholder names from {{name}} or {{name.field}} patterns
                matches = re.findall(r'\{\{([^}]+)\}\}', field)
                for match in matches:
                    # Skip if this is a valid persona/page template
                    if match not in valid_templates:
                        placeholders_found.append(match)

    # Build response message
    if placeholders_found:
        response_parts = ["**I need some details to complete this test plan:**\n"]
        unique_placeholders = list(set(placeholders_found))
        for placeholder in unique_placeholders:
            readable_name = placeholder.replace("_", " ").lower()
            response_parts.append(f"- What is the **{readable_name}**?")

        response_parts.append(f"\n**Draft Test Plan** ({len(result.steps)} steps):")
        for i, step in enumerate(result.steps, 1):
            response_parts.append(f"{i}. {step.description}")

        response_parts.append("\nPlease provide these details so I can finalize the test.")
    else:
        response_parts = [f"**Test Plan** ({len(result.steps)} steps):\n"]
        for i, step in enumerate(result.steps, 1):
            response_parts.append(f"{i}. {step.description}")

    # Also include clarification from structured output if present
    if result.needs_clarification and result.clarification_questions:
        response_parts.append("\n**Additional questions:**")
        for q in result.clarification_questions:
            response_parts.append(f"- {q}")

    return {
        "messages": [AIMessage(content="\n".join(response_parts))],
        "test_plan": test_plan,
        "current_step": 0,
        "test_results": [],
    }
