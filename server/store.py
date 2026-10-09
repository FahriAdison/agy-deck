"""AGY Web v3 - file store + conversation history.

File ops are jailed to WORKSPACE_DIR (symlinks resolved). Saving checks
mtime so concurrent edits don't silently clobber each other.
History reuses agy CLI's own SQLite + transcript logs (read-only).
"""
import json
import mimetypes
import os
import shutil
import sqlite3
import time

from . import config

WORKSPACE_DIR = config.WORKSPACE_DIR
TEXT_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".md", ".txt",
             ".sh", ".bash", ".css", ".html", ".xml", ".yml", ".yaml",
             ".toml", ".ini", ".cfg", ".env", ".log", ".c", ".h", ".cpp",
             ".java", ".go", ".rs", ".sql", ".vue", ".svelte"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}
MAX_PREVIEW = 2 * 1024 * 1024


class PathError(Exception):
    pass


def safe_path(rel_path):
    """Resolve rel_path inside the workspace; raise PathError on escape."""
    rel = (rel_path or "").strip().lstrip("/")
    full = os.path.realpath(os.path.join(WORKSPACE_DIR, rel))
    ws = os.path.realpath(WORKSPACE_DIR)
    if full != ws and not full.startswith(ws + os.sep):
        raise PathError("path keluar dari workspace")
    return full


def list_dir(rel_path=""):
    target = safe_path(rel_path)
    if not os.path.isdir(target):
        raise PathError("bukan folder")
    items = []
    for entry in sorted(os.listdir(target)):
        if entry.startswith(".") and entry not in (".env", ".gitignore", ".gemini"):
            continue
        full = os.path.join(target, entry)
        try:
            st = os.stat(full)
        except OSError:
            continue
        is_dir = os.path.isdir(full)
        _, ext = os.path.splitext(entry)
        items.append({
            "name": entry,
            "path": os.path.relpath(full, WORKSPACE_DIR),
            "is_dir": is_dir,
            "size": 0 if is_dir else st.st_size,
            "mtime": st.st_mtime,
            "mtime_str": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            "ext": ext.lower(),
        })
    rel_current = os.path.relpath(target, WORKSPACE_DIR)
    crumbs = [{"name": "workspace", "path": ""}]
    if rel_current != ".":
        cum = ""
        for part in rel_current.split(os.sep):
            cum = os.path.join(cum, part)
            crumbs.append({"name": part, "path": cum})
    parent = None
    if rel_current != ".":
        pr = os.path.relpath(os.path.dirname(target), WORKSPACE_DIR)
        parent = "" if pr == "." else pr
    return {
        "current_path": "" if rel_current == "." else rel_current,
        "parent_path": parent,
        "breadcrumbs": crumbs,
        "items": items,
    }


def read_file(rel_path):
    full = safe_path(rel_path)
    if not os.path.isfile(full):
        raise PathError("file tidak ditemukan")
    size = os.path.getsize(full)
    _, ext = os.path.splitext(full)
    ext = ext.lower()
    mime, _ = mimetypes.guess_type(full)
    if ext in IMAGE_EXTS:
        return {"kind": "image", "path": os.path.relpath(full, WORKSPACE_DIR),
                "name": os.path.basename(full), "size": size,
                "mime": mime or "image/png"}
    if size > MAX_PREVIEW:
        raise PathError(f"file terlalu besar (>{MAX_PREVIEW // 1048576}MB)")
    with open(full, "r", encoding="utf-8", errors="strict") as f:
        content = f.read()
    st = os.stat(full)
    return {"kind": "text", "path": os.path.relpath(full, WORKSPACE_DIR),
            "name": os.path.basename(full), "content": content,
            "size": size, "mtime": st.st_mtime,
            "mime": mime or "text/plain"}


def save_file(rel_path, content, expected_mtime=None):
    full = safe_path(rel_path)
    if full == os.path.realpath(WORKSPACE_DIR):
        raise PathError("tidak boleh menimpa workspace root")
    if os.path.exists(full) and expected_mtime:
        actual = os.path.getmtime(full)
        if abs(actual - float(expected_mtime)) > 1.0:
            raise PathError("CONFLICT: file berubah di luar editor — muat ulang dulu")
    os.makedirs(os.path.dirname(full), exist_ok=True)
    # atomic write via temp file
    tmp = full + ".tmp-agyweb"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, full)
    st = os.stat(full)
    return {"ok": True, "path": os.path.relpath(full, WORKSPACE_DIR),
            "mtime": st.st_mtime, "size": st.st_size}


