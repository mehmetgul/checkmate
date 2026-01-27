"""Test run management API routes."""

import asyncio
import json
from datetime import datetime
from typing import List, Optional, AsyncGenerator
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session

from db.session import get_session_dep, engine
from db.models import (
    TestRun, TestRunCreate, TestRunRead,
    TestRunStep, TestRunStepCreate, TestRunStepRead,
    RunStatus, RunTrigger, StepStatus,
)
from db import crud
from agent.utils.resolver import resolve_references, mask_passwords_in_steps
from agent.executor_client import (
    PlaywrightExecutorClient,
    test_executor_connection,
)

router = APIRouter(prefix="/test-runs", tags=["test-runs"])


class BrowserInfo(BaseModel):
    """Browser information."""
    id: str
    name: str
    headless: bool


class BrowsersResponse(BaseModel):
    """Available browsers response."""
    browsers: List[BrowserInfo]
    default: Optional[str]


@router.get("/browsers", response_model=BrowsersResponse)
async def get_available_browsers():
    """Get available browsers from playwright-http.

    Returns list of browsers that testers can select for test execution.
    """
    client = PlaywrightExecutorClient()
    try:
        data = await client.get_browsers()
        return BrowsersResponse(
            browsers=[BrowserInfo(**b) for b in data.get("browsers", [])],
            default=data.get("default"),
        )
    except Exception as e:
        # Return empty list if executor unavailable
        return BrowsersResponse(browsers=[], default=None)
    finally:
        await client.close()


@router.get("/project/{project_id}", response_model=List[TestRunRead])
def list_test_runs(
    project_id: int,
    skip: int = 0,
    limit: int = 100,
    session: Session = Depends(get_session_dep)
):
    """List all test runs for a project."""
    return crud.get_test_runs_by_project(session, project_id, skip=skip, limit=limit)


