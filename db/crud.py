"""CRUD operations for database models."""

from datetime import datetime
from typing import List, Optional
from sqlmodel import Session, select

from db.models import (
    Project, ProjectCreate,
    TestCase, TestCaseCreate,
    TestRun, TestRunCreate,
    TestRunStep, TestRunStepCreate,
    Persona, PersonaCreate, PersonaUpdate,
    Page, PageCreate, PageUpdate,
)
from db.encryption import encrypt_password


# --- Project CRUD ---

def create_project(session: Session, project: ProjectCreate) -> Project:
    """Create a new project."""
    db_project = Project.model_validate(project)
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

    # Delete the project
    session.delete(db_project)
    session.commit()
    return True


# --- TestCase CRUD ---

def create_test_case(session: Session, test_case: TestCaseCreate) -> TestCase:
    """Create a new test case."""
    db_test_case = TestCase.model_validate(test_case)
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

    for key, value in data.items():
        if hasattr(db_test_case, key):
            setattr(db_test_case, key, value)

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
    """Create a new persona with encrypted password."""
    db_persona = Persona(
        name=persona.name,
        username=persona.username,
        description=persona.description,
        project_id=persona.project_id,
        encrypted_password=encrypt_password(persona.password),
    )
    session.add(db_persona)
    session.commit()
    session.refresh(db_persona)
    return db_persona


def get_persona(session: Session, persona_id: int) -> Optional[Persona]:
    """Get a persona by ID."""
    return session.get(Persona, persona_id)


def get_personas_by_project(session: Session, project_id: int) -> List[Persona]:
    """Get all personas for a project."""
    statement = select(Persona).where(Persona.project_id == project_id)
    return session.exec(statement).all()


def update_persona(session: Session, persona_id: int, data: PersonaUpdate) -> Optional[Persona]:
    """Update a persona. Password is re-encrypted if provided."""
    db_persona = session.get(Persona, persona_id)
    if not db_persona:
        return None

    update_data = data.model_dump(exclude_unset=True)

    # Handle password separately - encrypt if provided
    if "password" in update_data and update_data["password"]:
        db_persona.encrypted_password = encrypt_password(update_data.pop("password"))
    elif "password" in update_data:
        update_data.pop("password")  # Remove None/empty password

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
