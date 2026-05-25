import os, json, asyncio, subprocess, difflib, glob, shutil, httpx, re
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import google.generativeai as genai
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
    def next(self):
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
    for p in sorted(base.rglob("*")):
        if any(part.startswith('.') or part in ('node_modules','__pycache__','.git') for part in p.parts):
            continue
        rel = p.relative_to(WORKSPACE)
        results.append({"path": str(rel), "type": "dir" if p.is_dir() else "file", "size": p.stat().st_size if p.is_file() else 0})
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
    orig_lines = original.splitlines(keepends=True)
    try:
        new_lines = list(orig_lines)
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
        lines_changed = diff.count("\n+") + diff.count("\n-")
        return {"success": True, "path": path, "lines_changed": lines_changed}
    except Exception as e:
        return {"error": str(e)}

def tool_run_command(command: str, cwd: str = ".") -> dict:
    work_dir = (WORKSPACE / cwd).resolve()
    if not str(work_dir).startswith(str(WORKSPACE)):
        return {"error": "Access denied"}
    try:
        result = subprocess.run(
            command, shell=True, cwd=work_dir,
            capture_output=True, text=True, timeout=60
        )
        return {
            "stdout": result.stdout[-8000:] if len(result.stdout) > 8000 else result.stdout,
            "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
            "exit_code": result.returncode,
            "command": command
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
    """Copy a workspace file to the output directory for user download"""
    target = (WORKSPACE / path).resolve()
    if not str(target).startswith(str(WORKSPACE)):
        return {"error": "Access denied"}
    if not target.exists():
        return {"error": f"File not found: {path}"}
    dest_name = output_name or target.name
    dest = OUTPUT_DIR / dest_name
    shutil.copy2(target, dest)
    return {"success": True, "output_path": f"output/{dest_name}", "download_url": f"/api/download?path=output/{dest_name}"}

async def tool_web_search(query: str) -> dict:
    """Search the web using multiple fallback sources"""
    results = []
    # Try DuckDuckGo Instant Answer API
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
                headers={"User-Agent": "Codesigner/1.0"}
            )
            data = r.json()
            if data.get("AbstractText"):
                results.append({
                    "title": data.get("Heading", ""),
                    "snippet": data["AbstractText"],
                    "url": data.get("AbstractURL", "")
                })
            for r2 in data.get("RelatedTopics", [])[:6]:
                if isinstance(r2, dict) and "Text" in r2:
                    results.append({
                        "title": r2.get("Text", "")[:80],
                        "snippet": r2.get("Text", ""),
                        "url": r2.get("FirstURL", "")
                    })
    except Exception:
        pass

    # Try DuckDuckGo HTML search as fallback
    if not results:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                r = await client.get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": query},
                    headers={"User-Agent": "Mozilla/5.0 (compatible; Codesigner/1.0)"}
                )
                text = r.text
                # Extract result snippets with simple regex
                titles = re.findall(r'class="result__title"[^>]*>.*?<a[^>]*>(.*?)</a>', text, re.DOTALL)
                snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</div>', text, re.DOTALL)
                urls = re.findall(r'uddg=(https?[^&"]+)', text)
                for i in range(min(5, len(snippets))):
                    results.append({
                        "title": re.sub(r'<[^>]+>', '', titles[i]).strip() if i < len(titles) else "",
                        "snippet": re.sub(r'<[^>]+>', '', snippets[i]).strip(),
                        "url": urls[i] if i < len(urls) else ""
                    })
        except Exception as e:
            return {"error": str(e), "query": query}

    return {"results": results[:8], "query": query}

async def tool_fetch_url(url: str) -> dict:
    """Fetch content from a URL"""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; Codesigner/1.0)"})
            text = r.text
            # Strip HTML tags
            clean = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
            clean = re.sub(r'<script[^>]*>.*?</script>', '', clean, flags=re.DOTALL)
            clean = re.sub(r'<[^>]+>', ' ', clean)
            clean = re.sub(r'\s+', ' ', clean).strip()
            return {"content": clean[:5000], "url": url, "status": r.status_code}
    except Exception as e:
        return {"error": str(e), "url": url}

