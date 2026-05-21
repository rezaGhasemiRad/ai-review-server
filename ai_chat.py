#!/usr/bin/env python3
"""Orchestrate a conversation between Claude Code and OpenAI Codex CLI.

Usage:
    python ai_chat.py --mode chat --topic "Is REST better than GraphQL?"
    python ai_chat.py --mode collab --task "Write a Python LRU cache"
    python ai_chat.py --mode debate --topic "Should AI write production code?"
    python ai_chat.py --mode orchestrator --task "Build a CLI todo app"
"""
import argparse
import datetime
import fnmatch
import pathlib
import shutil
import subprocess
import sys
import tempfile

# Hard cap on diff payload — model context limits.
MAX_REVIEW_CHARS = 500_000

# Path patterns that almost never carry signal in a feature-branch review.
NOISE_PATTERNS = [
    # Lockfiles
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
    "poetry.lock", "Gemfile.lock", "composer.lock", "Cargo.lock", "go.sum",
    "*.lock",
    # Minified / built
    "*.min.js", "*.min.css", "*.min.map", "*.map",
    # Generated / vendored / build output (directory components)
    "dist", "build", "out", "node_modules", ".next", ".nuxt", "coverage",
    "vendor", "__pycache__", ".venv", "venv", "target", "bin", "obj",
    # Binary-ish
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.ico", "*.pdf",
    "*.woff", "*.woff2", "*.ttf", "*.otf", "*.eot",
    "*.zip", "*.tar", "*.gz", "*.7z",
    "*.snap",  # jest snapshots
]

ROOT = pathlib.Path(__file__).parent.resolve()
PROMPTS_DIR = ROOT / "prompts"
TRANSCRIPTS_DIR = ROOT / "transcripts"


def _resolve(name: str) -> str:
    """Find an executable on PATH, handling Windows .cmd/.exe shims."""
    found = shutil.which(name)
    if not found:
        sys.exit(f"Could not find '{name}' on PATH. Install it and try again.")
    return found


CLAUDE_BIN = _resolve("claude")
CODEX_BIN = _resolve("codex")

C = {
    "claude": "\033[36m",   # cyan
    "codex":  "\033[33m",   # yellow
    "sys":    "\033[90m",   # grey
    "bold":   "\033[1m",
    "reset":  "\033[0m",
}


def _safe_print(s: str) -> None:
    try:
        print(s)
    except UnicodeEncodeError:
        print(s.encode("ascii", "replace").decode("ascii"))


def speak(who: str, text: str) -> None:
    color = C.get(who, "")
    _safe_print(f"{color}{C['bold']}== {who.upper()} =={C['reset']}")
    _safe_print(text)
    _safe_print("")


def info(msg: str) -> None:
    _safe_print(f"{C['sys']}[{msg}]{C['reset']}")


def load_roles(mode: str) -> dict[str, str]:
    """Read prompts/<mode>.md and split into sections by '### NAME' headers."""
    path = PROMPTS_DIR / f"{mode}.md"
    if not path.exists():
        sys.exit(f"Missing role prompts: {path}")
    sections: dict[str, str] = {}
    current = None
    buf: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("### "):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = line[4:].strip().lower()
            buf = []
        else:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def call_claude(role: str, user_msg: str) -> str:
    """Pipe user_msg via stdin to avoid Windows 8191-char command line limit."""
    cmd = [
        CLAUDE_BIN,
        "-p",
        "--append-system-prompt", role,
    ]
    result = subprocess.run(
        cmd, input=user_msg, capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def call_codex(role: str, user_msg: str) -> str:
    """Pipe role+user_msg via stdin; capture clean reply via --output-last-message."""
    combined = f"{role}\n\n---\n\n{user_msg}"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        out_path = f.name
    try:
        cmd = [
            CODEX_BIN, "exec",
            "-s", "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--color", "never",
            "--output-last-message", out_path,
            "-",  # read prompt from stdin
        ]
        result = subprocess.run(
            cmd, input=combined, capture_output=True, text=True, encoding="utf-8"
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"codex failed (exit {result.returncode}): {result.stderr.strip()}"
            )
        return pathlib.Path(out_path).read_text(encoding="utf-8").strip()
    finally:
        try:
            pathlib.Path(out_path).unlink()
        except OSError:
            pass


def build_transcript_view(history: list[dict], for_speaker: str) -> str:
    """Render history as a readable transcript for the next speaker."""
    if not history:
        return "(no previous messages)"
    lines = []
    for turn in history:
        speaker = turn["speaker"]
        tag = "YOU" if speaker == for_speaker else speaker.upper()
        lines.append(f"### {tag} said:")
        lines.append(turn["text"])
        lines.append("")
    return "\n".join(lines).strip()


def save_transcript(mode: str, seed: str, history: list[dict]) -> pathlib.Path:
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = TRANSCRIPTS_DIR / f"{stamp}_{mode}.md"
    lines = [f"# {mode.title()} session — {stamp}", "", f"**Seed:** {seed}", ""]
    for turn in history:
        lines.append(f"## {turn['speaker'].upper()}")
        lines.append("")
        lines.append(turn["text"])
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# Order of speakers per mode (cycles for `turns` rounds, each round = both speak once).
TURN_ORDER = {
    "chat":         ["claude", "codex"],
    "collab":       ["codex", "claude"],   # codex writes, claude reviews
    "debate":       ["claude", "codex"],
    "orchestrator": ["claude", "codex"],   # claude plans, codex executes
}

CONVERSATION_MODES = set(TURN_ORDER.keys())


def _git(repo: str | None, *args: str) -> subprocess.CompletedProcess:
    cmd = ["git"] + (["-C", repo] if repo else []) + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


def _split_diff_by_file(diff: str) -> list[tuple[str, str]]:
    """Parse `git diff` output into [(path, file_chunk), ...]."""
    chunks: list[tuple[str, str]] = []
    current_path: str | None = None
    buf: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_path is not None:
                chunks.append((current_path, "".join(buf)))
            parts = line.strip().split()
            # parts: ['diff', '--git', 'a/path/to/file', 'b/path/to/file']
            if len(parts) >= 4 and parts[2].startswith("a/"):
                current_path = parts[2][2:]
            elif len(parts) >= 4 and parts[3].startswith("b/"):
                current_path = parts[3][2:]
            else:
                current_path = "<unknown>"
            buf = [line]
        else:
            buf.append(line)
    if current_path is not None:
        chunks.append((current_path, "".join(buf)))
    return chunks


def _is_noise(path: str) -> bool:
    name = pathlib.Path(path).name
    parts = path.replace("\\", "/").split("/")
    for pat in NOISE_PATTERNS:
        # Directory-component match (no wildcard, no extension)
        if "/" not in pat and "." not in pat and "*" not in pat:
            if pat in parts:
                return True
            continue
        # Glob match against full path or basename
        if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(name, pat):
            return True
    return False


def _filter_diff(
    diff: str, only_globs: list[str], include_all: bool
) -> tuple[str, dict]:
    chunks = _split_diff_by_file(diff)
    kept: list[tuple[str, str]] = []
    skipped: list[tuple[str, int]] = []
    for path, chunk in chunks:
        if only_globs:
            keep = any(
                fnmatch.fnmatch(path, g) or fnmatch.fnmatch(pathlib.Path(path).name, g)
                for g in only_globs
            )
        elif include_all:
            keep = True
        else:
            keep = not _is_noise(path)
        if keep:
            kept.append((path, chunk))
        else:
            skipped.append((path, len(chunk)))
    body = "".join(c for _, c in kept)
    stats = {
        "total_files": len(chunks),
        "kept_files": len(kept),
        "skipped_files": len(skipped),
        "skipped_chars": sum(b for _, b in skipped),
        "kept_paths": [p for p, _ in kept],
        "skipped_paths": [p for p, _ in skipped],
    }
    return body, stats


def load_code_input(
    file_path: str | None,
    diff_ref: str | None,
    branch: str | None,
    base: str,
    repo: str | None,
    only_globs: list[str] | None = None,
    include_all: bool = False,
    summary_only: bool = False,
) -> tuple[str, str]:
    """Return (label, content) for the review subject."""
    if file_path:
        p = pathlib.Path(file_path)
        if not p.exists():
            sys.exit(f"File not found: {file_path}")
        content = p.read_text(encoding="utf-8", errors="replace")
        return (f"file: {p}", f"```\n{content}\n```")

    if branch:
        # Verify refs exist
        for ref in (branch, base):
            r = _git(repo, "rev-parse", "--verify", ref)
            if r.returncode != 0:
                sys.exit(f"git ref not found: {ref}\n{r.stderr.strip()}")

        log = _git(repo, "log", "--pretty=format:%h %s", f"{base}..{branch}")
        if log.returncode != 0:
            sys.exit(f"git log failed: {log.stderr.strip()}")
        commits = log.stdout.strip() or "(no commits ahead of base)"

        stat = _git(repo, "diff", "--stat", f"{base}...{branch}")
        if stat.returncode != 0:
            sys.exit(f"git diff --stat failed: {stat.stderr.strip()}")

        if summary_only:
            label = f"branch {branch} vs {base} (summary)"
            payload = (
                f"## Branch\n`{branch}` compared to base `{base}`\n\n"
                f"## Commits on this branch (oldest first)\n"
                f"```\n{commits}\n```\n\n"
                f"## File-level changes\n"
                f"```\n{stat.stdout.strip()}\n```\n\n"
                f"## Note\nNo line-level diff was provided. Review the design and "
                f"scope from the commits and file list; flag risks you'd want to verify "
                f"by reading the diff."
            )
            return (label, payload)

        diff = _git(repo, "diff", f"{base}...{branch}")
        if diff.returncode != 0:
            sys.exit(f"git diff failed: {diff.stderr.strip()}")
        if not diff.stdout.strip():
            sys.exit(f"No changes between {base} and {branch} — nothing to review.")

        raw_chars = len(diff.stdout)
        filtered, fstats = _filter_diff(diff.stdout, only_globs or [], include_all)
        if fstats["skipped_files"]:
            info(
                f"filtered {fstats['skipped_files']}/{fstats['total_files']} files "
                f"({fstats['skipped_chars']:,} chars skipped); "
                f"kept {fstats['kept_files']} files ({len(filtered):,} chars)"
            )
            if not only_globs and not include_all:
                preview = ", ".join(fstats["skipped_paths"][:5])
                extra = "" if fstats["skipped_files"] <= 5 else f", +{fstats['skipped_files'] - 5} more"
                info(f"skipped (noise): {preview}{extra}")

        if len(filtered) > MAX_REVIEW_CHARS:
            kept_preview = "\n  ".join(fstats["kept_paths"][:30])
            extra = "" if fstats["kept_files"] <= 30 else f"\n  ... +{fstats['kept_files'] - 30} more"
            sys.exit(
                f"Diff is still {len(filtered):,} chars after filtering "
                f"(raw was {raw_chars:,}, cap is {MAX_REVIEW_CHARS:,}).\n\n"
                f"Files still in scope:\n  {kept_preview}{extra}\n\n"
                f"Options:\n"
                f"  --only <glob>      restrict to specific paths (repeatable)\n"
                f"  --summary-only     skip the diff, review commit log + file stats only\n"
                f"  --include-all      bypass filtering (will likely fail at the model)\n\n"
                f"Tip: pick the 1-3 most important files with --only \"src/foo/*.py\" --only \"...\""
            )

        label = f"branch {branch} vs {base}" + (f" (repo: {repo})" if repo else "")
        if only_globs:
            label += f" — only: {', '.join(only_globs)}"
        payload = (
            f"## Branch\n`{branch}` compared to base `{base}`\n\n"
            f"## Commits on this branch (oldest first)\n"
            f"```\n{commits}\n```\n\n"
            f"## File-level changes\n"
            f"```\n{stat.stdout.strip()}\n```\n\n"
            f"## Diff (reviewing {fstats['kept_files']} of {fstats['total_files']} files)\n"
            f"```diff\n{filtered}\n```"
        )
        return (label, payload)

    if diff_ref is not None:
        # diff_ref == "" means "default git diff" (working tree vs HEAD)
        cmd = ["diff"] + ([diff_ref] if diff_ref else [])
        result = _git(repo, *cmd)
        if result.returncode != 0:
            sys.exit(f"git diff failed: {result.stderr.strip()}")
        if not result.stdout.strip():
            sys.exit("git diff produced no output — nothing to review.")
        label = f"git diff {diff_ref}" if diff_ref else "git diff (working tree)"
        return (label, f"```diff\n{result.stdout}\n```")

    sys.exit("review mode needs --file <path>, --diff [<ref>], or --branch <name>")


HANDOFF_AUTO_PATHS = ["HANDOFF.md", ".ai-collab/HANDOFF.md", "docs/HANDOFF.md"]


def _load_handoff(
    branch: str | None,
    repo: str | None,
    handoff_path: str | None,
    no_handoff: bool,
) -> tuple[str, str] | None:
    """Return (source_label, content) if a handoff was found, else None."""
    if no_handoff or not branch:
        return None
    candidates = [handoff_path] if handoff_path else HANDOFF_AUTO_PATHS
    for path in candidates:
        r = _git(repo, "show", f"{branch}:{path}")
        if r.returncode == 0 and r.stdout.strip():
            return (f"{branch}:{path}", r.stdout.strip())
    if handoff_path:
        sys.exit(f"Handoff file not found on branch {branch}: {handoff_path}")
    return None


def run_review(
    file_path: str | None,
    diff_ref: str | None,
    branch: str | None,
    base: str,
    repo: str | None,
    only_globs: list[str] | None = None,
    include_all: bool = False,
    summary_only: bool = False,
    context: str | None = None,
    context_file: str | None = None,
    handoff_path: str | None = None,
    no_handoff: bool = False,
) -> None:
    roles = load_roles("review")
    if "claude" not in roles or "codex" not in roles:
        sys.exit("prompts/review.md must contain '### CLAUDE' and '### CODEX' sections")

    label, payload = load_code_input(
        file_path, diff_ref, branch, base, repo,
        only_globs=only_globs, include_all=include_all, summary_only=summary_only,
    )

    extra_context_parts: list[str] = []
    handoff = _load_handoff(branch, repo, handoff_path, no_handoff)
    if handoff:
        src, content = handoff
        info(f"loaded handoff from {src} ({len(content):,} chars)")
        extra_context_parts.append(
            f"## Implementer's handoff (from `{src}`)\n\n{content}"
        )
    if context:
        extra_context_parts.append(f"## Inline note\n\n{context.strip()}")
    if context_file:
        cf = pathlib.Path(context_file)
        if not cf.exists():
            sys.exit(f"Context file not found: {context_file}")
        extra_context_parts.append(
            f"## Extra brief (from `{context_file}`)\n\n"
            f"{cf.read_text(encoding='utf-8', errors='replace').strip()}"
        )
    extra_context = "\n\n---\n\n".join(extra_context_parts).strip()
    size = len(payload)
    info(f"mode=review  subject={label}  size={size} chars")
    if size > 80_000:
        info("WARNING: very large input; reviews may be shallow or truncated by model context")

    result = _run_review_pipeline(roles, label, payload, extra_context)
    info(f"transcript saved: {result['transcript_path']}")


def _run_review_pipeline(
    roles: dict[str, str], label: str, payload: str, extra_context: str
) -> dict:
    """Two-phase review pipeline shared by the CLI and the HTTP server."""
    context_block = f"# Context\n{extra_context}\n\n" if extra_context else ""

    initial_msg = (
        f"{context_block}"
        f"# Code under review ({label})\n\n"
        f"{payload}\n\n"
        f"# Your task\n"
        f"Produce your structured review of the code above. You are reviewing INDEPENDENTLY — "
        f"you have not seen any other reviewer's notes. Be specific and concrete."
        + (
            " The context above includes the implementer's handoff and/or guidance from the "
            "requester — let it shape what you prioritize, but treat the implementer's "
            "self-assessment as a CLAIM to verify against the diff, not as ground truth."
            if extra_context else ""
        )
    )

    history: list[dict] = [{"speaker": "system", "text": f"Subject: {label}\n\n{payload}"}]

    info("phase 1/2 — independent reviews")
    info("claude reviewing...")
    claude_review = call_claude(roles["claude"], initial_msg)
    speak("claude", claude_review)
    history.append({"speaker": "claude (initial)", "text": claude_review})

    info("codex reviewing...")
    codex_review = call_codex(roles["codex"], initial_msg)
    speak("codex", codex_review)
    history.append({"speaker": "codex (initial)", "text": codex_review})

    info("phase 2/2 — reconciling disagreements")
    reconcile_msg = (
        f"{context_block}"
        f"# Code under review ({label})\n\n{payload}\n\n"
        f"# Reviewer A — Claude's review\n{claude_review}\n\n"
        f"# Reviewer B — Codex's review\n{codex_review}\n\n"
        f"# Your task\n"
        f"You have just seen both independent reviews above. Focus ONLY on points where the "
        f"two reviews disagree, or where one reviewer caught something the other missed. "
        f"For each such point, take a clear position: which reviewer is right (or are both "
        f"partially right, or both wrong)? Be brief — one short paragraph per disagreement. "
        f"If both reviews fully agree, say so in one sentence and stop. Do not re-summarize "
        f"points both reviews already agree on."
    )

    info("claude reconciling...")
    claude_reconcile = call_claude(roles["claude"], reconcile_msg)
    speak("claude", claude_reconcile)
    history.append({"speaker": "claude (reconcile)", "text": claude_reconcile})

    info("codex reconciling...")
    codex_reconcile = call_codex(roles["codex"], reconcile_msg)
    speak("codex", codex_reconcile)
    history.append({"speaker": "codex (reconcile)", "text": codex_reconcile})

    transcript_path = save_transcript("review", label, history)
    transcript_md = transcript_path.read_text(encoding="utf-8")

    return {
        "label": label,
        "claude_review": claude_review,
        "codex_review": codex_review,
        "claude_reconcile": claude_reconcile,
        "codex_reconcile": codex_reconcile,
        "transcript_path": str(transcript_path),
        "transcript_md": transcript_md,
    }


def review_branch_structured(
    repo: str,
    branch: str,
    base: str = "main",
    only_globs: list[str] | None = None,
    include_all: bool = False,
    summary_only: bool = False,
    handoff_path: str | None = None,
    no_handoff: bool = False,
    context: str | None = None,
) -> dict:
    """Server-friendly entrypoint. Runs a branch review and returns a structured result.

    Raises RuntimeError on validation/load failures (instead of sys.exit). The HTTP
    server catches these and reports them on the job record.
    """
    roles = load_roles("review")
    if "claude" not in roles or "codex" not in roles:
        raise RuntimeError("prompts/review.md must contain '### CLAUDE' and '### CODEX' sections")

    label, payload = load_code_input(
        None, None, branch, base, repo,
        only_globs=only_globs or [],
        include_all=include_all,
        summary_only=summary_only,
    )

    parts: list[str] = []
    handoff = _load_handoff(branch, repo, handoff_path, no_handoff)
    handoff_source = None
    if handoff:
        handoff_source, content = handoff
        info(f"loaded handoff from {handoff_source} ({len(content):,} chars)")
        parts.append(f"## Implementer's handoff (from `{handoff_source}`)\n\n{content}")
    if context:
        parts.append(f"## Inline note\n\n{context.strip()}")
    extra_context = "\n\n---\n\n".join(parts).strip()

    info(f"mode=review (server)  subject={label}  size={len(payload)} chars")
    result = _run_review_pipeline(roles, label, payload, extra_context)
    result["handoff_source"] = handoff_source
    result["request"] = {
        "repo": repo,
        "branch": branch,
        "base": base,
        "only_globs": only_globs or [],
        "include_all": include_all,
        "summary_only": summary_only,
        "handoff_path": handoff_path,
        "no_handoff": no_handoff,
    }
    return result


def run(mode: str, seed: str, turns: int) -> None:
    roles = load_roles(mode)
    if "claude" not in roles or "codex" not in roles:
        sys.exit(f"prompts/{mode}.md must contain '### CLAUDE' and '### CODEX' sections")

    info(f"mode={mode}  turns={turns}  seed={seed!r}")
    history: list[dict] = []
    order = TURN_ORDER[mode]

    for round_idx in range(turns):
        for speaker in order:
            role = roles[speaker]
            transcript = build_transcript_view(history, speaker)
            user_msg = (
                f"# Session seed\n{seed}\n\n"
                f"# Conversation so far\n{transcript}\n\n"
                f"# Your turn\n"
                f"Respond as yourself. Keep replies focused and conversational. "
                f"Do not run shell commands or modify files unless your role explicitly requires it."
            )
            info(f"round {round_idx + 1}/{turns} — {speaker} thinking...")
            try:
                if speaker == "claude":
                    text = call_claude(role, user_msg)
                else:
                    text = call_codex(role, user_msg)
            except Exception as e:
                info(f"{speaker} errored: {e}")
                text = f"(error: {e})"
            history.append({"speaker": speaker, "text": text})
            speak(speaker, text)

    path = save_transcript(mode, seed, history)
    info(f"transcript saved: {path}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--mode",
        choices=["chat", "collab", "debate", "orchestrator", "review"],
        required=True,
    )
    p.add_argument("--topic", help="Conversation topic (for chat/debate)")
    p.add_argument("--task", help="Task description (for collab/orchestrator)")
    p.add_argument("--file", help="Path to a source file to review (review mode)")
    p.add_argument(
        "--diff",
        nargs="?",
        const="",
        default=None,
        help="Review `git diff [<ref>]`. With no value, diffs working tree vs HEAD.",
    )
    p.add_argument(
        "--branch",
        help="Branch to review (review mode). Diffs <base>...<branch>, includes commit log.",
    )
    p.add_argument(
        "--base",
        default="main",
        help="Base ref to compare --branch against (default: main).",
    )
    p.add_argument(
        "--repo",
        help="Path to the git repo (default: current directory). Useful when ai_chat.py lives elsewhere.",
    )
    p.add_argument(
        "--only",
        action="append",
        default=[],
        help="Restrict --branch review to paths matching this glob. Repeatable.",
    )
    p.add_argument(
        "--include-all",
        action="store_true",
        help="Skip default noise filtering (lockfiles, builds, minified, binaries) for --branch.",
    )
    p.add_argument(
        "--summary-only",
        action="store_true",
        help="For --branch: review only the commit log + file stats, not the full diff.",
    )
    p.add_argument(
        "--context",
        help="Freeform context for the reviewers (intent of the branch, what to focus on, what to ignore).",
    )
    p.add_argument(
        "--context-file",
        help="Path to a file whose contents are prepended as reviewer context (design doc, ticket, brief).",
    )
    p.add_argument(
        "--handoff",
        help=(
            "Path (on the branch) to a handoff file written by the implementing AI. "
            "Defaults to auto-discovery: HANDOFF.md, .ai-collab/HANDOFF.md, docs/HANDOFF.md."
        ),
    )
    p.add_argument(
        "--no-handoff",
        action="store_true",
        help="Disable handoff auto-discovery for --branch reviews.",
    )
    p.add_argument("--turns", type=int, default=6, help="Rounds for conversation modes (default 6)")
    args = p.parse_args()

    if args.mode == "review":
        if not args.file and args.diff is None and not args.branch:
            sys.exit("review mode needs --file <path>, --diff [<ref>], or --branch <name>")
        run_review(
            args.file, args.diff, args.branch, args.base, args.repo,
            only_globs=args.only,
            include_all=args.include_all,
            summary_only=args.summary_only,
            context=args.context,
            context_file=args.context_file,
            handoff_path=args.handoff,
            no_handoff=args.no_handoff,
        )
        return

    seed = args.topic or args.task
    if not seed:
        sys.exit("Provide --topic (chat/debate) or --task (collab/orchestrator)")
    run(args.mode, seed, args.turns)


if __name__ == "__main__":
    main()
