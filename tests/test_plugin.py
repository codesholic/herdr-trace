import json
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

from herdr_trace import plugin
from support import ROOT, environment


class PluginCommandChecks(unittest.TestCase):
    """What herdr's startup hook and actions run, with the variables herdr hands a plugin."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.opened = self.base / "opened.txt"
        browser = self.base / "browser.sh"  # Records what would be opened; no real browser starts.
        browser.write_text(f"#!/bin/sh\necho \"$1\" >> {self.opened}\n", encoding="utf-8")
        browser.chmod(0o755)
        self.env = {**environment(self.base), "HERDR_SESSION": "work", "HERDR_SOCKET_PATH": str(self.base / "missing.sock"),
                    "BROWSER": str(browser)}
        self.state = self.base / "state" / "sessions" / "work"
        self.addCleanup(lambda: self.run_plugin("stop"))

    def run_plugin(self, action, **extra):
        return subprocess.run(["sh", str(ROOT / "scripts/herdr-plugin.sh"), action], env={**self.env, **extra},
                              capture_output=True, text=True, timeout=60)

    def test_start_detaches_once_per_session_and_stop_ends_it(self):
        started = self.run_plugin("start")
        self.assertEqual(started.returncode, 0, started.stderr)
        info = json.loads((self.state / "server.json").read_text(encoding="utf-8"))
        self.assertEqual(started.stdout.strip(), f"herdr-trace serves session work at {info['url']}")
        self.assertNotIn(info["token"], started.stdout + started.stderr)  # herdr keeps plugin output in its log.
        self.assertEqual(info["herdr_socket"], str(self.base / "missing.sock"))
        self.assertEqual((self.state / "server.log").stat().st_mode & 0o777, 0o600)
        again = self.run_plugin("start")
        self.assertEqual(again.stdout.strip(), f"herdr-trace already serves session work at {info['url']}")
        self.assertEqual(json.loads((self.state / "server.json").read_text(encoding="utf-8"))["pid"], info["pid"])
        self.assertIn("serves session work", self.run_plugin("status").stdout)
        stopped = self.run_plugin("stop")
        self.assertEqual(stopped.stdout.strip(), "herdr-trace stopped serving session work")
        self.assertFalse((self.state / "server.json").exists())
        self.assertIn("is not serving", self.run_plugin("status").stdout)

    def test_open_starts_when_needed_and_hands_the_token_only_to_the_browser(self):
        opened = self.run_plugin("open")
        self.assertEqual(opened.returncode, 0, opened.stderr)
        info = json.loads((self.state / "server.json").read_text(encoding="utf-8"))
        self.assertEqual(self.opened.read_text(encoding="utf-8").split(), [info["open_url"]])
        self.assertNotIn(info["token"], opened.stdout + opened.stderr)

    def test_settings_file_can_open_the_view_at_start_in_korean(self):
        config = self.base / "plugin-config"
        config.mkdir()
        (config / "env").write_text("# herdr-trace\nHERDR_TRACE_AUTO_OPEN=1\nHERDR_TRACE_LANG='ko'\n", encoding="utf-8")
        started = self.run_plugin("start", HERDR_PLUGIN_CONFIG_DIR=str(config))
        self.assertEqual(started.returncode, 0, started.stderr)
        info = json.loads((self.state / "server.json").read_text(encoding="utf-8"))
        self.assertEqual(self.opened.read_text(encoding="utf-8").split(), [info["open_url"]])
        request = urllib.request.Request(info["url"] + "/api/settings", headers={"Authorization": f"Bearer {info['token']}"})
        with urllib.request.urlopen(request, timeout=10) as reply:
            self.assertEqual(json.loads(reply.read())["lang"], "ko")

    def test_settings_prefer_the_environment_over_the_file(self):
        config = self.base / "plugin-config"
        config.mkdir()
        (config / "env").write_text("HERDR_TRACE_LANG=ko\nNOT_OURS=1\n", encoding="utf-8")
        values = plugin.settings({"HERDR_PLUGIN_CONFIG_DIR": str(config), "HERDR_TRACE_LANG": "en"})
        self.assertEqual(values["HERDR_TRACE_LANG"], "en")


class EntryScriptChecks(unittest.TestCase):
    def test_check_names_the_python_it_will_use(self):
        checked = subprocess.run(["sh", str(ROOT / "scripts/herdr-plugin.sh"), "check"], capture_output=True, text=True,
                                 timeout=30, env={**environment("/tmp"), "HERDR_TRACE_PYTHON": sys.executable})
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("herdr-trace will run with Python 3.", checked.stdout)


if __name__ == "__main__":
    unittest.main()
