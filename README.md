# Codesigner ⌘

自分専用AIコーディングエージェント。**Claude Code CLI + LiteLLM + Gemini**で動作。

## アーキテクチャ

```
ブラウザ（React UI）
    ↓ WebSocket
Python FastAPI（ブリッジ）
    ↓ subprocess NDJSON
Claude Code CLI
    ↓ ANTHROPIC_BASE_URL
LiteLLM proxy（localhost:4000）
    ↓ キーローテーション
Gemini 3.1 Flash-Lite（最大4キー）
```

## 機能

- 💬 **チャット管理** — 複数チャット、履歴はSQLiteに永続保存
- 🗂️ **セッションサンドボックス** — チャットごとに独立した作業フォルダ
- 📝 **ファイル編集** — Claude Code組み込みツール（Read/Write/Edit/Bash/Glob/Grep）
- ⚡ **コマンド実行** — Bash経由で自動実行
- 🔑 **APIキーローテーション** — LiteLLMで最大4キーをローテーション
- 🔄 **自動コンテキスト管理** — Claude Code側で自動コンパクション

## セットアップ（Oracle VM）

```bash
# Node.js（v18+）が必要
npm install -g @anthropic-ai/claude-code

# Python依存
cd backend
pip install -r requirements.txt

# 環境変数設定
cp .env.example .env
# .envにGEMINI_API_KEY_1〜4を設定

# 起動（LiteLLM + FastAPI）
./start.sh
```

## 環境変数（.env）

```
GEMINI_API_KEY_1=your_key_1
GEMINI_API_KEY_2=your_key_2
GEMINI_API_KEY_3=your_key_3
GEMINI_API_KEY_4=your_key_4
LITELLM_BASE_URL=http://localhost:4000
WORKSPACE=/workspace
```

APIキーは https://aistudio.google.com/apikey で取得。

## モデル変更

`backend/litellm_config.yaml` の `model:` を変更するだけ。例：
- `gemini/gemini-3.1-flash-lite`（デフォルト、RPD 500/キー）
- `gemini/gemini-3.5-flash`（高性能、RPD 20/キー）
- `gemini/gemma-4-27b-it`（RPD 1500/キー、無料枠最大）
