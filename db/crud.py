"""CRUD operations for database models."""

from datetime import datetime, timedelta
from typing import List, Optional
from sqlmodel import Session, select

from db.models import (
    Project, ProjectCreate,
    TestCase, TestCaseCreate,
    TestRun, TestRunCreate,
    TestRunStep, TestRunStepCreate,
    Persona, PersonaCreate, PersonaUpdate,
    Page, PageCreate, PageUpdate,
    Fixture, FixtureCreate, FixtureUpdate,
    FixtureState, FixtureStateCreate,
    NotificationChannel, NotificationChannelCreate, NotificationChannelUpdate,
    Schedule, ScheduleCreate, ScheduleUpdate,
    ScheduledRun, ScheduledRunCreate,
    TestFolder, TestFolderCreate, TestFolderUpdate,
    TestData, TestDataCreate, TestDataUpdate,
    Environment, EnvironmentCreate, EnvironmentUpdate,
)
from db.encryption import encrypt_password, encrypt_data, decrypt_data


# --- Project CRUD ---

def _generate_prefix(name: str) -> str:
    """Generate a short uppercase prefix from project name.

    Single word: take consonants (up to 5), fallback to first 4 chars.
    Multiple words: first letter of each word (up to 5).
    """
    words = name.strip().split()
    if len(words) > 1:
        prefix = "".join(w[0] for w in words if w)[:5]
    else:
        word = words[0] if words else "PRJ"
        consonants = "".join(c for c in word if c.upper() not in "AEIOU")
        prefix = consonants[:5] if len(consonants) >= 2 else word[:4]
    return prefix.upper() or "PRJ"


def create_project(session: Session, project: ProjectCreate) -> Project:
    """Create a new project."""
    db_project = Project.model_validate(project)
    if not db_project.test_case_prefix:
        db_project.test_case_prefix = _generate_prefix(db_project.name)
    session.add(db_project)
    session.commit()
    session.refresh(db_project)
    return db_project


def get_project(session: Session, project_id: int) -> Optional[Project]:
    """Get a project by ID."""
    return session.get(Project, project_id)


def get_projects(session: Session, skip: int = 0, limit: int = 100) -> List[Project]:
    """Get all projects with pagination."""
    statement = select(Project).offset(skip).limit(limit)
    return session.exec(statement).all()


def update_project(session: Session, project_id: int, project_data: dict) -> Optional[Project]:
    """Update a project."""
    db_project = session.get(Project, project_id)
    if not db_project:
        return None

    for key, value in project_data.items():
        if hasattr(db_project, key):
            setattr(db_project, key, value)

    db_project.updated_at = datetime.utcnow()
    session.add(db_project)
    session.commit()
    session.refresh(db_project)
    return db_project


def delete_project(session: Session, project_id: int) -> bool:
    """Delete a project and all related test cases, test runs, personas, and pages."""
    db_project = session.get(Project, project_id)
    if not db_project:
        return False

    # Delete related test run steps and test runs
    test_runs = session.exec(
        select(TestRun).where(TestRun.project_id == project_id)
    ).all()
    for test_run in test_runs:
        # Delete steps for this test run
        steps = session.exec(
            select(TestRunStep).where(TestRunStep.test_run_id == test_run.id)
        ).all()
        for step in steps:
            session.delete(step)
        session.delete(test_run)

    # Delete related test cases
    test_cases = session.exec(
        select(TestCase).where(TestCase.project_id == project_id)
    ).all()
    for test_case in test_cases:
        session.delete(test_case)

    # Delete related personas
    personas = session.exec(
        select(Persona).where(Persona.project_id == project_id)
    ).all()
    for persona in personas:
        session.delete(persona)

    # Delete related pages
    pages = session.exec(
        select(Page).where(Page.project_id == project_id)
    ).all()
    for page in pages:
        session.delete(page)

    # Delete related fixtures
    fixtures = session.exec(
        select(Fixture).where(Fixture.project_id == project_id)
    ).all()
    for fixture in fixtures:
        session.delete(fixture)

    # Delete related schedules
    schedules = session.exec(
        select(Schedule).where(Schedule.project_id == project_id)
    ).all()
    for schedule in schedules:
        session.delete(schedule)

    # Delete related test data
    test_data_items = session.exec(
        select(TestData).where(TestData.project_id == project_id)
    ).all()
    for td in test_data_items:
        session.delete(td)

    # Delete the project
    session.delete(db_project)
    session.commit()
    return True


# --- TestCase CRUD ---

def create_test_case(session: Session, test_case: TestCaseCreate) -> TestCase:
    """Create a new test case with auto-assigned number."""
    db_test_case = TestCase.model_validate(test_case)
    project = session.get(Project, test_case.project_id)
    if project:
        db_test_case.test_case_number = project.next_test_case_number
        project.next_test_case_number += 1
        session.add(project)
    session.add(db_test_case)
    session.commit()
    session.refresh(db_test_case)
    return db_test_case


def get_test_case(session: Session, test_case_id: int) -> Optional[TestCase]:
    """Get a test case by ID."""
    return session.get(TestCase, test_case_id)


def get_test_cases_by_project(
    session: Session,
    project_id: int,
    skip: int = 0,
    limit: int = 100
) -> List[TestCase]:
    """Get all test cases for a project."""
    statement = (
        select(TestCase)
        .where(TestCase.project_id == project_id)
        .offset(skip)
        .limit(limit)
    )
    return session.exec(statement).all()


def update_test_case(session: Session, test_case_id: int, data: dict) -> Optional[TestCase]:
    """Update a test case."""
    db_test_case = session.get(TestCase, test_case_id)
    if not db_test_case:
        return None

    # Never overwrite immutable fields via update
    immutable = {"id", "project_id", "test_case_number", "created_at"}
    for key, value in data.items():
        if key in immutable:
            continue
        if hasattr(db_test_case, key):
            setattr(db_test_case, key, value)

    db_test_case.updated_at = datetime.utcnow()
    session.add(db_test_case)
    session.commit()
    session.refresh(db_test_case)
    return db_test_case


