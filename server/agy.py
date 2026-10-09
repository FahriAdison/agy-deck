"""AGY Web v3 - agy CLI session manager.

Three session kinds behind one interface:
  * PersistentSession - one `agy --input-format stream-json --output-format
    stream-json` process per conversation; turns are fed via stdin NDJSON.
    Fast (no spawn per turn), live thinking/tool events.
  * OneshotSession   - fallback: `agy -p <prompt> --output-format stream-json`
    spawned per turn (for old CLI versions without --input-format).
  * MockSession      - scripted fake CLI used when no agy binary is installed
    (AGY_MOCK=1). Lets the whole UI be tested end-to-end.

All CLI output is normalized into small dict events {"t": ...} and pushed
into a thread-safe queue consumed by the WebSocket layer.
"""
import json
import os
import queue
import signal
import subprocess
import threading
import time
import uuid

from . import config

# --------------------------------------------------------------------------
# Event normalization
# --------------------------------------------------------------------------

def normalize_event(obj):
    """Turn one raw stream-json NDJSON object into a list of normalized events."""
    if not isinstance(obj, dict):
        return [{"t": "unknown", "raw": str(obj)}]
    ev = obj.get("event")

    if ev == "init":
        return [{
            "t": "init",
            "conversation_id": obj.get("conversation_id") or "",
            "permission_mode": obj.get("permission_mode") or "",
            "cwd": obj.get("cwd") or "",
        }]

    if ev == "step_update":
        su = obj.get("step_update", {}) or {}
        stype = su.get("step_type", "")
        out = []
        if stype == "agent_response":
            delta = su.get("text_delta")
            if delta:
                out.append({"t": "delta", "text": delta})
            think = su.get("thinking_delta") or su.get("thinking")
            if think:
                out.append({"t": "thinking", "text": think})
            usage = su.get("usage")
            if usage:
                out.append({"t": "usage", "usage": usage})
        elif stype == "tool":
            out.append({
                "t": "tool",
                "name": su.get("tool_name", "tool"),
                "state": su.get("state", "ACTIVE"),
                "info": su.get("tool_info", {}) or {},
                "summary": _tool_summary(su),
            })
        elif stype == "checkpoint":
            out.append({"t": "checkpoint"})
        # user_input and others: nothing to show
        return out

    if ev == "result":
        res = obj.get("result", {}) or {}
        return [{
            "t": "turn_end",
            "conversation_id": res.get("conversation_id") or "",
            "status": res.get("status", "SUCCESS"),
            "response": res.get("response", "") or "",
            "usage": res.get("usage", {}) or {},
            "duration": res.get("duration_seconds", 0),
            "error": res.get("error", "") or "",
        }]

    # --- experimental: permission / approval requests ---------------------
    # agy's interactive mode advertises permission_mode "request-review";
    # if it ever emits an approval-shaped event, surface it as a card.
    lowered = json.dumps(obj)[:400].lower()
    if ev in ("permission_request", "approval_request", "confirm_tool") or \
       ("permission" in lowered and "request" in lowered):
        return [{
            "t": "approval_request",
            "approval_id": obj.get("approval_id") or obj.get("id") or f"appr_{uuid.uuid4().hex[:8]}",
            "tool": obj.get("tool_name") or obj.get("tool") or "tool",
            "summary": obj.get("summary") or obj.get("description") or "",
            "raw": obj,
        }]

    if ev == "error":
        return [{"t": "error", "message": obj.get("message") or obj.get("error") or str(obj)}]

    return [{"t": "unknown", "raw": obj}]


def _tool_summary(su):
    info = su.get("tool_info", {}) or {}
    for key in ("summary", "description", "command", "path", "file"):
        if info.get(key):
            s = str(info[key])
            return s[:160]
    params = su.get("parameters") or info.get("parameters") or {}
    if isinstance(params, dict) and params:
        k = next(iter(params))
        return f"{k}={str(params[k])[:80]}"
    return ""


