import os, json, asyncio, subprocess, difflib, shutil, httpx, re
from pathlib import Path
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
OUTPUT_DIR = WORKSPACE / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

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

# ---- Tools ----
def tool_list_files(path: str = ".") -> dict:
    base = (WORKSPACE / path).resolve()
    if not str(base).startswith(str(WORKSPACE)):
        return {"error": "Access denied"}
    results = []
    try:
        for p in sorted(base.rglob("*")):
            if any(part.startswith('.') or part in ('node_modules', '__pycache__', '.git') for part in p.parts):
                continue
            rel = p.relative_to(WORKSPACE)
            results.append({"path": str(rel), "type": "dir" if p.is_dir() else "file", "size": p.stat().st_size if p.is_file() else 0})
    except Exception as e:
        return {"error": str(e)}
    return {"files": results}

def tool_read_file(path: str) -> dict:
    target = (WORKSPACE / path).resolve()
    if not str(target).startswith(str(WORKSPACE)):
        return {"error": "Access denied"}
    if not target.exists():
        return {"error": f"File not found: {path}"}
    try:
        return {"content": target.read_text(errors="replace"), "path": path}
    except Exception as e:
        return {"error": str(e)}

def tool_write_file(path: str, content: str) -> dict:
    target = (WORKSPACE / path).resolve()
    if not str(target).startswith(str(WORKSPACE)):
        return {"error": "Access denied"}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return {"success": True, "path": path, "bytes": len(content)}

def tool_apply_diff(path: str, diff: str) -> dict:
    target = (WORKSPACE / path).resolve()
    if not str(target).startswith(str(WORKSPACE)):
        return {"error": "Access denied"}
    original = target.read_text(errors="replace") if target.exists() else ""
    try:
        new_lines = list(original.splitlines(keepends=True))
        lines = diff.splitlines(keepends=True)
        i = 0
        offset = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("@@"):
                m = re.search(r"-(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))?", line)
                if m:
                    src_start = int(m.group(1)) - 1 + offset
                    i += 1
                    j = src_start
                    while i < len(lines) and not lines[i].startswith("@@"):
                        l = lines[i]
                        if l.startswith("-"):
                            if j < len(new_lines):
                                del new_lines[j]
                                offset -= 1
                        elif l.startswith("+"):
                            new_lines.insert(j, l[1:])
                            j += 1
                            offset += 1
                        else:
                            j += 1
                        i += 1
            else:
                i += 1
        new_content = "".join(new_lines)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_content)
        return {"success": True, "path": path}
    except Exception as e:
        return {"error": str(e)}

def tool_run_command(command: str, cwd: str = ".") -> dict:
    work_dir = (WORKSPACE / cwd).resolve()
    if not str(work_dir).startswith(str(WORKSPACE)):
        return {"error": "Access denied"}
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

def tool_delete_file(path: str) -> dict:
    target = (WORKSPACE / path).resolve()
    if not str(target).startswith(str(WORKSPACE)):
        return {"error": "Access denied"}
    if not target.exists():
        return {"error": f"Not found: {path}"}
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"success": True, "path": path}

def tool_search_files(query: str, path: str = ".") -> dict:
    base = (WORKSPACE / path).resolve()
    results = []
    for p in base.rglob("*"):
        if p.is_file() and query.lower() in p.name.lower():
            results.append(str(p.relative_to(WORKSPACE)))
    return {"matches": results[:50]}

def tool_copy_to_output(path: str, output_name: str = "") -> dict:
    target = (WORKSPACE / path).resolve()
    if not str(target).startswith(str(WORKSPACE)):
        return {"error": "Access denied"}
    if not target.exists():
        return {"error": f"File not found: {path}"}
    dest_name = output_name or target.name
    shutil.copy2(target, OUTPUT_DIR / dest_name)
    return {"success": True, "output_path": f"output/{dest_name}"}

async def tool_web_search(query: str) -> dict:
    results = []
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get("https://html.duckduckgo.com/html/", params={"q": query}, headers={"User-Agent": "Mozilla/5.0"})
            snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</div>', r.text, re.DOTALL)
            urls = re.findall(r'uddg=(https?[^&"]+)', r.text)
            for i in range(min(6, len(snippets))):
                results.append({
                    "snippet": re.sub(r'<[^>]+>', '', snippets[i]).strip(),
                    "url": httpx.URL(urls[i]).params.get("uddg", urls[i]) if i < len(urls) else ""
                })
    except Exception as e:
        return {"error": str(e), "query": query}
    return {"results": results, "query": query}

async def tool_fetch_url(url: str) -> dict:
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

async def dispatch_tool(name: str, args: dict):
    dispatch = {
        "list_files": lambda: tool_list_files(**args),
        "read_file": lambda: tool_read_file(**args),
        "write_file": lambda: tool_write_file(**args),
        "apply_diff": lambda: tool_apply_diff(**args),
        "run_command": lambda: tool_run_command(**args),
        "delete_file": lambda: tool_delete_file(**args),
        "search_files": lambda: tool_search_files(**args),
        "copy_to_output": lambda: tool_copy_to_output(**args),
        "web_search": lambda: tool_web_search(**args),
        "fetch_url": lambda: tool_fetch_url(**args),
    }
    fn = dispatch.get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}"}
    result = fn()
    if asyncio.iscoroutine(result):
        return await result
    return result

