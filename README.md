# Codesigner

An AI coding agent powered by Gemma 4 31B (via Google AI Studio API), with a VSCode-inspired web UI.

## Features
- 💬 Chat with an AI that can read/write/edit files, run commands, and search the web
- 📝 Monaco Editor (VSCode engine) for viewing/editing files
- 🔄 Unified diff display with syntax highlighting
- ✅ Command approval workflow (allow/reject before execution)
- 📥 File download from workspace
- 🔑 4-key API rotation for maximum free-tier usage

## Setup (Oracle Cloud VM)

### Backend
```bash
cd backend
pip install -r requirements.txt
cp .env.example .env
# Edit .env and add your Gemma API keys
WORKSPACE=/workspace uvicorn main:app --host 0.0.0.0 --port 8000
```

### Frontend (Cloudflare Pages)
The `frontend/` folder is deployed to Cloudflare Pages.
```bash
cd frontend
npm install
npm run build
# Deploy dist/ to Cloudflare Pages
```

### nginx config (HTTPS proxy)
```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;
    # ssl certs via certbot...

    location /ws {
        proxy_pass http://localhost:8000/ws;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
    location /api {
        proxy_pass http://localhost:8000;
    }
    location / {
        # Serve Cloudflare Pages or frontend/dist
        proxy_pass http://localhost:8000;
    }
}
```

## Environment Variables
| Variable | Description |
|---|---|
| `GEMMA_API_KEYS` | Comma-separated Google AI Studio API keys |
| `WORKSPACE` | Path to workspace directory (default: `/workspace`) |
| `PORT` | Server port (default: 8000) |
