# AI Communication Scenarios — Claude ↔ Codex

A Python orchestrator that runs structured conversations between **Claude Code** and **OpenAI Codex CLI**. Both CLIs are invoked in non-interactive mode; the orchestrator passes the running transcript between them turn by turn.

> **Remote review server:** `review` mode can also run as an HTTP service that a third AI (e.g. Manus) calls when a feature is done. See [SERVER.md](./SERVER.md) for deployment and API.

## Prerequisites

- Python 3.10+
- Claude Code installed and logged in (`claude` on PATH)
- OpenAI Codex CLI installed and logged in (`codex` on PATH)

Verify:
```powershell
claude --version
codex --version
```

## Modes

| Mode | Roles | Purpose |
|---|---|---|
| `chat` | Both equal | Casual back-and-forth on a topic |
| `collab` | Codex writes, Claude reviews | Iterative code authoring + review |
| `debate` | Claude = PRO, Codex = CON | Adversarial argument on a topic |
| `orchestrator` | Claude plans, Codex executes | Task decomposed into subtasks |
| `review` | Both review independently, then reconcile | **Cross-model code review** — most useful for analyzing your own code |

## Usage

```powershell
# Casual chat
python ai_chat.py --mode chat --topic "Will AI agents replace IDEs?"

# Pair programming
python ai_chat.py --mode collab --task "Write a Python LRU cache class with tests"

# Debate
python ai_chat.py --mode debate --topic "Should LLMs be allowed to commit to main?"

# Delegation
python ai_chat.py --mode orchestrator --task "Design a CLI todo app with SQLite storage"

# Cross-model code review — single file
python ai_chat.py --mode review --file path/to/code.py

# Working-tree diff or `git diff <ref>`
python ai_chat.py --mode review --diff
python ai_chat.py --mode review --diff main

# Whole feature branch — diff + commit log + file stats
python ai_chat.py --mode review --branch feature/foo
python ai_chat.py --mode review --branch feature/foo --base develop
python ai_chat.py --mode review --branch feature/foo --repo C:/path/to/other/repo
```

Common flags:
- `--turns N` — number of rounds for conversation modes (default 6; each round = both speakers go once). Ignored in `review` mode.
- `--topic` for `chat` / `debate`
- `--task` for `collab` / `orchestrator`
- `--file <path>` | `--diff [<ref>]` | `--branch <name>` for `review`
- `--base <ref>` — base for `--branch` comparison (default `main`)
- `--repo <path>` — point at a different git repo (default: current directory)

## Why `review` mode is the useful one

The other four modes are mostly novelty — two agreeable models tend to validate each other rather than find truth. `review` is structured to avoid that trap:

1. **Phase 1 — independent.** Each model reviews the code without seeing the other's notes. No anchoring. Each produces a structured review: verdict, bugs, concerns, nits.
2. **Phase 2 — reconcile.** Each model now sees both reviews and is asked to focus *only* on disagreements or things the other missed. They take positions.

What this gives you that single-model review doesn't:
- **High-confidence findings:** both flagged it → almost certainly real.
- **Useful disagreements:** worth a human look — those are the ambiguous spots.
- **Cross-checking factual claims:** in testing, Codex caught Claude making a factually wrong statement about Python semantics. Single-model review can't do that.

## Output

Live: colored speaker tags + replies streamed to the terminal.
Saved: `transcripts/YYYY-MM-DD_HHMMSS_<mode>.md`.

## Customizing roles

Edit the role prompts in `prompts/<mode>.md`. Each file has two sections marked `### CLAUDE` and `### CODEX`. Change tone, length limits, or constraints there — no Python edits needed.

## How it works

1. The orchestrator picks an order (e.g. Claude first, Codex second).
2. For each turn, it builds a transcript view tagged from the speaker's perspective ("YOU said:" vs "CODEX said:").
3. It calls the right CLI with a system prompt (the role) and a user message (transcript + seed + "your turn").
4. Output is captured and appended to history.
5. After all rounds, the full conversation is saved as markdown.

Codex is invoked with `-s read-only --ephemeral` so it cannot touch the filesystem.
Claude is invoked with `-p --append-system-prompt <role>` for clean isolated calls.
