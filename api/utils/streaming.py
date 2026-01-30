"""Streaming utilities for SSE test execution."""

import json
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Tuple

from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session
from httpx import HTTPError

from db.session import engine
from agent.executor_client import PlaywrightExecutorClient, test_executor_connection
from core.logging import get_logger

logger = get_logger(__name__)


# =============================================================================
# SSE Event Helpers
# =============================================================================


def sse_event(event_type: str, **data) -> str:
    """Format an SSE event.

    Args:
        event_type: Event type (e.g., 'error', 'warning', 'step_completed')
        **data: Additional event data

    Returns:
        Formatted SSE event string
    """
    return f"data: {json.dumps({'type': event_type, **data})}\n\n"


def sse_error(message: str) -> str:
    """Format an SSE error event.

    Args:
        message: Error message

    Returns:
        Formatted SSE error event string
    """
    return sse_event("error", message=message)


def sse_warning(message: str) -> str:
    """Format an SSE warning event.

    Args:
        message: Warning message

    Returns:
        Formatted SSE warning event string
    """
    return sse_event("warning", message=message)


# =============================================================================
# Streaming Context Manager
# =============================================================================


@asynccontextmanager
async def streaming_context() -> AsyncGenerator[
    Tuple[Session, PlaywrightExecutorClient, bool], None
]:
    """Async context manager for streaming test execution.

    Manages database session and executor client lifecycle for streaming
    test execution. Handles cleanup of both resources on exit.

    Yields:
        Tuple of:
            session: Database session
            executor_client: Playwright executor client
            use_simulation: True if executor unavailable

    Example:
        async with streaming_context() as (session, executor_client, use_simulation):
            if use_simulation:
                yield sse_warning('Using simulation mode')
            # ... streaming logic ...
    """
    session = Session(engine)
    executor_client = PlaywrightExecutorClient()
    use_simulation = False

    try:
        # Test executor connection
        if not await test_executor_connection(executor_client):
            use_simulation = True
        yield session, executor_client, use_simulation
        session.commit()
    except SQLAlchemyError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        await executor_client.close()
        session.close()
