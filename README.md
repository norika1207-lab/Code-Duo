# Code Duo

### Stop trusting one AI. Put two in the same room and make them check each other.

Claude and Codex, side by side in one window. Route a prompt to either or both, watch each one work step by step, hand a half-finished change from one to the other, and keep a watchdog on whether the AI actually did what it claimed — or just burned your tokens saying it would.

No API keys, ever. Code Duo drives the `claude` and `codex` CLIs you already pay for, on your existing Max / ChatGPT subscriptions.

![Code Duo](docs/cover.svg)

---

## The problem this solves

You give an AI a real task. It says "no problem, I've got this," sounds confident the whole way, burns a pile of tokens — and at the end shrugs and tells you it "overestimated the difficulty." The work never lands. You can't see it going in circles until it's already cost you an hour.

One confident AI will talk you into a wall. **Two AIs, watching each other, won't.**

## What you get

- **Two agents, one window, zero context-switching.** Press `Tab` to aim at Claude, Codex, or Both. `Both` runs the same prompt through both in parallel so you can compare two solutions instantly.
- **Watch the work, not just the answer.** Live streaming of every step — reasoning, commands run, files edited, reads — so you catch a bad path early instead of after it's burned your budget.
- **Cross-review and hand-off, built in.** One click sends one agent's work to the other to take over or audit, all on the same project files. When Claude is stuck, Codex gets a fresh crack at the exact same problem — on screen, side by side.
- **A watchdog for AI bullshit.** Every turn compares what the AI *claimed* against what *actually changed on disk*. It flags busywork ("5 actions claimed, 0 files changed") and going in circles ("looping 3×").
- **Your real sessions, not a blank slate.** Your actual Claude and Codex history shows up grouped by project (including Claude's custom groups), with clean titles. Resume any of them, or start a new one from a center dialog.
- **See the burn, kill the bloat.** Per-vendor cost, tokens, and cache-hit rate for the last 24h, parsed locally — plus one-click **Clear cache** to drop a bloated context and stop paying to re-cache it.
- **Per-agent controls** wired straight to the CLIs — model, permission/sandbox mode, reasoning effort, Fast mode.
- **Drag files in.** Upload from your computer or drop a file into the input; it lands in the project so the agent can read it.

## Who it's for

- You run several projects at once and you're tired of switching windows and losing context.
- You've been burned by an AI that promised the world and delivered a shrug.
- You want a second opinion on tap — two models, two solutions, arguing on one screen.
- You watch your spend and want it on the table, not on the month-end bill.

This isn't a cloud orchestrator tied to a plan that opens PRs for you. It's local, it drives your own logged-in CLIs, it brings your real sessions with you, and it never sends a thing to a paid API.

## Requirements

- macOS (Linux / Windows path discovery is built in but less tested)
- **Claude Code** CLI (`claude`), signed in with your subscription
- **Codex** (`/Applications/Codex.app`), signed in with your ChatGPT subscription
- Python 3 — standard library only, no dependencies

Code Duo auto-detects the CLIs and your session locations at startup and respects `CLAUDE_CONFIG_DIR` / `CODEX_HOME` / `DUO_CLAUDE_BIN` / `DUO_CODEX_BIN`.

## Quick start

```bash
./start.sh          # or: python3 app.py
```

Open **http://localhost:8765**. The startup banner prints which CLIs were detected.

## How it works

- `app.py` — a pure-stdlib HTTP server. It drives both CLIs in streaming mode (`claude --output-format stream-json`, `codex exec --json`), parses their events, and streams normalized steps to the browser as NDJSON.
- `index.html` — the whole UI in vanilla JS. No build step.

Titles and grouping come from each app's own local data — Claude's desktop session index and custom groups, Codex's `session_index.jsonl` and workspace labels — all read-only. Renames, pins, and archiving live only in Code Duo and never touch the official apps.
