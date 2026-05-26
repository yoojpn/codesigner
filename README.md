# Codesigner ⌘

自分専用AIコーディングエージェント。Gemini 3.1 Flash-Lite × Google AI Studio無料枠で動作。

## 構成

```
codesigner.site        → Cloudflare Pages（WebUI）
api.codesigner.site    → Oracle Cloud VM（バックエンド）
```

## 機能

- 💬 **チャット管理** — ChatGPTライクな複数チャット、履歴はSQLiteに永続保存
- 🗂️ **セッションサンドボックス** — チャットごとに独立した作業フォルダ（削除すると自動消去）
- 📝 **ファイル編集** — write_file / apply_diff（unified diff）
- ⚡ **コマンド実行** — sudo/外部アクセス時のみ承認、それ以外は自動実行
- 🌐 **Web検索 + URL取得** — ドキュメント参照に活用
- ⬇️ **ファイルダウンロード** — copy_to_outputでUIからDL
- 🔑 **APIキーローテーション** — 最大4キーで無料枠を最大活用
- 🧠 **Thinkingモード** — `/thinking on/off/auto` で切り替え（ThinkingConfig対応）

## セットアップ（Oracle VM）

```bash
# 依存インストール
cd backend
pip install -r requirements.txt

# 環境変数設定
cp .env.example .env
# .envを編集してGEMMA_API_KEYSに最大4つのキーをカンマ区切りで設定

# 起動
./start.sh
```

## デプロイ

### Oracle VM（バックエンド）

```bash
# nginx設定
sudo apt install nginx certbot python3-certbot-nginx
sudo certbot --nginx -d api.codesigner.site

# systemdサービス登録
sudo cp codesigner.service /etc/systemd/system/
sudo systemctl enable --now codesigner
```

### Cloudflare Pages（フロントエンド）

| 設定項目 | 値 |
|---|---|
| Root directory | `frontend` |
| Build command | `npm run build` |
| Build output | `dist` |
| 環境変数 | `VITE_BACKEND_URL=https://api.codesigner.site` |

## 承認フロー

| 操作 | 承認 |
|---|---|
| ファイル読み書き（セッション内） | 自動 |
| コマンド実行（通常） | 自動 |
| sudo / su / pkexec | **要承認** |
| セッション外へのファイルアクセス | **要承認** |

## モデル

現在: **Gemini 3.1 Flash-Lite** (`gemini-3.1-flash-lite`)
- RPD: 500/キー × 最大4キー = **2,000リクエスト/日**
- ThinkingConfig対応（`thinking_budget` で制御）
- メタコメント漏れなし

## ベンチマーク比較: Gemini 3.1 Flash-Lite vs Gemma 4 31B

コーディングエージェント用途での比較。

### 性能

| ベンチマーク | Gemini 3.1 Flash-Lite | Gemma 4 31B |
|---|---|---|
| HumanEval (コーディング) | **~75%** | ~70% |
| MBPP (Python問題) | **~72%** | ~65% |
| MATH | ~60% | **~65%** |
| MMLU | ~72% | **~76%** |
| 推論全般 | ◯ | ◎ |

### 実用性（コーディングエージェント用途）

| 項目 | Gemini 3.1 Flash-Lite | Gemma 4 31B |
|---|---|---|
| メタコメント漏れ | ✅ **なし** | ❌ 頻発 |
| Thinking制御 | ✅ **ThinkingConfig対応** | ❌ API非対応 |
| レスポンス速度 | ✅ **2.5倍速い** | 普通 |
| 無料枠 RPD/キー | 500 | 1,500 |
| 4キー合計 RPD | **2,000/日** | 6,000/日 |
| ツール呼び出し精度 | ✅ 高い | △ やや不安定 |
| 日本語応答 | ✅ 安定 | ❌ 英語混入あり |

### 結論

**コーディングエージェントとしての実用性はGemini 3.1 Flash-Liteが大幅に上回る。**

Gemma 4 31Bはベンチマーク上の推論性能は高いが、APIを通すとthinking内容がレスポンスに漏出する問題が制御不能で、コーディングエージェントとして使い続けるのが困難。Gemini 3.1 Flash-LiteはThinkingConfigで完全制御でき、速度・安定性ともに優れている。RPDは1/3になるが4キーで2,000/日あれば個人・小チーム用途には十分。
