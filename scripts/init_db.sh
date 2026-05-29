#!/bin/bash
# One-time database initialization script.
# Run this once after cloning the repo to set up Alembic migrations.
#
# Usage:  ./scripts/init_db.sh
#
# Prerequisites:
#   - PostgreSQL 15 running with database "company_brain" created
#   - DATABASE_URL set in .env or exported in the environment
#   - Python dependencies installed (pip install -r requirements.txt)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

if [ ! -f alembic.ini ]; then
    echo "ERROR: alembic.ini not found. Are you in the project root?"
    exit 1
fi

echo "=== Generating initial migration ==="
alembic revision --autogenerate -m "Create conversations and messages tables"

echo "=== Applying migrations ==="
alembic upgrade head

echo "=== Database initialized ==="
