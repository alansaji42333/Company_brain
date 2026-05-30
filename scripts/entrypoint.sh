#!/bin/bash
# Docker entrypoint: validate config, apply pending migrations, start the server.
set -euo pipefail

echo "=== Validating environment ==="
for var in DATABASE_URL OLLAMA_BASE_URL OLLAMA_API_KEY LLM_MODEL; do
    val=$(printenv "$var" 2>/dev/null || true)
    if [ -z "$val" ]; then
        echo "ERROR: $var is not set"
        exit 1
    fi
done

echo "=== Running database migrations ==="
alembic upgrade head

PORT=${PORT:-8000}
echo "=== Starting uvicorn on port $PORT ==="
exec uvicorn app.server:app --host 0.0.0.0 --port "$PORT" --timeout-graceful-shutdown 30