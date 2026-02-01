-- Migration: Add scheduled runs support
-- Date: 2026-02-01
-- Description: Creates tables for notification channels, schedules, and scheduled run history

-- Notification channels (reusable across schedules)
CREATE TABLE IF NOT EXISTS notificationchannel (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name VARCHAR NOT NULL,
    channel_type VARCHAR NOT NULL DEFAULT 'webhook',
    enabled BOOLEAN DEFAULT TRUE,
    webhook_url VARCHAR,
    webhook_template VARCHAR,
    email_recipients VARCHAR,
    email_template VARCHAR,
    notify_on VARCHAR DEFAULT 'failure',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_notificationchannel_project_id ON notificationchannel(project_id);

-- Schedules
CREATE TABLE IF NOT EXISTS schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name VARCHAR NOT NULL,
    description VARCHAR,
    cron_expression VARCHAR NOT NULL,
    timezone VARCHAR NOT NULL DEFAULT 'UTC',
    target_type VARCHAR NOT NULL DEFAULT 'test_case_ids',
    target_test_case_ids VARCHAR,
    target_tags VARCHAR,
    browser VARCHAR,
    retry_max INTEGER DEFAULT 0,
    retry_mode VARCHAR,
    enabled BOOLEAN DEFAULT TRUE,
    notification_channel_ids VARCHAR,
    last_run_at TIMESTAMP,
    next_run_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_schedule_project_id ON schedule(project_id);
CREATE INDEX IF NOT EXISTS ix_schedule_enabled ON schedule(enabled);

-- Scheduled run history
CREATE TABLE IF NOT EXISTS scheduledrun (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL REFERENCES schedule(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES project(id),
    thread_id VARCHAR NOT NULL,
    status VARCHAR DEFAULT 'pending',
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    test_count INTEGER DEFAULT 0,
    pass_count INTEGER DEFAULT 0,
    fail_count INTEGER DEFAULT 0,
    notifications_sent VARCHAR,
    notification_errors VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_scheduledrun_schedule_id ON scheduledrun(schedule_id);
CREATE INDEX IF NOT EXISTS ix_scheduledrun_project_id ON scheduledrun(project_id);
