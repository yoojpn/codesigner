#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env — please fill in GEMMA_API_KEYS before continuing"
  exit 1
fi

pip install -r requirements.txt -q
mkdir -p /workspace/output
uvicorn main:app --host 0.0.0.0 --port 8000
