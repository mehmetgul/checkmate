"""FastAPI application for the QA Testing Agent API."""

import time
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# Load .env file before any other imports that read env vars
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from core.logging import setup_logging, get_logger, request_id_var
from db.session import create_db_and_tables
from api.routes import projects, test_cases, test_runs, agent, settings, config, notifications, schedules, fixtures, folders, environments, vault, recorder, healer

# Initialize logging on module load
setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - create tables on startup."""
    from scheduler import scheduler_service

    logger.info("Starting QA Testing Agent API")
    create_db_and_tables()

    # Start the scheduler service
    await scheduler_service.start()

    yield

    # Stop the scheduler service
    await scheduler_service.stop()
    logger.info("Shutting down QA Testing Agent API")


app = FastAPI(
    title="QA Testing Agent API",
    description="API for managing QA test projects, test cases, and test runs",
    version="0.1.0",
    lifespan=lifespan,
    redirect_slashes=False,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(projects.router, prefix="/api")
app.include_router(test_cases.router, prefix="/api")
app.include_router(test_runs.router, prefix="/api")
app.include_router(agent.router, prefix="/api")
app.include_router(settings.router, prefix="/api")
app.include_router(config.router, prefix="/api")
app.include_router(notifications.router, prefix="/api")
app.include_router(schedules.router, prefix="/api")
app.include_router(schedules.project_runs_router, prefix="/api")
app.include_router(schedules.debug_router, prefix="/api")
app.include_router(fixtures.router, prefix="/api")
app.include_router(fixtures.fixture_router, prefix="/api")
app.include_router(folders.router, prefix="/api")
app.include_router(environments.router, prefix="/api")
app.include_router(vault.router, prefix="/api")
app.include_router(recorder.router, prefix="/api")
app.include_router(healer.router, prefix="/api")


@app.get("/")
def read_root():
    """Root endpoint."""
    return {"name": "QA Testing Agent", "version": "0.1.0"}


@app.get("/health")
def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log all HTTP requests with request ID for correlation."""
    # Generate and set request ID for entire request flow
    request_id = str(uuid.uuid4())
    request_id_var.set(request_id)

    start = time.perf_counter()
    logger.info(f"{request.method} {request.url.path}")

    response = await call_next(request)

    duration_ms = (time.perf_counter() - start) * 1000
    logger.info(f"{response.status_code} ({duration_ms:.1f}ms)")

    # Return request ID in response header for client debugging
    response.headers["X-Request-ID"] = request_id

    return response