# ---- Tool schemas ----
TOOL_DEFS = [
    types.Tool(function_declarations=[
        types.FunctionDeclaration(name="list_files", description="List files in workspace directory",
            parameters=types.Schema(type="OBJECT", properties={"path": types.Schema(type="STRING")}, required=[])),
        types.FunctionDeclaration(name="read_file", description="Read file content",
            parameters=types.Schema(type="OBJECT", properties={"path": types.Schema(type="STRING")}, required=["path"])),
        types.FunctionDeclaration(name="write_file", description="Write or create a file with full content",
            parameters=types.Schema(type="OBJECT", properties={"path": types.Schema(type="STRING"), "content": types.Schema(type="STRING")}, required=["path", "content"])),
        types.FunctionDeclaration(name="apply_diff", description="Apply unified diff patch to a file for targeted edits",
            parameters=types.Schema(type="OBJECT", properties={"path": types.Schema(type="STRING"), "diff": types.Schema(type="STRING")}, required=["path", "diff"])),
        types.FunctionDeclaration(name="run_command", description="Execute shell command (requires user approval)",
            parameters=types.Schema(type="OBJECT", properties={"command": types.Schema(type="STRING"), "cwd": types.Schema(type="STRING")}, required=["command"])),
        types.FunctionDeclaration(name="delete_file", description="Delete file or directory (requires user approval)",
            parameters=types.Schema(type="OBJECT", properties={"path": types.Schema(type="STRING")}, required=["path"])),
        types.FunctionDeclaration(name="search_files", description="Search files by name in workspace",
            parameters=types.Schema(type="OBJECT", properties={"query": types.Schema(type="STRING"), "path": types.Schema(type="STRING")}, required=["query"])),
        types.FunctionDeclaration(name="web_search", description="Search the web for documentation or information",
            parameters=types.Schema(type="OBJECT", properties={"query": types.Schema(type="STRING")}, required=["query"])),
        types.FunctionDeclaration(name="fetch_url", description="Fetch and read content from a URL",
            parameters=types.Schema(type="OBJECT", properties={"url": types.Schema(type="STRING")}, required=["url"])),
        types.FunctionDeclaration(name="copy_to_output", description="Copy workspace file to output directory for download",
            parameters=types.Schema(type="OBJECT", properties={"path": types.Schema(type="STRING"), "output_name": types.Schema(type="STRING")}, required=["path"])),
    ])
]

SYSTEM_PROMPT = """You are Codesigner, an expert AI coding assistant running on a Linux VM.
You help users write code, edit files, run commands, and manage projects.

Guidelines:
- Use apply_diff for targeted edits; write_file for new files or full rewrites
- Always read_file before editing existing files
- Use run_command for shell operations; check exit codes
- Use web_search + fetch_url to find docs or packages when needed
- Use copy_to_output to make files available for user download
- Be concise. Explain each step briefly. Show what changed.
- Workspace is at /workspace — all files live here
"""

# ---- Agent Loop ----
NEEDS_APPROVAL = {"run_command", "delete_file", "apply_diff", "write_file"}

async def run_agent(user_message: str, history: list, ws: WebSocket):
    api_key = rotator.next()
    client = genai.Client(api_key=api_key)

    messages = history + [types.Content(role="user", parts=[types.Part(text=user_message)])]

    for _ in range(20):
        response = client.models.generate_content(
            model="gemma-4-31b-it",
            contents=messages,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                tools=TOOL_DEFS,
                temperature=0.7,
            ),
        )
        candidate = response.candidates[0]
        text_parts, tool_calls = [], []

        for part in candidate.content.parts:
            if part.text:
                text_parts.append(part.text)
            if part.function_call:
                tool_calls.append(part.function_call)

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

            if name in NEEDS_APPROVAL:
                call_id = str(id(fc))
                await ws.send_json({"type": "approval_request", "tool": name, "args": args, "call_id": call_id})
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
                    result = {"error": "Approval timeout"}
                    tool_response_parts.append(types.Part(
                        function_response=types.FunctionResponse(name=name, response={"result": json.dumps(result)})
                    ))
                    continue

            result = await dispatch_tool(name, args)
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
    try:
        while True:
            data = await ws.receive_json()
            if data["type"] == "message":
                history = await run_agent(data["content"], history[-20:], ws)
            elif data["type"] == "clear":
                history = []
                await ws.send_json({"type": "cleared"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "content": str(e)})
        except Exception:
            pass

# ---- REST API ----
@app.get("/api/files")
def list_files_api(path: str = "."):
    return tool_list_files(path)

@app.get("/api/file")
def read_file_api(path: str):
    return tool_read_file(path)

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...), path: str = ""):
    dest_name = path or file.filename or "upload"
    target = (WORKSPACE / dest_name).resolve()
    if not str(target).startswith(str(WORKSPACE)):
        raise HTTPException(400, "Invalid path")
    target.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    target.write_bytes(content)
    return {"success": True, "path": dest_name, "size": len(content)}

@app.get("/api/download")
def download_file(path: str):
    target = (WORKSPACE / path).resolve()
    if not str(target).startswith(str(WORKSPACE)) or not target.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(target, filename=target.name)

@app.get("/api/outputs")
def list_outputs():
    return {"files": [{"name": p.name, "size": p.stat().st_size, "path": f"output/{p.name}"} for p in sorted(OUTPUT_DIR.iterdir()) if p.is_file()]}

@app.delete("/api/outputs/{filename}")
def delete_output(filename: str):
    target = OUTPUT_DIR / filename
    if not target.is_file():
        raise HTTPException(404, "Not found")
    target.unlink()
    return {"success": True}

@app.get("/health")
def health():
    return {"status": "ok", "workspace": str(WORKSPACE), "keys": len(rotator.keys)}

if Path("../frontend/dist").exists():
    app.mount("/", StaticFiles(directory="../frontend/dist", html=True), name="static")
