"""Test run management API routes."""

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

from db.session import get_session_dep
from db.models import (
    TestRun, TestRunCreate, TestRunRead,
    TestRunStep, TestRunStepCreate, TestRunStepRead,
    RunStatus, RunTrigger, StepStatus,
)
from db import crud
from agent.utils.resolver import resolve_references, mask_passwords_in_steps
from agent.executor_client import PlaywrightExecutorClient
from api.utils.streaming import (
    streaming_context,
    sse_event,
    sse_error,
    sse_warning,
)

logger = logging.getLogger(__name__)

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


class TestRunWithTestCase(TestRunRead):
    """Test run with test case name."""
    test_case_name: Optional[str] = None


@router.get("/project/{project_id}", response_model=List[TestRunWithTestCase])
def list_test_runs(
    project_id: int,
    thread_id: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    session: Session = Depends(get_session_dep)
):
    """List all test runs for a project, optionally filtered by thread_id."""
    if thread_id:
        runs = crud.get_test_runs_by_thread_id(session, project_id, thread_id)
    else:
        runs = crud.get_test_runs_by_project(session, project_id, skip=skip, limit=limit)

    # Add test case name to each run
    result = []
    for run in runs:
        test_case_name = None
        if run.test_case_id:
            test_case = crud.get_test_case(session, run.test_case_id)
            if test_case:
                test_case_name = test_case.name

        result.append(TestRunWithTestCase(
            id=run.id,
            project_id=run.project_id,
            test_case_id=run.test_case_id,
            trigger=run.trigger,
            status=run.status,
            thread_id=run.thread_id,
            started_at=run.started_at,
            completed_at=run.completed_at,
            summary=run.summary,
            error_count=run.error_count,
            pass_count=run.pass_count,
            created_at=run.created_at,
            retry_attempt=run.retry_attempt,
            max_retries=run.max_retries,
            original_run_id=run.original_run_id,
            retry_mode=run.retry_mode,
            retry_reason=run.retry_reason,
            test_case_name=test_case_name,
        ))

    return result


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
    fixture_ids: Optional[List[int]] = None  # Fixture IDs to prepend setup steps


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
            fixture_name=step.get("fixture_name"),
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

def _get_fixture_steps_by_ids(
    session,
    fixture_ids: List[int],
    project_id: int,
) -> tuple[List[dict], List[dict]]:
    """Get resolved fixture steps to prepend to test steps.

    Args:
        session: Database session
        fixture_ids: List of fixture IDs
        project_id: Project ID for resolving references

    Returns:
        Tuple of (resolved_steps, display_steps) for fixtures
    """
    if not fixture_ids:
        return [], []

    # Get fixtures
    fixtures = crud.get_fixtures_by_ids(session, fixture_ids)
    if not fixtures:
        logger.warning(f"No fixtures found for IDs: {fixture_ids}")
        return [], []

    all_fixture_steps = []
    fixture_names = []

    for fixture in fixtures:
        steps = fixture.get_setup_steps()
        if steps:
            # Add fixture_name to each step for tracking
            for step in steps:
                step["fixture_name"] = fixture.name
            all_fixture_steps.extend(steps)
            fixture_names.append(fixture.name)

    if not all_fixture_steps:
        return [], []

    logger.info(f"Prepending {len(all_fixture_steps)} fixture steps from: {', '.join(fixture_names)}")

    # Resolve references in fixture steps
    resolved_steps = resolve_references(session, project_id, all_fixture_steps)
    display_steps = mask_passwords_in_steps(resolved_steps)

    return resolved_steps, display_steps


