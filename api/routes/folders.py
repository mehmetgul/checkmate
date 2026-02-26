"""Folder management API routes."""

import json
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from db.session import get_session_dep
from db.models import (
    TestFolder, TestFolderCreate, TestFolderRead, TestFolderUpdate,
    TestCaseRead,
)
from db import crud
from core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/folders", tags=["folders"])


# --- Request/Response Models ---

class MoveFolderRequest(BaseModel):
    parent_id: Optional[int] = None


class MoveTestCaseRequest(BaseModel):
    folder_id: Optional[int] = None



class FolderRunResponse(BaseModel):
    test_case_ids: List[int]
    count: int


# --- Auto-seed default smart folders ---

def _seed_default_folders(session: Session, project_id: int) -> None:
    """Create default smart folders for a project on first access."""
    defaults = [
        {
            "name": "Regression Suite",
            "folder_type": "smart",
            "smart_criteria": json.dumps({
                "tags": ["regression"],
                "statuses": [],
            }),
            "order_index": 0,
        },
        {
            "name": "Smoke Tests",
            "folder_type": "smart",
            "smart_criteria": json.dumps({
                "tags": ["smoke"],
                "statuses": [],
            }),
            "order_index": 1,
        },
    ]
    for folder_data in defaults:
        crud.create_folder(session, TestFolderCreate(
            project_id=project_id,
            **folder_data,
        ))
    logger.info(f"Seeded default smart folders for project {project_id}")


# --- Endpoints ---

@router.get("/project/{project_id}", response_model=List[TestFolderRead])
def list_folders(
    project_id: int,
    session: Session = Depends(get_session_dep),
):
    """List all folders for a project. Auto-seeds defaults on first access."""
    project = crud.get_project(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    folders = crud.get_folders_by_project(session, project_id)

    # Auto-seed if no folders exist
    if not folders:
        _seed_default_folders(session, project_id)
        folders = crud.get_folders_by_project(session, project_id)

    return folders


@router.post("", response_model=TestFolderRead)
def create_folder(
    folder: TestFolderCreate,
    session: Session = Depends(get_session_dep),
):
    """Create a new folder (regular or smart)."""
    project = crud.get_project(session, folder.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        return crud.create_folder(session, folder)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{folder_id}", response_model=TestFolderRead)
def get_folder(
    folder_id: int,
    session: Session = Depends(get_session_dep),
):
    """Get a single folder by ID."""
    folder = crud.get_folder(session, folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    return folder


@router.put("/{folder_id}", response_model=TestFolderRead)
def update_folder(
    folder_id: int,
    data: TestFolderUpdate,
    session: Session = Depends(get_session_dep),
):
    """Update a folder."""
    try:
        folder = crud.update_folder(session, folder_id, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    return folder


@router.delete("/{folder_id}")
def delete_folder(
    folder_id: int,
    session: Session = Depends(get_session_dep),
):
    """Delete a folder. Only allowed if the folder has no test cases."""
    try:
        success = crud.delete_folder(session, folder_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not success:
        raise HTTPException(status_code=404, detail="Folder not found")
    return {"status": "deleted"}


@router.get("/{folder_id}/test-cases", response_model=List[TestCaseRead])
def get_folder_test_cases(
    folder_id: int,
    include_descendants: bool = False,
    session: Session = Depends(get_session_dep),
):
    """Get test cases in a folder. Smart folders compute dynamically."""
    folder = crud.get_folder(session, folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    if folder.folder_type == "smart":
        return crud.compute_smart_folder_tests(session, folder)

    return crud.get_test_cases_by_folder(
        session, folder_id, include_descendants=include_descendants,
    )


@router.patch("/{folder_id}/move", response_model=TestFolderRead)
def move_folder(
    folder_id: int,
    request: MoveFolderRequest,
    session: Session = Depends(get_session_dep),
):
    """Move a folder to a new parent (or root)."""
    try:
        folder = crud.move_folder(session, folder_id, request.parent_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    return folder


@router.patch("/test-cases/{test_case_id}/move", response_model=TestCaseRead)
def move_test_case(
    test_case_id: int,
    request: MoveTestCaseRequest,
    session: Session = Depends(get_session_dep),
):
    """Move a test case to a folder (or root)."""
    try:
        tc = crud.move_test_case_to_folder(session, test_case_id, request.folder_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not tc:
        raise HTTPException(status_code=404, detail="Test case not found")
    return tc


@router.post("/{folder_id}/run", response_model=FolderRunResponse)
def get_folder_runnable_ids(
    folder_id: int,
    session: Session = Depends(get_session_dep),
):
    """Get runnable test_case_ids for a folder (for batch run)."""
    from db.models import TestCaseStatus

    folder = crud.get_folder(session, folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    if folder.folder_type == "smart":
        test_cases = crud.compute_smart_folder_tests(session, folder)
    else:
        test_cases = crud.get_test_cases_by_folder(
            session, folder_id, include_descendants=True,
        )

    # Filter to runnable statuses with steps
    runnable_statuses = [
        TestCaseStatus.DRAFT, TestCaseStatus.ACTIVE, TestCaseStatus.READY, TestCaseStatus.APPROVED,
    ]
    runnable_ids = [
        tc.id for tc in test_cases
        if tc.status in runnable_statuses and tc.get_steps()
    ]

    return FolderRunResponse(test_case_ids=runnable_ids, count=len(runnable_ids))
