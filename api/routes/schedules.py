"""Schedule management API routes."""

import json
from datetime import datetime
from typing import List, Optional

import pytz
from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlmodel import Session

from db.session import get_session_dep
from db.models import (
    Schedule,
    ScheduleCreate,
    ScheduleRead,
    ScheduleUpdate,
    ScheduledRun,
    ScheduledRunRead,
)
from db import crud
from core.logging import get_logger
from scheduler import scheduler_service

logger = get_logger(__name__)

router = APIRouter(prefix="/projects/{project_id}/schedules", tags=["schedules"])


def validate_cron_expression(cron: str) -> str:
    """Validate a cron expression."""
    try:
        croniter(cron)
        return cron
    except (ValueError, KeyError) as e:
        raise ValueError(f"Invalid cron expression: {e}")


def validate_timezone(tz: str) -> str:
    """Validate a timezone string."""
    try:
        pytz.timezone(tz)
        return tz
    except pytz.UnknownTimeZoneError:
        raise ValueError(f"Unknown timezone: {tz}")


@router.get("", response_model=List[ScheduleRead])
def list_schedules(
    project_id: int,
    session: Session = Depends(get_session_dep)
):
    """List all schedules for a project."""
    project = crud.get_project(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return crud.get_schedules_by_project(session, project_id)


class ScheduleCreateRequest(BaseModel):
    """Request body for creating a schedule."""
    name: str
    description: Optional[str] = None
    cron_expression: str
    timezone: str = "UTC"
    target_type: str = "test_case_ids"
    target_test_case_ids: Optional[List[int]] = None
    target_tags: Optional[List[str]] = None
    browser: Optional[str] = None
    retry_max: int = 0
    retry_mode: Optional[str] = None
    enabled: bool = True
    notification_channel_ids: Optional[List[int]] = None

    @field_validator("cron_expression")
    @classmethod
    def validate_cron(cls, v):
        return validate_cron_expression(v)

    @field_validator("timezone")
    @classmethod
    def validate_tz(cls, v):
        return validate_timezone(v)


@router.post("", response_model=ScheduleRead)
def create_schedule(
    project_id: int,
    request: ScheduleCreateRequest,
    session: Session = Depends(get_session_dep)
):
    """Create a new schedule."""
    project = crud.get_project(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Validate target configuration
    if request.target_type == "test_case_ids" and not request.target_test_case_ids:
        raise HTTPException(status_code=400, detail="target_test_case_ids required when target_type is 'test_case_ids'")
    if request.target_type == "tags" and not request.target_tags:
        raise HTTPException(status_code=400, detail="target_tags required when target_type is 'tags'")

    # Validate notification channels exist
    if request.notification_channel_ids:
        channels = crud.get_notification_channels_by_ids(session, request.notification_channel_ids)
        if len(channels) != len(request.notification_channel_ids):
            raise HTTPException(status_code=400, detail="One or more notification channels not found")
        # Verify they belong to this project
        for ch in channels:
            if ch.project_id != project_id:
                raise HTTPException(status_code=400, detail=f"Notification channel {ch.id} does not belong to this project")

    # Calculate next run time
    tz = pytz.timezone(request.timezone)
    now = datetime.now(tz)
    cron = croniter(request.cron_expression, now)
    next_run = cron.get_next(datetime)

    schedule = ScheduleCreate(
        project_id=project_id,
        name=request.name,
        description=request.description,
        cron_expression=request.cron_expression,
        timezone=request.timezone,
        target_type=request.target_type,
        target_test_case_ids=json.dumps(request.target_test_case_ids) if request.target_test_case_ids else None,
        target_tags=json.dumps(request.target_tags) if request.target_tags else None,
        browser=request.browser,
        retry_max=request.retry_max,
        retry_mode=request.retry_mode,
        enabled=request.enabled,
        notification_channel_ids=json.dumps(request.notification_channel_ids) if request.notification_channel_ids else None,
    )

    db_schedule = crud.create_schedule(session, schedule)

    # Update next_run_at
    crud.update_schedule_run_times(session, db_schedule.id, None, next_run)

    # Refresh to get updated fields
    session.refresh(db_schedule)

    # Add to scheduler if enabled
    if db_schedule.enabled:
        scheduler_service.add_schedule(db_schedule)

    logger.info(f"Created schedule: id={db_schedule.id}, name={db_schedule.name}, next_run={next_run}")
    return db_schedule


@router.get("/{schedule_id}", response_model=ScheduleRead)
def get_schedule(
    project_id: int,
    schedule_id: int,
    session: Session = Depends(get_session_dep)
):
    """Get a schedule by ID."""
    schedule = crud.get_schedule(session, schedule_id)
    if not schedule or schedule.project_id != project_id:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return schedule


class ScheduleUpdateRequest(BaseModel):
    """Request body for updating a schedule."""
    name: Optional[str] = None
    description: Optional[str] = None
    cron_expression: Optional[str] = None
    timezone: Optional[str] = None
    target_type: Optional[str] = None
    target_test_case_ids: Optional[List[int]] = None
    target_tags: Optional[List[str]] = None
    browser: Optional[str] = None
    retry_max: Optional[int] = None
    retry_mode: Optional[str] = None
    enabled: Optional[bool] = None
    notification_channel_ids: Optional[List[int]] = None

    @field_validator("cron_expression")
    @classmethod
    def validate_cron(cls, v):
        if v is not None:
            return validate_cron_expression(v)
        return v

    @field_validator("timezone")
    @classmethod
    def validate_tz(cls, v):
        if v is not None:
            return validate_timezone(v)
        return v


@router.put("/{schedule_id}", response_model=ScheduleRead)
def update_schedule(
    project_id: int,
    schedule_id: int,
    request: ScheduleUpdateRequest,
    session: Session = Depends(get_session_dep)
):
    """Update a schedule."""
    schedule = crud.get_schedule(session, schedule_id)
    if not schedule or schedule.project_id != project_id:
        raise HTTPException(status_code=404, detail="Schedule not found")

    # Build update data
    update_data = ScheduleUpdate(
        name=request.name,
        description=request.description,
        cron_expression=request.cron_expression,
        timezone=request.timezone,
        target_type=request.target_type,
        target_test_case_ids=json.dumps(request.target_test_case_ids) if request.target_test_case_ids is not None else None,
        target_tags=json.dumps(request.target_tags) if request.target_tags is not None else None,
        browser=request.browser,
        retry_max=request.retry_max,
        retry_mode=request.retry_mode,
        enabled=request.enabled,
        notification_channel_ids=json.dumps(request.notification_channel_ids) if request.notification_channel_ids is not None else None,
    )

    updated = crud.update_schedule(session, schedule_id, update_data)
    if not updated:
        raise HTTPException(status_code=404, detail="Schedule not found")

    # Recalculate next run time if cron or timezone changed
    if request.cron_expression is not None or request.timezone is not None:
        tz = pytz.timezone(updated.timezone)
        now = datetime.now(tz)
        cron = croniter(updated.cron_expression, now)
        next_run = cron.get_next(datetime)
        crud.update_schedule_run_times(session, schedule_id, None, next_run)
        session.refresh(updated)

    # Update scheduler
    scheduler_service.update_schedule(updated)

    logger.info(f"Updated schedule: id={schedule_id}")
    return updated


@router.delete("/{schedule_id}")
def delete_schedule(
    project_id: int,
    schedule_id: int,
    session: Session = Depends(get_session_dep)
):
    """Delete a schedule."""
    schedule = crud.get_schedule(session, schedule_id)
    if not schedule or schedule.project_id != project_id:
        raise HTTPException(status_code=404, detail="Schedule not found")

    # Remove from scheduler
    scheduler_service.remove_schedule(schedule_id)

    success = crud.delete_schedule(session, schedule_id)
    if not success:
        raise HTTPException(status_code=404, detail="Schedule not found")

    logger.info(f"Deleted schedule: id={schedule_id}")
    return {"status": "deleted"}


@router.post("/{schedule_id}/run")
async def trigger_schedule_now(
    project_id: int,
    schedule_id: int,
    session: Session = Depends(get_session_dep)
):
    """Trigger a schedule to run immediately."""
    schedule = crud.get_schedule(session, schedule_id)
    if not schedule or schedule.project_id != project_id:
        raise HTTPException(status_code=404, detail="Schedule not found")

    # Execute the schedule (skip_claim=True for manual triggers to always allow)
    from scheduler.executor import execute_scheduled_run
    await execute_scheduled_run(schedule_id, skip_claim=True)

    logger.info(f"Triggered schedule: id={schedule_id}")
    return {"status": "triggered", "message": f"Schedule '{schedule.name}' execution started"}


class ScheduledRunWithSchedule(ScheduledRunRead):
    """Scheduled run with schedule name."""
    schedule_name: str


@router.get("/{schedule_id}/runs", response_model=List[ScheduledRunRead])
def get_schedule_runs(
    project_id: int,
    schedule_id: int,
    skip: int = 0,
    limit: int = 50,
    session: Session = Depends(get_session_dep)
):
    """Get run history for a schedule."""
    schedule = crud.get_schedule(session, schedule_id)
    if not schedule or schedule.project_id != project_id:
        raise HTTPException(status_code=404, detail="Schedule not found")

    return crud.get_scheduled_runs_by_schedule(session, schedule_id, skip=skip, limit=limit)


# Additional route at project level for all scheduled runs
project_runs_router = APIRouter(prefix="/projects/{project_id}/scheduled-runs", tags=["scheduled-runs"])


@project_runs_router.get("", response_model=List[ScheduledRunWithSchedule])
def get_project_scheduled_runs(
    project_id: int,
    skip: int = 0,
    limit: int = 50,
    session: Session = Depends(get_session_dep)
):
    """Get all scheduled runs for a project."""
    project = crud.get_project(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    runs = crud.get_scheduled_runs_by_project(session, project_id, skip=skip, limit=limit)

    # Add schedule name to each run
    result = []
    for run in runs:
        schedule = crud.get_schedule(session, run.schedule_id)
        result.append(ScheduledRunWithSchedule(
            id=run.id,
            schedule_id=run.schedule_id,
            project_id=run.project_id,
            thread_id=run.thread_id,
            status=run.status,
            started_at=run.started_at,
            completed_at=run.completed_at,
            test_count=run.test_count,
            pass_count=run.pass_count,
            fail_count=run.fail_count,
            notifications_sent=run.notifications_sent,
            notification_errors=run.notification_errors,
            created_at=run.created_at,
            schedule_name=schedule.name if schedule else "Unknown",
        ))

    return result
