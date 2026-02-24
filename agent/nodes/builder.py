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
    "assert_element", "assert_style", "assert_url", "back", "evaluate", "upload", "drag",
    "scroll"
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


class CredentialSuggestion(BaseModel):
    """A credential detected from user input that could be saved to the vault."""
    name: str = Field(description="Short identifier, e.g. 'admin', 'api_service'")
    credential_type: str = Field(default="login", description="login, api_key, token, or custom")
    username: Optional[str] = Field(default=None, description="Username if login type")
    password: Optional[str] = Field(default=None, description="Password if login type")
    description: Optional[str] = Field(default=None, description="What this credential is for")


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
    suggested_credentials: List[CredentialSuggestion] = Field(
        default_factory=list,
        description="Credentials detected in user input that could be saved to the vault"
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

Click example (target = exact element text, value MUST be null):
{{"action": "click", "target": "Login", "value": null, "description": "Click the Login button"}}

Type example:
{{"action": "type", "target": "Email input", "value": "test@example.com", "description": "Enter email"}}

Fill form with credentials (ALWAYS use references when credentials exist):
{{"action": "fill_form", "target": null, "value": "{{\\"email\\": \\"{{{{admin.username}}}}\\", \\"password\\": \\"{{{{admin.password}}}}\\"}}", "description": "Fill login form with admin credentials"}}

Assert URL example (target MUST be null):
{{"action": "assert_url", "target": null, "value": ".*dashboard.*", "description": "Verify URL contains 'dashboard'"}}

WRONG - Never do this:
{{"action": "navigate", "target": "/login", "value": null}}  <-- URL must be in value, not target!
{{"action": "fill_form", "target": null, "value": "{{\\"email\\": \\"admin@example.com\\", \\"password\\": \\"admin123\\"}}"}}  <-- NEVER hardcode credentials! Use {{{{name.field}}}} references!

## Available Actions (mapped to Playwright)
- navigate: Go to URL (target = null, value = URL path like "/login")
- click: Click element (target = exact visible text like "Login", "Features", "Submit", value = null)
- type: Type into single field (target = exact label text like "Email", "Password", value = text to type)
- fill_form: Fill multiple fields at once (target = null, value = JSON like '{{"email": "{{{{admin.username}}}}", "password": "{{{{admin.password}}}}"}}' — ALWAYS use credential references when available)
- select: Select from native <select> dropdown or combobox ONLY (target = exact label text, value = option text to select). Do NOT use for custom dialog buttons — use click instead.
- hover: Hover over element (target = exact visible text, value = null)
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
- scroll: Scroll the page (target = "page" for page scroll, or exact element text to scroll to that element; value = "up", "down", "top", "bottom", "smooth_bottom", "smooth_top", or pixel amount like "500")

## CRITICAL RULE: NEVER HARDCODE CREDENTIALS
You MUST check "Available Credentials" above BEFORE writing any step that involves usernames, passwords, emails, API keys, or tokens.

### Case 1: Credential already exists in vault
If a matching credential exists (e.g., "admin", "user", "technician"), ALWAYS use {{{{name.field}}}} references:
- Login: {{{{admin.username}}}}, {{{{admin.password}}}}
- API key: {{{{service.api_key}}}}
- Token: {{{{service.token}}}}
- Custom: {{{{name.field_name}}}}

### Case 2: User provides NEW credentials not in vault
If the user provides credentials in their message (e.g., "email: mike@example.com password: tech123") and NO matching credential exists, you MUST do BOTH:
1. Add a CredentialSuggestion to suggested_credentials (pick a short name like "technician", "mike", etc.)
2. Use that SAME name as {{{{name.field}}}} template variables in the steps — NOT the raw values

Example: User says "login with mike@example.com password tech123"
- Step 1: You decide the credential name will be "mike"
- Step 2: You add to suggested_credentials: {{"name": "mike", "credential_type": "login", "username": "mike@example.com", "password": "tech123"}}
- Step 3: In the fill_form step, you write: {{"email": "{{{{mike.username}}}}", "password": "{{{{mike.password}}}}"}}
- WRONG: {{"email": "mike@example.com", "password": "tech123"}} ← NEVER do this even for new credentials!

The user will be prompted to save the credential to the vault. The template references will resolve at test runtime.

## CRITICAL: Writing Executable Targets
The executor finds elements using Playwright's accessibility tree. Targets must be the EXACT visible text, label, or role name of the element — not a description of what it is.

GOOD targets (exact element text the executor can find):
  "Login"            — button whose text is "Login"
  "Features"         — link whose text is "Features"
  "Email"            — input labeled "Email"
  "Move"             — button whose text is "Move"
  "Mark as Ready"    — menu item whose text is "Mark as Ready"
  "Draft"            — badge/button whose text is "Draft"
  "Login func"       — folder button whose text is "Login func"
  "Unassigned Scenarios" — button whose text is "Unassigned Scenarios"

BAD targets (descriptions the executor CANNOT find):
  "Login button"          — element text is "Login", not "Login button"
  "Move button"           — element text is "Move", not "Move button"
  "the submit button"     — not real element text
  "Cancel button on move dialog" — too descriptive
  "Move-to-folder icon on a test case" — not element text
  "Draft status badge on a test case"  — not element text
  "Folder selection dropdown"          — not element text

RULES for targets:
- Use the EXACT text shown on the button/link/label. Do NOT append "button", "link", "icon", "field" to the text.
- For icon-only buttons without text, use the aria-label (e.g., "Close") or data-testid
- For generic repeated elements (3rd row, 5th item), acknowledge the limitation — prefer text that uniquely identifies the element
- Never invent target text. If you don't know the exact text, use a {{{{PLACEHOLDER}}}} and set needs_clarification=true

## CRITICAL: Click vs Navigate vs Select
- Use "click" to interact with buttons, links, menu items, and dialog options — this is the most common action
- Use "navigate" ONLY for direct URL entry (typing a URL in the browser). Do NOT use navigate for clicking links.
  WRONG: {{"action": "navigate", "target": null, "value": "/features"}} when user says "click Features link"
  RIGHT: {{"action": "click", "target": "Features", "value": null}}
- Use "select" ONLY for native HTML <select> dropdowns or combobox inputs, NOT for clicking options in custom dialogs/menus
  WRONG: {{"action": "select", "target": "Folder selection dropdown", "value": "Login func"}} for a dialog with folder buttons
  RIGHT: {{"action": "click", "target": "Login func", "value": null}} — folders in a dialog are buttons, use click

## CRITICAL: Scroll format
- Page scroll: target = "page", value = "down" or "up" (NOT target = null, NOT value = "bottom")
  RIGHT: {{"action": "scroll", "target": "page", "value": "down"}}
  WRONG: {{"action": "scroll", "target": null, "value": "bottom"}}
  WRONG: {{"action": "scroll", "target": "bottom", "value": null}}
- Scroll to element: target = element text, value = null

## Dialog and Menu Interactions
When a user clicks a trigger that opens a dialog/dropdown:
1. The click on the trigger opens the dialog — use "click" with the trigger's text or data-testid
2. Options inside the dialog are buttons — use "click" with the option's exact text
3. Confirmation buttons (Move, Save, Delete, Cancel) — use "click" with exact button text
Example flow for "move a test case to Login func folder":
  {{"action": "click", "target": "[data-testid=\\"move-folder-52\\"]", "value": null, "description": "Open move-to-folder dialog"}}
  {{"action": "click", "target": "Login func", "value": null, "description": "Select Login func folder"}}
  {{"action": "click", "target": "Move", "value": null, "description": "Confirm the move"}}

## Status Change Interactions
Status badges appear on EVERY test case in a list — text like "Draft" may match 40+ elements.
ALWAYS use the data-testid to target a specific test case's status badge:
  Pattern: [data-testid="status-trigger-{{ID}}"] where ID is the test case ID

Example — change status of test case #48 from Draft to Ready:
  {{"action": "click", "target": "[data-testid=\\"status-trigger-48\\"]", "value": null, "description": "Open status dropdown for test case #48"}}
  {{"action": "click", "target": "Mark as Ready", "value": null, "description": "Change status to Ready"}}

If you don't know the test case ID, use a placeholder and set needs_clarification=true:
  {{"action": "click", "target": "[data-testid=\\"status-trigger-{{{{TEST_CASE_ID}}}}\\"]", "value": null, "description": "Open status dropdown"}}

## Repeated Elements — When to Use data-testid
Many UI elements repeat across rows in a list (status badges, action icons, dropdown triggers).
When the user says "click X on a test case", the element text alone is ambiguous.
Use data-testid patterns to uniquely target the specific element:
  - Status badge: [data-testid="status-trigger-{{ID}}"]
  - Move to folder: [data-testid="move-folder-{{ID}}"]
If you know which test case (from context or user message), use the actual ID.
If not, use a placeholder like {{{{TEST_CASE_ID}}}} and set needs_clarification=true.

## Guidelines
1. Prefer fill_form for login/signup forms over multiple type actions
2. Use wait_for_page after clicks that trigger page navigation or redirects (use the project's default page load event from Project Settings above)
3. Use wait for element/text to appear after dynamic content loads
4. Use assert_text or assert_element to verify success
5. Use assert_url to verify navigation to correct page or URL pattern. When user asks to "check if URL contains X", use pattern ".*X.*" (not the full URL). Examples: ".*dashboard.*", ".*login.*", ".*exampl.*"
6. End with a screenshot to capture final state
7. When pages are available and user mentions a page name, use the template variable like {{{{login}}}} for navigation
8. When test data is available, use {{{{data.dataset_name.field}}}} to reference test data values
9. Preserve existing steps when adding new ones (unless user asks to remove/replace)
10. If specific details are missing and NO matching credential/page exists, use placeholder like {{{{BUTTON_NAME}}}} and set needs_clarification=true
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
    """Build context about available credentials, pages, and test data for the project."""
    if not project_id:
        return "No credentials, pages, or test data configured."

    context_parts = []

    with Session(engine) as session:
        personas = crud.get_personas_by_project(session, project_id)
        pages = crud.get_pages_by_project(session, project_id)
        test_data_items = crud.get_test_data_by_project(session, project_id)

    if personas:
        context_parts.append("## Available Credentials (USE THESE instead of hardcoding secrets)")
        for p in personas:
            desc = f" - {p.description}" if p.description else ""
            cred_type = getattr(p, 'credential_type', 'login') or 'login'
            if cred_type == "login":
                context_parts.append(f"  - '{p.name}' (login): Use {{{{{p.name}.username}}}} and {{{{{p.name}.password}}}}{desc}")
            elif cred_type == "api_key":
                context_parts.append(f"  - '{p.name}' (API key): Use {{{{{p.name}.api_key}}}}{desc}")
            elif cred_type == "token":
                context_parts.append(f"  - '{p.name}' (token): Use {{{{{p.name}.token}}}}{desc}")
            elif cred_type == "custom":
                context_parts.append(f"  - '{p.name}' (custom): Use {{{{{p.name}.<field_name>}}}}{desc}")

    if pages:
        context_parts.append("\n## Available Pages (USE THESE for navigation)")
        for p in pages:
            desc = f" - {p.description}" if p.description else ""
            context_parts.append(f"  - '{p.name}': Use {{{{{p.name}}}}} (resolves to '{p.path}'){desc}")

    if test_data_items:
        context_parts.append("\n## Available Test Data (USE THESE for test data)")
        for td in test_data_items:
            desc = f" - {td.description}" if td.description else ""
            context_parts.append(f"  - '{td.name}': Use {{{{data.{td.name}.<field>}}}}{desc}")

    if not context_parts:
        return "No credentials, pages, or test data configured for this project."

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
