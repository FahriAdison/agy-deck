"""Google OAuth login flow for agy.

Headless device-code style flow, driven from the web UI:
  start  -> spawn `agy` in a PTY, auto-select "Google OAuth", capture the
            accounts.google.com URL from the terminal output
  poll   -> return the URL + tail of output so the UI can show progress
  check  -> verify with `agy models` (background thread)
  code   -> manual fallback: paste the authorization code into the PTY
  cancel -> kill the login PTY

On success the credentials live in ~/.gemini (file storage forced via
GEMINI_FORCE_FILE_STORAGE) and a flag file marks Google auth active, so new
agy sessions skip GEMINI_API_KEY and run as the Google account.
"""
from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid

from .config import AGY_BIN

HOME = os.path.expanduser("~")
FLAG_PATH = os.path.join(HOME, ".agy_deck_google_auth")


def _agy_bin() -> str | None:
    if AGY_BIN:
        return AGY_BIN
    cand = os.path.join(HOME, ".local", "bin", "agy")
    return cand if os.path.exists(cand) else None

_logins: dict[str, dict] = {}
_lock = threading.Lock()


def _spawn_agy():
    bin_path = _agy_bin()
    if not bin_path:
        raise RuntimeError("agy binary not found")
    env = dict(os.environ)
    env["GEMINI_FORCE_FILE_STORAGE"] = "true"
    env["TERM"] = "xterm-256color"
    env["PATH"] = os.path.dirname(bin_path) + ":" + env.get("PATH", "")
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    proc = subprocess.Popen(
        [bin_path], stdin=slave, stdout=slave, stderr=slave,
        env=env, cwd=HOME, start_new_session=True,
    )
    os.close(slave)
    os.set_blocking(master, False)
    return proc, master


def start_login() -> str:
    proc, master = _spawn_agy()
    login_id = uuid.uuid4().hex[:12]
    with _lock:
        _logins[login_id] = {
            "proc": proc, "master": master, "buf": b"",
            "url": None, "sent_enter": False,
            "started": time.time(), "check": None,
        }
    return login_id


def _drain(lg: dict) -> None:
    try:
        while True:
            chunk = os.read(lg["master"], 65536)
            if not chunk:
                break
            lg["buf"] += chunk[-200000:] if len(lg["buf"]) > 200000 else chunk
    except (OSError, BlockingIOError):
        pass


_ANSI = re.compile(
    r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07"
    r"|\x1b[()][0-9A-Z]|\x1b[>][0-9;]*[a-zA-Z]"
    r"|\x1b[=?][0-9;]*[a-zA-Z]|\r"
)


def _clean(b: bytes) -> str:
    return _ANSI.sub("", b.decode("utf-8", "replace"))


def _extract_url(txt: str) -> str | None:
    # terminal wraps long lines mid-URL; join wrapped lines first
    flat = re.sub(r"[ \t]*\n[ \t]*", "", txt)
    m = re.search(r"https://accounts\.google\.com/o/oauth2/auth\?\S+", flat)
    return m.group(0) if m else None


def poll_login(login_id: str) -> dict:
    with _lock:
        lg = _logins.get(login_id)
    if not lg:
        return {"status": "unknown"}
    _drain(lg)
    alive = lg["proc"].poll() is None
    if alive and not lg["sent_enter"] and time.time() - lg["started"] > 4:
        try:
            os.write(lg["master"], b"\r")
            lg["sent_enter"] = True
        except OSError:
            pass
    if not lg["url"]:
        lg["url"] = _extract_url(_clean(lg["buf"]))
    tail = _clean(lg["buf"][-1200:])
    return {
        "status": "alive" if alive else "exited",
        "url": lg["url"],
        "tail": tail,
        "check": lg["check"],
    }


def submit_code(login_id: str, code: str) -> bool:
    with _lock:
        lg = _logins.get(login_id)
    if not lg:
        return False
    try:
        os.write(lg["master"], (code.strip() + "\r").encode())
        return True
    except OSError:
        return False


def cancel_login(login_id: str) -> None:
    with _lock:
        lg = _logins.pop(login_id, None)
    if not lg:
        return
    try:
        os.killpg(lg["proc"].pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        os.close(lg["master"])
    except Exception:
        pass


def has_google_auth() -> bool:
    if os.path.exists(FLAG_PATH):
        return True
    for p in (
        os.path.join(HOME, ".gemini", "oauth_creds.json"),
        os.path.join(HOME, ".gemini", "antigravity-cli", "oauth_creds.json"),
    ):
        if os.path.exists(p):
            return True
    return False


def mark_google_auth(ok: bool = True) -> None:
    try:
        if ok:
            with open(FLAG_PATH, "w") as f:
                f.write("1")
        elif os.path.exists(FLAG_PATH):
            os.remove(FLAG_PATH)
    except OSError:
        pass


def probe_agy_auth(timeout: int = 60) -> bool:
    """Ground truth: can agy actually list models?"""
    bin_path = _agy_bin()
    if not bin_path:
        return False
    try:
        r = subprocess.run(
            [bin_path, "models"], capture_output=True, text=True, timeout=timeout,
        )
        out = (r.stdout or "") + (r.stderr or "")
        return "please sign in" not in out.lower()
    except Exception:
        return False


def start_check(login_id: str) -> bool:
    with _lock:
        lg = _logins.get(login_id)
        if not lg or lg["check"] == "pending":
            return False
        lg["check"] = "pending"

    def _run():
        ok = probe_agy_auth()
        with _lock:
            if login_id in _logins:
                _logins[login_id]["check"] = "ok" if ok else "fail"

    threading.Thread(target=_run, daemon=True).start()
    return True