TOOL_DEFS = [
    {"name":"list_files","description":"List files in workspace directory","parameters":{"type":"object","properties":{"path":{"type":"string","description":"Directory path relative to workspace","default":"."}},"required":[]}},
    {"name":"read_file","description":"Read file content","parameters":{"type":"object","properties":{"path":{"type":"string","description":"File path relative to workspace"}},"required":["path"]}},
    {"name":"write_file","description":"Write or create a file","parameters":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}},
    {"name":"apply_diff","description":"Apply unified diff patch to a file","parameters":{"type":"object","properties":{"path":{"type":"string"},"diff":{"type":"string","description":"Unified diff format patch"}},"required":["path","diff"]}},
    {"name":"run_command","description":"Execute shell command in workspace","parameters":{"type":"object","properties":{"command":{"type":"string"},"cwd":{"type":"string","default":"."}},"required":["command"]}},
    {"name":"delete_file","description":"Delete file or directory","parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}},
    {"name":"search_files","description":"Search files by name","parameters":{"type":"object","properties":{"query":{"type":"string"},"path":{"type":"string","default":"."}},"required":["query"]}},
    {"name":"web_search","description":"Search the web for information","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}},
    {"name":"fetch_url","description":"Fetch and read content from a URL","parameters":{"type":"object","properties":{"url":{"type":"string"}},"required":["url"]}},
    {"name":"copy_to_output","description":"Copy a file to the output directory so the user can download it via the web UI","parameters":{"type":"object","properties":{"path":{"type":"string","description":"Source file path in workspace"},"output_name":{"type":"string","description":"Optional output filename","default":""}},"required":["path"]}},
]

async def dispatch_tool(name: str, args: dict) -> str:
    if name == "list_files":      result = tool_list_files(**args)
    elif name == "read_file":     result = tool_read_file(**args)
    elif name == "write_file":    result = tool_write_file(**args)
    elif name == "apply_diff":    result = tool_apply_diff(**args)
    elif name == "run_command":   result = tool_run_command(**args)
    elif name == "delete_file":   result = tool_delete_file(**args)
    elif name == "search_files":  result = tool_search_files(**args)
    elif name == "web_search":    result = await tool_web_search(**args)
    elif name == "fetch_url":     result = await tool_fetch_url(**args)
    elif name == "copy_to_output":result = tool_copy_to_output(**args)
    else: result = {"error": f"Unknown tool: {name}"}
    return json.dumps(result, ensure_ascii=False)

# ---- Agent Loop ----
SYSTEM_PROMPT = """You are Codesigner, an expert AI coding assistant similar to Codex CLI.
You help users write code, edit files, run commands, and manage projects on a Linux VM.

Guidelines:
- Prefer apply_diff for small changes to existing files; use write_file for new files or rewrites
- Always read a file before editing it
- Use run_command for shell operations; check exit codes and stderr
- Use web_search to find documentation, packages, or current information
- Use fetch_url to read specific pages after web_search
- When you produce output files for the user to download, use copy_to_output to place them in the output directory
- Be concise but thorough. Show diffs when making changes. Explain what you're doing step by step.
"""

async def run_agent(user_message: str, history: list, ws: WebSocket, file_context: str = ""):
    api_key = rotator.next()
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        model_name="gemma-4-31b-it",
        system_instruction=SYSTEM_PROMPT,
        tools=TOOL_DEFS
    )

    content = user_message
    if file_context:
        content = f"{user_message}\n\n[Attached files:\n{file_context}]"

    messages = history + [{"role": "user", "parts": [{"text": content}]}]

    for iteration in range(20):
        response = model.generate_content(messages)
        candidate = response.candidates[0]

        text_parts = []
        tool_calls = []
        for part in candidate.content.parts:
            if hasattr(part, 'text') and part.text:
                text_parts.append(part.text)
            if hasattr(part, 'function_call') and part.function_call:
                tool_calls.append(part.function_call)

        if text_parts:
            await ws.send_json({"type": "text", "content": "".join(text_parts)})

        if not tool_calls:
            messages.append({"role": "model", "parts": candidate.content.parts})
            break

        messages.append({"role": "model", "parts": candidate.content.parts})

        tool_results = []
        for fc in tool_calls:
            tool_name = fc.name
            tool_args = dict(fc.args)

            await ws.send_json({"type": "tool_call", "tool": tool_name, "args": tool_args})

            DANGEROUS = {"run_command", "delete_file", "apply_diff", "write_file"}
            needs_approval = tool_name in DANGEROUS

            if needs_approval:
                await ws.send_json({"type": "approval_request", "tool": tool_name, "args": tool_args, "call_id": f"{iteration}_{tool_name}"})
                try:
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=120)
                    if msg.get("type") == "approval" and msg.get("approved"):
                        pass
                    else:
                        result_str = json.dumps({"cancelled": True, "reason": "User rejected"})
                        await ws.send_json({"type": "tool_result", "tool": tool_name, "result": {"cancelled": True}})
                        tool_results.append({"function_response": {"name": tool_name, "response": {"result": result_str}}})
                        continue
                except asyncio.TimeoutError:
                    result_str = json.dumps({"error": "Approval timeout"})
                    tool_results.append({"function_response": {"name": tool_name, "response": {"result": result_str}}})
                    continue

            result_str = await dispatch_tool(tool_name, tool_args)
            result_data = json.loads(result_str)
            await ws.send_json({"type": "tool_result", "tool": tool_name, "result": result_data})
            tool_results.append({"function_response": {"name": tool_name, "response": {"result": result_str}}})

        messages.append({"role": "user", "parts": tool_results})

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
                file_context = data.get("file_context", "")
                history = await run_agent(data["content"], history[-20:], ws, file_context)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "content": str(e)})
        except:
            pass

# ---- File API ----
@app.get("/api/files")
def list_files_api(path: str = "."):
    return tool_list_files(path)

@app.get("/api/file")
def read_file_api(path: str):
    return tool_read_file(path)

@app.post("/api/file")
async def write_file_api(path: str, body: dict):
    return tool_write_file(path, body["content"])

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...), path: str = ""):
    dest_name = path or file.filename or "uploaded_file"
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
    """List files in the output directory available for download"""
    files = []
    for p in sorted(OUTPUT_DIR.iterdir()):
        if p.is_file():
            files.append({"name": p.name, "size": p.stat().st_size, "path": f"output/{p.name}"})
    return {"files": files}

@app.delete("/api/outputs/{filename}")
def delete_output(filename: str):
    target = OUTPUT_DIR / filename
    if not target.is_file():
        raise HTTPException(404, "File not found")
    target.unlink()
    return {"success": True}

@app.get("/health")
def health():
    return {"status": "ok", "workspace": str(WORKSPACE)}

# Serve frontend
if Path("../frontend/dist").exists():
    app.mount("/", StaticFiles(directory="../frontend/dist", html=True), name="static")