def _get_fixture_steps_by_ids(
    session,
    fixture_ids: List[int],
    project_id: int,
    browser: Optional[str] = None,
) -> tuple[List[dict], List[dict], bool]:
    """Get resolved fixture steps to prepend to test steps.
    
    Checks for valid cached state first. If cache hit, returns restore_state step.
    If cache miss, returns full fixture steps with capture_state step appended.

    Args:
        session: Database session
        fixture_ids: List of fixture IDs
        project_id: Project ID for resolving references
        browser: Browser type for cache lookup (e.g., 'chromium-headless')

    Returns:
        Tuple of (resolved_steps, display_steps, is_cached)
        - resolved_steps: Steps to execute (restore_state OR full fixture steps + capture_state)
        - display_steps: Steps to display in UI (with passwords masked)
        - is_cached: True if using cached state, False if running fresh fixture
    """
    if not fixture_ids:
        return [], [], False

    # Get fixtures
    fixtures = crud.get_fixtures_by_ids(session, fixture_ids)
    if not fixtures:
        logger.warning(f"No fixtures found for IDs: {fixture_ids}")
        return [], [], False

    # Check if any fixture has cached scope and valid state
    for fixture in fixtures:
        if fixture.scope == "cached":
            # Check for valid cached state
            cached_state = crud.get_valid_fixture_state(session, fixture.id, browser)
            if cached_state:
                # Cache HIT - return restore_state step
                logger.info(f"Using cached state for fixture '{fixture.name}' (ID: {fixture.id})")
                decrypted = crud.get_decrypted_fixture_state(session, cached_state)
                
                import json
                restore_step = {
                    "action": "restore_state",
                    "target": decrypted.get("url"),
                    "value": json.dumps(decrypted),
                    "description": f"Restore cached state from fixture: {fixture.name}",
                    "fixture_name": fixture.name,
                    "is_cached": True,
                }
                
                # Create display step with masked value for UI
                display_step = {
                    "action": "restore_state",
                    "target": decrypted.get("url"),
                    "value": "[cached browser state]",
                    "description": f"Restore cached state from fixture: {fixture.name}",
                    "fixture_name": fixture.name,
                    "is_cached": True,
                }
                
                return [restore_step], [display_step], True

    # Cache MISS - get full fixture steps and add capture_state
    logger.info(f"No valid cache for fixtures {fixture_ids}, running fresh setup")
    
    all_fixture_steps = []
    fixture_names = []

    for fixture in fixtures:
        steps = fixture.get_setup_steps()
        if steps:
            # Add fixture_name to each step for tracking
            for step in steps:
                step["fixture_name"] = fixture.name
            all_fixture_steps.extend(steps)
            fixture_names.append(fixture.name)

    if not all_fixture_steps:
        return [], [], False

    # Add capture_state step at the end for cached fixtures
    has_cached_fixture = any(f.scope == "cached" for f in fixtures)
    if has_cached_fixture:
        capture_step = {
            "action": "capture_state",
            "description": "Capture browser state for fixture caching",
            "fixture_name": fixture_names[0],  # Associate with first fixture
            "_internal": True,  # Mark as internal step (hidden in UI)
        }
        all_fixture_steps.append(capture_step)

    logger.info(f"Prepending {len(all_fixture_steps)} fixture steps from: {', '.join(fixture_names)}")

    # Resolve references in fixture steps
    resolved_steps = resolve_references(session, project_id, all_fixture_steps)
    display_steps = mask_passwords_in_steps(resolved_steps)

    return resolved_steps, display_steps, False


