"""Recorder API routes — start/stop recording sessions and relay events.

The backend acts as a relay + processor between the executor (raw DOM events)
and the frontend (processed test steps). It:
  1. Proxies start/stop calls to the executor
  2. Opens a WebSocket to the executor, receives raw events
  3. Processes events through the RecorderEventProcessor
  4. Forwards processed steps to the frontend via its own WebSocket
"""

import asyncio
import json
import os
from typing import List, Optional

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlmodel import Session

from agent.nodes.recorder_processor import RecorderEventProcessor, ProcessedStep
from agent.llm import get_llm
from langchain_core.prompts import ChatPromptTemplate
from db.session import engine
from db import crud
from core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/projects/{project_id}/recorder",
    tags=["recorder"],
)

EXECUTOR_URL = os.getenv("PLAYWRIGHT_EXECUTOR_URL", "http://localhost:8932")

# Active processor per project (one recording at a time per project)
_active_processors: dict[int, RecorderEventProcessor] = {}
_active_sessions: dict[int, str] = {}  # project_id -> session_id


class RecordStartRequest(BaseModel):
    base_url: str
    viewport_width: int = 1280
    viewport_height: int = 720


class RecordStartResponse(BaseModel):
    session_id: str
    ws_url: str


@router.post("/start")
async def start_recording(project_id: int, body: RecordStartRequest):
    """Start a recording session for a project."""
    if project_id in _active_sessions:
        return JSONResponse(
            status_code=409,
            content={"detail": "A recording session is already active for this project"},
        )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{EXECUTOR_URL}/recorder/start",
                json={
                    "base_url": body.base_url,
                    "viewport": {
                        "width": body.viewport_width,
                        "height": body.viewport_height,
                    },
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.error(f"Failed to start recording via executor: {e}")
        return JSONResponse(
            status_code=502,
            content={"detail": f"Executor unreachable: {e}"},
        )

    session_id = data["session_id"]
    _active_sessions[project_id] = session_id
    _active_processors[project_id] = RecorderEventProcessor(base_url=body.base_url)

    logger.info(f"Recording started for project {project_id}, session {session_id}")
    return {
        "session_id": session_id,
        "ws_url": f"/api/projects/{project_id}/recorder/ws",
    }


@router.post("/stop")
async def stop_recording(project_id: int):
    """Stop the active recording session for a project."""
    session_id = _active_sessions.get(project_id)
    if not session_id:
        return JSONResponse(
            status_code=404,
            content={"detail": "No active recording session"},
        )

    raw_events: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{EXECUTOR_URL}/recorder/stop",
                json={"session_id": session_id},
            )
            resp.raise_for_status()
            raw_events = resp.json().get("events", [])
    except httpx.HTTPError as e:
        logger.warning(f"Error stopping executor session: {e}")
    except Exception as e:
        logger.warning(f"Could not parse executor stop response: {e}")

    # Get final processed steps from the WebSocket-fed processor
    processor = _active_processors.get(project_id)
    steps = processor.get_all_steps() if processor else []

    # Fallback: if the WebSocket relay never forwarded events (e.g. connection
    # failed), re-process the raw events returned by the executor's stop call.
    if not steps and raw_events:
        logger.info(
            f"WS relay produced 0 steps; re-processing {len(raw_events)} raw events from executor"
        )
        base_url = processor._base_url if processor else ""
        fallback = RecorderEventProcessor(base_url=base_url)
        for event in raw_events:
            fallback.process_event(event)
        steps = fallback.get_all_steps()

    # Cleanup
    _active_sessions.pop(project_id, None)
    _active_processors.pop(project_id, None)

    logger.info(f"Recording stopped for project {project_id}: {len(steps)} steps")
    return {
        "session_id": session_id,
        "step_count": len(steps),
        "steps": [s.model_dump() for s in steps],
    }


@router.get("/status")
async def recording_status(project_id: int):
    """Check if a recording session is active for this project."""
    session_id = _active_sessions.get(project_id)
    processor = _active_processors.get(project_id)
    step_count = len(processor.steps) if processor else 0
    return {
        "active": session_id is not None,
        "session_id": session_id,
        "step_count": step_count,
    }


# ── AI metadata generation ────────────────────────────────────────────────────

class RecordedStepInput(BaseModel):
    action: str
    target: Optional[str] = None
    value: Optional[str] = None
    description: str = ""
    is_credential: bool = False
    coordinates: Optional[dict] = None
    locators: Optional[dict] = None
    causes_navigation: bool = False


class GenerateMetadataRequest(BaseModel):
    steps: List[RecordedStepInput]
    base_url: str = ""


