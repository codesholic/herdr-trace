"""Start a real `herdr-trace serve` on invented data for route tests.

The server reads a socket path that does not exist and keeps its state in the test's
temporary folder, so no test touches a real herdr session or the user's state.
"""
import http.client
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


# Variables that would let a test act on a real herdr session or claim a real agent session. Tests run from
# inside herdr panes too, so they are removed, and herdr itself points at a path that does not exist.
LIVE = ("HERDR_", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")


def environment(base):
    kept = {key: value for key, value in os.environ.items() if not key.startswith(LIVE)}
    return {**kept, "PYTHONPATH": str(ROOT / "src"), "HERDR_TRACE_STATE_DIR": str(Path(base) / "state"),
            "HOME": str(base), "HERDR_TRACE_HERDR_BIN": str(Path(base) / "no-herdr-in-tests")}


class Server:
    def __init__(self, case, lab, *extra, session="test", socket=None):
        base = Path(lab["base"])
        command = [sys.executable, "-m", "herdr_trace", "serve", "--session", session,
                   "--journal", str(lab["journal_file"]), "--codex-dir", str(lab["codex_dir"]),
                   "--claude-dir", str(lab["claude_dir"]), "--socket", str(socket or base / "missing.sock"), *extra]
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                        env=environment(base))
        case.addCleanup(self.close)
        line = self.process.stdout.readline()
        if not line:
            raise AssertionError(self.process.stderr.read())
        self.info = json.loads(line)
        self.origin, self.token = self.info["url"], self.info["token"]
        self.state = base / "state" / "sessions" / session

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(10)
        self.process.stdout.close()
        self.process.stderr.close()

    def request(self, path, value=None, headers=None, token=True, method=None):
        """One request without following redirects; returns (status, headers, body bytes)."""
        address = urlsplit(self.origin)
        connection = http.client.HTTPConnection(address.hostname, address.port, timeout=10)
        try:
            sent = {**({"Authorization": f"Bearer {self.token}"} if token else {}), **(headers or {})}
            body = None if value is None else json.dumps(value).encode()
            if body is not None:
                sent.setdefault("Content-Type", "application/json")
            connection.request(method or ("GET" if body is None else "POST"), path, body=body, headers=sent)
            reply = connection.getresponse()
            return reply.status, reply.headers, reply.read()
        finally:
            connection.close()

    def stream_frame(self):
        """The first data frame of the event stream."""
        address = urlsplit(self.origin)
        connection = http.client.HTTPConnection(address.hostname, address.port, timeout=10)
        try:
            connection.request("GET", "/api/stream", headers={"Authorization": f"Bearer {self.token}"})
            reply = connection.getresponse()
            for line in iter(reply.readline, b""):
                if line.startswith(b"data: "):
                    return json.loads(line[6:])
        finally:
            connection.close()
