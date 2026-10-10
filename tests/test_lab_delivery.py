import copy
import tempfile
import unittest
from pathlib import Path

from herdr_trace import claude_source
from herdr_trace import codex_source
from herdr_trace import delivery
from herdr_trace import demo_data as lab
from herdr_trace import journal


class LabCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lab = lab.build(self.temp.name)
        self.reviewer = claude_source.read_transcript(lab.reviewer_path(self.temp.name))
        self.lead = codex_source.read_rollout(lab.lead_path(self.temp.name))
        self.sessions = {lab.REVIEWER_KEY: self.reviewer, lab.LEAD_KEY: self.lead}
        self.events = self.journal()

    def journal(self):
        path = self.lab["journal"] / "events.jsonl"
        with path.open(encoding="utf-8") as handle:
            return journal.read_events(handle)


class ClaudeTranscriptChecks(LabCase):
    def test_reads_prompts_in_both_arrival_shapes_and_skips_private_or_meta_records(self):
        forms = [(p["form"], p["record_id"]) for p in self.reviewer["prompts"]]
        self.assertEqual(forms, [("user", "u0"), ("user", "u1"), ("user", "u2"), ("queued_command", "q1"),
                                 ("user", "u3")])
        self.assertEqual(self.reviewer["session_id"], lab.REVIEWER_ID)
        self.assertEqual(self.reviewer["title"], "샌드박스 리뷰")
        self.assertEqual(self.reviewer["record_types"]["atis-latch"], 1)
        absorbed = self.reviewer["prompts"][3]
        self.assertEqual((absorbed["delivery_id"], absorbed["turn_index"]), ("d1", 2))
        self.assertEqual([q["operation"] for q in self.reviewer["queue"]], ["enqueue", "enqueue", "remove"])

    def test_paste_envelope_is_read_as_its_inner_text_and_kept_as_evidence(self):
        prompt = self.reviewer["prompts"][1]
        self.assertEqual(prompt["text"], lab.text("A", "lead", "assignment", "git diff를 리뷰하고 문제만 보고해줘."))
        self.assertEqual((prompt["envelope"]["kind"], prompt["envelope"]["id"]), ("pasted_content", "e026"))
        self.assertIsNone(self.reviewer["prompts"][0]["envelope"])
        body = "[codex-dag msg=0123456789ab from=lead kind=message]\nhi"
        for text in ("note\n" + lab.pasted(body), lab.pasted(body).replace('</pasted_content id="e026">', '</pasted_content id="x">'),
                     lab.pasted(body) + lab.pasted(body, "f001")):
            with self.subTest(text=text):
                self.assertEqual(claude_source.unwrap(text), (text, None))

    def test_turn_boundaries_and_final_replies(self):
        turns = self.reviewer["turns"]
        self.assertEqual([t["status"] for t in turns], ["completed", "completed", "completed", "open"])
        self.assertEqual(turns[1]["final_text"], lab.REVIEW_REPLY)
        self.assertEqual(turns[1]["final_record_ids"], ["a1"])
        self.assertEqual(turns[1]["end_record_id"], "s1")
        self.assertEqual(turns[2]["absorbed_record_ids"], ["q1"])
        self.assertEqual(turns[2]["final_text"], lab.SECOND_REPLY)
        self.assertIsNone(turns[3]["final_text"])
        self.assertEqual([t["interrupted"] for t in turns], [False, False, False, True])
        self.assertEqual(self.reviewer["interruptions"], [{"line": 22, "at": lab.at(112), "record_id": "i1", "turn_index": 3}])

    def test_one_reply_split_over_records_is_joined_and_partial_tail_is_left_for_later(self):
        path = Path(self.temp.name) / "split.jsonl"
        lab.write_jsonl(path, [lab.human("u", 0, "질문", "p"), lab.reply("a", 1, "앞부분", "msg"),
                               lab.reply("b", 2, "뒷부분", "msg"),
                               lab.claude("system", 3, uuid="s", subtype="turn_duration", durationMs=3)])
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"type": "user", "message": {"content": "쓰는 중')
        session = claude_source.read_transcript(path)
        self.assertEqual(session["turns"][0]["final_text"], "앞부분\n뒷부분")
        self.assertEqual(len(session["prompts"]), 1)
        self.assertEqual(session["skipped_lines"], 0)


class CodexRolloutChecks(LabCase):
    def test_reads_user_blocks_turns_and_tool_outputs(self):
        self.assertEqual(self.lead["session_id"], lab.LEAD_ID)
        self.assertEqual([(p["record_id"], p["turn_id"]) for p in self.lead["prompts"]], [("x1", "turn-1"), ("x2", "turn-2")])
        self.assertEqual(len(self.lead["prompts"][0]["blocks"]), 2)
        self.assertEqual([(t["turn_id"], t["status"], t["final_text"]) for t in self.lead["turns"]],
                         [("turn-1", "completed", "리뷰를 맡겼다."), ("turn-2", "completed", "리뷰를 반영했다.")])
        self.assertEqual(self.lead["tool_outputs"][0]["record_id"], "call_read")


class DeliveryMatchChecks(LabCase):
    def match(self, events=None, sessions=None):
        return delivery.match(self.events if events is None else events, self.sessions if sessions is None else sessions)

    def test_each_journaled_message_gets_one_status_with_evidence(self):
        deliveries, unattributed = self.match()
        status = {d["message_id"]: d["status"] for d in deliveries}
        self.assertEqual(status, {lab.IDS["A"]: "confirmed", lab.IDS["B"]: "confirmed", lab.IDS["C"]: "undelivered",
                                  lab.IDS["D"]: "unconfirmed", lab.IDS["E"]: "queued",
                                  lab.IDS["F"]: "confirmed_modified", lab.IDS["G"]: "confirmed"})
        first = deliveries[0]
        envelope = {"kind": "pasted_content", "id": "e026", "prefix": "\n\n", "suffix": "\n",
                    "recorded_length": len(lab.pasted(lab.text("A", "lead", "assignment", "git diff를 리뷰하고 문제만 보고해줘.")))}
        self.assertEqual(first["receipts"], [{"agent_key": lab.REVIEWER_KEY, "path": str(lab.reviewer_path(self.temp.name)),
                                              "record_id": "u1", "line": 5, "at": lab.at(2), "form": "user", "turn": 1,
                                              "envelope": envelope}])
        self.assertIsNone(first["variation"])
        self.assertEqual(first["journal_seq"], 1)
        self.assertRegex(first["journal_event_id"], r"^[0-9a-f]{16}$")
        self.assertEqual(first["outcome"]["outcome"], "submitted")
        self.assertEqual(deliveries[1]["receipts"][0]["turn"], "turn-2")
        self.assertEqual(deliveries[6]["receipts"][0]["form"], "queued_command")
        self.assertEqual((deliveries[6]["receipts"][0]["at"], deliveries[6]["receipts"][0]["entered_at"]), (lab.at(95.5), lab.at(97)))
        self.assertEqual(deliveries[6]["latency_seconds"], 2.0)  # Journal +95 to entering the turn at +97, not the queue time.
        self.assertEqual(deliveries[5]["variation"]["equal_after"], "trailing_spaces")
        self.assertNotIn("label", deliveries[2])  # Statuses are codes; the page words them.

    def test_busy_recipient_without_a_record_is_pending_and_confirmed_deliveries_carry_latency(self):
        events = copy.deepcopy(self.events)
        for event in events:
            if event.get("type") == "agent.delivery" and event["message_id"] == lab.IDS["D"]:
                event["herdr"] = {"recipient_after": {"agent_status": "working"}}
        deliveries, _ = self.match(events=events)
        pending = next(d for d in deliveries if d["message_id"] == lab.IDS["D"])
        self.assertEqual((pending["status"], pending["recipient_status_at_send"]), ("pending_busy", "working"))
        first = deliveries[0]
        self.assertEqual((first["status"], first["latency_seconds"]), ("confirmed", 1.0))
        self.assertNotIn("latency_seconds", pending)
        blocked = next(d for d in deliveries if d["message_id"] == lab.IDS["C"])
        self.assertEqual(blocked["status"], "undelivered")

    def test_inputs_without_a_journaled_header_are_never_attributed_to_an_agent(self):
        deliveries, unattributed = self.match()
        self.assertEqual({(u["agent_key"], u["record_id"], u["reason"]) for u in unattributed}, {
            (lab.REVIEWER_KEY, "u0", "no_header"),
            (lab.REVIEWER_KEY, "u3", "unknown_message"),
            (lab.LEAD_KEY, "x1", "no_header")})
        receipt_ids = {r["record_id"] for d in deliveries for r in d["receipts"]}
        self.assertFalse(receipt_ids & {"u0", "u3", "x1"})

    def test_header_in_the_wrong_transcript_or_before_the_journal_does_not_confirm(self):
        moved = copy.deepcopy(self.sessions)
        prompt = moved[lab.REVIEWER_KEY]["prompts"].pop(1)
        moved[lab.LEAD_KEY]["prompts"].append({**prompt, "blocks": [prompt["text"]]})
        deliveries, unattributed = self.match(sessions=moved)
        self.assertEqual(deliveries[0]["status"], "unconfirmed")
        self.assertIn((lab.LEAD_KEY, "u1", "other_recipient"),
                      {(u["agent_key"], u["record_id"], u["reason"]) for u in unattributed})
        early = copy.deepcopy(self.sessions)
        early[lab.REVIEWER_KEY]["prompts"][1]["at"] = lab.at(0)
        deliveries, unattributed = self.match(sessions=early)
        self.assertEqual(deliveries[0]["status"], "unconfirmed")
        self.assertIn("before_journal", {u["reason"] for u in unattributed})

    def test_missing_recipient_transcript_is_reported_not_guessed(self):
        deliveries, _ = self.match(sessions={lab.LEAD_KEY: self.lead})
        first = deliveries[0]
        self.assertEqual((first["status"], first["reason"]), ("unconfirmed", "no_recipient_record"))

    def test_claude_system_inputs_start_turns_but_are_not_counted_as_a_persons_input(self):
        path = Path(self.temp.name) / "system.jsonl"
        lab.write_jsonl(path, [
            lab.human("h", 0, "사람의 요청", "p"),
            lab.claude("user", 5, uuid="t", promptId="q", origin={"kind": "task-notification", "producer": "session-task"},
                       promptSource="system", turnOrigin="task_notification",
                       message={"role": "user", "content": "<task-notification>\n<task-id>b1</task-id>\n</task-notification>"})])
        session = claude_source.read_transcript(path)
        self.assertEqual([(p["record_id"], p["system"]) for p in session["prompts"]], [("h", False), ("t", True)])
        self.assertEqual(len(session["turns"]), 2)
        _, unattributed = delivery.match([], {lab.REVIEWER_KEY: session})
        self.assertEqual([u["record_id"] for u in unattributed], ["h"])

    def test_body_variation_summary(self):
        self.assertIsNone(delivery.variation("a\nb", "a\nb"))
        self.assertEqual(delivery.variation("a\nb", "a\r\nb")["equal_after"], "line_endings")
        self.assertEqual(delivery.variation("a\nb", "a\nb\n")["equal_after"], "outer_whitespace")
        changed = delivery.variation("abc", "abd")
        self.assertEqual((changed["equal_after"], changed["first_difference"]), (None, 2))


class DerivedResultChecks(LabCase):
    def test_assignment_turn_reply_becomes_a_derived_result_with_its_evidence(self):
        deliveries, _ = delivery.match(self.events, self.sessions)
        messages = [e for e in self.events if e["type"] == "agent.message"]
        results = delivery.derived_results(messages, deliveries, self.sessions)
        self.assertEqual([(r["reply_to"], r["from_agent"], r["to_agent"], r["message"]) for r in results],
                         [(lab.IDS["A"], "reviewer", "lead", lab.REVIEW_REPLY),
                          (lab.IDS["F"], "reviewer", "lead", lab.SECOND_REPLY)])
        first, second = results
        self.assertEqual(first["basis"], "derived")
        self.assertEqual(first["evidence"]["final_record_ids"], ["a1"])
        self.assertEqual(first["evidence"]["end_record_id"], "s1")
        self.assertEqual(first["sender_receipt"], [{"record_id": "call_read", "line": 8, "at": lab.at(40)}])
        self.assertEqual(second["sender_receipt"], [])
        self.assertFalse(second["absorbed_mid_turn"])
        self.assertEqual(first["other_inputs_in_turn"], [])
        self.assertEqual(second["other_inputs_in_turn"], ["q1"])  # G was absorbed into F's turn

    def test_undelivered_or_unfinished_assignments_have_no_derived_result(self):
        deliveries, _ = delivery.match(self.events, self.sessions)
        messages = [e for e in self.events if e["type"] == "agent.message"]
        unfinished = copy.deepcopy(self.sessions)
        unfinished[lab.REVIEWER_KEY]["turns"][1]["status"] = "open"
        results = delivery.derived_results(messages, deliveries, unfinished)
        self.assertEqual([r["reply_to"] for r in results], [lab.IDS["F"]])


if __name__ == "__main__":
    unittest.main()