def create_item(rel_path, is_dir=False):
    full = safe_path(rel_path)
    if is_dir:
        os.makedirs(full, exist_ok=True)
    else:
        if os.path.exists(full):
            raise PathError("sudah ada")
        os.makedirs(os.path.dirname(full), exist_ok=True)
        open(full, "w").close()
    return {"ok": True, "path": os.path.relpath(full, WORKSPACE_DIR)}


def delete_item(rel_path):
    full = safe_path(rel_path)
    if full == os.path.realpath(WORKSPACE_DIR):
        raise PathError("tidak boleh menghapus workspace root")
    if not os.path.exists(full):
        raise PathError("tidak ditemukan")
    if os.path.isdir(full) and not os.path.islink(full):
        shutil.rmtree(full)
    else:
        os.remove(full)
    return {"ok": True}


# --------------------------------------------------------------------------
# Conversation history (agy CLI's own storage, read-only)
# --------------------------------------------------------------------------

def _db_candidates():
    return [
        os.path.expanduser("~/.gemini/antigravity-cli/conversation_summaries.db"),
        "/public/.gemini/antigravity-cli/conversation_summaries.db",
    ]


def _brain_candidates():
    return [
        os.path.expanduser("~/.gemini/antigravity-cli/brain"),
        "/public/.gemini/antigravity-cli/brain",
    ]


def _first_existing(paths):
    for p in paths:
        if os.path.exists(p):
            return p
    return paths[0]


def fetch_conversations(limit=60):
    db = _first_existing(_db_candidates())
    if not os.path.exists(db):
        return []
    try:
        conn = sqlite3.connect(db, timeout=5.0)
    except Exception:
        return []
    try:
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT conversation_id, title, preview, last_modified_time, step_count"
            " FROM conversation_summaries ORDER BY last_modified_time DESC LIMIT ?",
            (limit,)).fetchall()
        out = []
        for cid, title, preview, modified, steps in rows:
            if not title or not title.strip():
                title = (preview or "")[:45] or f"sesi {cid[:8]}"
            out.append({"id": cid, "title": title, "preview": preview or "",
                        "updated": modified or "", "steps": steps or 0})
        return out
    except Exception:
        return []
    finally:
        conn.close()


def delete_conversation(cid):
    db = _first_existing(_db_candidates())
    if not os.path.exists(db):
        return False
    try:
        conn = sqlite3.connect(db, timeout=5.0)
        conn.execute("DELETE FROM conversation_summaries WHERE conversation_id = ?", (cid,))
        conn.commit()
        conn.close()
    except Exception:
        return False
    brain = _first_existing(_brain_candidates())
    cdir = os.path.join(brain, cid)
    if os.path.isdir(cdir):
        shutil.rmtree(cdir, ignore_errors=True)
    return True


def rename_conversation(cid, title):
    db = _first_existing(_db_candidates())
    if not os.path.exists(db):
        return False
    try:
        conn = sqlite3.connect(db, timeout=5.0)
        conn.execute("UPDATE conversation_summaries SET title = ? WHERE conversation_id = ?",
                     (title, cid))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def parse_transcript(cid):
    """Transcript JSONL -> [{role, content, thinking, tools, time}]."""
    brain = _first_existing(_brain_candidates())
    tpath = os.path.join(brain, cid, ".system_generated", "logs", "transcript.jsonl")
    if not os.path.exists(tpath):
        return []
    messages, current = [], None
    try:
        with open(tpath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                stype, source = data.get("type"), data.get("source")
                if stype == "USER_INPUT":
                    if current:
                        messages.append(current)
                        current = None
                    content = data.get("content", "")
                    if "<USER_REQUEST>" in content:
                        try:
                            content = content.split("<USER_REQUEST>")[1].split("</USER_REQUEST>")[0].strip()
                        except Exception:
                            pass
                    messages.append({"role": "user", "content": content,
                                     "time": data.get("created_at", "")})
                elif stype == "PLANNER_RESPONSE" and source == "MODEL":
                    content = data.get("content", "")
                    thinking = data.get("thinking", "")
                    tools = [{"name": tc.get("name", "tool"),
                              "summary": (tc.get("args") or {}).get("toolSummary", ""),
                              "action": (tc.get("args") or {}).get("toolAction", "")}
                             for tc in data.get("tool_calls", [])]
                    if not current:
                        current = {"role": "assistant", "content": content,
                                   "thinking": thinking, "tools": tools,
                                   "time": data.get("created_at", "")}
                    else:
                        if content:
                            current["content"] += ("\n\n" + content if current["content"] else content)
                        if thinking:
                            current["thinking"] += ("\n" + thinking if current.get("thinking") else thinking)
                        current["tools"].extend(tools)
        if current:
            messages.append(current)
    except Exception:
        pass
    return messages
