"""Executor for scheduled test runs."""

import asyncio
import json
import uuid
from datetime import datetime

import pytz
from croniter import croniter

from core.logging import get_logger
from db.session import get_session
from db.models import RunStatus, RunTrigger, ScheduledRunCreate, TestRunCreate
from db import crud
from agent.executor_client import PlaywrightExecutorClient
from agent.utils.resolver import resolve_references, mask_passwords_in_steps
from scheduler.service import get_timezone

logger = get_logger(__name__)


async def execute_scheduled_run(schedule_id: int, skip_claim: bool = False):
    """Execute a scheduled run for a schedule.

    Args:
        schedule_id: ID of the schedule to execute
        skip_claim: If True, skip the distributed lock claim (used for manual triggers)
    """
    logger.info(f"Executing scheduled run for schedule_id={schedule_id}")

    with get_session() as session:
        # Try to claim execution (distributed lock) - prevents duplicate runs across pods
        if not skip_claim:
            if not crud.try_claim_schedule_execution(session, schedule_id):
                logger.info(f"Schedule {schedule_id} already claimed by another instance, skipping")
                return

        # Get the schedule
        schedule = crud.get_schedule(session, schedule_id)
        if not schedule:
            logger.error(f"Schedule {schedule_id} not found")
            return

        if not schedule.enabled:
            logger.info(f"Schedule {schedule_id} is disabled, skipping")
            return

        # Get the project
        project = crud.get_project(session, schedule.project_id)
        if not project:
            logger.error(f"Project {schedule.project_id} not found for schedule {schedule_id}")
            return

        # Resolve test case IDs based on target type
        test_case_ids = []
        if schedule.target_type == "test_case_ids":
            test_case_ids = schedule.get_target_test_case_ids()
        elif schedule.target_type == "tags":
            tags = schedule.get_target_tags()
            if tags:
                test_cases = crud.get_test_cases_by_tags(session, schedule.project_id, tags)
                test_case_ids = [tc.id for tc in test_cases]

        if not test_case_ids:
            logger.warning(f"No test cases found for schedule {schedule_id}")
            # Update last run time even if no tests
            now = datetime.utcnow()
            tz = get_timezone(schedule.timezone)
            cron = croniter(schedule.cron_expression, datetime.now(tz))
            next_run_local = cron.get_next(datetime)
            # Convert to UTC (naive) for storage
            next_run = next_run_local.astimezone(pytz.UTC).replace(tzinfo=None)
            crud.update_schedule_run_times(session, schedule_id, now, next_run)
            return

        # Create a thread ID for this batch
        thread_id = f"scheduled-{schedule_id}-{uuid.uuid4().hex[:8]}"

        # Create scheduled run record
        scheduled_run = crud.create_scheduled_run(session, ScheduledRunCreate(
            schedule_id=schedule_id,
            project_id=schedule.project_id,
            thread_id=thread_id,
            status=RunStatus.RUNNING,
            test_count=len(test_case_ids),
        ))
        crud.update_scheduled_run(session, scheduled_run.id, {"started_at": datetime.utcnow()})

        logger.info(f"Created scheduled run: run_id={scheduled_run.id}, thread_id={thread_id}, test_count={len(test_case_ids)}")

        pass_count = 0
        fail_count = 0

        # Execute each test case
        executor_client = PlaywrightExecutorClient()

        for tc_id in test_case_ids:
            test_case = crud.get_test_case(session, tc_id)
            if not test_case:
                logger.warning(f"Test case {tc_id} not found, skipping")
                continue

            logger.info(f"Running test case {tc_id}: {test_case.name}")

            # Parse steps
            try:
                steps_data = json.loads(test_case.steps) if isinstance(test_case.steps, str) else test_case.steps
            except json.JSONDecodeError:
                steps_data = []

            if not steps_data:
                logger.warning(f"Test case {tc_id} has no steps, skipping")
                continue

            # Resolve references
            resolved_steps = resolve_references(session, schedule.project_id, steps_data)
            display_steps = mask_passwords_in_steps(resolved_steps)

            # Create test run
            test_run = crud.create_test_run(session, TestRunCreate(
                project_id=schedule.project_id,
                test_case_id=tc_id,
                trigger=RunTrigger.SCHEDULED,
                status=RunStatus.RUNNING,
                thread_id=thread_id,
            ))
            crud.update_test_run(session, test_run.id, {
                "started_at": datetime.utcnow(),
                "max_retries": schedule.retry_max,
                "retry_mode": schedule.retry_mode,
            })

            # Execute test
            test_pass_count = 0
            test_error_count = 0

            try:
                execution_options = {"screenshot_on_failure": True}
                if schedule.browser:
                    execution_options["browser"] = schedule.browser

                async for event in executor_client.execute_stream(
                    base_url=project.base_url,
                    steps=resolved_steps,
                    test_id=str(tc_id),
                    options=execution_options,
                ):
                    event_type = event.get("type")

                    if event_type == "step_completed":
                        from db.models import StepStatus, TestRunStepCreate
                        step_number = event.get("step_number", 0)
                        status = event.get("status", "failed")
                        step_status = StepStatus.PASSED if status == "passed" else StepStatus.FAILED

                        step_idx = step_number - 1
                        display_step = display_steps[step_idx] if step_idx < len(display_steps) else {}

                        crud.create_test_run_step(session, TestRunStepCreate(
                            test_run_id=test_run.id,
                            test_case_id=tc_id,
                            step_number=step_number,
                            action=display_step.get("action", "unknown"),
                            target=display_step.get("target"),
                            value=display_step.get("value"),
                            status=step_status,
                            duration=event.get("duration", 0),
                            error=event.get("error"),
                            screenshot=event.get("screenshot"),
                        ))

                        if step_status == StepStatus.PASSED:
                            test_pass_count += 1
                        else:
                            test_error_count += 1

                    elif event_type == "error":
                        logger.error(f"Executor error for test case {tc_id}: {event.get('error')}")
                        test_error_count += 1
                        break

            except Exception as e:
                logger.error(f"Error executing test case {tc_id}: {e}")
                test_error_count += 1

            # Update test run
            final_status = RunStatus.PASSED if test_error_count == 0 else RunStatus.FAILED
            crud.update_test_run(session, test_run.id, {
                "status": final_status,
                "completed_at": datetime.utcnow(),
                "pass_count": test_pass_count,
                "error_count": test_error_count,
                "summary": f"Executed {test_pass_count + test_error_count} steps: {test_pass_count} passed, {test_error_count} failed",
            })

            if final_status == RunStatus.PASSED:
                pass_count += 1
            else:
                fail_count += 1

        # Update scheduled run with final results
        final_status = RunStatus.PASSED if fail_count == 0 else RunStatus.FAILED
        crud.update_scheduled_run(session, scheduled_run.id, {
            "status": final_status,
            "completed_at": datetime.utcnow(),
            "pass_count": pass_count,
            "fail_count": fail_count,
        })

        # Update schedule run times
        now = datetime.utcnow()
        tz = get_timezone(schedule.timezone)
        cron = croniter(schedule.cron_expression, datetime.now(tz))
        next_run_local = cron.get_next(datetime)
        # Convert to UTC (naive) for storage
        next_run = next_run_local.astimezone(pytz.UTC).replace(tzinfo=None)
        crud.update_schedule_run_times(session, schedule_id, now, next_run)

        logger.info(f"Scheduled run completed: run_id={scheduled_run.id}, passed={pass_count}, failed={fail_count}")

        # Send notifications
        await send_scheduled_run_notifications(session, scheduled_run.id)


