"""Fixture management API routes."""

import json
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session

from db.session import get_session_dep
from db.models import (
    Fixture,
    FixtureCreate,
    FixtureRead,
    FixtureUpdate,
    FixtureState,
    FixtureStateRead,
    FixtureScope,
)
from db import crud
from core.logging import get_logger

logger = get_logger(__name__)

# Router for project-scoped fixture endpoints
router = APIRouter(prefix="/projects/{project_id}/fixtures", tags=["fixtures"])

# Router for fixture-specific endpoints (not nested under project)
fixture_router = APIRouter(prefix="/fixtures", tags=["fixtures"])


# --- Request/Response Models ---

class FixtureCreateRequest(BaseModel):
    """Request body for creating a fixture."""
    name: str
    description: Optional[str] = None
    setup_steps: List[dict]  # Array of step objects
    scope: str = "cached"  # test or cached
    cache_ttl_seconds: int = 3600  # Default 1 hour


class FixtureUpdateRequest(BaseModel):
    """Request body for updating a fixture."""
    name: Optional[str] = None
    description: Optional[str] = None
    setup_steps: Optional[List[dict]] = None
    scope: Optional[str] = None
    cache_ttl_seconds: Optional[int] = None


class FixtureGenerateRequest(BaseModel):
    """Request body for generating fixture steps from NLP."""
    prompt: str  # Natural language description of setup
    name: Optional[str] = None  # Optional name (can be generated)


class FixtureReadWithState(FixtureRead):
    """Fixture read model with cache state info."""
    has_valid_cache: bool = False
    cache_expires_at: Optional[datetime] = None


# --- Project-scoped endpoints ---

