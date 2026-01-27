"""Playwright MCP client for browser automation."""

import asyncio
import logging
import os
import json
import re
import time
import httpx
from typing import Optional, Any, Tuple

# Set up logging for MCP client debugging
logger = logging.getLogger("mcp_client")

PLAYWRIGHT_MCP_URL = os.getenv("PLAYWRIGHT_MCP_URL", "http://localhost:8931")


class PlaywrightMCPClient:
    """Client for executing browser actions via Playwright MCP."""

    def __init__(self):
        # Strip /sse suffix if present (URL should be base endpoint)
        self.base_url = PLAYWRIGHT_MCP_URL.rstrip("/sse").rstrip("/")
        self.session_id: Optional[str] = None
        self.initialized: bool = False
        self.request_id: int = 0
        self.client = httpx.AsyncClient(timeout=60.0)

    def _next_id(self) -> int:
        """Get next request ID."""
        self.request_id += 1
        return self.request_id

    def _get_headers(self) -> dict:
        """Get headers for MCP requests per Streamable HTTP transport spec."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",  # Support both response types
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _parse_sse_response(self, text: str) -> dict:
        """Parse SSE response to extract JSON data."""
        if not text or not text.strip():
            return {}

        # SSE format: "event: message\ndata: {json}\n\n"
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("data: "):
                try:
                    return json.loads(line[6:])
                except json.JSONDecodeError:
                    continue

        # Fallback: try parsing as plain JSON
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}

    async def _send_request(self, method: str, params: dict = None) -> dict:
        """Send a JSON-RPC request to MCP server using Streamable HTTP transport."""
        request_id = self._next_id()
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "id": request_id,
        }
        if params:
            payload["params"] = params

        url = f"{self.base_url}/mcp"

        # Log request details for debugging
        tool_name = params.get("name", "") if params else ""
        logger.debug(f"[MCP] Request #{request_id}: method={method}, tool={tool_name}, session={self.session_id}")

        try:
            response = await self.client.post(
                url,
                json=payload,
                headers=self._get_headers()
            )
        except Exception as e:
            logger.error(f"[MCP] Request #{request_id} failed with network error: {e}")
            raise

        # Log response status
        logger.debug(f"[MCP] Response #{request_id}: status={response.status_code}, session_header={response.headers.get('Mcp-Session-Id', 'none')}")

        # Check for HTTP errors (e.g., 404 Session not found)
        if response.status_code >= 400:
            error_text = response.text.strip() or f"HTTP {response.status_code}"
            logger.error(f"[MCP] Request #{request_id} failed with HTTP {response.status_code}: {error_text}")
            raise Exception(f"MCP error: {error_text}")

        # Capture session ID from response header (per MCP spec)
        if "Mcp-Session-Id" in response.headers:
            new_session_id = response.headers["Mcp-Session-Id"]
            if self.session_id and self.session_id != new_session_id:
                logger.warning(f"[MCP] Session ID changed from {self.session_id} to {new_session_id}")
            self.session_id = new_session_id

        # Parse SSE or JSON response
        result = self._parse_sse_response(response.text)
        if "error" in result:
            logger.error(f"[MCP] Request #{request_id} returned JSON-RPC error: {result['error']}")
            raise Exception(result["error"].get("message", "MCP error"))
        return result.get("result", {})

    async def _send_notification(self, method: str, params: dict = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        payload = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params:
            payload["params"] = params

        await self.client.post(
            f"{self.base_url}/mcp",
            json=payload,
            headers=self._get_headers()
        )

    async def initialize(self) -> None:
        """Initialize the MCP session. Must be called before any tool calls."""
        if self.initialized:
            return

        # Step 1: Send initialize request
        await self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "checkmate",
                "version": "1.0.0"
            }
        })

        # Step 2: Send initialized notification
        await self._send_notification("notifications/initialized")

        self.initialized = True

    async def reinitialize(self) -> None:
        """Reinitialize the MCP session after a session loss."""
        self.initialized = False
        self.session_id = None
        self.request_id = 0
        await self.initialize()

    async def call_tool(self, tool_name: str, arguments: dict, retry_on_session_loss: bool = True) -> dict:
        """
        Call a Playwright MCP tool.

        Args:
            tool_name: Name of the MCP tool to call
            arguments: Arguments for the tool
            retry_on_session_loss: If True, reinitialize session and retry on "Session not found"
        """
        # Ensure we're initialized
        if not self.initialized:
            await self.initialize()

        try:
            return await self._send_request("tools/call", {
                "name": tool_name,
                "arguments": arguments
            })
        except Exception as e:
            error_msg = str(e)
            # Handle session loss by reinitializing and retrying once
            if retry_on_session_loss and ("Session not found" in error_msg or "404" in error_msg):
                try:
                    await self.reinitialize()
                    return await self._send_request("tools/call", {
                        "name": tool_name,
                        "arguments": arguments
                    })
                except Exception:
                    pass  # Re-raise original error
            raise

    async def get_snapshot(self) -> str:
        """Get current page snapshot as text."""
        result = await self.call_tool("browser_snapshot", {})
        content = result.get("content", [])
        if content and isinstance(content, list):
            for item in content:
                if item.get("type") == "text":
                    return item.get("text", "")
        return ""

    def _get_target_variations(self, target: str) -> list:
        """
        Generate variations of the target by removing common element type suffixes.

        Users often describe elements with their type, e.g.:
        - "submit button" → try "submit"
        - "credentials link" → try "credentials"
        - "Password input field" → try "Password input", "Password"

        Recursively strips suffixes to handle multi-word patterns.
        """
        variations = [target]
        current = target

        # Recursively strip suffixes
        while True:
            current_lower = current.lower()
            found_suffix = False

            for suffix in ELEMENT_TYPE_SUFFIXES:
                if current_lower.endswith(suffix):
                    stripped = current[:len(current) - len(suffix)].strip()
                    if stripped and stripped not in variations:
                        variations.append(stripped)
                        current = stripped
                        found_suffix = True
                        break

            if not found_suffix:
                break

        return variations

    def find_element_ref(self, snapshot: str, target: str) -> Optional[Tuple[str, str]]:
        """
        Find an element ref in the snapshot that matches the target.

        Handles common patterns where users describe elements with type suffixes,
        e.g., "credentials link" will match element text "or login with credentials".

        Returns:
            Tuple of (element_description, ref) if found, None otherwise
        """
        # Pattern to match elements with refs like: button "Gmail" [ref=e10]
        # or: link "Sign in" [ref=e20]
        pattern = r'(\w+)\s+"([^"]+)"\s+\[ref=(\w+)\]'

        # Text content pattern: text: Gmail
        text_pattern = r'text:\s*([^\n]+)'

        # Try each variation of the target
        for target_variation in self._get_target_variations(target):
            target_lower = target_variation.lower()

            # First, try matching against element text
            for match in re.finditer(pattern, snapshot):
                element_type, element_text, ref = match.groups()
                if target_lower in element_text.lower():
                    return (f'{element_type} "{element_text}"', ref)

            # Also check for text content
            for match in re.finditer(text_pattern, snapshot):
                text = match.group(1).strip()
                if target_lower in text.lower():
                    # Find the nearest ref above this text
                    pos = match.start()
                    ref_matches = list(re.finditer(r'\[ref=(\w+)\]', snapshot[:pos]))
                    if ref_matches:
                        ref = ref_matches[-1].group(1)
                        return (f'text "{text}"', ref)

        return None

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()


# Action to MCP tool mapping
# Most actions use browser_run_code for natural selectors (no ref required)
# Some actions use native MCP tools directly

def _build_click_code(step: dict) -> dict:
    """Build Playwright code for click action."""
    target = step.get("target", "")
    # Wrap in try-catch to prevent session destruction on failure
    code = f"""async (page) => {{
        try {{
            await page.getByRole('button', {{ name: /{_escape_regex(target)}/i }}).or(
                page.getByRole('link', {{ name: /{_escape_regex(target)}/i }})
            ).or(
                page.getByText(/{_escape_regex(target)}/i)
            ).or(
                page.locator('[data-testid*="{target}"]')
            ).first().click({{ timeout: 4000 }});
            return {{ success: true }};
        }} catch (e) {{
            return {{ success: false, error: e.message }};
        }}
    }}"""
    return {"code": code}


def _build_type_code(step: dict) -> dict:
    """Build Playwright code for type action."""
    target = step.get("target", "")
    text = step.get("value", "")
    # Wrap in try-catch to prevent session destruction on failure
    code = f"""async (page) => {{
        try {{
            const element = page.getByRole('textbox', {{ name: /{_escape_regex(target)}/i }}).or(
                page.getByPlaceholder(/{_escape_regex(target)}/i)
            ).or(
                page.getByLabel(/{_escape_regex(target)}/i)
            ).or(
                page.locator('input[name*="{target}" i], textarea[name*="{target}" i]')
            ).first();
            await element.fill({json.dumps(text)}, {{ timeout: 4000 }});
            return {{ success: true }};
        }} catch (e) {{
            return {{ success: false, error: e.message }};
        }}
    }}"""
    return {"code": code}


def _build_hover_code(step: dict) -> dict:
    """Build Playwright code for hover action."""
    target = step.get("target", "")
    # Wrap in try-catch to prevent session destruction on failure
    code = f"""async (page) => {{
        try {{
            await page.getByText(/{_escape_regex(target)}/i).or(
                page.getByRole('button', {{ name: /{_escape_regex(target)}/i }})
            ).first().hover({{ timeout: 4000 }});
            return {{ success: true }};
        }} catch (e) {{
            return {{ success: false, error: e.message }};
        }}
    }}"""
    return {"code": code}


def _build_select_code(step: dict) -> dict:
    """Build Playwright code for select action."""
    target = step.get("target", "")
    value = step.get("value", "")
    # Wrap in try-catch to prevent session destruction on failure
    code = f"""async (page) => {{
        try {{
            await page.getByRole('combobox', {{ name: /{_escape_regex(target)}/i }}).or(
                page.getByLabel(/{_escape_regex(target)}/i)
            ).first().selectOption({json.dumps(value)}, {{ timeout: 4000 }});
            return {{ success: true }};
        }} catch (e) {{
            return {{ success: false, error: e.message }};
        }}
    }}"""
    return {"code": code}


def _build_assert_text_code(step: dict) -> dict:
    """Build Playwright code for assert_text action."""
    text = step.get("value", "")
    # Wrap in try-catch to prevent session destruction on failure
    code = f"""async (page) => {{
        try {{
            const element = page.getByText(/{_escape_regex(text)}/i).first();
            await expect(element).toBeVisible({{ timeout: 4000 }});
            return {{ success: true }};
        }} catch (e) {{
            return {{ success: false, error: e.message }};
        }}
    }}"""
    return {"code": code}


def _build_assert_element_code(step: dict) -> dict:
    """Build Playwright code for assert_element action."""
    target = step.get("target", "")
    # Wrap in try-catch to prevent session destruction on failure
    code = f"""async (page) => {{
        try {{
            const element = page.getByRole('button', {{ name: /{_escape_regex(target)}/i }}).or(
                page.getByText(/{_escape_regex(target)}/i)
            ).or(
                page.locator('[data-testid*="{target}"]')
            ).first();
            await expect(element).toBeVisible({{ timeout: 4000 }});
            return {{ success: true }};
        }} catch (e) {{
            return {{ success: false, error: e.message }};
        }}
    }}"""
    return {"code": code}


def _build_drag_code(step: dict) -> dict:
    """Build Playwright code for drag action."""
    start = step.get("target", "")
    end = step.get("value", "")
    # Wrap in try-catch to prevent session destruction on failure
    code = f"""async (page) => {{
        try {{
            const source = page.getByText(/{_escape_regex(start)}/i).first();
            const target = page.getByText(/{_escape_regex(end)}/i).first();
            await source.dragTo(target);
            return {{ success: true }};
        }} catch (e) {{
            return {{ success: false, error: e.message }};
        }}
    }}"""
    return {"code": code}


def _escape_regex(text: str) -> str:
    """Escape special regex characters."""
    import re
    return re.sub(r'([.+*?^${}()|[\]\\])', r'\\\1', text)


# Common element type suffixes to strip from targets
ELEMENT_TYPE_SUFFIXES = [
    " link", " button", " btn", " input", " field", " text",
    " image", " img", " icon", " checkbox", " radio", " dropdown",
    " menu", " tab", " option", " label", " heading", " title",
]


def _strip_element_suffix(target: str) -> str:
    """Strip common element type suffixes from target text.

    E.g., "credentials link" → "credentials"
    """
    target_lower = target.lower()
    for suffix in ELEMENT_TYPE_SUFFIXES:
        if target_lower.endswith(suffix):
            return target[:len(target) - len(suffix)].strip()
    return target


def _build_wait_args(step: dict) -> dict:
    """Build arguments for browser_wait_for tool."""
    target = step.get("target")
    value = step.get("value", "")

    # If target is provided, wait for that text to appear
    # Strip common element type suffixes (e.g., "credentials link" → "credentials")
    if target:
        clean_target = _strip_element_suffix(target)
        return {"text": clean_target}

    # If value is a number (milliseconds), convert to seconds
    if value:
        try:
            ms = int(value)
            return {"time": ms / 1000}  # Convert ms to seconds
        except ValueError:
            # Wait for text to appear
            return {"text": value}

    # Default: wait 1 second
    return {"time": 1}


async def _poll_for_element(client: "PlaywrightMCPClient", text: str, timeout_ms: int = 10000) -> Tuple[bool, str]:
    """
    Poll for an element/text using snapshots instead of browser_wait_for.

    This is more resilient because:
    1. Snapshot failures don't destroy the session
    2. We can detect session issues and reinitialize

    Args:
        client: PlaywrightMCPClient instance
        text: Text to wait for
        timeout_ms: Maximum wait time in milliseconds

    Returns:
        Tuple of (found: bool, snapshot: str)
    """
    start_time = time.time()
    poll_interval = 0.5  # 500ms between polls
    text_lower = text.lower()

    while (time.time() - start_time) * 1000 < timeout_ms:
        try:
            snapshot = await client.get_snapshot()

            # Check if text is in snapshot
            if text_lower in snapshot.lower():
                return (True, snapshot)

        except Exception as e:
            error_msg = str(e)
            # If session is lost, try to reinitialize
            if "Session not found" in error_msg or "404" in error_msg:
                try:
                    client.initialized = False
                    client.session_id = None
                    await client.initialize()
                    # After reinitializing, try snapshot again
                    snapshot = await client.get_snapshot()
                    if text_lower in snapshot.lower():
                        return (True, snapshot)
                except Exception:
                    pass  # Continue polling

        await asyncio.sleep(poll_interval)

    # Timeout - return last snapshot if available
    try:
        snapshot = await client.get_snapshot()
        return (False, snapshot)
    except Exception:
        return (False, "")


ACTION_TO_TOOL = {
    # Native MCP tools (no ref required)
    "navigate": ("browser_navigate", lambda s: {"url": s.get("value")}),
    "back": ("browser_navigate_back", lambda s: {}),
    "press_key": ("browser_press_key", lambda s: {"key": s.get("value")}),
    "screenshot": ("browser_take_screenshot", lambda s: {"filename": s.get("value", "screenshot.png")}),
    "upload": ("browser_file_upload", lambda s: {"paths": _parse_paths(s.get("value", ""))}),
    "fill_form": ("browser_fill_form", lambda s: _parse_fill_form_args(s)),
    "wait": ("browser_wait_for", _build_wait_args),  # Use native wait

    # Use browser_run_code for actions that need element interaction
    "click": ("browser_run_code", _build_click_code),
    "type": ("browser_run_code", _build_type_code),
    "hover": ("browser_run_code", _build_hover_code),
    "select": ("browser_run_code", _build_select_code),
    "drag": ("browser_run_code", _build_drag_code),

    # Assertions via browser_run_code with expect
    "assert_text": ("browser_run_code", _build_assert_text_code),
    "assert_element": ("browser_run_code", _build_assert_element_code),
    "assert_style": ("browser_run_code", lambda s: {"code": f"async (page) => {{ /* TODO: style check */ }}"}),

    # Evaluate runs custom JS
    "evaluate": ("browser_run_code", lambda s: {"code": s.get("value")}),
}


def _parse_fill_form_args(step: dict) -> dict:
    """Parse fill_form step value to fields dict."""
    value = step.get("value", "{}")
    if isinstance(value, str):
        try:
            return {"fields": json.loads(value)}
        except json.JSONDecodeError:
            return {"fields": {}}
    return {"fields": value}


def _parse_paths(value: str) -> list:
    """Parse comma-separated file paths."""
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


async def execute_step(client: PlaywrightMCPClient, step: dict) -> dict:
    """
    Execute a single test step via MCP.

    For element interactions (click, type, hover), uses snapshot-based element finding
    for better error handling.

    Args:
        client: PlaywrightMCPClient instance
        step: Test step dict with action, target, value, description

    Returns:
        dict with status ('passed'/'failed'), error (if any), duration (ms), result (MCP response)
    """
    action = step.get("action", "")
    target = step.get("target", "")
    start_time = time.time()

    if action not in ACTION_TO_TOOL:
        return {
            "status": "failed",
            "error": f"Unknown action: {action}",
            "duration": 0
        }

    used_fallback = False  # Track if we used browser_run_code fallback

    try:
        # For element interactions, use snapshot-based approach for better error handling
        if action in ("click", "type", "hover", "select") and target:
            # Take snapshot first
            snapshot = await client.get_snapshot()
            element_info = client.find_element_ref(snapshot, target)

            if element_info:
                # Found in snapshot - use native MCP tools with element + ref
                element_desc, ref = element_info

                if action == "click":
                    result = await client.call_tool("browser_click", {
                        "element": element_desc,
                        "ref": ref
                    })
                elif action == "type":
                    text = step.get("value", "")
                    result = await client.call_tool("browser_type", {
                        "element": element_desc,
                        "ref": ref,
                        "text": text
                    })
                elif action == "hover":
                    result = await client.call_tool("browser_hover", {
                        "element": element_desc,
                        "ref": ref
                    })
                elif action == "select":
                    values = step.get("value", "")
                    result = await client.call_tool("browser_select_option", {
                        "element": element_desc,
                        "ref": ref,
                        "values": [values] if isinstance(values, str) else values
                    })
            else:
                # Element not in snapshot - fallback to flexible Playwright selectors
                # This handles <span> styled as buttons, custom elements, etc.
                # IMPORTANT: Wrap in try-catch to prevent session destruction on timeout
                used_fallback = True
                clean_target = _strip_element_suffix(target)

                if action == "click":
                    code = f"""async (page) => {{
                        try {{
                            await page.getByRole('button', {{ name: /{_escape_regex(clean_target)}/i }}).or(
                                page.getByRole('link', {{ name: /{_escape_regex(clean_target)}/i }})
                            ).or(
                                page.getByText(/{_escape_regex(clean_target)}/i)
                            ).first().click({{ timeout: 4000 }});
                            return {{ success: true }};
                        }} catch (e) {{
                            return {{ success: false, error: e.message }};
                        }}
                    }}"""
                    result = await client.call_tool("browser_run_code", {"code": code})
                elif action == "type":
                    text = step.get("value", "")
                    code = f"""async (page) => {{
                        try {{
                            await page.getByRole('textbox', {{ name: /{_escape_regex(clean_target)}/i }}).or(
                                page.getByPlaceholder(/{_escape_regex(clean_target)}/i)
                            ).or(
                                page.getByLabel(/{_escape_regex(clean_target)}/i)
                            ).first().fill({json.dumps(text)}, {{ timeout: 4000 }});
                            return {{ success: true }};
                        }} catch (e) {{
                            return {{ success: false, error: e.message }};
                        }}
                    }}"""
                    result = await client.call_tool("browser_run_code", {"code": code})
                elif action == "hover":
                    code = f"""async (page) => {{
                        try {{
                            await page.getByText(/{_escape_regex(clean_target)}/i).first().hover({{ timeout: 4000 }});
                            return {{ success: true }};
                        }} catch (e) {{
                            return {{ success: false, error: e.message }};
                        }}
                    }}"""
                    result = await client.call_tool("browser_run_code", {"code": code})
                elif action == "select":
                    values = step.get("value", "")
                    code = f"""async (page) => {{
                        try {{
                            await page.getByRole('combobox', {{ name: /{_escape_regex(clean_target)}/i }}).or(
                                page.getByLabel(/{_escape_regex(clean_target)}/i)
                            ).first().selectOption({json.dumps(values)}, {{ timeout: 4000 }});
                            return {{ success: true }};
                        }} catch (e) {{
                            return {{ success: false, error: e.message }};
                        }}
                    }}"""
                    result = await client.call_tool("browser_run_code", {"code": code})
        elif action == "wait":
            # Use polling approach for wait - more resilient than native browser_wait_for
            # Native wait can destroy MCP session on failure
            wait_args = _build_wait_args(step)
            logger.debug(f"[WAIT] step={step}, wait_args={wait_args}")

            if "text" in wait_args:
                # Wait for text to appear using polling
                text_to_find = wait_args["text"]
                found, snapshot = await _poll_for_element(client, text_to_find, timeout_ms=10000)

                if found:
                    result = {"content": [{"type": "text", "text": f"Found: {text_to_find}"}]}
                else:
                    duration = int((time.time() - start_time) * 1000)
                    return {
                        "status": "failed",
                        "error": f"Timeout waiting for: {text_to_find}",
                        "duration": duration
                    }
            elif "time" in wait_args:
                # Simple time-based wait using asyncio.sleep
                wait_seconds = wait_args["time"]
                logger.debug(f"[WAIT] Sleeping for {wait_seconds}s")
                await asyncio.sleep(wait_seconds)
                logger.debug(f"[WAIT] Sleep completed")
                result = {"content": [{"type": "text", "text": f"Waited {wait_seconds}s"}]}
            else:
                # Fallback to native wait
                result = await client.call_tool("browser_wait_for", wait_args)
        else:
            # Use the tool mapping for other actions
            tool_name, args_fn = ACTION_TO_TOOL[action]
            arguments = args_fn(step)
            result = await client.call_tool(tool_name, arguments)

        duration = int((time.time() - start_time) * 1000)

        # For fallback browser_run_code, empty result means failure
        # (the action timed out or failed, possibly destroying the session)
        if not result or not result.get("content"):
            if used_fallback:
                return {
                    "status": "failed",
                    "error": f"Element not found or action timed out: {target}",
                    "duration": duration
                }
            # For native MCP tools, empty result may be OK (element was verified via snapshot)
            result = result or {}

        # Check for wrapped code result (success/error pattern)
        # Our fallback code wraps actions in try-catch and returns {success, error}
        if used_fallback and result.get("content"):
            content = result.get("content", [])
            for item in content:
                if item.get("type") == "text":
                    text = item.get("text", "")
                    # Look for JSON result in the text (after "### Result" section)
                    if "success" in text.lower():
                        # Try to extract the result object
                        if '"success": false' in text or '"success":false' in text:
                            # Extract error message
                            error_match = re.search(r'"error":\s*"([^"]+)"', text)
                            error_msg = error_match.group(1) if error_match else f"Action failed on: {target}"
                            return {
                                "status": "failed",
                                "error": error_msg,
                                "duration": duration
                            }
                        elif '"success": true' in text or '"success":true' in text:
                            # Success - continue to return passed
                            break

        # Check for MCP error flag (isError: true in response)
        if result.get("isError"):
            # Extract error message from content
            error_msg = "Action failed"
            content = result.get("content", [])
            if content and isinstance(content, list):
                for item in content:
                    if item.get("type") == "text":
                        text = item.get("text", "")
                        # Extract error from text
                        if "Error" in text or "error" in text or "timeout" in text.lower():
                            lines = text.split("\n")
                            for line in lines:
                                if "Error" in line or "error" in line or "timeout" in line.lower():
                                    error_msg = line.strip().replace("### Result", "").strip()
                                    break
                            break
            return {
                "status": "failed",
                "error": error_msg,
                "duration": duration
            }

        # Check content for actual Playwright errors (not console messages)
        content = result.get("content", [])
        if content and isinstance(content, list):
            for item in content:
                if item.get("type") == "text":
                    text = item.get("text", "")
                    # Only check the "### Result" section for errors, not console messages
                    result_section = ""
                    if "### Result" in text:
                        # Extract just the Result section
                        parts = text.split("###")
                        for part in parts:
                            if part.strip().startswith("Result"):
                                result_section = part
                                break

                    # Check for Playwright errors in the Result section only
                    if result_section:
                        if "TimeoutError" in result_section or "Error:" in result_section:
                            # Extract the error line
                            lines = result_section.split("\n")
                            for line in lines:
                                line = line.strip()
                                if line and ("TimeoutError" in line or "Error:" in line):
                                    return {
                                        "status": "failed",
                                        "error": line,
                                        "duration": duration
                                    }
                            return {
                                "status": "failed",
                                "error": "Action failed",
                                "duration": duration
                            }

        # Check for assertion failures
        if action.startswith("assert_"):
            # For assertions, check the snapshot result
            if not result.get("success", True):
                return {
                    "status": "failed",
                    "error": result.get("message", "Assertion failed"),
                    "duration": duration
                }

        return {
            "status": "passed",
            "error": None,
            "duration": duration,
            "result": result
        }

    except Exception as e:
        duration = int((time.time() - start_time) * 1000)
        return {
            "status": "failed",
            "error": str(e),
            "duration": duration
        }


async def capture_failure_screenshot(client: PlaywrightMCPClient, step_number: int) -> Optional[str]:
    """
    Capture a screenshot when a step fails.

    Args:
        client: PlaywrightMCPClient instance
        step_number: The step number that failed

    Returns:
        Screenshot path if successful, None otherwise
    """
    try:
        result = await client.call_tool(
            "browser_take_screenshot",
            {"name": f"failure_step_{step_number}"}
        )
        return result.get("path")
    except Exception:
        return None


async def test_mcp_connection(client: PlaywrightMCPClient) -> bool:
    """
    Test if MCP server is available by calling browser_snapshot.

    Returns:
        True if connection successful, False otherwise
    """
    try:
        await client.call_tool("browser_snapshot", {})
        return True
    except Exception:
        return False
