# Codesigner ⌘

自分専用AIコーディングエージェント。Gemma 4 31B × Google AI Studio無料枠で動作。

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

- **Gemma 4 31B** (`gemma-4-31b-it`) — Google AI Studio無料枠
- RPD: 1,500/キー × 最大4キー = **6,000リクエスト/日**
- TPM: 無制限
