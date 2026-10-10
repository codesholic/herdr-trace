import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from herdr_trace import herdr_source

AGENT = {"name": "reviewer", "agent": "claude", "agent_status": "working", "pane_id": "w1:p2", "workspace_id": "w1",
         "tab_id": "w1:t1", "agent_session": {"source": "herdr:claude", "agent": "claude", "kind": "id", "value": "s1"}}


class FakeHerdrEvents:
    """Answers ping and agent.list, holds events.subscribe connections open, and can push, drop or reject."""

    def __init__(self, directory):
        self.path = Path(directory) / "e.sock"
        self.requests, self.subscribers, self.reject, self.agents = [], [], False, [AGENT]
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.server.listen()
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(connection,), daemon=True).start()

    def handle(self, connection):
        stream = connection.makefile("rwb")
        request = json.loads(stream.readline())
        self.requests.append(request["method"])
        if request["method"] != "events.subscribe":
            result = ({"type": "pong", "protocol": 22, "version": "0.9.3"} if request["method"] == "ping"
                      else {"type": "agent_list", "agents": self.agents})
            stream.write((json.dumps({"id": request["id"], "result": result}) + "\n").encode())
            stream.flush()
            connection.close()
            return
        if self.reject:
            stream.write((json.dumps({"id": request["id"], "error": {"code": "unsupported", "message": "no events"}}) + "\n").encode())
            stream.flush()
            connection.close()
            return
        subscriptions = request["params"]["subscriptions"]
        if any(item["type"] == "pane.agent_status_changed" and "pane_id" not in item for item in subscriptions):
            # herdr protocol 22 rejects this subscription without a pane_id (seen live on 2026-10-08).
            stream.write((json.dumps({"id": request["id"], "error": {"code": "invalid_request",
                          "message": "invalid request: missing field `pane_id`"}}) + "\n").encode())
            stream.flush()
            connection.close()
            return
        self.kinds = [(item["type"], item.get("pane_id")) for item in subscriptions]
        stream.write((json.dumps({"id": request["id"], "result": {"type": "subscription_started"}}) + "\n").encode())
        stream.flush()
        self.subscribers.append((connection, stream))

    def push(self, kind="pane.agent_status_changed"):
        for connection, stream in list(self.subscribers):
            stream.write((json.dumps({"event": kind, "data": {"pane_id": "w1:p2", "workspace_id": "w1",
                                                              "agent_status": "idle"}}) + "\n").encode())
            stream.flush()

    def drop(self):
        for connection, stream in self.subscribers:
            connection.close()
        self.subscribers.clear()

    def close(self):
        self.drop()
        self.server.close()


def until(condition, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


class EventSubscriptionChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")  # AF_UNIX paths must stay short on macOS.
        self.addCleanup(self.temp.cleanup)
        self.fake = FakeHerdrEvents(self.temp.name)
        self.addCleanup(self.fake.close)
        self.status = herdr_source.HerdrStatus(self.fake.path, interval=60, idle_interval=60, backoff_start=0.05,
                                               backoff_limit=0.2)
        self.addCleanup(self.status.close)

    def lists(self):
        return self.fake.requests.count("agent.list")

    def test_an_event_refreshes_the_agent_list_before_the_poll_interval(self):
        first = self.status.current()
        self.assertEqual(first["agents"][0]["agent_status"], "working")
        self.assertTrue(until(lambda: self.status.events["state"] == "subscribed"))
        self.assertEqual(self.fake.kinds, [(kind, None) for kind in herdr_source.SUBSCRIPTIONS]
                         + [("pane.agent_status_changed", "w1:p2")])
        self.status.current()  # Subscribing marks the list stale once, since events before it were missed.
        settled = self.lists()
        self.status.current()
        self.assertEqual(self.lists(), settled)  # No event, interval not over: no new request.
        self.fake.push()
        self.assertTrue(until(lambda: self.status.events["seen"] == 1))
        state = self.status.current()
        self.assertEqual(self.lists(), settled + 1)
        self.assertEqual((state["events"]["state"], state["events"]["last_kind"]), ("subscribed", "pane.agent_status_changed"))

    def test_a_new_agent_pane_is_subscribed_without_counting_a_failure(self):
        self.status.current()
        self.assertTrue(until(lambda: self.status.events["state"] == "subscribed"))
        self.fake.agents = [AGENT, {**AGENT, "name": "tester", "pane_id": "w1:p3"}]
        self.fake.push("pane.agent_detected")
        self.assertTrue(until(lambda: self.status.events["seen"] == 1))
        self.status.current()
        self.assertTrue(until(lambda: ("pane.agent_status_changed", "w1:p3") in self.fake.kinds))
        self.assertEqual(self.status.events["reconnects"], 0)
        self.assertTrue(until(lambda: self.status.events.get("panes") == ["w1:p2", "w1:p3"]))

    def test_a_dropped_subscription_is_reported_and_reconnects(self):
        self.status.current()
        self.assertTrue(until(lambda: self.status.events["state"] == "subscribed"))
        self.fake.drop()
        self.assertTrue(until(lambda: self.status.events["reconnects"] >= 1))
        self.assertTrue(until(lambda: self.status.events["state"] == "subscribed" and self.fake.subscribers))
        self.assertGreaterEqual(self.fake.requests.count("events.subscribe"), 2)

    def test_a_rejected_subscription_falls_back_to_polling_and_says_why(self):
        self.fake.reject = True
        self.status.interval = 0
        self.status.current()
        self.assertTrue(until(lambda: self.status.events["state"] == "reconnecting"))
        self.assertIn("events.subscribe refused", self.status.events["detail"])
        before = self.lists()
        self.status.current()
        self.assertGreater(self.lists(), before)  # Polling continues at the short interval.

    def test_close_stops_the_subscription_thread(self):
        self.status.current()
        self.assertTrue(until(lambda: self.status.events["state"] == "subscribed"))
        self.status.close()
        self.assertFalse(self.status.thread.is_alive())
        self.assertEqual(self.status.events["state"], "off")

    def test_without_a_socket_nothing_starts(self):
        idle = herdr_source.HerdrStatus(None)
        self.assertEqual(idle.current()["status"], "not_configured")
        self.assertIsNone(idle.thread)


if __name__ == "__main__":
    unittest.main()
