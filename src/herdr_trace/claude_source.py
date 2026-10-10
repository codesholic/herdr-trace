"""Read the public conversation skeleton of one Claude Code transcript.

Reads prompts that entered the conversation, queue records, turn boundaries and
each turn's final reply. Thinking, tool calls and results, meta records, side
chains and unknown record types are skipped: the format is unofficial and
changes often. Nothing is written.

Input arrives in one of two shapes. An idle session records a `user` record
that starts a turn. A working session queues the input (`queue-operation`
enqueue) and later absorbs it into the running turn as an `attachment` of type
`queued_command`; no new turn starts.

Pasted input is recorded inside an envelope, observed with Claude Code 2.1.292:
`\n\n<pasted_content id="e026">\n{text}\n</pasted_content id="e026">\n`. A prompt that
is exactly one envelope is read as its inner text, and the envelope is kept as evidence.

Claude Code also writes `[Request interrupted by user]` markers (for example after a
rejected tool use) as user records without an origin. They are not input; they mark the
running turn as interrupted.
"""
import json
import os
import re
from collections import Counter
from pathlib import Path

SKIPPED_USER_FLAGS = ("isMeta", "isCompactSummary", "isVisibleInTranscriptOnly", "isSidechain")
INTERRUPTED = re.compile(r"\[Request interrupted by user[^\]\n]*\]")
PASTE = re.compile(r'(?P<prefix>\s*)<pasted_content id="(?P<id>[^"]+)">\n(?P<body>.*)\n</pasted_content id="(?P=id)">(?P<suffix>\s*)',
                   re.S)


def unwrap(text):
    """Inner text and envelope evidence when text is exactly one paste envelope, else (text, None)."""
    match = PASTE.fullmatch(text) if isinstance(text, str) else None
    if not match:
        return text, None
    return match["body"], {"kind": "pasted_content", "id": match["id"], "prefix": match["prefix"],
                           "suffix": match["suffix"], "recorded_length": len(text)}


def text_of(content):
    """Plain text of a string or a list of content blocks; None if it holds tool results."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return None
    parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(parts) if parts else None


AGENT_TOOLS = {"Agent", "Task"}


class ClaudeTranscript:
    """Incremental reader: each update() parses only the complete lines appended since the last one."""

    def __init__(self, path, sidechain=False):
        self.path, self.sidechain = Path(path), sidechain
        self.reset()

    def reset(self):
        self.offset, self.line_number, self.inode, self.turn, self.final_ids, self.calls = 0, 0, None, None, {}, {}
        self.absorbed = {}  # queued text or delivery ID -> time the running turn took it
        self.types = Counter()
        self.session = {"provider": "claude", "session_id": None, "path": str(self.path), "title": None,
                        "prompts": [], "queue": [], "turns": [], "interruptions": [], "agent_calls": [],
                        "last_at": None, "skipped_lines": 0, "record_types": {}}

    def update(self):
        stat = self.path.stat()
        if stat.st_size < self.offset or (self.inode is not None and stat.st_ino != self.inode):
            self.reset()  # Replaced or truncated: read again from the start.
        self.inode = stat.st_ino
        with self.path.open("rb") as handle:
            handle.seek(self.offset)
            for raw in handle:
                if not raw.endswith(b"\n"):
                    break  # A partial tail is retried by the next update.
                self.offset += len(raw)
                self.line_number += 1
                try:
                    record = json.loads(raw)
                except ValueError:
                    record = None
                if not isinstance(record, dict):
                    self.session["skipped_lines"] += 1
                    continue
                self.ingest(record, self.line_number)
        self.session["record_types"] = dict(self.types)
        return self.session

    def ingest(self, record, line_number):
        session, turn = self.session, self.turn
        kind = record.get("type")
        self.types[kind] += 1
        session["session_id"] = session["session_id"] or record.get("sessionId")
        if record.get("isSidechain") and not self.sidechain:
            return
        at = record.get("timestamp")
        session["last_at"] = at or session["last_at"]
        if kind == "ai-title" and record.get("aiTitle"):
            session["title"] = record["aiTitle"]
        elif kind == "queue-operation":
            content, envelope = unwrap(record.get("content"))
            session["queue"].append({"line": line_number, "at": at, "operation": record.get("operation"),
                "content": content, "envelope": envelope, "delivery_id": record.get("deliveryId"),
                "reason": record.get("reason")})
            if record.get("operation") == "remove" and record.get("reason") == "absorbed_mid_turn":
                # The queued_command attachment that follows keeps the enqueue time; this is when it entered the turn.
                for key in (record.get("deliveryId"), content):
                    if key:
                        self.absorbed[key] = at
        elif kind == "user":
            if "toolUseResult" in record:
                self.agent_result(record, at)
                return
            if any(record.get(flag) for flag in SKIPPED_USER_FLAGS if flag != "isSidechain" or not self.sidechain):
                return
            text = text_of((record.get("message") or {}).get("content"))
            if text is None:
                return
            if INTERRUPTED.fullmatch(text.strip()) and not record.get("origin"):
                session["interruptions"].append({"line": line_number, "at": at, "record_id": record.get("uuid"),
                                                 "turn_index": turn["index"] if turn else None})
                if turn:
                    turn["interrupted"] = True
                return
            text, envelope = unwrap(text)
            prompt = {"form": "user", "record_id": record.get("uuid"), "line": line_number, "at": at,
                      "text": text, "envelope": envelope, "prompt_id": record.get("promptId"),
                      "origin_kind": (record.get("origin") or {}).get("kind"),
                      "prompt_source": record.get("promptSource")}
            # Claude Code's own inputs (for example a finished background task) start turns but nobody typed them.
            prompt["system"] = bool(prompt["origin_kind"]) and prompt["origin_kind"] != "human"
            session["prompts"].append(prompt)
            if turn and turn["status"] == "open":
                turn["status"] = "superseded"  # A new prompt began before this turn's end record.
            self.turn = turn = {"index": len(session["turns"]), "prompt_record_id": prompt["record_id"],
                                "prompt_id": prompt["prompt_id"], "started_at": at, "finished_at": None,
                                "duration_ms": None, "status": "open", "interrupted": False, "absorbed_record_ids": [],
                                "final_text": None, "final_record_ids": []}
            session["turns"].append(turn)
            prompt["turn_index"] = turn["index"]
        elif kind == "attachment":
            attachment = record.get("attachment") or {}
            if attachment.get("type") != "queued_command" or attachment.get("commandMode") != "prompt":
                return
            text = text_of(attachment.get("prompt"))
            if text is None:
                return
            text, envelope = unwrap(text)
            prompt = {"form": "queued_command", "record_id": record.get("uuid"), "line": line_number,
                      "at": attachment.get("timestamp") or at, "recorded_at": at, "text": text, "envelope": envelope,
                      "delivery_id": attachment.get("delivery_id"),
                      "origin_kind": (attachment.get("origin") or {}).get("kind"),
                      "entered_at": self.absorbed.get(attachment.get("delivery_id")) or self.absorbed.get(text),
                      "turn_index": turn["index"] if turn else None}
            session["prompts"].append(prompt)
            if turn:
                turn["absorbed_record_ids"].append(prompt["record_id"])
        elif kind == "assistant":
            message = record.get("message") or {}
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in AGENT_TOOLS:
                    self.agent_call(block, record, at, line_number)
            if not turn or turn["status"] != "open":
                return
            blocks = [b.get("text", "") for b in message.get("content") or [] if isinstance(b, dict) and b.get("type") == "text"]
            if not blocks:
                return
            if message.get("id") and message.get("id") == self.final_ids.get(turn["index"]):
                turn["final_text"] += "\n" + "\n".join(blocks)
            else:
                turn["final_text"] = "\n".join(blocks)
                turn["final_record_ids"] = []
            self.final_ids[turn["index"]] = message.get("id")
            turn["final_record_ids"].append(record.get("uuid"))
            turn["final_at"] = at
            turn["final_stop_reason"] = message.get("stop_reason")
        elif kind == "system" and record.get("subtype") == "turn_duration" and turn and turn["status"] == "open":
            turn.update(status="completed", finished_at=at, duration_ms=record.get("durationMs"),
                        end_record_id=record.get("uuid"), end_line=line_number)

    def agent_call(self, block, record, at, line_number):
        """A delegation to a subagent: the exact prompt is public in the Agent tool input."""
        tool_input = block.get("input") or {}
        call = {"tool_use_id": block.get("id"), "record_id": record.get("uuid"), "line": line_number, "at": at,
                "tool": block.get("name"), "description": tool_input.get("description"), "prompt": tool_input.get("prompt"),
                "subagent_type": tool_input.get("subagent_type"), "turn_index": self.turn["index"] if self.turn else None,
                "status": "called", "agent_id": None, "is_async": None, "result_text": None, "result_at": None}
        self.calls[call["tool_use_id"]] = call
        self.session["agent_calls"].append(call)

    def agent_result(self, record, at):
        for block in (record.get("message") or {}).get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            call = self.calls.get(block.get("tool_use_id"))
            if not call:
                continue
            result = record.get("toolUseResult") if isinstance(record.get("toolUseResult"), dict) else {}
            content = block.get("content")
            call.update(status=result.get("status") or ("error" if block.get("is_error") else "returned"),
                        agent_id=result.get("agentId"), is_async=result.get("isAsync"), result_at=at,
                        result_text=content if isinstance(content, str) else text_of(content))


def read_transcript(path, sidechain=False):
    return ClaudeTranscript(path, sidechain).update()


def read_subagents(transcript_path, readers=None):
    """Subagents of one session from <session>/subagents/agent-*.jsonl and their meta files.

    readers: optional dict reused across calls so each subagent transcript is read incrementally.
    """
    folder = Path(transcript_path).with_suffix("") / "subagents"
    readers = {} if readers is None else readers
    found = []
    for meta_path in sorted(folder.glob("agent-*.meta.json")) if folder.is_dir() else []:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        agent_id = meta_path.name[len("agent-"):-len(".meta.json")]
        path = folder / f"agent-{agent_id}.jsonl"
        entry = {"agent_id": agent_id, "agent_type": meta.get("agentType"), "description": meta.get("description"),
                 "tool_use_id": meta.get("toolUseId"), "spawn_depth": meta.get("spawnDepth"),
                 "background": meta.get("requestShape") == "background", "path": str(path),
                 "prompt": None, "final_text": None, "status": "unknown", "started_at": None, "last_at": None,
                 "finished_at": None}
        if path.is_file():
            reader = readers.setdefault(str(path), ClaudeTranscript(path, sidechain=True))
            sub = reader.update()
            first = sub["prompts"][0] if sub["prompts"] else None
            last = sub["turns"][-1] if sub["turns"] else None
            done = bool(last) and last.get("final_stop_reason") == "end_turn"
            entry.update(prompt=first and first["text"], started_at=first and first["at"],
                         final_text=last and last["final_text"], last_at=sub["last_at"],
                         finished_at=last["final_at"] if done else None, status="completed" if done else "running")
        found.append(entry)
    return found


def running_sessions(claude_dir):
    """Claude Code's own record of running sessions: ~/.claude/sessions/<pid>.json, keyed by session ID.

    alive is checked with signal 0, which only asks whether the PID exists.
    """
    found = {}
    for path in sorted(Path(claude_dir).glob("sessions/*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = int(path.stem)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not data.get("sessionId"):
            continue
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
        found[data["sessionId"]] = {"pid": pid, "status": data.get("status"), "name": data.get("name"),
                                    "cwd": data.get("cwd"), "updated_at": data.get("updatedAt"), "alive": alive}
    return found
