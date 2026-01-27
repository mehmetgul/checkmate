"""Project settings API routes for personas, pages, and context."""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from db.session import get_session_dep
from db.models import (
    Persona, PersonaCreate, PersonaRead, PersonaUpdate,
    Page, PageCreate, PageRead, PageUpdate,
)
from db import crud

router = APIRouter(prefix="/projects/{project_id}/settings", tags=["settings"])


# =============================================================================
# Context (Base Prompt) and Project Settings
# =============================================================================

class ContextResponse(BaseModel):
    """Response for context endpoint."""
    base_prompt: Optional[str] = None
    page_load_state: Optional[str] = "load"


class ContextUpdate(BaseModel):
    """Request body for updating context."""
    base_prompt: Optional[str] = None
    page_load_state: Optional[str] = None


@router.get("/context", response_model=ContextResponse)
def get_context(
    project_id: int,
    session: Session = Depends(get_session_dep)
):
    """Get project context (base prompt) and settings."""
    project = crud.get_project(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return ContextResponse(
        base_prompt=project.base_prompt,
        page_load_state=project.page_load_state or "load",
    )


@router.put("/context", response_model=ContextResponse)
def update_context(
    project_id: int,
    data: ContextUpdate,
    session: Session = Depends(get_session_dep)
):
    """Update project context (base prompt) and settings."""
    project = crud.get_project(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    update_data = {}
    if data.base_prompt is not None:
        update_data["base_prompt"] = data.base_prompt
    if data.page_load_state is not None:
        update_data["page_load_state"] = data.page_load_state

    if update_data:
        updated = crud.update_project(session, project_id, update_data)
        if not updated:
            raise HTTPException(status_code=404, detail="Project not found")
        return ContextResponse(
            base_prompt=updated.base_prompt,
            page_load_state=updated.page_load_state or "load",
        )

    return ContextResponse(
        base_prompt=project.base_prompt,
        page_load_state=project.page_load_state or "load",
    )


# =============================================================================
# Personas
# =============================================================================

@router.get("/personas", response_model=List[PersonaRead])
def list_personas(
    project_id: int,
    session: Session = Depends(get_session_dep)
):
    """List all personas for a project."""
    # Verify project exists
    project = crud.get_project(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return crud.get_personas_by_project(session, project_id)


@router.post("/personas", response_model=PersonaRead)
def create_persona(
    project_id: int,
    persona: PersonaCreate,
    session: Session = Depends(get_session_dep)
):
    """Create a new persona."""
    # Verify project exists
    project = crud.get_project(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Ensure project_id matches
    if persona.project_id != project_id:
        raise HTTPException(status_code=400, detail="Project ID mismatch")

    return crud.create_persona(session, persona)


@router.get("/personas/{persona_id}", response_model=PersonaRead)
def get_persona(
    project_id: int,
    persona_id: int,
    session: Session = Depends(get_session_dep)
):
    """Get a persona by ID."""
    persona = crud.get_persona(session, persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not found")
    if persona.project_id != project_id:
        raise HTTPException(status_code=404, detail="Persona not found")
    return persona


@router.put("/personas/{persona_id}", response_model=PersonaRead)
def update_persona(
    project_id: int,
    persona_id: int,
    data: PersonaUpdate,
    session: Session = Depends(get_session_dep)
):
    """Update a persona."""
    persona = crud.get_persona(session, persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not found")
    if persona.project_id != project_id:
        raise HTTPException(status_code=404, detail="Persona not found")

    updated = crud.update_persona(session, persona_id, data)
    if not updated:
        raise HTTPException(status_code=404, detail="Persona not found")
    return updated


@router.delete("/personas/{persona_id}")
def delete_persona(
    project_id: int,
    persona_id: int,
    session: Session = Depends(get_session_dep)
):
    """Delete a persona."""
    persona = crud.get_persona(session, persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not found")
    if persona.project_id != project_id:
        raise HTTPException(status_code=404, detail="Persona not found")

    success = crud.delete_persona(session, persona_id)
    if not success:
        raise HTTPException(status_code=404, detail="Persona not found")
    return {"status": "deleted"}


# =============================================================================
# Pages
# =============================================================================

@router.get("/pages", response_model=List[PageRead])
def list_pages(
    project_id: int,
    session: Session = Depends(get_session_dep)
):
    """List all pages for a project."""
    # Verify project exists
    project = crud.get_project(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return crud.get_pages_by_project(session, project_id)


@router.post("/pages", response_model=PageRead)
def create_page(
    project_id: int,
    page: PageCreate,
    session: Session = Depends(get_session_dep)
):
    """Create a new page."""
    # Verify project exists
    project = crud.get_project(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Ensure project_id matches
    if page.project_id != project_id:
        raise HTTPException(status_code=400, detail="Project ID mismatch")

    return crud.create_page(session, page)


@router.get("/pages/{page_id}", response_model=PageRead)
def get_page(
    project_id: int,
    page_id: int,
    session: Session = Depends(get_session_dep)
):
    """Get a page by ID."""
    page = crud.get_page(session, page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    if page.project_id != project_id:
        raise HTTPException(status_code=404, detail="Page not found")
    return page


@router.put("/pages/{page_id}", response_model=PageRead)
def update_page(
    project_id: int,
    page_id: int,
    data: PageUpdate,
    session: Session = Depends(get_session_dep)
):
    """Update a page."""
    page = crud.get_page(session, page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    if page.project_id != project_id:
        raise HTTPException(status_code=404, detail="Page not found")

    updated = crud.update_page(session, page_id, data)
    if not updated:
        raise HTTPException(status_code=404, detail="Page not found")
    return updated


@router.delete("/pages/{page_id}")
def delete_page(
    project_id: int,
    page_id: int,
    session: Session = Depends(get_session_dep)
):
    """Delete a page."""
    page = crud.get_page(session, page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    if page.project_id != project_id:
        raise HTTPException(status_code=404, detail="Page not found")

    success = crud.delete_page(session, page_id)
    if not success:
        raise HTTPException(status_code=404, detail="Page not found")
    return {"status": "deleted"}
