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
)
from db.encryption import encrypt_password, encrypt_data, decrypt_data


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
    cookies: Optional[str] = None,
    local_storage: Optional[str] = None,
    session_storage: Optional[str] = None,
    browser: Optional[str] = None,
    expires_at: Optional[datetime] = None
) -> FixtureState:
    """Create a new fixture state with encrypted data."""
    db_state = FixtureState(
        fixture_id=fixture_id,
        project_id=project_id,
        encrypted_cookies=encrypt_data(cookies) if cookies else None,
        encrypted_local_storage=encrypt_data(local_storage) if local_storage else None,
        encrypted_session_storage=encrypt_data(session_storage) if session_storage else None,
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
        dict with decrypted cookies, local_storage, session_storage
    """
    import json

    return {
        "cookies": json.loads(decrypt_data(state.encrypted_cookies)) if state.encrypted_cookies else None,
        "local_storage": json.loads(decrypt_data(state.encrypted_local_storage)) if state.encrypted_local_storage else None,
        "session_storage": json.loads(decrypt_data(state.encrypted_session_storage)) if state.encrypted_session_storage else None,
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

    # Get all active test cases for the project
    statement = (
        select(TestCase)
        .where(TestCase.project_id == project_id)
        .where(TestCase.status == TestCaseStatus.ACTIVE)
    )
    all_test_cases = session.exec(statement).all()

    # Filter by tags (JSON array in tags field)
    matching = []
    for tc in all_test_cases:
        tc_tags = tc.get_tags()
        if any(tag in tc_tags for tag in tags):
            matching.append(tc)

    return matching
