#!/usr/bin/env bash
# Run database migrations for Checkmate.
#
# For existing databases only — new databases are auto-created on first server start.
#
# Usage:
#   ./scripts/migrate.sh                  # SQLite (default)
#   ./scripts/migrate.sh --postgres       # PostgreSQL (runs .sql files via psql)
#
# Environment variables:
#   DATABASE_URL    SQLite connection string (default: sqlite:///./qa_testing.db)
#   PGDATABASE      PostgreSQL database name (for --postgres mode)
#   PGHOST          PostgreSQL host (default: localhost)
#   PGUSER          PostgreSQL user

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MIGRATIONS_DIR="$ROOT_DIR/db/migrations"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

if [[ "${1:-}" == "--postgres" ]]; then
  echo -e "${GREEN}Running PostgreSQL migrations...${NC}"
  echo ""

  if ! command -v psql &>/dev/null; then
    echo -e "${RED}Error: psql not found. Install PostgreSQL client tools.${NC}"
    exit 1
  fi

  PGDATABASE="${PGDATABASE:-checkmate}"
  echo "Database: $PGDATABASE"
  echo ""

  for f in "$MIGRATIONS_DIR"/*_postgres.sql; do
    [ -f "$f" ] || continue
    echo "  Applying $(basename "$f")..."
    psql -d "$PGDATABASE" -f "$f" 2>&1 | sed 's/^/    /'
  done

  # Also run non-postgres-specific migrations that don't have a _postgres variant
  for f in "$MIGRATIONS_DIR"/*.sql; do
    [ -f "$f" ] || continue
    basename="$(basename "$f")"
    [[ "$basename" == *_postgres.sql ]] && continue
    prefix="${basename%%_*}"
    if [ -f "$MIGRATIONS_DIR/${prefix}_"*"_postgres.sql" ] 2>/dev/null; then
      continue  # Has a postgres-specific version, skip
    fi
    echo "  Applying $basename..."
    psql -d "$PGDATABASE" -f "$f" 2>&1 | sed 's/^/    /'
  done

  echo ""
  echo -e "${GREEN}Done.${NC}"
else
  echo -e "${GREEN}Running SQLite migrations...${NC}"
  echo ""
  (cd "$ROOT_DIR" && uv run python db/migrations/migrate.py)
fi
