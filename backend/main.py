import os, json, asyncio, subprocess, shutil, httpx, re, uuid
from pathlib import Path
from datetime import datetime
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

# ---- Session Workspace ----
def make_session_dir() -> Path:
    """Create a new session-scoped working directory."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    sid = uuid.uuid4().hex[:6]
    session_dir = WORKSPACE / "sessions" / f"{ts}_{sid}"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "output").mkdir(exist_ok=True)
    return session_dir

# ---- Approval logic ----
SUDO_PATTERNS = ["sudo ", "sudo\t", "su ", "su\t", "pkexec", "doas "]

def needs_approval(tool_name: str, args: dict, session_dir: Path) -> tuple[bool, str]:
    """Return (needs_approval, reason)."""
    if tool_name == "run_command":
        cmd = args.get("command", "")
        if any(p in cmd for p in SUDO_PATTERNS):
            return True, "privilege escalation (sudo/su)"
        return False, ""

    # File ops outside session dir
    path_arg = args.get("path", "")
    if path_arg and tool_name in ("write_file", "apply_diff", "delete_file", "read_file", "list_files"):
        try:
            target = (session_dir / path_arg).resolve()
            if not str(target).startswith(str(session_dir.resolve())):
                return True, f"access outside session folder: {path_arg}"
        except Exception:
            return True, "invalid path"

    return False, ""

# ---- Tools ----
def _check_path(path: str, session_dir: Path, allow_outside: bool = False) -> tuple[Path | None, str | None]:
    target = (session_dir / path).resolve()
    if not allow_outside and not str(target).startswith(str(session_dir.resolve())):
        return None, "Access denied: outside session folder"
    if not str(target).startswith(str(WORKSPACE.resolve())):
        return None, "Access denied: outside workspace"
    return target, None

def tool_list_files(path: str = ".", *, session_dir: Path) -> dict:
    base = (session_dir / path).resolve()
    if not str(base).startswith(str(WORKSPACE.resolve())):
        return {"error": "Access denied"}
    results = []
    try:
        for p in sorted(base.rglob("*")):
            if any(part.startswith('.') or part in ('node_modules', '__pycache__', '.git') for part in p.parts):
                continue
            rel = p.relative_to(session_dir)
            results.append({"path": str(rel), "type": "dir" if p.is_dir() else "file", "size": p.stat().st_size if p.is_file() else 0})
    except Exception as e:
        return {"error": str(e)}
    return {"files": results, "session_dir": str(session_dir.relative_to(WORKSPACE))}

def tool_read_file(path: str, *, session_dir: Path) -> dict:
    target, err = _check_path(path, session_dir, allow_outside=True)
    if err: return {"error": err}
    if not target.exists(): return {"error": f"File not found: {path}"}
    try:
        return {"content": target.read_text(errors="replace"), "path": path}
    except Exception as e:
        return {"error": str(e)}

def tool_write_file(path: str, content: str, *, session_dir: Path) -> dict:
    target, err = _check_path(path, session_dir)
    if err: return {"error": err}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return {"success": True, "path": path, "bytes": len(content)}

def tool_apply_diff(path: str, diff: str, *, session_dir: Path) -> dict:
    target, err = _check_path(path, session_dir)
    if err: return {"error": err}
    original = target.read_text(errors="replace") if target.exists() else ""
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
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(new_lines))
        return {"success": True, "path": path}
    except Exception as e:
        return {"error": str(e)}

def tool_run_command(command: str, cwd: str = ".", *, session_dir: Path) -> dict:
    work_dir = (session_dir / cwd).resolve()
    # cwd must be inside workspace (not necessarily session_dir — approved commands may use workspace root)
    if not str(work_dir).startswith(str(WORKSPACE.resolve())):
        return {"error": "Access denied: cwd outside workspace"}
    try:
        result = subprocess.run(command, shell=True, cwd=work_dir, capture_output=True, text=True, timeout=60)
        return {
            "stdout": result.stdout[-8000:],
            "stderr": result.stderr[-2000:],
            "exit_code": result.returncode,
            "command": command,
        }
    except subprocess.TimeoutExpired:
        return {"error": "Command timed out (60s)"}
    except Exception as e:
        return {"error": str(e)}

def tool_delete_file(path: str, *, session_dir: Path) -> dict:
    target, err = _check_path(path, session_dir)
    if err: return {"error": err}
    if not target.exists(): return {"error": f"Not found: {path}"}
    if target.is_dir(): shutil.rmtree(target)
    else: target.unlink()
    return {"success": True, "path": path}

def tool_search_files(query: str, path: str = ".", *, session_dir: Path) -> dict:
    base = (session_dir / path).resolve()
    results = [str(p.relative_to(session_dir)) for p in base.rglob("*") if p.is_file() and query.lower() in p.name.lower()]
    return {"matches": results[:50]}

def tool_copy_to_output(path: str, output_name: str = "", *, session_dir: Path) -> dict:
    target, err = _check_path(path, session_dir)
    if err: return {"error": err}
    if not target.exists(): return {"error": f"File not found: {path}"}
    out_dir = session_dir / "output"
    out_dir.mkdir(exist_ok=True)
    dest_name = output_name or target.name
    shutil.copy2(target, out_dir / dest_name)
    return {"success": True, "output_path": f"output/{dest_name}"}

async def tool_web_search(query: str, **_) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get("https://html.duckduckgo.com/html/", params={"q": query}, headers={"User-Agent": "Mozilla/5.0"})
            snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</div>', r.text, re.DOTALL)
            urls = re.findall(r'uddg=(https?[^&"]+)', r.text)
            results = [{"snippet": re.sub(r'<[^>]+>', '', snippets[i]).strip(), "url": urls[i] if i < len(urls) else ""} for i in range(min(6, len(snippets)))]
            return {"results": results, "query": query}
    except Exception as e:
        return {"error": str(e), "query": query}

async def tool_fetch_url(url: str, **_) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            clean = re.sub(r'<style[^>]*>.*?</style>', '', r.text, flags=re.DOTALL)
            clean = re.sub(r'<script[^>]*>.*?</script>', '', clean, flags=re.DOTALL)
            clean = re.sub(r'<[^>]+>', ' ', clean)
            clean = re.sub(r'\s+', ' ', clean).strip()
            return {"content": clean[:6000], "url": url, "status": r.status_code}
    except Exception as e:
        return {"error": str(e), "url": url}

async def dispatch_tool(name: str, args: dict, session_dir: Path):
    kw = {**args, "session_dir": session_dir}
    fns = {
        "list_files": lambda: tool_list_files(**kw),
        "read_file": lambda: tool_read_file(**kw),
        "write_file": lambda: tool_write_file(**kw),
        "apply_diff": lambda: tool_apply_diff(**kw),
        "run_command": lambda: tool_run_command(**kw),
        "delete_file": lambda: tool_delete_file(**kw),
        "search_files": lambda: tool_search_files(**kw),
        "copy_to_output": lambda: tool_copy_to_output(**kw),
        "web_search": lambda: tool_web_search(**args),
        "fetch_url": lambda: tool_fetch_url(**args),
    }
    fn = fns.get(name)
    if not fn: return {"error": f"Unknown tool: {name}"}
    result = fn()
    return await result if asyncio.iscoroutine(result) else result

# ---- Tool schemas ----
TOOL_DEFS = [types.Tool(function_declarations=[
    types.FunctionDeclaration(name="list_files", description="List files in session working directory",
        parameters=types.Schema(type="OBJECT", properties={"path": types.Schema(type="STRING", description="Path relative to session dir, default '.'")}, required=[])),
    types.FunctionDeclaration(name="read_file", description="Read a file",
        parameters=types.Schema(type="OBJECT", properties={"path": types.Schema(type="STRING")}, required=["path"])),
    types.FunctionDeclaration(name="write_file", description="Write or create a file",
        parameters=types.Schema(type="OBJECT", properties={"path": types.Schema(type="STRING"), "content": types.Schema(type="STRING")}, required=["path", "content"])),
    types.FunctionDeclaration(name="apply_diff", description="Apply unified diff patch to a file",
        parameters=types.Schema(type="OBJECT", properties={"path": types.Schema(type="STRING"), "diff": types.Schema(type="STRING")}, required=["path", "diff"])),
    types.FunctionDeclaration(name="run_command", description="Run shell command in session directory",
        parameters=types.Schema(type="OBJECT", properties={"command": types.Schema(type="STRING"), "cwd": types.Schema(type="STRING", description="Working dir relative to session dir")}, required=["command"])),
    types.FunctionDeclaration(name="delete_file", description="Delete file or directory in session",
        parameters=types.Schema(type="OBJECT", properties={"path": types.Schema(type="STRING")}, required=["path"])),
    types.FunctionDeclaration(name="search_files", description="Search files by name",
        parameters=types.Schema(type="OBJECT", properties={"query": types.Schema(type="STRING"), "path": types.Schema(type="STRING")}, required=["query"])),
    types.FunctionDeclaration(name="web_search", description="Search the web",
        parameters=types.Schema(type="OBJECT", properties={"query": types.Schema(type="STRING")}, required=["query"])),
    types.FunctionDeclaration(name="fetch_url", description="Fetch content from a URL",
        parameters=types.Schema(type="OBJECT", properties={"url": types.Schema(type="STRING")}, required=["url"])),
    types.FunctionDeclaration(name="copy_to_output", description="Copy file to output folder for download",
        parameters=types.Schema(type="OBJECT", properties={"path": types.Schema(type="STRING"), "output_name": types.Schema(type="STRING")}, required=["path"])),
])]

def make_system_prompt(session_dir: Path) -> str:
    rel = session_dir.relative_to(WORKSPACE)
    return f"""You are Codesigner, an expert AI coding assistant running on a Linux VM.
