#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env — please add your GEMMA_API_KEYS"
fi

if ! python3 -c "import fastapi" 2>/dev/null; then
  pip3 install -r requirements.txt
fi

WORKSPACE="${WORKSPACE:-/workspace}"
mkdir -p "$WORKSPACE" "$WORKSPACE/output"

PORT="${PORT:-8000}"
echo "Starting Codesigner backend on port $PORT..."
uvicorn main:app --host 0.0.0.0 --port "$PORT" --reload
