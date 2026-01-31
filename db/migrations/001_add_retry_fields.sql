-- Migration: Add retry tracking fields to testrun table
-- Date: 2026-01-31
-- Description: Adds columns for test-level retry support

-- Check if columns exist before adding (SQLite compatible)
-- For PostgreSQL, you can use: ALTER TABLE ... ADD COLUMN IF NOT EXISTS

-- Add retry_attempt column (0 = original run, 1+ = retry attempts)
ALTER TABLE testrun ADD COLUMN retry_attempt INTEGER DEFAULT 0;

-- Add max_retries column (configured max retries for this run)
ALTER TABLE testrun ADD COLUMN max_retries INTEGER DEFAULT 0;

-- Add original_run_id column (links retry runs to their original)
ALTER TABLE testrun ADD COLUMN original_run_id INTEGER REFERENCES testrun(id);

-- Add retry_mode column ('simple' or 'intelligent')
ALTER TABLE testrun ADD COLUMN retry_mode VARCHAR;

-- Add retry_reason column (reason for retry from classifier)
ALTER TABLE testrun ADD COLUMN retry_reason VARCHAR;

-- Add index for efficient retry group lookups
CREATE INDEX IF NOT EXISTS ix_testrun_original_run_id ON testrun(original_run_id);
