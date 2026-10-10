import json
import subprocess
import sys
import tempfile
import unittest

from herdr_trace import demo_data as lab
from support import Server, environment


class AccessChecks(unittest.TestCase):
    """The view shows transcript text, so every request needs the token and must name this server."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.lab = lab.build(self.temp.name)
        self.server = Server(self, self.lab)

    def test_requests_without_or_with_a_wrong_token_are_refused(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Cookie": "herdr_trace_token=wrong"}):
            with self.subTest(headers=headers):
                status, _, body = self.server.request("/api/bundle", headers=headers, token=False)
                self.assertEqual((status, json.loads(body)), (401, {"error": "authentication required"}))
        status, headers, page = self.server.request("/", token=False)
        self.assertEqual((status, headers["Content-Type"]), (401, "text/html; charset=utf-8"))
        self.assertIn("herdr-trace serve", page.decode("utf-8"))
        status, _, _ = self.server.request("/?token=wrong", token=False)
        self.assertEqual(status, 401)

    def test_token_in_the_address_becomes_a_private_cookie_and_leaves_the_address(self):
        status, headers, _ = self.server.request(f"/?token={self.server.token}", token=False)
        self.assertEqual((status, headers["Location"]), (303, "/"))
        cookie = headers["Set-Cookie"]
        self.assertTrue(cookie.startswith(f"herdr_trace_token={self.server.token};"))
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        status, _, body = self.server.request("/api/bundle", headers={"Cookie": cookie.split(";")[0]}, token=False)
        self.assertEqual((status, len(json.loads(body)["edges"])), (200, 9))

    def test_other_host_names_and_foreign_origins_are_refused_even_with_the_token(self):
        port = self.server.origin.rsplit(":", 1)[1]
        for headers, error in (({"Host": f"localhost:{port}"}, "host rejected"),
                               ({"Host": f"attacker.example:{port}"}, "host rejected"),
                               ({"Origin": "http://attacker.example"}, "origin rejected")):
            with self.subTest(headers=headers):
                status, _, body = self.server.request("/api/bundle", headers=headers)
                self.assertEqual((status, json.loads(body)["error"]), (403, error))
        status, _, _ = self.server.request("/api/settings", {"herdr_web_url": ""}, headers={"Origin": "http://attacker.example"})
        self.assertEqual(status, 403)

    def test_one_server_per_session_with_a_private_record_removed_at_exit(self):
        record = self.server.state / "server.json"
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        info = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual((info["token"], info["url"], info["session"]), (self.server.token, self.server.origin, "test"))
        self.assertEqual(info["open_url"], f"{self.server.origin}/?token={self.server.token}")
        second = subprocess.run([sys.executable, "-m", "herdr_trace", "serve", "--session", "test",
                                 "--journal", str(self.lab["journal_file"]), "--socket", str(self.lab["base"] / "x.sock")],
                                env=environment(self.lab["base"]), capture_output=True, text=True, timeout=30)
        self.assertEqual(second.returncode, 1)
        self.assertIn("already serves session test", second.stderr)
        self.server.close()
        self.assertFalse(record.exists())


if __name__ == "__main__":
    unittest.main()
