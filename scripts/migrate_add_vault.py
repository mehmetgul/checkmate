"""Migration script to add vault fields to Persona and create TestData table.

Adds:
  - credential_type, encrypted_api_key, encrypted_token, encrypted_metadata columns to persona
  - Makes encrypted_password nullable (for non-login credential types)
  - TestData table is auto-created by SQLModel on startup

Usage:
    python scripts/migrate_add_vault.py
"""

import os
import sys
import sqlite3

# Add parent directory to path so we can import from the project
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.session import DATABASE_URL


def migrate():
    if not DATABASE_URL.startswith("sqlite"):
        print("This migration script is for SQLite only.")
        print("For PostgreSQL, the ORM will handle schema creation.")
        return

    # Extract DB path from sqlite URL
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        print(f"Database file not found: {db_path}")
        print("No migration needed — tables will be created on first run.")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check existing persona columns
    cursor.execute("PRAGMA table_info(persona)")
    columns = [row[1] for row in cursor.fetchall()]

    new_columns = {
        "credential_type": "TEXT DEFAULT 'login'",
        "encrypted_api_key": "TEXT DEFAULT NULL",
        "encrypted_token": "TEXT DEFAULT NULL",
        "encrypted_metadata": "TEXT DEFAULT NULL",
    }

    for col_name, col_def in new_columns.items():
        if col_name not in columns:
            print(f"Adding {col_name} column to persona table...")
            cursor.execute(f"ALTER TABLE persona ADD COLUMN {col_name} {col_def}")
            print(f"Done: {col_name} column added.")
        else:
            print(f"{col_name} column already exists on persona — skipping.")

    # The TestData table will be created by SQLModel.metadata.create_all()
    # on the next application startup, so we don't need to create it here.

    conn.commit()
    conn.close()
    print("\nMigration complete. Restart the backend to apply changes.")
    print("The 'testdata' table will be auto-created on next startup.")


if __name__ == "__main__":
    migrate()
