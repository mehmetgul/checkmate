"""Scheduler service for managing scheduled test runs."""

import asyncio
from datetime import datetime
from typing import Optional

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from croniter import croniter

from core.logging import get_logger
from db.session import get_session
from db import crud

logger = get_logger(__name__)


class SchedulerService:
    """Service for managing scheduled test runs."""

    def __init__(self):
        self._scheduler: Optional[AsyncIOScheduler] = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self):
        """Start the scheduler and load all enabled schedules."""
        if self._running:
            logger.warning("Scheduler already running")
            return

        logger.info("Starting scheduler service")
        self._scheduler = AsyncIOScheduler()
        self._scheduler.start()
        self._running = True

        # Load all enabled schedules from database
        await self.reload_all_schedules()
        logger.info("Scheduler service started")

    async def stop(self):
        """Stop the scheduler."""
        if not self._running:
            return

        logger.info("Stopping scheduler service")
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        self._running = False
        logger.info("Scheduler service stopped")

    async def reload_all_schedules(self):
        """Load all enabled schedules from the database."""
        if not self._scheduler:
            logger.warning("Scheduler not initialized")
            return

        logger.info("Reloading all schedules from database")

        # Remove all existing jobs
        self._scheduler.remove_all_jobs()

        with get_session() as session:
            schedules = crud.get_all_enabled_schedules(session)
            logger.info(f"Found {len(schedules)} enabled schedules")

            for schedule in schedules:
                self._add_schedule_job(schedule)

    def _add_schedule_job(self, schedule):
        """Add a schedule job to the scheduler."""
        if not self._scheduler:
            return

        job_id = f"schedule_{schedule.id}"

        try:
            # Parse cron expression
            trigger = CronTrigger.from_crontab(
                schedule.cron_expression,
                timezone=pytz.timezone(schedule.timezone)
            )

            # Add the job
            self._scheduler.add_job(
                self._execute_schedule,
                trigger=trigger,
                id=job_id,
                args=[schedule.id],
                replace_existing=True,
                name=f"Schedule: {schedule.name}",
            )

            # Calculate and update next run time
            tz = pytz.timezone(schedule.timezone)
            now = datetime.now(tz)
            cron = croniter(schedule.cron_expression, now)
            next_run = cron.get_next(datetime)

            with get_session() as session:
                crud.update_schedule_run_times(session, schedule.id, None, next_run)

            logger.info(f"Added schedule job: {job_id} ({schedule.name}), next run: {next_run}")

        except Exception as e:
            logger.error(f"Failed to add schedule job {job_id}: {e}")

    async def _execute_schedule(self, schedule_id: int):
        """Execute a scheduled run."""
        from scheduler.executor import execute_scheduled_run
        await execute_scheduled_run(schedule_id)

    def add_schedule(self, schedule):
        """Add or update a schedule job."""
        if not self._scheduler or not schedule.enabled:
            return

        self._add_schedule_job(schedule)

    def remove_schedule(self, schedule_id: int):
        """Remove a schedule job."""
        if not self._scheduler:
            return

        job_id = f"schedule_{schedule_id}"
        try:
            self._scheduler.remove_job(job_id)
            logger.info(f"Removed schedule job: {job_id}")
        except Exception as e:
            logger.debug(f"Job {job_id} not found or already removed: {e}")

    def update_schedule(self, schedule):
        """Update a schedule job (remove and re-add)."""
        self.remove_schedule(schedule.id)
        if schedule.enabled:
            self._add_schedule_job(schedule)

    def get_next_run_time(self, schedule_id: int) -> Optional[datetime]:
        """Get the next run time for a schedule."""
        if not self._scheduler:
            return None

        job_id = f"schedule_{schedule_id}"
        job = self._scheduler.get_job(job_id)
        return job.next_run_time if job else None


# Global scheduler service instance
scheduler_service = SchedulerService()
