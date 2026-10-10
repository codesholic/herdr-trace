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


class CommandShimChecks(unittest.TestCase):
    """The action that puts herdr-trace on PATH writes only its own small script and removes only that."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.bin = Path(self.temp.name) / "bin"
        self.env = {**environment(self.temp.name), "HERDR_TRACE_BIN_DIR": str(self.bin), "PATH": f"{self.bin}:/usr/bin:/bin"}

    def run_plugin(self, action):
        return subprocess.run(["sh", str(ROOT / "scripts/herdr-plugin.sh"), action], env=self.env, capture_output=True,
                              text=True, timeout=60)

    def test_installed_command_runs_herdr_trace_and_is_removed(self):
        installed = self.run_plugin("install-command")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(installed.stdout.strip(), f"herdr-trace command installed at {self.bin / 'herdr-trace'}")
        ran = subprocess.run(["herdr-trace", "--version"], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual((ran.returncode, ran.stdout.split()[0]), (0, "herdr-trace"), ran.stderr)
        self.assertEqual(self.run_plugin("install-command").returncode, 0)  # Its own file is rewritten.
        removed = self.run_plugin("remove-command")
        self.assertEqual(removed.stdout.strip(), f"herdr-trace command removed from {self.bin / 'herdr-trace'}")
        self.assertFalse((self.bin / "herdr-trace").exists())

    def test_a_file_it_did_not_write_is_left_alone(self):
        self.bin.mkdir()
        (self.bin / "herdr-trace").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        for action in ("install-command", "remove-command"):
            with self.subTest(action=action):
                refused = self.run_plugin(action)
                self.assertEqual(refused.returncode, 1)
                self.assertIn("not written by herdr-trace", refused.stderr)
        self.assertEqual((self.bin / "herdr-trace").read_text(encoding="utf-8"), "#!/bin/sh\necho mine\n")

    def test_a_folder_off_path_is_named(self):
        installed = subprocess.run(["sh", str(ROOT / "scripts/herdr-plugin.sh"), "install-command"],
                                   env={**self.env, "PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=60)
        self.assertIn("is not on PATH", installed.stdout)


class EntryScriptChecks(unittest.TestCase):
    def test_check_names_the_python_it_will_use(self):
        checked = subprocess.run(["sh", str(ROOT / "scripts/herdr-plugin.sh"), "check"], capture_output=True, text=True,
                                 timeout=30, env={**environment("/tmp"), "HERDR_TRACE_PYTHON": sys.executable})
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("herdr-trace will run with Python 3.", checked.stdout)


if __name__ == "__main__":
    unittest.main()
