import json
import os
import tempfile
import unittest
from pathlib import Path

from herdr_trace import claude_source
from herdr_trace import codex_source
from herdr_trace import demo_data as lab
from herdr_trace import journal
from herdr_trace.lab_view import LabView


def append(path, records, partial=None):
    with path.open("a", encoding="utf-8") as handle:
        handle.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
        if partial:
            handle.write(partial)


class AdapterCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)


class IncrementalReadingChecks(AdapterCase):
    def test_claude_reader_parses_only_appended_lines_and_restarts_on_replacement(self):
        path = self.base / "s.jsonl"
        append(path, [lab.human("u", 0, "질문", "p"), lab.reply("a", 1, "앞부분", "msg")], partial='{"type": "assi')
        reader = claude_source.ClaudeTranscript(path)
        first = reader.update()
        self.assertEqual((len(first["prompts"]), first["turns"][0]["final_text"], first["turns"][0]["status"]),
                         (1, "앞부분", "open"))
        offset = reader.offset
        with path.open("a", encoding="utf-8") as handle:  # Finish the partial line, then add the turn end.
            handle.write('stant", "uuid": "b", "timestamp": "' + lab.at(2) + '", "sessionId": "' + lab.REVIEWER_ID
                         + '", "message": {"id": "msg", "role": "assistant", "content": [{"type": "text", "text": "뒷부분"}]}}\n')
        append(path, [lab.claude("system", 3, uuid="s", subtype="turn_duration", durationMs=3)])
        second = reader.update()
        self.assertIs(second, first)
        self.assertGreater(reader.offset, offset)
        self.assertEqual((second["turns"][0]["final_text"], second["turns"][0]["status"]), ("앞부분\n뒷부분", "completed"))
        self.assertEqual(len(second["prompts"]), 1)
        path.write_text(json.dumps(lab.human("v", 5, "새 파일", "q"), ensure_ascii=False) + "\n", encoding="utf-8")
        third = reader.update()
        self.assertEqual([p["text"] for p in third["prompts"]], ["새 파일"])

    def test_codex_reader_parses_only_appended_lines(self):
        path = self.base / "rollout.jsonl"
        records = lab.lead_records()
        append(path, records[:3])
        reader = codex_source.CodexRollout(path)
        self.assertEqual([p["record_id"] for p in reader.update()["prompts"]], ["x1"])
        append(path, records[3:])
        session = reader.update()
        self.assertEqual([p["record_id"] for p in session["prompts"]], ["x1", "x2"])
        self.assertEqual([t["status"] for t in session["turns"]], ["completed", "completed"])


class SubagentChecks(AdapterCase):
    def write_parent(self):
        path = self.base / "projects/-sandbox" / f"{lab.REVIEWER_ID}.jsonl"
        tool = {"type": "tool_use", "id": "toolu_agent", "name": "Agent",
                "input": {"description": "테스트 확인", "prompt": "test_calc.py를 읽고 빠진 경우를 찾아줘.", "subagent_type": "Explore"}}
        lab.write_jsonl(path, [
            lab.human("u", 0, "리뷰해줘", "p"),
            lab.claude("assistant", 1, uuid="a1", message={"id": "m1", "role": "assistant", "stop_reason": "tool_use", "content": [tool]}),
            lab.claude("user", 9, uuid="r1", toolUseResult={"agentId": "abc123", "status": "completed", "isAsync": False},
                       message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_agent",
                                                              "content": [{"type": "text", "text": "빈 목록 테스트가 없다."}]}]}),
            lab.reply("a2", 10, "리뷰 끝", "m2"),
            lab.claude("system", 11, uuid="s", subtype="turn_duration", durationMs=11000)])
        folder = path.with_suffix("") / "subagents"
        folder.mkdir(parents=True)
        (folder / "agent-abc123.meta.json").write_text(json.dumps({"agentType": "Explore", "description": "테스트 확인",
            "toolUseId": "toolu_agent", "spawnDepth": 1, "requestShape": "foreground"}), encoding="utf-8")
        sub = folder / "agent-abc123.jsonl"
        lab.write_jsonl(sub, [lab.claude("user", 2, uuid="su", isSidechain=True, agentId="abc123",
                                         message={"role": "user", "content": "test_calc.py를 읽고 빠진 경우를 찾아줘."})])
        return path, sub

    def test_agent_calls_link_the_public_prompt_to_their_result(self):
        path, _ = self.write_parent()
        session = claude_source.read_transcript(path)
        call, = session["agent_calls"]
        self.assertEqual((call["tool_use_id"], call["subagent_type"], call["prompt"], call["turn_index"]),
                         ("toolu_agent", "Explore", "test_calc.py를 읽고 빠진 경우를 찾아줘.", 0))
        self.assertEqual((call["status"], call["agent_id"], call["is_async"], call["result_text"]),
                         ("completed", "abc123", False, "빈 목록 테스트가 없다."))
        self.assertEqual(session["turns"][0]["final_text"], "리뷰 끝")

    def test_subagent_transcripts_are_read_incrementally_with_their_meta(self):
        path, sub = self.write_parent()
        readers = {}
        running, = claude_source.read_subagents(path, readers)
        self.assertEqual((running["agent_id"], running["agent_type"], running["tool_use_id"], running["background"]),
                         ("abc123", "Explore", "toolu_agent", False))
        self.assertEqual((running["prompt"], running["status"], running["final_text"]),
                         ("test_calc.py를 읽고 빠진 경우를 찾아줘.", "running", None))
        self.assertEqual((running["started_at"], running["last_at"], running["finished_at"]), (lab.at(2), lab.at(2), None))
        append(sub, [lab.claude("assistant", 5, uuid="st", isSidechain=True, agentId="abc123", message={
            "id": "sw", "role": "assistant", "stop_reason": "tool_use", "content": [{"type": "text", "text": "읽는 중"}]})])
        working, = claude_source.read_subagents(path, readers)
        self.assertEqual((working["status"], working["last_at"], working["finished_at"]), ("running", lab.at(5), None))
        append(sub, [lab.claude("assistant", 8, uuid="sa", isSidechain=True, agentId="abc123", message={
            "id": "sm", "role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": "빈 목록 테스트가 없다."}]})])
        done, = claude_source.read_subagents(path, readers)
        self.assertEqual((done["status"], done["final_text"]), ("completed", "빈 목록 테스트가 없다."))
        self.assertEqual((done["last_at"], done["finished_at"]), (lab.at(8), lab.at(8)))
        self.assertEqual(len(readers), 1)
        self.assertEqual(claude_source.read_subagents(self.base / "none.jsonl"), [])


class RunningSessionChecks(AdapterCase):
    def test_running_sessions_report_status_and_whether_the_process_exists(self):
        folder = self.base / "sessions"
        folder.mkdir()
        (folder / f"{os.getpid()}.json").write_text(json.dumps({"sessionId": "live", "status": "busy", "name": "n"}), encoding="utf-8")
        (folder / "999999.json").write_text(json.dumps({"sessionId": "gone", "status": "idle"}), encoding="utf-8")
        (folder / "123.json").write_text("{broken", encoding="utf-8")
        found = claude_source.running_sessions(self.base)
        self.assertEqual((found["live"]["status"], found["live"]["alive"], found["live"]["pid"]), ("busy", True, os.getpid()))
        self.assertFalse(found["gone"]["alive"])
        self.assertEqual(set(found), {"live", "gone"})


class BundleAdapterChecks(AdapterCase):
    def test_claude_card_carries_runtime_and_subagents(self):
        built = lab.build(self.base / "lab")
        parent = lab.reviewer_path(built["base"])
        folder = parent.with_suffix("") / "subagents"
        folder.mkdir(parents=True)
        (folder / "agent-abc123.meta.json").write_text(json.dumps({"agentType": "Explore", "toolUseId": "toolu_x",
                                                                    "requestShape": "background"}), encoding="utf-8")
        sessions = built["claude_dir"] / "sessions"
        sessions.mkdir()
        (sessions / f"{os.getpid()}.json").write_text(json.dumps({"sessionId": lab.REVIEWER_ID, "status": "idle"}), encoding="utf-8")
        log = built["journal"] / "events.jsonl"

        def events():
            with log.open(encoding="utf-8") as handle:
                return journal.read_events(handle)

        bundle = LabView(events, built["claude_dir"], built["codex_dir"]).build()
        reviewer = next(a for a in bundle["agents"] if a["name"] == "reviewer")
        self.assertEqual((reviewer["runtime"]["status"], reviewer["runtime"]["alive"]), ("idle", True))
        self.assertEqual([(s["agent_id"], s["background"], s["status"], s["call"]) for s in reviewer["subagents"]],
                         [("abc123", True, "unknown", None)])
        lead = next(a for a in bundle["agents"] if a["name"] == "lead")
        self.assertNotIn("runtime", lead)


if __name__ == "__main__":
    unittest.main()
