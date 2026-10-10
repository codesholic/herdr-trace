"""Append-only event journal: one JSON object per line, numbered from 1, never edited.

The bridge writes the exact text of a message before it asks herdr to submit it, then
appends the transport outcome as a separate event. Readers and writers coordinate through
an advisory lock on a sidecar file, so a reader never sees half of an append.
"""
import contextlib
import fcntl
import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Fields an event of each type must carry; other types are refused.
REQUIRED = {"agent.message": ("message_id", "from_agent", "to_agent", "message"),
            "agent.delivery": ("message_id", "outcome")}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@contextlib.contextmanager
def lock(path, exclusive=False):
    """Hold the journal's advisory lock: shared for readers, exclusive for the writer."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path.parent / f".{path.name}.lock", "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_events(handle):
    """Events of an open journal, checking that lines are complete and numbered 1, 2, 3, ..."""
    handle.seek(0)
    data = handle.read()
    if data and not data.endswith("\n"):
        raise ValueError("Incomplete journal tail; preserve and repair it before continuing")
    events = []
    for number, line in enumerate(data.splitlines(), 1):
        event = json.loads(line)
        if event.get("seq") != number:
            raise ValueError(f"Invalid sequence at line {number}")
        events.append(event)
    return events


def load(path):
    path = Path(path)
    if not path.exists():
        return []
    with lock(path), path.open("r", encoding="utf-8") as handle:
        return read_events(handle)


def validate(event):
    required = REQUIRED.get(event.get("type"))
    if required is None:
        raise ValueError(f"Unknown event type: {event.get('type')}")
    if not all(event.get(key) for key in required):
        raise ValueError(f"{event['type']} events require {', '.join(required)}")


def append(path, event):
    """Validate and append one event; returns it with its time, sequence number and ID."""
    path = Path(path)
    validate(event)
    with lock(path, exclusive=True):
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "r+", encoding="utf-8") as handle:
            events = read_events(handle)
            recorded = {"at": now(), **event, "seq": len(events) + 1, "event_id": secrets.token_hex(8)}
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(recorded, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return recorded


def atomic_write(path, raw):
    """Replace a small file in one step, so a reader sees the old or the new content, never a mix."""
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
