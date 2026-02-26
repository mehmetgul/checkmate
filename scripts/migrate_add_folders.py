"""Migration script to add TestFolder table and folder_id column to TestCase.

Run this script if you have an existing SQLite database that was created
before the folders feature was added. SQLModel's create_all() will create
the new TestFolder table but won't add the folder_id column to the existing
testcase table.

Usage:
    python scripts/migrate_add_folders.py
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

    # Check if folder_id column already exists on testcase
    cursor.execute("PRAGMA table_info(testcase)")
    columns = [row[1] for row in cursor.fetchall()]

    if "folder_id" not in columns:
        print("Adding folder_id column to testcase table...")
        cursor.execute("ALTER TABLE testcase ADD COLUMN folder_id INTEGER DEFAULT NULL")
        print("Done: folder_id column added.")
    else:
        print("folder_id column already exists on testcase — skipping.")

    # The TestFolder table will be created by SQLModel.metadata.create_all()
    # on the next application startup, so we don't need to create it here.

    # Update existing smart folders to remove restrictive status filter
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='testfolder'")
    if cursor.fetchone():
        import json
        cursor.execute(
            "SELECT id, smart_criteria FROM testfolder WHERE folder_type = 'smart' AND smart_criteria IS NOT NULL"
        )
        for row in cursor.fetchall():
            folder_id, criteria_json = row
            try:
                criteria = json.loads(criteria_json)
                if criteria.get("statuses"):
                    criteria["statuses"] = []
                    cursor.execute(
                        "UPDATE testfolder SET smart_criteria = ? WHERE id = ?",
                        (json.dumps(criteria), folder_id),
                    )
                    print(f"Updated smart folder {folder_id}: removed status filter.")
            except (json.JSONDecodeError, TypeError):
                pass

    conn.commit()
    conn.close()
    print("Migration complete. Restart the backend to apply changes.")


if __name__ == "__main__":
    migrate()
