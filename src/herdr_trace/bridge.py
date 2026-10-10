"""Send a public message to another herdr agent, journaling the exact text first.

herdr records nothing about the sender of `agent prompt`, and the recipient's
transcript shows the text as if a person typed it. The bridge therefore writes
the exact submitted text (with a short header naming the message) to the
journal before it calls herdr, and appends the transport outcome afterwards.
Nothing already journaled is edited or removed.
"""
import json
import os
import re
import secrets
import subprocess
from pathlib import Path

from . import journal

KINDS = ("assignment", "message", "result")
NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
# New messages carry the herdr-trace header; the codex-dag header of the experiment's journals is still read.
HEADER = re.compile(r"\[(?:herdr-trace|codex-dag) msg=(?P<message_id>[0-9a-f]{12}) "
                    r"from=(?P<from_agent>[a-z][a-z0-9_-]{0,31}) kind=(?P<kind>[a-z]+)\]")
# herdr error codes that have a documented meaning; any other code is kept verbatim.
OUTCOMES = {"agent_blocked", "agent_prompt_stalled", "timeout"}
# Session ID variables each tool may set for the commands it runs (seen in the 2026-10 binaries).
SESSION_ENV = {"codex": ("CODEX_THREAD_ID", "CODEX_SESSION_ID"), "claude": ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")}


class HerdrError(Exception):
    def __init__(self, code, message, returncode=None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.returncode = code, message, returncode


def parse_header(text):
    """Header fields when the text starts with a bridge header, else None."""
    match = HEADER.match(text)
    return match.groupdict() if match else None


def compose(message_id, sender, kind, body):
    return f"[herdr-trace msg={message_id} from={sender} kind={kind}]\n{body.rstrip(chr(10))}"


def agent_key(info):
    """Durable `provider:session_id`; herdr names and pane IDs are display-only."""
    session = (info or {}).get("agent_session") or {}
    provider = session.get("agent") or (info or {}).get("agent")
    value = session.get("value")
    if not provider or not value:
        return None
    if session.get("kind") == "path":
        value = Path(value).stem  # Claude transcripts are named <session_id>.jsonl.
    return f"{provider}:{value}"


def call(arguments, herdr_bin="herdr", session=None, timeout=None):
    """Run one herdr CLI request and return its result object or raise HerdrError."""
    argv = [herdr_bin, *(["--session", session] if session else []), *arguments]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise HerdrError("bridge_timeout", f"herdr did not answer within {timeout}s") from exc
    except OSError as exc:
        raise HerdrError("herdr_unavailable", str(exc)) from exc
    payload = None
    for stream in (completed.stdout, completed.stderr):
        try:
            payload = json.loads(stream)
            break
        except ValueError:
            continue
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        error = payload["error"]
        raise HerdrError(str(error.get("code") or "error"), str(error.get("message") or ""), completed.returncode)
    if completed.returncode != 0 or not isinstance(payload, dict):
        detail = (completed.stderr or completed.stdout).strip()[:500]
        raise HerdrError("cli_failed", detail or f"exit {completed.returncode}", completed.returncode)
    return payload.get("result", payload)


def agent_info(result):
    return result.get("agent", result) if isinstance(result, dict) else {}


def parent_pid(pid):
    """Parent PID through ps, or None when it cannot be read (for example inside a sandbox)."""
    try:
        completed = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    return int(value) if completed.returncode == 0 and value.isdigit() else None


def ancestors(pid=None, parent=parent_pid, limit=64):
    """PIDs above pid up to launchd/init, or None when a step cannot be read."""
    chain, current = [], os.getpid() if pid is None else pid
    for _ in range(limit):
        above = parent(current)
        if above is None:
            return None
        if above <= 1:
            break
        chain.append(above)
        current = above
    return chain


def identify_sender(environ, declared, herdr_bin, session, lineage):
    """Return (sender AgentInfo, verification) or raise ValueError before anything is journaled.

    Pane variables identify the sender. Without them the caller names itself and the
    claim is checked against the tool's session ID variable, then the process tree.
    Only a contradiction refuses; a claim nothing can check is sent as unverified.
    """
    if environ.get("HERDR_ENV") == "1" and environ.get("HERDR_PANE_ID"):
        sender = agent_info(call(["agent", "get", environ["HERDR_PANE_ID"]], herdr_bin, session, 30))
        if declared and sender.get("name") != declared:
            raise ValueError(f"--from {declared} does not match the agent in this pane ({sender.get('name')})")
        return sender, {"method": "pane_env", "detail": f"HERDR_PANE_ID={environ['HERDR_PANE_ID']}"}
    if not declared:
        raise ValueError("No herdr pane variables here; name the sender with --from and the session with --herdr-session")
    if not session:
        raise ValueError("Without herdr pane variables --herdr-session is required, so the default session is never used")
    sender = agent_info(call(["agent", "get", declared], herdr_bin, session, 30))
    claimed = (agent_key(sender) or ":").split(":", 1)[1]
    provider = (sender.get("agent_session") or {}).get("agent") or sender.get("agent")
    for name in SESSION_ENV.get(provider, ()):
        if environ.get(name):
            if environ[name] != claimed:
                raise ValueError(f"{name}={environ[name]} is not the session of {declared} ({claimed or 'unreported'})")
            return sender, {"method": "agent_session_env", "detail": f"{name} matches {provider}:{claimed}"}
    chain = lineage()
    if chain is None:
        return sender, {"method": "declared", "detail": "세션 ID 변수가 없고 프로세스 계보를 읽지 못했다"}
    info = call(["pane", "process-info", "--pane", sender["pane_id"]], herdr_bin, session, 30).get("process_info") or {}
    pane_pids = {info.get("shell_pid")} | {p.get("pid") for p in info.get("foreground_processes") or []}
    if pane_pids & set(chain):
        return sender, {"method": "process_ancestry", "detail": f"pane {sender['pane_id']} 프로세스의 하위에서 실행"}
    return sender, {"method": "declared", "detail": f"pane {sender['pane_id']} 프로세스의 하위가 아니다(관리형 데몬이면 그럴 수 있다)"}


def herdr_fields(info):
    keep = ("name", "agent", "agent_status", "agent_session", "pane_id", "tab_id", "workspace_id",
            "state_change_seq", "completion_seq", "cwd")
    return {key: info.get(key) for key in keep if key in info}


def send(to, kind, body, *, journal_path, environ=None, herdr_bin="herdr", session=None,
         wait=False, timeout_ms=None, reply_to=None, declared=None, lineage=ancestors):
    """Journal, then submit. Returns the appended outcome event."""
    environ = os.environ if environ is None else environ
    if kind not in KINDS:
        raise ValueError(f"Kind must be one of {', '.join(KINDS)}")
    if reply_to is not None and not re.fullmatch(r"[0-9a-f]{12}", reply_to):
        raise ValueError("Reply-to must be a message ID from a bridge header")
    if not NAME.fullmatch(to or ""):
        raise ValueError("Recipient must be a live herdr agent name")
    if not body.strip():
        raise ValueError("Message body is empty")
    if declared is not None and not NAME.fullmatch(declared):
        raise ValueError("--from must be a live herdr agent name")
    session = session or (None if environ.get("HERDR_PANE_ID") else environ.get("HERDR_SESSION"))
    limit = None if timeout_ms is None else timeout_ms / 1000 + 10
    sender, verification = identify_sender(environ, declared, herdr_bin, session, lineage)
    recipient = agent_info(call(["agent", "get", to], herdr_bin, session, 30))
    if not sender.get("name"):
        raise ValueError("The sending pane has no named herdr agent")
    if recipient.get("pane_id") == sender.get("pane_id"):
        raise ValueError("Sender and recipient are the same agent")
    message_id = secrets.token_hex(6)
    text = compose(message_id, sender["name"], kind, body)
    sender_key, recipient_key = agent_key(sender), agent_key(recipient)
    event = {"type": "agent.message", "transport": "herdr", "message_id": message_id,
             "from_agent": sender["name"], "to_agent": recipient.get("name") or to, "message": text,
             "message_kind": kind, "session_id": sender_key, "project_path": str(Path(sender.get("cwd") or os.getcwd()).resolve()),
             "sender_agent_key": sender_key, "recipient_agent_key": recipient_key,
             "sender_session_id": sender_key and sender_key.split(":", 1)[1],
             "recipient_session_id": recipient_key and recipient_key.split(":", 1)[1],
             "herdr": {"session": session, "socket": environ.get("HERDR_SOCKET_PATH"),
                       "sender": herdr_fields(sender), "recipient_before": herdr_fields(recipient),
                       "sender_verification": verification}}
    if reply_to:
        event["reply_to"] = reply_to
    journal.append(journal_path, event)
    arguments = ["agent", "prompt", to, text]
    if wait:
        arguments.append("--wait")
    if timeout_ms is not None:
        arguments += ["--timeout", str(timeout_ms)]
    outcome = {"type": "agent.delivery", "transport": "herdr", "message_id": message_id,
               "from_agent": event["from_agent"], "to_agent": event["to_agent"], "session_id": sender_key,
               "project_path": event["project_path"], "wait": wait}
    try:
        result = call(arguments, herdr_bin, session, limit)
        outcome.update(outcome="submitted", herdr={"recipient_after": herdr_fields(agent_info(result))})
    except HerdrError as exc:
        # A timeout or stall does not prove the text was not delivered; only the
        # recipient transcript can confirm or rule out arrival.
        outcome.update(outcome=exc.code if exc.code in OUTCOMES else "error", error_code=exc.code,
                       error_message=exc.message, returncode=exc.returncode)
    return journal.append(journal_path, outcome)
