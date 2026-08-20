#!/bin/bash
# Docker entrypoint: validate config, apply migrations, start the server.
# Uses Gunicorn with Uvicorn workers for production (multiple workers).
# Falls back to single uvicorn if Gunicorn is unavailable.
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
WORKERS=${WEB_CONCURRENCY:-4}
TIMEOUT=${WORKER_TIMEOUT:-120}

echo "=== Starting server on port $PORT with $WORKERS worker(s) ==="
if command -v gunicorn >/dev/null 2>&1; then
    exec gunicorn app.server:app \
        --workers "$WORKERS" \
        --worker-class uvicorn.workers.UvicornWorker \
        --bind 0.0.0.0:"$PORT" \
        --timeout "$TIMEOUT" \
        --graceful-timeout 30
else
    exec uvicorn app.server:app --host 0.0.0.0 --port "$PORT" --timeout-graceful-shutdown 30
fi