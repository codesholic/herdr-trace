"""`herdr-trace plugin ...`: what the herdr plugin runs.

herdr's startup hooks run once per herdr start and do not supervise what they launch, so
`start` detaches the server and leaves it running; it is a no-op while a server already
answers for the session. herdr hands plugin commands the session's socket
(HERDR_SOCKET_PATH) and name (HERDR_SESSION); the server uses the same state folder as
`herdr-trace send`, so both meet at one journal.

Plugin output lands in herdr's plugin log, so it names addresses but never the token.
Settings come from the environment or from KEY=value lines in $HERDR_PLUGIN_CONFIG_DIR/env:
HERDR_TRACE_AUTO_OPEN=1 opens the view when herdr starts, HERDR_TRACE_LANG=ko starts it in Korean.
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from . import paths

READY_TIMEOUT = 15


def settings(environ=os.environ):
    values = {}
    config = environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if config and (Path(config) / "env").is_file():
        for line in (Path(config) / "env").read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key and not key.startswith("#"):
                values[key.strip()] = value.strip().strip("'\"")
    return {**values, **{k: v for k, v in environ.items() if k.startswith("HERDR_TRACE_")}}


def running(state_dir):
    """The record of a server that answers for this session, else None."""
    try:
        info = json.loads((Path(state_dir) / "server.json").read_text(encoding="utf-8"))
        os.kill(info["pid"], 0)
        request = urllib.request.Request(info["url"] + "/api/health", headers={"Authorization": f"Bearer {info['token']}"})
        with urllib.request.urlopen(request, timeout=2) as reply:
            return info if reply.status == 200 else None
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        return None


def start(environ=os.environ, out=sys.stdout, browse=webbrowser.open):
    session = paths.session_name(None, environ)
    state = paths.session_dir(session, environ)
    found = running(state)
    if found:
        print(f"herdr-trace already serves session {session} at {found['url']}", file=out)
        return found
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = settings(environ)
    command = [sys.executable, "-m", "herdr_trace", "serve", "--session", session,
               "--socket", str(paths.herdr_socket(session, environ)), "--lang", config.get("HERDR_TRACE_LANG", "en")]
    log = os.open(state / "server.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)  # It holds the token.
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=dict(environ),
                                   start_new_session=True)  # Outlives the hook that started it.
    finally:
        os.close(log)
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        found = running(state)
        if found and found["pid"] == process.pid:
            print(f"herdr-trace serves session {session} at {found['url']}", file=out)
            if config.get("HERDR_TRACE_AUTO_OPEN", "").lower() in {"1", "true", "yes"}:
                browse(found["open_url"])
            return found
        if process.poll() is not None:
            break
        time.sleep(0.2)
    raise ValueError(f"herdr-trace did not start for session {session}; see {state / 'server.log'}")


def stop(environ=os.environ, out=sys.stdout):
    session = paths.session_name(None, environ)
    state = paths.session_dir(session, environ)
    found = running(state)
    if not found:
        print(f"herdr-trace is not serving session {session}", file=out)
        return None
    os.kill(found["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (state / "server.json").exists():
        time.sleep(0.1)
    print(f"herdr-trace stopped serving session {session}", file=out)
    return found


def status(environ=os.environ, out=sys.stdout):
    session = paths.session_name(None, environ)
    found = running(paths.session_dir(session, environ))
    if found:
        print(f"herdr-trace serves session {session} at {found['url']} (journal {found['journal']})", file=out)
    else:
        print(f"herdr-trace is not serving session {session}", file=out)
    return found


def open_view(environ=os.environ, out=sys.stdout, browse=webbrowser.open):
    """Open the view in a browser, starting the server first when it is not running."""
    found = running(paths.session_dir(paths.session_name(None, environ), environ)) or start(environ, out=out, browse=browse)
    browse(found["open_url"])
    print(f"herdr-trace opened {found['url']}", file=out)
    return found


SHIM_MARK = "# Installed by the herdr-trace plugin."


def command_path(environ=os.environ):
    folder = environ.get("HERDR_TRACE_BIN_DIR") or str(Path.home() / ".local/bin")
    return Path(folder).expanduser() / "herdr-trace"


def install_command(environ=os.environ, out=sys.stdout):
    """Put a `herdr-trace` command on PATH for agents, pointing at this plugin's checkout.

    Run only when the user invokes the action. An existing file that this plugin did not write is left alone.
    """
    root = Path(environ.get("HERDR_PLUGIN_ROOT") or Path(__file__).resolve().parents[2])
    target = command_path(environ)
    if target.exists() and SHIM_MARK not in target.read_text(encoding="utf-8", errors="replace"):
        raise ValueError(f"{target} exists and was not written by herdr-trace; remove it or set HERDR_TRACE_BIN_DIR")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"#!/bin/sh\n{SHIM_MARK}\nexec \"{root / 'scripts/herdr-trace'}\" \"$@\"\n", encoding="utf-8")
    target.chmod(0o755)
    print(f"herdr-trace command installed at {target}", file=out)
    folders = [Path(entry).expanduser() for entry in environ.get("PATH", "").split(os.pathsep) if entry]
    if target.parent not in folders:
        print(f"{target.parent} is not on PATH; add it so agents can run herdr-trace", file=out)
    return target


def remove_command(environ=os.environ, out=sys.stdout):
    target = command_path(environ)
    if not target.exists():
        print(f"no herdr-trace command at {target}", file=out)
        return None
    if SHIM_MARK not in target.read_text(encoding="utf-8", errors="replace"):
        raise ValueError(f"{target} was not written by herdr-trace; it is left alone")
    target.unlink()
    print(f"herdr-trace command removed from {target}", file=out)
    return target


COMMANDS = {"start": start, "stop": stop, "status": status, "open": open_view,
            "install-command": install_command, "remove-command": remove_command}
