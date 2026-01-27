"""Reference resolution for personas and pages in test steps."""

import logging
import re
from typing import Dict, Any, List, Tuple
from sqlmodel import Session

from db import crud
from db.encryption import decrypt_password

logger = logging.getLogger(__name__)

# Pattern to detect password placeholders
PASSWORD_PATTERN = r"\{\{\w+\.password\}\}"


def mask_passwords_in_steps(
    steps: List[Dict[str, Any]],
    mask: str = "••••••••"
) -> List[Dict[str, Any]]:
    """
    Mask any resolved passwords in step values for display purposes.

    This should be called AFTER resolve_references to mask the actual
    password values before storing in database or sending to frontend.
    """
    masked_steps = []
    for step in steps:
        masked_step = dict(step)
        # Check if this step likely contains a password (from fill_form or type actions)
        value = masked_step.get("value", "")
        if isinstance(value, str):
            # For fill_form with JSON, mask password fields
            if step.get("action") == "fill_form" and value.startswith("{"):
                try:
                    import json
                    form_data = json.loads(value)
                    for key in form_data:
                        if "password" in key.lower():
                            form_data[key] = mask
                    masked_step["value"] = json.dumps(form_data)
                except (json.JSONDecodeError, TypeError):
                    pass
            # For type action targeting password fields
            elif step.get("action") == "type":
                target = (step.get("target") or "").lower()
                if "password" in target:
                    masked_step["value"] = mask
        masked_steps.append(masked_step)
    return masked_steps


def resolve_references(
    session: Session,
    project_id: int,
    steps: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Resolve {{persona.field}} and {{page}} references in test steps.
    Also resolves relative URLs to absolute using project's base_url.

    Supported patterns:
    - {{persona_name.username}} - resolves to the persona's username
    - {{persona_name.password}} - resolves to the decrypted password
    - {{page_name}} - resolves to the page's path

    Args:
        session: Database session
        project_id: Project ID to fetch personas/pages from
        steps: List of step dictionaries

    Returns:
        List of steps with all references resolved
    """
    # Load project for base_url
    project = crud.get_project(session, project_id)
    base_url = (project.base_url or "").rstrip("/") if project else ""

    # Load personas and pages for this project
    personas = {p.name: p for p in crud.get_personas_by_project(session, project_id)}
    pages = {p.name: p for p in crud.get_pages_by_project(session, project_id)}

    logger.info(f"[resolver] project_id={project_id}, personas={list(personas.keys())}, pages={list(pages.keys())}")

    # Pattern matches {{word}} or {{word.word}}
    pattern = r"\{\{(\w+(?:\.\w+)?)\}\}"

    def replace(match: re.Match) -> str:
        ref = match.group(1)

        if "." in ref:
            # Persona reference: {{name.field}}
            name, field = ref.split(".", 1)
            if name in personas:
                if field == "username":
                    return personas[name].username
                elif field == "password":
                    try:
                        decrypted = decrypt_password(personas[name].encrypted_password)
                        logger.info(f"[resolver] Decrypted password for '{name}': length={len(decrypted)}")
                        return decrypted
                    except Exception as e:
                        logger.error(f"[resolver] Failed to decrypt password for '{name}': {e}")
                        return match.group(0)
            # Unknown persona or field - return original
            logger.warning(f"[resolver] Persona '{name}' not found or unknown field '{field}'")
            return match.group(0)
        else:
            # Page reference: {{name}}
            if ref in pages:
                return pages[ref].path
            # Unknown page - return original
            return match.group(0)

    def resolve_value(value: Any) -> Any:
        """Resolve references in a value if it's a string."""
        if isinstance(value, str):
            return re.sub(pattern, replace, value)
        return value

    # Process each step
    resolved_steps = []
    for step in steps:
        resolved_step = {
            k: resolve_value(v) for k, v in step.items()
        }

        # Handle relative URLs for navigate action
        if resolved_step.get("action") == "navigate":
            url = resolved_step.get("value", "")
            if url and url.startswith("/") and base_url:
                resolved_step["value"] = base_url + url

        resolved_steps.append(resolved_step)

    return resolved_steps
