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
| `herdr-trace serve [--session NAME] [--open] [--lang en\|ko]` | Serves the view of one herdr session on 127.0.0.1. Prints an address with an access token |
| `herdr-trace send --to NAME --kind assignment\|message\|result --message-file FILE [--reply-to ID]` | Sends a message to another agent through herdr, journaling the exact text first |
| `herdr-trace rules [--session NAME]` | Prints the rules to put in an agent's AGENTS.md so it sends through herdr-trace |
| `herdr-trace demo [--open]` | Serves the view on invented data with a fake herdr socket |

The page is in English or Korean. `--lang` sets the language it starts in; each viewer can switch with the
button in the top bar, and the page remembers that choice.

## As a herdr plugin

```sh
herdr plugin link /path/to/herdr-trace      # a local checkout
herdr plugin install codesholic/herdr-trace  # once the repository is public
```

Whenever herdr starts a session, the plugin serves that session's view in the background; it does nothing
while a view already runs for the session. It needs Python 3.11 or later.

| Action | What it does |
| --- | --- |
| `herdr-trace-open` | Opens the view in a browser, starting it first if needed |
| `herdr-trace-status` | Says whether the view runs for this session, and where |
| `herdr-trace-stop` | Stops the view for this session |

Settings go in `env` inside the plugin's config folder (`herdr plugin config-dir codesholic.herdr-trace`):
`HERDR_TRACE_AUTO_OPEN=1` opens the view when herdr starts, `HERDR_TRACE_LANG=ko` starts it in Korean.
herdr's plugin log shows the view's address, never its token.

Agents send through the `herdr-trace` command, so it must be on their PATH as well.

## Privacy

- Everything stays on your machine. The server listens on 127.0.0.1 only and needs the token it prints.
- herdr-trace only reads Codex and Claude Code transcripts and asks herdr read-only questions. It never types
  into an agent and never answers an approval.
- Private reasoning is not collected.

## Notes

herdr-trace is not affiliated with herdr, OpenAI or Anthropic. It grew out of
[codex-dag](https://github.com/codesholic/codex-dag) (MIT) and keeps its licence.
