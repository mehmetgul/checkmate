-- Migration 007: Add vault feature (credential types + test data)
-- Date: 2026-02-26
-- Description: Extends persona with credential types, adds testdata table
-- Previously: scripts/migrate_add_vault.py

-- Extend persona table with multi-credential fields
ALTER TABLE persona ADD COLUMN credential_type VARCHAR NOT NULL DEFAULT 'login';
ALTER TABLE persona ADD COLUMN environment_id INTEGER;
ALTER TABLE persona ADD COLUMN encrypted_api_key VARCHAR;
ALTER TABLE persona ADD COLUMN encrypted_token VARCHAR;
ALTER TABLE persona ADD COLUMN encrypted_metadata VARCHAR;

-- TestData table
CREATE TABLE IF NOT EXISTS testdata (
    name VARCHAR NOT NULL,
    description VARCHAR,
    data VARCHAR NOT NULL,
    tags VARCHAR,
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id),
    environment_id INTEGER,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_testdata_project_id ON testdata(project_id);
CREATE INDEX IF NOT EXISTS ix_testdata_name ON testdata(name);
