import http.client
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from herdr_trace import paths
from support import environment


def run(*args, base, input=None):
    return subprocess.run([sys.executable, "-m", "herdr_trace", *args], env=environment(base), capture_output=True,
                          text=True, input=input, timeout=30)


class CommandChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_rules_name_the_session_and_the_send_command(self):
        printed = run("rules", "--session", "work", base=self.base)
        self.assertEqual(printed.returncode, 0, printed.stderr)
        self.assertIn("herdr-trace send --to <agent name>", printed.stdout)
        self.assertIn("--herdr-session work", printed.stdout)
        self.assertNotIn("{session}", printed.stdout)

    def test_send_outside_herdr_journals_and_sends_nothing(self):
        journal = self.base / "events.jsonl"
        refused = run("send", "--to", "reviewer", "--kind", "message", "--message", "hi", "--journal", str(journal),
                      base=self.base)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("herdr pane", refused.stderr)
        self.assertFalse(journal.exists())

    def test_session_names_are_checked_before_any_folder_is_made(self):
        refused = run("rules", "--session", "../x", base=self.base)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("session names", refused.stderr)

    def test_demo_serves_invented_data_behind_the_token(self):
        demo = subprocess.Popen([sys.executable, "-m", "herdr_trace", "demo", "--dir", str(self.base / "demo")],
                                env=environment(self.base), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (demo.terminate(), demo.wait(10), demo.stdout.close(), demo.stderr.close()))
        self.assertIn("invented data", demo.stdout.readline())
        info = json.loads(demo.stdout.readline())
        address = urlsplit(info["url"])

        def get(path, token):
            connection = http.client.HTTPConnection(address.hostname, address.port, timeout=10)
            connection.request("GET", path, headers={"Authorization": f"Bearer {token}"})
            reply = connection.getresponse()
            return reply.status, reply.read()

        self.assertEqual(get("/api/bundle", "wrong")[0], 401)
        status, raw = get("/api/bundle", info["token"])
        bundle = json.loads(raw)
        self.assertEqual((status, bundle["herdr"]["status"], info["session"]), (200, "ok", "demo"))
        self.assertEqual(sorted(a["name"] for a in bundle["agents"] if a["live"]), ["lead", "reviewer"])
        self.assertEqual(len([e for e in bundle["edges"] if e["basis"] == "journal"]), 7)


class PathChecks(unittest.TestCase):
    def test_state_lives_under_xdg_state_and_sockets_follow_herdr_layout(self):
        env = {"HOME": "/home/u"}
        self.assertEqual(paths.journal_file("work", {"XDG_STATE_HOME": "/s"}), Path("/s/herdr-trace/sessions/work/events.jsonl"))
        self.assertEqual(paths.state_root({"HERDR_TRACE_STATE_DIR": "/x"}), Path("/x"))
        self.assertEqual(paths.session_name(None, {"HERDR_SESSION": "work"}), "work")
        self.assertEqual(paths.session_name(None, {}), "default")
        self.assertEqual(paths.herdr_socket("work", {"XDG_CONFIG_HOME": "/c"}), Path("/c/herdr/sessions/work/herdr.sock"))
        self.assertEqual(paths.herdr_socket("default", {"XDG_CONFIG_HOME": "/c"}), Path("/c/herdr/herdr.sock"))
        inside = {"HERDR_SESSION": "work", "HERDR_SOCKET_PATH": "/run/h.sock"}
        self.assertEqual(paths.herdr_socket("work", inside), Path("/run/h.sock"))
        self.assertEqual(paths.herdr_socket("other", {**inside, "XDG_CONFIG_HOME": "/c"}), Path("/c/herdr/sessions/other/herdr.sock"))
        for bad in ("", "..", "a/b"):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "session names"):
                    paths.session_dir(bad, env)


if __name__ == "__main__":
    unittest.main()
