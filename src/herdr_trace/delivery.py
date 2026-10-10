"""Connect journaled herdr messages to the recipient transcripts, and derive results.

Only a transcript record that carries the bridge header of a journaled message,
and was recorded after that journal event, confirms a delivery. Inputs without
such a header are a person's (or unknown) input and are never drawn as a
message from another agent.

A freshly started agent may not have reported its session when the bridge sent
to it, so the journal names no recipient key. Such a message is looked up only in
the sessions herdr reported under the recipient's name, tool and pane.
"""
from datetime import datetime

from .bridge import parse_header

# Statuses, reasons and normalizations are codes; the page words them in the viewer's language.
STATUSES = ("confirmed", "confirmed_modified", "queued", "pending_busy", "unconfirmed", "undelivered")


def moment(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def variation(sent, received):
    """None when identical; otherwise the first normalization that makes them equal, or a diff summary."""
    if sent == received:
        return None
    steps = (("line_endings", lambda s: s.replace("\r\n", "\n")),
             ("trailing_spaces", lambda s: "\n".join(line.rstrip() for line in s.replace("\r\n", "\n").split("\n"))),
             ("outer_whitespace", lambda s: "\n".join(line.rstrip() for line in s.replace("\r\n", "\n").split("\n")).strip()))
    for label, normalize in steps:
        if normalize(sent) == normalize(received):
            return {"equal_after": label, "sent_length": len(sent), "received_length": len(received)}
    offset = next((i for i, (a, b) in enumerate(zip(sent, received)) if a != b), min(len(sent), len(received)))
    return {"equal_after": None, "sent_length": len(sent), "received_length": len(received), "first_difference": offset}


def header_texts(prompt):
    """Codex keeps input blocks apart; the header must start one of them."""
    return prompt.get("blocks") or [prompt["text"]]


def match(events, sessions, late=None):
    """events: journal events; sessions: agent key -> reader output of the recipient transcript.

    late: message ID -> candidate agent keys for messages journaled without a recipient key.
    """
    late = late or {}
    messages = [e for e in events if e.get("type") == "agent.message" and e.get("transport") == "herdr"
                and e.get("message_id")]
    by_id = {m["message_id"]: m for m in messages}
    outcomes = {}
    for event in events:
        if event.get("type") == "agent.delivery":
            outcomes.setdefault(event.get("message_id"), []).append(event)
    found, unattributed = {}, []
    for key, session in sessions.items():
        for prompt in session.get("prompts", []):
            if prompt.get("injected") or prompt.get("system"):
                continue
            texts = [t for t in header_texts(prompt) if parse_header(t)]
            if not texts:
                unattributed.append(evidence(key, session, prompt, reason="no_header"))
                continue
            header = parse_header(texts[0])
            message = by_id.get(header["message_id"])
            if message is None:
                unattributed.append(evidence(key, session, prompt, reason="unknown_message"))
            elif message.get("recipient_agent_key") != key and key not in late.get(message["message_id"], ()):
                unattributed.append(evidence(key, session, prompt, reason="other_recipient"))
            elif moment(prompt.get("at")) and moment(message.get("at")) and moment(prompt["at"]) < moment(message["at"]):
                unattributed.append(evidence(key, session, prompt, reason="before_journal"))
            else:
                found.setdefault(message["message_id"], []).append((prompt, texts[0], session, key))
    deliveries = []
    for message in messages:
        key = message.get("recipient_agent_key")
        records = found.get(message["message_id"], [])
        resolved = None
        if not key and message["message_id"] in late:
            before = (message.get("herdr") or {}).get("recipient_before") or {}
            resolved = {"name": before.get("name") or message.get("to_agent"), "pane_id": before.get("pane_id"),
                        "agent": before.get("agent"), "candidates": sorted(late[message["message_id"]])}
            key = records[0][3] if records else None
        session = sessions.get(key)
        outcome = (outcomes.get(message["message_id"]) or [None])[-1]
        # herdr's view of the recipient in the reply to the submission itself.
        busy = (((outcome or {}).get("herdr") or {}).get("recipient_after") or {}).get("agent_status")
        item = {"message_id": message["message_id"], "journal_seq": message.get("seq"),
                "journal_event_id": message.get("event_id"), "journal_at": message.get("at"),
                "outcome": outcome and {k: outcome.get(k) for k in ("seq", "at", "outcome", "error_code", "error_message")},
                "recipient_agent_key": key, "recipient_status_at_send": busy, "receipts": [], "duplicates": 0}
        if resolved:
            item["recipient_resolved"] = resolved
        if records:
            prompt, text, receiving, _ = records[0]
            change = variation(message["message"], text)
            item.update(status="confirmed" if change is None else "confirmed_modified", variation=change,
                        receipts=[evidence(k, s, p) for p, t, s, k in records], duplicates=len(records) - 1)
            sent_at, received_at = moment(message.get("at")), moment(prompt.get("entered_at") or prompt.get("at"))
            if sent_at and received_at:
                item["latency_seconds"] = round((received_at - sent_at).total_seconds(), 3)
        else:
            queued = [q for q in (session or {}).get("queue", []) if q.get("operation") == "enqueue"
                      and isinstance(q.get("content"), str) and (parse_header(q["content"]) or {}).get("message_id")
                      == message["message_id"]]
            if queued:
                item.update(status="queued", queue=[{"line": q["line"], "at": q["at"]} for q in queued])
            elif outcome and outcome.get("outcome") == "agent_blocked":
                item.update(status="undelivered")
            elif session and outcome and outcome.get("outcome") == "submitted" and busy == "working":
                # Codex keeps input for a working agent on screen only, so no record exists until it is taken.
                item.update(status="pending_busy")
            else:
                item.update(status="unconfirmed", reason=None if session else
                            "late_recipient_not_found" if resolved else "no_recipient_record")
        deliveries.append(item)
    return deliveries, unattributed


def evidence(key, session, prompt, reason=None):
    result = {"agent_key": key, "path": session.get("path"), "record_id": prompt.get("record_id"),
              "line": prompt.get("line"), "at": prompt.get("at"), "form": prompt.get("form"),
              "turn": prompt.get("turn_index", prompt.get("turn_id"))}
    if prompt.get("entered_at"):
        result["entered_at"] = prompt["entered_at"]  # Queued input: when the running turn took it in.
    if prompt.get("envelope"):
        result["envelope"] = prompt["envelope"]  # The recipient tool wrapped the text; the inner text was compared.
    if reason:
        result["reason"] = reason
    return result


def derived_results(messages, deliveries, sessions):
    """Path A: the reply that closes the turn a delivered assignment started.

    The result travels recipient -> sender. Whether the sender then read it is a
    separate receipt, confirmed only by the exact reply text in a sender tool output.
    """
    by_id = {m["message_id"]: m for m in messages}
    results = []
    for delivery in deliveries:
        message = by_id[delivery["message_id"]]
        if delivery["status"] not in {"confirmed", "confirmed_modified"} or message.get("message_kind") != "assignment":
            continue
        receipt = delivery["receipts"][0]
        session = sessions[delivery["recipient_agent_key"]]
        turn = find_turn(session, receipt)
        if not turn or turn.get("status") != "completed" or not turn.get("final_text"):
            continue
        sender = sessions.get(message.get("sender_agent_key")) or {}
        read_back = [o for o in sender.get("tool_outputs", []) if turn["final_text"].strip() in o.get("text", "")
                     and (not moment(turn.get("finished_at")) or not moment(o.get("at"))
                          or moment(o["at"]) >= moment(turn["finished_at"]))]
        results.append({"id": "derived:" + delivery["message_id"], "basis": "derived", "reply_to": delivery["message_id"],
            "from_agent": message["to_agent"], "to_agent": message["from_agent"],
            "from_agent_key": delivery["recipient_agent_key"], "to_agent_key": message.get("sender_agent_key"),
            "message": turn["final_text"], "at": turn.get("finished_at"),
            "absorbed_mid_turn": receipt.get("form") == "queued_command",
            "other_inputs_in_turn": other_inputs(session, receipt),
            "evidence": {"path": session.get("path"), "turn": receipt.get("turn"),
                         "final_record_ids": turn.get("final_record_ids"), "end_record_id": turn.get("end_record_id"),
                         "end_line": turn.get("end_line")},
            "sender_receipt": [{"record_id": o["record_id"], "line": o["line"], "at": o["at"]} for o in read_back]})
    return results


def other_inputs(session, receipt):
    """Record IDs of other inputs that entered the same turn; the reply may answer them too."""
    turn = receipt.get("turn")
    if session.get("provider") == "claude":
        ids = [p["record_id"] for p in session.get("prompts", []) if p.get("turn_index") == turn]
    else:
        ids = [p["record_id"] for p in session.get("prompts", []) if p.get("turn_id") == turn and not p.get("injected")]
    return [i for i in ids if i != receipt.get("record_id")]


def find_turn(session, receipt):
    turn = receipt.get("turn")
    if session.get("provider") == "claude":
        return session["turns"][turn] if isinstance(turn, int) and 0 <= turn < len(session["turns"]) else None
    return next((t for t in session.get("turns", []) if t.get("turn_id") == turn), None)
