"""Project management API routes."""

from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from db.session import get_session_dep
from db.models import Project, ProjectCreate, ProjectRead
from db import crud

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("/stats")
def get_stats(session: Session = Depends(get_session_dep)):
    """Get global statistics for the dashboard."""
    return crud.get_stats(session)


@router.get("", response_model=List[ProjectRead])
def list_projects(
    skip: int = 0,
    limit: int = 100,
    session: Session = Depends(get_session_dep)
):
    """List all projects."""
    return crud.get_projects(session, skip=skip, limit=limit)


@router.post("", response_model=ProjectRead)
def create_project(
    project: ProjectCreate,
    session: Session = Depends(get_session_dep)
):
    """Create a new project."""
    return crud.create_project(session, project)


@router.get("/{project_id}", response_model=ProjectRead)
def get_project(
    project_id: int,
    session: Session = Depends(get_session_dep)
):
    """Get a project by ID."""
    project = crud.get_project(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.put("/{project_id}", response_model=ProjectRead)
def update_project(
    project_id: int,
    project_data: ProjectCreate,
    session: Session = Depends(get_session_dep)
):
    """Update a project."""
    project = crud.update_project(session, project_id, project_data.model_dump())
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.delete("/{project_id}")
def delete_project(
    project_id: int,
    session: Session = Depends(get_session_dep)
):
    """Delete a project."""
    success = crud.delete_project(session, project_id)
    if not success:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"status": "deleted"}