class GeneratedMetadata(BaseModel):
    """AI-generated test case metadata from recorded steps."""
    name: str = Field(description="Short, descriptive test case name (e.g. 'Login Flow Test')")
    description: str = Field(description="Natural language description of what the test verifies")
    priority: str = Field(default="medium", description="Priority: low, medium, high, critical")
    tags: List[str] = Field(default_factory=list, description="Tags for categorization (e.g. 'auth', 'smoke', 'form')")


METADATA_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """You are a QA test case naming assistant. Given a sequence of recorded browser interaction steps, generate appropriate test case metadata.

Rules:
- Name should be concise but descriptive (e.g. "Contact Form Submission", "User Login Flow", "Product Search and Add to Cart")
- Description should explain what the test verifies in one sentence
- Priority: "critical" for auth/payment flows, "high" for core features, "medium" for standard features, "low" for cosmetic/minor
- Tags: 2-4 relevant tags (e.g. "auth", "form", "navigation", "smoke", "regression", "contact", "search")

Base URL: {base_url}"""),
    ("human", """Here are the recorded steps:

{steps_text}

Generate the test case metadata.""")
])


@router.post("/generate-metadata")
async def generate_metadata(project_id: int, body: GenerateMetadataRequest):
    """Use AI to generate test case name, description, priority, and tags from recorded steps."""
    if not body.steps:
        return JSONResponse(
            status_code=400,
            content={"detail": "No steps provided"},
        )

    # Format steps for the LLM
    steps_lines = []
    for i, step in enumerate(body.steps, 1):
        parts = [f"{i}. {step.action}"]
        if step.target:
            parts.append(f"target={step.target}")
        if step.value:
            parts.append(f"value={step.value}")
        if step.description:
            parts.append(f"({step.description})")
        if step.is_credential:
            parts.append("[CREDENTIAL]")
        steps_lines.append(" ".join(parts))

    steps_text = "\n".join(steps_lines)

    try:
        model = get_llm("default")
        structured_model = model.with_structured_output(GeneratedMetadata)
        chain = METADATA_PROMPT | structured_model

        result = await chain.ainvoke({
            "base_url": body.base_url,
            "steps_text": steps_text,
        })

        logger.info(f"Generated metadata for project {project_id}: {result.name}")
        return result.model_dump()
    except Exception as e:
        logger.error(f"Failed to generate metadata: {e}")
        # Fallback: generate basic metadata without AI
        first_action = body.steps[0].action if body.steps else "test"
        return {
            "name": f"Recorded Test ({len(body.steps)} steps)",
            "description": f"Recorded browser interaction starting with {first_action}",
            "priority": "medium",
            "tags": ["recorded"],
        }


# ── AI step refinement ─────────────────────────────────────────────────────

class RefinedStep(BaseModel):
    """A single refined test step matching the builder's action vocabulary."""
    action: str = Field(description="Action: navigate, click, type, fill_form, select, hover, press_key, wait, wait_for_page, screenshot, assert_text, assert_element, assert_url, back, scroll")
    target: Optional[str] = Field(default=None, description="Element identifier for click/type/hover/select/assert_element. MUST be preserved exactly if it is a CSS selector (starts with '[', '#', 'button[', 'input[', etc.) — these encode unique element identifiers critical for test reliability. For plain elements use readable text like 'Login button'. Must be null for navigate/fill_form/wait_for_page/screenshot/assert_text/back.")
    value: Optional[str] = Field(default=None, description="For navigate: relative URL path. For type: text. For assert_text: expected text. Must be null for click/hover.")
    description: str = Field(description="Human-readable description of this step")


class RefinedStepsResponse(BaseModel):
    """AI-refined steps matching builder quality."""
    steps: List[RefinedStep] = Field(description="Refined, high-quality test steps")


class RefineStepsRequest(BaseModel):
    steps: List[RecordedStepInput]
    base_url: str = ""