def update_test_case_status(
    session: Session,
    test_case_id: int,
    new_status: str,
) -> Optional[TestCase]:
    """Update test case status with transition validation.

    Valid transitions:
      draft -> ready (only if steps exist)
      ready -> in_review
      in_review -> approved
      in_review -> draft (send back)
      any -> archived
    """
    from db.models import TestCaseStatus
    import json

    db_test_case = session.get(TestCase, test_case_id)
    if not db_test_case:
        return None

    current = db_test_case.status
    target = new_status

    # Always allow archiving and skipping from any status
    if target in (TestCaseStatus.ARCHIVED, TestCaseStatus.SKIPPED):
        db_test_case.status = target
        db_test_case.updated_at = datetime.utcnow()
        session.add(db_test_case)
        session.commit()
        session.refresh(db_test_case)
        return db_test_case

    # Validate transitions
    valid_transitions = {
        TestCaseStatus.DRAFT: [TestCaseStatus.READY, TestCaseStatus.ACTIVE],
        TestCaseStatus.ACTIVE: [TestCaseStatus.READY, TestCaseStatus.DRAFT],
        TestCaseStatus.READY: [TestCaseStatus.IN_REVIEW, TestCaseStatus.DRAFT],
        TestCaseStatus.IN_REVIEW: [TestCaseStatus.APPROVED, TestCaseStatus.DRAFT],
        TestCaseStatus.APPROVED: [TestCaseStatus.DRAFT, TestCaseStatus.IN_REVIEW],
        TestCaseStatus.SKIPPED: [TestCaseStatus.DRAFT],
    }

    allowed = valid_transitions.get(current, [])
    if target not in allowed:
        raise ValueError(f"Cannot transition from {current} to {target}")

    # Draft -> Ready requires steps
    if current == TestCaseStatus.DRAFT and target == TestCaseStatus.READY:
        steps = json.loads(db_test_case.steps) if db_test_case.steps else []
        if not steps:
            raise ValueError("Cannot mark as Ready: test case has no steps")

    db_test_case.status = target
    db_test_case.updated_at = datetime.utcnow()
    session.add(db_test_case)
    session.commit()
    session.refresh(db_test_case)
    return db_test_case


def update_test_case_visibility(
    session: Session,
    test_case_id: int,
    visibility: str,
) -> Optional[TestCase]:
    """Update test case visibility (private/public)."""
    db_test_case = session.get(TestCase, test_case_id)
    if not db_test_case:
        return None

    if visibility not in ("private", "public"):
        raise ValueError(f"Invalid visibility: {visibility}")

    db_test_case.visibility = visibility
    db_test_case.updated_at = datetime.utcnow()
    session.add(db_test_case)
    session.commit()
    session.refresh(db_test_case)
    return db_test_case


def delete_test_case(session: Session, test_case_id: int) -> bool:
    """Delete a test case."""
    db_test_case = session.get(TestCase, test_case_id)
    if not db_test_case:
        return False

    session.delete(db_test_case)
    session.commit()
    return True


# --- TestRun CRUD ---

def create_test_run(session: Session, test_run: TestRunCreate) -> TestRun:
    """Create a new test run."""
    db_test_run = TestRun.model_validate(test_run)
    session.add(db_test_run)
    session.commit()
    session.refresh(db_test_run)
    return db_test_run


def get_test_run(session: Session, test_run_id: int) -> Optional[TestRun]:
    """Get a test run by ID."""
    return session.get(TestRun, test_run_id)


