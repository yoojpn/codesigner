#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env — please fill in OPENROUTER_API_KEY before continuing"
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

# FastAPIサーバー起動
uvicorn main:app --host 0.0.0.0 --port 8000
