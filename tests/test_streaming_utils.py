"""Tests for streaming utilities."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from api.utils.streaming import (
    sse_event,
    sse_error,
    sse_warning,
    streaming_context,
)


class TestSSEHelpers:
    """Tests for SSE event formatting helpers."""

    def test_sse_event_basic(self):
        """Test basic SSE event formatting."""
        result = sse_event("test_event")
        assert result == 'data: {"type": "test_event"}\n\n'

    def test_sse_event_with_data(self):
        """Test SSE event with additional data."""
        result = sse_event("step_completed", step_number=1, status="passed")
        parsed = json.loads(result.replace("data: ", "").strip())
        assert parsed["type"] == "step_completed"
        assert parsed["step_number"] == 1
        assert parsed["status"] == "passed"

    def test_sse_event_with_complex_data(self):
        """Test SSE event with complex nested data."""
        result = sse_event(
            "run_completed",
            run_id=42,
            pass_count=5,
            error_count=1,
            summary="Executed 6 steps: 5 passed, 1 failed",
        )
        parsed = json.loads(result.replace("data: ", "").strip())
        assert parsed["type"] == "run_completed"
        assert parsed["run_id"] == 42
        assert parsed["pass_count"] == 5
        assert parsed["error_count"] == 1
        assert "5 passed" in parsed["summary"]

    def test_sse_error(self):
        """Test SSE error event formatting."""
        result = sse_error("Something went wrong")
        parsed = json.loads(result.replace("data: ", "").strip())
        assert parsed["type"] == "error"
        assert parsed["message"] == "Something went wrong"

    def test_sse_warning(self):
        """Test SSE warning event formatting."""
        result = sse_warning("Executor unavailable")
        parsed = json.loads(result.replace("data: ", "").strip())
        assert parsed["type"] == "warning"
        assert parsed["message"] == "Executor unavailable"

    def test_sse_event_ends_with_double_newline(self):
        """Verify SSE events end with double newline per spec."""
        assert sse_event("test").endswith("\n\n")
        assert sse_error("err").endswith("\n\n")
        assert sse_warning("warn").endswith("\n\n")


class TestStreamingContext:
    """Tests for streaming_context context manager."""

    @pytest.mark.asyncio
    async def test_streaming_context_yields_resources(self):
        """Test that context manager yields session, client, and simulation flag."""
        with patch("api.utils.streaming.Session") as mock_session_class, \
             patch("api.utils.streaming.PlaywrightExecutorClient") as mock_client_class, \
             patch("api.utils.streaming.test_executor_connection") as mock_test_conn:

            mock_session = MagicMock()
            mock_session_class.return_value = mock_session

            mock_client = MagicMock()
            mock_client.close = AsyncMock()
            mock_client_class.return_value = mock_client

            mock_test_conn.return_value = True

            async with streaming_context() as (session, executor_client, use_simulation):
                assert session is mock_session
                assert executor_client is mock_client
                assert use_simulation is False

            # Verify cleanup
            mock_session.commit.assert_called_once()
            mock_client.close.assert_awaited_once()
            mock_session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_streaming_context_simulation_mode_when_executor_unavailable(self):
        """Test that simulation mode is enabled when executor health check fails."""
        with patch("api.utils.streaming.Session") as mock_session_class, \
             patch("api.utils.streaming.PlaywrightExecutorClient") as mock_client_class, \
             patch("api.utils.streaming.test_executor_connection") as mock_test_conn:

            mock_session = MagicMock()
            mock_session_class.return_value = mock_session

            mock_client = MagicMock()
            mock_client.close = AsyncMock()
            mock_client_class.return_value = mock_client

            mock_test_conn.return_value = False  # Executor unavailable

            async with streaming_context() as (session, executor_client, use_simulation):
                assert use_simulation is True

    @pytest.mark.asyncio
    async def test_streaming_context_rollback_on_exception(self):
        """Test that session is rolled back when exception occurs."""
        with patch("api.utils.streaming.Session") as mock_session_class, \
             patch("api.utils.streaming.PlaywrightExecutorClient") as mock_client_class, \
             patch("api.utils.streaming.test_executor_connection") as mock_test_conn:

            mock_session = MagicMock()
            mock_session_class.return_value = mock_session

            mock_client = MagicMock()
            mock_client.close = AsyncMock()
            mock_client_class.return_value = mock_client

            mock_test_conn.return_value = True

            with pytest.raises(ValueError):
                async with streaming_context() as (session, executor_client, use_simulation):
                    raise ValueError("Test error")

            # Verify rollback and cleanup happened
            mock_session.rollback.assert_called_once()
            mock_client.close.assert_awaited_once()
            mock_session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_streaming_context_cleanup_on_success(self):
        """Test that resources are cleaned up on successful completion."""
        with patch("api.utils.streaming.Session") as mock_session_class, \
             patch("api.utils.streaming.PlaywrightExecutorClient") as mock_client_class, \
             patch("api.utils.streaming.test_executor_connection") as mock_test_conn:

            mock_session = MagicMock()
            mock_session_class.return_value = mock_session

            mock_client = MagicMock()
            mock_client.close = AsyncMock()
            mock_client_class.return_value = mock_client

            mock_test_conn.return_value = True

            async with streaming_context() as (session, executor_client, use_simulation):
                pass  # Do nothing, just exit cleanly

            # Verify commit (not rollback) and cleanup
            mock_session.commit.assert_called_once()
            mock_session.rollback.assert_not_called()
            mock_client.close.assert_awaited_once()
            mock_session.close.assert_called_once()
