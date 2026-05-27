import os, json, asyncio, subprocess, shutil, httpx, re, uuid, sqlite3
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Form
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
LOGIN_PASSCODE = os.getenv("LOGIN_PASSCODE", "")  # 空文字なら認証なし

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
            msg_type TEXT NOT NULL DEFAULT 'text',
            created_at TEXT NOT NULL,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
        )""")
        # マイグレーション: msg_typeカラムがない場合追加
        cols = [r[1] for r in c.execute("PRAGMA table_info(messages)").fetchall()]
        if 'msg_type' not in cols:
            c.execute("ALTER TABLE messages ADD COLUMN msg_type TEXT NOT NULL DEFAULT 'text'")
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
        self._clients: dict = {}

    def next(self) -> str:
        if not self.keys:
            raise RuntimeError("No API keys configured. Set GEMMA_API_KEYS in .env")
        key = self.keys[self.index % len(self.keys)]
        self.index += 1
        return key

    def get_client(self, key: str):
        if key not in self._clients:
            self._clients[key] = genai.Client(api_key=key)
        return self._clients[key]

    def next_client(self):
        key = self.next()
        return key, self.get_client(key)

rotator = KeyRotator()

# ---- Session / Chat ----
def make_session_dir() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    sid = uuid.uuid4().hex[:6]
    d = WORKSPACE / "sessions" / f"{ts}_{sid}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "output").mkdir(exist_ok=True)
    (d / "input").mkdir(exist_ok=True)
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

def save_message(chat_id: str, role: str, content: str, msg_type: str = "text"):
    with get_db() as db:
        db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)",
                   (uuid.uuid4().hex, chat_id, role, content, msg_type, now_iso()))
        db.execute("UPDATE chats SET updated_at=? WHERE id=?", (now_iso(), chat_id))

def truncate_messages_from(chat_id: str, from_index: int):
    """指定インデックス以降のメッセージをDBから削除"""
    with get_db() as db:
        msgs = db.execute(
            "SELECT id FROM messages WHERE chat_id=? ORDER BY created_at", (chat_id,)
        ).fetchall()
        ids_to_delete = [m["id"] for m in msgs[from_index:]]
        if ids_to_delete:
            db.execute(
                f"DELETE FROM messages WHERE id IN ({','.join('?'*len(ids_to_delete))})",
                ids_to_delete
            )

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

def tool_read_file(path, *, session_dir, start_line=None, end_line=None):
    t, e = _guard(path, session_dir, allow_outside=True)
    if e: return {"error": e}
    if not t.exists(): return {"error": f"Not found: {path}"}
    try:
        raw = t.read_text(errors="replace")
        lines = raw.splitlines(keepends=True)
        total_lines = len(lines)
        total_bytes = len(raw.encode("utf-8", errors="replace"))
        if start_line is not None or end_line is not None:
            s = max(0, (int(start_line) - 1) if start_line is not None else 0)
            e2 = min(total_lines, int(end_line) if end_line is not None else total_lines)
            chunk = "".join(lines[s:e2])
            return {
                "content": chunk, "path": path,
                "start_line": s+1, "end_line": e2,
                "total_lines": total_lines, "total_bytes": total_bytes,
                "note": f"Showing lines {s+1}-{e2} of {total_lines} total. Use start_line/end_line to read other sections."
            }
        # 大きいファイルは最初の500行のみ返し、残りは範囲指定で読むよう案内
        CHUNK_LINES = 500
        if total_lines > CHUNK_LINES:
            chunk = "".join(lines[:CHUNK_LINES])
            return {
                "content": chunk, "path": path,
                "start_line": 1, "end_line": CHUNK_LINES,
                "total_lines": total_lines, "total_bytes": total_bytes,
                "note": f"File has {total_lines} lines ({total_bytes} bytes). Showing lines 1-{CHUNK_LINES}. Use start_line/end_line params to read other sections. Use search_in_file to find specific functions/patterns."
            }
        return {"content": raw, "path": path, "total_lines": total_lines, "total_bytes": total_bytes}
    except Exception as ex: return {"error": str(ex)}

def tool_search_in_file(path, pattern, *, session_dir, context_lines=3):
    """ファイル内をgrepして行番号付きで返す（大きいファイルの特定部分を探すのに最適）"""
    t, e = _guard(path, session_dir, allow_outside=True)
    if e: return {"error": e}
    if not t.exists(): return {"error": f"Not found: {path}"}
    try:
        import re as _re
        lines = t.read_text(errors="replace").splitlines()
        results = []
        for i, line in enumerate(lines):
            if pattern.lower() in line.lower() or _re.search(pattern, line, _re.IGNORECASE):
                s = max(0, i - context_lines)
                e2 = min(len(lines), i + context_lines + 1)
                results.append({
                    "line": i + 1,
                    "match": line.strip(),
                    "context": "\n".join(f"{s+j+1}: {lines[s+j]}" for j in range(e2-s))
                })
                if len(results) >= 20:
                    break
        return {"matches": results, "total_matches": len(results), "path": path,
                "hint": "Use read_file with start_line/end_line around the matching line numbers to read those sections."}
    except Exception as ex:
        return {"error": str(ex)}


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

    import hashlib, difflib
    diff_hash = hashlib.md5(diff.encode()).hexdigest()
    marker_dir = session_dir / ".diff_applied"
    marker_dir.mkdir(exist_ok=True)
    marker = marker_dir / f"{t.name}_{diff_hash}"
    if marker.exists():
        return {"error": "already_applied", "message": "This diff has already been applied. Do not apply again."}
    marker.touch()

    def normalize_line(l):
        return ' '.join(l.split())

    def _find_block(file_lines, search_lines, hint=None):
        """完全一致 -> strip一致 -> 空白正規化 の3段階。fuzzyは廃止（大ファイルでO(n^2)ハングの原因）"""
        n = len(search_lines)
        if n == 0:
            return hint or 0

        total = max(0, len(file_lines) - n + 1)
        search_order = list(range(total))
        if hint is not None:
            search_order.sort(key=lambda x: abs(x - hint))

        # 段階1: 完全一致
        for i in search_order:
            if all(i+k < len(file_lines) and
                   file_lines[i+k].rstrip("\n\r") == search_lines[k].rstrip("\n\r")
                   for k in range(n)):
                return i
        # 段階2: strip一致
        for i in search_order:
            if all(i+k < len(file_lines) and
                   file_lines[i+k].rstrip("\n\r").strip() == search_lines[k].rstrip("\n\r").strip()
                   for k in range(n)):
                return i
        # 段階3: 空白正規化（バッククォート・${}など特殊文字を含む行に有効）
        for i in search_order:
            if all(i+k < len(file_lines) and
                   normalize_line(file_lines[i+k]) == normalize_line(search_lines[k])
                   for k in range(n)):
                return i
        # fuzzyマッチは廃止: O(n^2)のSequenceMatcherが大きいファイルで数十分ハングする原因
        return None

    def _make_hint(search_lines, file_lines):
        """失敗時のヒント生成（軽量版: get_close_matchesも大ファイルで重いため単純部分一致のみ）"""
        if not search_lines:
            return ""
        first = search_lines[0].strip() if search_lines else ""
        for idx, l in enumerate(file_lines):
            if first and first[:30] in l:
                return f"\nHINT: Closest line at {idx+1}: {l.strip()[:120]!r}\nUse read_file to get exact content, then retry."
        return f"\nHINT: First SEARCH line {first[:80]!r} not found. Use read_file to check current content."

    try:
        # ── 形式0: V4A patch (*** Begin Patch) ──────────────────────────
        if "*** Begin Patch" in diff or "*** Update File" in diff:
            text = original
            lines_added = lines_removed = 0
            # V4A: @@ <context_anchor> ヘッダ + +/-/space lines
            # contextアンカー行を含む全hunkを処理
            file_lines = text.splitlines()
            result_lines = list(file_lines)
            offset = 0
            hunk_pat = re.compile(r'^@@\s*(.*)', re.MULTILINE)
            diff_body_lines = diff.splitlines()
            i = 0
            hunk_errors = []
            while i < len(diff_body_lines):
                line = diff_body_lines[i]
                if line.startswith('@@'):
                    # アンカー: @@ の後のテキストがコンテキスト
                    anchor_text = line[2:].strip()
                    hunk_body = []
                    i += 1
                    while i < len(diff_body_lines) and not diff_body_lines[i].startswith('@@') \
                          and not diff_body_lines[i].startswith('*** '):
                        hunk_body.append(diff_body_lines[i])
                        i += 1
                    # context行（空白で始まる）とsearch行（-）を抽出
                    search_lines = [l[1:] if l and l[0] in (' ', '-') else l
                                    for l in hunk_body if l and l[0] != '+']
                    # アンカーテキストがある場合はそこを起点に探す
                    hint = None
                    if anchor_text:
                        for li, fl in enumerate(result_lines):
                            if anchor_text.strip() and anchor_text.strip() in fl:
                                hint = li + offset
                                break
                    best_pos = _find_block(result_lines, search_lines, hint=hint)
                    if best_pos is None:
                        hunk_errors.append(f"V4A hunk '{anchor_text[:60]}': match failed" + _make_hint(search_lines, result_lines))
                        continue
                    j = best_pos
                    for hl in hunk_body:
                        if not hl:
                            j += 1
                            continue
                        prefix = hl[0]
                        content = hl[1:]
                        if prefix == '-':
                            if j < len(result_lines):
                                del result_lines[j]
                                offset -= 1
                                lines_removed += 1
                        elif prefix == '+':
                            result_lines.insert(j, content)
                            j += 1
                            offset += 1
                            lines_added += 1
                        else:  # context
                            j += 1
                else:
                    i += 1
            if hunk_errors and lines_added == 0 and lines_removed == 0:
                marker.unlink(missing_ok=True)
                return {"error": "V4A patch failed:\n" + "\n".join(hunk_errors),
                        "file_content_preview": "\n".join(original.splitlines()[:30])}
            t.parent.mkdir(parents=True, exist_ok=True)
            result_text = "\n".join(result_lines)
            if original.endswith("\n"):
                result_text += "\n"
            t.write_text(result_text)
            res = {"success": True, "path": path, "lines": len(result_lines),
                   "lines_changed": lines_added, "lines_removed": lines_removed, "format": "v4a"}
            if hunk_errors:
                res["warnings"] = hunk_errors
            return res

        # ── 形式1: SEARCH/REPLACE ブロック ──────────────────────────────
        if "<<<<<<" in diff and "=======" in diff:
            text = original
            pattern = re.compile(
                r"<{6,7}\s*SEARCH\s*\n(.*?)\n?={6,7}\n(.*?)\n?>{6,7}\s*REPLACE",
                re.DOTALL
            )
            matches = list(pattern.finditer(diff))
            if not matches:
                marker.unlink(missing_ok=True)
                return {"error": "SEARCH/REPLACE parse failed. Use: <<<<<<< SEARCH / ======= / >>>>>>> REPLACE"}
            applied, errors = 0, []
            fl = text.splitlines()
            for m in matches:
                search_block = m.group(1)
                replace_block = m.group(2)
                sl = search_block.splitlines()
                found = False
                # 完全一致
                if search_block in text:
                    text = text.replace(search_block, replace_block, 1)
                    applied += 1
                    found = True
                else:
                    fl = text.splitlines()
                    best_pos = _find_block(fl, sl)
                    if best_pos is not None:
                        actual = "\n".join(fl[best_pos:best_pos+len(sl)])
                        text = text.replace(actual, replace_block, 1)
                        applied += 1
                        found = True
                if not found:
                    fl_now = text.splitlines()
                    errors.append(f"SEARCH block not found:{_make_hint(sl, fl_now)}\nSEARCH was:\n{search_block[:300]}")
            if errors and applied == 0:
                marker.unlink(missing_ok=True)
                return {"error": "\n".join(errors),
                        "file_content_preview": "\n".join(original.splitlines()[:30])}
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_text(text)
            result = {"success": True, "path": path, "lines": len(text.splitlines()),
                      "lines_changed": applied, "lines_removed": len(matches) - applied,
                      "format": "search_replace"}
            if errors:
                result["warnings"] = errors
            return result

        # ── 形式2: unified diff (@@ ... @@) ────────────────────────────
        diff_lines = diff.splitlines(keepends=True)
        hunks = []
        i = 0
        while i < len(diff_lines):
            line = diff_lines[i]
            if line.startswith("@@"):
                m = re.search(r"-(\d+)(?:,\d+)?", line)
                hint = int(m.group(1)) - 1 if m else None
                # V4A風: @@ の後にコンテキスト文字列があればアンカーとして使う
                anchor = re.sub(r"@@\s*-\d+(?:,\d+)?\s*\+\d+(?:,\d+)?\s*@@", "", line).strip()
                hunk_body = []
                i += 1
                while i < len(diff_lines) and not diff_lines[i].startswith("@@"):
                    if not diff_lines[i].startswith(("---", "+++")):
                        hunk_body.append(diff_lines[i])
                    i += 1
                hunks.append((hint, anchor, hunk_body))
            else:
                i += 1

        if not hunks:
            marker.unlink(missing_ok=True)
            return {"error": "Unknown diff format. Use: unified diff (@@ ... @@), SEARCH/REPLACE, or V4A (*** Begin Patch)"}

        new_lines = list(original.splitlines(keepends=True))
        offset = 0
        hunk_errors = []

        for hunk_idx, (hint, anchor, hunk_body) in enumerate(hunks):
            search_lines = [l[1:] if l.startswith((" ", "-")) else l
                            for l in hunk_body if not l.startswith("+")]
            hint_adj = (hint + offset) if hint is not None else None
            # アンカー文字列があればヒントを補正
            if anchor:
                for li, fl in enumerate(new_lines):
                    if anchor.strip() and anchor.strip() in fl:
                        hint_adj = li
                        break
            best_pos = _find_block(new_lines, search_lines, hint=hint_adj)

            if best_pos is None:
                hunk_errors.append(f"hunk {hunk_idx+1}: match failed" + _make_hint(search_lines, new_lines))
                continue

            j = best_pos
            for l in hunk_body:
                if l.startswith("-"):
                    if j < len(new_lines):
                        del new_lines[j]
                        offset -= 1
                elif l.startswith("+"):
                    ins = l[1:] if l[1:].endswith("\n") else l[1:] + "\n"
                    new_lines.insert(j, ins)
                    j += 1
                    offset += 1
                else:
                    j += 1

        if hunk_errors and len(hunk_errors) == len(hunks):
            marker.unlink(missing_ok=True)
            return {"error": "All hunks failed:\n" + "\n".join(hunk_errors),
                    "file_content_preview": "\n".join(original.splitlines()[:30])}

        t.parent.mkdir(parents=True, exist_ok=True)
        result_text = "".join(new_lines)
        t.write_text(result_text)
        lines_added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
        lines_removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
        result = {"success": True, "path": path, "lines": len(new_lines),
                  "lines_changed": lines_added, "lines_removed": lines_removed,
                  "format": "unified_diff"}
        if hunk_errors:
            result["warnings"] = hunk_errors
        return result

    except Exception as ex:
        marker.unlink(missing_ok=True)
        return {"error": str(ex)}

async def tool_run_command_streaming(command, cwd=".", *, session_dir, ws=None):
    """コマンドをストリーミング実行しWSにリアルタイム送信"""
    work_dir = (session_dir / cwd).resolve()
    if not str(work_dir).startswith(str(WORKSPACE.resolve())):
        return {"error": "Access denied"}
    try:
        proc = await asyncio.create_subprocess_shell(
            command, cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output_lines = []
        async def read_stream():
            async for line in proc.stdout:
                decoded = line.decode('utf-8', errors='replace')
                output_lines.append(decoded)
                if ws:
                    try:
                        await ws.send_json({"type": "cmd_stream", "line": decoded.rstrip()})
                    except Exception:
                        pass
        try:
            await asyncio.wait_for(read_stream(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            return {"error": "Timed out (60s)", "stdout": ''.join(output_lines)[-4000:]}
        await proc.wait()
        stdout = ''.join(output_lines)
        return {"stdout": stdout[-8000:], "stderr": "", "exit_code": proc.returncode, "command": command}
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

def to_json_safe(obj):
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        try:
            cleaned = obj.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
        except Exception:
            cleaned = repr(obj)
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', cleaned)
        return cleaned[:100000] + "...(truncated)" if len(cleaned) > 100000 else cleaned
    if isinstance(obj, dict):
        return {str(k): to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_json_safe(i) for i in obj]
    try:
        return to_json_safe(dict(obj))
    except Exception:
        pass
    try:
        return str(obj)
    except Exception:
        return ""

def sanitize_result(result, max_len=100000):
    def _safe(obj):
        if obj is None or isinstance(obj, (bool, int, float)):
            return obj
        if isinstance(obj, str):
            try:
                cleaned = obj.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
            except Exception:
                cleaned = repr(obj)
            cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', cleaned)
            return cleaned[:max_len] + "...(truncated)" if len(cleaned) > max_len else cleaned
        if isinstance(obj, dict):
            return {str(k): _safe(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_safe(i) for i in obj]
        try:
            return _safe(dict(obj))
        except Exception:
            pass
        try:
            return str(obj)
        except Exception:
            return ""
    r = _safe(result) if isinstance(result, dict) else {"output": _safe(result)}
    return r


async def dispatch_tool(name, args, session_dir, ws=None):
    sd = session_dir
    if name == "run_command":
        return await tool_run_command_streaming(
            command=args.get("command", ""), cwd=args.get("cwd", "."),
            session_dir=sd, ws=ws
        )
    if name == "list_files":
        return tool_list_files(path=args.get("path", "."), session_dir=sd)
    if name == "read_file":
        return tool_read_file(path=args["path"], session_dir=sd)
    if name == "write_file":
        return tool_write_file(path=args["path"], content=args["content"], session_dir=sd)
    if name == "apply_diff":
        return tool_apply_diff(path=args["path"], diff=args["diff"], session_dir=sd)
    if name == "delete_file":
        return tool_delete_file(path=args["path"], session_dir=sd)
    if name == "search_files":
        return tool_search_files(query=args["query"], path=args.get("path", "."), session_dir=sd)
    if name == "search_in_file":
        return tool_search_in_file(path=args["path"], pattern=args["pattern"],
            session_dir=sd, context_lines=int(args.get("context_lines", 3)))
    if name == "copy_to_output":
        return tool_copy_to_output(path=args["path"], output_name=args.get("output_name", ""), session_dir=sd)
    if name == "web_search":
        return await tool_web_search(query=args["query"])
    if name == "fetch_url":
        return await tool_fetch_url(url=args["url"])
    return {"error": f"Unknown tool: {name}"}

# ---- Tool schemas ----
TOOL_DEFS = [types.Tool(function_declarations=[
    types.FunctionDeclaration(name="list_files",description="List files in session directory",
        parameters=types.Schema(type="OBJECT",properties={"path":types.Schema(type="STRING")},required=[])),
    types.FunctionDeclaration(name="read_file",
        description="Read file content. Large files are auto-chunked to 500 lines. Use start_line/end_line to read specific sections.",
        parameters=types.Schema(type="OBJECT",properties={
            "path":types.Schema(type="STRING"),
            "start_line":types.Schema(type="INTEGER",description="First line to read (1-indexed)"),
            "end_line":types.Schema(type="INTEGER",description="Last line to read (inclusive)")
        },required=["path"])),
    types.FunctionDeclaration(name="search_in_file",
        description="Search for a pattern/function name inside a file and return matching lines with context. Best for locating specific code in large files.",
        parameters=types.Schema(type="OBJECT",properties={
            "path":types.Schema(type="STRING"),
            "pattern":types.Schema(type="STRING",description="Text or regex to search for"),
            "context_lines":types.Schema(type="INTEGER",description="Lines of context around each match (default 3)")
        },required=["path","pattern"])),
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

AUTO_THINKING_KEYWORDS = [
    "debug", "fix", "error", "bug", "why", "implement", "design", "architect",
    "optimize", "refactor", "algorithm", "analyze", "compare", "explain",
    "バグ", "エラー", "修正", "実装", "設計", "最適化", "リファクタ", "アルゴリズム",
    "分析", "なぜ", "どうして", "仕組み", "デバッグ",
]

def should_think(message: str) -> bool:
    lower = message.lower()
    return any(kw in lower for kw in AUTO_THINKING_KEYWORDS)

def make_system_prompt(session_dir: Path, thinking_on: bool = False) -> str:
    rel = str(session_dir.relative_to(WORKSPACE))
    prompt = "You are Codesigner — a senior software engineer and AI coding agent running on a Linux VM.\n"
    prompt += "Session working directory: /workspace/" + rel + "\n"
    prompt += """All file paths are relative to this directory.

