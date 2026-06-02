#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env — please fill in GEMINI_API_KEY_1..4 before continuing"
  exit 1
fi

# Python依存パッケージ
pip install -r requirements.txt -q

# Claude Code CLI（未インストールの場合）
if ! command -v claude &> /dev/null; then
  echo "[setup] Installing @anthropic-ai/claude-code..."
  npm install -g @anthropic-ai/claude-code
fi

mkdir -p /workspace/output

# LiteLLMプロキシをバックグラウンドで起動
if ! lsof -i:4000 -t &>/dev/null; then
  echo "[setup] Starting LiteLLM proxy on port 4000..."
  source .env 2>/dev/null || true
  litellm --config litellm_config.yaml --port 4000 &
  LITELLM_PID=$!
  echo "[setup] LiteLLM PID=$LITELLM_PID"
  sleep 3
fi

# FastAPIサーバー起動
uvicorn main:app --host 0.0.0.0 --port 8000