@router.post("", response_model=TestRunRead)
def create_test_run(
    test_run: TestRunCreate,
    session: Session = Depends(get_session_dep)
):
    """Create a new test run."""
    # Verify project exists
    project = crud.get_project(session, test_run.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return crud.create_test_run(session, test_run)


@router.get("/{test_run_id}", response_model=TestRunRead)
def get_test_run(
    test_run_id: int,
    session: Session = Depends(get_session_dep)
):
    """Get a test run by ID."""
    test_run = crud.get_test_run(session, test_run_id)
    if not test_run:
        raise HTTPException(status_code=404, detail="Test run not found")
    return test_run


@router.get("/{test_run_id}/steps", response_model=List[TestRunStepRead])
def get_test_run_steps(
    test_run_id: int,
    session: Session = Depends(get_session_dep)
):
    """Get all steps for a test run."""
    test_run = crud.get_test_run(session, test_run_id)
    if not test_run:
        raise HTTPException(status_code=404, detail="Test run not found")
    return crud.get_test_run_steps(session, test_run_id)


@router.put("/{test_run_id}", response_model=TestRunRead)
def update_test_run(
    test_run_id: int,
    data: dict,
    session: Session = Depends(get_session_dep)
):
    """Update a test run."""
    test_run = crud.update_test_run(session, test_run_id, data)
    if not test_run:
        raise HTTPException(status_code=404, detail="Test run not found")
    return test_run


# =============================================================================
# Execute Steps Directly (without saved test case)
# =============================================================================

class ExecuteStepRequest(BaseModel):
    """A single step to execute."""
    action: str
    target: Optional[str] = None
    value: Optional[str] = None
    description: str


class ExecuteRequest(BaseModel):
    """Request to execute steps directly."""
    project_id: int
    steps: List[ExecuteStepRequest]
    browser: Optional[str] = None  # Browser ID (e.g., "chrome", "chromium-headless")


class ExecuteStepResult(BaseModel):
    """Result of a single step execution."""
    step_number: int
    action: str
    target: Optional[str] = None
    value: Optional[str] = None
    description: str
    status: str
    duration: Optional[int] = None
    error: Optional[str] = None


class ExecuteResponse(BaseModel):
    """Response from execute endpoint."""
    run_id: int
    status: str
    pass_count: int
    error_count: int
    steps: List[ExecuteStepResult]
    summary: str


@router.post("/execute", response_model=ExecuteResponse)
def execute_steps(
    request: ExecuteRequest,
    session: Session = Depends(get_session_dep)
):
    """
    Execute a sequence of steps directly without a saved test case.
    Useful for testing steps before saving them as a test case.
    """
    # Verify project exists
    project = crud.get_project(session, request.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Resolve persona/page references in steps
    steps_as_dicts = [step.model_dump() for step in request.steps]
    resolved_steps = resolve_references(session, request.project_id, steps_as_dicts)

    # Create test run (without test_case_id)
    test_run = crud.create_test_run(session, TestRunCreate(
        project_id=request.project_id,
        test_case_id=None,  # No test case - direct execution
        trigger=RunTrigger.MANUAL,
        status=RunStatus.RUNNING,
    ))

    # Update with start time
    crud.update_test_run(session, test_run.id, {"started_at": datetime.utcnow()})

    # Execute steps (simulated for now)
    step_results = []
    pass_count = 0
    error_count = 0

    for i, step in enumerate(resolved_steps):
        # Simulate execution - TODO: integrate with Playwright MCP
        step_status = StepStatus.PASSED
        step_error = None
        step_duration = 100 + (i * 50)  # Simulated duration

        # Create step record (using resolved values)
        db_step = crud.create_test_run_step(session, TestRunStepCreate(
            test_run_id=test_run.id,
            test_case_id=None,
            step_number=i + 1,
            action=step.get("action", "unknown"),
            target=step.get("target"),
            value=step.get("value"),
            status=step_status,
            duration=step_duration,
            error=step_error,
        ))

        step_results.append(ExecuteStepResult(
            step_number=i + 1,
            action=step.get("action", "unknown"),
            target=step.get("target"),
            value=step.get("value"),
            description=step.get("description", ""),
            status=step_status.value,
            duration=step_duration,
            error=step_error,
        ))

        if step_status == StepStatus.PASSED:
            pass_count += 1
        else:
            error_count += 1

    # Update test run with results
    final_status = RunStatus.PASSED if error_count == 0 else RunStatus.FAILED
    crud.update_test_run(session, test_run.id, {
        "status": final_status,
        "completed_at": datetime.utcnow(),
        "pass_count": pass_count,
        "error_count": error_count,
        "summary": f"Executed {len(resolved_steps)} steps: {pass_count} passed, {error_count} failed",
    })

    return ExecuteResponse(
        run_id=test_run.id,
        status=final_status.value,
        pass_count=pass_count,
        error_count=error_count,
        steps=step_results,
        summary=f"Executed {len(resolved_steps)} steps: {pass_count} passed, {error_count} failed",
    )


# =============================================================================
# Execute Steps with SSE Streaming
# =============================================================================

async def execute_steps_stream(
    project_id: int,
    steps: List[ExecuteStepRequest],
    browser: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """
    Stream step execution results via SSE.
    Each step result is sent as a separate event.

    Args:
        project_id: Project ID
        steps: Steps to execute
        browser: Optional browser ID (e.g., "chrome", "chromium-headless")
    """
    # Create a new session for the streaming context
    session = Session(engine)
    executor_client: Optional[PlaywrightExecutorClient] = None
    use_simulation = False

    try:
        # Verify project exists
        project = crud.get_project(session, project_id)
        if not project:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Project not found'})}\n\n"
            return

        # Try to connect to playwright-http
        executor_client = PlaywrightExecutorClient()
        if not await test_executor_connection(executor_client):
            use_simulation = True
            yield f"data: {json.dumps({'type': 'warning', 'message': 'Playwright executor unavailable, using simulation mode'})}\n\n"

        # Resolve persona/page references in steps
        steps_as_dicts = [step.model_dump() for step in steps]
        resolved_steps = resolve_references(session, project_id, steps_as_dicts)
        # Create masked version for display (database storage)
        display_steps = mask_passwords_in_steps(resolved_steps)

        # Create test run
        test_run = crud.create_test_run(session, TestRunCreate(
            project_id=project_id,
            test_case_id=None,
            trigger=RunTrigger.MANUAL,
            status=RunStatus.RUNNING,
        ))
        crud.update_test_run(session, test_run.id, {"started_at": datetime.utcnow()})

        # Send run started event
        yield f"data: {json.dumps({'type': 'run_started', 'run_id': test_run.id})}\n\n"

        pass_count = 0
        error_count = 0

        if use_simulation:
            # Fallback: simulate execution
            for i, step in enumerate(resolved_steps):
                display_step = display_steps[i]  # Masked version for display
                action = step.get("action", "unknown")
                description = step.get("description", "")

                yield f"data: {json.dumps({'type': 'step_started', 'step_number': i + 1, 'action': action, 'description': description})}\n\n"

                await asyncio.sleep(0.3 + (i * 0.1))
                step_status = StepStatus.PASSED
                step_error = None
                step_duration = 100 + (i * 50)

                crud.create_test_run_step(session, TestRunStepCreate(
                    test_run_id=test_run.id,
                    test_case_id=None,
                    step_number=i + 1,
                    action=action,
                    target=display_step.get("target"),  # Masked
                    value=display_step.get("value"),  # Masked value stored in DB
                    status=step_status,
                    duration=step_duration,
                    error=step_error,
                ))

                pass_count += 1

                yield f"data: {json.dumps({'type': 'step_completed', 'step_number': i + 1, 'action': action, 'description': description, 'status': step_status.value, 'duration': step_duration, 'error': step_error})}\n\n"
        else:
            # Execute via playwright-http
            execution_options = {"screenshot_on_failure": True}
            if browser:
                execution_options["browser"] = browser

            async for event in executor_client.execute_stream(
                base_url=project.base_url,
                steps=resolved_steps,
                options=execution_options,
            ):
                event_type = event.get("type")

                if event_type == "error":
                    yield f"data: {json.dumps({'type': 'error', 'message': event.get('error')})}\n\n"
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
                        test_case_id=None,
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
                        "target": display_step.get("target"),
                        "value": display_step.get("value"),
                    }
                    yield f"data: {json.dumps(masked_event)}\n\n"

                elif event_type == "completed":
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
        yield f"data: {json.dumps({'type': 'run_completed', 'run_id': test_run.id, 'status': final_status.value, 'pass_count': pass_count, 'error_count': error_count, 'summary': summary})}\n\n"

        # Commit the transaction
        session.commit()

    except Exception as e:
        session.rollback()
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
    finally:
        if executor_client:
            await executor_client.close()
        session.close()


@router.post("/execute/stream")
async def execute_steps_streaming(request: ExecuteRequest):
    """
    Execute steps with SSE streaming.
    Returns step-by-step results in real-time.

    Optionally specify a browser to use for execution.
    """
    return StreamingResponse(
        execute_steps_stream(request.project_id, request.steps, browser=request.browser),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )
