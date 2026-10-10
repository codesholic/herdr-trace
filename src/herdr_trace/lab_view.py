"""Collaboration bundle: agents across tools in one herdr session and the lines between them.

Agents are keyed by `provider:session_id`. Lines come from the journal (what a
bridge sent, with its delivery status) and from recipient transcripts (results
derived from the turn an assignment started). Inputs that no journaled message
explains are listed apart and never drawn as agent-to-agent lines.
"""
import hashlib
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import claude_source, codex_source, delivery
from .bridge import agent_key, parse_header
from .herdr_source import HerdrStatus



def headline(text, trusted):
    """A turn's opening input as {"text"}, plus who sent it when a bridge header opens it.

    A bridge header names its sender only when the record is a confirmed delivery of a
    journaled message; anyone can type a header, so an unconfirmed one is marked unverified
    and its sender is left out. The page words the parts in the viewer's language.
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return None
    header = parse_header(lines[0])
    if not header:
        return {"text": lines[0][:140]}
    body = next(iter(lines[1:]), "")[:140]
    if trusted:
        return {"text": body, "from": header["from_agent"], "kind": header["kind"]}
    return {"text": body, "unverified": True}


def turn_inputs(session, turn):
    if session.get("provider") == "claude":
        return [p for p in session.get("prompts", []) if p.get("turn_index") == turn["index"]]
    return [p for p in session.get("prompts", []) if p.get("turn_id") == turn.get("turn_id") and not p.get("injected")]


def turn_title(first, confirmed):
    if first and first.get("blocks"):  # Codex keeps injected context blocks beside the typed one.
        typed = [b for b in first["blocks"] if not codex_source.INJECTED.match(b)]
        text = typed[0] if typed else first["text"]
    else:
        text = first["text"] if first else None
    return headline(text, bool(first) and first.get("record_id") in confirmed)


def turn_summary(session, confirmed=frozenset()):
    """The latest turn of a transcript: what opened it, when, and how many inputs entered it."""
    turns = (session or {}).get("turns") or []
    if not turns:
        return None
    turn = turns[-1]
    inputs = turn_inputs(session, turn)
    first = inputs[0] if inputs else None
    return {"status": turn["status"], "started_at": turn.get("started_at"), "finished_at": turn.get("finished_at"),
            "title": turn_title(first, confirmed), "system": bool(first and first.get("system")), "inputs": len(inputs),
            "count": len(turns)}


def turn_intervals(session, confirmed=frozenset(), since=None, limit=300):
    """Work periods of one transcript for the time-order view, newest `limit` turns from `since` on.

    end is the recorded turn end. A turn replaced by a new prompt ends where the next one starts;
    an unclosed turn ends at its last reply if there is one, else stays open (end None).
    """
    turns = (session or {}).get("turns") or []
    result = []
    for position, turn in enumerate(turns):
        start = turn.get("started_at")
        if not start or (since and start < since and not (turn.get("finished_at") and turn["finished_at"] >= since)):
            continue
        following = turns[position + 1].get("started_at") if position + 1 < len(turns) else None
        status = turn.get("status")
        if status == "completed":
            end, state = turn.get("finished_at"), "done"
        elif status == "failed":
            end, state = turn.get("finished_at"), "failed"
        elif status == "superseded":
            end, state = following, "done"
        else:
            end, state = (following or turn.get("final_at")), ("interrupted" if turn.get("interrupted") else "open")
        inputs = turn_inputs(session, turn)
        first = inputs[0] if inputs else None
        result.append({"start": start, "end": end, "state": state, "system": bool(first and first.get("system")),
                       "inputs": len(inputs), "title": turn_title(first, confirmed), "session_id": session.get("session_id")})
    return result[-limit:]

def later(value, ended):
    """Whether value is unknown or after ended; work that is not known to be done counts as still going."""
    moment, end = delivery.moment(value) if value else None, delivery.moment(ended) if ended else None
    return moment is None or (end is not None and moment > end)


def waiting_on(session, subagents, turn_index, entered, ended, key, messages):
    """Work a turn handed off that had not come back when the turn ended.

    Either a background subagent the turn launched, or an assignment the agent sent through the bridge
    during the turn whose explicit result arrived only later. The turn's last reply then only reports that
    work is under way; it is an interim reply, not the result.
    """
    by_call = {s.get("tool_use_id"): s for s in subagents or []}
    waiting = []
    for call in (session or {}).get("agent_calls", []):
        if call.get("turn_index") != turn_index or not call.get("is_async"):
            continue
        sub = by_call.get(call.get("tool_use_id")) or {}
        if later(sub.get("finished_at"), ended):
            waiting.append({"kind": "subagent", "agent_id": sub.get("agent_id") or call.get("agent_id"),
                            "description": call.get("description"), "status": sub.get("status", "unknown"),
                            "finished_at": sub.get("finished_at")})
    start, end = delivery.moment(entered), delivery.moment(ended)
    for message in messages:
        sent = delivery.moment(message.get("at"))
        if (message.get("sender_agent_key") != key or message.get("message_kind") != "assignment"
                or not (sent and start and end and start <= sent <= end)):
            continue
        answers = sorted((m.get("at") for m in messages if m.get("message_kind") == "result"
                          and m.get("reply_to") == message.get("message_id") and m.get("at")), key=delivery.moment)
        if later(answers[0] if answers else None, ended):
            waiting.append({"kind": "agent", "message_id": message.get("message_id"), "to_agent": message.get("to_agent"),
                            "answered_at": answers[0] if answers else None})
    return waiting


READERS = {"claude": claude_source.ClaudeTranscript, "codex": codex_source.CodexRollout}
SESSION_NAME = re.compile(r"[A-Za-z0-9._-]+")  # herdr's own rule for session names


def herdr_session_socket(name):
    """Socket of a named herdr session (protocol 22 layout: ~/.config/herdr/sessions/<name>/herdr.sock)."""
    if not SESSION_NAME.fullmatch(name or "") or name in {".", ".."}:
        raise ValueError("herdr session names use ASCII letters, numbers, '.', '_' and '-'")
    return Path.home() / ".config/herdr/sessions" / name / "herdr.sock"


def herdr_web_base(value):
    """Address of a herdr web ui, checked before the page turns it into links.

    Only http(s) without a query, fragment or credentials: the page appends `/?pane=<id>`.
    """
    parts = urlsplit((value or "").strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("herdr web ui address must be an http(s) URL with a host, e.g. http://127.0.0.1:7317")
    if parts.query or parts.fragment or "@" in parts.netloc:
        raise ValueError("herdr web ui address must not carry a query, fragment or credentials")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def herdr_web_link(base, pane_id):
    """herdr web ui opens `/?pane=<id>` on that pane (its notification deep link); None without both parts."""
    return f"{base}/?pane={quote(pane_id, safe=':')}" if base and pane_id else None


class LabSettings:
    """Sources of the collaboration view: launch options > saved settings > defaults."""

    def __init__(self, data_dir, claude_dir=None, herdr_socket=None):
        self.path = Path(data_dir) / "settings.json"
        self.fixed = {"claude_dir": claude_dir, "herdr_socket": herdr_socket}

    def saved(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict):
            raise ValueError("Lab settings must be an object")
        return value

    def effective(self):
        saved = self.saved()
        socket, socket_source = None, "none"
        if self.fixed["herdr_socket"]:
            socket, socket_source = Path(self.fixed["herdr_socket"]), "option"
        elif saved.get("herdr_socket"):
            socket, socket_source = Path(saved["herdr_socket"]), "saved"
        elif saved.get("herdr_session"):
            socket, socket_source = herdr_session_socket(saved["herdr_session"]), "saved"
        claude = (Path(self.fixed["claude_dir"]) if self.fixed["claude_dir"] else
                  Path(saved["claude_dir"]) if saved.get("claude_dir") else Path.home() / ".claude")
        claude_source_name = "option" if self.fixed["claude_dir"] else "saved" if saved.get("claude_dir") else "default"
        return {"claude_dir": str(claude.expanduser()), "claude_dir_source": claude_source_name,
                "herdr_session": saved.get("herdr_session"), "herdr_socket": str(socket.expanduser()) if socket else None,
                "herdr_socket_source": socket_source, "herdr_socket_exists": bool(socket and socket.expanduser().exists()),
                # No default: a web ui serves the one herdr session that started it, and pane IDs repeat across
                # sessions, so links appear only once the viewer names a web ui that serves the session read here.
                "herdr_web_url": saved.get("herdr_web_url"),
                "fixed": [key for key, value in self.fixed.items() if value]}

    def save(self, values, write):
        """Validate and store; write(path, bytes) is the caller's atomic writer. Launch options stay in force."""
        saved = self.saved()
        if "claude_dir" in values:
            claude = (values["claude_dir"] or "").strip()
            if claude and not Path(claude).expanduser().is_dir():
                raise ValueError(f"Claude folder does not exist: {claude}")
            saved["claude_dir"] = claude or None
        if "herdr_session" in values:
            name = (values["herdr_session"] or "").strip()
            if name:
                herdr_session_socket(name)
            saved["herdr_session"] = name or None
        if "herdr_socket" in values:
            saved["herdr_socket"] = (values["herdr_socket"] or "").strip() or None
        if "herdr_web_url" in values:
            address = (values["herdr_web_url"] or "").strip()
            saved["herdr_web_url"] = herdr_web_base(address) if address else None
        write(self.path, (json.dumps({k: v for k, v in saved.items() if v}, ensure_ascii=False, sort_keys=True) + "\n").encode())
        return self.effective()


