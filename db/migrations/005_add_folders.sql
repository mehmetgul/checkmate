-- Migration 005: Add folders feature
-- Date: 2026-02-26
-- Description: Creates testfolder table and adds folder_id to testcase
-- Previously: scripts/migrate_add_folders.py

-- TestFolder table (SQLModel.create_all() may have already created this)
CREATE TABLE IF NOT EXISTS testfolder (
    name VARCHAR NOT NULL,
    description VARCHAR,
    folder_type VARCHAR NOT NULL DEFAULT 'regular',
    smart_criteria VARCHAR,
    order_index INTEGER NOT NULL DEFAULT 0,
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id),
    parent_id INTEGER REFERENCES testfolder(id),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_testfolder_name ON testfolder(name);
CREATE INDEX IF NOT EXISTS ix_testfolder_project_id ON testfolder(project_id);

-- Add folder_id to testcase
ALTER TABLE testcase ADD COLUMN folder_id INTEGER REFERENCES testfolder(id);

CREATE INDEX IF NOT EXISTS ix_testcase_folder_id ON testcase(folder_id);
