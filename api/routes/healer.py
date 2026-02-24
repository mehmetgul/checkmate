"""Auto-heal API route — POST /api/test-cases/{id}/heal."""

import json
import os
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from db.session import get_session_dep
from db.models import TestCase, TestRun, TestRunStep, Project, RunStatus, StepStatus
from db import crud
from agent.nodes.healer import suggest_heal, HealSuggestion
from core.logging import get_logger

EXECUTOR_URL = os.getenv("PLAYWRIGHT_EXECUTOR_URL", "http://localhost:8932")

logger = get_logger(__name__)

router = APIRouter(prefix="/test-cases", tags=["healer"])


async def _scan_page_elements(url: str) -> Optional[list[str]]:
    """Call the executor's /scan-elements endpoint for a given URL.

    Returns a list of visible element texts, or None on any error so the
    healer can still run in screenshot-only mode.
    """
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{EXECUTOR_URL}/scan-elements",
                json={"url": url, "timeout": 12000},
            )
            if resp.status_code == 200:
                data = resp.json()
                elements = data.get("elements", [])
                logger.info(f"DOM scan: {len(elements)} elements at {url}")
                return elements or None
    except Exception as exc:
        logger.warning(f"DOM scan failed for {url}: {exc}")
    return None


def _resolve_failing_page_url(base_url: str, original_steps: list[dict], failed_step_number: int) -> Optional[str]:
    """Find the URL the test was on when the failing step ran.

    Strategy: walk the steps that ran BEFORE the failing step and return the
    value of the last 'navigate' action, resolved against base_url.
    Falls back to base_url itself if no navigate step is found.
    """
    last_navigate_value: Optional[str] = None
    for step in original_steps:
        sn = step.get("step_number", 0)
        if isinstance(sn, int) and sn >= failed_step_number:
            break
        if step.get("action") == "navigate":
            last_navigate_value = step.get("value") or step.get("target")

    if not last_navigate_value:
        # Fallback: use base_url root
        return base_url.rstrip("/") + "/"

    # If the value is a relative path, prepend base_url
    if last_navigate_value.startswith("http"):
        return last_navigate_value
    return base_url.rstrip("/") + "/" + last_navigate_value.lstrip("/")


class HealRequest(BaseModel):
    run_id: int


@router.post("/{test_case_id}/heal", response_model=HealSuggestion)
async def heal_test_case(
    test_case_id: int,
    request: HealRequest,
    session: Session = Depends(get_session_dep),
):
    """Analyse a failed run and return an AI-suggested step list fix.

    The endpoint:
    1. Verifies the test case exists.
    2. Verifies the run belongs to this test case and is in a failed state.
    3. Loads the failed TestRunStep rows (with error and screenshot).
    4. Calls the healer LLM node.
    5. Returns HealSuggestion for the frontend to render the diff dialog.
    """

    # 1. Load test case
    test_case = crud.get_test_case(session, test_case_id)
    if not test_case:
        raise HTTPException(status_code=404, detail="Test case not found")

    # 2. Validate run
    test_run = session.get(TestRun, request.run_id)
    if not test_run:
        raise HTTPException(status_code=404, detail="Test run not found")
    if test_run.test_case_id != test_case_id:
        raise HTTPException(status_code=400, detail="Run does not belong to this test case")
    if test_run.status != RunStatus.FAILED:
        raise HTTPException(
            status_code=400,
            detail=f"Run status is '{test_run.status}', expected 'failed'",
        )

    # 3. Load failed steps ordered by step_number
    stmt = (
        select(TestRunStep)
        .where(
            TestRunStep.test_run_id == request.run_id,
            TestRunStep.status == StepStatus.FAILED,
        )
        .order_by(TestRunStep.step_number)
    )
    failed_steps = session.exec(stmt).all()

    if not failed_steps:
        raise HTTPException(
            status_code=400,
            detail="No failed steps found in this run — cannot generate heal suggestion",
        )

    # 4. Parse original steps from the test case
    try:
        original_steps: list[dict] = json.loads(test_case.steps) if test_case.steps else []
    except (json.JSONDecodeError, TypeError):
        original_steps = []

    # 5. Get project base URL for LLM context
    project = session.get(Project, test_case.project_id)
    base_url = project.base_url if project else ""

    # 6. Build failed-steps dicts for the healer
    failed_steps_data = [
        {
            "step_number": s.step_number,
            "action": s.action,
            "target": s.target,
            "value": s.value,
            "error": s.error,
            "screenshot": s.screenshot,
        }
        for s in failed_steps
    ]

    logger.info(
        f"Starting heal for test_case={test_case_id}, run={request.run_id}, "
        f"failed_steps={len(failed_steps_data)}"
    )

    # 7. DOM scan — figure out which page the failing step was on and ask the
    #    executor to extract all visible interactive elements from that URL.
    #    This gives the LLM a ground-truth element list for fuzzy matching
    #    (e.g. "Block button" → "Blog").
    first_failed_step_number = failed_steps_data[0]["step_number"]

    # Build a numbered version of original_steps for _resolve_failing_page_url
    numbered_steps = [
        {**s, "step_number": i + 1}
        for i, s in enumerate(original_steps)
    ]

    scan_url = _resolve_failing_page_url(base_url, numbered_steps, first_failed_step_number)
    page_elements = await _scan_page_elements(scan_url) if scan_url else None

    # 8. Call healer LLM node
    suggestion = await suggest_heal(
        test_case_name=test_case.name,
        natural_query=test_case.natural_query or "",
        base_url=base_url,
        original_steps=original_steps,
        failed_steps=failed_steps_data,
        page_elements=page_elements,
    )

    return suggestion
