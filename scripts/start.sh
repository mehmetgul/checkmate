#!/usr/bin/env bash
# Start the Checkmate backend server.
#
# Usage:
#   ./scripts/start.sh                    # Start server (auto-migrates if DB exists)
#   ./scripts/start.sh --install          # Install dependencies first
#   ./scripts/start.sh --migrate-only     # Run migrations without starting server
#
# Environment variables:
#   DATABASE_URL    Database connection string (default: sqlite:///./qa_testing.db)
#   HOST            Server host (default: 0.0.0.0)
#   PORT            Server port (default: 8000)

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

log() { echo -e "${1}${2}${NC}"; }

check_prerequisites() {
  if ! command -v uv &>/dev/null; then
    log "$RED" "Error: uv not found. Install: https://docs.astral.sh/uv/"
    exit 1
  fi
}

install_deps() {
  log "$BLUE" "Installing dependencies..."
  (cd "$ROOT_DIR" && uv sync)
  log "$GREEN" "Dependencies installed."
  echo ""
}

run_migrations() {
  local db_file
  db_file=$(cd "$ROOT_DIR" && python3 -c "
import os
url = os.getenv('DATABASE_URL', 'sqlite:///./qa_testing.db')
if url.startswith('sqlite:///'):
    print(url.replace('sqlite:///', ''))
" 2>/dev/null || true)

  if [ -n "$db_file" ] && [ -f "$ROOT_DIR/$db_file" ]; then
    log "$YELLOW" "Existing database found. Running migrations..."
    (cd "$ROOT_DIR" && uv run python db/migrations/migrate.py)
    echo ""
  elif [ -n "$db_file" ]; then
    log "$BLUE" "No database found. A fresh one will be created on first start."
    echo ""
  fi
}

setup_env() {
  if [ ! -f "$ROOT_DIR/.env" ]; then
    if [ -f "$ROOT_DIR/.env.example" ]; then
      log "$YELLOW" "No .env found — copying from .env.example"
      cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
      log "$YELLOW" "Edit .env to add your OPENAI_API_KEY and ENCRYPTION_KEY"
      echo ""
    fi
  fi
}

start_server() {
  log "$BOLD" "♛  Checkmate Backend"
  echo ""
  log "$GREEN" "Starting on http://$HOST:$PORT"
  echo ""
  cd "$ROOT_DIR" && exec uv run python -m uvicorn api.main:app \
    --host "$HOST" \
    --port "$PORT" \
    --reload
}

# ── Main ──────────────────────────────────────

check_prerequisites

case "${1:-}" in
  --install)
    install_deps
    setup_env
    run_migrations
    start_server
    ;;
  --migrate-only)
    run_migrations
    ;;
  *)
    setup_env
    run_migrations
    start_server
    ;;
esac
