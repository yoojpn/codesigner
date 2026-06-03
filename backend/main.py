import os, json, asyncio, subprocess, shutil, httpx, re, uuid, sqlite3, logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("codesigner")
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
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

# ---- Gemini API Key ローテーション ----
_GEMINI_KEYS = [
    k for k in [
        os.getenv("GEMINI_API_KEY_1"),
        os.getenv("GEMINI_API_KEY_2"),
        os.getenv("GEMINI_API_KEY_3"),
        os.getenv("GEMINI_API_KEY_4"),
    ] if k
]
if not _GEMINI_KEYS:
    raise RuntimeError("GEMINI_API_KEY_1 (or _2/_3/_4) not set in .env")
_gemini_key_index = 0

def get_next_gemini_key() -> str:
    global _gemini_key_index
    key = _GEMINI_KEYS[_gemini_key_index % len(_GEMINI_KEYS)]
    _gemini_key_index += 1
    return key

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
                "content_chunk": chunk, "path": path,
                "start_line": s+1, "end_line": e2,
                "total_lines": total_lines, "total_bytes": total_bytes,
                "WARNING": "THIS IS A PARTIAL READ. DO NOT use this content with write_file — that would destroy the rest of the file.",
                "note": f"Showing lines {s+1}-{e2} of {total_lines} total. Use start_line/end_line to read other sections."
            }
        # 大きいファイルは最初の500行のみ返し、残りは範囲指定で読むよう案内
        CHUNK_LINES = 500
        if total_lines > CHUNK_LINES:
            chunk = "".join(lines[:CHUNK_LINES])
            return {
                "content_chunk": chunk, "path": path,
                "start_line": 1, "end_line": CHUNK_LINES,
                "total_lines": total_lines, "total_bytes": total_bytes,
                "WARNING": "THIS IS A PARTIAL READ. DO NOT use this content with write_file — that would destroy the rest of the file. Use search_in_file + read_file(start_line,end_line) to find and edit specific sections only. Use apply_diff or sed via run_command for edits.",
                "note": f"File has {total_lines} lines ({total_bytes} bytes). Showing lines 1-{CHUNK_LINES} only. Use start_line/end_line to read other sections."
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
        # 正規表現が無効な場合は固定文字列マッチにフォールバック
        try:
            _re.compile(pattern, _re.IGNORECASE)
            use_regex = True
        except _re.error:
            use_regex = False
        results = []
        for i, line in enumerate(lines):
            if pattern.lower() in line.lower() or (use_regex and _re.search(pattern, line, _re.IGNORECASE)):
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
    if path.startswith("input/") or path.startswith("input\\"):
        return {"error": "WRITE BLOCKED: input/ folder is read-only. Copy the file to the working directory first with: run_command cp input/filename.html filename.html"}
    t, e = _guard(path, session_dir)
    if e: return {"error": e}
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_text(content)
    return {"success": True, "path": path, "bytes": len(content)}

def tool_apply_diff(path, diff, *, session_dir):
    if path.startswith("input/") or path.startswith("input\\"):
        return {"error": "DIFF BLOCKED: input/ folder is read-only. Copy the file first with: run_command cp input/filename.html filename.html"}
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

        def _line_eq(a, b, mode):
            a = a.rstrip("\n\r")
            b = b.rstrip("\n\r")
            if mode == 1: return a == b
            if mode == 2: return a.strip() == b.strip()
            if mode == 3: return normalize_line(a) == normalize_line(b)
            return False

        for mode in (1, 2, 3):
            for i in search_order:
                if all(i+k < len(file_lines) and _line_eq(file_lines[i+k], search_lines[k], mode)
                       for k in range(n)):
                    return i

        # 段階4: hintがある場合、最初の行だけ一致すれば位置を返す（アンカー行のみマッチ）
        if hint is not None:
            for mode in (1, 2, 3):
                for i in search_order[:20]:  # hint周辺20行に限定
                    if i < len(file_lines) and _line_eq(file_lines[i], search_lines[0], mode):
                        return i
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
                    anchor_found = False
                    if anchor_text:
                        anchor_stripped = anchor_text.strip()
                        for li, fl in enumerate(result_lines):
                            if anchor_stripped and anchor_stripped in fl:
                                hint = li
                                anchor_found = True
                                break
                        # アンカーが見つからない場合: 先頭のcontext/search行で補完検索
                        if not anchor_found and search_lines:
                            first_search = search_lines[0].strip()
                            if first_search:
                                for li, fl in enumerate(result_lines):
                                    if first_search in fl:
                                        hint = li
                                        break
                    best_pos = _find_block(result_lines, search_lines, hint=hint)
                    if best_pos is None:
                        # アンカーが見つからなかった場合は専用エラーメッセージ
                        if anchor_text and not anchor_found:
                            anchor_hint = ""
                            # 近い行を探してヒント提示
                            ak = anchor_text.strip()[:40]
                            for li, fl in enumerate(result_lines):
                                if ak[:20] in fl:
                                    anchor_hint = f"\nNOTE: anchor line not found in file. Similar line at {li+1}: {fl.strip()[:100]!r}"
                                    break
                            if not anchor_hint:
                                anchor_hint = f"\nNOTE: anchor '{anchor_text.strip()[:60]}' does not exist in the file yet. Use an EXISTING adjacent line as anchor, not the line being inserted."
                            err_msg = f"V4A hunk '{anchor_text[:60]}': anchor not found.{anchor_hint}" + _make_hint(search_lines, result_lines)
                        else:
                            err_msg = f"V4A hunk '{anchor_text[:60]}': match failed" + _make_hint(search_lines, result_lines)
                        hunk_errors.append(err_msg)
                        # 失敗時は即座にエラー返却（後続hunkが行ずれで連鎖失敗するのを防ぐ）
                        marker.unlink(missing_ok=True)
                        return {"error": "V4A patch failed:\n" + "\n".join(hunk_errors),
                                "hint": "IMPORTANT: The @@ anchor must be an EXISTING line already in the file (the line BEFORE or AFTER the insertion point). Do NOT use a line you are adding as the anchor. Use read_file to see exact current content, then retry with a correct anchor."}
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
            # 変更が0行でファイルも同じ場合は「実際には何も変わっていない」とLLMに伝える
            if lines_added == 0 and lines_removed == 0 and result_text == original:
                marker.unlink(missing_ok=True)
                return {"error": "V4A patch applied but made NO changes to the file. "
                        "Your diff may have context lines only, or the +/- lines did not match any content. "
                        "Use read_file to verify current content, then retry with correct SEARCH content."}
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
    # 必須引数チェック（欠落時はエラーを返してクラッシュを防ぐ）
    _required = {
        "read_file": ["path"], "write_file": ["path", "content"],
        "apply_diff": ["path", "diff"], "delete_file": ["path"],
        "search_files": ["query"], "search_in_file": ["path", "pattern"],
        "copy_to_output": ["path"], "web_search": ["query"], "fetch_url": ["url"],
    }
    for req in _required.get(name, []):
        if req not in args:
            hint = ""
            if name == "apply_diff" and req == "diff":
                hint = " IMPORTANT: The 'diff' argument must contain the full patch text (*** Begin Patch ... *** End Patch). Do NOT call apply_diff without the diff content."
            elif name == "search_in_file" and req == "pattern":
                hint = " The 'pattern' argument must be a regex or string to search for."
            return {"error": f"{name} called without required argument '{req}'.{hint} Please retry with all required arguments."}

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

def auto_title(message: str) -> str:
    msg = message.replace("<thought off>", "").strip()
    words = msg.strip().split()[:8]
    title = " ".join(words)
    return title[:50] + ("…" if len(title) > 50 else "")

def clean_text(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # AIがツール呼び出しをJSONテキストとして出力してしまった場合を除去
    text = re.sub(r'\{"tool"\s*:.*?\}\s*', "", text, flags=re.DOTALL)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # stallingフレーズを除去（行単位）
    _stall_patterns = [
        r"^少々お待ちください.*$",
        r"^しばらくお待ちください.*$",
        r"^確認します.*$",
        r"^調べます.*$",
        r"^確認しています.*$",
        r"^準備しています.*$",
        r"^ファイルを確認.*必要な実装.*$",
        r"^修正が必要.*現在.*確認.*$",
    ]
    for pat in _stall_patterns:
        text = re.sub(pat, "", text, flags=re.MULTILINE)
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


# ---- Agent Loop: Claude Code CLI subprocess (NDJSON) ----
async def run_agent(user_message: str, history: list, ws: WebSocket, session_dir: Path, chat_id: str, thinking_level: str = "none"):
    """
    Claude Code CLIをサブプロセスとして起動し、NDJSONストリームをWebSocketに流す。
    OpenRouter BYOK経由でGemma 4 31B（google/gemma-4-31b-it:free）を使用。
    thinking漏れはOpenRouter側でreasoning_detailsに分離されるためcontentには混入しない。
    """
    openrouter_url = "https://openrouter.ai/api/v1"
    claude_bin = shutil.which("claude") or os.path.expanduser("~/.npm-global/bin/claude")

    if not claude_bin or not os.path.exists(claude_bin):
        # フォールバック: npm globalパスを探す
        for candidate in [
            "/usr/local/bin/claude",
            "/usr/bin/claude",
            os.path.expanduser("~/.local/bin/claude"),
        ]:
            if os.path.exists(candidate):
                claude_bin = candidate
                break
        else:
            await ws.send_json({"type": "error", "content": "claude CLI not found. Run: npm install -g @anthropic-ai/claude-code"})
            await ws.send_json({"type": "done"})
            return history

    litellm_url = os.getenv("LITELLM_BASE_URL", "http://localhost:4000")
    litellm_key = os.getenv("LITELLM_MASTER_KEY", "sk-litellm")
    gemini_key = get_next_gemini_key()
    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = litellm_url
    env["ANTHROPIC_API_KEY"] = litellm_key
    env["ANTHROPIC_AUTH_TOKEN"] = litellm_key
    env["GEMINI_API_KEY"] = gemini_key
    env["GOOGLE_API_KEY"] = gemini_key
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"

    # --print + --verbose + --output-format stream-json の3つが必須
    # --input-format stream-json は --print と併用する場合のみ有効
    # --cwd は存在しないフラグ → create_subprocess_execのcwd引数で渡す
    # --no-update-check は存在しないフラグ → 削除
    cmd = [
        claude_bin,
        "-p", user_message,
        "--output-format", "stream-json",
        "--verbose",                           # stream-jsonに必須
        "--include-partial-messages",          # トークンレベルのストリーミングに必須
        "--permission-mode", "acceptEdits",   # ファイル編集を自動承認
        "--allowedTools", "Bash,Edit,Glob,Grep,LS,Read,Write",
    ]

    logger.info(f"[ClaudeCode] spawn: cwd={session_dir}")

    _file_snapshots: dict = {}

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(session_dir),  # --cwdフラグではなくここで指定
            limit=10 * 1024 * 1024,  # 10MB: Gemma応答が大きいためデフォルト64KBを拡張
        )

        assistant_text = ""
        # tool_useとtool_resultを紐づけるためのキャッシュ
        _pending_tool_uses: dict = {}  # id -> {name, input}

        async def read_stdout():
            nonlocal assistant_text
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                raw_line = line.decode("utf-8", errors="replace").strip()
                if not raw_line:
                    continue
                try:
                    msg = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                mtype = msg.get("type", "")

                if mtype == "stream_event":
                    # --include-partial-messagesで流れてくるトークンデルタ
                    event = msg.get("event", {})
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            txt = delta.get("text", "")
                            if txt:
                                assistant_text += txt
                                await ws.send_json({"type": "stream", "content": txt})

                elif mtype == "assistant":
                    # アシスタントのテキスト + ツール呼び出し宣言
                    content = msg.get("message", {}).get("content", [])
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type", "")
                        if btype == "text":
                            txt = clean_text(block.get("text", ""))
                            if txt:
                                assistant_text += txt
                                await ws.send_json({"type": "stream", "content": txt})
                        elif btype == "tool_use":
                            tool_id = block.get("id", "")
                            tool_name = block.get("name", "")
                            tool_input = block.get("input", {})
                            # tool_idでキャッシュ（後でtool_resultと紐づけるため）
                            _pending_tool_uses[tool_id] = {"name": tool_name, "input": tool_input}
                            label = _tool_status_label(tool_name, tool_input)
                            await ws.send_json({"type": "agent_status", "label": label, "tool": tool_name, "args": tool_input})
                            save_message(chat_id, "tool", json.dumps({"tool": tool_name, "args": to_json_safe(tool_input)}), msg_type="tool_call")

                elif mtype == "user":
                    # Claude Code が tool_result を user メッセージとして返す
                    content = msg.get("message", {}).get("content", [])
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_result":
                            tool_id = block.get("tool_use_id", "")
                            tool_info = _pending_tool_uses.get(tool_id, {})
                            tool_name = tool_info.get("name", "unknown")
                            tool_input = tool_info.get("input", {})
                            raw_content = block.get("content", "")
                            if isinstance(raw_content, list):
                                result_text = " ".join(b.get("text", "") for b in raw_content if isinstance(b, dict))
                            else:
                                result_text = str(raw_content)
                            result = {"output": result_text}
                            await ws.send_json({"type": "tool_result", "tool": tool_name, "result": result})
                            save_message(chat_id, "tool", json.dumps({"tool": tool_name, "result": result}), msg_type="tool_result")
                            # Write/Editの場合はdiffを送信
                            if tool_name in ("Write", "Edit"):
                                fpath_str = tool_input.get("file_path") or tool_input.get("path", "")
                                if fpath_str:
                                    await _send_diff_for_file(fpath_str, session_dir, _file_snapshots, ws)

                elif mtype == "result":
                    # 最終結果（type="result"）
                    is_error = msg.get("is_error", False)
                    final = msg.get("result", "")
                    if final and isinstance(final, str) and not is_error:
                        cleaned = clean_text(final)
                        # assistantメッセージで既にストリームしていない場合のみ送信
                        if cleaned and cleaned not in assistant_text:
                            assistant_text = cleaned
                            await ws.send_json({"type": "stream", "content": cleaned})
                    if is_error and final:
                        logger.error(f"[ClaudeCode] result error: {final}")
                        await ws.send_json({"type": "error", "content": final})
                    await ws.send_json({"type": "stream_end"})

                elif mtype == "system":
                    # init情報（無視）
                    logger.info(f"[ClaudeCode] system init model={msg.get('model','')} session={msg.get('session_id','')[:8]}")

                else:
                    logger.debug(f"[ClaudeCode] unknown msg type={mtype}")

        async def read_stderr():
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                logger.warning(f"[ClaudeCode stderr] {line.decode('utf-8', errors='replace').rstrip()}")

        await asyncio.gather(read_stdout(), read_stderr())
        await proc.wait()

        if assistant_text:
            save_message(chat_id, "assistant", assistant_text)

        logger.info(f"[ClaudeCode] done rc={proc.returncode}")

    except Exception as e:
        logger.error(f"[ClaudeCode] exception: {e}")
        await ws.send_json({"type": "error", "content": str(e)})

    await ws.send_json({"type": "done"})

    # historyはClaude Code側が管理するため、呼び出し元に空リストを返す（互換のため）
    return []


async def _send_diff_for_file(fpath_str: str, session_dir: Path, snapshots: dict, ws: WebSocket):
    """ファイル編集後にdiff_resultをWebSocketに送信する"""
    import difflib
    try:
        fpath = (session_dir / fpath_str).resolve()
        if not fpath.exists():
            return
        after = fpath.read_text(errors="replace")
        fkey = str(fpath)
        before = snapshots.get(fkey, "")
        if before == after:
            return
        before_lines = before.splitlines(keepends=True)
        after_lines = after.splitlines(keepends=True)
        udiff = list(difflib.unified_diff(before_lines, after_lines,
                                           fromfile=f"a/{fpath_str}", tofile=f"b/{fpath_str}", n=3))
        added = sum(1 for l in udiff if l.startswith('+') and not l.startswith('+++'))
        removed = sum(1 for l in udiff if l.startswith('-') and not l.startswith('---'))
        await ws.send_json({
            "type": "diff_result",
            "path": fpath_str,
            "added": added,
            "removed": removed,
            "diff": "".join(udiff),
        })
        # 次のdiff計算の基点を更新
        snapshots[fkey] = after
    except Exception as e:
        logger.warning(f"[Diff] failed for {fpath_str}: {e}")

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

    # Claude Code CLIはステートレス（セッション内の会話履歴を自身で管理）
    # historyは互換性のため空リストとして保持
    history = []

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
    # UIプレビュー用: チャンク制限なしで全文返す
    session_dir = WORKSPACE / chat["session_dir"]
    t, e = _guard(path, session_dir, allow_outside=True)
    if e: raise HTTPException(400, e)
    if not t.exists(): raise HTTPException(404)
    try:
        return {"content": t.read_text(errors="replace"), "path": path}
    except Exception as ex:
        raise HTTPException(500, str(ex))

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
    return {"status": "ok", "workspace": str(WORKSPACE), "model": "google/gemma-4-31b-it:free", "via": "openrouter"}

if Path("../frontend/dist").exists():
    app.mount("/", StaticFiles(directory="../frontend/dist", html=True), name="static")