class LabView:
    def close(self):
        self.herdr.close()

    def __init__(self, load_events, claude_dir, codex_dir, herdr_socket=None, herdr_web_url=None):
        self.load_events = load_events
        self.claude_dir, self.codex_dir = Path(claude_dir), Path(codex_dir)
        self.herdr_web_url = herdr_web_url
        self.herdr = HerdrStatus(herdr_socket)
        self.cache, self.located, self.sub_readers = {}, {}, {}
        self.lock = threading.Lock()

    def locate(self, key, hints):
        provider, _, session_id = key.partition(":")
        for hint in hints:
            if hint and Path(hint).is_file():
                return Path(hint)
        if key in self.located and self.located[key].is_file():
            return self.located[key]
        if provider == "claude":
            found = sorted(self.claude_dir.glob(f"projects/*/{session_id}.jsonl"))
        elif provider == "codex":
            found = sorted(self.codex_dir.glob(f"sessions/*/*/*/rollout-*-{session_id}.jsonl"))
        else:
            found = []
        if found:
            self.located[key] = found[-1]
            return found[-1]
        return None

    def read(self, key, path):
        """Incremental: a reader per transcript parses only what was appended since the last build."""
        reader = self.cache.get(path)
        if reader is None:
            reader = self.cache[path] = READERS[key.split(":", 1)[0]](path)
        return reader.update()

    def build(self):
        with self.lock:
            return self._build()

    def lineage(self, agents, sessions, errors):
        """Join Codex sessions continued by fork into one card; returns session key -> card key.

        Only a recorded forked_from_id joins sessions, never a shared herdr name. Earlier
        sessions are read too, so their inputs and receipts stay visible. Lines keep their
        real session keys.
        """
        parent = {}
        pending = [key for key in sessions if key.startswith("codex:")]
        while pending:
            key = pending.pop()
            forked = sessions[key].get("forked_from")
            if not forked or parent.get(key):
                continue
            parent[key] = earlier = "codex:" + forked
            if earlier in sessions:
                continue
            path = self.locate(earlier, [])
            if not path:
                continue
            try:
                sessions[earlier] = self.read(earlier, path)
                pending.append(earlier)
            except (OSError, ValueError) as exc:
                errors.append(f"could not read the transcript of {earlier}: {exc}")
        children = {}
        for key, above in parent.items():
            children.setdefault(above, []).append(key)
        cards = {}
        for key in set(parent) | set(children):
            root = key
            while root in parent:
                root = parent[root]
            members, frontier = [root], [root]
            while frontier:
                frontier = [child for item in frontier for child in sorted(children.get(item, []))]
                members += frontier
            live = [m for m in members if agents.get(m, {}).get("live")]
            current = live[-1] if live else members[-1]
            for member in members:
                cards[member] = current
            card = agents.setdefault(current, {"key": current, "provider": "codex", "session_id": current.split(":", 1)[1],
                                               "name": None, "live": False, "status": None})
            card.setdefault("transcript", (sessions.get(current) or {}).get("path"))
            card["name"] = card.get("name") or next((agents[m]["name"] for m in reversed(members) if agents.get(m, {}).get("name")), None)
            card["sessions"] = [{"key": m, "session_id": m.split(":", 1)[1], "forked_from": parent.get(m),
                                 "transcript": (sessions.get(m) or {}).get("path"), "current": m == current,
                                 "created_at": (sessions.get(m) or {}).get("created_at")} for m in members]
            for member in members:
                if member != current:
                    agents.pop(member, None)
        return cards

    def _build(self):
        herdr = self.herdr.current()
        events = self.load_events()
        messages = [e for e in events if e.get("type") == "agent.message" and e.get("transport") == "herdr"]
        agents, hints, errors = {}, {}, []
        places = {}  # (name, tool, pane) -> agent keys herdr reported there, in the journal or the live list

        def note(key, name=None, info=None, live=False):
            if not key:
                return
            agent = agents.setdefault(key, {"key": key, "provider": key.split(":", 1)[0], "session_id": key.split(":", 1)[1],
                                            "name": None, "live": False, "status": None})
            agent["name"] = name or agent["name"]
            if (info or {}).get("pane_id") and (info.get("name") or name):
                places.setdefault((info.get("name") or name, agent["provider"], info["pane_id"]), set()).add(key)
            session = (info or {}).get("agent_session") or {}
            if session.get("kind") == "path":
                hints.setdefault(key, []).append(session.get("value"))
            if live:
                agent.update(live=True, pane_id=info.get("pane_id"), status=info.get("agent_status"),
                             web_link=herdr_web_link(self.herdr_web_url, info.get("pane_id")))

        for message in messages:
            details = message.get("herdr") or {}
            note(message.get("sender_agent_key"), message.get("from_agent"), details.get("sender"))
            note(message.get("recipient_agent_key"), message.get("to_agent"), details.get("recipient_before"))
        unreported = []
        for info in herdr.get("agents", []):
            key = agent_key(info)
            if key:
                note(key, info.get("name"), info, live=True)
            else:
                unreported.append({"name": info.get("name"), "pane_id": info.get("pane_id"), "agent": info.get("agent"),
                                   "status": info.get("agent_status"),
                                   "web_link": herdr_web_link(self.herdr_web_url, info.get("pane_id"))})
        late = {}  # Sent before the recipient reported its session (a freshly started Codex does so on its first input).
        for message in messages:
            before = (message.get("herdr") or {}).get("recipient_before") or {}
            if not message.get("recipient_agent_key") and before.get("pane_id"):
                place = (before.get("name") or message.get("to_agent"), before.get("agent"), before["pane_id"])
                late[message.get("message_id")] = places.get(place, set())
        sessions = {}
        for key, agent in agents.items():
            path = self.locate(key, hints.get(key, []))
            agent["transcript"] = str(path) if path else None
            if not path:
                continue
            try:
                sessions[key] = self.read(key, path)
                agent["title"] = sessions[key].get("title")
            except (OSError, ValueError) as exc:
                errors.append(f"could not read the transcript of {key}: {exc}")
        cards = self.lineage(agents, sessions, errors)
        running = claude_source.running_sessions(self.claude_dir)
        for key, agent in agents.items():
            if agent["provider"] != "claude":
                continue
            agent["runtime"] = running.get(agent["session_id"])
            if key in sessions:
                calls = {c["tool_use_id"]: c for c in sessions[key].get("agent_calls", [])}
                subagents = claude_source.read_subagents(sessions[key]["path"], self.sub_readers)
                for sub in subagents:
                    call = calls.get(sub["tool_use_id"]) or {}
                    sub["call"] = {k: call.get(k) for k in ("record_id", "line", "at", "status", "is_async", "result_at")} if call else None
                agent["subagents"] = subagents
        deliveries, unattributed = delivery.match(events, sessions, late)
        confirmed = {r["record_id"] for d in deliveries if d["status"].startswith("confirmed") for r in d["receipts"]}
        since = min((m.get("at") or "" for m in messages), default=None)
        if since:  # Ten minutes before the first bridged message; journal times are UTC ISO, like the transcripts'.
            since = (datetime.fromisoformat(since.replace("Z", "+00:00")) - timedelta(minutes=10)).astimezone(timezone.utc)
            since = since.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        for key, agent in agents.items():
            agent["turn"] = turn_summary(sessions.get(key), confirmed)
            members = [m["key"] for m in agent.get("sessions") or []] or [key]
            agent["turns"] = [t for member in members for t in turn_intervals(sessions.get(member), confirmed, since)]
        derived = delivery.derived_results(messages, deliveries, sessions)
        by_id = {d["message_id"]: d for d in deliveries}
        edges = []
        for message in messages:
            state = by_id[message["message_id"]]
            edges.append({"id": "journal:" + message["message_id"], "basis": "journal", "kind": message.get("message_kind"),
                          "message_id": message["message_id"], "reply_to": message.get("reply_to"), "at": message.get("at"),
                          "from_key": message.get("sender_agent_key"), "to_key": state["recipient_agent_key"],
                          "from_name": message.get("from_agent"), "to_name": message.get("to_agent"),
                          "text": message.get("message"), "delivery": state,
                          "sender_verification": (message.get("herdr") or {}).get("sender_verification")})
        explicit = {m.get("reply_to"): m["message_id"] for m in messages if m.get("message_kind") == "result" and m.get("reply_to")}
        for result in derived:
            receipt = (by_id[result["reply_to"]]["receipts"] or [{}])[0]
            left = waiting_on(sessions.get(result["from_agent_key"]), agents.get(result["from_agent_key"], {}).get("subagents"),
                              result["evidence"]["turn"], receipt.get("entered_at") or receipt.get("at"), result["at"],
                              result["from_agent_key"], messages)
            edges.append({"id": result["id"], "basis": "derived", "kind": "result", "reply_to": result["reply_to"],
                          "interim": bool(left), "waiting_on": left,
                          "explicit_result": explicit.get(result["reply_to"]),
                          "other_inputs_in_turn": result["other_inputs_in_turn"],
                          "at": result["at"], "from_key": result["from_agent_key"], "to_key": result["to_agent_key"],
                          "from_name": result["from_agent"], "to_name": result["to_agent"], "text": result["message"],
                          "evidence": result["evidence"], "absorbed_mid_turn": result["absorbed_mid_turn"],
                          "sender_receipt": result["sender_receipt"]})
        edges.sort(key=lambda e: (delivery.moment(e["at"]) or datetime.min.replace(tzinfo=timezone.utc), e["id"]))
        order = []
        for edge in edges:
            edge["from_card"], edge["to_card"] = cards.get(edge["from_key"], edge["from_key"]), cards.get(edge["to_key"], edge["to_key"])
            for key in (edge["from_card"], edge["to_card"]):
                if key and key not in order:
                    order.append(key)
        order += [key for key in agents if key not in order and cards.get(key, key) == key]
        bundle = {"herdr": {k: v for k, v in herdr.items() if k != "agents"}, "agents": [agents[k] for k in order if k in agents],
                  "unreported_agents": unreported, "edges": edges, "unattributed": unattributed, "errors": errors,
                  "journal_events": len(events)}
        steady = {k: v for k, v in bundle["herdr"].items() if k not in ("checked_at", "events")}
        steady["events_state"] = (bundle["herdr"].get("events") or {}).get("state")  # Counters alone do not change the page.
        bundle["revision"] = hashlib.sha256(json.dumps({**bundle, "herdr": steady}, sort_keys=True, ensure_ascii=False,
                                                       default=str).encode()).hexdigest()[:16]
        bundle["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        return bundle
