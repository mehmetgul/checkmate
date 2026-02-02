"""Agent API routes - consolidates LangGraph invocation into FastAPI."""

import uuid
from typing import Optional, List, Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session
from langchain_core.messages import HumanMessage

from db.session import get_session_dep
from db.models import Project, TestCaseCreate
from db import crud
from agent.graph import graph
from agent.nodes.builder import build_test_case


router = APIRouter(prefix="/agent", tags=["agent"])


# =============================================================================
# Build Test Case Endpoint (Structured I/O)
# =============================================================================

class TestStepRequest(BaseModel):
    """A single test step in a request."""
    action: str
    target: Optional[str] = None
    value: Optional[str] = None
    description: str


class TestCaseRequest(BaseModel):
    """Current test case state in request."""
    name: Optional[str] = None
    natural_query: Optional[str] = None
    priority: Optional[str] = "medium"
    tags: Optional[List[str]] = []
    steps: Optional[List[TestStepRequest]] = []
    original_steps: Optional[List[TestStepRequest]] = []  # For edit context


class BuildRequest(BaseModel):
    """Request body for build endpoint."""
    message: str
    previous_messages: Optional[List[str]] = []
    test_case: Optional[TestCaseRequest] = None


class TestStepResponse(BaseModel):
    """A single test step in a response."""
    action: str
    target: Optional[str] = None
    value: Optional[str] = None
    description: str


class TestCaseResponse(BaseModel):
    """Updated test case in response."""
    name: str
    natural_query: str
    priority: str
    tags: List[str]
    steps: List[TestStepResponse]
    fixture_ids: List[int] = []


class BuildResponse(BaseModel):
    """Response from build endpoint."""
    test_case: TestCaseResponse
    message: Optional[str] = None
    needs_clarification: bool = False


@router.post("/projects/{project_id}/build", response_model=BuildResponse)
async def build(
    project_id: int,
    request: BuildRequest,
    session: Session = Depends(get_session_dep)
):
    """
    Build or modify a test case using natural language.

    This endpoint uses structured I/O:
    - Input: user message + previous messages + current test case state
    - Output: updated test case state + optional message
    """
    # 1. Load project from DB
    project = crud.get_project(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # 2. Convert request test case to dict for builder
    current_test_case = None
    if request.test_case:
        current_test_case = {
            "name": request.test_case.name,
            "natural_query": request.test_case.natural_query,
            "priority": request.test_case.priority,
            "tags": request.test_case.tags or [],
            "steps": [
                {
                    "action": step.action,
                    "target": step.target,
                    "value": step.value,
                    "description": step.description,
                }
                for step in (request.test_case.steps or [])
            ],
            "original_steps": [
                {
                    "action": step.action,
                    "target": step.target,
                    "value": step.value,
                    "description": step.description,
                }
                for step in (request.test_case.original_steps or [])
            ] if request.test_case.original_steps else None
        }

    # 3. Call the builder
    try:
        result = await build_test_case(
            current_message=request.message,
            previous_messages=request.previous_messages or [],
            current_test_case=current_test_case,
            project_name=project.name,
            base_url=project.base_url,
            project_id=project_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Builder error: {str(e)}")

    # 4. Convert result to response format
    return BuildResponse(
        test_case=TestCaseResponse(
            name=result.test_case.name,
            natural_query=result.test_case.natural_query,
            priority=result.test_case.priority,
            tags=result.test_case.tags,
            steps=[
                TestStepResponse(
                    action=step.action,
                    target=step.target,
                    value=step.value,
                    description=step.description,
                )
                for step in result.test_case.steps
            ],
            fixture_ids=result.test_case.fixture_ids,
        ),
        message=result.message,
        needs_clarification=result.needs_clarification,
    )


# =============================================================================
# Chat Endpoint (Conversational - Legacy)
# =============================================================================


class ChatRequest(BaseModel):
    """Request body for chat endpoint."""
    message: str
    thread_id: Optional[str] = None


class GeneratedTestCaseResponse(BaseModel):
    """Generated test case in response."""
    name: str
    natural_query: str
    priority: str
    tags: List[str]


class ChatResponse(BaseModel):
    """Response from chat endpoint."""
    thread_id: str
    intent: Optional[str] = None
    message: str
    test_plan: Optional[dict] = None
    generated_test_cases: Optional[List[GeneratedTestCaseResponse]] = None
    summary: Optional[str] = None


@router.post("/projects/{project_id}/chat", response_model=ChatResponse)
async def chat(
    project_id: int,
    request: ChatRequest,
    session: Session = Depends(get_session_dep)
):
    """
    Chat with the QA agent for a specific project.

    This endpoint invokes the LangGraph agent directly, eliminating the need
    for a separate LangGraph dev server.
    """
    # 1. Load project from DB
    project = crud.get_project(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # 2. Generate or use provided thread_id
    thread_id = request.thread_id or str(uuid.uuid4())

    # 3. Build initial state with project context + user message
    initial_state = {
        "messages": [HumanMessage(content=request.message)],
        # Unified project settings object
        "project_settings": {
            "id": str(project_id),
            "name": project.name,
            "url": project.base_url,
            "config": project.get_config() if project.config else {},
            "base_prompt": project.base_prompt,
        },
        # Legacy fields (for backward compatibility)
        "project_id": str(project_id),
        "project_name": project.name,
        "project_url": project.base_url,
        "project_config": project.get_config() if project.config else {},
        "current_step": 0,
        "test_results": [],
    }

    # 4. Invoke graph with thread_id for conversation continuity
    config = {"configurable": {"thread_id": thread_id}}

    try:
        result = await graph.ainvoke(initial_state, config=config)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Agent error: {str(e)}")

    # 5. Extract response data from result
    messages = result.get("messages", [])
    last_message = messages[-1].content if messages else "No response generated"

    intent = result.get("intent")
    test_plan = result.get("test_plan")
    summary = result.get("summary")
    generated_test_cases_raw = result.get("generated_test_cases", [])

    # 6. If test cases were generated, save them to the database
    saved_test_cases = []
    if generated_test_cases_raw:
        for tc_data in generated_test_cases_raw:
            # Create test case in database
            test_case_create = TestCaseCreate(
                project_id=project_id,
                name=tc_data.get("name", "Untitled"),
                natural_query=tc_data.get("natural_query", ""),
                steps="[]",  # Generator doesn't produce steps yet
                priority=tc_data.get("priority", "medium"),
                tags=str(tc_data.get("tags", [])).replace("'", '"'),  # JSON format
            )

            saved_tc = crud.create_test_case(session, test_case_create)
            saved_test_cases.append(GeneratedTestCaseResponse(
                name=tc_data.get("name", "Untitled"),
                natural_query=tc_data.get("natural_query", ""),
                priority=tc_data.get("priority", "medium"),
                tags=tc_data.get("tags", []),
            ))

    # 7. Return ChatResponse
    return ChatResponse(
        thread_id=thread_id,
        intent=intent,
        message=last_message,
        test_plan=test_plan,
        generated_test_cases=saved_test_cases if saved_test_cases else None,
        summary=summary,
    )
