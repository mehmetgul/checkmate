-- Migration 004: Add fixture_name to TestRunStep (PostgreSQL version)
-- Tracks which fixture a step belongs to for visual distinction in UI

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'testrunstep' AND column_name = 'fixture_name'
    ) THEN
        ALTER TABLE testrunstep ADD COLUMN fixture_name VARCHAR;
    END IF;
END $$;