REFINE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """You are a QA test step refiner. You take raw browser-recorded steps and transform them into high-quality test steps that match the Checkmate test builder format.

Project URL: {base_url}

## Available Actions (mapped to Playwright)
- navigate: Go to URL (target = null, value = relative URL path like "/login")
- click: Click element (target = readable element description like "Login button", value = null)
- type: Type into field (target = field description like "Email input", value = text to type)
- fill_form: Fill multiple fields at once (target = null, value = JSON object)
- select: Select dropdown option (target = dropdown description, value = option text)
- hover: Hover over a nav menu item to reveal its submenu (target = element description, value = null). Always add a hover step BEFORE clicking items inside hover-triggered dropdowns.
- press_key: Press key (target = null, value = key name like "Enter")
- wait: Wait for element/text (target = text/element to wait for)
- wait_for_page: Wait for page load (target = null, value = "load")
- screenshot: Capture screenshot (target = null, value = null)
- assert_text: Verify text visible (target = null, value = expected text)
- assert_element: Verify element exists (target = element description, value = null)
- assert_url: Verify URL matches pattern (target = null, value = regex like ".*dashboard.*")
- back: Navigate back (target = null, value = null)
- scroll: Scroll page (target = null or element, value = direction)

## Refinement Rules

### Scroll handling
- **Keep ONE scroll step before each click/interaction** when the user scrolled to reach that element. Scrolling changes which elements are visible, which is critical when multiple elements share the same text (e.g. multiple "Draft" status badges at different positions on the page).
- **Collapse multiple consecutive scroll steps** between two non-scroll actions into a single scroll step.
- **Remove scroll steps that are immediately followed by another scroll** with no interaction between them.
- When in doubt, **keep** the scroll — it ensures the correct element is in the viewport.

### Navigation & clicks
1. Convert full URLs to relative paths (e.g. "https://example.com/about" → "/about")
2. Add wait_for_page (value "load") after clicks that trigger page navigation
3. Remove redundant navigate steps that follow a click on a link (the click already navigates)
4. Keep the first navigate step (initial page load)
5. For click targets use the button/link text (e.g. "Blog" not a CSS selector) **UNLESS the target is already a CSS selector** (starts with `[`, `#`, `button[`, `input[`, etc.). CSS selector targets must be kept EXACTLY as recorded — they encode unique element identifiers (e.g. `[data-testid="status-trigger-47"]`, `button[title="Move to folder"]`) that are critical for test reliability. Do NOT rewrite them as natural language.

### Hover steps (CRITICAL — missing hover steps cause test failures)
6. Hover steps are emitted automatically by the recorder at click time — each `hover` step immediately before a `click` is the correct parent trigger for that click. **Always keep hover steps that precede a click; never remove them.**
7. If multiple consecutive `hover` steps appear before a single `click`, keep only the **last** hover immediately before the click (it is the direct parent; earlier hovers are nav traversal noise).
8. **Clean up nav link targets**: nav menu items often have a heading AND a subtitle concatenated with no space (e.g. "Auto Key CalculatorInstant auto key quotes"). Use ONLY the heading portion as the click target (e.g. "Auto Key Calculator").

### Form inputs
9. Use descriptive, human-readable target names for inputs
10. Merge consecutive type steps on the same field into a single step
11. For type targets use the input label or placeholder (e.g. "Email input")

### Quality
12. Add assert_text or assert_element where appropriate to verify success after key interactions
13. End with a screenshot to capture the final state
14. Aim for the minimum number of steps that faithfully represent the user's intent"""),
    ("human", """Here are the raw recorded steps to refine:

{steps_text}

Transform these into high-quality test steps.""")
])


def _is_css_selector_target(target: str) -> bool:
    """Return True if target is a CSS selector rather than natural language.

    Detects: [attr="val"], #id, button[attr], input[type="x"], etc.
    Natural language always has spaces or commas, CSS selectors we emit don't.
    """
    import re as _re
    if not target or " " in target or "," in target:
        return False
    return (
        target.startswith("[")
        or target.startswith("#")
        or bool(_re.match(r"^[\w-]+[\[#]", target))
    )


def _scope_dropdown_assertions(steps: list[dict]) -> list[dict]:
    """Scope generic assert_text steps to specific CSS trigger elements.

    Detects the dropdown pattern:
        click [CSS_selector]        ← trigger opens dropdown
        click "Menuitem text"       ← user selects an option
        [optionally: wait / wait_for_page ...]
        assert_text value="Status"  ← AI adds this; BUT it matches filter chips too

    When there is NO wait_for_page between the trigger click and the menuitem
    click (i.e. the click opened a dropdown, not a new page), the assert_text
    is replaced with:
        assert_element [CSS_selector]:has-text("Status")

    This uses Playwright's :has-text() pseudo-class to check only the trigger
    element's own text content, eliminating false positives from filter chips
    or other page elements that happen to share the same text.

    Only short values (< 40 chars) are replaced — long values are page-level
    messages (form submission success, etc.), not element state labels.
    """
    result = list(steps)
    i = 0
    while i < len(result):
        step = result[i]
        if step.get("action") != "click":
            i += 1
            continue
        css_target = step.get("target", "")
        if not _is_css_selector_target(css_target):
            i += 1
            continue

        # Scan ahead: find the next click step, checking for page navigation
        navigated = False
        menuitem_idx = -1
        for j in range(i + 1, min(i + 4, len(result))):
            action = result[j].get("action", "")
            if action == "wait_for_page":
                navigated = True
                break
            if action == "click" and not _is_css_selector_target(result[j].get("target", "")):
                menuitem_idx = j
                break

        if navigated or menuitem_idx < 0:
            i += 1
            continue

        # Found dropdown pattern — look for assert_text in the next 3 steps
        for k in range(menuitem_idx + 1, min(menuitem_idx + 4, len(result))):
            s = result[k]
            if s.get("action") == "assert_text":
                value = s.get("value", "")
                if value and len(value) < 40:
                    result[k] = {
                        "action": "assert_element",
                        "target": f'{css_target}:has-text("{value}")',
                        "value": None,
                        "description": f'Verify element now shows "{value}"',
                        "coordinates": None,
                    }
                break

        i += 1
    return result


