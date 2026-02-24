"""Auto-heal node — analyzes a failed test run and proposes corrected steps."""

from typing import Optional

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from agent.llm import get_llm
from core.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

class HealedStep(BaseModel):
    action: str
    target: Optional[str] = None
    value: Optional[str] = None
    description: str
    change_reason: Optional[str] = Field(
        default=None,
        description="Why this step changed. Omit if unchanged.",
    )


class HealSuggestion(BaseModel):
    healed_steps: list[HealedStep]
    changed_step_numbers: list[int] = Field(
        description="1-based step numbers that were modified."
    )
    explanation: str = Field(
        description="Plain-language summary shown to the user."
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="Confidence score from 0.0 to 1.0.",
    )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM = """\
You are a QA automation healing assistant.

A Playwright-based test run has failed. Your job is to analyse the failure
evidence and return a corrected, complete list of test steps that should make
the test pass.

## Step schema
Each step has four fields:
  action      – one of: navigate, click, type, fill_form, select, hover,
                press_key, wait, wait_for_page, screenshot, assert_text,
                assert_element, assert_url, back, evaluate, upload, drag
  target      – plain visible text of the element (preferred), or a CSS/role
                selector. ALWAYS prefer the plain visible text the user sees
                (e.g. "Blog", "Sign In", "Contact Us") over CSS selectors.
  value       – typed text, assertion expected value, wait duration (ms), etc.
  description – human-readable summary

## Rules
1. Return ALL steps (not just the changed ones). Preserve unchanged steps verbatim.
2. For every step you change, set change_reason to a short explanation.
3. **Most important rule for "Element not found" errors**: the target name in
   the step is stale or misspelled. Look at the list of ACTUAL PAGE ELEMENTS
   provided (if available) and find the closest match by meaning or text
   similarity. For example: "Block button" → "Blog", "Signin" → "Sign In".
   Use the exact visible text from the page elements list as the new target.
4. Use the screenshot to understand what the page looks like at failure.
5. Do NOT fix a working selector to a CSS selector — keep targets as plain text.
6. Set confidence between 0.0 and 1.0 reflecting certainty about the fix.
   If actual page elements are provided and you found a close match, confidence
   should be ≥ 0.85.
"""

_HUMAN_TEMPLATE = """\
## Test case
Name: {name}
Intent: {natural_query}
Base URL: {base_url}

## Original steps
{original_steps_text}

## Failure details
{failure_text}

{page_elements_section}
{screenshot_note}
Analyse the failure(s) and return the full corrected step list.
"""


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

async def suggest_heal(
    test_case_name: str,
    natural_query: str,
    base_url: str,
    original_steps: list[dict],
    failed_steps: list[dict],   # TestRunStep rows: step_number, action, target, value, error, screenshot
    page_elements: Optional[list[str]] = None,  # live DOM scan of the failing page
) -> HealSuggestion:
    """Produce a healed step list by inspecting failure evidence via LLM.

    Args:
        test_case_name: Display name of the test case.
        natural_query:  Original intent written by the user.
        base_url:       Project base URL for context.
        original_steps: The test case's current step list (list of dicts).
        failed_steps:   Rows from TestRunStep that have status="failed".
                        Each dict should contain step_number, action, target,
                        value, error, and optionally screenshot (base64 PNG).
        page_elements:  Optional live-scanned list of visible element texts on
                        the page at the point of failure. When provided, the LLM
                        can match stale/misspelled targets to real element names.

    Returns:
        HealSuggestion with healed_steps, changed_step_numbers, explanation,
        and confidence.
    """

    # --- format original steps -------------------------------------------------
    orig_lines = []
    for i, s in enumerate(original_steps, 1):
        parts = [f"{i}. [{s.get('action')}]"]
        if s.get("target"):
            parts.append(f"target={s['target']}")
        if s.get("value"):
            parts.append(f"value={s['value']}")
        parts.append(f"— {s.get('description', '')}")
        orig_lines.append(" ".join(parts))
    original_steps_text = "\n".join(orig_lines) if orig_lines else "(none)"

    # --- format failure details ------------------------------------------------
    # First failed step gets its screenshot; the rest are described as text only.
    first_screenshot: Optional[str] = None
    failure_lines = []
    for idx, fs in enumerate(failed_steps):
        sn = fs.get("step_number", "?")
        action = fs.get("action", "?")
        target = fs.get("target") or "(none)"
        value  = fs.get("value") or "(none)"
        error  = fs.get("error") or "(unknown error)"
        failure_lines.append(
            f"Step {sn} [{action}] target={target} value={value}\n"
            f"  Error: {error}"
        )
        if idx == 0 and fs.get("screenshot"):
            first_screenshot = fs["screenshot"]

    failure_text = "\n".join(failure_lines) if failure_lines else "(no failed steps provided)"

    # --- format live page elements (the key enrichment) -----------------------
    if page_elements:
        # Group into a readable bullet list; keep it short
        bullets = "\n".join(f"  - {e}" for e in page_elements[:80])
        page_elements_section = (
            "## Actual interactive elements currently on the page\n"
            "Use this list to find the closest match for any 'Element not found' error.\n"
            f"{bullets}\n"
        )
    else:
        page_elements_section = ""

    screenshot_note = (
        "A screenshot taken at the moment of the first failure is attached "
        "(read the text carefully — element labels are visible).\n"
        if first_screenshot
        else ""
    )

    # --- build messages -------------------------------------------------------
    human_text = _HUMAN_TEMPLATE.format(
        name=test_case_name,
        natural_query=natural_query,
        base_url=base_url,
        original_steps_text=original_steps_text,
        failure_text=failure_text,
        page_elements_section=page_elements_section,
        screenshot_note=screenshot_note,
    )

    if first_screenshot:
        human_msg = HumanMessage(content=[
            {"type": "text", "text": human_text},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{first_screenshot}",
                    "detail": "high",  # need to read nav text clearly
                },
            },
        ])
    else:
        human_msg = HumanMessage(content=human_text)

    messages = [
        ("system", _SYSTEM),
        human_msg,
    ]

    # --- call LLM with structured output -------------------------------------
    model = get_llm("default")
    structured_model = model.with_structured_output(HealSuggestion)

    try:
        result: HealSuggestion = await structured_model.ainvoke(messages)
        logger.info(
            f"Heal suggestion: {len(result.changed_step_numbers)} step(s) changed, "
            f"confidence={result.confidence:.2f}"
        )
        return result
    except Exception as exc:
        logger.error(f"Healer LLM call failed: {exc}")
        # Return no-op suggestion so the frontend can still show the dialog.
        return HealSuggestion(
            healed_steps=[
                HealedStep(**{k: v for k, v in s.items() if k in HealedStep.model_fields})
                for s in original_steps
            ],
            changed_step_numbers=[],
            explanation=f"Auto-heal analysis failed: {exc}",
            confidence=0.0,
        )
