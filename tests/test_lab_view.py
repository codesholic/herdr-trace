import copy
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from herdr_trace import herdr_source
from herdr_trace import demo_data as lab
from herdr_trace import journal
from herdr_trace.lab_view import LabView, herdr_web_link
from support import Server


class FakeHerdrSocket:
    """Line-delimited JSON socket answering ping and agent.list from a mutable agent list."""

    def __init__(self, directory, agents):
        self.path = Path(directory) / "h.sock"
        self.agents, self.requests = agents, []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.server.listen()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            with connection, connection.makefile("rwb") as stream:
                request = json.loads(stream.readline())
                self.requests.append(request["method"])
                result = ({"type": "pong", "protocol": 22, "version": "0.9.3"} if request["method"] == "ping"
                          else {"type": "agent_list", "agents": self.agents})
                stream.write(b'{"event":"unrelated"}\n')
                stream.write((json.dumps({"id": request["id"], "result": result}) + "\n").encode())
                stream.flush()

    def close(self):
        self.server.close()


class LabViewCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")  # AF_UNIX paths must stay short on macOS.
        self.addCleanup(self.temp.cleanup)
        self.lab = lab.build(self.temp.name)
        self.log = self.lab["journal"] / "events.jsonl"

    def events(self):
        with self.log.open(encoding="utf-8") as handle:
            return journal.read_events(handle)

    def view(self, socket_path=None, web_url=None):
        view = LabView(self.events, self.lab["claude_dir"], self.lab["codex_dir"], socket_path, web_url)
        view.herdr.interval = 0
        self.addCleanup(view.herdr.close)
        return view


