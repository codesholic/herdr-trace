import json
import re
import tempfile
import unittest
from pathlib import Path

from herdr_trace import demo_data as lab
from support import ROOT, Server

PAGE = (ROOT / "src/herdr_trace/web/page.html").read_text(encoding="utf-8")
TABLE = re.search(r"^const STRINGS = (\{.*\});$", PAGE, re.M)


class WordingChecks(unittest.TestCase):
    """Every word of the page comes from the en/ko table; the table covers what the page asks for."""

    def setUp(self):
        self.assertIsNotNone(TABLE, "the page must keep its wording in one STRINGS line")
        self.strings = json.loads(TABLE.group(1))

    def test_no_korean_outside_the_table(self):
        outside = PAGE.replace(TABLE.group(0), "")
        self.assertEqual(re.findall(r"[가-힣]+", outside), [])

    def test_both_languages_have_the_same_keys(self):
        self.assertEqual(set(self.strings["en"]), set(self.strings["ko"]))

    def test_every_key_the_page_names_exists(self):
        named = set(re.findall(r'\bt\("([a-z_]+)"', PAGE))
        named |= set(re.findall(r'data-i18n(?:-aria|-title)?="([a-z_]+)"', PAGE))
        named |= set(re.findall(r'label: "((?:kind|cat)_[a-z_]+)"', PAGE))
        named = {key for key in named if not key.endswith("_")}  # Prefixes of codes; the next test covers them.
        self.assertEqual(sorted(named - set(self.strings["en"])), [])

    def test_codes_from_the_server_have_words(self):
        expected = {f"st_{s}" for s in ("confirmed", "confirmed_modified", "queued", "pending_busy", "unconfirmed", "undelivered")}
        expected |= {f"rs_{r}" for r in ("no_header", "unknown_message", "other_recipient", "before_journal",
                                         "no_recipient_record", "late_recipient_not_found")}
        expected |= {f"var_{v}" for v in ("line_endings", "trailing_spaces", "outer_whitespace")}
        expected |= {f"ag_{a}" for a in ("idle", "working", "blocked", "done", "unknown")}
        expected |= {f"verify_{m}" for m in ("pane_env", "agent_session_env", "process_ancestry", "declared")}
        expected |= {f"src_{s}" for s in ("option", "saved", "default", "none")}
        self.assertEqual(sorted(expected - set(self.strings["en"])), [])


class LanguageOptionChecks(unittest.TestCase):
    def test_the_server_names_its_default_language(self):
        temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(temp.cleanup)
        built = lab.build(temp.name)
        for extra, expected in (((), "en"), (("--lang", "ko"), "ko")):
            with self.subTest(expected=expected):
                server = Server(self, built, *extra, session=f"lang-{expected}")
                status, _, raw = server.request("/api/settings")
                self.assertEqual((status, json.loads(raw)["lang"]), (200, expected))
                server.close()


if __name__ == "__main__":
    unittest.main()
