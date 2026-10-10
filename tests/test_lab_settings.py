import json
import tempfile
import unittest
from pathlib import Path

from herdr_trace import journal
from herdr_trace import demo_data as lab
from herdr_trace.lab_view import LabSettings, herdr_session_socket
from support import Server

class LabSettingsChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)

    def test_defaults_then_saved_values_then_launch_options(self):
        settings = LabSettings(self.data)
        default = settings.effective()
        self.assertEqual((default["claude_dir"], default["claude_dir_source"]), (str(Path.home() / ".claude"), "default"))
        self.assertEqual((default["herdr_socket"], default["herdr_socket_source"]), (None, "none"))
        claude = self.data / "claude"
        claude.mkdir()
        saved = settings.save({"claude_dir": str(claude), "herdr_session": "dag-lab"}, journal.atomic_write)
        self.assertEqual((saved["claude_dir"], saved["claude_dir_source"]), (str(claude), "saved"))
        self.assertEqual(saved["herdr_socket"], str(Path.home() / ".config/herdr/sessions/dag-lab/herdr.sock"))
        self.assertEqual(json.loads((self.data / "settings.json").read_text()), {"claude_dir": str(claude), "herdr_session": "dag-lab"})
        fixed = LabSettings(self.data, claude_dir="/opt/claude", herdr_socket="/tmp/x.sock").effective()
        self.assertEqual((fixed["claude_dir_source"], fixed["herdr_socket"], fixed["herdr_socket_source"]),
                         ("option", "/tmp/x.sock", "option"))
        self.assertEqual(sorted(fixed["fixed"]), ["claude_dir", "herdr_socket"])

    def test_explicit_socket_wins_over_session_and_empty_values_clear(self):
        settings = LabSettings(self.data)
        settings.save({"herdr_session": "dag-lab", "herdr_socket": "/tmp/other.sock"}, journal.atomic_write)
        self.assertEqual(settings.effective()["herdr_socket"], "/tmp/other.sock")
        cleared = settings.save({"herdr_session": "", "herdr_socket": ""}, journal.atomic_write)
        self.assertEqual((cleared["herdr_socket"], cleared["herdr_socket_source"]), (None, "none"))

    def test_herdr_web_ui_address_is_off_until_saved_and_must_be_a_plain_web_address(self):
        settings = LabSettings(self.data)
        self.assertIsNone(settings.effective()["herdr_web_url"])
        saved = settings.save({"herdr_web_url": " http://127.0.0.1:7317/ "}, journal.atomic_write)
        self.assertEqual(saved["herdr_web_url"], "http://127.0.0.1:7317")
        self.assertEqual(settings.save({"herdr_web_url": "https://box.tail.ts.net/herdr/"}, journal.atomic_write)["herdr_web_url"],
                         "https://box.tail.ts.net/herdr")
        for value in ("javascript:alert(1)", "ftp://127.0.0.1:7317", "127.0.0.1:7317", "localhost:7317",
                      "http://127.0.0.1:7317/?pane=w1:p1", "http://127.0.0.1:7317/#x", "http://user:secret@127.0.0.1:7317"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "herdr web ui address"):
                    settings.save({"herdr_web_url": value}, journal.atomic_write)
        self.assertEqual(settings.effective()["herdr_web_url"], "https://box.tail.ts.net/herdr")  # A refused value changes nothing.
        self.assertIsNone(settings.save({"herdr_web_url": ""}, journal.atomic_write)["herdr_web_url"])

    def test_invalid_values_are_refused_and_nothing_is_written(self):
        settings = LabSettings(self.data)
        for values, error in (({"herdr_session": "../default"}, "session names"), ({"herdr_session": ".."}, "session names"),
                              ({"claude_dir": str(self.data / "missing")}, "does not exist")):
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, error):
                    settings.save(values, journal.atomic_write)
        self.assertFalse((self.data / "settings.json").exists())
        self.assertEqual(herdr_session_socket("a.b_c-1").name, "herdr.sock")


class LabSettingsRouteChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.lab = lab.build(self.temp.name)
        self.server = Server(self, self.lab)

    def test_nothing_may_frame_the_view(self):
        for path in ("/", "/api/settings"):
            with self.subTest(path=path):
                _, headers, _ = self.server.request(path)
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_settings_name_the_session_keep_launch_options_and_refuse_bad_values(self):
        status, _, raw = self.server.request("/api/settings")
        current = json.loads(raw)
        self.assertEqual((status, current["session"], current["herdr_socket_source"]), (200, "test", "option"))
        self.assertEqual(sorted(current["fixed"]), ["claude_dir", "herdr_socket"])
        status, _, raw = self.server.request("/api/settings", {"herdr_web_url": "http://127.0.0.1:7317/"})
        self.assertEqual((status, json.loads(raw)["herdr_web_url"]), (200, "http://127.0.0.1:7317"))
        self.assertTrue((self.server.state / "settings.json").exists())
        status, _, raw = self.server.request("/api/settings", {"herdr_session": "../x"})
        self.assertEqual(status, 400)
        self.assertIn("session names", json.loads(raw)["error"])
        status, _, raw = self.server.request("/api/bundle")
        self.assertEqual((status, len(json.loads(raw)["edges"])), (200, 9))


if __name__ == "__main__":
    unittest.main()