# --------------------------------------------------------------------------
# Subprocess pump (shared by persistent + oneshot)
# --------------------------------------------------------------------------

class _Pump:
    """Reads a process' stdout lines in a thread, normalizes, queues events."""

    def __init__(self, proc, out_queue, tag="agy"):
        self.proc = proc
        self.q = out_queue
        self.tag = tag
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name=f"agy-pump-{tag}")
        self.thread.start()
        self.err_thread = threading.Thread(target=self._drain_stderr, daemon=True,
                                           name=f"agy-err-{tag}")
        self.err_thread.start()

    def _run(self):
        try:
            for line in iter(self.proc.stdout.readline, ""):
                if self._stop.is_set():
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    # Non-JSON stdout (warnings etc.) -> visible debug text
                    self.q.put({"t": "stderr_line", "text": line[:500]})
                    continue
                for ev in normalize_event(obj):
                    self.q.put(ev)
        except Exception as e:  # noqa: BLE001
            self.q.put({"t": "error", "message": f"reader: {e}"})
        finally:
            self.q.put({"t": "proc_exit", "code": self.proc.poll()})

    def _drain_stderr(self):
        try:
            for line in iter(self.proc.stderr.readline, ""):
                if self._stop.is_set():
                    break
                line = line.strip()
                if line:
                    self.q.put({"t": "stderr_line", "text": line[:500]})
        except Exception:
            pass

    def stop(self):
        self._stop.set()


def _spawn(cmd, cwd):
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=cwd,
        env=config.agy_env(),
        start_new_session=True,  # own process group -> abort kills the tree
    )