async def execute_steps_stream(
    project_id: int,
    steps: List[ExecuteStepRequest],
    browser: Optional[str] = None,
    fixture_ids: Optional[List[int]] = None,
) -> AsyncGenerator[str, None]:
    """
    Stream step execution results via SSE.
    Each step result is sent as a separate event.

    Args:
        project_id: Project ID
        steps: Steps to execute
        browser: Optional browser ID (e.g., "chrome", "chromium-headless")
        fixture_ids: Optional fixture IDs to prepend setup steps
    """
    try:
        async with streaming_context() as (session, executor_client, use_simulation):
            # Verify project exists
            project = crud.get_project(session, project_id)
            if not project:
                yield sse_error("Project not found")
                return

            if use_simulation:
                yield sse_warning("Playwright executor unavailable, using simulation mode")

            # Resolve persona/page references in steps
            steps_as_dicts = [step.model_dump() for step in steps]
            resolved_steps = resolve_references(session, project_id, steps_as_dicts)
            # Create masked version for display (database storage)
            display_steps = mask_passwords_in_steps(resolved_steps)

            # Prepend fixture steps if fixture_ids provided
            fixtures_cached = False
            if fixture_ids:
                fixture_resolved, fixture_display, fixtures_cached = _get_fixture_steps_by_ids(
                    session, fixture_ids, project_id, browser
                )
                if fixture_resolved:
                    resolved_steps = fixture_resolved + resolved_steps
                    display_steps = fixture_display + display_steps
                    logger.info(f"Total steps after fixture prepend: {len(resolved_steps)} (cached: {fixtures_cached})")

            # Create test run
            test_run = crud.create_test_run(session, TestRunCreate(
                project_id=project_id,
                test_case_id=None,
                trigger=RunTrigger.MANUAL,
                status=RunStatus.RUNNING,
            ))
            crud.update_test_run(session, test_run.id, {"started_at": datetime.utcnow()})

            # Send run started event
            yield sse_event("run_started", run_id=test_run.id)

            pass_count = 0
            error_count = 0

            if use_simulation:
                # Fallback: simulate execution
                for i, step in enumerate(resolved_steps):
                    display_step = display_steps[i]  # Masked version for display
                    action = step.get("action", "unknown")
                    description = step.get("description", "")

                    yield sse_event("step_started", step_number=i + 1, action=action, description=description, fixture_name=display_step.get("fixture_name"))

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
                        fixture_name=display_step.get("fixture_name"),
                    ))

                    pass_count += 1

                    yield sse_event("step_completed", step_number=i + 1, action=action, description=description, status=step_status.value, duration=step_duration, error=step_error, fixture_name=display_step.get("fixture_name"))
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
                            "fixture_name": display_step.get("fixture_name"),
                        }
                        yield f"data: {json.dumps(masked_event)}\n\n"

                    elif event_type == "step_completed":
                        step_number = event.get("step_number", 0)
                        status = event.get("status", "failed")
                        step_status = StepStatus.PASSED if status == "passed" else StepStatus.FAILED

                        # Get step data for this step (use display_steps for masked values)
                        step_idx = step_number - 1
                        display_step = display_steps[step_idx] if step_idx < len(display_steps) else {}
                        action = display_step.get("action", "unknown")

                        # Handle capture_state action - persist browser state for fixture caching
                        if action == "capture_state" and status == "passed" and fixture_ids:
                            result = event.get("result", {})
                            if result and isinstance(result, dict):
                                captured_url = result.get("url")
                                captured_state = result.get("state")
                                
                                if captured_url and captured_state:
                                    from datetime import timedelta
                                    
                                    # Save state for all cached fixtures
                                    fixtures = crud.get_fixtures_by_ids(session, fixture_ids)
                                    for fixture in fixtures:
                                        if fixture.scope == "cached":
                                            expires_at = datetime.utcnow() + timedelta(seconds=fixture.cache_ttl_seconds)
                                            
                                            try:
                                                # Delete any existing state for this fixture
                                                crud.delete_fixture_states_by_fixture(session, fixture.id)
                                                
                                                # Create new cached state
                                                crud.create_fixture_state(
                                                    session=session,
                                                    fixture_id=fixture.id,
                                                    project_id=project_id,
                                                    url=captured_url,
                                                    state_json=json.dumps(captured_state),
                                                    browser=browser,
                                                    expires_at=expires_at,
                                                )
                                                logger.info(f"Cached state for fixture '{fixture.name}' (expires: {expires_at})")
                                            except Exception as e:
                                                logger.error(f"Failed to cache state for fixture '{fixture.name}': {e}")

                        # Create step record in DB with masked values
                        crud.create_test_run_step(session, TestRunStepCreate(
                            test_run_id=test_run.id,
                            test_case_id=None,
                            step_number=step_number,
                            action=action,
                            target=display_step.get("target"),
                            value=display_step.get("value"),  # Masked value
                            status=step_status,
                            duration=event.get("duration", 0),
                            error=event.get("error"),
                            screenshot=event.get("screenshot"),
                            fixture_name=display_step.get("fixture_name"),
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
                            "fixture_name": display_step.get("fixture_name"),
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
            yield sse_event("run_completed", run_id=test_run.id, status=final_status.value, pass_count=pass_count, error_count=error_count, summary=summary)

    except SQLAlchemyError as e:
        logger.error(f"Database error in execute_steps_stream: {e}")
        yield sse_error(f"Database error: {e}")
    except HTTPError as e:
        logger.error(f"Executor connection error: {e}")
        yield sse_error(f"Browser connection error: {e}")
    except json.JSONDecodeError as e:
        yield sse_error(f"Invalid step data: {e}")
    except Exception as e:
        logger.exception("Unexpected error in execute_steps_stream")
        yield sse_error(str(e))


@router.post("/execute/stream")
async def execute_steps_streaming(request: ExecuteRequest):
    """
    Execute steps with SSE streaming.
    Returns step-by-step results in real-time.

    Optionally specify a browser to use for execution.
    Optionally specify fixture_ids to prepend fixture setup steps.
    """
    return StreamingResponse(
        execute_steps_stream(
            request.project_id,
            request.steps,
            browser=request.browser,
            fixture_ids=request.fixture_ids,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )
