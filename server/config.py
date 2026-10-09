"""AGY Web v3 - configuration & secrets.

Loads .env (KEY=VALUE, no dependency), resolves workspace, manages the
auth token used by the web UI. Secrets never leave this module except
as environment variables for the agy subprocess.
"""
import os
import secrets
import shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))      # .../server
ROOT_DIR = os.path.dirname(BASE_DIR)                        # .../agy-web-v3
STATIC_DIR = os.path.join(ROOT_DIR, "static")
ENV_PATH = os.path.join(ROOT_DIR, ".env")
TOKEN_PATH = os.path.join(ROOT_DIR, ".auth_token")


def _parse_dotenv(path):
    vals = {}
    if not os.path.exists(path):
        return vals
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k:
                vals[k] = v
    return vals


def _load_dotenv(path):
    for k, v in _parse_dotenv(path).items():
        if k not in os.environ:
            os.environ[k] = v


def set_env_value(key, value):
    """Persist one KEY=VALUE into .env (chmod 600). Never logs the value."""
    lines, found = [], False
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    out = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s and s.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    try:
        os.chmod(ENV_PATH, 0o600)
    except Exception:
        pass
    os.environ[key] = value


def gemini_key_configured():
    return bool(_parse_dotenv(ENV_PATH).get("GEMINI_API_KEY")
                or os.environ.get("GEMINI_API_KEY"))


_load_dotenv(ENV_PATH)


def _resolve_workspace():
    env_ws = os.environ.get("WORKSPACE_DIR")
    if env_ws and os.path.isdir(env_ws):
        return os.path.abspath(env_ws)
    home_ws = os.path.expanduser("~/workspace")
    if os.path.isdir(home_ws):
        return home_ws
    return ROOT_DIR


WORKSPACE_DIR = _resolve_workspace()

# Make sure a user-local agy install is discoverable even if PATH lacks it.
_local_bin = os.path.expanduser("~/.local/bin")
if os.path.isdir(_local_bin) and _local_bin not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _local_bin + os.pathsep + os.environ.get("PATH", "")

PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "127.0.0.1")
AGY_BIN = shutil.which("agy")
AGY_MOCK = os.environ.get("AGY_MOCK", "0") == "1" or not AGY_BIN
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
AGY_MODEL_PROVIDER = os.environ.get("AGY_MODEL_PROVIDER", "gemini" if GEMINI_API_KEY else "")
TURN_TIMEOUT = int(os.environ.get("AGY_TURN_TIMEOUT", "600"))
DEFAULT_MODEL = os.environ.get("AGY_DEFAULT_MODEL", "")
YOLO_DEFAULT = os.environ.get("AGY_YOLO", "1") == "1"


def get_auth_token():
    """Return the persistent auth token.

    AGY_WEB_TOKEN env (e.g. a Hugging Face Space secret) wins when set,
    so the token is known without reading server logs.
    """
    env_tok = os.environ.get("AGY_WEB_TOKEN", "").strip()
    if env_tok:
        return env_tok
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            tok = f.read().strip()
            if tok:
                return tok
    tok = secrets.token_hex(24)
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(tok)
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except Exception:
        pass
    return tok


def agy_env():
    """Environment for the agy subprocess (injects the user's own key).

    Re-reads .env on every call so a key saved via the web UI takes
    effect on the next spawned session without a server restart.

    Google OAuth (login via web UI) takes precedence: when active, the
    API key is NOT injected so agy runs as the Google account.
    """
    env = dict(os.environ)
    env["GEMINI_FORCE_FILE_STORAGE"] = "true"
    try:
        from .auth_google import has_google_auth
        google = has_google_auth()
    except Exception:
        google = False
    if google:
        env.pop("GEMINI_API_KEY", None)
        return env
    key = _parse_dotenv(ENV_PATH).get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
    if key:
        env["GEMINI_API_KEY"] = key
    return env
