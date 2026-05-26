import os, json, asyncio, subprocess, shutil, httpx, re, uuid, sqlite3
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

WORKSPACE = Path(os.getenv("WORKSPACE", "/workspace"))
WORKSPACE.mkdir(parents=True, exist_ok=True)
DB_PATH = WORKSPACE / "codesigner.db"

# ---- DB ----
def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS chats (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT 'New Chat',
            session_dir TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
        )""")
        c.execute("PRAGMA foreign_keys = ON")
        c.commit()

init_db()

@contextmanager
def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()

def now_iso():
    return datetime.utcnow().isoformat()

# ---- API Key Rotator ----
class KeyRotator:
    def __init__(self):
        keys_raw = os.getenv("GEMMA_API_KEYS", "")
        self.keys = [k.strip() for k in keys_raw.split(",") if k.strip()]
        self.index = 0

    def next(self) -> str:
        if not self.keys:
            raise RuntimeError("No API keys configured. Set GEMMA_API_KEYS in .env")
        key = self.keys[self.index % len(self.keys)]
        self.index += 1
        return key

rotator = KeyRotator()

# ---- Session / Chat ----
def make_session_dir() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    sid = uuid.uuid4().hex[:6]
    d = WORKSPACE / "sessions" / f"{ts}_{sid}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "output").mkdir(exist_ok=True)
    return d

def create_chat(title: str = "New Chat") -> dict:
    chat_id = uuid.uuid4().hex
    session_dir = make_session_dir()
    rel = str(session_dir.relative_to(WORKSPACE))
    ts = now_iso()
    with get_db() as db:
        db.execute("INSERT INTO chats VALUES (?,?,?,?,?)", (chat_id, title, rel, ts, ts))
    return {"id": chat_id, "title": title, "session_dir": rel, "created_at": ts, "updated_at": ts, "messages": []}

def get_chat(chat_id: str) -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not row: return None
        msgs = db.execute("SELECT * FROM messages WHERE chat_id=? ORDER BY created_at", (chat_id,)).fetchall()
    chat = dict(row)
    chat["messages"] = [dict(m) for m in msgs]
    return chat

def list_chats() -> list:
    with get_db() as db:
        rows = db.execute("SELECT id,title,session_dir,created_at,updated_at FROM chats ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]

def save_message(chat_id: str, role: str, content: str):
    with get_db() as db:
        db.execute("INSERT INTO messages VALUES (?,?,?,?,?)", (uuid.uuid4().hex, chat_id, role, content, now_iso()))
        db.execute("UPDATE chats SET updated_at=? WHERE id=?", (now_iso(), chat_id))

def update_chat_title(chat_id: str, title: str):
    with get_db() as db:
        db.execute("UPDATE chats SET title=? WHERE id=?", (title, chat_id))

def delete_chat(chat_id: str):
    with get_db() as db:
        row = db.execute("SELECT session_dir FROM chats WHERE id=?", (chat_id,)).fetchone()
        if row:
            session_path = WORKSPACE / row["session_dir"]
            if session_path.exists():
                shutil.rmtree(session_path)
        db.execute("DELETE FROM chats WHERE id=?", (chat_id,))

# ---- Approval logic ----
SUDO_PATTERNS = ["sudo ", "sudo\t", "su ", "su\t", "pkexec", "doas "]

def needs_approval(tool_name: str, args: dict, session_dir: Path) -> tuple[bool, str]:
    if tool_name == "run_command":
        cmd = args.get("command", "")
        if any(p in cmd for p in SUDO_PATTERNS):
            return True, "privilege escalation (sudo/su)"
        return False, ""
    path_arg = args.get("path", "")
    if path_arg and tool_name in ("write_file", "apply_diff", "delete_file", "read_file"):
        try:
            target = (session_dir / path_arg).resolve()
            if not str(target).startswith(str(session_dir.resolve())):
                return True, f"access outside session folder: {path_arg}"
        except Exception:
            return True, "invalid path"
    return False, ""

# ---- Tools ----
def _guard(path: str, session_dir: Path, allow_outside=False):
    target = (session_dir / path).resolve()
    if not allow_outside and not str(target).startswith(str(session_dir.resolve())):
        return None, "Access denied: outside session folder"
    if not str(target).startswith(str(WORKSPACE.resolve())):
        return None, "Access denied: outside workspace"
    return target, None

def tool_list_files(path=".", *, session_dir):
    base = (session_dir / path).resolve()
    if not str(base).startswith(str(WORKSPACE.resolve())):
        return {"error": "Access denied"}
    results = []
    try:
        for p in sorted(base.rglob("*")):
            if any(part.startswith('.') or part in ('node_modules','__pycache__','.git','output') for part in p.parts):
                continue
            rel = p.relative_to(session_dir)
            results.append({"path": str(rel), "type": "dir" if p.is_dir() else "file", "size": p.stat().st_size if p.is_file() else 0})
    except Exception as e:
        return {"error": str(e)}
    return {"files": results}

def tool_read_file(path, *, session_dir):
    t, e = _guard(path, session_dir, allow_outside=True)
    if e: return {"error": e}
    if not t.exists(): return {"error": f"Not found: {path}"}
    try: return {"content": t.read_text(errors="replace"), "path": path}
    except Exception as ex: return {"error": str(ex)}

def tool_write_file(path, content, *, session_dir):
    t, e = _guard(path, session_dir)
    if e: return {"error": e}
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_text(content)
    return {"success": True, "path": path, "bytes": len(content)}

def tool_apply_diff(path, diff, *, session_dir):
    t, e = _guard(path, session_dir)
    if e: return {"error": e}
    original = t.read_text(errors="replace") if t.exists() else ""
    try:
        new_lines = list(original.splitlines(keepends=True))
        lines = diff.splitlines(keepends=True)
        i = 0; offset = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("@@"):
                m = re.search(r"-(\d+)(?:,\d+)? \+(\d+)", line)
                if m:
                    src_start = int(m.group(1)) - 1 + offset
                    i += 1; j = src_start
                    while i < len(lines) and not lines[i].startswith("@@"):
                        l = lines[i]
                        if l.startswith("-"):
                            if j < len(new_lines): del new_lines[j]; offset -= 1
                        elif l.startswith("+"):
                            new_lines.insert(j, l[1:]); j += 1; offset += 1
                        else:
                            j += 1
                        i += 1
            else:
                i += 1
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text("".join(new_lines))
        return {"success": True, "path": path}
    except Exception as ex:
        return {"error": str(ex)}

def tool_run_command(command, cwd=".", *, session_dir):
    work_dir = (session_dir / cwd).resolve()
    if not str(work_dir).startswith(str(WORKSPACE.resolve())):
        return {"error": "Access denied"}
    try:
        r = subprocess.run(command, shell=True, cwd=work_dir, capture_output=True, text=True, timeout=60)
        return {"stdout": r.stdout[-8000:], "stderr": r.stderr[-2000:], "exit_code": r.returncode, "command": command}
    except subprocess.TimeoutExpired:
        return {"error": "Timed out (60s)"}
    except Exception as ex:
        return {"error": str(ex)}

def tool_delete_file(path, *, session_dir):
    t, e = _guard(path, session_dir)
    if e: return {"error": e}
    if not t.exists(): return {"error": f"Not found: {path}"}
    shutil.rmtree(t) if t.is_dir() else t.unlink()
    return {"success": True, "path": path}

def tool_search_files(query, path=".", *, session_dir):
    base = (session_dir / path).resolve()
    return {"matches": [str(p.relative_to(session_dir)) for p in base.rglob("*") if p.is_file() and query.lower() in p.name.lower()][:50]}

def tool_copy_to_output(path, output_name="", *, session_dir):
    t, e = _guard(path, session_dir)
    if e: return {"error": e}
    if not t.exists(): return {"error": f"Not found: {path}"}
    out = session_dir / "output"; out.mkdir(exist_ok=True)
    dest = output_name or t.name
    shutil.copy2(t, out / dest)
    return {"success": True, "output_path": f"output/{dest}"}

async def tool_web_search(query, **_):
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            r = await c.get("https://html.duckduckgo.com/html/", params={"q": query}, headers={"User-Agent": "Mozilla/5.0"})
            snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</div>', r.text, re.DOTALL)
            urls = re.findall(r'uddg=(https?[^&"]+)', r.text)
            results = [{"snippet": re.sub(r'<[^>]+>','',snippets[i]).strip(), "url": urls[i] if i<len(urls) else ""} for i in range(min(6,len(snippets)))]
            return {"results": results, "query": query}
    except Exception as ex:
        return {"error": str(ex)}

async def tool_fetch_url(url, **_):
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
            clean = re.sub(r'<style[^>]*>.*?</style>','',r.text,flags=re.DOTALL)
            clean = re.sub(r'<script[^>]*>.*?</script>','',clean,flags=re.DOTALL)
            clean = re.sub(r'<[^>]+',' ',clean); clean = re.sub(r'\s+',' ',clean).strip()
            return {"content": clean[:6000], "url": url, "status": r.status_code}
    except Exception as ex:
        return {"error": str(ex)}

async def dispatch_tool(name, args, session_dir):
    kw = {**args, "session_dir": session_dir}
    fns = {
        "list_files":     lambda: tool_list_files(**kw),
        "read_file":      lambda: tool_read_file(**kw),
        "write_file":     lambda: tool_write_file(**kw),
        "apply_diff":     lambda: tool_apply_diff(**kw),
        "run_command":    lambda: tool_run_command(**kw),
        "delete_file":    lambda: tool_delete_file(**kw),
        "search_files":   lambda: tool_search_files(**kw),
        "copy_to_output": lambda: tool_copy_to_output(**kw),
        "web_search":     lambda: tool_web_search(**args),
        "fetch_url":      lambda: tool_fetch_url(**args),
    }
    fn = fns.get(name)
    if not fn: return {"error": f"Unknown tool: {name}"}
    result = fn()
    return await result if asyncio.iscoroutine(result) else result

# ---- Tool schemas ----
TOOL_DEFS = [types.Tool(function_declarations=[
    types.FunctionDeclaration(name="list_files",description="List files in session directory",
        parameters=types.Schema(type="OBJECT",properties={"path":types.Schema(type="STRING")},required=[])),
    types.FunctionDeclaration(name="read_file",description="Read file content",
        parameters=types.Schema(type="OBJECT",properties={"path":types.Schema(type="STRING")},required=["path"])),
    types.FunctionDeclaration(name="write_file",description="Write or create a file",
        parameters=types.Schema(type="OBJECT",properties={"path":types.Schema(type="STRING"),"content":types.Schema(type="STRING")},required=["path","content"])),
    types.FunctionDeclaration(name="apply_diff",description="Apply unified diff patch",
        parameters=types.Schema(type="OBJECT",properties={"path":types.Schema(type="STRING"),"diff":types.Schema(type="STRING")},required=["path","diff"])),
    types.FunctionDeclaration(name="run_command",description="Run shell command",
        parameters=types.Schema(type="OBJECT",properties={"command":types.Schema(type="STRING"),"cwd":types.Schema(type="STRING")},required=["command"])),
    types.FunctionDeclaration(name="delete_file",description="Delete file or directory",
        parameters=types.Schema(type="OBJECT",properties={"path":types.Schema(type="STRING")},required=["path"])),
    types.FunctionDeclaration(name="search_files",description="Search files by name",
        parameters=types.Schema(type="OBJECT",properties={"query":types.Schema(type="STRING"),"path":types.Schema(type="STRING")},required=["query"])),
    types.FunctionDeclaration(name="web_search",description="Search the web",
        parameters=types.Schema(type="OBJECT",properties={"query":types.Schema(type="STRING")},required=["query"])),
    types.FunctionDeclaration(name="fetch_url",description="Fetch URL content",
        parameters=types.Schema(type="OBJECT",properties={"url":types.Schema(type="STRING")},required=["url"])),
    types.FunctionDeclaration(name="copy_to_output",description="Copy file to output for download",
        parameters=types.Schema(type="OBJECT",properties={"path":types.Schema(type="STRING"),"output_name":types.Schema(type="STRING")},required=["path"])),
])]

def make_system_prompt(session_dir: Path) -> str:
    rel = session_dir.relative_to(WORKSPACE)
    return f"""You are Codesigner, an expert AI coding assistant on a Linux VM.
