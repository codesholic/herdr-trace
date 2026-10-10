"""herdr-trace command line: serve the view, send through the bridge, print the agent rules, run the demo."""
import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__, bridge, paths


def main(argv=None):
    parser = argparse.ArgumentParser(prog="herdr-trace", description="See what passed between coding agents in herdr, "
                                     "and whether it arrived.")
    parser.add_argument("--version", action="version", version=f"herdr-trace {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="Serve the view of one herdr session on 127.0.0.1")
    serve.add_argument("--session", help="herdr session to read (default: the session this runs in, else herdr's default)")
    serve.add_argument("--socket", type=Path, help="herdr socket to read instead of the session's usual one")
    serve.add_argument("--journal", type=Path, help="Journal file to read instead of the session's own")
    serve.add_argument("--port", type=int, default=0, help="Port on 127.0.0.1 (default: any free port)")
    serve.add_argument("--claude-dir", type=Path, help="Claude Code data folder (default: ~/.claude), read only")
    serve.add_argument("--codex-dir", type=Path, help="Codex data folder (default: ~/.codex), read only")
    serve.add_argument("--open", action="store_true", help="Open the view in a browser")

    send = commands.add_parser("send", help="Send a message to another herdr agent, journaling the exact text first")
    send.add_argument("--to", required=True, help="herdr agent name of the recipient")
    send.add_argument("--kind", required=True, choices=bridge.KINDS)
    body = send.add_mutually_exclusive_group(required=True)
    body.add_argument("--message-file", type=Path, help="UTF-8 message body; - reads standard input")
    body.add_argument("--message", help="Message body")
    send.add_argument("--reply-to", help="Message ID (msg=... in the received header) this message answers")
    send.add_argument("--from", dest="sender", help="Own herdr agent name, where the tool removed herdr's variables")
    send.add_argument("--herdr-session", help="herdr session, where the tool removed herdr's variables")
    send.add_argument("--journal", type=Path, help="Journal file (default: the session's own)")
    send.add_argument("--herdr-bin", default=os.environ.get("HERDR_TRACE_HERDR_BIN", "herdr"))
    send.add_argument("--wait", action="store_true", help="Wait until herdr sees the recipient settle")
    send.add_argument("--timeout-ms", type=int)

    rules = commands.add_parser("rules", help="Print the rules agents follow to send through herdr-trace")
    rules.add_argument("--session", help="Session name to put in the rules (default: the session this runs in)")

    demo = commands.add_parser("demo", help="Serve the view on invented data with a fake herdr socket")
    demo.add_argument("--port", type=int, default=0)
    demo.add_argument("--open", action="store_true", help="Open the view in a browser")
    demo.add_argument("--dir", type=Path, help="Folder for the invented data (default: a temporary folder)")

    args = parser.parse_args(argv)
    try:
        return {"serve": run_serve, "send": run_send, "rules": run_rules, "demo": run_demo}[args.command](args)
    except (ValueError, OSError, bridge.HerdrError) as exc:
        parser.exit(1, f"herdr-trace: error: {exc}\n")


def run_serve(args):
    from .server import serve
    session = paths.session_name(args.session)
    claude = args.claude_dir or (paths.claude_dir() if os.environ.get("CLAUDE_CONFIG_DIR") else None)
    serve(session=session, state_dir=paths.session_dir(session), journal_path=args.journal or paths.journal_file(session),
          herdr_socket=args.socket or paths.herdr_socket(session), claude_dir=claude,
          codex_dir=args.codex_dir or paths.codex_dir(), port=args.port, open_browser=args.open)
    return 0


def run_send(args):
    if args.message is not None:
        text = args.message
    elif str(args.message_file) == "-":
        text = sys.stdin.read()
    else:
        text = args.message_file.read_text(encoding="utf-8")
    journal = args.journal or paths.journal_file(paths.session_name(args.herdr_session))
    outcome = bridge.send(args.to, args.kind, text, journal_path=journal, herdr_bin=args.herdr_bin,
                          session=args.herdr_session, wait=args.wait, timeout_ms=args.timeout_ms,
                          reply_to=args.reply_to, declared=args.sender)
    print(json.dumps({k: outcome.get(k) for k in ("message_id", "outcome", "error_code", "error_message")
                      if outcome.get(k) is not None}, ensure_ascii=False))
    return 0 if outcome["outcome"] == "submitted" else 2


def run_rules(args):
    text = (Path(__file__).with_name("rules.md")).read_text(encoding="utf-8")
    print(text.replace("{session}", paths.session_name(args.session)), end="")
    return 0


def run_demo(args):
    from .demo import run
    return run(port=args.port, open_browser=args.open, directory=args.dir)
