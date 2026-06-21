# Code Duo

**Two AI coding agents — Claude and Codex — collaborating in one window.** Route a prompt to either or both, watch each one work step by step, hand work from one to the other, and keep an eye on cost and on whether the AI actually did what it claimed.

No API keys. Code Duo drives the `claude` and `codex` CLIs you already have, authenticated with your existing Max / ChatGPT subscriptions.

![Code Duo](docs/cover.svg)

## Why

When you pair-program with an AI you often want a second opinion, or to hand a half-finished change to a different model, or to run the same task two ways and compare. Doing that across two separate apps is clumsy. Code Duo puts both agents side by side, working on the same project, and adds the things that make a multi-agent session trustworthy: live step-by-step output, cross-review, token/cost tracking, and a watchdog that flags when an agent says it did work but the disk never changed.

## Features

- **One window, two agents.** Press `Tab` to switch target — Claude → Codex → Both. `Both` runs the same prompt through both in parallel so you can compare.
- **Live streaming.** See each agent work as it happens: narration, tool steps (`⌘` commands, `✎` file edits, `📖` reads), then the answer — not just a final blob.
- **Hand-off & cross-review.** Every reply has `→ Hand to …` and `→ … review` so one agent can take over or audit the other's work, all on the same project files.
- **Shared project.** Point both agents at a real project directory; whatever one writes, the other can read.
- **Per-agent controls.** Model, permission/sandbox mode, reasoning effort, and Fast mode — wired straight to the CLI flags.
- **History rails.** Your real Claude and Codex sessions, grouped by project (including Claude's custom groups), with clean AI titles; click one to resume it, or start a new session from a center dialog.
- **Token panel.** Per-vendor cost, tokens, cache-hit rate, sessions for the last 24h, parsed locally — plus one-click **Clear cache** to drop a bloated context.
- **Watchdog.** Each turn compares what the AI *claimed* against what *actually changed on disk*, and flags busywork ("5 actions claimed, 0 files changed") or going in circles ("looping 3×").
- **Add files.** Upload from your computer or drag-and-drop into the input; the file is saved into the project so the agent can read it.

## Requirements

- macOS (Linux/Windows path discovery is built in but less tested)
- **Claude Code** CLI (`claude`), signed in with your subscription
- **Codex** (`/Applications/Codex.app`), signed in with your ChatGPT subscription
- Python 3 (standard library only — no third-party packages)

Code Duo auto-detects the CLIs and your session locations at startup, across default install paths, and respects `CLAUDE_CONFIG_DIR` / `CODEX_HOME` / `DUO_CLAUDE_BIN` / `DUO_CODEX_BIN`.

## Quick start

```bash
./start.sh          # or: python3 app.py
```

Open **http://localhost:8765** in a browser. The startup banner prints which CLIs were detected.

## Architecture

- `app.py` — pure-stdlib HTTP server. Drives the two CLIs in streaming mode (`claude --output-format stream-json`, `codex exec --json`), parses their events, and streams normalized steps to the browser as NDJSON.
- `index.html` — the whole UI (vanilla JS, no build step).
- `logo.svg`, `docs/cover.svg` — brand mark and cover image.

Titles and grouping come from each app's own local data: Claude's desktop session index and custom groups (read from its Local Storage), Codex's `session_index.jsonl` and workspace labels — all read-only.

## Notes

- It's all subscription-driven; Code Duo never sends anything to a paid API.
- Per-session renames / pins / archive are stored locally in Code Duo and never touch the official apps' data.