@router.get("", response_model=List[FixtureReadWithState])
def list_fixtures(
    project_id: int,
    session: Session = Depends(get_session_dep)
):
    """List all fixtures for a project with cache status."""
    project = crud.get_project(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    fixtures = crud.get_fixtures_by_project(session, project_id)
    result = []

    for fixture in fixtures:
        # Check for valid cached state
        valid_state = crud.get_valid_fixture_state(session, fixture.id)

        fixture_data = FixtureReadWithState(
            id=fixture.id,
            project_id=fixture.project_id,
            name=fixture.name,
            description=fixture.description,
            setup_steps=fixture.setup_steps,
            scope=fixture.scope,
            cache_ttl_seconds=fixture.cache_ttl_seconds,
            created_at=fixture.created_at,
            updated_at=fixture.updated_at,
            has_valid_cache=valid_state is not None,
            cache_expires_at=valid_state.expires_at if valid_state else None,
        )
        result.append(fixture_data)

    return result


@router.post("", response_model=FixtureRead)
def create_fixture(
    project_id: int,
    request: FixtureCreateRequest,
    session: Session = Depends(get_session_dep)
):
    """Create a new fixture."""
    project = crud.get_project(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Validate scope
    if request.scope not in [FixtureScope.TEST.value, FixtureScope.CACHED.value]:
        raise HTTPException(status_code=400, detail=f"Invalid scope: {request.scope}. Must be 'test' or 'cached'")

    # Validate steps
    if not request.setup_steps:
        raise HTTPException(status_code=400, detail="setup_steps cannot be empty")

    fixture = FixtureCreate(
        project_id=project_id,
        name=request.name,
        description=request.description,
        setup_steps=json.dumps(request.setup_steps),
        scope=request.scope,
        cache_ttl_seconds=request.cache_ttl_seconds,
    )

    db_fixture = crud.create_fixture(session, fixture)
    logger.info(f"Created fixture: id={db_fixture.id}, name={db_fixture.name}, scope={db_fixture.scope}")

    return db_fixture


@router.post("/generate")
async def generate_fixture(
    project_id: int,
    request: FixtureGenerateRequest,
    session: Session = Depends(get_session_dep)
):
    """Generate fixture steps from natural language description.

    Uses the existing LangGraph agent to convert NLP to steps.
    Returns the generated fixture data without saving it.
    """
    project = crud.get_project(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Import the planner directly to generate steps
    from agent.nodes.planner import plan_test
    from langchain_core.messages import HumanMessage

    # Build state for the planner
    # Skip fixtures context since we're generating a fixture (no dependencies)
    state = {
        "messages": [HumanMessage(content=request.prompt)],
        "project_id": project_id,
        "project_settings": {
            "id": project_id,
            "url": project.base_url,
            "name": project.name,
        },
        "skip_fixtures_context": True,  # Don't suggest fixtures when generating a fixture
    }

    try:
        result = await plan_test(state)
        test_plan = result.get("test_plan")

        if not test_plan:
            raise HTTPException(status_code=500, detail="Failed to generate fixture steps")

        # Extract steps from test plan
        steps = test_plan.get("steps", [])
        if not steps:
            raise HTTPException(status_code=500, detail="Generated fixture has no steps")

        # Generate name if not provided
        name = request.name or "Generated Fixture"

        # Return the fixture data without saving
        logger.info(f"Generated fixture preview: name={name}, steps={len(steps)}")

        return {
            "name": name,
            "description": request.prompt,
            "setup_steps": steps,
            "scope": "cached",
            "cache_ttl_seconds": 3600,
        }

    except Exception as e:
        logger.error(f"Failed to generate fixture: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate fixture: {str(e)}")


# --- Fixture-specific endpoints ---

@fixture_router.get("/{fixture_id}", response_model=FixtureReadWithState)
def get_fixture(
    fixture_id: int,
    session: Session = Depends(get_session_dep)
):
    """Get a fixture by ID."""
    fixture = crud.get_fixture(session, fixture_id)
    if not fixture:
        raise HTTPException(status_code=404, detail="Fixture not found")

    # Check for valid cached state
    valid_state = crud.get_valid_fixture_state(session, fixture.id)

    return FixtureReadWithState(
        id=fixture.id,
        project_id=fixture.project_id,
        name=fixture.name,
        description=fixture.description,
        setup_steps=fixture.setup_steps,
        scope=fixture.scope,
        cache_ttl_seconds=fixture.cache_ttl_seconds,
        created_at=fixture.created_at,
        updated_at=fixture.updated_at,
        has_valid_cache=valid_state is not None,
        cache_expires_at=valid_state.expires_at if valid_state else None,
    )


@fixture_router.put("/{fixture_id}", response_model=FixtureRead)
def update_fixture(
    fixture_id: int,
    request: FixtureUpdateRequest,
    session: Session = Depends(get_session_dep)
):
    """Update a fixture."""
    fixture = crud.get_fixture(session, fixture_id)
    if not fixture:
        raise HTTPException(status_code=404, detail="Fixture not found")

    # Validate scope if provided
    if request.scope and request.scope not in [FixtureScope.TEST.value, FixtureScope.CACHED.value]:
        raise HTTPException(status_code=400, detail=f"Invalid scope: {request.scope}. Must be 'test' or 'cached'")

    # Build update data
    update_data = FixtureUpdate(
        name=request.name,
        description=request.description,
        setup_steps=json.dumps(request.setup_steps) if request.setup_steps is not None else None,
        scope=request.scope,
        cache_ttl_seconds=request.cache_ttl_seconds,
    )

    updated = crud.update_fixture(session, fixture_id, update_data)

    # If setup_steps changed, invalidate cached state
    if request.setup_steps is not None:
        count = crud.delete_fixture_states_by_fixture(session, fixture_id)
        if count > 0:
            logger.info(f"Invalidated {count} cached states for fixture {fixture_id} due to steps update")

    logger.info(f"Updated fixture: id={fixture_id}")
    return updated


@fixture_router.delete("/{fixture_id}")
def delete_fixture(
    fixture_id: int,
    session: Session = Depends(get_session_dep)
):
    """Delete a fixture and all its cached states."""
    fixture = crud.get_fixture(session, fixture_id)
    if not fixture:
        raise HTTPException(status_code=404, detail="Fixture not found")

    success = crud.delete_fixture(session, fixture_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete fixture")

    logger.info(f"Deleted fixture: id={fixture_id}")
    return {"status": "deleted"}


@fixture_router.post("/{fixture_id}/preview")
async def preview_fixture(
    fixture_id: int,
    browser: Optional[str] = None,
    session: Session = Depends(get_session_dep)
):
    """Execute fixture setup steps and return results as SSE stream.

    If execution succeeds and fixture scope is 'cached', automatically saves the browser state.
    """
    fixture = crud.get_fixture(session, fixture_id)
    if not fixture:
        raise HTTPException(status_code=404, detail="Fixture not found")

    project = crud.get_project(session, fixture.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Import executor client
    from agent.executor_client import PlaywrightExecutorClient

    async def generate():
        """Stream fixture execution events."""
        client = PlaywrightExecutorClient()
        captured_state = None
        execution_status = None
        final_url = None
        
        try:
            steps = fixture.get_setup_steps()

            # Resolve any template references in steps
            from api.routes.test_cases import resolve_references, mask_passwords_in_steps
            resolved_steps = resolve_references(session, project.id, steps)
            display_steps = mask_passwords_in_steps(resolved_steps)

            # For cached fixtures, append capture_state step
            if fixture.scope == "cached":
                resolved_steps.append({
                    "action": "capture_state",
                    "target": None,
                    "value": None,
                    "description": "Capture browser state for caching",
                })
                display_steps.append({
                    "action": "capture_state",
                    "target": None,
                    "value": None,
                    "description": "Capture browser state for caching",
                })

            async for event in client.execute_stream(
                base_url=project.base_url,
                steps=resolved_steps,
                test_id=f"fixture-preview-{fixture_id}",
                options={"screenshot_on_failure": True, "browser": browser},
            ):
                # Mask passwords in output
                if event.get("type") in ["step_started", "step_completed", "step_retry"]:
                    step_num = event.get("step_number", 1) - 1
                    if 0 <= step_num < len(display_steps):
                        display_step = display_steps[step_num]
                        event["target"] = display_step.get("target")
                        event["value"] = display_step.get("value")

                # Capture state from capture_state step
                if event.get("type") == "step_completed":
                    if event.get("action") == "capture_state" and event.get("status") == "passed":
                        captured_state = event.get("result")
                        final_url = event.get("url")
                        logger.info(f"Captured state for fixture {fixture.id}, url={final_url}")
                
                # Track execution status
                if event.get("type") == "completed":
                    execution_status = event.get("status")

                yield f"data: {json.dumps(event)}\n\n"
            
            # Debug logging
            logger.info(f"Post-execution: status={execution_status}, scope={fixture.scope}, has_state={captured_state is not None}")
            
            # Save state if execution succeeded and scope is cached
            if execution_status == "passed" and fixture.scope == "cached" and captured_state:
                try:
                    from datetime import timedelta
                    from db.encryption import encrypt_data
                    from db.session import get_session
                    
                    # Use a new session for database operations
                    with get_session() as db:
                        # Encrypt the state
                        encrypted_state = encrypt_data(json.dumps(captured_state))
                        
                        # Calculate expiration
                        expires_at = datetime.utcnow() + timedelta(seconds=fixture.cache_ttl_seconds)
                        
                        # Delete old states for this fixture/browser combo
                        from sqlmodel import select
                        old_states = db.exec(
                            select(FixtureState)
                            .where(FixtureState.fixture_id == fixture.id)
                            .where(FixtureState.browser == browser)
                        ).all()
                        for old_state in old_states:
                            db.delete(old_state)
                        
                        # Save new state
                        state_create = FixtureStateCreate(
                            fixture_id=fixture.id,
                            project_id=fixture.project_id,
                            url=final_url,
                            encrypted_state_json=encrypted_state,
                            browser=browser,
                            expires_at=expires_at,
                        )
                        crud.create_fixture_state(db, state_create)
                        db.commit()
                        logger.info(f"Saved state for fixture {fixture.id} (browser={browser}, expires={expires_at})")
                except Exception as e:
                    logger.error(f"Failed to save fixture state: {e}", exc_info=True)
                    # Don't fail the whole preview, just log the error
                    
        finally:
            await client.close()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@fixture_router.delete("/{fixture_id}/state")
def invalidate_fixture_state(
    fixture_id: int,
    session: Session = Depends(get_session_dep)
):
    """Invalidate all cached states for a fixture.

    Use this to force fixture setup to run fresh on next test execution.
    """
    fixture = crud.get_fixture(session, fixture_id)
    if not fixture:
        raise HTTPException(status_code=404, detail="Fixture not found")

    count = crud.delete_fixture_states_by_fixture(session, fixture_id)
    logger.info(f"Invalidated {count} cached states for fixture {fixture_id}")

    return {"status": "invalidated", "count": count}


@fixture_router.get("/{fixture_id}/state", response_model=Optional[FixtureStateRead])
def get_fixture_state(
    fixture_id: int,
    browser: Optional[str] = None,
    session: Session = Depends(get_session_dep)
):
    """Get the current valid cached state for a fixture.

    Returns None if no valid (non-expired) state exists.
    """
    fixture = crud.get_fixture(session, fixture_id)
    if not fixture:
        raise HTTPException(status_code=404, detail="Fixture not found")

    state = crud.get_valid_fixture_state(session, fixture_id, browser)
    return state
