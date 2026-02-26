"""Migration: add environment table for multi-environment support."""

import sqlite3
import os

DB_PATH = os.getenv("SQLITE_DB_PATH", "qa_agent.db")


def migrate(db_path: str = DB_PATH):
    print(f"Running migration on: {db_path}")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS environment (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  INTEGER NOT NULL REFERENCES project(id),
            name        TEXT NOT NULL,
            base_url    TEXT NOT NULL,
            variables   TEXT NOT NULL DEFAULT '{}',
            is_default  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS ix_environment_project_id ON environment (project_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_environment_name ON environment (name)")

    conn.commit()
    conn.close()
    print("Migration complete: environment table ready.")


if __name__ == "__main__":
    migrate()
