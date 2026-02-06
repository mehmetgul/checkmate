"""SQLModel database models."""

from datetime import datetime
from enum import Enum
from typing import Optional, List
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import Column, Integer, ForeignKey
from pydantic import field_serializer
import json


def serialize_datetime_utc(dt: Optional[datetime]) -> Optional[str]:
    """Serialize datetime to ISO format with Z suffix (UTC indicator)."""
    if dt is None:
        return None
    return dt.isoformat() + "Z"


class Priority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TestCaseStatus(str, Enum):
    ACTIVE = "active"
    DRAFT = "draft"
    ARCHIVED = "archived"


class RunTrigger(str, Enum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    NATURAL_LANGUAGE = "natural_language"
    CI_CD = "ci_cd"


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


# --- Project ---

class PageLoadState(str, Enum):
    """Page load states for wait_for_page action."""
    LOAD = "load"
    DOMCONTENTLOADED = "domcontentloaded"
    NETWORKIDLE = "networkidle"


class ProjectBase(SQLModel):
    name: str = Field(index=True)
    description: Optional[str] = None
    base_url: str
    config: Optional[str] = None  # JSON string for extra config
    base_prompt: Optional[str] = None  # Custom context about app setup, auth flow, etc.
    page_load_state: Optional[str] = Field(default="load")  # Default page load event: load, domcontentloaded, networkidle


class Project(ProjectBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    test_cases: List["TestCase"] = Relationship(back_populates="project")
    test_runs: List["TestRun"] = Relationship(back_populates="project")
    personas: List["Persona"] = Relationship(back_populates="project")
    pages: List["Page"] = Relationship(back_populates="project")
    fixtures: List["Fixture"] = Relationship(back_populates="project")

    def get_config(self) -> dict:
        """Parse config JSON."""
        return json.loads(self.config) if self.config else {}

    def set_config(self, config: dict):
        """Set config as JSON."""
        self.config = json.dumps(config)


class ProjectCreate(ProjectBase):
    pass


class ProjectRead(ProjectBase):
    id: int
    created_at: datetime
    updated_at: datetime


# --- TestCase ---

class TestCaseBase(SQLModel):
    name: str = Field(index=True)
    description: Optional[str] = None
    natural_query: str
    steps: str  # JSON array of steps
    expected_result: Optional[str] = None
    tags: Optional[str] = None  # JSON array
    fixture_ids: Optional[str] = None  # JSON array of fixture IDs
    priority: Priority = Priority.MEDIUM
    status: TestCaseStatus = TestCaseStatus.ACTIVE


class TestCase(TestCaseBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: Optional[str] = None

    project: Project = Relationship(back_populates="test_cases")
    test_runs: List["TestRun"] = Relationship(back_populates="test_case", sa_relationship_kwargs={"cascade": "all, delete-orphan"})

    def get_steps(self) -> list:
        """Parse steps JSON."""
        return json.loads(self.steps) if self.steps else []

    def set_steps(self, steps: list):
        """Set steps as JSON."""
        self.steps = json.dumps(steps)

    def get_tags(self) -> list:
        """Parse tags JSON."""
        return json.loads(self.tags) if self.tags else []

    def set_tags(self, tags: list):
        """Set tags as JSON."""
        self.tags = json.dumps(tags)

    def get_fixture_ids(self) -> list:
        """Parse fixture_ids JSON."""
        return json.loads(self.fixture_ids) if self.fixture_ids else []

    def set_fixture_ids(self, ids: list):
        """Set fixture_ids as JSON."""
        self.fixture_ids = json.dumps(ids)


class TestCaseCreate(TestCaseBase):
    project_id: int


class TestCaseRead(TestCaseBase):
    id: int
    project_id: int
    created_at: datetime
    updated_at: datetime
    created_by: Optional[str]
    fixture_ids: Optional[str] = None


# --- TestRun ---

class TestRunBase(SQLModel):
    trigger: RunTrigger = RunTrigger.MANUAL
    status: RunStatus = RunStatus.PENDING
    thread_id: Optional[str] = None


class TestRun(TestRunBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    test_case_id: Optional[int] = Field(default=None, sa_column=Column(Integer, ForeignKey("testcase.id", ondelete="CASCADE"), index=True))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    summary: Optional[str] = None
    error_count: int = 0
    pass_count: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # Retry tracking
    retry_attempt: int = 0  # Current retry attempt (0 = original run)
    max_retries: int = 0  # Maximum retries configured for this run
    original_run_id: Optional[int] = Field(default=None, foreign_key="testrun.id", index=True)
    retry_mode: Optional[str] = None  # "intelligent" or "simple"
    retry_reason: Optional[str] = None  # Reason for retry (from classifier)

    project: Project = Relationship(back_populates="test_runs")
    test_case: Optional["TestCase"] = Relationship(back_populates="test_runs")
    steps: List["TestRunStep"] = Relationship(back_populates="test_run", sa_relationship_kwargs={"cascade": "all, delete-orphan"})


class TestRunCreate(TestRunBase):
    project_id: int
    test_case_id: Optional[int] = None


class TestRunRead(TestRunBase):
    id: int
    project_id: int
    test_case_id: Optional[int]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    summary: Optional[str]
    error_count: int
    pass_count: int
    created_at: datetime
    # Retry tracking
    retry_attempt: int = 0
    max_retries: int = 0
    original_run_id: Optional[int] = None
    retry_mode: Optional[str] = None
    retry_reason: Optional[str] = None


# --- TestRunStep ---

class TestRunStepBase(SQLModel):
    step_number: int
    action: str
    target: Optional[str] = None
    value: Optional[str] = None
    status: StepStatus = StepStatus.PENDING
    result: Optional[str] = None
    screenshot: Optional[str] = None  # Base64 or file path
    duration: Optional[int] = None  # Milliseconds
    error: Optional[str] = None
    logs: Optional[str] = None  # JSON
    fixture_name: Optional[str] = None  # Name of fixture if this is a fixture step


class TestRunStep(TestRunStepBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    test_run_id: int = Field(sa_column=Column(Integer, ForeignKey("testrun.id", ondelete="CASCADE"), index=True, nullable=False))
    test_case_id: Optional[int] = Field(default=None, sa_column=Column(Integer, ForeignKey("testcase.id", ondelete="CASCADE")))
    created_at: datetime = Field(default_factory=datetime.utcnow)

    test_run: TestRun = Relationship(back_populates="steps")


class TestRunStepCreate(TestRunStepBase):
    test_run_id: int
    test_case_id: Optional[int] = None


class TestRunStepRead(TestRunStepBase):
    id: int
    test_run_id: int
    test_case_id: Optional[int]
    created_at: datetime


# --- Persona ---

class PersonaBase(SQLModel):
    name: str = Field(index=True)      # e.g., "admin", "readonly_user"
    username: str
    description: Optional[str] = None


class Persona(PersonaBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    encrypted_password: str            # Fernet-encrypted
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    project: "Project" = Relationship(back_populates="personas")


class PersonaCreate(PersonaBase):
    project_id: int
    password: str                      # Plain text, encrypted before storage


class PersonaRead(PersonaBase):
    id: int
    project_id: int
    created_at: datetime
    updated_at: datetime
    # Note: password never returned in API


class PersonaUpdate(SQLModel):
    name: Optional[str] = None
    username: Optional[str] = None
    description: Optional[str] = None
    password: Optional[str] = None     # Only update if provided


# --- Page ---

class PageBase(SQLModel):
    name: str = Field(index=True)      # e.g., "login", "dashboard"
    path: str                          # e.g., "/login", "/dashboard"
    description: Optional[str] = None


class Page(PageBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    project: "Project" = Relationship(back_populates="pages")


class PageCreate(PageBase):
    project_id: int


class PageRead(PageBase):
    id: int
    project_id: int
    created_at: datetime
    updated_at: datetime


class PageUpdate(SQLModel):
    name: Optional[str] = None
    path: Optional[str] = None
    description: Optional[str] = None


# --- Fixture ---

class FixtureScope(str, Enum):
    TEST = "test"       # Fresh setup per test (no state reuse)
    CACHED = "cached"   # Reuse state until TTL expires


class FixtureBase(SQLModel):
    name: str = Field(index=True)
    description: Optional[str] = None
    setup_steps: str  # JSON array of steps
    scope: str = Field(default="cached")  # test or cached
    cache_ttl_seconds: int = Field(default=3600)  # Default 1 hour for cached scope


class Fixture(FixtureBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    project: "Project" = Relationship(back_populates="fixtures")
    states: List["FixtureState"] = Relationship(back_populates="fixture", sa_relationship_kwargs={"cascade": "all, delete-orphan"})

    def get_setup_steps(self) -> list:
        """Parse setup_steps JSON."""
        return json.loads(self.setup_steps) if self.setup_steps else []

    def set_setup_steps(self, steps: list):
        """Set setup_steps as JSON."""
        self.setup_steps = json.dumps(steps)


class FixtureCreate(FixtureBase):
    project_id: int


class FixtureRead(FixtureBase):
    id: int
    project_id: int
    created_at: datetime
    updated_at: datetime

    @field_serializer('created_at', 'updated_at')
    def serialize_dt(self, dt: Optional[datetime], _info) -> Optional[str]:
        return serialize_datetime_utc(dt)


class FixtureUpdate(SQLModel):
    name: Optional[str] = None
    description: Optional[str] = None
    setup_steps: Optional[str] = None
    scope: Optional[str] = None
    cache_ttl_seconds: Optional[int] = None


# --- FixtureState (cached browser state) ---

class FixtureStateBase(SQLModel):
    url: Optional[str] = None  # URL where state was captured
    encrypted_state_json: Optional[str] = None  # Encrypted Playwright storage_state (cookies + origins)
    browser: Optional[str] = None


class FixtureState(FixtureStateBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    fixture_id: int = Field(foreign_key="fixture.id", index=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    captured_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None

    fixture: "Fixture" = Relationship(back_populates="states")


class FixtureStateCreate(FixtureStateBase):
    fixture_id: int
    project_id: int
    expires_at: Optional[datetime] = None


class FixtureStateRead(FixtureStateBase):
    id: int
    fixture_id: int
    project_id: int
    captured_at: datetime
    expires_at: Optional[datetime]

    @field_serializer('captured_at', 'expires_at')
    def serialize_dt(self, dt: Optional[datetime], _info) -> Optional[str]:
        return serialize_datetime_utc(dt)


# --- NotificationChannel ---

class NotificationChannelType(str, Enum):
    WEBHOOK = "webhook"
    EMAIL = "email"
    SLACK = "slack"


class NotifyOn(str, Enum):
    ALWAYS = "always"
    FAILURE = "failure"
    SUCCESS = "success"


class NotificationChannelBase(SQLModel):
    name: str = Field(index=True)
    channel_type: str = Field(default="webhook")
    enabled: bool = True
    webhook_url: Optional[str] = None
    webhook_template: Optional[str] = None
    email_recipients: Optional[str] = None  # JSON array of emails
    email_template: Optional[str] = None
    notify_on: str = Field(default="failure")


class NotificationChannel(NotificationChannelBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    def get_email_recipients(self) -> list:
        """Parse email recipients JSON."""
        return json.loads(self.email_recipients) if self.email_recipients else []

    def set_email_recipients(self, recipients: list):
        """Set email recipients as JSON."""
        self.email_recipients = json.dumps(recipients)


class NotificationChannelCreate(NotificationChannelBase):
    project_id: int


class NotificationChannelRead(NotificationChannelBase):
    id: int
    project_id: int
    created_at: datetime
    updated_at: datetime


class NotificationChannelUpdate(SQLModel):
    name: Optional[str] = None
    channel_type: Optional[str] = None
    enabled: Optional[bool] = None
    webhook_url: Optional[str] = None
    webhook_template: Optional[str] = None
    email_recipients: Optional[str] = None
    email_template: Optional[str] = None
    notify_on: Optional[str] = None


# --- Schedule ---

class ScheduleTargetType(str, Enum):
    TEST_CASE_IDS = "test_case_ids"
    TAGS = "tags"


class ScheduleBase(SQLModel):
    name: str = Field(index=True)
    description: Optional[str] = None
    cron_expression: str
    timezone: str = Field(default="UTC")
    target_type: str = Field(default="test_case_ids")
    target_test_case_ids: Optional[str] = None  # JSON array
    target_tags: Optional[str] = None  # JSON array
    browser: Optional[str] = None
    retry_max: int = 0
    retry_mode: Optional[str] = None
    enabled: bool = True
    notification_channel_ids: Optional[str] = None  # JSON array of channel IDs


class Schedule(ScheduleBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    scheduled_runs: List["ScheduledRun"] = Relationship(back_populates="schedule", sa_relationship_kwargs={"cascade": "all, delete-orphan"})

    def get_target_test_case_ids(self) -> list:
        """Parse target test case IDs JSON."""
        return json.loads(self.target_test_case_ids) if self.target_test_case_ids else []

    def set_target_test_case_ids(self, ids: list):
        """Set target test case IDs as JSON."""
        self.target_test_case_ids = json.dumps(ids)

    def get_target_tags(self) -> list:
        """Parse target tags JSON."""
        return json.loads(self.target_tags) if self.target_tags else []

    def set_target_tags(self, tags: list):
        """Set target tags as JSON."""
        self.target_tags = json.dumps(tags)

    def get_notification_channel_ids(self) -> list:
        """Parse notification channel IDs JSON."""
        return json.loads(self.notification_channel_ids) if self.notification_channel_ids else []

    def set_notification_channel_ids(self, ids: list):
        """Set notification channel IDs as JSON."""
        self.notification_channel_ids = json.dumps(ids)


class ScheduleCreate(ScheduleBase):
    project_id: int


class ScheduleRead(ScheduleBase):
    id: int
    project_id: int
    last_run_at: Optional[datetime]
    next_run_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    @field_serializer('last_run_at', 'next_run_at', 'created_at', 'updated_at')
    def serialize_dt(self, dt: Optional[datetime], _info) -> Optional[str]:
        return serialize_datetime_utc(dt)


class ScheduleUpdate(SQLModel):
    name: Optional[str] = None
    description: Optional[str] = None
    cron_expression: Optional[str] = None
    timezone: Optional[str] = None
    target_type: Optional[str] = None
    target_test_case_ids: Optional[str] = None
    target_tags: Optional[str] = None
    browser: Optional[str] = None
    retry_max: Optional[int] = None
    retry_mode: Optional[str] = None
    enabled: Optional[bool] = None
    notification_channel_ids: Optional[str] = None


# --- ScheduledRun ---

class ScheduledRunBase(SQLModel):
    thread_id: str
    status: RunStatus = RunStatus.PENDING
    test_count: int = 0
    pass_count: int = 0
    fail_count: int = 0
    notifications_sent: Optional[str] = None  # JSON array of channel IDs
    notification_errors: Optional[str] = None  # JSON object of channel_id -> error


class ScheduledRun(ScheduledRunBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    schedule_id: int = Field(foreign_key="schedule.id", index=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    schedule: Schedule = Relationship(back_populates="scheduled_runs")

    def get_notifications_sent(self) -> list:
        """Parse notifications sent JSON."""
        return json.loads(self.notifications_sent) if self.notifications_sent else []

    def set_notifications_sent(self, ids: list):
        """Set notifications sent as JSON."""
        self.notifications_sent = json.dumps(ids)

    def get_notification_errors(self) -> dict:
        """Parse notification errors JSON."""
        return json.loads(self.notification_errors) if self.notification_errors else {}

    def set_notification_errors(self, errors: dict):
        """Set notification errors as JSON."""
        self.notification_errors = json.dumps(errors)


class ScheduledRunCreate(ScheduledRunBase):
    schedule_id: int
    project_id: int


class ScheduledRunRead(ScheduledRunBase):
    id: int
    schedule_id: int
    project_id: int
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    created_at: datetime

    @field_serializer('started_at', 'completed_at', 'created_at')
    def serialize_dt(self, dt: Optional[datetime], _info) -> Optional[str]:
        return serialize_datetime_utc(dt)
