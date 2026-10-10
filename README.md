# herdr-trace

See what passed between your coding agents in [herdr](https://herdr.dev), and whether it arrived.

> **Status: pre-alpha, not released. macOS only for now; Linux comes later.** The plan is in [docs/plan.md](docs/plan.md).

![The graph view: a Codex agent and a Claude Code agent with the assignments, results and messages between them, and the evidence for one delivery.](docs/media/graph.png)

When agents from different tools (Codex, Claude Code) work together in one herdr session, herdr shows each
pane, but not the conversation between them. herdr-trace draws it: who asked whom for what, whether the
text reached the other agent's conversation, and which reply answered it. Every line carries its evidence:
the exact text the sender's bridge journaled, and the record in the recipient's own transcript.

It is local and read-only. It reads Codex and Claude Code transcripts and asks herdr read-only questions;
it never types into an agent and never answers an approval.

## What you see

- **Agents as cards**, one per tool session, with herdr's state (working, idle, awaiting a response) and
  the turn each one is on. A working agent's card moves; one waiting on an approval stands out.
- **Lines between them** for assignments, results and messages, each with its delivery state:
  delivered, delivered with the text changed, queued, sent while the recipient was busy, not confirmed,
  or not delivered.
- **Results two ways.** A result an agent sends back is drawn as sent. The last reply of the turn an
  assignment started is drawn as derived; when that turn ended while work it handed off (a background
  subagent, or another agent) was still out, the reply is marked as interim, not as a result.
- **Subagents** of Claude Code agents, with the prompt they were given and their final reply.
- **A timeline** with each agent's turns as bars on real time, and the deliveries between them.
- **Links into [herdr web ui](https://github.com/devswha/herdr-web-ui)**: give its address, and each card
  opens that agent's pane there.
- English or Korean; each viewer picks.

![The timeline view: turns as bars on real time, with the deliveries between the agents.](docs/media/timeline.png)

## How it works

```
agent ── herdr-trace send ──▶ journal (exact text, first) ──▶ herdr agent prompt ──▶ the other agent
                                   │
herdr-trace serve ◀── journal + the recipient's own transcript (~/.codex, ~/.claude, read only) + herdr socket (read only)
```

herdr does not record who sent a prompt, and the recipient's transcript shows the text as if a person typed
it. So agents send through `herdr-trace send`, which writes the text with a short header
(`[herdr-trace msg=<id> from=<name> kind=<kind>]`) to a journal before herdr delivers it. A delivery is
confirmed only when that header appears in the recipient's transcript after the journal entry. Input
without a journaled header is listed as a person's input and never drawn as a message from an agent.

## Install

macOS, Python 3.11 or later.

```sh
pipx install git+https://github.com/codesholic/herdr-trace   # once the repository is public
pipx install /path/to/herdr-trace                             # from a local checkout
```

Or run it as a herdr plugin (below); its `herdr-trace-install-command` action puts the command on PATH.

## Quick start

1. Run `herdr-trace serve --open` inside herdr, or install the plugin so every session gets its view.
2. Tell your agents how to send: `herdr-trace rules` prints the rules; add them to the project's
   `AGENTS.md` (or `CLAUDE.md`).
3. Ask one agent to hand work to another, for example "ask reviewer to review this change and apply what it
   finds". The lines appear as the messages travel.

To look around first without any agents, `herdr-trace demo --open` serves the view on invented data.

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
while a view already runs for the session.

| Action | What it does |
| --- | --- |
| `herdr-trace-open` | Opens the view in a browser, starting it first if needed |
| `herdr-trace-status` | Says whether the view runs for this session, and where |
| `herdr-trace-stop` | Stops the view for this session |
| `herdr-trace-install-command` | Writes `~/.local/bin/herdr-trace`, which runs this plugin's checkout, so agents can send |
| `herdr-trace-remove-command` | Removes that file again; a file the plugin did not write is left alone |

Settings go in `env` inside the plugin's config folder (`herdr plugin config-dir codesholic.herdr-trace`):
`HERDR_TRACE_AUTO_OPEN=1` opens the view when herdr starts, `HERDR_TRACE_LANG=ko` starts it in Korean.
herdr's plugin log shows the view's address, never its token.

## Limits

- Checked on macOS 26 with herdr 0.9.3 (protocol 22), Codex CLI 0.159.2 and Claude Code 2.1.296, on Python
  3.12 and 3.13. Python 3.11 is supported but not yet checked.
- Codex and Claude Code transcripts, and herdr's API, are not public formats and change between versions.
  herdr-trace ignores what it does not know and says when it cannot read something.
- Only messages sent through `herdr-trace send` are drawn between agents. Codex's default sandbox blocks
  herdr's socket, so Codex asks for approval each time an agent sends.
- herdr can judge a Codex start-up dialog (an update notice, for example) as idle. Check a freshly started
  Codex agent's screen before sending to it.
- A derived result is an inference from the recipient's transcript; the result an agent sends back is the
  evidence to rely on.
- herdr web ui serves the one herdr session that started it, and pane IDs repeat across sessions; give its
  address only when it serves the session herdr-trace reads.

## Privacy

- Everything stays on your machine. The server listens on 127.0.0.1 only, needs the token it prints, and
  checks the Host and Origin of every request.
- Transcripts are read, never changed. Private reasoning is not collected.

## Notes

herdr-trace is not affiliated with herdr, OpenAI or Anthropic. It grew out of
[codex-dag](https://github.com/codesholic/codex-dag) and keeps its MIT licence.
