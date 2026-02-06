-- Migration: Update fixture state schema for Playwright storage_state
-- Date: 2026-02-06
-- Description: Replace separate encrypted fields with url and encrypted_state_json

-- SQLite version: Need to recreate table (no ALTER COLUMN support)

-- Step 1: Create new table with updated schema
CREATE TABLE IF NOT EXISTS fixturestate_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id INTEGER NOT NULL REFERENCES fixture(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    url VARCHAR,
    encrypted_state_json VARCHAR,
    browser VARCHAR,
    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP
);

-- Step 2: Copy existing data (if any) - leaving url and encrypted_state_json NULL
-- Old cached states will be invalidated and need to be regenerated
INSERT INTO fixturestate_new (id, fixture_id, project_id, browser, captured_at, expires_at)
SELECT id, fixture_id, project_id, browser, captured_at, expires_at
FROM fixturestate;

-- Step 3: Drop old table
DROP TABLE fixturestate;

-- Step 4: Rename new table
ALTER TABLE fixturestate_new RENAME TO fixturestate;

-- Step 5: Recreate indexes
CREATE INDEX IF NOT EXISTS ix_fixturestate_fixture_id ON fixturestate(fixture_id);
CREATE INDEX IF NOT EXISTS ix_fixturestate_project_id ON fixturestate(project_id);
