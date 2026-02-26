-- Migration 006: Add environments feature
-- Date: 2026-02-26
-- Description: Creates environment table for per-project env config
-- Previously: scripts/migrate_add_environments.py

CREATE TABLE IF NOT EXISTS environment (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id),
    name VARCHAR NOT NULL,
    base_url VARCHAR NOT NULL,
    variables VARCHAR NOT NULL DEFAULT '{}',
    is_default BOOLEAN NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_environment_project_id ON environment(project_id);
CREATE INDEX IF NOT EXISTS ix_environment_name ON environment(name);