Session working directory: /workspace/{rel}
All file operations are relative to this directory.

- apply_diff for targeted edits; write_file for new/full-rewrite files
- read_file before editing existing files
- run_command to execute, test, install packages
- web_search + fetch_url for docs/packages
- copy_to_output to make files downloadable
- Be concise. Show what changed. Explain steps briefly.
"""

def auto_title(message: str) -> str:
    words = message.strip().split()[:8]
    title = " ".join(words)
    return title[:50] + ("…" if len(title) > 50 else "")

# ---- Agent Loop ----
async def run_agent(user_message: str, history: list, ws: WebSocket, session_dir: Path, chat_id: str):
    api_key = rotator.next()
    client = genai.Client(api_key=api_key)
    messages = history + [types.Content(role="user", parts=[types.Part(text=user_message)])]

    for _ in range(20):
        response = client.models.generate_content(
            model="gemma-4-31b-it",
            contents=messages,
            config=types.GenerateContentConfig(
                system_instruction=make_system_prompt(session_dir),
                tools=TOOL_DEFS,
                temperature=0.7,
            ),
        )
        candidate = response.candidates[0]
        text_parts, tool_calls = [], []
        for part in candidate.content.parts:
            if part.text: text_parts.append(part.text)
            if part.function_call: tool_calls.append(part.function_call)

        if text_parts:
            text = "".join(text_parts)
            await ws.send_json({"type": "text", "content": text})

        if not tool_calls:
            messages.append(candidate.content)
            # Save assistant message
            if text_parts:
                save_message(chat_id, "assistant", "".join(text_parts))
            break

        messages.append(candidate.content)
        tool_response_parts = []

        for fc in tool_calls:
            name, args = fc.name, dict(fc.args)
            await ws.send_json({"type": "tool_call", "tool": name, "args": args})

            required, reason = needs_approval(name, args, session_dir)
            if required:
                call_id = str(id(fc))
                await ws.send_json({"type": "approval_request", "tool": name, "args": args, "call_id": call_id, "reason": reason})
                try:
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=120)
                    if not (msg.get("type") == "approval" and msg.get("approved")):
                        result = {"cancelled": True}
                        await ws.send_json({"type": "tool_result", "tool": name, "result": result})
                        tool_response_parts.append(types.Part(
                            function_response=types.FunctionResponse(name=name, response={"result": json.dumps(result)})
                        ))
                        continue
                except asyncio.TimeoutError:
                    result = {"error": "approval timeout"}
                    tool_response_parts.append(types.Part(
                        function_response=types.FunctionResponse(name=name, response={"result": json.dumps(result)})
                    ))
                    continue

            result = await dispatch_tool(name, args, session_dir)
            await ws.send_json({"type": "tool_result", "tool": name, "result": result})
            tool_response_parts.append(types.Part(
                function_response=types.FunctionResponse(name=name, response={"result": json.dumps(result, ensure_ascii=False)})
            ))

        messages.append(types.Content(role="user", parts=tool_response_parts))

    await ws.send_json({"type": "done"})
    return messages

# ---- WebSocket ----
@app.websocket("/ws/{chat_id}")
async def websocket_endpoint(ws: WebSocket, chat_id: str):
    await ws.accept()

    chat = get_chat(chat_id)
    if not chat:
        await ws.send_json({"type": "error", "content": "Chat not found"})
        await ws.close()
        return

    session_dir = WORKSPACE / chat["session_dir"]
    session_dir.mkdir(parents=True, exist_ok=True)

    # Rebuild LLM history from DB
    history = []
    for m in chat["messages"]:
        role = "user" if m["role"] == "user" else "model"
        history.append(types.Content(role=role, parts=[types.Part(text=m["content"])]))

    await ws.send_json({"type": "ready", "chat": {
        "id": chat["id"], "title": chat["title"],
        "session_dir": chat["session_dir"],
        "messages": chat["messages"]
    }})

    try:
        while True:
            data = await ws.receive_json()
            if data["type"] == "message":
                user_msg = data["content"]
                save_message(chat_id, "user", user_msg)

                # Auto-title after first message
                if len(chat["messages"]) == 0:
                    title = auto_title(user_msg)
                    update_chat_title(chat_id, title)
                    await ws.send_json({"type": "title_updated", "title": title})

                history = await run_agent(user_msg, history, ws, session_dir, chat_id)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "content": str(e)})
        except Exception:
            pass

# ---- Chat REST API ----
@app.get("/api/chats")
def api_list_chats():
    return {"chats": list_chats()}

@app.post("/api/chats")
def api_create_chat():
    return create_chat()

@app.get("/api/chats/{chat_id}")
def api_get_chat(chat_id: str):
    chat = get_chat(chat_id)
    if not chat: raise HTTPException(404, "Not found")
    return chat

@app.patch("/api/chats/{chat_id}")
def api_update_chat(chat_id: str, body: dict):
    if "title" in body:
        update_chat_title(chat_id, body["title"])
    return {"success": True}

@app.delete("/api/chats/{chat_id}")
def api_delete_chat(chat_id: str):
    chat = get_chat(chat_id)
    if not chat: raise HTTPException(404, "Not found")
    delete_chat(chat_id)
    return {"success": True}

# ---- Files REST API ----
@app.get("/api/files")
def list_files_api(chat_id: str = ""):
    if not chat_id: return {"files": []}
    chat = get_chat(chat_id)
    if not chat: raise HTTPException(404)
    session_dir = WORKSPACE / chat["session_dir"]
    return tool_list_files(session_dir=session_dir)

@app.get("/api/file")
def read_file_api(path: str, chat_id: str = ""):
    chat = get_chat(chat_id)
    if not chat: raise HTTPException(404)
    return tool_read_file(path, session_dir=WORKSPACE / chat["session_dir"])

@app.get("/api/outputs")
def list_outputs(chat_id: str = ""):
    chat = get_chat(chat_id)
    if not chat: raise HTTPException(404)
    out_dir = WORKSPACE / chat["session_dir"] / "output"
    if not out_dir.exists(): return {"files": []}
    return {"files": [{"name": p.name, "size": p.stat().st_size, "path": f"output/{p.name}"} for p in sorted(out_dir.iterdir()) if p.is_file()]}

@app.get("/api/download")
def download_file(path: str, chat_id: str = ""):
    chat = get_chat(chat_id)
    if not chat: raise HTTPException(404)
    target = (WORKSPACE / chat["session_dir"] / path).resolve()
    if not target.is_file(): raise HTTPException(404)
    return FileResponse(target, filename=target.name)

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...), chat_id: str = "", path: str = ""):
    chat = get_chat(chat_id)
    if not chat: raise HTTPException(404)
    session_dir = WORKSPACE / chat["session_dir"]
    dest = (session_dir / (path or file.filename or "upload")).resolve()
    if not str(dest).startswith(str(session_dir.resolve())): raise HTTPException(400)
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    dest.write_bytes(content)
    return {"success": True, "size": len(content)}

@app.get("/health")
def health():
    return {"status": "ok", "workspace": str(WORKSPACE), "keys": len(rotator.keys)}

if Path("../frontend/dist").exists():
    app.mount("/", StaticFiles(directory="../frontend/dist", html=True), name="static")