═══════════════════════════════════════════════════════
RESPONSE QUALITY — NON-NEGOTIABLE
═══════════════════════════════════════════════════════
- Write PRODUCTION-READY, COMPLETE, IMMEDIATELY RUNNABLE code. Always.
- NEVER output placeholder code: no "// TODO", no "# implement here", no "/* ... */",
  no "pass  # add logic", no "example only", no stub functions with empty bodies.
- NEVER say "here's a simplified version" or "for illustration purposes".
  If you are uncertain about a detail, make a reasonable implementation decision.
- Every function must have a real implementation. Every file must be complete.
- Code must handle errors, edge cases, and real-world inputs.
- If the user writes in Japanese, respond entirely in Japanese (except code identifiers).
- Begin your response IMMEDIATELY with the answer. No preamble, no meta-commentary.
- NEVER output lines like "User said:", "The user wrote:", "I should", "Role:", "Constraint:".

═══════════════════════════════════════════════════════
FILE EDITING — RULES
═══════════════════════════════════════════════════════
- ALWAYS read_file before editing an existing file. Never assume content.
- For LARGE FILES (read_file returns total_lines > 500):
  1. Use search_in_file to locate the specific function/section you need to edit.
  2. Use read_file with start_line/end_line to read only that section.
  3. Apply apply_diff targeting only those lines. Do NOT try to read the whole file at once.
