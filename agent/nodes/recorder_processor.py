"""Recorder event processor — converts raw DOM events into Checkmate test steps.

Pipeline: Raw Event → Debounce/Merge → Rule-Based Classifier → ProcessedStep

Most common interactions are handled by deterministic rules (fast path).
After recording stops, a separate AI refinement pass transforms these into
builder-quality steps (with proper actions, readable targets, waits, etc.).
"""

from __future__ import annotations

import time
from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from core.logging import get_logger

logger = get_logger(__name__)


class ProcessedStep(BaseModel):
    """A single processed test step ready for the frontend."""
    action: str
    target: Optional[str] = None
    value: Optional[str] = None
    description: str = ""
    is_credential: bool = False
    confidence: float = 1.0
    coordinates: Optional[dict] = None  # {x, y, pageX, pageY} from recorder
    locators: Optional[dict] = None  # Waterfall fallback: {css, text, ariaPath, coordinates}
    causes_navigation: bool = False  # True when click triggered page navigation


class RecorderEventProcessor:
    """Stateful processor that converts raw recorder events into test steps.

    Handles debouncing (e.g. consecutive type events on the same field),
    merging (click on link + navigate → keep click, drop navigate), and
    classification (rule-based for common patterns).
    """

    def __init__(self, base_url: str = ""):
        self.steps: list[ProcessedStep] = []
        self._pending_click: dict | None = None
        self._pending_click_ts: float = 0
        self._last_navigate_url: str = ""
        self._last_click_ts: float = 0  # timestamp of last emitted click (any tag)
        self._base_url = base_url
        self._base_origin = ""
        if base_url:
            parsed = urlparse(base_url)
            self._base_origin = f"{parsed.scheme}://{parsed.netloc}"

    def _to_relative_path(self, url: str) -> str:
        """Convert a full URL to a relative path if it shares the same origin."""
        if not url or not self._base_origin:
            return url
        if url.startswith(self._base_origin):
            path = url[len(self._base_origin):]
            return path if path else "/"
        return url

    def process_event(self, event: dict) -> ProcessedStep | None:
        """Process a single raw event and return a step, or None if buffered.

        Returns:
            A ProcessedStep if one is ready, None if the event was buffered.
        """
        event_type = event.get("type", "")

        # If we have a pending click and a navigate arrives within 500ms,
        # KEEP the click and DROP the navigate (the click caused the navigation)
        if self._pending_click and event_type == "navigate":
            elapsed = (event.get("timestamp", 0) - self._pending_click_ts)
            if elapsed < 500:
                # Flush the click as the actual step — skip the navigate
                click_step = self._make_click_step(self._pending_click)
                click_step.causes_navigation = True
                self._pending_click = None
                # Track the navigate URL (relative form) to suppress duplicate navigates
                nav_url = event.get("value") or event.get("url", "")
                if nav_url:
                    self._last_navigate_url = self._to_relative_path(nav_url)
                self.steps.append(click_step)
                return click_step

        # Flush any pending click before processing a non-navigate event
        flushed = self._flush_pending_click()

        step = None

        if event_type == "navigate":
            step = self._process_navigate(event)
        elif event_type == "click":
            step = self._process_click(event)
        elif event_type == "type":
            step = self._process_type(event)
        elif event_type == "select":
            step = self._process_select(event)
        elif event_type == "scroll":
            step = self._process_scroll(event)
        elif event_type == "hover":
            step = self._process_hover(event)
        else:
            step = self._process_unknown(event)

        # If the click was flushed, append it before the current step
        if flushed:
            self.steps.append(flushed)

        if step:
            self.steps.append(step)

        return flushed or step

    def _flush_pending_click(self) -> ProcessedStep | None:
        """Flush any buffered click event as a step."""
        if not self._pending_click:
            return None
        step = self._make_click_step(self._pending_click)
        self._pending_click = None
        return step

    def _process_navigate(self, event: dict) -> ProcessedStep | None:
        url = event.get("value") or event.get("url", "")
        if not url:
            return None
        relative = self._to_relative_path(url)
        # Compare after relative conversion — raw URLs from _handle_navigation
        # (full: "http://localhost:3001/") differ from recorder_script.js
        # (relative: "/"), but both convert to "/" after _to_relative_path.
        if relative == self._last_navigate_url:
            return None
        # Suppress navigates that follow a click within 800ms — the click
        # already navigated (via router.push, <a> href, etc.). Without this,
        # "Click Checkmate-qa" is followed by a redundant "Navigate to /projects/2".
        nav_ts = event.get("timestamp", 0)
        if self._last_click_ts and nav_ts and (nav_ts - self._last_click_ts) < 800:
            self._last_navigate_url = relative
            return None
        self._last_navigate_url = relative
        return ProcessedStep(
            action="navigate",
            value=relative,
            description=f"Navigate to {relative}",
        )

    def _process_click(self, event: dict) -> ProcessedStep | None:
        tag = (event.get("tag") or "").upper()

        # For links, buffer the click — a navigate event may follow
        if tag == "A":
            self._pending_click = event
            self._pending_click_ts = event.get("timestamp", time.time() * 1000)
            return None

        return self._make_click_step(event)

    def _make_click_step(self, event: dict) -> ProcessedStep:
        # Track click time for navigate suppression (any click, not just <a>)
        self._last_click_ts = event.get("timestamp", 0)

        tag = (event.get("tag") or "").upper()
        text = event.get("text", "").strip()
        selector = event.get("selector", "")

        coords = event.get("coordinates")  # {x, y, pageX, pageY}
        aria_path = event.get("ariaPath", "")

        # Build locators dict for waterfall resolution at execution time.
        # Each key is a strategy the executor can try in order.
        locators: dict = {}
        if selector:
            locators["css"] = selector
        if text:
            locators["text"] = text
        if aria_path:
            locators["ariaPath"] = aria_path
        if coords:
            locators["coordinates"] = coords

        # Prefer data-testid CSS selectors — they are unique per element and
        # allow the executor to find the exact element without ambiguity.
        if selector and selector.startswith("[data-testid="):
            desc = f'Click "{text}"' if text else f"Click {selector}"
            return ProcessedStep(
                action="click",
                target=selector,
                description=desc,
                confidence=1.0,
                coordinates=coords,
                locators=locators or None,
            )

        # Determine target description
        if tag in ("BUTTON", "A") and text:
            target = text
            desc = f'Click "{text}"'
        elif tag == "INPUT" and event.get("type") == "submit":
            target = text or "Submit"
            desc = f"Click submit button"
        elif text and len(text) < 50:
            target = text
            desc = f'Click "{text}"'
        else:
            target = selector
            desc = f"Click {selector}"

        return ProcessedStep(
            action="click",
            target=target,
            description=desc,
            confidence=0.9 if tag in ("BUTTON", "A", "INPUT") else 0.6,
            coordinates=coords,
            locators=locators or None,
        )

    def _process_type(self, event: dict) -> ProcessedStep:
        label = event.get("text", "").strip()
        value = event.get("value", "")
        is_password = event.get("is_password", False)

        target = label if label else event.get("selector", "input")
        desc = f'Type in "{target}"'
        if is_password:
            desc += " (password)"

        return ProcessedStep(
            action="type",
            target=target,
            value="{{password}}" if is_password else value,
            description=desc,
            is_credential=is_password,
        )

    def _process_select(self, event: dict) -> ProcessedStep:
        label = event.get("text", "").strip()
        value = event.get("value", "")
        target = label if label else event.get("selector", "select")

        return ProcessedStep(
            action="select",
            target=target,
            value=value,
            description=f'Select "{value}" from "{target}"',
        )

    def _process_hover(self, event: dict) -> ProcessedStep:
        text = event.get("text", "").strip()
        target = text if text else event.get("selector", "")
        return ProcessedStep(
            action="hover",
            target=target,
            description=f'Hover over "{target}" to reveal submenu',
            confidence=0.9,
        )

    def _process_scroll(self, event: dict) -> ProcessedStep | None:
        # Collapse consecutive scroll events — if the last step is already a scroll,
        # update it in place rather than adding a new one.
        if self.steps and self.steps[-1].action == "scroll":
            return None  # absorbed into the existing scroll step
        return ProcessedStep(
            action="scroll",
            target="page",
            value="down",
            description="Scroll down the page",
            confidence=0.7,
        )

    def _process_unknown(self, event: dict) -> ProcessedStep | None:
        """Handle unknown event types — low confidence."""
        event_type = event.get("type", "unknown")
        logger.debug(f"Unknown event type: {event_type}")
        return ProcessedStep(
            action="click",
            target=event.get("selector", ""),
            description=f"Interaction ({event_type})",
            confidence=0.4,
        )

    def get_all_steps(self) -> list[ProcessedStep]:
        """Return all processed steps so far, flushing any pending events."""
        flushed = self._flush_pending_click()
        if flushed:
            self.steps.append(flushed)
        return self.steps.copy()
