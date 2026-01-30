"""Test case management API routes."""

import asyncio
import json
import logging
from datetime import datetime
from typing import List, Optional, AsyncGenerator
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session
from httpx import HTTPError

from db.session import get_session_dep, engine
from db.models import (
    TestCase, TestCaseCreate, TestCaseRead,
    TestRun, TestRunCreate, TestRunRead,
    TestRunStep, TestRunStepCreate, TestRunStepRead,
    RunStatus, RunTrigger, StepStatus,
)
from db import crud
from agent.utils.resolver import resolve_references, mask_passwords_in_steps
from api.utils.streaming import (
    streaming_context,
    sse_event,
    sse_error,
    sse_warning,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/test-cases", tags=["test-cases"])


@router.get("/project/{project_id}", response_model=List[TestCaseRead])
def list_test_cases(
    project_id: int,
    skip: int = 0,
    limit: int = 100,
    session: Session = Depends(get_session_dep)
):
    """List all test cases for a project."""
    return crud.get_test_cases_by_project(session, project_id, skip=skip, limit=limit)


@router.post("", response_model=TestCaseRead)
def create_test_case(
    test_case: TestCaseCreate,
    session: Session = Depends(get_session_dep)
):
    """Create a new test case."""
    # Verify project exists
    project = crud.get_project(session, test_case.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return crud.create_test_case(session, test_case)


@router.get("/{test_case_id}", response_model=TestCaseRead)
def get_test_case(
    test_case_id: int,
    session: Session = Depends(get_session_dep)
):
    """Get a test case by ID."""
    test_case = crud.get_test_case(session, test_case_id)
    if not test_case:
        raise HTTPException(status_code=404, detail="Test case not found")
    return test_case


@router.put("/{test_case_id}", response_model=TestCaseRead)
def update_test_case(
    test_case_id: int,
    data: TestCaseCreate,
    session: Session = Depends(get_session_dep)
):
    """Update a test case."""
    test_case = crud.update_test_case(session, test_case_id, data.model_dump())
    if not test_case:
        raise HTTPException(status_code=404, detail="Test case not found")
    return test_case


@router.delete("/{test_case_id}")
def delete_test_case(
    test_case_id: int,
    session: Session = Depends(get_session_dep)
):
    """Delete a test case."""
    success = crud.delete_test_case(session, test_case_id)
    if not success:
        raise HTTPException(status_code=404, detail="Test case not found")
    return {"status": "deleted"}


# =============================================================================
# Test Case Runs
# =============================================================================

class TestRunWithSteps(BaseModel):
    """Test run with its steps."""
    id: int
    test_case_id: Optional[int]
    project_id: int
    trigger: str
    status: str
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    summary: Optional[str]
    error_count: int
    pass_count: int
    created_at: datetime
    steps: List[TestRunStepRead]


@router.get("/{test_case_id}/runs", response_model=List[TestRunWithSteps])
def get_test_case_runs(
    test_case_id: int,
    skip: int = 0,
    limit: int = 50,
    session: Session = Depends(get_session_dep)
):
    """Get all runs for a test case with their steps."""
    # Verify test case exists
    test_case = crud.get_test_case(session, test_case_id)
    if not test_case:
        raise HTTPException(status_code=404, detail="Test case not found")

    # Get runs
    runs = crud.get_test_runs_by_test_case(session, test_case_id, skip=skip, limit=limit)

    # Get steps for each run
    result = []
    for run in runs:
        steps = crud.get_test_run_steps(session, run.id)
        result.append(TestRunWithSteps(
            id=run.id,
            test_case_id=run.test_case_id,
            project_id=run.project_id,
            trigger=run.trigger.value,
            status=run.status.value,
            started_at=run.started_at,
            completed_at=run.completed_at,
            summary=run.summary,
            error_count=run.error_count,
            pass_count=run.pass_count,
            created_at=run.created_at,
            steps=steps,
        ))

    return result


@router.post("/{test_case_id}/runs", response_model=TestRunWithSteps)
def run_test_case(
    test_case_id: int,
    session: Session = Depends(get_session_dep)
):
    """Execute a test case and create a new run."""
    # Get test case
    test_case = crud.get_test_case(session, test_case_id)
    if not test_case:
        raise HTTPException(status_code=404, detail="Test case not found")

    # Parse steps from test case
    try:
        steps_data = json.loads(test_case.steps) if isinstance(test_case.steps, str) else test_case.steps
    except json.JSONDecodeError:
        steps_data = []

    # Resolve persona/page references in steps
    resolved_steps = resolve_references(session, test_case.project_id, steps_data)

    # Create test run
    test_run = crud.create_test_run(session, TestRunCreate(
        project_id=test_case.project_id,
        test_case_id=test_case_id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.RUNNING,
    ))

    # Update with start time
    crud.update_test_run(session, test_run.id, {"started_at": datetime.utcnow()})

    # Create steps and simulate execution
    created_steps = []
    pass_count = 0
    error_count = 0

    for i, step_data in enumerate(resolved_steps):
        # Create step record
        step = crud.create_test_run_step(session, TestRunStepCreate(
            test_run_id=test_run.id,
            test_case_id=test_case_id,
            step_number=i + 1,
            action=step_data.get("action", "unknown"),
            target=step_data.get("target"),
            value=step_data.get("value"),
            status=StepStatus.PASSED,  # Simulated - all pass for now
            duration=100 + (i * 50),  # Simulated duration
        ))
        created_steps.append(step)
        pass_count += 1

    # Update test run with results
    crud.update_test_run(session, test_run.id, {
        "status": RunStatus.PASSED if error_count == 0 else RunStatus.FAILED,
        "completed_at": datetime.utcnow(),
        "pass_count": pass_count,
        "error_count": error_count,
        "summary": f"Executed {len(resolved_steps)} steps: {pass_count} passed, {error_count} failed",
    })

    # Fetch updated run
    updated_run = crud.get_test_run(session, test_run.id)

    return TestRunWithSteps(
        id=updated_run.id,
        test_case_id=updated_run.test_case_id,
        project_id=updated_run.project_id,
        trigger=updated_run.trigger.value,
        status=updated_run.status.value,
        started_at=updated_run.started_at,
        completed_at=updated_run.completed_at,
        summary=updated_run.summary,
        error_count=updated_run.error_count,
        pass_count=updated_run.pass_count,
        created_at=updated_run.created_at,
        steps=created_steps,
    )


# =============================================================================
# Test Case Run with SSE Streaming
# =============================================================================

async def run_test_case_stream(test_case_id: int, browser: Optional[str] = None) -> AsyncGenerator[str, None]:
    """
    Stream test case execution results via SSE.
    Each step result is sent as a separate event.

    Args:
        test_case_id: ID of test case to run
        browser: Optional browser ID (e.g., "chrome", "chromium-headless")
    """
    try:
        async with streaming_context() as (session, executor_client, use_simulation):
            # Get test case
            test_case = crud.get_test_case(session, test_case_id)
            if not test_case:
                yield sse_error("Test case not found")
                return

            # Get project for base_url
            project = crud.get_project(session, test_case.project_id)
            if not project:
                yield sse_error("Project not found")
                return

            # Parse steps from test case
            try:
                steps_data = json.loads(test_case.steps) if isinstance(test_case.steps, str) else test_case.steps
            except json.JSONDecodeError:
                steps_data = []

            if not steps_data:
                yield sse_error("No steps defined in test case")
                return

            if use_simulation:
                yield sse_warning("Playwright executor unavailable, using simulation mode")

            # Resolve persona/page references in steps
            resolved_steps = resolve_references(session, test_case.project_id, steps_data)
            # Create masked version for display (database storage and frontend)
            display_steps = mask_passwords_in_steps(resolved_steps)

            # Create test run
            test_run = crud.create_test_run(session, TestRunCreate(
                project_id=test_case.project_id,
                test_case_id=test_case_id,
                trigger=RunTrigger.MANUAL,
                status=RunStatus.RUNNING,
            ))
            crud.update_test_run(session, test_run.id, {"started_at": datetime.utcnow()})

            # Send run started event
            yield sse_event("run_started", run_id=test_run.id, test_case_id=test_case_id, total_steps=len(resolved_steps))

            pass_count = 0
            error_count = 0

            if use_simulation:
                # Fallback: simulate execution
                for i, step_data in enumerate(resolved_steps):
                    display_step = display_steps[i]  # Use masked version for display
                    action = step_data.get("action", "unknown")
                    target = display_step.get("target")  # Masked
                    value = display_step.get("value")  # Masked
                    description = step_data.get("description", f"Step {i + 1}")

                    yield sse_event("step_started", step_number=i + 1, action=action, description=description)

                    await asyncio.sleep(0.3 + (i * 0.1))
                    step_status = StepStatus.PASSED
                    step_error = None
                    step_duration = 100 + (i * 50)

                    crud.create_test_run_step(session, TestRunStepCreate(
                        test_run_id=test_run.id,
                        test_case_id=test_case_id,
                        step_number=i + 1,
                        action=action,
                        target=target,
                        value=value,  # Masked value stored in DB
                        status=step_status,
                        duration=step_duration,
                        error=step_error,
                    ))

                    pass_count += 1

                    yield sse_event("step_completed", step_number=i + 1, action=action, description=description, status=step_status.value, duration=step_duration, error=step_error)
            else:
                # Execute via playwright-http
                execution_options = {"screenshot_on_failure": True}
                if browser:
                    execution_options["browser"] = browser

                async for event in executor_client.execute_stream(
                    base_url=project.base_url,
                    steps=resolved_steps,
                    test_id=str(test_case_id),
                    options=execution_options,
                ):
                    event_type = event.get("type")

                    if event_type == "error":
                        yield sse_error(event.get("error", "Unknown executor error"))
                        break

                    elif event_type == "step_started":
                        step_number = event.get("step_number", 0)
                        # Use masked values from display_steps for frontend
                        step_idx = step_number - 1
                        display_step = display_steps[step_idx] if step_idx < len(display_steps) else {}
                        masked_event = {
                            **event,
                            "target": display_step.get("target"),
                            "value": display_step.get("value"),
                        }
                        yield f"data: {json.dumps(masked_event)}\n\n"

                    elif event_type == "step_completed":
                        step_number = event.get("step_number", 0)
                        status = event.get("status", "failed")
                        step_status = StepStatus.PASSED if status == "passed" else StepStatus.FAILED

                        # Get step data for this step (use display_steps for masked values)
                        step_idx = step_number - 1
                        display_step = display_steps[step_idx] if step_idx < len(display_steps) else {}

                        # Create step record in DB with masked values
                        crud.create_test_run_step(session, TestRunStepCreate(
                            test_run_id=test_run.id,
                            test_case_id=test_case_id,
                            step_number=step_number,
                            action=display_step.get("action", "unknown"),
                            target=display_step.get("target"),
                            value=display_step.get("value"),  # Masked value
                            status=step_status,
                            duration=event.get("duration", 0),
                            error=event.get("error"),
                            screenshot=event.get("screenshot"),
                        ))

                        if step_status == StepStatus.PASSED:
                            pass_count += 1
                        else:
                            error_count += 1

                        # Use masked values from display_steps for frontend
                        masked_event = {
                            **event,
                            "target": display_step.get("target"),
                            "value": display_step.get("value"),
                        }
                        yield f"data: {json.dumps(masked_event)}\n\n"

                    elif event_type == "completed":
                        # Executor signals completion
                        pass

            # Update test run with final results
            final_status = RunStatus.PASSED if error_count == 0 else RunStatus.FAILED
            executed_count = pass_count + error_count
            skipped_count = len(resolved_steps) - executed_count
            if skipped_count > 0:
                summary = f"Executed {executed_count} of {len(resolved_steps)} steps: {pass_count} passed, {error_count} failed, {skipped_count} skipped"
            else:
                summary = f"Executed {len(resolved_steps)} steps: {pass_count} passed, {error_count} failed"

            crud.update_test_run(session, test_run.id, {
                "status": final_status,
                "completed_at": datetime.utcnow(),
                "pass_count": pass_count,
                "error_count": error_count,
                "summary": summary,
            })

            # Send run completed event
            yield sse_event("run_completed", run_id=test_run.id, status=final_status.value, pass_count=pass_count, error_count=error_count, summary=summary)

    except SQLAlchemyError as e:
        logger.error(f"Database error in run_test_case_stream: {e}")
        yield sse_error(f"Database error: {e}")
    except HTTPError as e:
        logger.error(f"Executor connection error: {e}")
        yield sse_error(f"Browser connection error: {e}")
    except json.JSONDecodeError as e:
        yield sse_error(f"Invalid step data: {e}")
    except Exception as e:
        logger.exception("Unexpected error in run_test_case_stream")
        yield sse_error(str(e))


class RunTestCaseRequest(BaseModel):
    """Request body for running a test case."""
    browser: Optional[str] = None  # Browser ID (e.g., "chrome", "chromium-headless")


@router.post("/{test_case_id}/runs/stream")
async def run_test_case_streaming(test_case_id: int, request: Optional[RunTestCaseRequest] = None):
    """
    Execute a test case with SSE streaming.
    Returns step-by-step results in real-time.

    Optionally specify a browser to use for execution.
    """
    browser = request.browser if request else None
    return StreamingResponse(
        run_test_case_stream(test_case_id, browser=browser),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )


# =============================================================================
# Batch Run (Suite) with SSE Streaming
# =============================================================================

class BatchRunRequest(BaseModel):
    """Request body for batch run."""
    test_case_ids: List[int]
    browser: Optional[str] = None  # Browser ID (e.g., "chrome", "chromium-headless")


async def run_batch_stream(project_id: int, test_case_ids: List[int], browser: Optional[str] = None) -> AsyncGenerator[str, None]:
    """
    Stream batch test execution results via SSE.
    Runs multiple test cases sequentially, streaming progress for each.
    """
    import uuid

    try:
        async with streaming_context() as (session, executor_client, use_simulation):
            # Verify project exists
            project = crud.get_project(session, project_id)
            if not project:
                yield sse_error("Project not found")
                return

            # Filter to valid test cases that belong to this project
            valid_test_cases = []
            for tc_id in test_case_ids:
                tc = crud.get_test_case(session, tc_id)
                if tc and tc.project_id == project_id:
                    valid_test_cases.append(tc)

            if not valid_test_cases:
                yield sse_error("No valid test cases found")
                return

            if use_simulation:
                yield sse_warning("Playwright executor unavailable, using simulation mode")

            # Generate batch ID to group these runs
            batch_id = f"batch-{uuid.uuid4().hex[:8]}"

            # Send batch started event
            yield sse_event("batch_started", batch_id=batch_id, total_tests=len(valid_test_cases), test_case_ids=[tc.id for tc in valid_test_cases])

            batch_passed = 0
            batch_failed = 0
            run_ids = []

            # Execute each test case
            for idx, test_case in enumerate(valid_test_cases):
                # Send test started event
                yield sse_event("test_started", test_case_id=test_case.id, name=test_case.name, index=idx + 1, total=len(valid_test_cases))

                # Parse steps
                try:
                    steps_data = json.loads(test_case.steps) if isinstance(test_case.steps, str) else test_case.steps
                except json.JSONDecodeError:
                    steps_data = []

                if not steps_data:
                    yield sse_event("test_completed", test_case_id=test_case.id, status="skipped", message="No steps defined")
                    continue

                # Resolve persona/page references
                resolved_steps = resolve_references(session, project_id, steps_data)
                # Create masked version for display (database storage)
                display_steps = mask_passwords_in_steps(resolved_steps)

                # Create test run with batch_id in thread_id
                test_run = crud.create_test_run(session, TestRunCreate(
                    project_id=project_id,
                    test_case_id=test_case.id,
                    trigger=RunTrigger.MANUAL,
                    status=RunStatus.RUNNING,
                    thread_id=batch_id,  # Store batch_id to identify suite runs
                ))
                crud.update_test_run(session, test_run.id, {"started_at": datetime.utcnow()})
                run_ids.append(test_run.id)

                pass_count = 0
                error_count = 0

                if use_simulation:
                    # Fallback: simulate execution
                    for i, step_data in enumerate(resolved_steps):
                        display_step = display_steps[i]  # Masked version for display
                        action = step_data.get("action", "unknown")
                        target = display_step.get("target")  # Masked
                        value = display_step.get("value")  # Masked
                        description = step_data.get("description", f"Step {i + 1}")

                        yield sse_event("step_started", test_case_id=test_case.id, step_number=i + 1, action=action, description=description)

                        await asyncio.sleep(0.2)
                        step_status = StepStatus.PASSED
                        step_error = None
                        step_duration = 100 + (i * 50)

                        crud.create_test_run_step(session, TestRunStepCreate(
                            test_run_id=test_run.id,
                            test_case_id=test_case.id,
                            step_number=i + 1,
                            action=action,
                            target=target,
                            value=value,  # Masked value stored in DB
                            status=step_status,
                            duration=step_duration,
                            error=step_error,
                        ))

                        pass_count += 1

                        yield sse_event("step_completed", test_case_id=test_case.id, step_number=i + 1, status=step_status.value, duration=step_duration)
                else:
                    # Execute via playwright-http
                    execution_options = {"screenshot_on_failure": True}
                    if browser:
                        execution_options["browser"] = browser

                    async for event in executor_client.execute_stream(
                        base_url=project.base_url,
                        steps=resolved_steps,
                        test_id=str(test_case.id),
                        options=execution_options,
                    ):
                        event_type = event.get("type")

                        if event_type == "error":
                            yield sse_error(event.get("error", "Unknown executor error"))
                            error_count += 1
                            break

                        elif event_type == "step_started":
                            step_number = event.get("step_number", 0)
                            # Use masked values from display_steps for frontend
                            step_idx = step_number - 1
                            display_step = display_steps[step_idx] if step_idx < len(display_steps) else {}
                            masked_event = {
                                **event,
                                "test_case_id": test_case.id,
                                "target": display_step.get("target"),
                                "value": display_step.get("value"),
                            }
                            yield f"data: {json.dumps(masked_event)}\n\n"

                        elif event_type == "step_completed":
                            step_number = event.get("step_number", 0)
                            status = event.get("status", "failed")
                            step_status = StepStatus.PASSED if status == "passed" else StepStatus.FAILED

                            # Get step data for this step (use display_steps for masked values)
                            step_idx = step_number - 1
                            display_step = display_steps[step_idx] if step_idx < len(display_steps) else {}

                            # Create step record in DB with masked values
                            crud.create_test_run_step(session, TestRunStepCreate(
                                test_run_id=test_run.id,
                                test_case_id=test_case.id,
                                step_number=step_number,
                                action=display_step.get("action", "unknown"),
                                target=display_step.get("target"),
                                value=display_step.get("value"),  # Masked value
                                status=step_status,
                                duration=event.get("duration", 0),
                                error=event.get("error"),
                                screenshot=event.get("screenshot"),
                            ))

                            if step_status == StepStatus.PASSED:
                                pass_count += 1
                            else:
                                error_count += 1

                            # Use masked values for frontend
                            masked_event = {
                                **event,
                                "test_case_id": test_case.id,
                                "target": display_step.get("target"),
                                "value": display_step.get("value"),
                            }
                            yield f"data: {json.dumps(masked_event)}\n\n"

                        elif event_type == "completed":
                            # Executor signals completion for this test case
                            pass

                # Update test run with consistent summary format
                final_status = RunStatus.PASSED if error_count == 0 else RunStatus.FAILED
                executed_count = pass_count + error_count
                skipped_count = len(resolved_steps) - executed_count
                if skipped_count > 0:
                    summary = f"Executed {executed_count} of {len(resolved_steps)} steps: {pass_count} passed, {error_count} failed, {skipped_count} skipped"
                else:
                    summary = f"Executed {len(resolved_steps)} steps: {pass_count} passed, {error_count} failed"
                crud.update_test_run(session, test_run.id, {
                    "status": final_status,
                    "completed_at": datetime.utcnow(),
                    "pass_count": pass_count,
                    "error_count": error_count,
                    "summary": summary,
                })

                if final_status == RunStatus.PASSED:
                    batch_passed += 1
                else:
                    batch_failed += 1

                # Send test completed event
                yield sse_event("test_completed", test_case_id=test_case.id, run_id=test_run.id, status=final_status.value, pass_count=pass_count, error_count=error_count)

            # Send batch completed event
            yield sse_event("batch_completed", passed=batch_passed, failed=batch_failed, total=len(valid_test_cases), run_ids=run_ids)

    except SQLAlchemyError as e:
        logger.error(f"Database error in run_batch_stream: {e}")
        yield sse_error(f"Database error: {e}")
    except HTTPError as e:
        logger.error(f"Executor connection error: {e}")
        yield sse_error(f"Browser connection error: {e}")
    except json.JSONDecodeError as e:
        yield sse_error(f"Invalid step data: {e}")
    except Exception as e:
        logger.exception("Unexpected error in run_batch_stream")
        yield sse_error(str(e))


@router.post("/project/{project_id}/run-batch/stream")
async def run_batch_streaming(project_id: int, request: BatchRunRequest):
    """
    Execute multiple test cases with SSE streaming.
    Returns step-by-step results for each test case in real-time.

    Optionally specify a browser to use for execution.
    """
    return StreamingResponse(
        run_batch_stream(project_id, request.test_case_ids, browser=request.browser),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )
