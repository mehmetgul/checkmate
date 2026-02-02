"""Playwright Executor client for browser automation.

Communicates with the playwright-http service via HTTP/SSE.
"""

import json
import os
from typing import AsyncGenerator, Optional

import httpx

from core.logging import get_logger, request_id_var

logger = get_logger(__name__)

PLAYWRIGHT_EXECUTOR_URL = os.getenv("PLAYWRIGHT_EXECUTOR_URL", "http://localhost:8932")


class PlaywrightExecutorClient:
    """Client for executing browser tests via playwright-http service."""

    def __init__(self):
        self.base_url = PLAYWRIGHT_EXECUTOR_URL.rstrip("/")
        self.client = httpx.AsyncClient(timeout=300.0)

    def _get_headers(self) -> dict:
        """Get headers with request ID for distributed tracing."""
        return {"X-Request-ID": request_id_var.get()}

    async def health_check(self) -> bool:
        """Check if playwright-http is available.

        Returns:
            True if service is healthy, False otherwise
        """
        try:
            response = await self.client.get(
                f"{self.base_url}/health",
                headers=self._get_headers(),
            )
            if response.status_code == 200:
                data = response.json()
                return data.get("status") == "ok"
            return False
        except Exception as e:
            logger.warning(f"Playwright executor health check failed: {e}")
            return False

    async def get_browsers(self) -> dict:
        """Get available browsers from playwright-http.

        Returns:
            Dict with 'browsers' list and 'default' browser ID.
            Returns empty list if service unavailable.
        """
        try:
            response = await self.client.get(
                f"{self.base_url}/browsers",
                headers=self._get_headers(),
            )
            if response.status_code == 200:
                return response.json()
            return {"browsers": [], "default": None}
        except Exception as e:
            logger.warning(f"Failed to get browsers: {e}")
            return {"browsers": [], "default": None}

    async def execute_stream(
        self,
        base_url: str,
        steps: list[dict],
        test_id: Optional[str] = None,
        options: Optional[dict] = None,
    ) -> AsyncGenerator[dict, None]:
        """Execute test steps and stream results via SSE.

        Args:
            base_url: Base URL for the test
            steps: List of test steps to execute
            test_id: Optional test identifier
            options: Optional execution options

        Yields:
            Event dicts from the execution stream
        """
        request_body = {
            "test_id": test_id,
            "base_url": base_url,
            "steps": steps,
            "options": options or {"screenshot_on_failure": True},
        }

        try:
            async with self.client.stream(
                "POST",
                f"{self.base_url}/execute",
                json=request_body,
                headers=self._get_headers(),
                timeout=300.0,
            ) as response:
                if response.status_code != 200:
                    yield {
                        "type": "error",
                        "error": f"Executor returned {response.status_code}",
                    }
                    return

                buffer = ""
                async for chunk in response.aiter_text():
                    buffer += chunk
                    while "\n\n" in buffer:
                        message, buffer = buffer.split("\n\n", 1)
                        for line in message.split("\n"):
                            if line.startswith("data: "):
                                try:
                                    data = json.loads(line[6:])
                                    yield data
                                except json.JSONDecodeError:
                                    logger.warning(f"Failed to parse SSE data: {line}")

                # Process any remaining data in buffer after stream ends
                if buffer.strip():
                    for line in buffer.split("\n"):
                        if line.startswith("data: "):
                            try:
                                data = json.loads(line[6:])
                                yield data
                            except json.JSONDecodeError:
                                logger.warning(f"Failed to parse remaining SSE data: {line}")

        except Exception as e:
            logger.error(f"Playwright executor error: {e}")
            yield {
                "type": "error",
                "error": str(e),
            }

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()


async def test_executor_connection(client: PlaywrightExecutorClient) -> bool:
    """Test if playwright-http service is available.

    Args:
        client: PlaywrightExecutorClient instance

    Returns:
        True if connection successful, False otherwise
    """
    return await client.health_check()
