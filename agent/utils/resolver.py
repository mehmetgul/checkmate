"""Reference resolution for personas, pages, and test data in test steps."""

import json
import logging
import re
from typing import Dict, Any, List, Optional, Tuple
from sqlmodel import Session

from db import crud
from db.encryption import decrypt_password, decrypt_data

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
    steps: List[Dict[str, Any]],
    env_vars: Optional[Dict[str, str]] = None,
    override_base_url: Optional[str] = None,
    environment_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Resolve {{persona.field}}, {{page}}, {{env.VAR}}, and {{data.*}} references in test steps.
    Also resolves relative URLs to absolute using base_url (env override takes priority).

    Supported patterns:
    - {{env.VAR_NAME}}           - resolves from active environment variables
    - {{persona_name.username}}  - resolves to the persona's username
    - {{persona_name.password}}  - resolves to the decrypted password
    - {{page_name}}              - resolves to the page's path
    - {{data.dataset.field}}     - resolves from test data

    Args:
        session: Database session
        project_id: Project ID to fetch personas/pages from
        steps: List of step dictionaries
        env_vars: Optional dict of environment variables to inject ({{env.KEY}})
        override_base_url: Optional base_url from active environment (overrides project base_url)

    Returns:
        List of steps with all references resolved
    """
    # Load project for base_url; env override takes priority
    project = crud.get_project(session, project_id)
    base_url = (override_base_url or (project.base_url if project else "") or "").rstrip("/")
    _env_vars = env_vars or {}

    # Load personas and test data — env-scoped items override globals with the same name
    pages = {p.name: p for p in crud.get_pages_by_project(session, project_id)}

    if environment_id is not None:
        all_personas = crud.get_personas_by_project(session, project_id, environment_id)
        personas: Dict[str, Any] = {p.name: p for p in all_personas if p.environment_id is None}
        personas.update({p.name: p for p in all_personas if p.environment_id == environment_id})
        all_td = crud.get_test_data_by_project(session, project_id, environment_id)
        test_data_items: Dict[str, Any] = {td.name: td for td in all_td if td.environment_id is None}
        test_data_items.update({td.name: td for td in all_td if td.environment_id == environment_id})
    else:
        personas = {p.name: p for p in crud.get_personas_by_project(session, project_id)}
        test_data_items = {td.name: td for td in crud.get_test_data_by_project(session, project_id)}

    # Pattern matches {{word}}, {{word.word}}, or {{word.word.word}}
    pattern = r"\{\{(\w+(?:\.\w+(?:\.\w+)?)?)\}\}"

    def replace(match: re.Match) -> str:
        ref = match.group(1)
        parts = ref.split(".")

        if len(parts) == 3:
            # 3-part: {{data.dataset_name.field}} for test data
            prefix, dataset_name, field = parts
            if prefix == "data" and dataset_name in test_data_items:
                try:
                    parsed = json.loads(test_data_items[dataset_name].data)
                    if isinstance(parsed, dict) and field in parsed:
                        return str(parsed[field])
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning(f"Failed to parse test data '{dataset_name}': {e}")
            return match.group(0)

        elif len(parts) == 2:
            # 2-part: {{env.VAR}} for environment variables
            name, field = parts
            if name == "env":
                return str(_env_vars.get(field, match.group(0)))

            # 2-part: {{name.field}} for persona/credential
            if name in personas:
                persona = personas[name]
                if field == "username":
                    return persona.username or ""
                elif field == "password":
                    try:
                        if persona.encrypted_password:
                            return decrypt_password(persona.encrypted_password)
                    except Exception as e:
                        logger.warning(f"Failed to decrypt password for '{name}': {e}")
                    return match.group(0)
                elif field == "api_key":
                    try:
                        if persona.encrypted_api_key:
                            return decrypt_data(persona.encrypted_api_key)
                    except Exception as e:
                        logger.warning(f"Failed to decrypt api_key for '{name}': {e}")
                    return match.group(0)
                elif field == "token":
                    try:
                        if persona.encrypted_token:
                            return decrypt_data(persona.encrypted_token)
                    except Exception as e:
                        logger.warning(f"Failed to decrypt token for '{name}': {e}")
                    return match.group(0)
                else:
                    # Try custom metadata field
                    try:
                        if persona.encrypted_metadata:
                            metadata = json.loads(decrypt_data(persona.encrypted_metadata))
                            if isinstance(metadata, dict) and field in metadata:
                                return str(metadata[field])
                    except Exception as e:
                        logger.warning(f"Failed to decrypt metadata for '{name}': {e}")
            return match.group(0)

        else:
            # 1-part: {{name}} for page reference
            if ref in pages:
                return pages[ref].path
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
