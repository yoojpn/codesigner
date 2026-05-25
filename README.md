# Codesigner

AI coding assistant powered by Gemma 4 31B (Google AI Studio), with a VS Code-inspired web UI.

## Features
- 💬 Chat with an AI that reads/writes/edits files, runs commands, and searches the web
- 📝 Monaco Editor (VS Code engine) for viewing and editing files
- 🔄 Unified diff display with syntax highlighting
- ✅ Command approval workflow — review diffs and commands before execution
- 🌐 Web search + URL fetch built-in
- 📥 Output directory with one-click file download from the web UI
- 📎 File attachment — drag & drop local files as context for the AI
- 🔑 4-key API rotation for maximum free-tier usage

## Quick Start (Oracle Cloud VM)

### 1. Clone & configure backend
```bash
git clone https://github.com/yoojpn/codesigner
cd codesigner/backend
pip3 install -r requirements.txt
cp .env.example .env
# Edit .env — add comma-separated GEMMA_API_KEYS
```

### 2. Start backend
```bash
./start.sh
# or: uvicorn main:app --host 0.0.0.0 --port 8000
```

### 3. Build frontend (Cloudflare Pages)
```bash
cd frontend
npm install
npm run build
# Upload dist/ to Cloudflare Pages
# Set environment variable: VITE_WS_URL=wss://your-domain.com
```

### 4. nginx (HTTPS + WebSocket proxy)
```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;
    # ssl via: certbot --nginx -d your-domain.com

    location /ws {
        proxy_pass http://localhost:8000/ws;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
    }
    location /api {
        proxy_pass http://localhost:8000;
    }
    location / {
        proxy_pass http://localhost:8000;
    }
}
```

## Environment Variables
| Variable | Default | Description |
|---|---|---|
| `GEMMA_API_KEYS` | — | Comma-separated Google AI Studio API keys |
| `WORKSPACE` | `/workspace` | Path to workspace directory |
| `PORT` | `8000` | Server port |

## Downloading Files
The AI can call `copy_to_output(path)` to copy any workspace file to the **Downloads** panel in the sidebar. You can also download any file directly from the file tree.

## API Rotation
Add up to 4 Google AI Studio keys to `GEMMA_API_KEYS` (comma-separated). The backend rotates keys automatically to maximize free-tier throughput (Gemma 4 31B: 1,500 RPD × 4 keys = 6,000 req/day).
