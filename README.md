# herdr-trace

See what passed between your coding agents in [herdr](https://herdr.dev), and whether it arrived.

> **Status: pre-alpha, not released.** The plan is in [docs/plan.md](docs/plan.md).

herdr-trace is a local, read-only view of how agents from different tools (Codex, Claude Code) work
together inside one herdr session: who asked whom for what, whether the message reached the other agent's
conversation, and which reply answered it. Each line in the view carries its evidence: the exact text the
sender's bridge journaled, and the record in the recipient's own transcript.

## Try it on invented data

```sh
PYTHONPATH=src python3 -m herdr_trace demo --open
```

## Commands

| Command | What it does |
| --- | --- |
| `herdr-trace serve [--session NAME] [--open]` | Serves the view of one herdr session on 127.0.0.1. Prints an address with an access token |
| `herdr-trace send --to NAME --kind assignment\|message\|result --message-file FILE [--reply-to ID]` | Sends a message to another agent through herdr, journaling the exact text first |
| `herdr-trace rules [--session NAME]` | Prints the rules to put in an agent's AGENTS.md so it sends through herdr-trace |
| `herdr-trace demo [--open]` | Serves the view on invented data with a fake herdr socket |

## Privacy

- Everything stays on your machine. The server listens on 127.0.0.1 only and needs the token it prints.
- herdr-trace only reads Codex and Claude Code transcripts and asks herdr read-only questions. It never types
  into an agent and never answers an approval.
- Private reasoning is not collected.

## Notes

herdr-trace is not affiliated with herdr, OpenAI or Anthropic. It grew out of
[codex-dag](https://github.com/codesholic/codex-dag) (MIT) and keeps its licence.
