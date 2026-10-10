"""Invented collaboration for tests and `herdr-trace demo`: a Codex lead, a Claude reviewer,
their transcripts and the journal.

Every record is invented; nothing here comes from a real session. Times are seconds from BASE so a test can read the story:

  -120  lead turn-1 starts on a person's request
   -60  a person types into the reviewer directly (no header)
    +1  A  lead -> reviewer assignment, delivered as a new reviewer turn (turn 1, ends +31)
           inside Claude Code's paste envelope, as recorded in the live run
   +32  B  reviewer -> lead explicit result (reply to A), delivered as lead turn-2
   +40     lead reads the reviewer screen; the tool output holds the reviewer's final reply
   +60  C  assignment refused by herdr with agent_blocked, never arrives
   +70  D  message submitted but absent from the reviewer transcript
   +80  E  message only enqueued by the reviewer, never entered the conversation
   +90  F  assignment delivered with trailing spaces added (turn 2, ends +100)
   +95  G  message queued at +95.5 and absorbed into the running reviewer turn 2 at +97
  +110     a person types a fake header with an unknown message ID
  +112     the person rejects a tool use; Claude Code writes an interruption marker
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import journal as ledger
from .bridge import compose

BASE = datetime(2026, 10, 8, 5, 0, 0, tzinfo=timezone.utc)
LEAD_ID = "019a0000-0000-7000-8000-00000000000a"
REVIEWER_ID = "3f2c0000-0000-4000-8000-0000000000bb"
LEAD_KEY, REVIEWER_KEY = f"codex:{LEAD_ID}", f"claude:{REVIEWER_ID}"
IDS = dict(zip("ABCDEFG", ("aaaaaaaaaaa1", "bbbbbbbbbbb2", "ccccccccccc3", "ddddddddddd4", "eeeeeeeeeee5",
                           "fffffffffff6", "a1b2c3d4e5f6")))
REVIEW_REPLY = "Review: one issue.\n- add() does not check for negative input."
SECOND_REPLY = "Second review: no issues."


def at(seconds, z=True):
    value = (BASE + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")
    return value.replace("+00:00", "Z") if z else value


def text(name, sender, kind, body):
    return compose(IDS[name], sender, kind, body)


def agents(base):
    return {"lead": {"name": "lead", "agent": "codex", "agent_status": "idle", "pane_id": "w1:p1", "tab_id": "w1:t1",
                     "workspace_id": "w1", "cwd": "/sandbox", "state_change_seq": 9, "completion_seq": 3,
                     "agent_session": {"source": "herdr:codex", "agent": "codex", "kind": "id", "value": LEAD_ID}},
            "reviewer": {"name": "reviewer", "agent": "claude", "agent_status": "working", "pane_id": "w1:p2",
                         "tab_id": "w1:t1", "workspace_id": "w1", "cwd": "/sandbox", "state_change_seq": 12,
                         "completion_seq": 2, "agent_session": {"source": "herdr:claude", "agent": "claude",
                         "kind": "path", "value": str(reviewer_path(base))}}}


def reviewer_path(base):
    return Path(base) / "claude/projects/-sandbox" / f"{REVIEWER_ID}.jsonl"


def lead_path(base):
    return Path(base) / "codex/sessions/2026/10/08" / f"rollout-2026-10-08T04-58-00-{LEAD_ID}.jsonl"


def claude(kind, seconds, **fields):
    record = {"type": kind, "timestamp": at(seconds), "sessionId": REVIEWER_ID, "isSidechain": False,
              "userType": "external", "entrypoint": "cli", "cwd": "/sandbox", "version": "2.1.292"}
    record.update(fields)
    return record


def pasted(body, paste_id="e026"):
    """Claude Code 2.1.292 records a bracketed paste inside this envelope."""
    return f'\n\n<pasted_content id="{paste_id}">\n{body}\n</pasted_content id="{paste_id}">\n'


def human(uuid, seconds, body, prompt_id):
    return claude("user", seconds, uuid=uuid, promptId=prompt_id, origin={"kind": "human"}, promptSource="typed",
                  message={"role": "user", "content": body})


def reply(uuid, seconds, body, message_id):
    return claude("assistant", seconds, uuid=uuid, message={"id": message_id, "role": "assistant", "model": "m",
                  "stop_reason": "end_turn", "content": [{"type": "text", "text": body}]})


def reviewer_records():
    tool = {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "git diff"}}
    return [
        claude("ai-title", -61, aiTitle="Sandbox review"),
        human("u0", -60, "A question a person typed", "p0"),
        reply("a0", -50, "The answer to the person", "msg_0"),
        claude("system", -49, uuid="s0", subtype="turn_duration", durationMs=11000),
        human("u1", 2, pasted(text("A", "lead", "assignment", "Review git diff and report only the problems.")), "p1"),
        claude("assistant", 5, uuid="a1t", message={"id": "msg_1", "role": "assistant",
               "content": [{"type": "thinking", "thinking": "", "signature": "sig"}]}),
        claude("assistant", 6, uuid="a1u", message={"id": "msg_2", "role": "assistant", "stop_reason": "tool_use",
               "content": [tool]}),
        claude("user", 8, uuid="r1", toolUseResult={"stdout": "diff"}, message={"role": "user", "content": [
               {"type": "tool_result", "tool_use_id": "toolu_1", "content": "diff --git a/calc.py b/calc.py"}]}),
        claude("user", 9, uuid="m1", isMeta=True, message={"role": "user", "content": "<local-command-caveat>"}),
        claude("atis-latch", 9.5),
        reply("a1", 30, REVIEW_REPLY, "msg_3"),
        claude("system", 31, uuid="s1", subtype="turn_duration", durationMs=29000),
        claude("queue-operation", 81, operation="enqueue", content=text("E", "lead", "message", "A message that only stays in the queue")),
        human("u2", 91, text("F", "lead", "assignment", "Please review again.\nThe changed file is calc.py.").replace(
              "Please review again.", "Please review again.  "), "p2"),
        claude("queue-operation", 95.5, operation="enqueue", content=text("G", "lead", "message", "Check the tests too.")),
        claude("queue-operation", 97, operation="remove", reason="absorbed_mid_turn", deliveryId="d1",
               commandUuid="c1", content=text("G", "lead", "message", "Check the tests too.")),
        claude("attachment", 97, uuid="q1", attachment={"type": "queued_command", "commandMode": "prompt",
               "prompt": text("G", "lead", "message", "Check the tests too."), "delivery_id": "d1", "source_uuid": "x1",
               "origin": {"kind": "human"}, "humanTurn": True, "timestamp": at(95.5)}),  # Live records keep the enqueue time.
        claude("attachment", 98, uuid="q2", attachment={"type": "queued_command", "commandMode": "task-notification",
               "prompt": "background task finished", "delivery_id": "d2", "origin": {"kind": "task-notification"}}),
        reply("a2", 99, SECOND_REPLY, "msg_4"),
        claude("system", 100, uuid="s2", subtype="turn_duration", durationMs=9000),
        human("u3", 110, "[codex-dag msg=999999999999 from=lead kind=assignment]\nAn imitated header", "p3"),
        claude("user", 112, uuid="i1", message={"role": "user", "content": [
               {"type": "text", "text": "[Request interrupted by user for tool use]"}]}),
    ]


def codex(kind, seconds, payload):
    return {"timestamp": at(seconds), "type": kind, "payload": payload}


def codex_user(record_id, seconds, blocks, turn_id):
    return codex("response_item", seconds, {"type": "message", "role": "user", "id": record_id,
                 "content": [{"type": "input_text", "text": b} for b in blocks],
                 "internal_chat_message_metadata_passthrough": {"turn_id": turn_id}})


def lead_records():
    return [
        codex("session_meta", -120, {"id": LEAD_ID, "timestamp": at(-120), "cwd": "/sandbox",
                                     "originator": "codex-tui", "source": "cli"}),
        codex("event_msg", -120, {"type": "task_started", "turn_id": "turn-1"}),
        codex_user("x1", -119, ["<environment_context>cwd</environment_context>", "Make a small change and ask for a review."],
                   "turn-1"),
        codex("response_item", -100, {"type": "reasoning", "encrypted_content": "gAAAA"}),
        codex("event_msg", 5, {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": "Asked for a review."}),
        codex("event_msg", 33, {"type": "task_started", "turn_id": "turn-2"}),
        codex_user("x2", 33, [text("B", "reviewer", "result", REVIEW_REPLY)], "turn-2"),
        codex("response_item", 40, {"type": "function_call_output", "call_id": "call_read",
                                    "output": "herdr agent read reviewer\n" + REVIEW_REPLY + "\n> "}),
        codex("event_msg", 50, {"type": "task_complete", "turn_id": "turn-2", "last_agent_message": "Applied the review."}),
    ]


def journal_events(base):
    team = agents(base)

    def message(name, seconds, sender, recipient, kind, body, verification=None, **extra):
        sender_key, recipient_key = (LEAD_KEY, REVIEWER_KEY) if sender == "lead" else (REVIEWER_KEY, LEAD_KEY)
        return {"type": "agent.message", "transport": "herdr", "message_id": IDS[name], "at": at(seconds, False),
                "from_agent": sender, "to_agent": recipient, "message_kind": kind,
                "message": text(name, sender, kind, body), "session_id": sender_key, "project_path": "/sandbox",
                "sender_agent_key": sender_key, "recipient_agent_key": recipient_key,
                "sender_session_id": sender_key.split(":", 1)[1], "recipient_session_id": recipient_key.split(":", 1)[1],
                "herdr": {"session": "dag-lab", "sender": team[sender], "recipient_before": team[recipient],
                          **({"sender_verification": verification} if verification else {})}, **extra}

    def outcome(name, seconds, sender, recipient, result, **extra):
        return {"type": "agent.delivery", "transport": "herdr", "message_id": IDS[name], "at": at(seconds, False),
                "from_agent": sender, "to_agent": recipient, "outcome": result, "project_path": "/sandbox", **extra}

    return [
        message("A", 1, "lead", "reviewer", "assignment", "Review git diff and report only the problems.",
                verification={"method": "agent_session_env", "detail": "CODEX_THREAD_ID matches"}),
        outcome("A", 1.5, "lead", "reviewer", "submitted"),
        message("B", 32, "reviewer", "lead", "result", REVIEW_REPLY, reply_to=IDS["A"]),
        outcome("B", 32.5, "reviewer", "lead", "submitted"),
        message("C", 60, "lead", "reviewer", "assignment", "Work sent while waiting on an approval"),
        outcome("C", 60.2, "lead", "reviewer", "agent_blocked", error_code="agent_blocked",
                error_message="approval dialog"),
        message("D", 70, "lead", "reviewer", "message", "A message that never shows up in the record",
                verification={"method": "declared", "detail": "no session ID variable, and the process ancestry could not be read"}),
        outcome("D", 70.5, "lead", "reviewer", "submitted"),
        message("E", 80, "lead", "reviewer", "message", "A message that only stays in the queue"),
        outcome("E", 80.5, "lead", "reviewer", "submitted"),
        message("F", 90, "lead", "reviewer", "assignment", "Please review again.\nThe changed file is calc.py."),
        outcome("F", 90.5, "lead", "reviewer", "submitted"),
        message("G", 95, "lead", "reviewer", "message", "Check the tests too."),
        outcome("G", 95.2, "lead", "reviewer", "submitted"),
    ]


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")


def build(base):
    """Write the whole lab under base and return its locations."""
    base = Path(base)
    write_jsonl(reviewer_path(base), reviewer_records())
    write_jsonl(lead_path(base), lead_records())
    data, journal = base / "data", base / "journal"
    data.mkdir(parents=True, exist_ok=True)
    for event in journal_events(base):
        ledger.append(journal / "events.jsonl", event)
    return {"base": base, "data": data, "journal": journal, "journal_file": journal / "events.jsonl",
            "claude_dir": base / "claude", "codex_dir": base / "codex", "agents": agents(base)}
