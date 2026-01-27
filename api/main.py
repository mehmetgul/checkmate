"""FastAPI application for the QA Testing Agent API."""

from contextlib import asynccontextmanager
from dotenv import load_dotenv

# Load .env file before any other imports that read env vars
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from db.session import create_db_and_tables
from api.routes import projects, test_cases, test_runs, agent, settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - create tables on startup."""
    create_db_and_tables()
    yield


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


@app.get("/")
def read_root():
    """Root endpoint."""
    return {"name": "QA Testing Agent", "version": "0.1.0"}


@app.get("/health")
def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}