- NEVER output file contents or diffs in chat. Use tools exclusively.
- NEVER output tool calls as JSON text — always use the function calling mechanism.
- When editing multiple files: call apply_diff/write_file for EVERY file before writing
  your summary. Do not stop after one file.
- After apply_diff or write_file succeeds, do NOT read_file to verify — trust the result.

═══════════════════════════════════════════════════════
PATCH FORMAT — USE V4A (preferred) or SEARCH/REPLACE
═══════════════════════════════════════════════════════
PREFERRED: V4A patch format (most reliable, handles special chars like ${}, backticks):

  *** Begin Patch
  *** Update File: path/to/file.js
  @@ functionName or class name context anchor
   context line (space prefix)
  -line to remove
  +line to add
   context line
  *** End Patch

  Rules for V4A:
  - @@ line: write the function/class name or a unique nearby string as the anchor.
    Do NOT use line numbers. The anchor locates the edit region by content matching.
  - Use 2-3 context lines (space prefix) around edits to anchor position.
  - Files containing ${...}, backtick strings, or special chars MUST use V4A or write_file.
  - One *** Begin Patch can contain multiple *** Update File sections.
  - To create: use *** Add File: path  then + lines.
  - To delete: use *** Delete File: path

ALTERNATIVE: SEARCH/REPLACE (for simple targeted edits without special chars):
  <<<<<<< SEARCH
  exact lines to find (must match file exactly, whitespace-tolerant)
  =======
  replacement lines
  >>>>>>> REPLACE

