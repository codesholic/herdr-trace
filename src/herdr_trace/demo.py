"""`herdr-trace demo`: the view on invented data, with a fake herdr socket.

Nothing here reads real Codex, Claude Code or herdr data. The fake socket rereads
<dir>/herdr-agents.json on every request, so editing an agent_status there (for
example working -> idle) shows up on the page within a second or two.
"""
import json
import shutil
import socket
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import demo_data
from .server import serve

MARKER = ".herdr-trace-demo"


def serve_socket(server, agents_file):
    while True:
        try:
            connection, _ = server.accept()
        except OSError:
            return
        threading.Thread(target=answer, args=(connection, agents_file), daemon=True).start()


def answer(connection, agents_file):
    """ping and agent.list reply once; events.subscribe stays open and sends an event when the agents file changes."""
    with connection, connection.makefile("rwb") as stream:
        request = json.loads(stream.readline())
        if request["method"] == "events.subscribe":
            stream.write((json.dumps({"id": request["id"], "result": {"type": "subscription_started"}}) + "\n").encode())
            stream.flush()
            seen = agents_file.stat().st_mtime_ns
            while True:
                time.sleep(0.5)
                changed = agents_file.stat().st_mtime_ns
                if changed != seen:
                    seen = changed
                    try:
                        stream.write((json.dumps({"event": "pane.agent_status_changed", "data": {"pane_id": "w1:p2",
                                      "workspace_id": "w1", "agent_status": "idle"}}) + "\n").encode())
                        stream.flush()
                    except OSError:
                        return
        agents = json.loads(agents_file.read_text(encoding="utf-8"))
        result = ({"type": "pong", "protocol": 22, "version": "demo"} if request["method"] == "ping"
                  else {"type": "agent_list", "agents": agents})
        stream.write((json.dumps({"id": request["id"], "result": result}) + "\n").encode())
        stream.flush()


def add_subagent(parent):
    """An invented running Explore subagent under the reviewer, so the demo shows a delegation line."""
    folder = parent.with_suffix("") / "subagents"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "agent-demo1.meta.json").write_text(json.dumps({"agentType": "Explore", "description": "테스트 빈틈 찾기",
        "toolUseId": "toolu_demo", "spawnDepth": 1, "requestShape": "background"}, ensure_ascii=False), encoding="utf-8")
    demo_data.write_jsonl(folder / "agent-demo1.jsonl", [demo_data.claude("user", 99.5, uuid="sd", isSidechain=True,
        agentId="demo1", message={"role": "user", "content": "test_calc.py에서 빠진 경우를 찾아 보고해줘."})])


def prepare(directory):
    """Write the invented story under directory, shifted to the last few minutes, and return its locations."""
    directory = Path(directory)
    if directory.exists() and any(directory.iterdir()):
        if not (directory / MARKER).exists():
            raise ValueError(f"{directory} is not an earlier demo folder; choose an empty --dir")
        shutil.rmtree(directory)  # Only an earlier demo folder of invented data is replaced.
    demo_data.BASE = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=3)
    built = demo_data.build(directory)
    (directory / MARKER).write_text("invented data written by herdr-trace demo\n", encoding="utf-8")
    add_subagent(demo_data.reviewer_path(directory))
    built["agents_file"] = directory / "herdr-agents.json"
    built["agents_file"].write_text(json.dumps(list(built["agents"].values()), ensure_ascii=False, indent=2), encoding="utf-8")
    return built


def run(port=0, open_browser=False, directory=None):
    owned = directory is None
    directory = Path(directory) if directory else Path(tempfile.mkdtemp(prefix="herdr-trace-demo-"))
    built = prepare(directory)
    socket_dir = Path(tempfile.mkdtemp(prefix="ht-", dir="/tmp"))  # AF_UNIX paths must stay short on macOS.
    path = socket_dir / "herdr.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen()
    threading.Thread(target=serve_socket, args=(listener, built["agents_file"]), daemon=True).start()
    print(f"herdr-trace demo: invented data in {directory}; edit {built['agents_file']} to change agent states", flush=True)
    try:
        serve(session="demo", state_dir=directory / "state", journal_path=built["journal_file"], herdr_socket=path,
              claude_dir=built["claude_dir"], codex_dir=built["codex_dir"], port=port, open_browser=open_browser)
    finally:
        listener.close()
        shutil.rmtree(socket_dir, ignore_errors=True)
        if owned:
            shutil.rmtree(directory, ignore_errors=True)
    return 0
