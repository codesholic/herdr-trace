"""Local web server for the collaboration view.

It listens on 127.0.0.1 only and every request needs the token printed at start-up: the
view shows transcript text, so another local user or a web page must not read it. The
token arrives once in the address (`/?token=...`), becomes an HttpOnly cookie and leaves
the address bar. Requests must also name this server in Host (DNS rebinding) and, when
they carry an Origin, this server's origin.
"""
import fcntl
import hmac
import json
import os
import secrets
import signal
import socketserver
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import journal
from .lab_view import LabSettings, LabView

PAGE = Path(__file__).with_name("web") / "page.html"
COOKIE = "herdr_trace_token"
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
SIGN_IN = ("<!doctype html><meta charset=utf-8><title>herdr-trace</title><body style=\"font:15px system-ui;padding:24px\">"
           "<p>Open the address that <code>herdr-trace serve</code> printed; it carries the access token.</p>"
           "<p><code>herdr-trace serve</code>가 출력한 주소로 여세요. 주소에 접근 토큰이 들어 있습니다.</p>").encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "herdr-trace"

    def handle(self):
        try:  # A browser can close between stream frames or while HTTP/1.1 waits for the next request.
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def log_request(self, code="-", size="-"):
        try:
            if 200 <= int(code) < 400:
                return  # The page polls and streams; only failures are worth a line.
        except (TypeError, ValueError):
            pass
        super().log_request(code, size)

    def log_message(self, fmt, *args):
        super().log_message(fmt, *(str(v).split("?", 1)[0] if "token=" in str(v) else v for v in args))

    def reply(self, code, data, mime="application/json; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def authorize(self, bootstrap=False):
        origin = self.server.origin
        if self.headers.get("Host") != origin.split("://", 1)[1]:
            self.reply(403, b'{"error":"host rejected"}')
            return False
        if self.headers.get("Origin") not in {None, origin}:
            self.reply(403, b'{"error":"origin rejected"}')
            return False
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            supplied = supplied or (cookie[COOKIE].value if COOKIE in cookie else "")
        except Exception:
            supplied = ""
        if bootstrap:
            supplied = parse_qs(urlsplit(self.path).query).get("token", [supplied])[0]
        if not hmac.compare_digest(supplied.encode("utf-8"), self.server.token.encode("utf-8")):
            if bootstrap:
                self.reply(401, SIGN_IN, "text/html; charset=utf-8")
            else:
                self.reply(401, b'{"error":"authentication required"}')
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        stream_started = False
        try:
            if not self.authorize(bootstrap=path == "/"):
                return
            if path == "/":
                if "token" in parse_qs(urlsplit(self.path).query):
                    return self.reply(303, b"", headers={"Location": "/", "Set-Cookie":
                                      f"{COOKIE}={self.server.token}; Path=/; HttpOnly; SameSite=Strict"})
                return self.reply(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/health":
                return self.reply(200, json.dumps({"status": "ready", "session": self.server.session}).encode())
            if path == "/api/settings":
                value = {"session": self.server.session, "journal": str(self.server.journal_path), **self.server.settings.effective()}
                return self.reply(200, json.dumps(value, ensure_ascii=False).encode())
            if path == "/api/bundle":
                return self.reply(200, json.dumps(self.server.view().build(), ensure_ascii=False).encode())
            if path != "/api/stream":
                return self.reply(404, b'{"error":"not found"}')
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            stream_started = True
            self.close_connection = True
            self.wfile.write(b"retry: 1500\n\n")
            last_revision, last_heartbeat = None, time.monotonic()
            while not self.server.stop.is_set():
                bundle = self.server.view().build()  # Picks up a view rebuilt by a settings change.
                if bundle["revision"] != last_revision:
                    data = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
                    self.wfile.write(f"id: {bundle['revision']}\ndata: {data}\n\n".encode())
                    self.wfile.flush()
                    last_revision = bundle["revision"]
                if time.monotonic() - last_heartbeat > 10:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    last_heartbeat = time.monotonic()
                self.server.stop.wait(1)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as exc:
            if not stream_started:
                return self.reply(500, json.dumps({"error": str(exc)}).encode())
            try:
                self.wfile.write(f"event: source-error\ndata: {json.dumps({'error': str(exc)})}\n\n".encode())
                self.wfile.flush()
            except OSError:
                pass
            self.close_connection = True

    def do_POST(self):
        if not self.authorize():
            return
        if urlsplit(self.path).path != "/api/settings":
            return self.reply(404, b'{"error":"not found"}')
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                raise ValueError("Invalid request size")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError("Settings must be an object")
            result = self.server.settings.save(value, journal.atomic_write)
            self.server.reset_view()
            return self.reply(200, json.dumps(result, ensure_ascii=False).encode())
        except (ValueError, KeyError, TypeError, OSError) as exc:
            return self.reply(400, json.dumps({"error": str(exc)}, ensure_ascii=False).encode())


class LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind resolves the address with getfqdn, which can stall on slow reverse DNS.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def _interrupt(_signum, _frame):
    raise KeyboardInterrupt


def serve(*, session, state_dir, journal_path, herdr_socket, claude_dir, codex_dir, port=0, open_browser=False,
          out=sys.stdout):
    """Serve one herdr session until interrupted. One server per session: a second start is refused."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(state_dir / ".server.lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f"herdr-trace already serves session {session}; see {state_dir / 'server.json'}") from None
        _serve_locked(session, state_dir, Path(journal_path), herdr_socket, claude_dir, codex_dir, port, open_browser, out)


def _serve_locked(session, state_dir, journal_path, herdr_socket, claude_dir, codex_dir, port, open_browser, out):
    httpd = LoopbackServer(("127.0.0.1", port), Handler)
    httpd.session, httpd.journal_path, httpd.stop = session, journal_path, threading.Event()
    httpd.token = secrets.token_urlsafe(32)
    httpd.origin = f"http://127.0.0.1:{httpd.server_port}"
    httpd.settings = LabSettings(state_dir, str(claude_dir) if claude_dir else None, str(herdr_socket) if herdr_socket else None)
    view_lock, current = threading.Lock(), {}

    def view():
        with view_lock:  # Built on first use, so the server costs nothing until the page is opened.
            if "view" not in current:
                settings = httpd.settings.effective()
                current["view"] = LabView(lambda: journal.load(journal_path), settings["claude_dir"], codex_dir,
                                          settings["herdr_socket"], settings["herdr_web_url"])
            return current["view"]

    def reset_view():
        with view_lock:
            old = current.pop("view", None)
        if old:
            old.close()

    httpd.view, httpd.reset_view = view, reset_view
    info_path = state_dir / "server.json"
    info = {"pid": os.getpid(), "url": httpd.origin, "open_url": f"{httpd.origin}/?token={httpd.token}",
            "token": httpd.token, "session": session, "journal": str(journal_path),
            "herdr_socket": str(herdr_socket) if herdr_socket else None, "started_at": journal.now()}
    try:
        with open(info_path, "w", encoding="utf-8", opener=lambda path, flags: os.open(path, flags, 0o600)) as handle:
            json.dump(info, handle)
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, _interrupt)  # A plain kill stops the server like Ctrl+C, cleaning up.
        print(json.dumps(info), file=out, flush=True)
        print(f"herdr-trace: session {session} · open {info['open_url']}", file=sys.stderr, flush=True)
        if open_browser:
            import webbrowser
            webbrowser.open(info["open_url"])
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        httpd.stop.set()
        reset_view()
        httpd.server_close()
        info_path.unlink(missing_ok=True)
