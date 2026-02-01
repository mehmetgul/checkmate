"""Notification dispatcher for scheduled runs."""

import json
import re
from typing import List, Tuple, Dict, Any

import httpx

from core.logging import get_logger
from db.models import NotificationChannel, Schedule, ScheduledRun, RunStatus

logger = get_logger(__name__)

# Default Slack-compatible webhook template
DEFAULT_WEBHOOK_TEMPLATE = """{
  "text": "Scheduled Run: {{schedule.name}}",
  "attachments": [{
    "color": "{{color}}",
    "fields": [
      {"title": "Status", "value": "{{status}}", "short": true},
      {"title": "Tests", "value": "{{pass_count}}/{{test_count}} passed", "short": true},
      {"title": "Failed", "value": "{{fail_count}}", "short": true}
    ]
  }]
}"""


def _get_template_context(scheduled_run: ScheduledRun, schedule: Schedule) -> Dict[str, Any]:
    """Build template context from scheduled run and schedule data."""
    status = "passed" if scheduled_run.status == RunStatus.PASSED else "failed"
    color = "good" if status == "passed" else "danger"

    # Calculate duration if we have both timestamps
    duration = None
    if scheduled_run.started_at and scheduled_run.completed_at:
        delta = scheduled_run.completed_at - scheduled_run.started_at
        duration = f"{int(delta.total_seconds())}s"

    return {
        "schedule.name": schedule.name,
        "schedule.id": str(schedule.id),
        "status": status,
        "color": color,
        "pass_count": str(scheduled_run.pass_count),
        "fail_count": str(scheduled_run.fail_count),
        "test_count": str(scheduled_run.test_count),
        "started_at": scheduled_run.started_at.isoformat() if scheduled_run.started_at else "",
        "completed_at": scheduled_run.completed_at.isoformat() if scheduled_run.completed_at else "",
        "duration": duration or "N/A",
        "run_id": str(scheduled_run.id),
        "thread_id": scheduled_run.thread_id,
    }


def _render_template(template: str, context: Dict[str, Any]) -> str:
    """Render a template with simple {{variable}} substitution."""
    result = template
    for key, value in context.items():
        result = result.replace("{{" + key + "}}", str(value))
    return result


async def send_webhook(channel: NotificationChannel, scheduled_run: ScheduledRun, schedule: Schedule) -> Tuple[bool, str]:
    """Send a webhook notification.

    Returns:
        Tuple of (success, error_message)
    """
    if not channel.webhook_url:
        return False, "No webhook URL configured"

    # Get template
    template = channel.webhook_template or DEFAULT_WEBHOOK_TEMPLATE

    # Build context and render
    context = _get_template_context(scheduled_run, schedule)
    payload_str = _render_template(template, context)

    try:
        payload = json.loads(payload_str)
    except json.JSONDecodeError as e:
        logger.error(f"Invalid webhook template JSON for channel {channel.id}: {e}")
        return False, f"Invalid template JSON: {e}"

    # Send the webhook
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                channel.webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            logger.info(f"Webhook sent successfully to channel {channel.id}")
            return True, ""
    except httpx.HTTPStatusError as e:
        error = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        logger.error(f"Webhook failed for channel {channel.id}: {error}")
        return False, error
    except httpx.RequestError as e:
        error = f"Request error: {str(e)}"
        logger.error(f"Webhook failed for channel {channel.id}: {error}")
        return False, error
    except Exception as e:
        error = f"Unexpected error: {str(e)}"
        logger.error(f"Webhook failed for channel {channel.id}: {error}")
        return False, error


async def send_email(channel: NotificationChannel, scheduled_run: ScheduledRun, schedule: Schedule) -> Tuple[bool, str]:
    """Send an email notification (placeholder for future implementation).

    Returns:
        Tuple of (success, error_message)
    """
    # Email sending requires SMTP configuration, which is not yet implemented
    logger.warning(f"Email notifications not yet implemented for channel {channel.id}")
    return False, "Email notifications not yet implemented"


async def send_notifications(
    scheduled_run: ScheduledRun,
    schedule: Schedule,
    channels: List[NotificationChannel]
) -> Tuple[List[int], Dict[int, str]]:
    """Send notifications to all specified channels.

    Returns:
        Tuple of (list of successful channel IDs, dict of channel_id -> error message)
    """
    sent_ids = []
    errors = {}

    for channel in channels:
        success = False
        error = ""

        if channel.channel_type == "webhook":
            success, error = await send_webhook(channel, scheduled_run, schedule)
        elif channel.channel_type == "email":
            success, error = await send_email(channel, scheduled_run, schedule)
        elif channel.channel_type == "slack":
            # Slack uses webhook URL, same as generic webhook
            success, error = await send_webhook(channel, scheduled_run, schedule)
        else:
            error = f"Unknown channel type: {channel.channel_type}"
            logger.warning(error)

        if success:
            sent_ids.append(channel.id)
        else:
            errors[channel.id] = error

    logger.info(f"Notifications sent: {len(sent_ids)} success, {len(errors)} failed")
    return sent_ids, errors