async def send_scheduled_run_notifications(session, scheduled_run_id: int):
    """Send notifications for a completed scheduled run."""
    from scheduler.notifier import send_notifications

    scheduled_run = crud.get_scheduled_run(session, scheduled_run_id)
    if not scheduled_run:
        return

    schedule = crud.get_schedule(session, scheduled_run.schedule_id)
    if not schedule:
        return

    # Get notification channel IDs
    channel_ids = schedule.get_notification_channel_ids()
    if not channel_ids:
        logger.debug(f"No notification channels configured for schedule {schedule.id}")
        return

    # Get the channels
    channels = crud.get_notification_channels_by_ids(session, channel_ids)
    if not channels:
        return

    # Determine if we should notify based on status
    final_status = "passed" if scheduled_run.status == RunStatus.PASSED else "failed"

    # Filter channels by notify_on setting
    channels_to_notify = []
    for channel in channels:
        if not channel.enabled:
            continue
        if channel.notify_on == "always":
            channels_to_notify.append(channel)
        elif channel.notify_on == "failure" and final_status == "failed":
            channels_to_notify.append(channel)
        elif channel.notify_on == "success" and final_status == "passed":
            channels_to_notify.append(channel)

    if not channels_to_notify:
        logger.debug(f"No channels match notify_on criteria for run {scheduled_run_id}")
        return

    # Send notifications
    sent_ids, errors = await send_notifications(scheduled_run, schedule, channels_to_notify)

    # Update scheduled run with notification results
    update_data = {}
    if sent_ids:
        update_data["notifications_sent"] = json.dumps(sent_ids)
    if errors:
        update_data["notification_errors"] = json.dumps(errors)

    if update_data:
        crud.update_scheduled_run(session, scheduled_run_id, update_data)
