-- Migration: Add fixtures support (PostgreSQL version)
-- Date: 2026-02-02
-- Description: Creates tables for fixtures and fixture state caching

-- Fixtures (reusable setup sequences)
CREATE TABLE IF NOT EXISTS fixture (
    id SERIAL PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name VARCHAR NOT NULL,
    description VARCHAR,
    setup_steps VARCHAR NOT NULL,
    scope VARCHAR DEFAULT 'cached',
    cache_ttl_seconds INTEGER DEFAULT 3600,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_fixture_project_id ON fixture(project_id);
CREATE INDEX IF NOT EXISTS ix_fixture_name ON fixture(name);

-- Fixture state cache (encrypted browser state)
CREATE TABLE IF NOT EXISTS fixturestate (
    id SERIAL PRIMARY KEY,
    fixture_id INTEGER NOT NULL REFERENCES fixture(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    encrypted_cookies VARCHAR,
    encrypted_local_storage VARCHAR,
    encrypted_session_storage VARCHAR,
    browser VARCHAR,
    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_fixturestate_fixture_id ON fixturestate(fixture_id);
CREATE INDEX IF NOT EXISTS ix_fixturestate_project_id ON fixturestate(project_id);

-- Add fixture_ids to test cases (skip if column exists)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'testcase' AND column_name = 'fixture_ids'
    ) THEN
        ALTER TABLE testcase ADD COLUMN fixture_ids VARCHAR;
    END IF;
END $$;
