import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from herdr_trace import bridge as herdr_bridge
from herdr_trace import journal

ROOT = Path(__file__).resolve().parents[1]
CLI = [sys.executable, "-m", "herdr_trace", "send"]
FAKE_HERDR = """#!{python}
import json, os, sys
scenario = json.load(open(os.environ["FAKE_HERDR_SCENARIO"], encoding="utf-8"))
args = sys.argv[1:]
session = None
if args[:1] == ["--session"]:
    session, args = args[1], args[2:]
entry = {{"argv": args, "session": session}}
def reply(stream, payload, code=0):
    with open(os.environ["FAKE_HERDR_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps(entry, ensure_ascii=False) + "\\n")
    print(json.dumps(payload, ensure_ascii=False), file=stream)
    sys.exit(code)
if args[:2] == ["agent", "get"]:
    info = scenario["agents"].get(args[2])
    if info is None:
        reply(sys.stderr, {{"id": "cli", "error": {{"code": "agent_not_found", "message": args[2]}}}}, 1)
    reply(sys.stdout, {{"id": "cli", "result": {{"type": "agent_info", "agent": info}}}})
if args[:2] == ["pane", "process-info"]:
    info = scenario.get("process_info", {{}}).get(args[3])
    if info is None:
        reply(sys.stderr, {{"id": "cli", "error": {{"code": "pane_not_found", "message": args[3]}}}}, 1)
    reply(sys.stdout, {{"id": "cli", "result": {{"type": "pane_process_info", "process_info": info}}}})
if args[:2] == ["agent", "prompt"]:
    journal = os.environ["FAKE_HERDR_JOURNAL"]
    entry["journal_at_prompt"] = open(journal, encoding="utf-8").read() if os.path.exists(journal) else None
    behavior = scenario.get("prompt", {{}})
    if "error" in behavior:
        reply(sys.stderr, {{"id": "cli", "error": behavior["error"]}}, 1)
    after = dict(scenario["agents"][args[2]], agent_status="working", state_change_seq=8)
    reply(sys.stdout, {{"id": "cli", "result": {{"type": "agent_prompted", "agent": after}}}})
reply(sys.stderr, {{"id": "cli", "error": {{"code": "unsupported", "message": " ".join(args)}}}}, 1)
"""
LEAD = {"name": "lead", "agent": "codex", "agent_status": "working", "pane_id": "w1:p1", "tab_id": "w1:t1",
        "workspace_id": "w1", "cwd": "/sandbox", "state_change_seq": 3, "completion_seq": 1,
        "agent_session": {"source": "herdr:codex", "agent": "codex", "kind": "id",
                          "value": "019a0000-0000-7000-8000-00000000000a"}}
REVIEWER = {"name": "reviewer", "agent": "claude", "agent_status": "idle", "pane_id": "w1:p2", "tab_id": "w1:t1",
            "workspace_id": "w1", "cwd": "/sandbox", "state_change_seq": 5, "completion_seq": 2,
            "agent_session": {"source": "herdr:claude", "agent": "claude", "kind": "path",
                              "value": "/home/u/.claude/projects/-sandbox/3f2c0000-0000-4000-8000-0000000000bb.jsonl"}}


class BridgeCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.data, self.journal = self.base / "data", self.base / "journal"
        self.fake = self.base / "herdr"
        self.fake.write_text(FAKE_HERDR.format(python=sys.executable), encoding="utf-8")
        self.fake.chmod(0o755)
        self.log = self.base / "herdr-calls.jsonl"
        self.scenario = self.base / "scenario.json"
        self.use({})
        self.env = {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1", "HERDR_SOCKET_PATH": "/tmp/lab.sock"}
        os.environ.update(FAKE_HERDR_SCENARIO=str(self.scenario), FAKE_HERDR_LOG=str(self.log),
                          FAKE_HERDR_JOURNAL=str(self.journal / "events.jsonl"))
        self.addCleanup(self.temp.cleanup)
        for key in ("FAKE_HERDR_SCENARIO", "FAKE_HERDR_LOG", "FAKE_HERDR_JOURNAL"):
            self.addCleanup(os.environ.pop, key, None)

    def use(self, prompt):
        self.scenario.write_text(json.dumps({"agents": {"w1:p1": LEAD, "lead": LEAD, "reviewer": REVIEWER},
                                             "process_info": {"w1:p1": {"pane_id": "w1:p1", "shell_pid": 500,
                                                                        "foreground_processes": [{"pid": 510}]}},
                                             "prompt": prompt}), encoding="utf-8")

    def send(self, body="Review the diff.\n", **options):
        return herdr_bridge.send("reviewer", "assignment", body, journal_path=self.journal / "events.jsonl",
                                 environ=self.env, herdr_bin=str(self.fake), **options)

    def events(self):
        path = self.journal / "events.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def calls(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []


class BridgeChecks(BridgeCase):
    def test_journal_precedes_prompt_and_holds_the_exact_submitted_text(self):
        outcome = self.send("Review the diff.\nReport findings only.\n\n")
        message, delivery = self.events()
        prompt = self.calls()[-1]
        self.assertEqual(prompt["argv"][:3], ["agent", "prompt", "reviewer"])
        submitted = prompt["argv"][3]
        self.assertEqual(message["message"].encode("utf-8"), submitted.encode("utf-8"))
        self.assertEqual(submitted, f"[herdr-trace msg={outcome['message_id']} from=lead kind=assignment]\n"
                                    "Review the diff.\nReport findings only.")
        # The fake herdr saw the journaled message, and no outcome yet, when it received the prompt.
        seen = [json.loads(line) for line in prompt["journal_at_prompt"].splitlines()]
        self.assertEqual([(e["type"], e.get("message_id")) for e in seen], [("agent.message", outcome["message_id"])])
        self.assertLess(message["seq"], delivery["seq"])
        self.assertEqual(delivery["outcome"], "submitted")
        self.assertEqual(delivery["herdr"]["recipient_after"]["agent_status"], "working")

    def test_message_names_both_sessions_by_provider_key_and_keeps_herdr_details_for_display(self):
        self.send()
        message = self.events()[0]
        self.assertEqual(message["sender_agent_key"], "codex:019a0000-0000-7000-8000-00000000000a")
        self.assertEqual(message["recipient_agent_key"], "claude:3f2c0000-0000-4000-8000-0000000000bb")
        self.assertEqual(message["recipient_session_id"], "3f2c0000-0000-4000-8000-0000000000bb")
        self.assertEqual((message["from_agent"], message["to_agent"], message["message_kind"]),
                         ("lead", "reviewer", "assignment"))
        self.assertEqual(message["herdr"]["recipient_before"]["pane_id"], "w1:p2")
        self.assertEqual(message["herdr"]["recipient_before"]["completion_seq"], 2)
        self.assertEqual(message["transport"], "herdr")

    def test_blocked_recipient_appends_an_undelivered_outcome_without_editing_the_message(self):
        self.use({"error": {"code": "agent_blocked", "message": "approval dialog"}})
        outcome = self.send()
        message, delivery = self.events()
        self.assertEqual(delivery["outcome"], "agent_blocked")
        self.assertEqual(delivery["message_id"], message["message_id"])
        self.assertEqual(outcome["returncode"], 1)
        self.assertTrue(message["message"].startswith("[herdr-trace msg="))

    def test_stall_and_timeout_keep_their_codes_and_other_codes_stay_verbatim(self):
        for code, expected in (("agent_prompt_stalled", "agent_prompt_stalled"), ("timeout", "timeout"),
                               ("pane_not_found", "error")):
            with self.subTest(code=code):
                self.use({"error": {"code": code, "message": "x"}})
                outcome = self.send(wait=True, timeout_ms=1000)
                self.assertEqual((outcome["outcome"], outcome["error_code"]), (expected, code))
        prompts = [c["argv"] for c in self.calls() if c["argv"][:2] == ["agent", "prompt"]]
        self.assertEqual(prompts[-1][4:], ["--wait", "--timeout", "1000"])

    def test_outside_herdr_nothing_is_journaled_or_sent(self):
        self.env = {}
        with self.assertRaisesRegex(ValueError, "herdr pane"):
            self.send()
        self.assertEqual(self.events(), [])
        self.assertEqual(self.calls(), [])

    def test_failed_journal_write_prevents_the_prompt(self):
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.journal.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(OSError):
            self.send()
        self.assertFalse([c for c in self.calls() if c["argv"][:2] == ["agent", "prompt"]])

    def test_input_validation(self):
        for to, kind, body, error in (("Reviewer", "assignment", "x", "agent name"), ("reviewer", "review", "x", "Kind"),
                                      ("reviewer", "assignment", " \n", "empty")):
            with self.subTest(to=to, kind=kind):
                with self.assertRaisesRegex(ValueError, error):
                    herdr_bridge.send(to, kind, body, journal_path=self.journal / "events.jsonl",
                                      environ=self.env, herdr_bin=str(self.fake))
        self.assertEqual(self.events(), [])

    def test_header_round_trip(self):
        text = herdr_bridge.compose("0123456789ab", "lead", "result", "LGTM\n")
        self.assertEqual(text, "[herdr-trace msg=0123456789ab from=lead kind=result]\nLGTM")
        self.assertEqual(herdr_bridge.parse_header(text),
                         {"message_id": "0123456789ab", "from_agent": "lead", "kind": "result"})
        self.assertIsNone(herdr_bridge.parse_header("please review [herdr-trace msg=0123456789ab from=lead kind=result]"))
        # Journals of the codex-dag-herdr experiment used the earlier header; they still read.
        self.assertEqual(herdr_bridge.parse_header("[herdr-trace msg=0123456789ab from=lead kind=result]\nLGTM")["message_id"],
                         "0123456789ab")

    def test_command_line_exit_codes(self):
        body = self.base / "body.md"
        body.write_text("Review the diff.\n", encoding="utf-8")
        env = {**os.environ, **self.env, "HERDR_TRACE_HERDR_BIN": str(self.fake), "PYTHONPATH": str(ROOT / "src")}
        command = [*CLI, "--to", "reviewer", "--kind", "assignment", "--message-file", str(body),
                   "--journal", str(self.journal / "events.jsonl")]
        sent = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertEqual(json.loads(sent.stdout)["outcome"], "submitted")
        self.use({"error": {"code": "agent_blocked", "message": "approval dialog"}})
        blocked = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertEqual(blocked.returncode, 2, blocked.stderr)
        self.assertEqual(json.loads(blocked.stdout)["outcome"], "agent_blocked")
        self.assertEqual([e["type"] for e in self.events()], ["agent.message", "agent.delivery"] * 2)


class SenderIdentityChecks(BridgeCase):
    """Where an agent tool strips the pane variables (Codex with inherit = "core")."""

    def setUp(self):
        super().setUp()
        self.env = {"PATH": os.environ.get("PATH", "")}

    def claim(self, lineage=lambda: None, **options):
        return self.send(declared="lead", session="dag-lab", lineage=lineage, **options)

    def verification(self):
        return self.events()[0]["herdr"]["sender_verification"]

    def herdr_calls(self):
        return [c["argv"][:2] for c in self.calls()]

    def test_pane_variables_identify_the_sender_and_a_different_claim_is_refused(self):
        self.env = {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1"}
        self.send()
        self.assertEqual(self.verification(), {"method": "pane_env", "detail": "HERDR_PANE_ID=w1:p1"})
        with self.assertRaisesRegex(ValueError, "does not match the agent in this pane"):
            self.send(declared="reviewer")
        self.assertEqual(len(self.events()), 2)

    def test_matching_session_variable_verifies_the_claim(self):
        self.env["CODEX_THREAD_ID"] = "019a0000-0000-7000-8000-00000000000a"
        self.claim()
        self.assertEqual(self.verification()["method"], "agent_session_env")
        self.assertEqual(self.events()[0]["from_agent"], "lead")
        self.assertEqual({c["session"] for c in self.calls()}, {"dag-lab"})

    def test_a_different_session_variable_is_an_impersonation_and_nothing_is_sent(self):
        self.env["CODEX_THREAD_ID"] = "019a0000-0000-7000-8000-0000000000ff"
        with self.assertRaisesRegex(ValueError, "is not the session of lead"):
            self.claim()
        self.assertEqual(self.events(), [])
        self.assertNotIn(["agent", "prompt"], self.herdr_calls())

    def test_process_tree_under_the_sender_pane_verifies_the_claim(self):
        self.claim(lineage=lambda: [900, 700, 510, 300])
        self.assertEqual(self.verification()["method"], "process_ancestry")

    def test_unverifiable_claims_are_sent_as_declared_with_the_reason(self):
        self.claim(lineage=lambda: [900, 800])  # e.g. a managed daemon reparented to launchd
        self.assertEqual(self.verification()["method"], "declared")
        self.assertIn("하위가 아니다", self.verification()["detail"])
        self.temp.cleanup()
        self.setUp()
        self.claim(lineage=lambda: None)
        self.assertEqual(self.verification(), {"method": "declared", "detail": "세션 ID 변수가 없고 프로세스 계보를 읽지 못했다"})

    def test_missing_claim_or_session_stops_before_any_herdr_call(self):
        with self.assertRaisesRegex(ValueError, "--from"):
            self.send()
        with self.assertRaisesRegex(ValueError, "--herdr-session is required"):
            self.send(declared="lead")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.events(), [])

    def test_command_line_claim_without_pane_variables(self):
        body = self.base / "body.md"
        body.write_text("Review the diff.\n", encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", ""), "CODEX_THREAD_ID": "019a0000-0000-7000-8000-00000000000a",
               "HERDR_TRACE_HERDR_BIN": str(self.fake), "PYTHONPATH": str(ROOT / "src"),
               **{k: os.environ[k] for k in os.environ if k.startswith("FAKE_HERDR_")}}
        command = [*CLI, "--to", "reviewer", "--kind", "assignment", "--from", "lead", "--herdr-session", "dag-lab",
                   "--message-file", str(body), "--journal", str(self.journal / "events.jsonl")]
        sent = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertEqual(self.verification()["method"], "agent_session_env")
        refused = subprocess.run([c for c in command if c not in ("--from", "lead")], env=env, capture_output=True, text=True)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("--from", refused.stderr)

    def test_ancestors_follow_parents_until_launchd(self):
        parents = {40: 30, 30: 20, 20: 1}
        self.assertEqual(herdr_bridge.ancestors(40, parent=parents.get), [30, 20])
        self.assertIsNone(herdr_bridge.ancestors(40, parent={40: 30}.get))


class DeliveryLedgerChecks(unittest.TestCase):
    def test_journal_accepts_messages_and_outcomes_and_requires_their_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            message = {"type": "agent.message", "message_id": "0123456789ab", "from_agent": "lead", "to_agent": "reviewer",
                       "message": "x"}
            delivery = {"type": "agent.delivery", "message_id": "0123456789ab", "outcome": "submitted"}
            journal.append(path, message)
            journal.append(path, delivery)
            self.assertEqual([(e["seq"], e["type"]) for e in journal.load(path)], [(1, "agent.message"), (2, "agent.delivery")])
            for refused, error in (({**delivery, "outcome": ""}, "message_id, outcome"), ({"type": "workflow.created"}, "Unknown")):
                with self.subTest(refused=refused):
                    with self.assertRaisesRegex(ValueError, error):
                        journal.append(path, refused)
            self.assertEqual(len(journal.load(path)), 2)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