@router.post("/refine-steps")
async def refine_steps(project_id: int, body: RefineStepsRequest):
    """Use AI to transform raw recorder steps into builder-quality steps."""
    if not body.steps:
        return JSONResponse(
            status_code=400,
            content={"detail": "No steps provided"},
        )

    # ── Separate preserved steps from AI-refineable steps ───────────────────
    # CSS-selector steps (e.g. [data-testid="x"]) are NEVER sent to the AI
    # because the AI always rewrites them to natural language.
    # Additionally, a click step immediately AFTER a CSS step is its paired
    # menuitem action (e.g. CSS trigger → "Mark as Ready") and must stay
    # in sequence with the trigger. Both are preserved verbatim.
    #
    # Each original step is tagged as either "preserved" or "text" (AI-refineable).
    # After AI refinement, we rebuild the output respecting the original order.
    preserved_indices: set[int] = set()  # indices to keep verbatim
    text_steps: list[tuple[int, RecordedStepInput]] = []  # (orig_idx, step)

    for i, step in enumerate(body.steps):
        if step.target and _is_css_selector_target(step.target):
            preserved_indices.add(i)
            # Also preserve the next step if it's a click (menuitem action)
            if i + 1 < len(body.steps) and body.steps[i + 1].action == "click":
                preserved_indices.add(i + 1)
        elif step.action == "scroll":
            # Scroll steps are preserved verbatim — AI refinement rewrites
            # target="page" to page element names like "Features".
            preserved_indices.add(i)
        elif step.causes_navigation:
            # Navigation clicks (link clicks that triggered page navigation)
            # must be preserved — AI refinement removes them thinking they're
            # redundant, but they're essential for reaching the correct page.
            preserved_indices.add(i)
        elif i not in preserved_indices:
            text_steps.append((i, step))
    # ── End separation ─────────────────────────────────────────────────────────

    # Format only the text-based steps for the LLM
    steps_lines = []
    for seq, (_, step) in enumerate(text_steps, 1):
        parts = [f"{seq}. [{step.action}]"]
        if step.target:
            parts.append(f"target=\"{step.target}\"")
        if step.value:
            parts.append(f"value=\"{step.value}\"")
        if step.description:
            parts.append(f"— {step.description}")
        if step.is_credential:
            parts.append("[CREDENTIAL]")
        steps_lines.append(" ".join(parts))

    steps_text = "\n".join(steps_lines)

    try:
        # Only call AI if there are text steps to refine
        if text_steps:
            model = get_llm("default")
            structured_model = model.with_structured_output(RefinedStepsResponse)
            chain = REFINE_PROMPT | structured_model
            result = await chain.ainvoke({
                "base_url": body.base_url,
                "steps_text": steps_text,
            })
            refined_text_steps = list(result.steps)
        else:
            refined_text_steps = []

        # ── Merge back: interleave preserved steps with refined text steps ─────
        # Strategy: walk through the original step indices in order.
        # - Preserved steps are inserted verbatim at their original position.
        # - Text steps are replaced by the next refined step from the AI.
        # This guarantees preserved pairs (CSS trigger → menuitem) stay in order
        # and that trigger always precedes its menuitem action.
        total_text = len(text_steps)
        total_refined = len(refined_text_steps)

        steps_out: list[dict] = []
        refined_cursor = 0  # tracks which refined step to consume next

        # Group text_steps by their original index for lookup
        text_step_orig_indices = {orig_idx for (orig_idx, _) in text_steps}

        for i in range(len(body.steps)):
            if i in preserved_indices:
                # Preserved step (CSS trigger or its paired menuitem)
                steps_out.append(body.steps[i].model_dump())
            elif i in text_step_orig_indices:
                # This original text step maps to refined output.
                # Consume the next refined step(s). The AI may have expanded
                # one original step into multiple (e.g. click → click + wait),
                # so we consume proportionally.
                if refined_cursor < total_refined:
                    d = refined_text_steps[refined_cursor].model_dump()
                    # Restore coordinates and locators from this original step
                    orig_step = body.steps[i]
                    if orig_step.coordinates:
                        d["coordinates"] = orig_step.coordinates
                    if orig_step.locators:
                        d["locators"] = orig_step.locators
                    steps_out.append(d)
                    refined_cursor += 1

        # Append any remaining refined steps the AI added beyond the original count
        # (e.g. wait_for_page, assertions, screenshot)
        while refined_cursor < total_refined:
            steps_out.append(refined_text_steps[refined_cursor].model_dump())
            refined_cursor += 1
        # ── End merge ──────────────────────────────────────────────────────────

        # ── Scope dropdown assertions ───────────────────────────────────────────
        # Pattern: CSS trigger click → (no page nav) → menuitem click → assert_text
        # Problem: AI-generated assert_text checks the whole page body, so it finds
        # filter chips and other elements that share the text (always passes — false +).
        # Fix: replace with assert_element using `CSS_SEL:has-text("value")` which
        # narrows the assertion to the specific trigger element whose state changed.
        steps_out = _scope_dropdown_assertions(steps_out)
        # ── End assertion scoping ───────────────────────────────────────────────

        logger.info(
            f"Refined {len(body.steps)} raw steps → {len(steps_out)} quality steps "
            f"({len(preserved_indices)} steps kept verbatim) "
            f"for project {project_id}"
        )
        return {"steps": steps_out}
    except Exception as e:
        logger.error(f"Failed to refine steps: {e}")
        # Fallback: return the raw steps as-is
        return {
            "steps": [s.model_dump() for s in body.steps],
        }


