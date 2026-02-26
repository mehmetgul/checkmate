"""Vault API routes — credentials (enhanced personas) and test data."""

import json
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from db.session import get_session_dep
from db.models import (
    PersonaCreate, PersonaRead, PersonaUpdate,
    TestDataCreate, TestDataRead, TestDataUpdate,
)
from db import crud
from db.encryption import decrypt_password, decrypt_data

router = APIRouter(
    prefix="/projects/{project_id}/vault",
    tags=["vault"],
)


def _verify_project(session: Session, project_id: int):
    project = crud.get_project(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


# =============================================================================
# Credentials (wraps Persona CRUD with credential_type support)
# =============================================================================

@router.get("/credentials", response_model=List[PersonaRead])
def list_credentials(
    project_id: int,
    environment_id: Optional[int] = None,
    session: Session = Depends(get_session_dep),
):
    """List credentials for a project, optionally filtered to an environment (+ globals)."""
    _verify_project(session, project_id)
    return crud.get_personas_by_project(session, project_id, environment_id)


@router.post("/credentials", response_model=PersonaRead)
def create_credential(
    project_id: int,
    data: PersonaCreate,
    session: Session = Depends(get_session_dep),
):
    """Create a new credential."""
    _verify_project(session, project_id)

    if data.project_id != project_id:
        raise HTTPException(status_code=400, detail="Project ID mismatch")

    # Validate required fields based on credential type
    cred_type = data.credential_type or "login"
    if cred_type == "login" and not data.password:
        raise HTTPException(status_code=400, detail="Password is required for login credentials")
    if cred_type == "api_key" and not data.api_key:
        raise HTTPException(status_code=400, detail="API key is required for api_key credentials")
    if cred_type == "token" and not data.token:
        raise HTTPException(status_code=400, detail="Token is required for token credentials")

    return crud.create_persona(session, data)


@router.get("/credentials/{credential_id}", response_model=PersonaRead)
def get_credential(
    project_id: int,
    credential_id: int,
    session: Session = Depends(get_session_dep),
):
    """Get a credential by ID."""
    persona = crud.get_persona(session, credential_id)
    if not persona or persona.project_id != project_id:
        raise HTTPException(status_code=404, detail="Credential not found")
    return persona


class RevealedCredential(BaseModel):
    """Decrypted secrets for a credential."""
    password: Optional[str] = None
    api_key: Optional[str] = None
    token: Optional[str] = None
    custom_fields: Optional[dict] = None


@router.get("/credentials/{credential_id}/reveal", response_model=RevealedCredential)
def reveal_credential(
    project_id: int,
    credential_id: int,
    session: Session = Depends(get_session_dep),
):
    """Reveal decrypted secrets for a credential."""
    persona = crud.get_persona(session, credential_id)
    if not persona or persona.project_id != project_id:
        raise HTTPException(status_code=404, detail="Credential not found")

    result = RevealedCredential()

    if persona.encrypted_password:
        try:
            result.password = decrypt_password(persona.encrypted_password)
        except Exception:
            result.password = None

    if persona.encrypted_api_key:
        try:
            result.api_key = decrypt_data(persona.encrypted_api_key)
        except Exception:
            result.api_key = None

    if persona.encrypted_token:
        try:
            result.token = decrypt_data(persona.encrypted_token)
        except Exception:
            result.token = None

    if persona.encrypted_metadata:
        try:
            result.custom_fields = json.loads(decrypt_data(persona.encrypted_metadata))
        except Exception:
            result.custom_fields = None

    return result


@router.put("/credentials/{credential_id}", response_model=PersonaRead)
def update_credential(
    project_id: int,
    credential_id: int,
    data: PersonaUpdate,
    session: Session = Depends(get_session_dep),
):
    """Update a credential."""
    persona = crud.get_persona(session, credential_id)
    if not persona or persona.project_id != project_id:
        raise HTTPException(status_code=404, detail="Credential not found")

    updated = crud.update_persona(session, credential_id, data)
    if not updated:
        raise HTTPException(status_code=404, detail="Credential not found")
    return updated


@router.delete("/credentials/{credential_id}")
def delete_credential(
    project_id: int,
    credential_id: int,
    session: Session = Depends(get_session_dep),
):
    """Delete a credential."""
    persona = crud.get_persona(session, credential_id)
    if not persona or persona.project_id != project_id:
        raise HTTPException(status_code=404, detail="Credential not found")

    if not crud.delete_persona(session, credential_id):
        raise HTTPException(status_code=404, detail="Credential not found")
    return {"status": "deleted"}


# =============================================================================
# Test Data
# =============================================================================

@router.get("/test-data", response_model=List[TestDataRead])
def list_test_data(
    project_id: int,
    environment_id: Optional[int] = None,
    session: Session = Depends(get_session_dep),
):
    """List test data entries for a project, optionally filtered to an environment (+ globals)."""
    _verify_project(session, project_id)
    return crud.get_test_data_by_project(session, project_id, environment_id)


@router.post("/test-data", response_model=TestDataRead)
def create_test_data(
    project_id: int,
    data: TestDataCreate,
    session: Session = Depends(get_session_dep),
):
    """Create a new test data entry."""
    _verify_project(session, project_id)

    if data.project_id != project_id:
        raise HTTPException(status_code=400, detail="Project ID mismatch")

    return crud.create_test_data(session, data)


@router.get("/test-data/{test_data_id}", response_model=TestDataRead)
def get_test_data(
    project_id: int,
    test_data_id: int,
    session: Session = Depends(get_session_dep),
):
    """Get a test data entry by ID."""
    td = crud.get_test_data(session, test_data_id)
    if not td or td.project_id != project_id:
        raise HTTPException(status_code=404, detail="Test data not found")
    return td


@router.put("/test-data/{test_data_id}", response_model=TestDataRead)
def update_test_data(
    project_id: int,
    test_data_id: int,
    data: TestDataUpdate,
    session: Session = Depends(get_session_dep),
):
    """Update a test data entry."""
    td = crud.get_test_data(session, test_data_id)
    if not td or td.project_id != project_id:
        raise HTTPException(status_code=404, detail="Test data not found")

    updated = crud.update_test_data(session, test_data_id, data)
    if not updated:
        raise HTTPException(status_code=404, detail="Test data not found")
    return updated


@router.delete("/test-data/{test_data_id}")
def delete_test_data(
    project_id: int,
    test_data_id: int,
    session: Session = Depends(get_session_dep),
):
    """Delete a test data entry."""
    td = crud.get_test_data(session, test_data_id)
    if not td or td.project_id != project_id:
        raise HTTPException(status_code=404, detail="Test data not found")

    if not crud.delete_test_data(session, test_data_id):
        raise HTTPException(status_code=404, detail="Test data not found")
    return {"status": "deleted"}
