"""Where herdr-trace keeps each herdr session's journal, settings and running-server record,
and which herdr socket and tool folders it reads."""
import os
import re
from pathlib import Path

SESSION_NAME = re.compile(r"[A-Za-z0-9._-]+")  # herdr's own rule for session names
DEFAULT_SESSION = "default"


def check_session(name):
    if not SESSION_NAME.fullmatch(name or "") or name in {".", ".."}:
        raise ValueError("herdr session names use ASCII letters, numbers, '.', '_' and '-'")
    return name


def state_root(environ=os.environ):
    if environ.get("HERDR_TRACE_STATE_DIR"):
        return Path(environ["HERDR_TRACE_STATE_DIR"]).expanduser()
    base = Path(environ["XDG_STATE_HOME"]).expanduser() if environ.get("XDG_STATE_HOME") else Path.home() / ".local/state"
    return base / "herdr-trace"


def session_name(name=None, environ=os.environ):
    """The named session, else the one this process runs in (herdr sets HERDR_SESSION), else herdr's default."""
    return check_session(name or environ.get("HERDR_SESSION") or DEFAULT_SESSION)


def session_dir(name, environ=os.environ):
    return state_root(environ) / "sessions" / check_session(name)


def journal_file(name, environ=os.environ):
    return session_dir(name, environ) / "events.jsonl"


def herdr_socket(name, environ=os.environ):
    """herdr's socket for a session: the one herdr hands its own panes and plugins, else the usual layout."""
    if environ.get("HERDR_SOCKET_PATH") and (environ.get("HERDR_SESSION") or DEFAULT_SESSION) == name:
        return Path(environ["HERDR_SOCKET_PATH"])
    config = Path(environ["XDG_CONFIG_HOME"]).expanduser() if environ.get("XDG_CONFIG_HOME") else Path.home() / ".config"
    if name == DEFAULT_SESSION:
        return config / "herdr" / "herdr.sock"
    return config / "herdr" / "sessions" / check_session(name) / "herdr.sock"


def codex_dir(environ=os.environ):
    return Path(environ["CODEX_HOME"]).expanduser() if environ.get("CODEX_HOME") else Path.home() / ".codex"


def claude_dir(environ=os.environ):
    return Path(environ["CLAUDE_CONFIG_DIR"]).expanduser() if environ.get("CLAUDE_CONFIG_DIR") else Path.home() / ".claude"
