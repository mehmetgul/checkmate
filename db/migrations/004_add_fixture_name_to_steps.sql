-- Migration 004: Add fixture_name to TestRunStep
-- Tracks which fixture a step belongs to for visual distinction in UI

ALTER TABLE testrunstep ADD COLUMN fixture_name VARCHAR;