@router.websocket("/ws")
async def recorder_websocket(websocket: WebSocket, project_id: int):
    """WebSocket relay: executor raw events → process → frontend steps.

    Also accepts control commands from frontend (stop).
    """
    session_id = _active_sessions.get(project_id)
    if not session_id:
        await websocket.close(code=4004, reason="No active recording session")
        return

    await websocket.accept()
    logger.info(f"Frontend WebSocket connected for project {project_id}")

    processor = _active_processors.get(project_id)
    if not processor:
        processor = RecorderEventProcessor()
        _active_processors[project_id] = processor

    # Send any already-processed steps (in case of reconnect)
    for step in processor.steps:
        await websocket.send_json({
            "type": "step",
            "data": step.model_dump(),
        })

    # Poll executor for new raw events every 500ms and stream processed steps
    # to the frontend. Simpler and more reliable than a WS-to-WS relay.
    stop_flag = asyncio.Event()
    last_event_count = len(processor.steps)  # skip already-sent steps

    async def poll_and_stream():
        nonlocal last_event_count
        poll_cursor = 0  # tracks how many raw events we've processed
        async with httpx.AsyncClient() as client:
            while not stop_flag.is_set():
                try:
                    resp = await client.get(
                        f"{EXECUTOR_URL}/recorder/events/{session_id}",
                        timeout=3.0,
                    )
                    if resp.status_code == 200:
                        all_events = resp.json().get("events", [])
                        new_events = all_events[poll_cursor:]
                        for raw_event in new_events:
                            prev_count = len(processor.steps)
                            processor.process_event(raw_event)
                            for s in processor.steps[prev_count:]:
                                await websocket.send_json({
                                    "type": "step",
                                    "data": s.model_dump(),
                                })
                        poll_cursor = len(all_events)
                except Exception as e:
                    logger.debug(f"Poll error: {e}")
                await asyncio.sleep(0.5)

    async def receive_commands():
        """Listen for frontend disconnect or stop commands."""
        try:
            while True:
                data = await websocket.receive_json()
                if data.get("command") == "stop":
                    stop_flag.set()
                    break
        except WebSocketDisconnect:
            logger.info(f"Frontend WebSocket disconnected for project {project_id}")
            stop_flag.set()
        except Exception as e:
            logger.debug(f"Frontend WS receive ended: {e}")
            stop_flag.set()

    poller = asyncio.create_task(poll_and_stream())
    receiver = asyncio.create_task(receive_commands())
    done, pending = await asyncio.wait(
        [poller, receiver], return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()

    try:
        await websocket.close()
    except Exception:
        pass
    logger.info(f"Recorder WebSocket closed for project {project_id}")