def _kill_tree(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

class PersistentSession:
    """One long-lived agy process; each turn = one NDJSON line on stdin."""

    kind = "persistent"

    def __init__(self, out_queue, conversation_id=None, model=None, yolo=True):
        self.q = out_queue
        self.conversation_id = conversation_id or ""
        self.model = model or ""
        self.yolo = yolo
        self.key = ("persistent", self.conversation_id, self.model, yolo)
        cmd = [config.AGY_BIN, "--input-format", "stream-json",
               "--output-format", "stream-json",
               "--add-dir", config.WORKSPACE_DIR]
        if conversation_id:
            cmd += ["--conversation", conversation_id]
        if model:
            cmd += ["--model", model]
        if yolo:
            cmd += ["--dangerously-skip-permissions"]
        self.cmd = cmd
        self.proc = _spawn(cmd, config.WORKSPACE_DIR)
        self.pump = _Pump(self.proc, out_queue, tag="persistent")
        self._aborted = threading.Event()

    def alive(self):
        return self.proc.poll() is None

    def send_prompt(self, prompt):
        if not self.alive():
            raise RuntimeError("sesi agy sudah mati")
        msg = {"event": "user", "message": {"role": "user", "content": prompt}}
        try:
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError as e:
            raise RuntimeError(f"stdin agy putus: {e}")

    def abort(self):
        self._aborted.set()
        _kill_tree(self.proc)
        self.pump.stop()

    def close(self):
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        _kill_tree(self.proc)
        self.pump.stop()


class OneshotSession:
    """Fallback: one `agy -p` process per turn (old CLI without --input-format)."""

    kind = "oneshot"

    def __init__(self, out_queue, conversation_id=None, model=None, yolo=True):
        self.q = out_queue
        self.conversation_id = conversation_id or ""
        self.model = model or ""
        self.yolo = yolo
        self.key = ("oneshot", self.conversation_id, self.model, yolo)
        self.proc = None
        self.pump = None

    def alive(self):
        return True  # stateless between turns

    def send_prompt(self, prompt):
        self.abort()  # just in case
        cmd = [config.AGY_BIN, "-p", prompt,
               "--output-format", "stream-json",
               "--add-dir", config.WORKSPACE_DIR]
        if self.conversation_id:
            cmd += ["--conversation", self.conversation_id]
        if self.model:
            cmd += ["--model", self.model]
        if self.yolo:
            cmd += ["--dangerously-skip-permissions"]
        self.proc = _spawn(cmd, config.WORKSPACE_DIR)
        self.pump = _Pump(self.proc, self.q, tag="oneshot")

    def abort(self):
        if self.pump:
            self.pump.stop()
            self.pump = None
        _kill_tree(self.proc)
        self.proc = None

    def close(self):
        self.abort()


class MockSession:
    """Scripted fake CLI: exercises the whole UI without an agy binary."""

    kind = "mock"

    def __init__(self, out_queue, conversation_id=None, model=None, yolo=True):
        self.q = out_queue
        self.conversation_id = conversation_id or f"mock-{uuid.uuid4().hex[:8]}"
        self.model = model or "mock-model"
        self.yolo = yolo
        self.key = ("mock", self.conversation_id, self.model, yolo)
        self._stop = threading.Event()
        self._approval = {}
        self._approval_lock = threading.Lock()
        self._thread = None

    def alive(self):
        return not self._stop.is_set()

    def _emit(self, ev, delay=0.35):
        if self._stop.is_set():
            return False
        time.sleep(delay)
        if self._stop.is_set():
            return False
        self.q.put(ev)
        return True

    def send_prompt(self, prompt):
        self._stop.clear()
        short = prompt[:64].replace("\n", " ")
        self._thread = threading.Thread(target=self._script, args=(prompt, short),
                                        daemon=True, name="mock-agy")
        self._thread.start()

    def _script(self, prompt, short):
        e = self._emit
        e({"t": "init", "conversation_id": self.conversation_id,
           "permission_mode": "request-review" if not self.yolo else "auto",
           "cwd": config.WORKSPACE_DIR}, 0.2)
        if not e({"t": "thinking", "text": f"menganalisis permintaan: “{short}”…"}): return
        if not e({"t": "thinking", "text": "memetakan workspace & menyusun rencana eksekusi…"}): return
        if not e({"t": "tool", "name": "list_files", "state": "ACTIVE",
                  "info": {"path": "."}, "summary": "path=."}): return
        if not e({"t": "tool", "name": "list_files", "state": "DONE",
                  "info": {"count": 12}, "summary": "12 entri ditemukan"}): return
        md = (
            "# Hasil (mode mock)\n\n"
            "Ini **simulasi** — `agy` asli belum terhubung.\n\n"
            f"- prompt: `{short}`\n- model: `{self.model}`\n"
            "- mode: `mock`\n\n"
            "```bash\n$ agy --version\n# → pasang API key untuk sesi live\n```\n"
        )
        for chunk in [md[i:i + 24] for i in range(0, len(md), 24)]:
            if not e({"t": "delta", "text": chunk}, 0.05):
                return
        # experimental approval demo
        appr_id = f"appr_{uuid.uuid4().hex[:6]}"
        if not e({"t": "approval_request", "approval_id": appr_id,
                  "tool": "write_file",
                  "summary": "tulis contoh.txt (demo approval)"}, 0.2):
            return
        decision = self._wait_approval(appr_id, timeout=120)
        if decision == "deny":
            e({"t": "tool", "name": "write_file", "state": "ERROR",
               "info": {}, "summary": "ditolak pengguna"}, 0.2)
            e({"t": "delta", "text": "\n\n*ditolak — tidak ada file yang ditulis.*\n"}, 0.1)
        else:
            e({"t": "tool", "name": "write_file", "state": "ACTIVE",
               "info": {"path": "contoh.txt"}, "summary": "path=contoh.txt"}, 0.2)
            e({"t": "tool", "name": "write_file", "state": "DONE",
               "info": {"bytes": 128}, "summary": "contoh.txt ditulis (128 B)"}, 0.4)
            e({"t": "delta", "text": "\n\n`[ok]` contoh.txt ditulis.\n"}, 0.1)
        e({"t": "turn_end", "conversation_id": self.conversation_id,
           "status": "SUCCESS", "response": "",
           "usage": {"input_tokens": 412, "output_tokens": 188, "total_tokens": 600},
           "duration": 4.2, "error": ""}, 0.2)

    def _wait_approval(self, appr_id, timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._stop.is_set():
                return "deny"
            with self._approval_lock:
                if appr_id in self._approval:
                    return self._approval.pop(appr_id)
            time.sleep(0.1)
        return "deny"

    def decide_approval(self, appr_id, decision):
        with self._approval_lock:
            self._approval[appr_id] = decision

    def abort(self):
        self._stop.set()

    def close(self):
        self._stop.set()


# --------------------------------------------------------------------------
# Manager: one active session, respawn on new/resume/abort
# --------------------------------------------------------------------------

class SessionManager:
    def __init__(self):
        self.session = None
        self.events = queue.Queue()
        self._lock = threading.Lock()
        self._persistent_ok = None  # None = unknown, True/False after probe

    def _probe_persistent(self):
        if self._persistent_ok is not None:
            return self._persistent_ok
        if config.AGY_MOCK:
            self._persistent_ok = False
            return False
        try:
            cmd = [config.AGY_BIN, "--input-format", "stream-json",
                   "--output-format", "stream-json", "--add-dir", config.WORKSPACE_DIR]
            p = _spawn(cmd, config.WORKSPACE_DIR)
            time.sleep(2.0)
            alive = p.poll() is None
            _kill_tree(p)
            self._persistent_ok = alive
        except Exception:
            self._persistent_ok = False
        return self._persistent_ok

    def ensure(self, conversation_id=None, model=None, yolo=True):
        with self._lock:
            key = (conversation_id or "", model or "", bool(yolo))
            if self.session and getattr(self.session, "key", None)[1:] == key:
                if self.session.alive():
                    return self.session
            self._close_locked()
            if config.AGY_MOCK:
                self.session = MockSession(self.events, conversation_id, model, yolo)
            elif self._probe_persistent():
                self.session = PersistentSession(self.events, conversation_id, model, yolo)
            else:
                self.session = OneshotSession(self.events, conversation_id, model, yolo)
            return self.session

    def new_chat(self, model=None, yolo=True):
        with self._lock:
            self._close_locked()
            if config.AGY_MOCK:
                self.session = MockSession(self.events, None, model, yolo)
            elif self._probe_persistent():
                self.session = PersistentSession(self.events, None, model, yolo)
            else:
                self.session = OneshotSession(self.events, None, model, yolo)
            return self.session

    def abort(self):
        """Kill the running turn's process tree, then respawn a fresh session
        with the same key so the conversation can continue."""
        with self._lock:
            if not self.session:
                return False
            key = getattr(self.session, "key", None)
            try:
                self.session.abort()
            except Exception:
                pass
            self._close_locked()
            if key:
                kind, cid, model, yolo = key
                cid = cid or None
                if config.AGY_MOCK or kind == "mock":
                    self.session = MockSession(self.events, cid, model, yolo)
                elif kind == "persistent":
                    self.session = PersistentSession(self.events, cid, model, yolo)
                else:
                    self.session = OneshotSession(self.events, cid, model, yolo)
            # drain stale events from the killed turn
            try:
                while True:
                    self.events.get_nowait()
            except queue.Empty:
                pass
            return True

    def decide_approval(self, approval_id, decision):
        sess = self.session
        if isinstance(sess, MockSession):
            sess.decide_approval(approval_id, decision)
            return True
        # Real CLI: best-effort — forward the decision on stdin. The exact
        # schema is version-dependent; unknown events are ignored by agy.
        if isinstance(sess, PersistentSession) and sess.alive():
            try:
                msg = {"event": "approval_response", "approval_id": approval_id,
                       "decision": decision}
                sess.proc.stdin.write(json.dumps(msg) + "\n")
                sess.proc.stdin.flush()
                return True
            except Exception:
                return False
        return False

    def _close_locked(self):
        if self.session:
            try:
                self.session.close()
            except Exception:
                pass
            self.session = None

    def close(self):
        with self._lock:
            self._close_locked()


manager = SessionManager()
