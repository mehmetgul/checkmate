"""Test case management API routes."""

import asyncio
import json
from datetime import datetime, timedelta
from typing import List, Literal, Optional, AsyncGenerator
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
    Fixture, FixtureScope,
)
from db import crud
from agent.utils.resolver import resolve_references, mask_passwords_in_steps
from agent.nodes.failure_classifier import classify_failure
from api.utils.streaming import (
    streaming_context,
    sse_event,
    sse_error,
    sse_warning,
)
from core.logging import get_logger
from core.config import INTELLIGENT_RETRY_ENABLED

logger = get_logger(__name__)

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


class StatusUpdateRequest(BaseModel):
    status: str


@router.patch("/{test_case_id}/status", response_model=TestCaseRead)
def update_test_case_status(
    test_case_id: int,
    request: StatusUpdateRequest,
    session: Session = Depends(get_session_dep),
):
    """Update test case status with transition validation.

    Valid transitions:
      draft -> ready (only if steps exist)
      ready -> in_review
      in_review -> approved or back to draft
      any -> archived
    """
    try:
        result = crud.update_test_case_status(session, test_case_id, request.status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not result:
        raise HTTPException(status_code=404, detail="Test case not found")
    return result


class VisibilityUpdateRequest(BaseModel):
    visibility: str  # "private" | "public"


@router.patch("/{test_case_id}/visibility", response_model=TestCaseRead)
def update_test_case_visibility(
    test_case_id: int,
    request: VisibilityUpdateRequest,
    session: Session = Depends(get_session_dep),
):
    """Toggle test case visibility between private and public."""
    try:
        result = crud.update_test_case_visibility(session, test_case_id, request.visibility)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not result:
        raise HTTPException(status_code=404, detail="Test case not found")
    return result


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
    # Retry tracking
    retry_attempt: int = 0
    max_retries: int = 0
    original_run_id: Optional[int] = None
    retry_mode: Optional[str] = None
    retry_reason: Optional[str] = None


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
            retry_attempt=run.retry_attempt,
            max_retries=run.max_retries,
            original_run_id=run.original_run_id,
            retry_mode=run.retry_mode,
            retry_reason=run.retry_reason,
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
            fixture_name=step_data.get("fixture_name"),
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

class RetryConfig(BaseModel):
    """Configuration for test-level retry."""
    max_retries: int = 0  # Number of retry attempts (0 = no retry)
    retry_mode: Literal["intelligent", "simple"] = "intelligent"  # intelligent uses LLM, simple always retries


class ViewportConfig(BaseModel):
    """Viewport size for browser."""
    width: int = 1280
    height: int = 720


def _get_fixture_steps(
    session,
    test_case,
    project_id: int,
    browser: Optional[str] = None,
) -> tuple[List[dict], List[dict], bool]:
    """Get resolved fixture steps to prepend to test steps.
    
    Checks for valid cached state first. If cache hit, returns restore_state step.
    If cache miss, returns full fixture steps with capture_state step appended.

    Args:
        session: Database session
        test_case: Test case with fixture_ids
        project_id: Project ID for resolving references
        browser: Browser type for cache lookup (e.g., 'chromium-headless')

    Returns:
        Tuple of (resolved_steps, display_steps, is_cached)
        - resolved_steps: Steps to execute (restore_state OR full fixture steps + capture_state)
        - display_steps: Steps to display in UI (with passwords masked)
        - is_cached: True if using cached state, False if running fresh fixture
    """
    fixture_ids = test_case.get_fixture_ids()
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
            logger.info(f"Checking cache for fixture '{fixture.name}' (ID: {fixture.id}, browser: {browser})")
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
def _merge_browser_state(target: dict, source: dict) -> None:
    """Merge source browser state into target."""
    if source.get("cookies"):
        target["cookies"].extend(source["cookies"])
    if source.get("local_storage"):
        target["local_storage"].update(source["local_storage"])
    if source.get("session_storage"):
        target["session_storage"].update(source["session_storage"])


async def _execute_single_run(
    session,
    executor_client,
    use_simulation: bool,
    test_case,
    project,
    resolved_steps: list,
    display_steps: list,
    browser: Optional[str],
    fixture_ids: Optional[list] = None,
    viewport: Optional[dict] = None,
    retry_attempt: int = 0,
    max_retries: int = 0,
    original_run_id: Optional[int] = None,
    retry_mode: Optional[str] = None,
    retry_reason: Optional[str] = None,
    thread_id: Optional[str] = None,
    batch_label: Optional[str] = None,
    env_base_url: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Execute a single test run and yield SSE events.

    Returns a generator that yields SSE events. The final event will be
    run_completed with the test results.

    Also yields a special _run_result event (not sent to client) containing
    the failure info needed for retry decisions.

    Fixture steps are prepended to test steps before calling this function,
    so everything runs in one fresh browser session.
    """
    test_case_id = test_case.id

    # Create test run with retry tracking
    test_run = crud.create_test_run(session, TestRunCreate(
        project_id=test_case.project_id,
        test_case_id=test_case_id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.RUNNING,
        thread_id=thread_id,
        batch_label=batch_label,
        browser=browser,
    ))
    crud.update_test_run(session, test_run.id, {
        "started_at": datetime.utcnow(),
        "retry_attempt": retry_attempt,
        "max_retries": max_retries,
        "original_run_id": original_run_id,
        "retry_mode": retry_mode,
        "retry_reason": retry_reason,
    })

    logger.info(f"Created test run: run_id={test_run.id}, attempt={retry_attempt + 1}/{max_retries + 1}, steps={len(resolved_steps)}")

    # Send run started event
    yield sse_event(
        "run_started",
        run_id=test_run.id,
        test_case_id=test_case_id,
        total_steps=len(resolved_steps),
        retry_attempt=retry_attempt,
        max_retries=max_retries,
        original_run_id=original_run_id,
        browser=browser,
    )

    pass_count = 0
    error_count = 0
    failure_info = None  # Store failure info for retry decisions

    if use_simulation:
        # Fallback: simulate execution
        for i, step_data in enumerate(resolved_steps):
            display_step = display_steps[i]
            action = step_data.get("action", "unknown")
            target = display_step.get("target")
            value = display_step.get("value")
            description = step_data.get("description", f"Step {i + 1}")

            logger.info(f"Executing step {i + 1}/{len(resolved_steps)}: {action} - {description[:50]}")
            yield sse_event("step_started", step_number=i + 1, action=action, description=description, fixture_name=display_step.get("fixture_name"))

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
                value=value,
                status=step_status,
                duration=step_duration,
                error=step_error,
                fixture_name=display_step.get("fixture_name"),
            ))

            pass_count += 1
            yield sse_event("step_completed", step_number=i + 1, action=action, description=description, status=step_status.value, duration=step_duration, error=step_error, fixture_name=display_step.get("fixture_name"))
    else:
        # Execute via playwright-http (fixture steps are prepended, runs fresh every time)
        effective_base_url = env_base_url or project.base_url
        logger.info(f"Executing via playwright-http: base_url={effective_base_url}, steps={len(resolved_steps)}")
        execution_options = {"screenshot_on_failure": True}
        if browser:
            execution_options["browser"] = browser
        if viewport:
            execution_options["viewport"] = viewport

        async for event in executor_client.execute_stream(
            base_url=effective_base_url,
            steps=resolved_steps,
            test_id=str(test_case_id),
            options=execution_options,
        ):
            event_type = event.get("type")

            if event_type == "error":
                logger.error(f"Executor error: {event.get('error')}")
                yield sse_error(event.get("error", "Unknown executor error"))
                break

            elif event_type == "step_started":
                step_number = event.get("step_number", 0)
                logger.info(f"Executing step {step_number}/{len(resolved_steps)}: {event.get('action', 'unknown')}")
                step_idx = step_number - 1
                display_step = display_steps[step_idx] if step_idx < len(display_steps) else {}
                masked_event = {
                    **event,
                    "target": display_step.get("target"),
                    "value": display_step.get("value"),
                    "fixture_name": display_step.get("fixture_name"),
                }
                yield f"data: {json.dumps(masked_event)}\n\n"

            elif event_type == "step_retry":
                # Forward step retry event from playwright-http
                step_number = event.get("step_number", 0)
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
                duration = event.get("duration", 0)

                if step_status == StepStatus.PASSED:
                    logger.info(f"Step {step_number} passed ({duration}ms)")
                else:
                    logger.warning(f"Step {step_number} failed: {event.get('error', 'unknown error')}")

                step_idx = step_number - 1
                display_step = display_steps[step_idx] if step_idx < len(display_steps) else {}
                action = display_step.get("action", "unknown")

                # Handle capture_state action - persist browser state for fixture caching
                if action == "capture_state" and status == "passed" and fixture_ids:
                    result = event.get("result", {})
                    logger.info(f"capture_state completed. Result type: {type(result)}, Result: {result}")
                    if result and isinstance(result, dict):
                        captured_url = result.get("url")
                        captured_state = result.get("state")
                        logger.info(f"Extracted from result: url={captured_url}, state present={bool(captured_state)}")
                        
                        if captured_url and captured_state:
                            from datetime import timedelta
                            
                            logger.info(f"Attempting to cache state for fixture_ids: {fixture_ids}")
                            # Save state for all cached fixtures
                            fixtures = crud.get_fixtures_by_ids(session, fixture_ids)
                            logger.info(f"Found {len(fixtures)} fixtures to cache")
                            for fixture in fixtures:
                                if fixture.scope == "cached":
                                    logger.info(f"Saving cache for fixture '{fixture.name}' (ID: {fixture.id})")
                                    expires_at = datetime.utcnow() + timedelta(seconds=fixture.cache_ttl_seconds)
                                    
                                    try:
                                        # Delete any existing state for this fixture
                                        crud.delete_fixture_states_by_fixture(session, fixture.id)
                                        
                                        # Create new cached state
                                        crud.create_fixture_state(
                                            session=session,
                                            fixture_id=fixture.id,
                                            project_id=test_case.project_id,
                                            url=captured_url,
                                            state_json=json.dumps(captured_state),
                                            browser=browser,
                                            expires_at=expires_at,
                                        )
                                        logger.info(f"Cached state for fixture '{fixture.name}' (expires: {expires_at})")
                                    except Exception as e:
                                        logger.error(f"Failed to cache state for fixture '{fixture.name}': {e}")

                crud.create_test_run_step(session, TestRunStepCreate(
                    test_run_id=test_run.id,
                    test_case_id=test_case_id,
                    step_number=step_number,
                    action=action,
                    target=display_step.get("target"),
                    value=display_step.get("value"),
                    status=step_status,
                    duration=duration,
                    error=event.get("error"),
                    screenshot=event.get("screenshot"),
                    fixture_name=display_step.get("fixture_name"),
                ))

                if step_status == StepStatus.PASSED:
                    pass_count += 1
                else:
                    error_count += 1
                    # Capture failure info for retry decision
                    failure_info = {
                        "action": display_step.get("action", "unknown"),
                        "target": display_step.get("target"),
                        "value": display_step.get("value"),
                        "error": event.get("error"),
                        "screenshot": event.get("screenshot"),
                    }

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

    logger.info(f"Test run completed: run_id={test_run.id}, status={final_status.value}, passed={pass_count}, failed={error_count}")

    # Send run completed event
    yield sse_event(
        "run_completed",
        run_id=test_run.id,
        status=final_status.value,
        pass_count=pass_count,
        error_count=error_count,
        summary=summary,
        retry_attempt=retry_attempt,
        max_retries=max_retries,
    )

    # Yield internal result for retry logic (prefixed with _ to indicate internal)
    yield json.dumps({
        "_internal": True,
        "run_id": test_run.id,
        "status": final_status.value,
        "failure_info": failure_info,
    })


async def run_test_case_stream(
    test_case_id: int,
    browser: Optional[str] = None,
    viewport: Optional[dict] = None,
    retry_config: Optional[RetryConfig] = None,
    environment_id: Optional[int] = None,
) -> AsyncGenerator[str, None]:
    """
    Stream test case execution results via SSE.
    Each step result is sent as a separate event.

    Args:
        test_case_id: ID of test case to run
        browser: Optional browser ID (e.g., "chrome", "chromium-headless")
        viewport: Optional viewport size {"width": int, "height": int}
        retry_config: Optional retry configuration
    """
    max_retries = retry_config.max_retries if retry_config else 0
    retry_mode = retry_config.retry_mode if retry_config else "simple"

    logger.info(f"Starting test case execution: test_case_id={test_case_id}, browser={browser or 'default'}, max_retries={max_retries}, mode={retry_mode}")

    try:
        async with streaming_context() as (session, executor_client, use_simulation):
            # Get test case
            test_case = crud.get_test_case(session, test_case_id)
            if not test_case:
                logger.warning(f"Test case not found: {test_case_id}")
                yield sse_error("Test case not found")
                return

            # Get project for base_url
            project = crud.get_project(session, test_case.project_id)
            if not project:
                yield sse_error("Project not found")
                return

            # Load active environment (if specified)
            env_vars: dict = {}
            env_base_url: Optional[str] = None
            if environment_id:
                env = crud.get_environment(session, environment_id)
                if env and env.project_id == test_case.project_id:
                    env_vars = env.get_variables()
                    env_base_url = env.base_url
                    logger.info(f"Using environment '{env.name}' (base_url={env_base_url})")

            # Parse steps from test case
            try:
                steps_data = json.loads(test_case.steps) if isinstance(test_case.steps, str) else test_case.steps
            except json.JSONDecodeError:
                steps_data = []

            if not steps_data:
                yield sse_error("No steps defined in test case")
                return

            if use_simulation:
                logger.info("Playwright executor unavailable, using simulation mode")
                yield sse_warning("Playwright executor unavailable, using simulation mode")

            # Resolve persona/page/env references in steps
            resolved_steps = resolve_references(
                session, test_case.project_id, steps_data,
                env_vars=env_vars, override_base_url=env_base_url,
                environment_id=environment_id,
            )
            display_steps = mask_passwords_in_steps(resolved_steps)

            # Handle fixtures - prepend fixture steps to test steps (fresh every time)
            fixture_ids = test_case.get_fixture_ids()
            if fixture_ids and not use_simulation:
                logger.info(f"Test case has fixtures: {fixture_ids}")
                yield sse_event("fixtures_loading", fixture_ids=fixture_ids)

                try:
                    fixture_resolved, fixture_display, fixtures_cached = _get_fixture_steps(
                        session=session,
                        test_case=test_case,
                        project_id=test_case.project_id,
                        browser=browser,
                    )
                    if fixture_resolved:
                        # Prepend fixture steps to test steps
                        resolved_steps = fixture_resolved + resolved_steps
                        display_steps = fixture_display + display_steps
                        yield sse_event(
                            "fixtures_loaded",
                            fixture_steps=len(fixture_resolved),
                            total_steps=len(resolved_steps),
                            fixtures_cached=fixtures_cached,
                        )
                except Exception as e:
                    logger.error(f"Failed to load fixtures: {e}")
                    yield sse_warning(f"Failed to load fixtures: {e}")
                    # Continue without fixtures

            original_run_id = None
            current_attempt = 0

            while current_attempt <= max_retries:
                retry_reason = None

                # Execute the test run
                internal_result = None
                async for event in _execute_single_run(
                    session=session,
                    executor_client=executor_client,
                    use_simulation=use_simulation,
                    test_case=test_case,
                    project=project,
                    resolved_steps=resolved_steps,
                    display_steps=display_steps,
                    browser=browser,
                    viewport=viewport,
                    retry_attempt=current_attempt,
                    max_retries=max_retries,
                    original_run_id=original_run_id,
                    retry_mode=retry_mode if max_retries > 0 else None,
                    retry_reason=retry_reason,
                    fixture_ids=fixture_ids,
                    env_base_url=env_base_url,
                ):
                    # Check if this is the internal result
                    if event.startswith("{") and "_internal" in event:
                        internal_result = json.loads(event)
                    else:
                        yield event

                if not internal_result:
                    break

                # Track original run ID for linking retries
                if original_run_id is None:
                    original_run_id = internal_result["run_id"]

                # Check if test passed or no more retries
                if internal_result["status"] == "passed":
                    break

                if current_attempt >= max_retries:
                    break

                # Test failed - determine if we should retry
                failure_info = internal_result.get("failure_info")

                if retry_mode == "intelligent" and failure_info:
                    # Use LLM to classify failure
                    logger.info("Classifying failure for intelligent retry...")
                    classification = await classify_failure(
                        action=failure_info.get("action", ""),
                        target=failure_info.get("target"),
                        value=failure_info.get("value"),
                        error_message=failure_info.get("error", ""),
                        screenshot_b64=failure_info.get("screenshot"),
                    )

                    if not classification.is_retryable:
                        # Emit retry_skipped event
                        logger.info(f"Retry skipped: {classification.failure_category} - {classification.reasoning}")
                        yield sse_event(
                            "retry_skipped",
                            run_id=internal_result["run_id"],
                            reason=f"Non-retryable: {classification.failure_category}",
                            details=classification.reasoning,
                            confidence=classification.confidence,
                        )
                        break

                    retry_reason = f"{classification.failure_category}: {classification.reasoning}"
                else:
                    # Simple mode - always retry
                    retry_reason = "simple retry mode"

                # Emit test_retry event before retrying
                current_attempt += 1
                logger.info(f"Retrying test (attempt {current_attempt + 1}/{max_retries + 1}): {retry_reason}")
                yield sse_event(
                    "test_retry",
                    run_id=internal_result["run_id"],
                    attempt=current_attempt + 1,
                    max_attempts=max_retries + 1,
                    reason=retry_reason,
                )

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
    viewport: Optional[ViewportConfig] = None  # Browser viewport size
    retry: Optional[RetryConfig] = None  # Retry configuration
    environment_id: Optional[int] = None  # Active environment ID


@router.post("/{test_case_id}/runs/stream")
async def run_test_case_streaming(test_case_id: int, request: Optional[RunTestCaseRequest] = None):
    """
    Execute a test case with SSE streaming.
    Returns step-by-step results in real-time.

    Optionally specify:
    - browser: Browser ID to use for execution
    - viewport: Browser viewport size {width, height}
    - retry: Retry configuration with max_retries and retry_mode
    - environment_id: Active environment whose base_url and variables override project defaults
    """
    browser = request.browser if request else None
    viewport = request.viewport.model_dump() if request and request.viewport else None
    retry_config = request.retry if request else None
    environment_id = request.environment_id if request else None

    # Validate intelligent retry mode is enabled
    if retry_config and retry_config.retry_mode == "intelligent" and not INTELLIGENT_RETRY_ENABLED:
        raise HTTPException(
            status_code=400,
            detail="Intelligent retry is not enabled on this deployment. Use retry_mode='simple' or contact your administrator."
        )

    return StreamingResponse(
        run_test_case_stream(test_case_id, browser=browser, viewport=viewport, retry_config=retry_config, environment_id=environment_id),
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
    browser: Optional[str] = None  # Legacy single-browser ID (e.g., "chrome", "chromium-headless")
    browsers: List[str] = []  # Multi-browser IDs for cross-browser execution (takes precedence over browser)
    viewport: Optional[ViewportConfig] = None  # Browser viewport size
    retry: Optional[RetryConfig] = None  # Retry configuration
    parallel: int = 1  # Max concurrent tests per browser (1-5, default=1 = sequential)
    environment_id: Optional[int] = None  # Active environment ID
    context: Optional[str] = None  # Execution context label (e.g., "All Scenarios", folder name)


def _enrich_event(sse_string: str, test_case_id: int, browser: Optional[str] = None) -> str:
    """Inject test_case_id and browser into an SSE event for frontend correlation during parallel execution."""
    if not sse_string.startswith("data: "):
        return sse_string
    try:
        payload = json.loads(sse_string[6:].strip())
        if "test_case_id" not in payload:
            payload["test_case_id"] = test_case_id
        if browser and "browser" not in payload:
            payload["browser"] = browser
        return f"data: {json.dumps(payload)}\n\n"
    except (json.JSONDecodeError, ValueError):
        return sse_string


async def _run_browser_batch(
    browser: str,
    test_cases: list,
    parallel: int,
    event_queue: "asyncio.Queue",
    counters: dict,
    executor_client,
    use_simulation: bool,
    project,
    batch_env_vars: dict,
    batch_env_base_url: Optional[str],
    environment_id: Optional[int],
    max_retries: int,
    retry_mode: str,
    batch_id: str,
    context: Optional[str],
    viewport: Optional[dict],
    project_id: int,
) -> None:
    """Execute all test cases for one browser, pushing SSE events to a shared queue.

    Each browser gets its own asyncio.Semaphore for concurrency control.
    Does NOT put a sentinel on the queue — the caller manages that.
    counters dict is mutated directly (safe because asyncio is single-threaded cooperative).
    """
    semaphore = asyncio.Semaphore(parallel)

    async def _run_worker(idx: int, test_case):
        worker_session = Session(engine)
        tc_id = test_case.id
        try:
            async with semaphore:
                await event_queue.put(
                    _enrich_event(
                        sse_event("test_started", test_case_id=tc_id, name=test_case.name, index=idx + 1, total=len(test_cases)),
                        tc_id, browser=browser,
                    )
                )

                try:
                    steps_data = json.loads(test_case.steps) if isinstance(test_case.steps, str) else test_case.steps
                except json.JSONDecodeError:
                    steps_data = []

                if not steps_data:
                    await event_queue.put(
                        _enrich_event(
                            sse_event("test_completed", test_case_id=tc_id, status="skipped", message="No steps defined"),
                            tc_id, browser=browser,
                        )
                    )
                    return

                resolved_steps = resolve_references(worker_session, project_id, steps_data, env_vars=batch_env_vars, override_base_url=batch_env_base_url, environment_id=environment_id)
                display_steps = mask_passwords_in_steps(resolved_steps)

                original_run_id = None
                current_attempt = 0
                final_internal = None

                while current_attempt <= max_retries:
                    retry_reason = None
                    internal_result = None

                    async for event in _execute_single_run(
                        session=worker_session,
                        executor_client=executor_client,
                        use_simulation=use_simulation,
                        test_case=test_case,
                        project=project,
                        resolved_steps=resolved_steps,
                        display_steps=display_steps,
                        browser=browser,
                        viewport=viewport,
                        retry_attempt=current_attempt,
                        max_retries=max_retries,
                        original_run_id=original_run_id,
                        retry_mode=retry_mode if max_retries > 0 else None,
                        retry_reason=retry_reason,
                        thread_id=batch_id,
                        batch_label=context,
                        env_base_url=batch_env_base_url,
                    ):
                        if event.startswith("{") and "_internal" in event:
                            internal_result = json.loads(event)
                        else:
                            await event_queue.put(_enrich_event(event, tc_id, browser=browser))

                    if not internal_result:
                        break

                    final_internal = internal_result
                    counters["run_ids"].append(internal_result["run_id"])

                    if original_run_id is None:
                        original_run_id = internal_result["run_id"]

                    if internal_result["status"] == "passed":
                        break

                    if current_attempt >= max_retries:
                        break

                    failure_info = internal_result.get("failure_info")

                    if retry_mode == "intelligent" and failure_info:
                        classification = await classify_failure(
                            action=failure_info.get("action", ""),
                            target=failure_info.get("target"),
                            value=failure_info.get("value"),
                            error_message=failure_info.get("error", ""),
                            screenshot_b64=failure_info.get("screenshot"),
                        )

                        if not classification.is_retryable:
                            logger.info(f"Batch retry skipped for tc {tc_id} on {browser}: {classification.failure_category}")
                            await event_queue.put(_enrich_event(sse_event(
                                "retry_skipped",
                                test_case_id=tc_id,
                                run_id=internal_result["run_id"],
                                reason=f"Non-retryable: {classification.failure_category}",
                                details=classification.reasoning,
                                confidence=classification.confidence,
                            ), tc_id, browser=browser))
                            break

                        retry_reason = f"{classification.failure_category}: {classification.reasoning}"
                    else:
                        retry_reason = "simple retry mode"

                    current_attempt += 1
                    logger.info(f"Batch retrying tc {tc_id} on {browser} (attempt {current_attempt + 1}/{max_retries + 1}): {retry_reason}")
                    await event_queue.put(_enrich_event(sse_event(
                        "test_retry",
                        test_case_id=tc_id,
                        run_id=internal_result["run_id"],
                        attempt=current_attempt + 1,
                        max_attempts=max_retries + 1,
                        reason=retry_reason,
                    ), tc_id, browser=browser))

                if final_internal:
                    if final_internal["status"] == "passed":
                        counters["passed"] += 1
                    else:
                        counters["failed"] += 1

                    await event_queue.put(_enrich_event(
                        sse_event("test_completed", test_case_id=tc_id, run_id=final_internal["run_id"], status=final_internal["status"]),
                        tc_id, browser=browser,
                    ))

        except Exception as worker_err:
            logger.error(f"Worker error for tc {tc_id} on {browser}: {worker_err}")
            await event_queue.put(_enrich_event(sse_error(f"Error running test case {tc_id}: {worker_err}"), tc_id, browser=browser))
        finally:
            try:
                worker_session.commit()
            except Exception:
                worker_session.rollback()
            worker_session.close()

    tasks = [
        asyncio.create_task(_run_worker(idx, tc))
        for idx, tc in enumerate(test_cases)
    ]
    await asyncio.gather(*tasks, return_exceptions=True)


async def run_batch_stream(project_id: int, test_case_ids: List[int], browser: Optional[str] = None, browsers: Optional[List[str]] = None, viewport: Optional[dict] = None, retry_config: Optional[RetryConfig] = None, parallel: int = 1, context: Optional[str] = None, environment_id: Optional[int] = None) -> AsyncGenerator[str, None]:
    """
    Stream batch test execution results via SSE.
    Supports per-test retry and optional multi-browser cross-browser execution.

    `browsers` (multi-browser list) takes precedence over `browser` (legacy single).
    `parallel` controls concurrency per browser — total sessions = N_tests × N_browsers × parallel.

    Args:
        browsers: List of browser IDs for cross-browser execution (preferred)
        browser: Legacy single-browser ID (fallback when browsers is empty/None)
        parallel: Max concurrent tests per browser (1 = sequential, 2-5 = parallel workers)
    """
    import uuid

    max_retries = retry_config.max_retries if retry_config else 0
    retry_mode = retry_config.retry_mode if retry_config else "simple"

    # Resolve effective browsers: new multi-browser list > legacy single > default
    effective_browsers = browsers if browsers else ([browser] if browser else ["chromium"])

    logger.info(f"Starting batch execution: project_id={project_id}, test_cases={len(test_case_ids)}, browsers={effective_browsers}, max_retries={max_retries}, mode={retry_mode}, parallel={parallel}")
    try:
        async with streaming_context() as (session, executor_client, use_simulation):
            # Verify project exists
            project = crud.get_project(session, project_id)
            if not project:
                yield sse_error("Project not found")
                return

            # Load active environment (if specified)
            batch_env_vars: dict = {}
            batch_env_base_url: Optional[str] = None
            if environment_id:
                env = crud.get_environment(session, environment_id)
                if env and env.project_id == project_id:
                    batch_env_vars = env.get_variables()
                    batch_env_base_url = env.base_url
                    logger.info(f"Batch using environment '{env.name}' (base_url={batch_env_base_url})")

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
                logger.info("Batch: Playwright executor unavailable, using simulation mode")
                yield sse_warning("Playwright executor unavailable, using simulation mode")

            # Generate batch ID to group these runs
            batch_id = f"batch-{uuid.uuid4().hex[:8]}"
            total_tests = len(valid_test_cases) * len(effective_browsers)
            logger.info(f"Batch started: batch_id={batch_id}, test_cases={len(valid_test_cases)}, browsers={effective_browsers}")

            # Send batch started event
            yield sse_event("batch_started", batch_id=batch_id, total_tests=total_tests, browsers=effective_browsers, test_case_ids=[tc.id for tc in valid_test_cases])

            # Shared counters — safe without locks because asyncio is single-threaded cooperative
            counters: dict = {"passed": 0, "failed": 0, "run_ids": []}

            if len(effective_browsers) == 1 and parallel <= 1:
                # ── Sequential path (preserved for zero-regression on existing usage) ──
                eff_browser = effective_browsers[0]
                for idx, test_case in enumerate(valid_test_cases):
                    yield sse_event("test_started", test_case_id=test_case.id, name=test_case.name, index=idx + 1, total=len(valid_test_cases))

                    try:
                        steps_data = json.loads(test_case.steps) if isinstance(test_case.steps, str) else test_case.steps
                    except json.JSONDecodeError:
                        steps_data = []

                    if not steps_data:
                        yield sse_event("test_completed", test_case_id=test_case.id, status="skipped", message="No steps defined")
                        continue

                    resolved_steps = resolve_references(session, project_id, steps_data, env_vars=batch_env_vars, override_base_url=batch_env_base_url, environment_id=environment_id)
                    display_steps = mask_passwords_in_steps(resolved_steps)

                    original_run_id = None
                    current_attempt = 0
                    final_internal = None

                    while current_attempt <= max_retries:
                        retry_reason = None

                        internal_result = None
                        async for event in _execute_single_run(
                            session=session,
                            executor_client=executor_client,
                            use_simulation=use_simulation,
                            test_case=test_case,
                            project=project,
                            resolved_steps=resolved_steps,
                            display_steps=display_steps,
                            browser=eff_browser,
                            viewport=viewport,
                            retry_attempt=current_attempt,
                            max_retries=max_retries,
                            original_run_id=original_run_id,
                            retry_mode=retry_mode if max_retries > 0 else None,
                            retry_reason=retry_reason,
                            thread_id=batch_id,
                            batch_label=context,
                            env_base_url=batch_env_base_url,
                        ):
                            if event.startswith("{") and "_internal" in event:
                                internal_result = json.loads(event)
                            else:
                                yield event

                        if not internal_result:
                            break

                        final_internal = internal_result
                        counters["run_ids"].append(internal_result["run_id"])

                        if original_run_id is None:
                            original_run_id = internal_result["run_id"]

                        if internal_result["status"] == "passed":
                            break

                        if current_attempt >= max_retries:
                            break

                        failure_info = internal_result.get("failure_info")

                        if retry_mode == "intelligent" and failure_info:
                            classification = await classify_failure(
                                action=failure_info.get("action", ""),
                                target=failure_info.get("target"),
                                value=failure_info.get("value"),
                                error_message=failure_info.get("error", ""),
                                screenshot_b64=failure_info.get("screenshot"),
                            )

                            if not classification.is_retryable:
                                logger.info(f"Batch retry skipped for tc {test_case.id}: {classification.failure_category}")
                                yield sse_event(
                                    "retry_skipped",
                                    test_case_id=test_case.id,
                                    run_id=internal_result["run_id"],
                                    reason=f"Non-retryable: {classification.failure_category}",
                                    details=classification.reasoning,
                                    confidence=classification.confidence,
                                )
                                break

                            retry_reason = f"{classification.failure_category}: {classification.reasoning}"
                        else:
                            retry_reason = "simple retry mode"

                        current_attempt += 1
                        logger.info(f"Batch retrying tc {test_case.id} (attempt {current_attempt + 1}/{max_retries + 1}): {retry_reason}")
                        yield sse_event(
                            "test_retry",
                            test_case_id=test_case.id,
                            run_id=internal_result["run_id"],
                            attempt=current_attempt + 1,
                            max_attempts=max_retries + 1,
                            reason=retry_reason,
                        )

                    if final_internal:
                        if final_internal["status"] == "passed":
                            counters["passed"] += 1
                        else:
                            counters["failed"] += 1

                        yield sse_event("test_completed", test_case_id=test_case.id, run_id=final_internal["run_id"], status=final_internal["status"])

            else:
                # ── Parallel / multi-browser path ──
                # Each browser gets its own _run_browser_batch task with its own Semaphore.
                # All SSE events flow into one shared queue → single stream to the frontend.
                event_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()

                browser_tasks = [
                    asyncio.create_task(_run_browser_batch(
                        browser=b,
                        test_cases=valid_test_cases,
                        parallel=parallel,
                        event_queue=event_queue,
                        counters=counters,
                        executor_client=executor_client,
                        use_simulation=use_simulation,
                        project=project,
                        batch_env_vars=batch_env_vars,
                        batch_env_base_url=batch_env_base_url,
                        environment_id=environment_id,
                        max_retries=max_retries,
                        retry_mode=retry_mode,
                        batch_id=batch_id,
                        context=context,
                        viewport=viewport,
                        project_id=project_id,
                    ))
                    for b in effective_browsers
                ]

                # Sentinel: wait for all browser tasks, then close the queue
                async def _sentinel():
                    await asyncio.gather(*browser_tasks, return_exceptions=True)
                    await event_queue.put(None)

                asyncio.create_task(_sentinel())

                # Drain the queue and yield events
                while True:
                    event = await event_queue.get()
                    if event is None:
                        break
                    yield event

            # Send batch completed event
            logger.info(f"Batch completed: batch_id={batch_id}, passed={counters['passed']}, failed={counters['failed']}, browsers={effective_browsers}")
            yield sse_event("batch_completed", passed=counters["passed"], failed=counters["failed"], total=total_tests, run_ids=counters["run_ids"], browsers=effective_browsers)

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

    Optionally specify a browser and retry configuration.
    """
    retry_config = request.retry

    # Validate intelligent retry mode is enabled
    if retry_config and retry_config.retry_mode == "intelligent" and not INTELLIGENT_RETRY_ENABLED:
        raise HTTPException(
            status_code=400,
            detail="Intelligent retry is not enabled on this deployment. Use retry_mode='simple' or contact your administrator."
        )

    parallel = max(1, min(request.parallel, 5))

    return StreamingResponse(
        run_batch_stream(
            project_id,
            request.test_case_ids,
            browser=request.browser,
            browsers=request.browsers if request.browsers else None,
            viewport=request.viewport.model_dump() if request.viewport else None,
            retry_config=retry_config,
            parallel=parallel,
            context=request.context,
            environment_id=request.environment_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )
