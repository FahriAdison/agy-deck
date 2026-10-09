"""AGY Web v3 - FastAPI application.

- Static UI served from ./static
- Token-authenticated REST (/api/*) and WebSocket (/ws)
- WebSocket protocol (client -> server):
    {"type":"auth","token":...}
    {"type":"chat","prompt":...,"model":...,"conversation_id":...,
     "yolo":true,"new_chat":false}
    {"type":"abort"}
    {"type":"approve","approval_id":...,"decision":"allow"|"deny"}
    {"type":"term_start","cols":..,"rows":..}
    {"type":"term_input","data":"..."}        (raw string)
    {"type":"term_resize","cols":..,"rows":..}
    {"type":"models"}  -> refresh model list
    {"type":"ping"}
- Server -> client: ready, session, turn_start, delta, thinking, tool,
  approval_request, turn_end, error, log, term_output, term_exit, models, pong.
"""
import asyncio
import base64
import json
import os
import queue
import subprocess
import threading
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Depends, HTTPException
from fastapi.responses import JSONResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import config, agy, store
from .terminal import PtySession

AUTH_TOKEN = config.get_auth_token()

app = FastAPI(title="AGY Web v3", docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def _token_ok(token):
    return bool(token) and token == AUTH_TOKEN


async def require_auth(request: Request):
    token = request.headers.get("x-auth-token") or request.query_params.get("token")
    if not _token_ok(token):
        raise HTTPException(status_code=401, detail="unauthorized")


# --------------------------------------------------------------------------
# Event hub: session thread-queue -> per-websocket asyncio queues
# --------------------------------------------------------------------------

class Hub:
    def __init__(self):
        self.queues = set()          # asyncio.Queue per connected ws
        self.turn = None             # {"started": ts, "deadline": ts}
        self._lock = threading.Lock()

    def register(self, q):
        self.queues.add(q)

    def unregister(self, q):
        self.queues.discard(q)

    def broadcast(self, msg):
        for q in list(self.queues):
            try:
                q.put_nowait(msg)
            except Exception:
                pass

    def turn_start(self):
        with self._lock:
            self.turn = {"started": time.time(),
                         "deadline": time.time() + config.TURN_TIMEOUT}

    def turn_end(self):
        with self._lock:
            self.turn = None

    def turn_timed_out(self):
        with self._lock:
            return self.turn is not None and time.time() > self.turn["deadline"]


hub = Hub()


async def _dispatcher():
    """Drain the session manager's thread queue, forward as WS messages."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            ev = await loop.run_in_executor(None, agy.manager.events.get, True, 0.5)
        except Exception:
            ev = None
        if ev is None:
            if hub.turn_timed_out():
                _do_abort("TIMEOUT")
            continue
        t = ev.get("t")
        if t == "init":
            hub.broadcast({"type": "session",
                           "conversation_id": ev.get("conversation_id", ""),
                           "permission_mode": ev.get("permission_mode", ""),
                           "session_kind": agy.manager.session.kind
                           if agy.manager.session else "?"})
        elif t == "delta":
            hub.broadcast({"type": "delta", "text": ev.get("text", "")})
        elif t == "thinking":
            hub.broadcast({"type": "thinking", "text": ev.get("text", "")})
        elif t == "tool":
            hub.broadcast({"type": "tool", "name": ev.get("name"),
                           "state": ev.get("state"), "info": ev.get("info", {}),
                           "summary": ev.get("summary", "")})
        elif t == "approval_request":
            hub.broadcast({"type": "approval_request",
                           "approval_id": ev.get("approval_id"),
                           "tool": ev.get("tool"), "summary": ev.get("summary", "")})
        elif t == "turn_end":
            hub.turn_end()
            hub.broadcast({"type": "turn_end",
                           "conversation_id": ev.get("conversation_id", ""),
                           "status": ev.get("status", "SUCCESS"),
                           "response": ev.get("response", ""),
                           "usage": ev.get("usage", {}),
                           "duration": ev.get("duration", 0),
                           "error": ev.get("error", "")})
        elif t == "error":
            hub.broadcast({"type": "error", "message": ev.get("message", "")})
        elif t == "stderr_line":
            hub.broadcast({"type": "log", "text": ev.get("text", "")})
        elif t == "proc_exit":
            # process died mid-turn without a result event
            with hub._lock:
                active = hub.turn is not None
            if active:
                hub.turn_end()
                hub.broadcast({"type": "turn_end", "status": "ERROR",
                               "conversation_id": "",
                               "error": f"proses agy mati (exit {ev.get('code')})",
                               "usage": {}, "duration": 0, "response": ""})


def _do_abort(status):
    agy.manager.abort()
    hub.turn_end()
    hub.broadcast({"type": "turn_end", "status": status, "conversation_id": "",
                   "response": "", "usage": {}, "duration": 0,
                   "error": "dihentikan" if status == "ABORTED" else "timeout"})


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_dispatcher())


@app.on_event("shutdown")
def _shutdown():
    agy.manager.close()


# --------------------------------------------------------------------------
# REST API
# --------------------------------------------------------------------------

@app.get("/api/status", dependencies=[Depends(require_auth)])
def api_status():
    version = ""
    if config.AGY_BIN and not config.AGY_MOCK:
        try:
            r = subprocess.run([config.AGY_BIN, "--version"], capture_output=True,
                               text=True, timeout=10)
            version = (r.stdout or r.stderr or "").strip().split("\n")[0][:40]
        except Exception:
            pass
    return {
        "status": "online",
        "mock": config.AGY_MOCK,
        "agy_version": version,
        "agy_bin": config.AGY_BIN or "",
        "workspace": config.WORKSPACE_DIR,
        "turn_active": hub.turn is not None,
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


_models_cache = {"at": 0, "models": []}
_FALLBACK_MODELS = [
    {"id": "gemini-3.8-flash-high", "name": "Gemini 3.8 Flash (High)"},
    {"id": "gemini-3.1-pro-high", "name": "Gemini 3.1 Pro (High)"},
    {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6 (Thinking)"},
    {"id": "gpt-oss-120b-medium", "name": "GPT-OSS 120B (Medium)"},
]


def _discover_models():
    now = time.time()
    if now - _models_cache["at"] < 300 and _models_cache["models"]:
        return _models_cache["models"]
    models = []
    if config.AGY_MOCK:
        models = [{"id": m["id"], "name": m["name"] + " (mock)"} for m in _FALLBACK_MODELS]
    elif config.AGY_BIN:
        try:
            r = subprocess.run([config.AGY_BIN, "models"], capture_output=True,
                               text=True, timeout=25, cwd=config.WORKSPACE_DIR,
                               env=config.agy_env())
            out = (r.stdout or "").strip()
            if out.startswith("[") or out.startswith("{"):
                data = json.loads(out)
                items = data if isinstance(data, list) else data.get("models", [])
                for it in items:
                    if isinstance(it, str):
                        models.append({"id": it, "name": it})
                    elif isinstance(it, dict):
                        mid = it.get("id") or it.get("name") or ""
                        if mid:
                            models.append({"id": mid,
                                           "name": it.get("display_name") or it.get("name") or mid})
            else:
                for line in out.split("\n"):
                    line = line.strip().lstrip("*-• ").strip()
                    if line and len(line) < 80 and " " not in line[:1]:
                        models.append({"id": line, "name": line})
        except Exception:
            pass
    if not models:
        models = _FALLBACK_MODELS
    _models_cache.update({"at": now, "models": models})
    return models


@app.get("/api/settings", dependencies=[Depends(require_auth)])
def api_settings():
    return {"gemini_key_set": config.gemini_key_configured(),
            "mock": config.AGY_MOCK}


@app.post("/api/settings/key", dependencies=[Depends(require_auth)])
async def api_settings_key(request: Request):
    """Save the user's own Gemini API key (from their Google account).

    The key goes straight into .env (chmod 600) and is never returned
    or logged. It is injected as env var into the agy subprocess only.
    """
    body = await request.json()
    key = (body.get("key") or "").strip()
    if len(key) < 20:
        raise HTTPException(400, "key tidak valid")
    config.set_env_value("GEMINI_API_KEY", key)
    return {"ok": True, "gemini_key_set": True}


# --------------------------------------------------------------------------
# Google OAuth login (headless device-code flow for agy)
# --------------------------------------------------------------------------

@app.get("/api/health")
def api_health():
    """Unauthenticated health check (for hosts like Render + uptime pingers)."""
    return {"ok": True}


@app.get("/api/auth/status", dependencies=[Depends(require_auth)])
def api_auth_status():
    from . import auth_google
    if auth_google.has_google_auth():
        method = "google"
    elif config.gemini_key_configured():
        method = "key"
    else:
        method = "none"
    return {"method": method, "mock": config.AGY_MOCK}


@app.post("/api/auth/google/start", dependencies=[Depends(require_auth)])
def api_google_start():
    from . import auth_google
    try:
        login_id = auth_google.start_login()
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return {"login_id": login_id}


@app.get("/api/auth/google/poll", dependencies=[Depends(require_auth)])
def api_google_poll(login_id: str):
    from . import auth_google
    return auth_google.poll_login(login_id)


@app.post("/api/auth/google/check", dependencies=[Depends(require_auth)])
async def api_google_check(request: Request):
    """Verify the login actually worked (agy models as ground truth)."""
    from . import auth_google
    body = await request.json()
    login_id = (body.get("login_id") or "").strip()
    if not auth_google.start_check(login_id):
        raise HTTPException(400, "login tidak dikenal / pengecekan sudah jalan")
    return {"ok": True}


@app.post("/api/auth/google/code", dependencies=[Depends(require_auth)])
async def api_google_code(request: Request):
    """Manual fallback: paste the authorization code shown by Google."""
    from . import auth_google
    body = await request.json()
    login_id = (body.get("login_id") or "").strip()
    code = (body.get("code") or "").strip()
    if not code or not auth_google.submit_code(login_id, code):
        raise HTTPException(400, "gagal mengirim kode")
    auth_google.start_check(login_id)
    return {"ok": True}


@app.post("/api/auth/google/done", dependencies=[Depends(require_auth)])
async def api_google_done(request: Request):
    """Finalize a verified Google login: flag it, drop the PTY, and
    restart the agy session so the next turn runs as the Google account."""
    from . import auth_google
    body = await request.json()
    login_id = (body.get("login_id") or "").strip()
    auth_google.mark_google_auth(True)
    auth_google.cancel_login(login_id)
    agy.manager.close()
    return {"ok": True}


@app.post("/api/auth/google/cancel", dependencies=[Depends(require_auth)])
async def api_google_cancel(request: Request):
    from . import auth_google
    body = await request.json()
    auth_google.cancel_login((body.get("login_id") or "").strip())
    return {"ok": True}


@app.get("/api/models", dependencies=[Depends(require_auth)])
def api_models():
    return {"models": _discover_models(),
            "default": config.DEFAULT_MODEL or (_discover_models()[0]["id"]
                                                if _discover_models() else "")}


@app.get("/api/conversations", dependencies=[Depends(require_auth)])
def api_conversations():
    return {"conversations": store.fetch_conversations()}


@app.get("/api/conversations/{cid}", dependencies=[Depends(require_auth)])
def api_conversation(cid: str):
    return {"conversation_id": cid, "messages": store.parse_transcript(cid)}


@app.delete("/api/conversations/{cid}", dependencies=[Depends(require_auth)])
def api_delete_conversation(cid: str):
    return {"ok": store.delete_conversation(cid), "conversation_id": cid}


@app.post("/api/conversations/{cid}/rename", dependencies=[Depends(require_auth)])
async def api_rename_conversation(cid: str, request: Request):
    body = await request.json()
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "title kosong")
    return {"ok": store.rename_conversation(cid, title)}


@app.get("/api/files", dependencies=[Depends(require_auth)])
def api_files(path: str = ""):
    try:
        return store.list_dir(path)
    except store.PathError as e:
        raise HTTPException(400, str(e))


@app.get("/api/file_content", dependencies=[Depends(require_auth)])
def api_file_content(path: str = ""):
    try:
        return store.read_file(path)
    except store.PathError as e:
        raise HTTPException(400, str(e))


@app.get("/api/raw", dependencies=[Depends(require_auth)])
def api_raw(path: str = ""):
    """Serve raw file bytes (images)."""
    try:
        full = store.safe_path(path)
    except store.PathError as e:
        raise HTTPException(400, str(e))
    if not os.path.isfile(full):
        raise HTTPException(404, "tidak ditemukan")
    import mimetypes as _m
    mime, _ = _m.guess_type(full)
    return FileResponse(full, media_type=mime or "application/octet-stream")


@app.post("/api/file_save", dependencies=[Depends(require_auth)])
async def api_file_save(request: Request):
    body = await request.json()
    try:
        return store.save_file(body.get("path", ""), body.get("content", ""),
                               body.get("expected_mtime"))
    except store.PathError as e:
        msg = str(e)
        raise HTTPException(409 if msg.startswith("CONFLICT") else 400, msg)


@app.post("/api/file_create", dependencies=[Depends(require_auth)])
async def api_file_create(request: Request):
    body = await request.json()
    try:
        return store.create_item(body.get("path", ""), bool(body.get("is_dir")))
    except store.PathError as e:
        raise HTTPException(400, str(e))


@app.post("/api/file_delete", dependencies=[Depends(require_auth)])
async def api_file_delete(request: Request):
    body = await request.json()
    try:
        return store.delete_item(body.get("path", ""))
    except store.PathError as e:
        raise HTTPException(400, str(e))


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    # --- auth handshake (5s) ---
    try:
        first = await asyncio.wait_for(ws.receive_json(), timeout=5)
    except Exception:
        await ws.close(code=4401)
        return
    if first.get("type") != "auth" or not _token_ok(first.get("token")):
        await ws.send_json({"type": "error", "message": "unauthorized"})
        await ws.close(code=4401)
        return

    q: asyncio.Queue = asyncio.Queue()
    hub.register(q)
    await ws.send_json({
        "type": "ready",
        "mock": config.AGY_MOCK,
        "workspace": config.WORKSPACE_DIR,
        "models": _discover_models(),
        "default_model": config.DEFAULT_MODEL,
        "yolo_default": config.YOLO_DEFAULT,
    })

    pty_sess = None

    async def sender():
        while True:
            msg = await q.get()
            if msg is None:
                break
            # term_output bytes -> base64
            if msg.get("type") == "term_output":
                data = msg.get("data", b"")
                if isinstance(data, bytes):
                    msg = {"type": "term_output",
                           "data": base64.b64encode(data).decode()}
            await ws.send_json(msg)

    send_task = asyncio.create_task(sender())
    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")

            if mtype == "ping":
                await ws.send_json({"type": "pong"})

            elif mtype == "chat":
                prompt = (msg.get("prompt") or "").strip()
                if not prompt:
                    await ws.send_json({"type": "error", "message": "prompt kosong"})
                    continue
                if hub.turn is not None:
                    await ws.send_json({"type": "error",
                                        "message": "masih ada turn berjalan — stop dulu"})
                    continue
                model = msg.get("model") or config.DEFAULT_MODEL or ""
                yolo = bool(msg.get("yolo", config.YOLO_DEFAULT))
                cid = msg.get("conversation_id") or ""
                try:
                    if msg.get("new_chat"):
                        sess = agy.manager.new_chat(model=model, yolo=yolo)
                    else:
                        sess = agy.manager.ensure(conversation_id=cid or None,
                                                 model=model, yolo=yolo)
                except Exception as e:  # noqa: BLE001
                    await ws.send_json({"type": "error", "message": f"sesi: {e}"})
                    continue
                hub.turn_start()
                await ws.send_json({"type": "turn_start",
                                    "session_kind": sess.kind, "yolo": yolo})
                try:
                    sess.send_prompt(prompt)
                except Exception as e:  # noqa: BLE001
                    hub.turn_end()
                    await ws.send_json({"type": "turn_end", "status": "ERROR",
                                        "conversation_id": sess.conversation_id,
                                        "error": str(e), "response": "",
                                        "usage": {}, "duration": 0})

            elif mtype == "abort":
                if hub.turn is not None:
                    _do_abort("ABORTED")
                else:
                    await ws.send_json({"type": "error",
                                        "message": "tidak ada turn berjalan"})

            elif mtype == "approve":
                ok = agy.manager.decide_approval(msg.get("approval_id", ""),
                                                 msg.get("decision", "deny"))
                await ws.send_json({"type": "approval_ack",
                                    "approval_id": msg.get("approval_id"),
                                    "ok": ok})

            elif mtype == "term_start":
                if pty_sess:
                    pty_sess.close()
                term_q: queue.Queue = queue.Queue()
                pty_sess = PtySession(term_q,
                                      cols=int(msg.get("cols", 100)),
                                      rows=int(msg.get("rows", 32)))

                async def _term_pump():
                    loop = asyncio.get_event_loop()
                    while True:
                        try:
                            ev = await loop.run_in_executor(None, term_q.get, True, 0.5)
                        except Exception:
                            ev = None
                        if ev is None:
                            if pty_sess is None or not pty_sess.alive():
                                await q.put({"type": "term_exit"})
                                break
                            continue
                        if ev.get("t") == "term_output":
                            await q.put({"type": "term_output",
                                         "data": ev.get("data", b"")})
                        elif ev.get("t") == "term_exit":
                            await q.put({"type": "term_exit"})
                            break

                asyncio.create_task(_term_pump())
                await ws.send_json({"type": "term_started"})

            elif mtype == "term_input":
                if pty_sess:
                    data = msg.get("data", "")
                    pty_sess.write(data.encode("utf-8", errors="replace"))

            elif mtype == "term_resize":
                if pty_sess:
                    pty_sess.resize(int(msg.get("cols", 100)),
                                    int(msg.get("rows", 32)))

            elif mtype == "models":
                _models_cache["at"] = 0
                await ws.send_json({"type": "models",
                                    "models": _discover_models()})

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        hub.unregister(q)
        await q.put(None)
        send_task.cancel()
        if pty_sess:
            pty_sess.close()


# --------------------------------------------------------------------------
# Static UI (served last; API/WS take precedence)
# --------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def _index():
    return FileResponse(os.path.join(config.STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


def run():
    import uvicorn
    token = AUTH_TOKEN
    print("=" * 58)
    print("  AGY WEB v3 — Terminal Deck")
    print("=" * 58)
    print(f"  URL lokal : http://{config.HOST}:{config.PORT}")
    print(f"  Workspace : {config.WORKSPACE_DIR}")
    print(f"  Mode agy  : {'MOCK (simulasi)' if config.AGY_MOCK else config.AGY_BIN}")
    print(f"  Auth token: {token}")
    print("  Tempel token itu di layar login. Jangan disebar.")
    print("=" * 58)
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")


if __name__ == "__main__":
    run()
