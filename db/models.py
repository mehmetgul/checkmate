"""SQLModel database models."""

from datetime import datetime
from enum import Enum
from typing import Optional, List
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import Column, Integer, ForeignKey
import json


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


class TestCaseCreate(TestCaseBase):
    project_id: int


class TestCaseRead(TestCaseBase):
    id: int
    project_id: int
    created_at: datetime
    updated_at: datetime
    created_by: Optional[str]


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