class BundleChecks(LabViewCase):
    def test_bundle_places_both_tools_and_every_line_with_its_basis(self):
        bundle = self.view().build()
        self.assertEqual([(a["key"], a["name"], a["provider"]) for a in bundle["agents"]],
                         [(lab.LEAD_KEY, "lead", "codex"), (lab.REVIEWER_KEY, "reviewer", "claude")])
        self.assertEqual(bundle["agents"][0]["transcript"], str(lab.lead_path(self.temp.name)))
        self.assertEqual(bundle["agents"][1]["transcript"], str(lab.reviewer_path(self.temp.name)))
        self.assertEqual(bundle["agents"][1]["title"], "샌드박스 리뷰")
        lines = [(e["basis"], e["kind"], e["from_name"], e["to_name"], (e.get("delivery") or {}).get("status"))
                 for e in bundle["edges"]]
        self.assertEqual(lines, [
            ("journal", "assignment", "lead", "reviewer", "confirmed"),
            ("derived", "result", "reviewer", "lead", None),
            ("journal", "result", "reviewer", "lead", "confirmed"),
            ("journal", "assignment", "lead", "reviewer", "undelivered"),
            ("journal", "message", "lead", "reviewer", "unconfirmed"),
            ("journal", "message", "lead", "reviewer", "queued"),
            ("journal", "assignment", "lead", "reviewer", "confirmed_modified"),
            ("journal", "message", "lead", "reviewer", "confirmed"),
            ("derived", "result", "reviewer", "lead", None)])
        derived, explicit = bundle["edges"][1], bundle["edges"][2]
        self.assertEqual((derived["reply_to"], explicit["reply_to"]), (lab.IDS["A"], lab.IDS["A"]))
        self.assertEqual(derived["explicit_result"], lab.IDS["B"])
        late = bundle["edges"][-1]
        self.assertEqual((late["basis"], late["reply_to"], late["explicit_result"], late["other_inputs_in_turn"]),
                         ("derived", lab.IDS["F"], None, ["q1"]))
        self.assertEqual(derived["text"], lab.REVIEW_REPLY)
        self.assertEqual(len(bundle["unattributed"]), 3)
        self.assertEqual([(e.get("sender_verification") or {}).get("method") for e in bundle["edges"]][:5],
                         ["agent_session_env", None, None, None, "declared"])
        self.assertEqual(bundle["herdr"]["status"], "not_configured")
        self.assertEqual([a["status_label"] for a in bundle["agents"]], ["상태 미확인", "상태 미확인"])

    def test_cards_summarise_their_latest_turn(self):
        bundle = self.view().build()
        lead, reviewer = bundle["agents"]
        self.assertEqual((lead["turn"]["status"], lead["turn"]["title"], lead["turn"]["count"]),
                         ("completed", "reviewer · 결과 · 리뷰 결과: 문제 1건.", 2))
        # The reviewer's last turn opens with a typed fake header: it must not read as sent by lead.
        self.assertEqual((reviewer["turn"]["status"], reviewer["turn"]["title"], reviewer["turn"]["inputs"]),
                         ("open", "확인 안 된 머리말 · 흉내 낸 머리말", 1))
        self.assertEqual(reviewer["turn"]["started_at"], lab.at(110))

    def test_cards_carry_their_work_periods_for_the_time_order_view(self):
        bundle = self.view().build()
        lead, reviewer = bundle["agents"]
        self.assertEqual([(t["start"], t["end"], t["state"]) for t in lead["turns"]],
                         [(lab.at(-120), lab.at(5), "done"), (lab.at(33), lab.at(50), "done")])
        # Turn 0 (a person's question at -60) is inside the window: ten minutes before the first message at +1.
        self.assertEqual([(t["start"], t["end"], t["state"]) for t in reviewer["turns"]],
                         [(lab.at(-60), lab.at(-49), "done"), (lab.at(2), lab.at(31), "done"),
                          (lab.at(91), lab.at(100), "done"), (lab.at(110), None, "interrupted")])
        self.assertEqual(reviewer["turns"][1]["title"], "lead · 작업 요청 · git diff를 리뷰하고 문제만 보고해줘.")

    def test_revision_is_stable_until_evidence_changes(self):
        view = self.view()
        first, second = view.build(), view.build()
        self.assertEqual(first["revision"], second["revision"])
        journal.append(self.lab["journal_file"], {"type": "agent.delivery", "message_id": lab.IDS["D"], "outcome": "timeout",
                              "at": lab.at(200, False)})
        third = view.build()
        self.assertNotEqual(third["revision"], first["revision"])
        unconfirmed = next(e for e in third["edges"] if e.get("message_id") == lab.IDS["D"])
        self.assertEqual((unconfirmed["delivery"]["status"], unconfirmed["delivery"]["outcome"]["outcome"]),
                         ("unconfirmed", "timeout"))

    def test_claude_session_reported_by_id_is_found_under_projects(self):
        # herdr 0.9.3 keeps only the session ID for Claude (kind "id"), not the transcript path.
        reported = copy.deepcopy(list(self.lab["agents"].values()))
        reported[1]["agent_session"] = {"source": "herdr:claude", "agent": "claude", "kind": "id",
                                        "value": lab.REVIEWER_ID}
        fake = FakeHerdrSocket(self.temp.name, reported)
        self.addCleanup(fake.close)
        view = LabView(lambda: [], self.lab["claude_dir"], self.lab["codex_dir"], fake.path)
        view.herdr.interval = 0
        self.addCleanup(view.herdr.close)
        reviewer = next(a for a in view.build()["agents"] if a["name"] == "reviewer")
        self.assertEqual((reviewer["key"], reviewer["transcript"]),
                         (lab.REVIEWER_KEY, str(lab.reviewer_path(self.temp.name))))

    def test_codex_fork_joins_one_card_and_lines_keep_their_session_keys(self):
        old = "019a0000-0000-7000-8000-0000000000a0"
        old_path = lab.lead_path(self.temp.name).with_name(f"rollout-2026-10-08T04-50-00-{old}.jsonl")
        lab.write_jsonl(old_path, [
            lab.codex("session_meta", -300, {"id": old, "timestamp": lab.at(-300), "cwd": "/sandbox", "originator": "codex-tui"}),
            lab.codex("event_msg", -300, {"type": "task_started", "turn_id": "old-1"}),
            lab.codex_user("x0", -299, ["처음 요청"], "old-1"),
            lab.codex("event_msg", -290, {"type": "task_complete", "turn_id": "old-1", "last_agent_message": "끝"})])
        records = lab.lead_records()
        records[0]["payload"]["forked_from_id"] = old
        lab.write_jsonl(lab.lead_path(self.temp.name), records)
        journal.append(self.lab["journal_file"], {"type": "agent.message", "transport": "herdr", "message_id": "a0a0a0a0a0a0",
                              "at": lab.at(-295, False), "from_agent": "lead", "to_agent": "reviewer",
                              "message_kind": "message", "message": lab.compose("a0a0a0a0a0a0", "lead", "message", "fork 전"),
                              "sender_agent_key": f"codex:{old}", "recipient_agent_key": lab.REVIEWER_KEY})
        bundle = self.view().build()
        self.assertEqual([a["key"] for a in bundle["agents"]], [lab.LEAD_KEY, lab.REVIEWER_KEY])
        lead = bundle["agents"][0]
        self.assertEqual([(m["session_id"], m["current"], m["forked_from"]) for m in lead["sessions"]],
                         [(old, False, None), (lab.LEAD_ID, True, f"codex:{old}")])
        self.assertEqual(lead["name"], "lead")
        early = next(e for e in bundle["edges"] if e.get("message_id") == "a0a0a0a0a0a0")
        self.assertEqual((early["from_key"], early["from_card"], early["to_card"]), (f"codex:{old}", lab.LEAD_KEY, lab.REVIEWER_KEY))
        self.assertIn("x0", [u["record_id"] for u in bundle["unattributed"]])
        self.assertTrue(all(e["from_card"] in (lab.LEAD_KEY, lab.REVIEWER_KEY) for e in bundle["edges"]))

    def test_message_journaled_before_the_recipient_reported_its_session_is_found_in_its_name_and_pane(self):
        # A freshly started Codex reports no session until its first input, so the bridge journals no recipient key.
        lab.write_jsonl(lab.lead_path(self.temp.name), lab.lead_records() + [
            lab.codex("event_msg", 200, {"type": "task_started", "turn_id": "turn-3"}),
            lab.codex_user("x3", 200.6, [lab.compose("c0c0c0c0c0c0", "reviewer", "assignment", "새 작업")], "turn-3"),
            lab.codex("event_msg", 210, {"type": "task_complete", "turn_id": "turn-3", "last_agent_message": "새 작업을 끝냈다."}),
            lab.codex("event_msg", 220, {"type": "task_started", "turn_id": "turn-4"}),
            lab.codex_user("x4", 220.6, [lab.compose("d0d0d0d0d0d0", "reviewer", "assignment", "다른 pane에 보낸 작업")], "turn-4"),
            lab.codex("event_msg", 230, {"type": "task_complete", "turn_id": "turn-4", "last_agent_message": "끝."})])

        def send(message_id, seconds, body, pane):
            before = {**self.lab["agents"]["lead"], "agent_session": None, "pane_id": pane}
            journal.append(self.lab["journal_file"], {"type": "agent.message", "transport": "herdr", "message_id": message_id,
                                  "at": lab.at(seconds, False), "from_agent": "reviewer", "to_agent": "lead",
                                  "message_kind": "assignment", "message": lab.compose(message_id, "reviewer", "assignment", body),
                                  "sender_agent_key": lab.REVIEWER_KEY, "recipient_agent_key": None,
                                  "herdr": {"session": "dag-lab", "sender": self.lab["agents"]["reviewer"], "recipient_before": before}})

        send("c0c0c0c0c0c0", 200, "새 작업", "w1:p1")
        send("d0d0d0d0d0d0", 220, "다른 pane에 보낸 작업", "w9:p9")  # lead's record carries this ID, but lead was never there.
        bundle = self.view().build()
        found = next(e for e in bundle["edges"] if e.get("message_id") == "c0c0c0c0c0c0")
        self.assertEqual((found["delivery"]["status"], found["to_key"], found["to_card"]), ("confirmed", lab.LEAD_KEY, lab.LEAD_KEY))
        self.assertEqual(found["delivery"]["recipient_resolved"],
                         {"name": "lead", "pane_id": "w1:p1", "agent": "codex", "candidates": [lab.LEAD_KEY]})
        self.assertEqual(found["delivery"]["receipts"][0]["record_id"], "x3")
        derived = next(e for e in bundle["edges"] if e["basis"] == "derived" and e["reply_to"] == "c0c0c0c0c0c0")
        self.assertEqual((derived["from_key"], derived["to_key"], derived["text"]), (lab.LEAD_KEY, lab.REVIEWER_KEY, "새 작업을 끝냈다."))
        elsewhere = next(e for e in bundle["edges"] if e.get("message_id") == "d0d0d0d0d0d0")
        self.assertEqual((elsewhere["delivery"]["status"], elsewhere["to_key"]), ("unconfirmed", None))
        self.assertEqual(elsewhere["delivery"]["recipient_resolved"]["candidates"], [])
        self.assertIn("같은 이름·도구·pane", elsewhere["delivery"]["reason"])
        self.assertIn(("x4", "다른 수신자에게 보낸 메시지 ID"), [(u["record_id"], u["reason"]) for u in bundle["unattributed"]])

    def test_reply_ending_a_turn_while_its_background_subagent_still_works_is_an_interim_reply(self):
        records = lab.reviewer_records()
        after = next(i for i, r in enumerate(records) if r.get("uuid") == "r1") + 1  # Inside turn 1 (A's assignment).
        tool = {"type": "tool_use", "id": "toolu_bg", "name": "Agent", "input": {
            "description": "경계 사례", "prompt": "경계 사례를 점검해줘.", "subagent_type": "general-purpose", "run_in_background": True}}
        records[after:after] = [
            lab.claude("assistant", 8.5, uuid="ab", message={"id": "msg_bg", "role": "assistant", "stop_reason": "tool_use", "content": [tool]}),
            lab.claude("user", 8.6, uuid="rb", toolUseResult={"agentId": "bg1", "status": "async_launched", "isAsync": True},
                       message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_bg", "content": "launched"}]})]
        path = lab.reviewer_path(self.temp.name)
        lab.write_jsonl(path, records)
        folder = path.with_suffix("") / "subagents"
        folder.mkdir(parents=True)
        (folder / "agent-bg1.meta.json").write_text(json.dumps({"agentType": "general-purpose", "description": "경계 사례",
                                                               "toolUseId": "toolu_bg", "requestShape": "background"}), encoding="utf-8")

        def subagent_finishing_at(seconds):
            lab.write_jsonl(folder / "agent-bg1.jsonl", [
                lab.claude("user", 8.7, uuid="su", isSidechain=True, agentId="bg1",
                           message={"role": "user", "content": "경계 사례를 점검해줘."}),
                lab.claude("assistant", seconds, uuid="sa", isSidechain=True, agentId="bg1", message={
                    "id": "sm", "role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": "문제 없음"}]})])
            return self.view().build()

        late = subagent_finishing_at(40)  # The turn ended at 31, before the subagent did.
        derived = next(e for e in late["edges"] if e["id"] == "derived:" + lab.IDS["A"])
        self.assertTrue(derived["interim"])
        self.assertEqual(derived["waiting_on"], [{"kind": "subagent", "agent_id": "bg1", "description": "경계 사례",
                                                  "status": "completed", "finished_at": lab.at(40)}])
        self.assertFalse(next(e for e in late["edges"] if e["id"] == "derived:" + lab.IDS["F"])["interim"])
        early = subagent_finishing_at(20)  # Finished inside the turn: the last reply could report it.
        self.assertFalse(next(e for e in early["edges"] if e["id"] == "derived:" + lab.IDS["A"])["interim"])

    def test_reply_ending_a_turn_before_a_peer_answered_its_assignment_is_an_interim_reply(self):
        # Inside the turn A started (2 to 31), the reviewer hands work on to lead through the bridge.
        def record(message_id, seconds, sender, kind, **extra):
            keys = (lab.REVIEWER_KEY, lab.LEAD_KEY) if sender == "reviewer" else (lab.LEAD_KEY, lab.REVIEWER_KEY)
            journal.append(self.lab["journal_file"], {"type": "agent.message", "transport": "herdr", "message_id": message_id,
                                  "at": lab.at(seconds, False), "from_agent": sender,
                                  "to_agent": "lead" if sender == "reviewer" else "reviewer", "message_kind": kind,
                                  "message": lab.compose(message_id, sender, kind, "넘긴 일"), "sender_agent_key": keys[0],
                                  "recipient_agent_key": keys[1], **extra})

        record("e0e0e0e0e0e0", 20, "reviewer", "assignment")
        waiting = self.view().build()
        derived = next(e for e in waiting["edges"] if e["id"] == "derived:" + lab.IDS["A"])
        self.assertTrue(derived["interim"])
        self.assertEqual(derived["waiting_on"], [{"kind": "agent", "message_id": "e0e0e0e0e0e0", "to_agent": "lead", "answered_at": None}])
        record("e1e1e1e1e1e1", 40, "lead", "result", reply_to="e0e0e0e0e0e0")  # Answered after the turn ended at 31.
        late = next(e for e in self.view().build()["edges"] if e["id"] == "derived:" + lab.IDS["A"])
        self.assertEqual((late["interim"], late["waiting_on"][0]["answered_at"]), (True, lab.at(40, False)))
        self.assertFalse(next(e for e in self.view().build()["edges"] if e["id"] == "derived:" + lab.IDS["F"])["interim"])

    def test_peer_answer_inside_the_turn_leaves_the_reply_a_result(self):
        for message_id, seconds, sender, kind, extra in (("e2e2e2e2e2e2", 10, "reviewer", "assignment", {}),
                                                         ("e3e3e3e3e3e3", 25, "lead", "result", {"reply_to": "e2e2e2e2e2e2"})):
            keys = (lab.REVIEWER_KEY, lab.LEAD_KEY) if sender == "reviewer" else (lab.LEAD_KEY, lab.REVIEWER_KEY)
            journal.append(self.lab["journal_file"], {"type": "agent.message", "transport": "herdr", "message_id": message_id,
                                  "at": lab.at(seconds, False), "from_agent": sender, "message_kind": kind,
                                  "to_agent": "lead" if sender == "reviewer" else "reviewer",
                                  "message": lab.compose(message_id, sender, kind, "넘긴 일"), "sender_agent_key": keys[0],
                                  "recipient_agent_key": keys[1], **extra})
        derived = next(e for e in self.view().build()["edges"] if e["id"] == "derived:" + lab.IDS["A"])
        self.assertEqual((derived["interim"], derived["waiting_on"]), (False, []))

    def test_missing_transcripts_are_reported_not_invented(self):
        lab.reviewer_path(self.temp.name).unlink()
        bundle = self.view().build()
        reviewer = bundle["agents"][1]
        self.assertIsNone(reviewer["transcript"])
        first = bundle["edges"][0]
        self.assertEqual((first["delivery"]["status"], first["delivery"]["reason"]),
                         ("unconfirmed", "수신 세션 기록을 찾지 못했다"))
        self.assertFalse([e for e in bundle["edges"] if e["basis"] == "derived"])


