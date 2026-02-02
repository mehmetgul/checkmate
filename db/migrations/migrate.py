#!/usr/bin/env python3
"""
Database migration runner for Checkmate.

Usage:
    uv run python db/migrations/migrate.py

This script runs all pending SQL migrations in order.
"""

import os
import sqlite3
from pathlib import Path

# Get database path from environment or use default
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///checkmate.db")

def get_db_path() -> str:
    """Extract database file path from DATABASE_URL."""
    if DATABASE_URL.startswith("sqlite:///"):
        return DATABASE_URL.replace("sqlite:///", "")
    elif DATABASE_URL.startswith("postgresql://"):
        raise NotImplementedError(
            "PostgreSQL migrations should be run directly:\n"
            "  psql -d your_database -f db/migrations/001_add_retry_fields.sql"
        )
    else:
        raise ValueError(f"Unsupported DATABASE_URL: {DATABASE_URL}")


def get_applied_migrations(conn: sqlite3.Connection) -> set[str]:
    """Get set of already applied migration names."""
    cursor = conn.cursor()

    # Create migrations tracking table if it doesn't exist
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            name VARCHAR PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()

    cursor.execute("SELECT name FROM _migrations")
    return {row[0] for row in cursor.fetchall()}


def run_migration(conn: sqlite3.Connection, migration_file: Path) -> bool:
    """Run a single migration file. Returns True if applied, False if skipped."""
    migration_name = migration_file.name

    # Check if already applied
    applied = get_applied_migrations(conn)
    if migration_name in applied:
        print(f"  Skipping {migration_name} (already applied)")
        return False

    print(f"  Applying {migration_name}...")

    # Read and execute migration
    sql = migration_file.read_text()
    cursor = conn.cursor()

    # Execute each statement separately (SQLite doesn't support multiple statements)
    # Commit after each statement so tables exist before indexes are created
    for statement in sql.split(";"):
        statement = statement.strip()
        if not statement:
            continue

        # Remove leading comment lines to get to the actual SQL
        lines = statement.split("\n")
        sql_lines = [line for line in lines if not line.strip().startswith("--")]
        clean_statement = "\n".join(sql_lines).strip()

        if not clean_statement:
            continue  # Skip comment-only blocks

        try:
            cursor.execute(statement)  # Execute original (with comments is fine for SQLite)
            conn.commit()  # Commit after each statement
        except sqlite3.OperationalError as e:
            # Handle "duplicate column" errors gracefully
            if "duplicate column" in str(e).lower():
                print(f"    Column already exists, skipping: {e}")
            # Handle "table already exists" errors gracefully
            elif "already exists" in str(e).lower():
                print(f"    Already exists, skipping: {e}")
            else:
                raise

    # Record migration as applied
    cursor.execute("INSERT INTO _migrations (name) VALUES (?)", (migration_name,))
    conn.commit()

    print(f"  Applied {migration_name}")
    return True


def main():
    """Run all pending migrations."""
    db_path = get_db_path()
    migrations_dir = Path(__file__).parent

    print(f"Database: {db_path}")
    print(f"Migrations directory: {migrations_dir}")
    print()

    # Check if database exists
    if not Path(db_path).exists():
        print("Database does not exist. It will be created on first server start.")
        print("No migrations needed for new databases.")
        return

    # Get all SQL migration files
    migration_files = sorted(migrations_dir.glob("*.sql"))

    if not migration_files:
        print("No migration files found.")
        return

    print(f"Found {len(migration_files)} migration(s):")

    # Connect and run migrations
    conn = sqlite3.connect(db_path)
    try:
        applied_count = 0
        for migration_file in migration_files:
            if run_migration(conn, migration_file):
                applied_count += 1

        print()
        if applied_count > 0:
            print(f"Applied {applied_count} migration(s) successfully.")
        else:
            print("No new migrations to apply.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