def get_test_runs_by_project(
    session: Session,
    project_id: int,
    skip: int = 0,
    limit: int = 100
) -> List[TestRun]:
    """Get all test runs for a project."""
    statement = (
        select(TestRun)
        .where(TestRun.project_id == project_id)
        .order_by(TestRun.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    return session.exec(statement).all()


def get_test_runs_by_test_case(
    session: Session,
    test_case_id: int,
    skip: int = 0,
    limit: int = 100
) -> List[TestRun]:
    """Get all test runs for a specific test case."""
    statement = (
        select(TestRun)
        .where(TestRun.test_case_id == test_case_id)
        .order_by(TestRun.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    return session.exec(statement).all()


def get_test_runs_by_thread_id(
    session: Session,
    project_id: int,
    thread_id: str
) -> List[TestRun]:
    """Get all test runs for a specific thread_id (batch/scheduled run)."""
    statement = (
        select(TestRun)
        .where(TestRun.project_id == project_id)
        .where(TestRun.thread_id == thread_id)
        .order_by(TestRun.created_at.asc())
    )
    return session.exec(statement).all()


def delete_test_run(session: Session, test_run_id: int) -> bool:
    """Delete a single test run and its steps."""
    db_run = session.get(TestRun, test_run_id)
    if not db_run:
        return False

    steps = session.exec(
        select(TestRunStep).where(TestRunStep.test_run_id == test_run_id)
    ).all()
    for step in steps:
        session.delete(step)

    session.delete(db_run)
    session.commit()
    return True


def delete_test_runs_by_project(session: Session, project_id: int) -> int:
    """Delete all test runs for a project. Returns count deleted."""
    runs = session.exec(
        select(TestRun).where(TestRun.project_id == project_id)
    ).all()

    count = len(runs)
    for run in runs:
        steps = session.exec(
            select(TestRunStep).where(TestRunStep.test_run_id == run.id)
        ).all()
        for step in steps:
            session.delete(step)
        session.delete(run)

    session.commit()
    return count


def delete_test_runs_by_test_case(session: Session, test_case_id: int) -> int:
    """Delete all test runs for a specific test case. Returns count deleted."""
    runs = session.exec(
        select(TestRun).where(TestRun.test_case_id == test_case_id)
    ).all()

    count = len(runs)
    for run in runs:
        steps = session.exec(
            select(TestRunStep).where(TestRunStep.test_run_id == run.id)
        ).all()
        for step in steps:
            session.delete(step)
        session.delete(run)

    session.commit()
    return count


def update_test_run(session: Session, test_run_id: int, data: dict) -> Optional[TestRun]:
    """Update a test run."""
    db_test_run = session.get(TestRun, test_run_id)
    if not db_test_run:
        return None

    for key, value in data.items():
        if hasattr(db_test_run, key):
            setattr(db_test_run, key, value)

    session.add(db_test_run)
    session.commit()
    session.refresh(db_test_run)
    return db_test_run


# --- TestRunStep CRUD ---

def create_test_run_step(session: Session, step: TestRunStepCreate) -> TestRunStep:
    """Create a new test run step."""
    db_step = TestRunStep.model_validate(step)
    session.add(db_step)
    session.commit()
    session.refresh(db_step)
    return db_step


def get_test_run_steps(session: Session, test_run_id: int) -> List[TestRunStep]:
    """Get all steps for a test run."""
    statement = (
        select(TestRunStep)
        .where(TestRunStep.test_run_id == test_run_id)
        .order_by(TestRunStep.step_number)
    )
    return session.exec(statement).all()


# --- Persona CRUD ---

def create_persona(session: Session, persona: PersonaCreate) -> Persona:
    """Create a new persona/credential with encrypted secrets."""
    import json

    cred_type = persona.credential_type or "login"

    db_persona = Persona(
        name=persona.name,
        username=persona.username,
        description=persona.description,
        project_id=persona.project_id,
        environment_id=persona.environment_id,
        credential_type=cred_type,
        encrypted_password=encrypt_password(persona.password) if persona.password else None,
        encrypted_api_key=encrypt_data(persona.api_key) if persona.api_key else None,
        encrypted_token=encrypt_data(persona.token) if persona.token else None,
        encrypted_metadata=encrypt_data(json.dumps(persona.custom_fields)) if persona.custom_fields else None,
    )
    session.add(db_persona)
    session.commit()
    session.refresh(db_persona)
    return db_persona


def get_persona(session: Session, persona_id: int) -> Optional[Persona]:
    """Get a persona by ID."""
    return session.get(Persona, persona_id)


def get_personas_by_project(
    session: Session,
    project_id: int,
    environment_id: Optional[int] = None,
) -> List[Persona]:
    """Get personas for a project.

    If environment_id is given, returns env-specific items PLUS global items
    (environment_id IS NULL). Otherwise returns all personas for the project.
    """
    stmt = select(Persona).where(Persona.project_id == project_id)
    if environment_id is not None:
        stmt = stmt.where(
            (Persona.environment_id == environment_id) | (Persona.environment_id == None)  # noqa: E711
        )
    return session.exec(stmt).all()


def update_persona(session: Session, persona_id: int, data: PersonaUpdate) -> Optional[Persona]:
    """Update a persona/credential. Secrets are re-encrypted if provided."""
    import json

    db_persona = session.get(Persona, persona_id)
    if not db_persona:
        return None

    update_data = data.model_dump(exclude_unset=True)

    # Handle encrypted fields separately
    if "password" in update_data and update_data["password"]:
        db_persona.encrypted_password = encrypt_password(update_data.pop("password"))
    elif "password" in update_data:
        update_data.pop("password")

    if "api_key" in update_data and update_data["api_key"]:
        db_persona.encrypted_api_key = encrypt_data(update_data.pop("api_key"))
    elif "api_key" in update_data:
        update_data.pop("api_key")

    if "token" in update_data and update_data["token"]:
        db_persona.encrypted_token = encrypt_data(update_data.pop("token"))
    elif "token" in update_data:
        update_data.pop("token")

    if "custom_fields" in update_data and update_data["custom_fields"]:
        db_persona.encrypted_metadata = encrypt_data(json.dumps(update_data.pop("custom_fields")))
    elif "custom_fields" in update_data:
        update_data.pop("custom_fields")

    for key, value in update_data.items():
        if hasattr(db_persona, key):
            setattr(db_persona, key, value)

    db_persona.updated_at = datetime.utcnow()
    session.add(db_persona)
    session.commit()
    session.refresh(db_persona)
    return db_persona


def delete_persona(session: Session, persona_id: int) -> bool:
    """Delete a persona."""
    db_persona = session.get(Persona, persona_id)
    if not db_persona:
        return False

    session.delete(db_persona)
    session.commit()
    return True


# --- Page CRUD ---

def create_page(session: Session, page: PageCreate) -> Page:
    """Create a new page."""
    db_page = Page.model_validate(page)
    session.add(db_page)
    session.commit()
    session.refresh(db_page)
    return db_page


def get_page(session: Session, page_id: int) -> Optional[Page]:
    """Get a page by ID."""
    return session.get(Page, page_id)


def get_pages_by_project(session: Session, project_id: int) -> List[Page]:
    """Get all pages for a project."""
    statement = select(Page).where(Page.project_id == project_id)
    return session.exec(statement).all()


def update_page(session: Session, page_id: int, data: PageUpdate) -> Optional[Page]:
    """Update a page."""
    db_page = session.get(Page, page_id)
    if not db_page:
        return None

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        if hasattr(db_page, key):
            setattr(db_page, key, value)

    db_page.updated_at = datetime.utcnow()
    session.add(db_page)
    session.commit()
    session.refresh(db_page)
    return db_page


def delete_page(session: Session, page_id: int) -> bool:
    """Delete a page."""
    db_page = session.get(Page, page_id)
    if not db_page:
        return False

    session.delete(db_page)
    session.commit()
    return True


# --- Fixture CRUD ---

def create_fixture(session: Session, fixture: FixtureCreate) -> Fixture:
    """Create a new fixture."""
    db_fixture = Fixture.model_validate(fixture)
    session.add(db_fixture)
    session.commit()
    session.refresh(db_fixture)
    return db_fixture


def get_fixture(session: Session, fixture_id: int) -> Optional[Fixture]:
    """Get a fixture by ID."""
    return session.get(Fixture, fixture_id)


def get_fixtures_by_project(session: Session, project_id: int) -> List[Fixture]:
    """Get all fixtures for a project."""
    statement = select(Fixture).where(Fixture.project_id == project_id)
    return session.exec(statement).all()


def get_fixtures_by_ids(session: Session, fixture_ids: List[int]) -> List[Fixture]:
    """Get fixtures by their IDs."""
    if not fixture_ids:
        return []
    statement = select(Fixture).where(Fixture.id.in_(fixture_ids))
    return session.exec(statement).all()


def update_fixture(session: Session, fixture_id: int, data: FixtureUpdate) -> Optional[Fixture]:
    """Update a fixture."""
    db_fixture = session.get(Fixture, fixture_id)
    if not db_fixture:
        return None

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        if hasattr(db_fixture, key):
            setattr(db_fixture, key, value)

    db_fixture.updated_at = datetime.utcnow()
    session.add(db_fixture)
    session.commit()
    session.refresh(db_fixture)
    return db_fixture


def delete_fixture(session: Session, fixture_id: int) -> bool:
    """Delete a fixture and all related fixture states."""
    db_fixture = session.get(Fixture, fixture_id)
    if not db_fixture:
        return False

    session.delete(db_fixture)
    session.commit()
    return True


# --- FixtureState CRUD ---

def create_fixture_state(
    session: Session,
    fixture_id: int,
    project_id: int,
    url: Optional[str] = None,
    state_json: Optional[str] = None,
    browser: Optional[str] = None,
    expires_at: Optional[datetime] = None
) -> FixtureState:
    """Create a new fixture state with encrypted data.
    
    Args:
        session: Database session
        fixture_id: Fixture ID
        project_id: Project ID
        url: URL where state was captured
        state_json: JSON string of Playwright storage_state (cookies + origins)
        browser: Browser type (e.g., 'chromium-headless')
        expires_at: Expiration timestamp
    """
    db_state = FixtureState(
        fixture_id=fixture_id,
        project_id=project_id,
        url=url,
        encrypted_state_json=encrypt_data(state_json) if state_json else None,
        browser=browser,
        expires_at=expires_at,
    )
    session.add(db_state)
    session.commit()
    session.refresh(db_state)
    return db_state


def get_fixture_state(session: Session, state_id: int) -> Optional[FixtureState]:
    """Get a fixture state by ID."""
    return session.get(FixtureState, state_id)


def get_valid_fixture_state(
    session: Session,
    fixture_id: int,
    browser: Optional[str] = None
) -> Optional[FixtureState]:
    """Get a valid (non-expired) fixture state for a fixture.

    Args:
        session: Database session
        fixture_id: Fixture ID
        browser: Optional browser filter (e.g., 'chromium-headless')

    Returns:
        Valid FixtureState or None if no valid state exists
    """
    now = datetime.utcnow()

    statement = (
        select(FixtureState)
        .where(FixtureState.fixture_id == fixture_id)
        .where(FixtureState.expires_at > now)
        .order_by(FixtureState.captured_at.desc())
    )

    if browser:
        statement = statement.where(FixtureState.browser == browser)

    return session.exec(statement).first()


def get_decrypted_fixture_state(session: Session, state: FixtureState) -> dict:
    """Decrypt fixture state data.

    Args:
        session: Database session (unused but kept for consistency)
        state: FixtureState to decrypt

    Returns:
        dict with url, state (Playwright storage_state), and browser
    """
    import json

    state_data = None
    if state.encrypted_state_json:
        decrypted = decrypt_data(state.encrypted_state_json)
        state_data = json.loads(decrypted) if decrypted else None

    return {
        "url": state.url,
        "state": state_data,
        "browser": state.browser,
    }


def delete_fixture_state(session: Session, state_id: int) -> bool:
    """Delete a fixture state."""
    db_state = session.get(FixtureState, state_id)
    if not db_state:
        return False

    session.delete(db_state)
    session.commit()
    return True


def delete_fixture_states_by_fixture(session: Session, fixture_id: int) -> int:
    """Delete all fixture states for a fixture.

    Returns:
        Number of states deleted
    """
    states = session.exec(
        select(FixtureState).where(FixtureState.fixture_id == fixture_id)
    ).all()

    count = len(states)
    for state in states:
        session.delete(state)

    session.commit()
    return count


def delete_expired_fixture_states(session: Session) -> int:
    """Delete all expired fixture states.

    Returns:
        Number of states deleted
    """
    now = datetime.utcnow()
    expired_states = session.exec(
        select(FixtureState).where(FixtureState.expires_at <= now)
    ).all()

    count = len(expired_states)
    for state in expired_states:
        session.delete(state)

    session.commit()
    return count


# --- TestFolder CRUD ---

def create_folder(session: Session, folder: TestFolderCreate) -> TestFolder:
    """Create a new folder. Validates 2-level max nesting."""
    if folder.parent_id is not None:
        parent = session.get(TestFolder, folder.parent_id)
        if not parent:
            raise ValueError("Parent folder not found")
        if parent.project_id != folder.project_id:
            raise ValueError("Parent folder belongs to a different project")
        if parent.parent_id is not None:
            raise ValueError("Maximum folder nesting depth is 2 levels")

    db_folder = TestFolder.model_validate(folder)
    session.add(db_folder)
    session.commit()
    session.refresh(db_folder)
    return db_folder


def get_folder(session: Session, folder_id: int) -> Optional[TestFolder]:
    """Get a folder by ID."""
    return session.get(TestFolder, folder_id)


def get_folders_by_project(session: Session, project_id: int) -> List[TestFolder]:
    """Get all folders for a project, ordered by parent_id then order_index."""
    statement = (
        select(TestFolder)
        .where(TestFolder.project_id == project_id)
        .order_by(TestFolder.parent_id, TestFolder.order_index, TestFolder.name)
    )
    return session.exec(statement).all()


def update_folder(session: Session, folder_id: int, data: TestFolderUpdate) -> Optional[TestFolder]:
    """Update a folder. Validates nesting on parent change."""
    db_folder = session.get(TestFolder, folder_id)
    if not db_folder:
        return None

    update_data = data.model_dump(exclude_unset=True)

    # Smart folders: only allow name, description, order_index edits
    if db_folder.folder_type == "smart":
        allowed = {"name", "description", "order_index", "smart_criteria"}
        update_data = {k: v for k, v in update_data.items() if k in allowed}

    # Validate nesting if parent is changing
    if "parent_id" in update_data and update_data["parent_id"] is not None:
        new_parent_id = update_data["parent_id"]
        if new_parent_id == folder_id:
            raise ValueError("Cannot set folder as its own parent")
        parent = session.get(TestFolder, new_parent_id)
        if not parent:
            raise ValueError("Parent folder not found")
        if parent.project_id != db_folder.project_id:
            raise ValueError("Parent folder belongs to a different project")
        if parent.parent_id is not None:
            raise ValueError("Maximum folder nesting depth is 2 levels")

    for key, value in update_data.items():
        if hasattr(db_folder, key):
            setattr(db_folder, key, value)

    db_folder.updated_at = datetime.utcnow()
    session.add(db_folder)
    session.commit()
    session.refresh(db_folder)
    return db_folder


def delete_folder(session: Session, folder_id: int) -> bool:
    """Delete a folder only if it has no test cases."""
    db_folder = session.get(TestFolder, folder_id)
    if not db_folder:
        return False

    if db_folder.folder_type == "smart":
        session.delete(db_folder)
        session.commit()
        return True

    # Check for test cases in this folder
    test_cases = session.exec(
        select(TestCase).where(TestCase.folder_id == folder_id)
    ).all()
    if len(test_cases) > 0:
        raise ValueError(
            f"Folder contains {len(test_cases)} test case(s). "
            "Move them to another folder before deleting."
        )

    # Orphan child folders to root
    children = session.exec(
        select(TestFolder).where(TestFolder.parent_id == folder_id)
    ).all()
    for child in children:
        child.parent_id = None
        child.updated_at = datetime.utcnow()
        session.add(child)

    session.delete(db_folder)
    session.commit()
    return True


def get_test_cases_by_folder(
    session: Session,
    folder_id: int,
    include_descendants: bool = False,
) -> List[TestCase]:
    """Get test cases in a folder. Optionally include sub-folder contents."""
    if not include_descendants:
        statement = select(TestCase).where(TestCase.folder_id == folder_id)
        return session.exec(statement).all()

    folder_ids = [folder_id]
    children = session.exec(
        select(TestFolder).where(TestFolder.parent_id == folder_id)
    ).all()
    folder_ids.extend(c.id for c in children)

    statement = select(TestCase).where(TestCase.folder_id.in_(folder_ids))
    return session.exec(statement).all()


def compute_smart_folder_tests(session: Session, folder: TestFolder) -> List[TestCase]:
    """Evaluate smart_criteria against all project test cases."""
    criteria = folder.get_smart_criteria()
    if not criteria:
        return []

    filter_tags = criteria.get("tags", [])
    filter_statuses = criteria.get("statuses", [])

    statement = select(TestCase).where(TestCase.project_id == folder.project_id)

    if filter_statuses:
        statement = statement.where(TestCase.status.in_(filter_statuses))

    all_cases = session.exec(statement).all()

    if filter_tags:
        filter_tags_lower = [t.lower() for t in filter_tags]
        matching = []
        for tc in all_cases:
            tc_tags_lower = [t.lower() for t in tc.get_tags()]
            if any(tag in tc_tags_lower for tag in filter_tags_lower):
                matching.append(tc)
        return matching

    return list(all_cases)


def move_test_case_to_folder(
    session: Session,
    test_case_id: int,
    folder_id: Optional[int],
) -> Optional[TestCase]:
    """Move a test case to a folder (or root if folder_id is None)."""
    db_tc = session.get(TestCase, test_case_id)
    if not db_tc:
        return None

    if folder_id is not None:
        folder = session.get(TestFolder, folder_id)
        if not folder:
            raise ValueError("Target folder not found")
        if folder.project_id != db_tc.project_id:
            raise ValueError("Target folder belongs to a different project")
        if folder.folder_type == "smart":
            raise ValueError("Cannot move test cases into a smart folder")

    db_tc.folder_id = folder_id
    db_tc.updated_at = datetime.utcnow()
    session.add(db_tc)
    session.commit()
    session.refresh(db_tc)
    return db_tc


def move_folder(
    session: Session,
    folder_id: int,
    new_parent_id: Optional[int],
) -> Optional[TestFolder]:
    """Move a folder to a new parent (or root if None)."""
    db_folder = session.get(TestFolder, folder_id)
    if not db_folder:
        return None

    if new_parent_id is not None:
        if new_parent_id == folder_id:
            raise ValueError("Cannot set folder as its own parent")
        parent = session.get(TestFolder, new_parent_id)
        if not parent:
            raise ValueError("Parent folder not found")
        if parent.project_id != db_folder.project_id:
            raise ValueError("Parent folder belongs to a different project")
        if parent.parent_id is not None:
            raise ValueError("Maximum folder nesting depth is 2 levels")
        # Prevent circular reference
        children = session.exec(
            select(TestFolder).where(TestFolder.parent_id == folder_id)
        ).all()
        if any(c.id == new_parent_id for c in children):
            raise ValueError("Cannot create circular folder reference")

    db_folder.parent_id = new_parent_id
    db_folder.updated_at = datetime.utcnow()
    session.add(db_folder)
    session.commit()
    session.refresh(db_folder)
    return db_folder


# --- Stats ---

def get_stats(session: Session) -> dict:
    """Get global statistics for the dashboard."""
    from datetime import timedelta
    from sqlmodel import func

    # Total counts
    total_projects = session.exec(select(func.count(Project.id))).one()
    total_test_cases = session.exec(select(func.count(TestCase.id))).one()

    # Recent runs (last 7 days)
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    recent_runs = session.exec(
        select(func.count(TestRun.id)).where(TestRun.created_at >= seven_days_ago)
    ).one()

    # Pass rate (from completed runs only)
    from db.models import RunStatus
    completed_statuses = [RunStatus.PASSED, RunStatus.FAILED]
    completed_runs = session.exec(
        select(func.count(TestRun.id)).where(TestRun.status.in_(completed_statuses))
    ).one()
    passed_runs = session.exec(
        select(func.count(TestRun.id)).where(TestRun.status == RunStatus.PASSED)
    ).one()

    pass_rate = round((passed_runs / completed_runs * 100), 1) if completed_runs > 0 else 0

    return {
        "total_projects": total_projects,
        "total_test_cases": total_test_cases,
        "recent_runs": recent_runs,
        "pass_rate": pass_rate,
    }


# --- Project Dashboard ---

def get_project_dashboard(session: Session, project_id: int) -> dict:
    """Return all data needed for the project dashboard in one call."""
    from sqlmodel import func
    from db.models import RunStatus
    import json as _json
    from collections import defaultdict

    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    fourteen_days_ago = datetime.utcnow() - timedelta(days=14)

    # --- KPIs ---
    total_tests = session.exec(
        select(func.count(TestCase.id)).where(TestCase.project_id == project_id)
    ).one()

    executed_tc_ids = session.exec(
        select(TestRun.test_case_id).distinct()
        .where(TestRun.project_id == project_id)
        .where(TestRun.created_at >= thirty_days_ago)
        .where(TestRun.test_case_id.isnot(None))
    ).all()
    executed_pct = round(len(executed_tc_ids) / total_tests * 100, 1) if total_tests else 0.0

    passed_30d = session.exec(
        select(func.count(TestRun.id))
        .where(TestRun.project_id == project_id)
        .where(TestRun.status == RunStatus.PASSED)
        .where(TestRun.created_at >= thirty_days_ago)
    ).one()
    failed_30d = session.exec(
        select(func.count(TestRun.id))
        .where(TestRun.project_id == project_id)
        .where(TestRun.status == RunStatus.FAILED)
        .where(TestRun.created_at >= thirty_days_ago)
    ).one()
    completed_30d = passed_30d + failed_30d
    pass_rate = round(passed_30d / completed_30d * 100, 1) if completed_30d else 0.0

    completed_runs_raw = session.exec(
        select(TestRun)
        .where(TestRun.project_id == project_id)
        .where(TestRun.status.in_([RunStatus.PASSED, RunStatus.FAILED]))
        .where(TestRun.started_at.isnot(None))
        .where(TestRun.completed_at.isnot(None))
    ).all()
    durations = [
        (r.completed_at - r.started_at).total_seconds() * 1000
        for r in completed_runs_raw
        if r.completed_at and r.started_at
    ]
    avg_duration_ms = round(sum(durations) / len(durations)) if durations else 0.0

    kpis = {
        "total_tests": total_tests,
        "executed_pct": executed_pct,
        "pass_rate": pass_rate,
        "fail_count": failed_30d,
        "avg_duration_ms": avg_duration_ms,
    }

    # --- Status breakdown: latest run per test case ---
    all_tc_ids = session.exec(
        select(TestCase.id).where(TestCase.project_id == project_id)
    ).all()

    status_passed = status_failed = status_not_run = 0
    for tc_id in all_tc_ids:
        latest = session.exec(
            select(TestRun)
            .where(TestRun.test_case_id == tc_id)
            .order_by(TestRun.created_at.desc())
            .limit(1)
        ).first()
        if not latest or latest.status not in (RunStatus.PASSED, RunStatus.FAILED):
            status_not_run += 1
        elif latest.status == RunStatus.PASSED:
            status_passed += 1
        else:
            status_failed += 1

    status_breakdown = {"passed": status_passed, "failed": status_failed, "not_run": status_not_run}

    # --- Daily runs: last 14 calendar days ---
    daily_raw = session.exec(
        select(TestRun)
        .where(TestRun.project_id == project_id)
        .where(TestRun.created_at >= fourteen_days_ago)
        .where(TestRun.status.in_([RunStatus.PASSED, RunStatus.FAILED]))
    ).all()

    daily_map: dict = defaultdict(lambda: {"passed": 0, "failed": 0})
    for run in daily_raw:
        day = run.created_at.strftime("%Y-%m-%d")
        if run.status == RunStatus.PASSED:
            daily_map[day]["passed"] += 1
        else:
            daily_map[day]["failed"] += 1

    daily_runs = []
    for i in range(14):
        d = (datetime.utcnow() - timedelta(days=13 - i)).strftime("%Y-%m-%d")
        daily_runs.append({"date": d, **daily_map.get(d, {"passed": 0, "failed": 0})})

    # --- Module health + bottlenecks: group by first tag ---
    all_tcs = session.exec(
        select(TestCase).where(TestCase.project_id == project_id)
    ).all()

    module_map: dict = defaultdict(lambda: {"passed": 0, "failed": 0, "not_run": 0})
    bottleneck_map: dict = defaultdict(lambda: {"failed": 0, "total": 0})

    for tc in all_tcs:
        try:
            tags = _json.loads(tc.tags) if tc.tags else []
        except Exception:
            tags = []
        module = tags[0] if tags else "Untagged"

        latest = session.exec(
            select(TestRun)
            .where(TestRun.test_case_id == tc.id)
            .order_by(TestRun.created_at.desc())
            .limit(1)
        ).first()

        if not latest or latest.status not in (RunStatus.PASSED, RunStatus.FAILED):
            module_map[module]["not_run"] += 1
        elif latest.status == RunStatus.PASSED:
            module_map[module]["passed"] += 1
            bottleneck_map[module]["total"] += 1
        else:
            module_map[module]["failed"] += 1
            bottleneck_map[module]["failed"] += 1
            bottleneck_map[module]["total"] += 1

    module_health = [{"module": m, **v} for m, v in module_map.items()]

    top_bottlenecks = sorted(
        [
            {
                "module": m,
                "fail_rate": round(v["failed"] / v["total"] * 100, 1) if v["total"] else 0.0,
                "failed": v["failed"],
                "total": v["total"],
            }
            for m, v in bottleneck_map.items()
            if v["total"] > 0
        ],
        key=lambda x: x["fail_rate"],
        reverse=True,
    )[:3]

    # --- Browser stats (last 30d) ---
    browser_runs = session.exec(
        select(TestRun)
        .where(TestRun.project_id == project_id)
        .where(TestRun.created_at >= thirty_days_ago)
        .where(TestRun.status.in_([RunStatus.PASSED, RunStatus.FAILED]))
    ).all()

    browser_map: dict = defaultdict(lambda: {"count": 0, "passed": 0})
    for run in browser_runs:
        b = run.browser or "unknown"
        browser_map[b]["count"] += 1
        if run.status == RunStatus.PASSED:
            browser_map[b]["passed"] += 1

    browser_stats = [{"browser": b, **v} for b, v in browser_map.items()]

    # --- Recent runs: 8 most recent completed ---
    recent_runs_raw = session.exec(
        select(TestRun)
        .where(TestRun.project_id == project_id)
        .where(TestRun.status.in_([RunStatus.PASSED, RunStatus.FAILED]))
        .order_by(TestRun.created_at.desc())
        .limit(10)
    ).all()

    recent_runs = []
    for run in recent_runs_raw:
        tc = session.get(TestCase, run.test_case_id) if run.test_case_id else None
        dur_ms = None
        if run.started_at and run.completed_at:
            dur_ms = int((run.completed_at - run.started_at).total_seconds() * 1000)
        recent_runs.append({
            "id": run.id,
            "test_case_name": tc.name if tc else "—",
            "status": run.status,
            "browser": run.browser,
            "created_at": run.created_at.isoformat(),
            "duration_ms": dur_ms,
        })

    # --- Release recommendation ---
    not_run_pct = status_not_run / total_tests * 100 if total_tests else 100
    if pass_rate >= 90 and not_run_pct < 10:
        recommendation = "GO"
    elif pass_rate < 70 or failed_30d > 20:
        recommendation = "NO-GO"
    else:
        recommendation = "CONDITIONAL"

    return {
        "kpis": kpis,
        "status_breakdown": status_breakdown,
        "daily_runs": daily_runs,
        "module_health": module_health,
        "browser_stats": browser_stats,
        "recent_runs": recent_runs,
        "top_bottlenecks": top_bottlenecks,
        "release_recommendation": recommendation,
    }


# --- NotificationChannel CRUD ---

def create_notification_channel(session: Session, channel: NotificationChannelCreate) -> NotificationChannel:
    """Create a new notification channel."""
    db_channel = NotificationChannel.model_validate(channel)
    session.add(db_channel)
    session.commit()
    session.refresh(db_channel)
    return db_channel


def get_notification_channel(session: Session, channel_id: int) -> Optional[NotificationChannel]:
    """Get a notification channel by ID."""
    return session.get(NotificationChannel, channel_id)


def get_notification_channels_by_project(session: Session, project_id: int) -> List[NotificationChannel]:
    """Get all notification channels for a project."""
    statement = select(NotificationChannel).where(NotificationChannel.project_id == project_id)
    return session.exec(statement).all()


def get_notification_channels_by_ids(session: Session, channel_ids: List[int]) -> List[NotificationChannel]:
    """Get notification channels by their IDs."""
    if not channel_ids:
        return []
    statement = select(NotificationChannel).where(NotificationChannel.id.in_(channel_ids))
    return session.exec(statement).all()


def update_notification_channel(session: Session, channel_id: int, data: NotificationChannelUpdate) -> Optional[NotificationChannel]:
    """Update a notification channel."""
    db_channel = session.get(NotificationChannel, channel_id)
    if not db_channel:
        return None

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        if hasattr(db_channel, key):
            setattr(db_channel, key, value)

    db_channel.updated_at = datetime.utcnow()
    session.add(db_channel)
    session.commit()
    session.refresh(db_channel)
    return db_channel


def delete_notification_channel(session: Session, channel_id: int) -> bool:
    """Delete a notification channel."""
    db_channel = session.get(NotificationChannel, channel_id)
    if not db_channel:
        return False

    session.delete(db_channel)
    session.commit()
    return True


# --- Schedule CRUD ---

def create_schedule(session: Session, schedule: ScheduleCreate) -> Schedule:
    """Create a new schedule."""
    db_schedule = Schedule.model_validate(schedule)
    session.add(db_schedule)
    session.commit()
    session.refresh(db_schedule)
    return db_schedule


def get_schedule(session: Session, schedule_id: int) -> Optional[Schedule]:
    """Get a schedule by ID."""
    return session.get(Schedule, schedule_id)


def get_schedules_by_project(session: Session, project_id: int) -> List[Schedule]:
    """Get all schedules for a project."""
    statement = select(Schedule).where(Schedule.project_id == project_id)
    return session.exec(statement).all()


def get_all_enabled_schedules(session: Session) -> List[Schedule]:
    """Get all enabled schedules across all projects."""
    statement = select(Schedule).where(Schedule.enabled == True)
    return session.exec(statement).all()


def update_schedule(session: Session, schedule_id: int, data: ScheduleUpdate) -> Optional[Schedule]:
    """Update a schedule."""
    db_schedule = session.get(Schedule, schedule_id)
    if not db_schedule:
        return None

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        if hasattr(db_schedule, key):
            setattr(db_schedule, key, value)

    db_schedule.updated_at = datetime.utcnow()
    session.add(db_schedule)
    session.commit()
    session.refresh(db_schedule)
    return db_schedule


def update_schedule_run_times(session: Session, schedule_id: int, last_run_at: Optional[datetime], next_run_at: Optional[datetime]) -> Optional[Schedule]:
    """Update schedule run times."""
    db_schedule = session.get(Schedule, schedule_id)
    if not db_schedule:
        return None

    if last_run_at is not None:
        db_schedule.last_run_at = last_run_at
    if next_run_at is not None:
        db_schedule.next_run_at = next_run_at

    db_schedule.updated_at = datetime.utcnow()
    session.add(db_schedule)
    session.commit()
    session.refresh(db_schedule)
    return db_schedule


def try_claim_schedule_execution(session: Session, schedule_id: int, claim_window_seconds: int = 60) -> bool:
    """
    Atomically try to claim a schedule for execution (distributed lock).

    Uses optimistic locking: updates last_run_at only if it hasn't been
    updated within the claim window. This prevents multiple pods from
    executing the same schedule simultaneously.

    Args:
        session: Database session
        schedule_id: Schedule to claim
        claim_window_seconds: Minimum seconds between executions (default 60)

    Returns:
        True if this instance claimed the execution, False if another instance did
    """
    from sqlalchemy import update, or_

    now = datetime.utcnow()
    threshold = now - timedelta(seconds=claim_window_seconds)

    # Atomic UPDATE with condition - only succeeds if not recently claimed
    # Using SQLAlchemy ORM for cross-database compatibility (SQLite + PostgreSQL)
    stmt = (
        update(Schedule)
        .where(Schedule.id == schedule_id)
        .where(Schedule.enabled == True)
        .where(or_(Schedule.last_run_at == None, Schedule.last_run_at < threshold))
        .values(last_run_at=now, updated_at=now)
    )
    result = session.execute(stmt)
    session.commit()

    # If rowcount is 1, we successfully claimed it
    return result.rowcount == 1


def delete_schedule(session: Session, schedule_id: int) -> bool:
    """Delete a schedule and all related scheduled runs."""
    db_schedule = session.get(Schedule, schedule_id)
    if not db_schedule:
        return False

    session.delete(db_schedule)
    session.commit()
    return True


# --- ScheduledRun CRUD ---

def create_scheduled_run(session: Session, run: ScheduledRunCreate) -> ScheduledRun:
    """Create a new scheduled run."""
    db_run = ScheduledRun.model_validate(run)
    session.add(db_run)
    session.commit()
    session.refresh(db_run)
    return db_run


def get_scheduled_run(session: Session, run_id: int) -> Optional[ScheduledRun]:
    """Get a scheduled run by ID."""
    return session.get(ScheduledRun, run_id)


def get_scheduled_runs_by_schedule(session: Session, schedule_id: int, skip: int = 0, limit: int = 50) -> List[ScheduledRun]:
    """Get all scheduled runs for a schedule."""
    statement = (
        select(ScheduledRun)
        .where(ScheduledRun.schedule_id == schedule_id)
        .order_by(ScheduledRun.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    return session.exec(statement).all()


def get_scheduled_runs_by_project(session: Session, project_id: int, skip: int = 0, limit: int = 50) -> List[ScheduledRun]:
    """Get all scheduled runs for a project."""
    statement = (
        select(ScheduledRun)
        .where(ScheduledRun.project_id == project_id)
        .order_by(ScheduledRun.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    return session.exec(statement).all()


def update_scheduled_run(session: Session, run_id: int, data: dict) -> Optional[ScheduledRun]:
    """Update a scheduled run."""
    db_run = session.get(ScheduledRun, run_id)
    if not db_run:
        return None

    for key, value in data.items():
        if hasattr(db_run, key):
            setattr(db_run, key, value)

    session.add(db_run)
    session.commit()
    session.refresh(db_run)
    return db_run


def get_test_cases_by_tags(session: Session, project_id: int, tags: List[str]) -> List[TestCase]:
    """Get test cases that have any of the specified tags."""
    from db.models import TestCaseStatus
    import json

    # Get all runnable test cases for the project (active, ready, approved)
    runnable_statuses = [TestCaseStatus.ACTIVE, TestCaseStatus.READY, TestCaseStatus.APPROVED]
    statement = (
        select(TestCase)
        .where(TestCase.project_id == project_id)
        .where(TestCase.status.in_(runnable_statuses))
    )
    all_test_cases = session.exec(statement).all()

    # Filter by tags (JSON array in tags field)
    matching = []
    for tc in all_test_cases:
        tc_tags = tc.get_tags()
        if any(tag in tc_tags for tag in tags):
            matching.append(tc)

    return matching


# --- TestData CRUD ---

def create_test_data(session: Session, test_data: TestDataCreate) -> TestData:
    """Create a new test data entry."""
    db_td = TestData.model_validate(test_data)
    session.add(db_td)
    session.commit()
    session.refresh(db_td)
    return db_td


def get_test_data(session: Session, test_data_id: int) -> Optional[TestData]:
    """Get a test data entry by ID."""
    return session.get(TestData, test_data_id)


def get_test_data_by_project(
    session: Session,
    project_id: int,
    environment_id: Optional[int] = None,
) -> List[TestData]:
    """Get test data entries for a project.

    If environment_id is given, returns env-specific items PLUS global items
    (environment_id IS NULL). Otherwise returns all entries for the project.
    """
    stmt = select(TestData).where(TestData.project_id == project_id)
    if environment_id is not None:
        stmt = stmt.where(
            (TestData.environment_id == environment_id) | (TestData.environment_id == None)  # noqa: E711
        )
    return session.exec(stmt).all()


def update_test_data(session: Session, test_data_id: int, data: TestDataUpdate) -> Optional[TestData]:
    """Update a test data entry."""
    db_td = session.get(TestData, test_data_id)
    if not db_td:
        return None

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        if hasattr(db_td, key):
            setattr(db_td, key, value)

    db_td.updated_at = datetime.utcnow()
    session.add(db_td)
    session.commit()
    session.refresh(db_td)
    return db_td


def delete_test_data(session: Session, test_data_id: int) -> bool:
    """Delete a test data entry."""
    db_td = session.get(TestData, test_data_id)
    if not db_td:
        return False

    session.delete(db_td)
    session.commit()
    return True


# --- Environment CRUD ---

import json as _json


def _clear_default_env(session: Session, project_id: int):
    """Remove is_default from all environments for a project."""
    for env in get_environments_by_project(session, project_id):
        if env.is_default:
            env.is_default = False
            session.add(env)
    session.commit()


def create_environment(session: Session, data: EnvironmentCreate) -> Environment:
    """Create a new environment. If is_default, clears existing defaults first."""
    if data.is_default:
        _clear_default_env(session, data.project_id)

    env = Environment(
        project_id=data.project_id,
        name=data.name,
        base_url=data.base_url,
        variables=_json.dumps(data.variables or {}),
        is_default=data.is_default,
    )
    session.add(env)
    session.commit()
    session.refresh(env)
    return env


def get_environment(session: Session, env_id: int) -> Optional[Environment]:
    return session.get(Environment, env_id)


def get_environments_by_project(session: Session, project_id: int) -> List[Environment]:
    return list(session.exec(select(Environment).where(Environment.project_id == project_id)).all())


def get_default_environment(session: Session, project_id: int) -> Optional[Environment]:
    return session.exec(
        select(Environment).where(
            Environment.project_id == project_id,
            Environment.is_default == True,
        )
    ).first()


def update_environment(session: Session, env_id: int, data: EnvironmentUpdate) -> Optional[Environment]:
    env = session.get(Environment, env_id)
    if not env:
        return None

    if data.is_default:
        _clear_default_env(session, env.project_id)

    update_data = data.model_dump(exclude_unset=True)
    if "variables" in update_data and isinstance(update_data["variables"], dict):
        update_data["variables"] = _json.dumps(update_data["variables"])

    for key, value in update_data.items():
        if hasattr(env, key):
            setattr(env, key, value)

    env.updated_at = datetime.utcnow()
    session.add(env)
    session.commit()
    session.refresh(env)
    return env


def delete_environment(session: Session, env_id: int) -> bool:
    env = session.get(Environment, env_id)
    if not env:
        return False
    session.delete(env)
    session.commit()
    return True