FALLBACK: write_file — use when creating new files or when the whole file needs rewriting.

═══════════════════════════════════════════════════════
DIFF FAILURE RECOVERY
═══════════════════════════════════════════════════════
- If apply_diff returns an error with file_content_preview: read the preview, then
  immediately call read_file to get the full current content, then retry with a
  corrected patch that matches the actual file content exactly.
- Never give up after one failure. Read → fix → retry.
- If a file contains lots of ${...} or backtick template literals, switch to write_file
  for that file (rewrite the whole thing) rather than patching.

═══════════════════════════════════════════════════════
OTHER TOOLS
═══════════════════════════════════════════════════════
- run_command: execute, test, build, install packages.
- web_search + fetch_url: look up docs, packages, APIs.
- copy_to_output: make a file downloadable by the user.
- User uploads are in the input/ folder.
- Be concise in summaries: list changed files and what changed.
"""
    return prompt

def auto_title(message: str) -> str:
    msg = message.replace("<thought off>", "").strip()
    words = msg.strip().split()[:8]
    title = " ".join(words)
    return title[:50] + ("…" if len(title) > 50 else "")

MODELS = ["gemini-3.1-flash-lite", "gemini-3.1-flash-lite-001"]


def clean_text(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # AIがツール呼び出しをJSONテキストとして出力してしまった場合を除去
    text = re.sub(r'\{"tool"\s*:.*?\}\s*', "", text, flags=re.DOTALL)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def _tool_status_label(tool: str, args: dict) -> str:
    """ツール名とargsから人間が読めるステータスラベルを生成"""
    if tool == "read_file":
        return f"読み込み中: {args.get('path', '')}"
    if tool == "write_file":
        return f"書き込み中: {args.get('path', '')}"
    if tool == "apply_diff":
        return f"パッチ適用中: {args.get('path', '')}"
    if tool == "run_command":
        cmd = args.get("command", "")[:60]
        return f"実行中: {cmd}"
    if tool == "web_search":
        return f"検索中: {args.get('query', '')[:50]}"
    if tool == "fetch_url":
        url = args.get("url", "")[:60]
        return f"取得中: {url}"
    if tool == "search_files":
        return f"ファイル検索中: {args.get('pattern', '')}"
    if tool == "delete_file":
        return f"削除中: {args.get('path', '')}"
    if tool == "copy_to_output":
        return f"出力コピー中: {args.get('path', '')}"
    return f"{tool} 実行中..."


# ---- Agent Loop with streaming ----
async def run_agent(user_message: str, history: list, ws: WebSocket, session_dir: Path, chat_id: str, thinking_level: str = "none"):
    _, client = rotator.next_client()
    messages = history + [types.Content(role="user", parts=[types.Part(text=user_message)])]
    loop = asyncio.get_event_loop()

    # diff失敗カウンタ（無限ループ防止）
    diff_fail_count = 0
    diff_fail_per_file: dict = {}  # ファイルごとの失敗カウント
    MAX_DIFF_RETRIES = 5  # 全体上限
    MAX_PER_FILE = 3       # 同一ファイルの上限

    for _ in range(30):
        response = None
        last_err = None
        tried = set()
        for model in MODELS:
            for attempt in range(len(rotator.keys)):
                try_key = rotator.keys[(rotator.index + attempt) % len(rotator.keys)]
                if (model, try_key) in tried:
                    continue
                tried.add((model, try_key))
                try_client = rotator.get_client(try_key)
                try:
                    accumulated_text = ""
                    tool_calls = []
                    candidate_content = None

                    if thinking_level == "auto":
                        thinking_on = should_think(user_message)
                    else:
                        thinking_on = thinking_level in ("on", "high")

                    thinking_config = types.ThinkingConfig(
                        thinking_budget=1024 if thinking_on else 0
                    )

                    gen_config = types.GenerateContentConfig(
                        system_instruction=make_system_prompt(session_dir, thinking_on=thinking_on),
                        tools=TOOL_DEFS,
                        tool_config=types.ToolConfig(
                            function_calling_config=types.FunctionCallingConfig(mode="AUTO")
                        ),
                        temperature=0.3,
                        thinking_config=thinking_config,
                    )

                    def _iter_stream():
                        return try_client.models.generate_content_stream(
                            model=model, contents=messages, config=gen_config
                        )

                    stream_iter = await loop.run_in_executor(None, _iter_stream)

                    # チャンクをキューに入れてasyncで消費（ブロッキング解放）
                    chunk_queue = asyncio.Queue()

                    def _read_chunks():
                        try:
                            for chunk in stream_iter:
                                loop.call_soon_threadsafe(chunk_queue.put_nowait, chunk)
                        finally:
                            loop.call_soon_threadsafe(chunk_queue.put_nowait, None)  # sentinel

                    import threading
                    t = threading.Thread(target=_read_chunks, daemon=True)
                    t.start()

                    announced_tools = set()  # ストリーム中に先行通知済みのツール

                    while True:
                        chunk = await chunk_queue.get()
                        if chunk is None:
                            break
                        if not chunk.candidates:
                            continue
                        candidate = chunk.candidates[0]
                        candidate_content = candidate.content
                        for part in candidate.content.parts:
                            if part.text:
                                cleaned = clean_text(part.text)
                                if cleaned:
                                    accumulated_text += cleaned
                                    await ws.send_json({"type": "stream", "content": cleaned})
                                    await asyncio.sleep(0)  # イベントループに制御を返す
                            if part.function_call:
                                tool_calls.append(part.function_call)
                                # ストリーム中にfunction_callが確定した瞬間 → agent_statusで先行通知
                                fc_name = part.function_call.name
                                fc_args = dict(part.function_call.args) if part.function_call.args else {}
                                tool_key = f"{fc_name}:{fc_args.get('path','')}{fc_args.get('command','')}{fc_args.get('query','')}"
                                if tool_key not in announced_tools:
                                    announced_tools.add(tool_key)
                                    label = _tool_status_label(fc_name, fc_args)
                                    await ws.send_json({"type": "agent_status", "label": label, "tool": fc_name, "args": fc_args})
                                    await asyncio.sleep(0)

                    if accumulated_text:
                        await ws.send_json({"type": "stream_end"})

                    response = {"text": accumulated_text, "tool_calls": tool_calls, "content": candidate_content}
                    rotator.index = rotator.keys.index(try_key) + 1
                    break
                except Exception as e:
                    err_str = str(e)
                    if any(x in err_str for x in ("503", "500", "UNAVAILABLE", "INTERNAL", "429", "RESOURCE_EXHAUSTED")):
                        last_err = e
                        await asyncio.sleep(1)
                        continue
                    raise
            if response is not None:
                break

        if response is None:
            raise last_err

        text = response["text"]
        tool_calls = response["tool_calls"]
        candidate_content = response["content"]

        if not tool_calls:
            if candidate_content:
                messages.append(candidate_content)
            if text:
                save_message(chat_id, "assistant", text)
            break

        if candidate_content:
            messages.append(candidate_content)
        tool_response_parts = []
        has_diff_error = False

        for fc in tool_calls:
            name, args = fc.name, dict(fc.args)
            # tool_callをDBに保存（リロード後復元用）
            save_message(chat_id, "tool", json.dumps({"tool": name, "args": to_json_safe(args)}), msg_type="tool_call")
            await ws.send_json({"type": "tool_call", "tool": name, "args": to_json_safe(args)})

            required, reason = needs_approval(name, args, session_dir)
            if required:
                call_id = str(id(fc))
                await ws.send_json({"type": "approval_request", "tool": name, "args": args, "call_id": call_id, "reason": reason})
                try:
                    raw = await asyncio.wait_for(ws.receive_text(), timeout=120)
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        msg = {}
                    if not (msg.get("type") == "approval" and msg.get("approved")):
                        result = {"cancelled": True}
                        await ws.send_json({"type": "tool_result", "tool": name, "result": result})
                        save_message(chat_id, "tool", json.dumps({"tool": name, "result": result}), msg_type="tool_result")
                        tool_response_parts.append(types.Part(
                            function_response=types.FunctionResponse(name=name, response=sanitize_result(result))
                        ))
                        continue
                except asyncio.TimeoutError:
                    result = {"error": "approval timeout"}
                    tool_response_parts.append(types.Part(
                        function_response=types.FunctionResponse(name=name, response=sanitize_result(result))
                    ))
                    continue

            result = await dispatch_tool(name, args, session_dir, ws=ws)

            # write_file / apply_diff 成功時: output/ へ自動コピー
            if result.get("success") and name in ("write_file", "apply_diff"):
                file_path = args.get("path", "")
                if file_path and not file_path.startswith("output/"):
                    src = session_dir / file_path
                    if src.exists() and src.is_file():
                        out_dir = session_dir / "output"
                        out_dir.mkdir(exist_ok=True)
                        dest = out_dir / src.name
                        shutil.copy2(src, dest)
                        result["output_copied"] = f"output/{src.name}"
            # tool_resultをDBに保存
            save_message(chat_id, "tool", json.dumps({"tool": name, "result": result}), msg_type="tool_result")

            # apply_diffのエラーはUIに出さず、AIへの内部フィードバックのみ
            if name == "apply_diff" and result.get("error") and result.get("error") != "already_applied":
                # UIには送らない（AIのhistoryには積む）
                # ファイルの現在の内容を自動でフィードバックに含める
                file_path = args.get("path", "")
                auto_content = ""
                if file_path:
                    try:
                        src = session_dir / file_path
                        if src.exists():
                            content = src.read_text(errors="replace")
                            auto_content = f"\n\nCurrent file content of {file_path}:\n```\n{content[:8000]}\n```\nPlease use this exact content to construct a correct patch."
                    except Exception:
                        pass
                # tool_response_partsにエラー+内容を積む（AIへのフィードバック）
                enriched = sanitize_result(result, max_len=100000)
                if auto_content and isinstance(enriched, dict):
                    enriched = dict(enriched)
                    enriched["current_file_content"] = auto_content
                tool_response_parts.append(types.Part(
                    function_response=types.FunctionResponse(name=name, response=enriched)
                ))
                # 失敗カウントを更新
                diff_fail_count += 1
                diff_fail_per_file[file_path] = diff_fail_per_file.get(file_path, 0) + 1
                has_diff_error = True
                continue  # 通常のtool_response_parts追加をスキップ
            else:
                await ws.send_json({"type": "tool_result", "tool": name, "result": result})
            max_len = 500000 if name == "read_file" else 100000
            tool_response_parts.append(types.Part(
                function_response=types.FunctionResponse(name=name, response=sanitize_result(result, max_len=max_len))
            ))

            # diff成功でカウンタリセット
            if name == "apply_diff" and result.get("success"):
                diff_fail_count = 0
                file_path = args.get("path", "")
                if file_path in diff_fail_per_file:
                    del diff_fail_per_file[file_path]

        messages.append(types.Content(role="user", parts=tool_response_parts))

        # diff失敗が多すぎる場合は終了
        if diff_fail_count >= MAX_DIFF_RETRIES:
            msg = "複数のdiff適用に繰り返し失敗しました。対象ファイルを確認し、write_fileで全体を書き直すアプローチを検討してください。"
            save_message(chat_id, "assistant", msg)
            await ws.send_json({"type": "stream", "content": msg})
            await ws.send_json({"type": "stream_end"})
            break

        # diff失敗の場合はAIに再試行させる（ループ継続、テキストは破棄）
        if has_diff_error:
            # ストリームで既に出ていたテキストはそのまま表示、ループ継続
            continue

        # ツール呼び出しがあった場合は継続（AIはまだ作業中）
        # → ループを続けてAIに次の行動をさせる

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

    # historyはuser/assistantのみ（tool系は除く）
    history = []
    for m in chat["messages"]:
        if m["msg_type"] in ("tool_call", "tool_result"):
            continue
        role = "user" if m["role"] == "user" else "model"
        history.append(types.Content(role=role, parts=[types.Part(text=m["content"])]))

    thinking_level = "none"

    await ws.send_json({"type": "ready", "chat": {
        "id": chat["id"], "title": chat["title"],
        "session_dir": chat["session_dir"],
        "messages": chat["messages"]
    }, "thinking_level": thinking_level})

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if data["type"] == "message":
                user_msg = data["content"]

                cmd = user_msg.strip().lower()
                if cmd in ("/thinking on", "/thinking high"):
                    thinking_level = "high"
                    await ws.send_json({"type": "thinking_level", "level": thinking_level})
                    await ws.send_json({"type": "system_msg", "content": "🧠 Thinking: ON (high)"})
                    continue
                elif cmd in ("/thinking off", "/thinking none"):
                    thinking_level = "none"
                    await ws.send_json({"type": "thinking_level", "level": thinking_level})
                    await ws.send_json({"type": "system_msg", "content": "⚡ Thinking: OFF"})
                    continue
                elif cmd in ("/thinking auto",):
                    thinking_level = "auto"
                    await ws.send_json({"type": "thinking_level", "level": thinking_level})
                    await ws.send_json({"type": "system_msg", "content": "🔄 Thinking: AUTO"})
                    continue
                elif cmd == "/thinking":
                    await ws.send_json({"type": "system_msg", "content": f"現在のThinkingモード: **{thinking_level}**\n`/thinking on` | `/thinking off` | `/thinking auto` で切り替え"})
                    continue

                save_message(chat_id, "user", user_msg)

                if len([m for m in chat["messages"] if m["role"] == "user"]) == 0:
                    title = auto_title(user_msg)
                    update_chat_title(chat_id, title)
                    await ws.send_json({"type": "title_updated", "title": title})

                history = await run_agent(user_msg, history, ws, session_dir, chat_id, thinking_level=thinking_level)
                # チャットを再取得してupdateする
                chat = get_chat(chat_id) or chat

            elif data["type"] in ("edit", "retry"):
                truncate_at = data.get("truncate_at")
                user_msg = data.get("content", "")

                if truncate_at is not None:
                    truncate_messages_from(chat_id, truncate_at)
                    history = history[:truncate_at]
                    chat["messages"] = chat["messages"][:truncate_at] if chat else []

                if not user_msg:
                    last_user = next((m for m in reversed(chat["messages"] if chat else []) if m["role"] == "user"), None)
                    user_msg = last_user["content"] if last_user else ""

                if not user_msg:
                    continue

                save_message(chat_id, "user", user_msg)
                history = await run_agent(user_msg, history, ws, session_dir, chat_id, thinking_level=thinking_level)
                chat = get_chat(chat_id) or chat

    except WebSocketDisconnect:
        pass
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            await ws.send_json({"type": "error", "content": str(e)})
        except Exception:
            pass

# ---- Auth ----
@app.post("/api/auth")
def api_auth(body: dict):
    if not LOGIN_PASSCODE:
        return {"success": True}
    if body.get("passcode") == LOGIN_PASSCODE:
        return {"success": True}
    raise HTTPException(401, "Invalid passcode")

@app.get("/api/auth/required")
def api_auth_required():
    return {"required": bool(LOGIN_PASSCODE)}

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
async def upload_file(file: UploadFile = File(...), chat_id: str = Form(""), path: str = Form("")):
    chat = get_chat(chat_id)
    if not chat: raise HTTPException(404)
    session_dir = WORKSPACE / chat["session_dir"]
    save_path = path or f"input/{file.filename or 'upload'}"
    dest = (session_dir / save_path).resolve()
    if not str(dest).startswith(str(session_dir.resolve())): raise HTTPException(400)
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    dest.write_bytes(content)
    return {"success": True, "size": len(content), "path": save_path, "filename": dest.name}

@app.get("/health")
def health():
    return {"status": "ok", "workspace": str(WORKSPACE), "keys": len(rotator.keys)}

if Path("../frontend/dist").exists():
    app.mount("/", StaticFiles(directory="../frontend/dist", html=True), name="static")
