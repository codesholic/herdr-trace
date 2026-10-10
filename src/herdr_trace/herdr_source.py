"""Read-only herdr socket client for the lab view.

It connects to an existing socket and never starts a server: the CLI form
`herdr --session NAME ...` may create a session, a socket connection cannot.
Only read methods are allowed, so the monitor cannot control agents.
"""
import json
import socket
import threading
import time

READ_METHODS = {"ping", "agent.list", "agent.get", "session.snapshot"}
# Pane events that can change which agents exist or their state; each one triggers a fresh agent.list.
# pane.agent_status_changed requires a pane_id (protocol 22), so it is subscribed per agent pane.
SUBSCRIPTIONS = ("pane.agent_detected", "pane.created", "pane.closed", "pane.exited", "pane.moved")
PER_PANE = "pane.agent_status_changed"
PROTOCOL = 22
STATUS_LABELS = {"idle": "대기", "working": "작업 중", "blocked": "승인·질문 대기", "done": "완료(미확인)",
                 "unknown": "판정 불가"}


class HerdrUnavailable(Exception):
    pass


def request(path, method, params=None, timeout=2.0):
    if method not in READ_METHODS:
        raise ValueError(f"herdr method is not read-only: {method}")
    identity = f"herdr-trace-{time.monotonic_ns()}"
    line = json.dumps({"id": identity, "method": method, "params": params or {}}) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(path))
            client.sendall(line.encode("utf-8"))
            buffer = b""
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    raise HerdrUnavailable("herdr closed the connection without a reply")
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    reply = json.loads(raw)
                    if reply.get("id") != identity:
                        continue  # Not our reply.
                    if "error" in reply:
                        error = reply["error"] or {}
                        raise HerdrUnavailable(f"{error.get('code')}: {error.get('message')}")
                    return reply.get("result") or {}
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise HerdrUnavailable(f"소켓에 연결할 수 없다: {exc.strerror or exc}") from exc
    except (socket.timeout, OSError, ValueError) as exc:
        raise HerdrUnavailable(f"herdr 응답 오류: {exc}") from exc


class HerdrStatus:
    """Agent list from one socket, refreshed on herdr events with polling as the fallback.

    A subscription thread keeps one events.subscribe connection open and reconnects with
    backoff. Events carry no session IDs, so an event only marks the list stale; the
    next current() call reads agent.list again. Every failure is reported, never hidden.
    """

    def __init__(self, socket_path=None, interval=2.0, idle_interval=15.0, subscribe=True, backoff_start=1.0,
                 backoff_limit=30.0):
        self.socket_path, self.interval, self.idle_interval = socket_path, interval, idle_interval
        self.subscribe, self.backoff_start, self.backoff_limit = subscribe and bool(socket_path), backoff_start, backoff_limit
        self.checked, self.state = 0.0, {"configured": bool(socket_path), "status": "not_configured",
                                         "detail": "herdr 소켓을 지정하지 않아 조회하지 않는다", "agents": []}
        self.events = {"state": "off" if self.subscribe else "disabled", "seen": 0, "last_at": None, "last_kind": None,
                       "detail": None, "reconnects": 0}
        self.stale, self.stopping, self.thread = threading.Event(), threading.Event(), None
        self.panes, self.subscribed_panes, self.resubscribe = (), None, threading.Event()

    def current(self):
        if not self.socket_path:
            return self.state
        interval = self.idle_interval if self.events["state"] == "subscribed" else self.interval
        if not self.stale.is_set() and time.monotonic() - self.checked < interval:
            return {**self.state, "events": dict(self.events)}
        self.stale.clear()
        self.checked = time.monotonic()
        state = {"configured": True, "socket": str(self.socket_path), "checked_at": time.time()}
        try:
            pong = request(self.socket_path, "ping")
            agents = request(self.socket_path, "agent.list").get("agents", [])
            protocol = pong.get("protocol")
            state.update(status="ok" if protocol == PROTOCOL else "protocol_mismatch", version=pong.get("version"),
                         protocol=protocol, agents=agents,
                         detail=None if protocol == PROTOCOL else f"protocol {protocol} (기준 {PROTOCOL})")
        except HerdrUnavailable as exc:
            state.update(status="unavailable", detail=str(exc), agents=[])
        self.state = state
        self.panes = tuple(sorted({a["pane_id"] for a in state["agents"] if a.get("pane_id")}))
        if self.subscribed_panes is not None and self.panes != self.subscribed_panes:
            self.resubscribe.set()  # Agents moved, came or went: subscribe to their panes again.
        if self.subscribe and self.thread is None:
            self.thread = threading.Thread(target=self.listen, name="herdr-events", daemon=True)
            self.thread.start()
        return {**state, "events": dict(self.events)}

    def close(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=5)

    def listen(self):
        backoff = self.backoff_start
        while not self.stopping.is_set():
            self.subscribed_once = False
            try:
                self.follow()
            except (OSError, ValueError, HerdrUnavailable) as exc:
                detail = str(exc)
            else:
                detail = "herdr closed the event connection"
            if self.stopping.is_set():
                break
            if self.resubscribe.is_set():
                self.resubscribe.clear()
                continue  # Planned: a new pane set, not a failure.
            self.events.update(state="reconnecting", detail=detail, reconnects=self.events["reconnects"] + 1)
            self.stale.set()  # Missed events while disconnected: read the list again.
            if self.subscribed_once:
                backoff = self.backoff_start  # The last connection worked; start over gently.
            if self.stopping.wait(backoff):
                break
            backoff = min(backoff * 2, self.backoff_limit)
        self.events["state"] = "off"

    def follow(self):
        identity = f"herdr-trace-events-{time.monotonic_ns()}"
        panes = self.panes
        kinds = [{"type": kind} for kind in SUBSCRIPTIONS] + [{"type": PER_PANE, "pane_id": pane} for pane in panes]
        line = json.dumps({"id": identity, "method": "events.subscribe", "params": {"subscriptions": kinds}}) + "\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1.0)
            client.connect(str(self.socket_path))
            client.sendall(line.encode("utf-8"))
            buffer = b""
            while not self.stopping.is_set() and not self.resubscribe.is_set():
                try:
                    chunk = client.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    message = json.loads(raw)
                    if message.get("id") == identity:
                        if "error" in message:
                            error = message["error"] or {}
                            raise HerdrUnavailable(f"events.subscribe 거부: {error.get('code')}: {error.get('message')}")
                        self.subscribed_panes = panes
                        self.events.update(state="subscribed", detail=None, panes=list(panes))
                        self.stale.set()
                        self.subscribed_once = True
                    elif "event" in message:
                        self.events.update(seen=self.events["seen"] + 1, last_at=time.time(), last_kind=message.get("event"))
                        self.stale.set()
