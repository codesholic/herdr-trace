"""Read the public conversation skeleton of one Codex rollout for delivery matching.

Collects user messages with their turn, turn lifecycle with the final agent
message, and tool outputs (kept for receipt checks only). Encrypted content,
reasoning and unknown record types are skipped. Nothing is written.
"""
import json
import re
from pathlib import Path

# Whole blocks Codex injects itself; a message made only of these is not anyone's input.
INJECTED = re.compile(r"\s*(?:<(?:environment_context|in-app-browser-context|external_codex_apps_open_page)\b"
                      r"|# AGENTS\.md instructions for )")


class CodexRollout:
    """Incremental reader: each update() parses only the complete lines appended since the last one."""

    def __init__(self, path):
        self.path = Path(path)
        self.reset()

    def reset(self):
        self.offset, self.line_number, self.inode, self.turns, self.current, self.created_at = 0, 0, None, {}, None, None
        self.session = {"provider": "codex", "session_id": None, "path": str(self.path), "cwd": None, "originator": None,
                        "forked_from": None, "created_at": None,
                        "prompts": [], "turns": [], "tool_outputs": [], "skipped_lines": 0}

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
                    row = json.loads(raw)
                except ValueError:
                    row = None
                if not isinstance(row, dict):
                    self.session["skipped_lines"] += 1
                    continue
                self.ingest(row, self.line_number)
        return self.session

    def ingest(self, row, line_number):
        session, turns = self.session, self.turns
        at, kind, payload = row.get("timestamp"), row.get("type"), row.get("payload") or {}
        if kind == "session_meta":
            if session["session_id"] is None:
                self.created_at = payload.get("timestamp") or at
                session.update(session_id=payload.get("id"), cwd=payload.get("cwd"),
                               originator=payload.get("originator"), forked_from=payload.get("forked_from_id"),
                               created_at=self.created_at)
            return
        if self.created_at and at and at < self.created_at:
            return  # Forks replay earlier parent history.
        if kind == "event_msg":
            event = payload.get("type")
            turn_id = payload.get("turn_id") or self.current
            if event == "task_started" and turn_id:
                self.current = turn_id
                turns[turn_id] = {"turn_id": turn_id, "started_at": at, "finished_at": None, "status": "open",
                                  "final_text": None, "start_line": line_number}
                session["turns"].append(turns[turn_id])
            elif event in {"task_complete", "turn_aborted", "task_failed"} and turn_id in turns:
                turns[turn_id].update(status={"task_complete": "completed"}.get(event, "failed"), finished_at=at,
                                      final_text=payload.get("last_agent_message"), end_line=line_number)
            return
        if kind != "response_item":
            return
        item = payload.get("type")
        if item == "message" and payload.get("role") == "user":
            blocks = [b.get("text", "") for b in payload.get("content") or []
                      if isinstance(b, dict) and b.get("type") == "input_text"]
            meta = payload.get("internal_chat_message_metadata_passthrough") or {}
            session["prompts"].append({"form": "user", "record_id": payload.get("id"), "line": line_number,
                "at": at, "blocks": blocks, "text": "\n".join(blocks),
                "turn_id": meta.get("turn_id") or self.current,
                "injected": bool(blocks) and all(INJECTED.match(b) for b in blocks)})
        elif item in {"function_call_output", "custom_tool_call_output"}:
            output = payload.get("output")
            if isinstance(output, dict):
                output = output.get("content") or output.get("output")
            if isinstance(output, list):
                output = "\n".join(b.get("text", "") for b in output if isinstance(b, dict))
            if isinstance(output, str):
                session["tool_outputs"].append({"record_id": payload.get("call_id"), "line": line_number,
                                                "at": at, "text": output})


def read_rollout(path):
    return CodexRollout(path).update()
