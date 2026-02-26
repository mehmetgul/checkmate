"""Environment management API routes."""

from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from db.session import get_session_dep
from db.models import EnvironmentCreate, EnvironmentRead, EnvironmentUpdate
from db import crud

router = APIRouter(
    prefix="/projects/{project_id}/environments",
    tags=["environments"],
)


def _verify_project(session: Session, project_id: int):
    project = crud.get_project(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("", response_model=List[EnvironmentRead])
def list_environments(
    project_id: int,
    session: Session = Depends(get_session_dep),
):
    """List all environments for a project."""
    _verify_project(session, project_id)
    return crud.get_environments_by_project(session, project_id)


@router.post("", response_model=EnvironmentRead)
def create_environment(
    project_id: int,
    data: EnvironmentCreate,
    session: Session = Depends(get_session_dep),
):
    """Create a new environment."""
    _verify_project(session, project_id)
    if data.project_id != project_id:
        raise HTTPException(status_code=400, detail="Project ID mismatch")
    return crud.create_environment(session, data)


@router.put("/{env_id}", response_model=EnvironmentRead)
def update_environment(
    project_id: int,
    env_id: int,
    data: EnvironmentUpdate,
    session: Session = Depends(get_session_dep),
):
    """Update an environment."""
    _verify_project(session, project_id)
    env = crud.update_environment(session, env_id, data)
    if not env:
        raise HTTPException(status_code=404, detail="Environment not found")
    return env


@router.delete("/{env_id}")
def delete_environment(
    project_id: int,
    env_id: int,
    session: Session = Depends(get_session_dep),
):
    """Delete an environment."""
    _verify_project(session, project_id)
    deleted = crud.delete_environment(session, env_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Environment not found")
    return {"ok": True}