Your working directory for this session is: /workspace/{rel}
All file operations default to this session directory.

Guidelines:
- Use apply_diff for targeted edits; write_file for new files or full rewrites
- Always read_file before editing existing files
- Use run_command to execute code, install packages, run tests
- Use web_search + fetch_url to look up docs or packages
- Use copy_to_output to make files available for user download
- Be concise. Explain steps briefly. Show what changed.
"""

# ---- Agent Loop ----
async def run_agent(user_message: str, history: list, ws: WebSocket, session_dir: Path):
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
            await ws.send_json({"type": "text", "content": "".join(text_parts)})

        if not tool_calls:
            messages.append(candidate.content)
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
                        result = {"cancelled": True, "reason": "rejected by user"}
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
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    history = []
    session_dir = make_session_dir()
    # Notify client of session directory
    await ws.send_json({"type": "session_init", "session_dir": str(session_dir.relative_to(WORKSPACE)), "session_id": session_dir.name})
    try:
        while True:
            data = await ws.receive_json()
            if data["type"] == "message":
                history = await run_agent(data["content"], history[-20:], ws, session_dir)
            elif data["type"] == "clear":
                history = []
                session_dir = make_session_dir()
                await ws.send_json({"type": "session_init", "session_dir": str(session_dir.relative_to(WORKSPACE)), "session_id": session_dir.name})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "content": str(e)})
        except Exception:
            pass

# ---- REST API ----
@app.get("/api/files")
def list_files_api(path: str = ".", session_id: str = ""):
    if session_id:
        session_dir = WORKSPACE / "sessions" / session_id
    else:
        session_dir = WORKSPACE
    return tool_list_files(path, session_dir=session_dir)

@app.get("/api/file")
def read_file_api(path: str, session_id: str = ""):
    session_dir = WORKSPACE / "sessions" / session_id if session_id else WORKSPACE
    return tool_read_file(path, session_dir=session_dir)

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...), session_id: str = "", path: str = ""):
    session_dir = WORKSPACE / "sessions" / session_id if session_id else WORKSPACE
    dest_name = path or file.filename or "upload"
    target = (session_dir / dest_name).resolve()
    if not str(target).startswith(str(WORKSPACE.resolve())):
        raise HTTPException(400, "Invalid path")
    target.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    target.write_bytes(content)
    return {"success": True, "path": dest_name, "size": len(content)}

@app.get("/api/download")
def download_file(path: str, session_id: str = ""):
    session_dir = WORKSPACE / "sessions" / session_id if session_id else WORKSPACE
    target = (session_dir / path).resolve()
    if not str(target).startswith(str(WORKSPACE.resolve())) or not target.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(target, filename=target.name)

@app.get("/api/outputs")
def list_outputs(session_id: str = ""):
    session_dir = WORKSPACE / "sessions" / session_id if session_id else WORKSPACE
    out_dir = session_dir / "output"
    if not out_dir.exists():
        return {"files": []}
    return {"files": [{"name": p.name, "size": p.stat().st_size, "path": f"output/{p.name}"} for p in sorted(out_dir.iterdir()) if p.is_file()]}

@app.delete("/api/outputs/{filename}")
def delete_output(filename: str, session_id: str = ""):
    session_dir = WORKSPACE / "sessions" / session_id if session_id else WORKSPACE
    target = session_dir / "output" / filename
    if not target.is_file():
        raise HTTPException(404, "Not found")
    target.unlink()
    return {"success": True}

@app.get("/api/sessions")
def list_sessions():
    sessions_dir = WORKSPACE / "sessions"
    if not sessions_dir.exists():
        return {"sessions": []}
    sessions = []
    for d in sorted(sessions_dir.iterdir(), reverse=True):
        if d.is_dir():
            files = list(d.rglob("*"))
            sessions.append({"id": d.name, "created": d.stat().st_ctime, "files": len([f for f in files if f.is_file()])})
    return {"sessions": sessions[:20]}

@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str):
    target = WORKSPACE / "sessions" / session_id
    if not target.exists():
        raise HTTPException(404, "Session not found")
    shutil.rmtree(target)
    return {"success": True}

@app.get("/health")
def health():
    return {"status": "ok", "workspace": str(WORKSPACE), "keys": len(rotator.keys)}

if Path("../frontend/dist").exists():
    app.mount("/", StaticFiles(directory="../frontend/dist", html=True), name="static")
