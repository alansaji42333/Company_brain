#!/bin/bash
# Start the ARQ background worker (ingestion + synthesis jobs).
# Run as a separate process/container from the web server.
set -euo pipefail

for var in DATABASE_URL OLLAMA_BASE_URL OLLAMA_API_KEY LLM_MODEL REDIS_URL; do
    val=$(printenv "$var" 2>/dev/null || true)
    if [ -z "$val" ]; then
        echo "ERROR: $var is not set"
        exit 1
    fi
done

echo "=== Starting ARQ worker ==="
exec arq app.worker.WorkerSettings