class HerdrStatusChecks(LabViewCase):
    def test_live_states_come_from_the_socket_and_changes_move_the_revision(self):
        agents = copy.deepcopy(list(self.lab["agents"].values()))
        agents.append({"name": "spare", "agent": "codex", "agent_status": "idle", "pane_id": "w1:p3",
                       "workspace_id": "w1", "tab_id": "w1:t1", "agent_session": None})
        fake = FakeHerdrSocket(self.temp.name, agents)
        self.addCleanup(fake.close)
        view = self.view(fake.path)
        working = view.build()
        self.assertEqual(working["herdr"]["status"], "ok")
        self.assertEqual([(a["name"], a["live"], a["status_label"]) for a in working["agents"]],
                         [("lead", True, "대기"), ("reviewer", True, "작업 중")])
        self.assertEqual([a["name"] for a in working["unreported_agents"]], ["spare"])
        agents[1]["agent_status"] = "idle"
        idle = view.build()
        self.assertEqual(idle["agents"][1]["status_label"], "대기")
        self.assertNotEqual(idle["revision"], working["revision"])
        self.assertTrue(set(fake.requests) <= {"ping", "agent.list", "events.subscribe"})

    def test_live_panes_link_to_herdr_web_ui_only_once_an_address_is_set(self):
        agents = copy.deepcopy(list(self.lab["agents"].values()))
        agents.append({"name": "spare", "agent": "codex", "agent_status": "blocked", "pane_id": "w1:p3",
                       "workspace_id": "w1", "tab_id": "w1:t1", "agent_session": None})
        fake = FakeHerdrSocket(self.temp.name, agents)
        self.addCleanup(fake.close)
        silent = self.view(fake.path).build()
        self.assertEqual([a["web_link"] for a in silent["agents"]], [None, None])
        self.assertIsNone(silent["unreported_agents"][0]["web_link"])
        linked = self.view(fake.path, "http://127.0.0.1:7317").build()
        self.assertEqual([a["web_link"] for a in linked["agents"]],
                         ["http://127.0.0.1:7317/?pane=w1:p1", "http://127.0.0.1:7317/?pane=w1:p2"])
        self.assertEqual(linked["unreported_agents"][0]["web_link"], "http://127.0.0.1:7317/?pane=w1:p3")
        self.assertNotEqual(linked["revision"], silent["revision"])
        # Without herdr there is no pane to open, whatever the address.
        offline = self.view(None, "http://127.0.0.1:7317").build()
        self.assertFalse([a for a in offline["agents"] if a.get("web_link")])
        self.assertEqual(herdr_web_link("https://box.tail.ts.net/herdr", "w1:p 2&x=1"),
                         "https://box.tail.ts.net/herdr/?pane=w1:p%202%26x%3D1")
        self.assertIsNone(herdr_web_link(None, "w1:p1"))

    def test_absent_socket_is_unavailable_and_control_methods_are_refused(self):
        bundle = self.view(Path(self.temp.name) / "missing.sock").build()
        self.assertEqual(bundle["herdr"]["status"], "unavailable")
        self.assertIn("소켓에 연결할 수 없다", bundle["herdr"]["detail"])
        for method in ("agent.prompt", "agent.send_keys", "server.stop", "pane.send_text"):
            with self.assertRaisesRegex(ValueError, "not read-only"):
                herdr_source.request(Path(self.temp.name) / "missing.sock", method)


class LabRouteChecks(LabViewCase):
    def test_page_bundle_and_stream_are_served(self):
        server = Server(self, self.lab)
        status, _, page = server.request("/")
        self.assertEqual(status, 200)
        self.assertIn("<title>herdr-trace</title>", page.decode("utf-8"))
        status, _, raw = server.request("/api/bundle")
        bundle = json.loads(raw)
        self.assertEqual(len(bundle["edges"]), 9)
        self.assertEqual(bundle["herdr"]["status"], "unavailable")  # The test socket does not exist.
        frame = server.stream_frame()  # The missing socket moves the event feed to "reconnecting", so compare content.
        self.assertEqual((len(frame["edges"]), frame["herdr"]["status"]), (9, "unavailable"))


if __name__ == "__main__":
    unittest.main()
