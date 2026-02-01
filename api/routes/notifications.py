"""Notification channel management API routes."""

from typing import List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from db.session import get_session_dep
from db.models import (
    NotificationChannel,
    NotificationChannelCreate,
    NotificationChannelRead,
    NotificationChannelUpdate,
)
from db import crud
from core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/projects/{project_id}/notifications", tags=["notifications"])


@router.get("", response_model=List[NotificationChannelRead])
def list_notification_channels(
    project_id: int,
    session: Session = Depends(get_session_dep)
):
    """List all notification channels for a project."""
    # Verify project exists
    project = crud.get_project(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return crud.get_notification_channels_by_project(session, project_id)


class NotificationChannelCreateRequest(BaseModel):
    """Request body for creating a notification channel."""
    name: str
    channel_type: str = "webhook"
    enabled: bool = True
    webhook_url: str | None = None
    webhook_template: str | None = None
    email_recipients: List[str] | None = None
    email_template: str | None = None
    notify_on: str = "failure"


@router.post("", response_model=NotificationChannelRead)
def create_notification_channel(
    project_id: int,
    request: NotificationChannelCreateRequest,
    session: Session = Depends(get_session_dep)
):
    """Create a new notification channel."""
    # Verify project exists
    project = crud.get_project(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Convert email_recipients list to JSON string if provided
    import json
    email_recipients_json = json.dumps(request.email_recipients) if request.email_recipients else None

    channel = NotificationChannelCreate(
        project_id=project_id,
        name=request.name,
        channel_type=request.channel_type,
        enabled=request.enabled,
        webhook_url=request.webhook_url,
        webhook_template=request.webhook_template,
        email_recipients=email_recipients_json,
        email_template=request.email_template,
        notify_on=request.notify_on,
    )
    return crud.create_notification_channel(session, channel)


@router.get("/{channel_id}", response_model=NotificationChannelRead)
def get_notification_channel(
    project_id: int,
    channel_id: int,
    session: Session = Depends(get_session_dep)
):
    """Get a notification channel by ID."""
    channel = crud.get_notification_channel(session, channel_id)
    if not channel or channel.project_id != project_id:
        raise HTTPException(status_code=404, detail="Notification channel not found")
    return channel


class NotificationChannelUpdateRequest(BaseModel):
    """Request body for updating a notification channel."""
    name: str | None = None
    channel_type: str | None = None
    enabled: bool | None = None
    webhook_url: str | None = None
    webhook_template: str | None = None
    email_recipients: List[str] | None = None
    email_template: str | None = None
    notify_on: str | None = None


@router.put("/{channel_id}", response_model=NotificationChannelRead)
def update_notification_channel(
    project_id: int,
    channel_id: int,
    request: NotificationChannelUpdateRequest,
    session: Session = Depends(get_session_dep)
):
    """Update a notification channel."""
    channel = crud.get_notification_channel(session, channel_id)
    if not channel or channel.project_id != project_id:
        raise HTTPException(status_code=404, detail="Notification channel not found")

    # Convert email_recipients list to JSON string if provided
    import json
    update_data = NotificationChannelUpdate(
        name=request.name,
        channel_type=request.channel_type,
        enabled=request.enabled,
        webhook_url=request.webhook_url,
        webhook_template=request.webhook_template,
        email_recipients=json.dumps(request.email_recipients) if request.email_recipients is not None else None,
        email_template=request.email_template,
        notify_on=request.notify_on,
    )

    updated = crud.update_notification_channel(session, channel_id, update_data)
    if not updated:
        raise HTTPException(status_code=404, detail="Notification channel not found")
    return updated


@router.delete("/{channel_id}")
def delete_notification_channel(
    project_id: int,
    channel_id: int,
    session: Session = Depends(get_session_dep)
):
    """Delete a notification channel."""
    channel = crud.get_notification_channel(session, channel_id)
    if not channel or channel.project_id != project_id:
        raise HTTPException(status_code=404, detail="Notification channel not found")

    success = crud.delete_notification_channel(session, channel_id)
    if not success:
        raise HTTPException(status_code=404, detail="Notification channel not found")
    return {"status": "deleted"}


class TestNotificationRequest(BaseModel):
    """Request body for testing a notification channel."""
    pass  # Empty for now, could add custom test payload later


@router.post("/{channel_id}/test")
async def test_notification_channel(
    project_id: int,
    channel_id: int,
    request: TestNotificationRequest | None = None,
    session: Session = Depends(get_session_dep)
):
    """Send a test notification to verify channel configuration."""
    channel = crud.get_notification_channel(session, channel_id)
    if not channel or channel.project_id != project_id:
        raise HTTPException(status_code=404, detail="Notification channel not found")

    # Create a mock scheduled run for testing
    from db.models import RunStatus, Schedule, ScheduledRun
    from scheduler.notifier import send_webhook, send_email

    # Create mock objects
    mock_schedule = Schedule(
        id=0,
        project_id=project_id,
        name="Test Schedule",
        cron_expression="0 0 * * *",
        timezone="UTC",
        target_type="test_case_ids",
    )

    mock_run = ScheduledRun(
        id=0,
        schedule_id=0,
        project_id=project_id,
        thread_id="test-notification",
        status=RunStatus.PASSED,
        test_count=5,
        pass_count=4,
        fail_count=1,
    )

    # Send test notification
    success = False
    error = ""

    if channel.channel_type in ("webhook", "slack"):
        success, error = await send_webhook(channel, mock_run, mock_schedule)
    elif channel.channel_type == "email":
        success, error = await send_email(channel, mock_run, mock_schedule)
    else:
        error = f"Unknown channel type: {channel.channel_type}"

    if success:
        return {"status": "success", "message": "Test notification sent successfully"}
    else:
        raise HTTPException(status_code=400, detail=f"Failed to send test notification: {error}")
