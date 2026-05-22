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
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

# Model-call resilience (issue #3): each claude/codex invocation is bounded
# by a timeout and retried on transient failure.
MODEL_CALL_TIMEOUT_S = 600   # 10 min — a single review call should never exceed this
MODEL_CALL_RETRIES = 1       # total attempts = RETRIES + 1
MODEL_CALL_BACKOFF_S = 10

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


EMPTY_USAGE = {"input_tokens": 0, "output_tokens": 0, "usd": 0.0}


def _run_cli(cmd: list[str], stdin_text: str, label: str) -> subprocess.CompletedProcess:
    """Run a model CLI with a timeout, retrying on timeout or non-zero exit.

    Transient failures (rate limit, network blip, a hung call) recover;
    a genuinely broken call just fails one retry later. Raises RuntimeError
    after the final attempt.
    """
    last_err = "unknown error"
    for attempt in range(MODEL_CALL_RETRIES + 1):
        try:
            result = subprocess.run(
                cmd, input=stdin_text, capture_output=True, text=True,
                encoding="utf-8", timeout=MODEL_CALL_TIMEOUT_S,
            )
            if result.returncode == 0:
                return result
            last_err = (f"exit {result.returncode}: "
                        f"{(result.stderr or '').strip()[:300]}")
        except subprocess.TimeoutExpired:
            last_err = f"timed out after {MODEL_CALL_TIMEOUT_S}s"
        if attempt < MODEL_CALL_RETRIES:
            info(f"{label} failed ({last_err}); retry in {MODEL_CALL_BACKOFF_S}s")
            time.sleep(MODEL_CALL_BACKOFF_S)
    raise RuntimeError(
        f"{label} failed after {MODEL_CALL_RETRIES + 1} attempt(s): {last_err}"
    )


def call_claude(role: str, user_msg: str) -> tuple[str, dict]:
    """Run claude in JSON-output mode. Returns (text, usage).

    usage = {input_tokens, output_tokens, usd}. Falls back to plain text
    parsing if the JSON envelope can't be read.
    """
    cmd = [
        CLAUDE_BIN,
        "-p",
        "--output-format", "json",
        "--append-system-prompt", role,
    ]
    result = _run_cli(cmd, user_msg, "claude")
    try:
        env = json.loads(result.stdout)
        u = env.get("usage") or {}
        usage = {
            "input_tokens": (u.get("input_tokens", 0) or 0)
            + (u.get("cache_read_input_tokens", 0) or 0)
            + (u.get("cache_creation_input_tokens", 0) or 0),
            "output_tokens": u.get("output_tokens", 0) or 0,
            "usd": float(env.get("total_cost_usd", 0.0) or 0.0),
        }
        return (env.get("result", "") or "").strip(), usage
    except (json.JSONDecodeError, AttributeError, TypeError):
        return result.stdout.strip(), dict(EMPTY_USAGE)


def call_codex(role: str, user_msg: str) -> tuple[str, dict]:
    """Run codex in JSON-event mode. Returns (text, usage).

    Clean reply text comes from --output-last-message; token usage is
    parsed from the `turn.completed` event on stdout. Codex reports no
    USD (subscription), so usd stays 0.
    """
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
            "--json",
            "--output-last-message", out_path,
            "-",  # read prompt from stdin
        ]
        result = _run_cli(cmd, combined, "codex")
        text = pathlib.Path(out_path).read_text(encoding="utf-8").strip()
        usage = dict(EMPTY_USAGE)
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "turn.completed":
                u = ev.get("usage") or {}
                usage["input_tokens"] = (u.get("input_tokens", 0) or 0) \
                    + (u.get("cached_input_tokens", 0) or 0)
                usage["output_tokens"] = u.get("output_tokens", 0) or 0
        return text, usage
    finally:
        try:
            pathlib.Path(out_path).unlink()
        except OSError:
            pass


def _extract_findings_block(review_text: str) -> tuple[str, dict | None]:
    """Split a trailing ```json {...}``` findings block off a review.

    Returns (clean_markdown_with_block_removed, parsed_dict_or_None).
    Never raises — a malformed block just yields None.
    """
    txt = review_text.strip()
    m = re.search(r"```json\s*(\{.*\})\s*```\s*$", txt, re.DOTALL)
    if not m:
        return review_text, None
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return review_text, None
    return txt[:m.start()].rstrip(), data


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


def _build_prior_iteration_section(prior_result: dict | None) -> str:
    """Render a prior-iteration context block for re-reviews. Empty if none."""
    if not prior_result:
        return ""
    parts = [
        "## Previous iteration context",
        "",
        "This is a **re-review** of the same feature. The reconciled findings from the "
        "prior review pass are below. Treat them as known context.",
        "",
        "Focus your effort on:",
        "1. **Verifying** that fixes claimed in the implementer's handoff are actually "
        "present and correct in the current diff.",
        "2. **New issues** introduced by the changes since the prior pass.",
        "3. **Carry-over** — findings from the prior review that are still unaddressed.",
        "",
        "Do NOT re-raise findings the implementer marked as resolved or that were already "
        "explicitly accepted as out-of-scope.",
        "",
    ]
    def _findings_digest(findings: list) -> str:
        if not findings:
            return "_(no structured findings recorded)_"
        rows = []
        for f in findings:
            if not isinstance(f, dict):
                continue
            sev = f.get("severity", "?")
            loc = f.get("file") or f.get("repo") or ""
            rows.append(f"- [{sev}] {f.get('title', '(untitled)')}"
                        + (f" — `{loc}`" if loc else ""))
        return "\n".join(rows) if rows else "_(no structured findings recorded)_"

    cf = prior_result.get("claude_findings")
    co_f = prior_result.get("codex_findings")
    if cf is not None or co_f is not None:
        parts += [
            "### Findings raised in the prior pass",
            "",
            "Reviewer A (Claude):",
            _findings_digest(cf or []),
            "",
            "Reviewer B (Codex):",
            _findings_digest(co_f or []),
            "",
        ]
    cr = prior_result.get("claude_reconcile")
    if cr:
        parts += ["### Prior reviewer A (Claude) — reconciled", "", cr, ""]
    co = prior_result.get("codex_reconcile")
    if co:
        parts += ["### Prior reviewer B (Codex) — reconciled", "", co, ""]
    return "\n".join(parts).strip()


def _build_test_section(test_results_by_repo: dict | None) -> str:
    """Render a test-results block. Empty if no tests were run."""
    if not test_results_by_repo:
        return ""
    lines = ["## Test results", ""]
    for repo_key, r in test_results_by_repo.items():
        status = r.get("status", "unknown")
        header = f"### `{repo_key}` — {status}"
        if "duration_s" in r:
            header += f" ({r['duration_s']}s)"
        lines.append(header)
        if status == "skipped":
            lines.append(f"_{r.get('reason', 'no test.sh')}_")
        elif status == "timeout":
            lines.append(f"_timed out after {r.get('timeout_s', '?')}s_")
        elif status == "error":
            lines.append(f"_runner error: {r.get('reason', '')}_")
        else:
            ec = r.get("exit_code", "?")
            lines.append(f"exit_code={ec}")
            tail = r.get("stdout_tail", "").strip()
            err = r.get("stderr_tail", "").strip()
            if tail:
                lines += ["", "```", tail, "```"]
            if err:
                lines += ["", "stderr:", "```", err, "```"]
        lines.append("")
    return "\n".join(lines).strip()


def _run_review_pipeline(
    roles: dict[str, str],
    label: str,
    payload: str,
    extra_context: str,
    prior_result: dict | None = None,
    test_results_by_repo: dict | None = None,
    progress_cb=None,
) -> dict:
    """Two-phase review pipeline shared by the CLI and the HTTP server."""
    cb = progress_cb if callable(progress_cb) else (lambda _m: None)
    context_block = f"# Context\n{extra_context}\n\n" if extra_context else ""

    prior_block = _build_prior_iteration_section(prior_result)
    if prior_block:
        prior_block = prior_block + "\n\n"

    test_block = _build_test_section(test_results_by_repo)
    if test_block:
        test_block = test_block + "\n\n"

    initial_msg = (
        f"{context_block}"
        f"{prior_block}"
        f"{test_block}"
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
        + (
            " The 'Previous iteration context' is from a prior review of the same feature — "
            "use it to avoid re-raising resolved findings, but verify the implementer's claimed "
            "fixes are actually in the current diff."
            if prior_block else ""
        )
        + (
            " The 'Test results' section above shows whether each repo's pre-existing test "
            "suite passes on this branch. Failing tests are a hard finding — call them out "
            "even if the rest of the diff looks fine."
            if test_block else ""
        )
        + "\n\n# Machine-readable findings\n"
        "After your markdown review, append a fenced ```json code block — and NOTHING "
        "after it — matching this exact shape:\n"
        '{"verdict": "approve|request-changes|blocking", "findings": ['
        '{"severity": "bug|concern|nit", "repo": "<repo name or null>", '
        '"file": "<path or null>", "lines": "<range or null>", '
        '"title": "<one-line summary>", "detail": "<short explanation>"}]}\n'
        "The findings array must mirror the issues in your markdown review — one entry "
        "per concrete issue. If you found nothing in a category, simply omit it. The "
        "markdown review above the block stays the human-readable version; the JSON is "
        "the machine copy."
    )

    history: list[dict] = [{"speaker": "system", "text": f"Subject: {label}\n\n{payload}"}]
    usage_acc = {"claude": dict(EMPTY_USAGE), "codex": dict(EMPTY_USAGE)}

    def _add(who: str, u: dict) -> None:
        for k in ("input_tokens", "output_tokens", "usd"):
            usage_acc[who][k] += u.get(k, 0) or 0

    info("phase 1/2 — independent reviews")
    cb("phase 1/2 · Claude reviewing")
    info("claude reviewing...")
    claude_review, u = call_claude(roles["claude"], initial_msg)
    _add("claude", u)
    claude_review, claude_block = _extract_findings_block(claude_review)
    speak("claude", claude_review)
    history.append({"speaker": "claude (initial)", "text": claude_review})

    cb("phase 1/2 · Codex reviewing")
    info("codex reviewing...")
    codex_review, u = call_codex(roles["codex"], initial_msg)
    _add("codex", u)
    codex_review, codex_block = _extract_findings_block(codex_review)
    speak("codex", codex_review)
    history.append({"speaker": "codex (initial)", "text": codex_review})

    info("phase 2/2 — reconciling disagreements")
    reconcile_msg = (
        f"{context_block}"
        f"{prior_block}"
        f"{test_block}"
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

    cb("phase 2/2 · Claude reconciling")
    info("claude reconciling...")
    claude_reconcile, u = call_claude(roles["claude"], reconcile_msg)
    _add("claude", u)
    speak("claude", claude_reconcile)
    history.append({"speaker": "claude (reconcile)", "text": claude_reconcile})

    cb("phase 2/2 · Codex reconciling")
    info("codex reconciling...")
    codex_reconcile, u = call_codex(roles["codex"], reconcile_msg)
    _add("codex", u)
    speak("codex", codex_reconcile)
    history.append({"speaker": "codex (reconcile)", "text": codex_reconcile})
    cb("finalizing")

    transcript_path = save_transcript("review", label, history)
    transcript_md = transcript_path.read_text(encoding="utf-8")

    def _findings(block: dict | None) -> list:
        if isinstance(block, dict) and isinstance(block.get("findings"), list):
            return block["findings"]
        return []

    def _verdict(block: dict | None, review_md: str) -> str | None:
        if isinstance(block, dict) and block.get("verdict"):
            return str(block["verdict"]).lower()
        m = re.search(r"\*{0,2}Verdict:?\*{0,2}\s*`?([A-Za-z\-]+)`?", review_md)
        return m.group(1).lower() if m else None

    claude_findings = _findings(claude_block)
    codex_findings = _findings(codex_block)
    usage = {
        "claude": usage_acc["claude"],
        "codex": usage_acc["codex"],
        "total_input_tokens": usage_acc["claude"]["input_tokens"] + usage_acc["codex"]["input_tokens"],
        "total_output_tokens": usage_acc["claude"]["output_tokens"] + usage_acc["codex"]["output_tokens"],
        "total_usd": round(usage_acc["claude"]["usd"] + usage_acc["codex"]["usd"], 4),
    }
    info(
        f"usage: claude {usage_acc['claude']['input_tokens']}/{usage_acc['claude']['output_tokens']} tok "
        f"${usage_acc['claude']['usd']:.4f} · codex "
        f"{usage_acc['codex']['input_tokens']}/{usage_acc['codex']['output_tokens']} tok"
    )

    return {
        "label": label,
        "claude_review": claude_review,
        "codex_review": codex_review,
        "claude_reconcile": claude_reconcile,
        "codex_reconcile": codex_reconcile,
        "claude_findings": claude_findings,
        "codex_findings": codex_findings,
        "claude_verdict": _verdict(claude_block, claude_review),
        "codex_verdict": _verdict(codex_block, codex_review),
        "usage": usage,
        "transcript_path": str(transcript_path),
        "transcript_md": transcript_md,
    }


def _branch_payload(
    repo: str,
    branch: str,
    base: str,
    only_globs: list[str],
    include_all: bool,
    summary_only: bool,
) -> tuple[str, str, dict, int]:
    """Build the payload for a single (repo, branch, base) triple.

    Returns (short_label, payload_markdown, fstats, raw_diff_chars).
    Caller is responsible for the size cap when combining multiple repos.
    """
    label, payload = load_code_input(
        None, None, branch, base, repo,
        only_globs=only_globs, include_all=include_all, summary_only=summary_only,
    )
    return label, payload, {}, len(payload)


def review_multi_repo_structured(
    repos: list[dict],
    handoff_path: str | None = None,
    no_handoff: bool = False,
    context: str | None = None,
    only_globs: list[str] | None = None,
    include_all: bool = False,
    summary_only: bool = False,
    prior_result: dict | None = None,
    test_results_by_repo: dict | None = None,
    progress_cb=None,
) -> dict:
    """Run a single review whose subject is changes across multiple repos.

    Each entry in `repos` is {repo: <local path>, repo_url: <url>, branch: <name>,
    base: <name>}. The server is responsible for cloning/fetching before calling.
    """
    if not repos:
        raise RuntimeError("repos must be non-empty")

    roles = load_roles("review")
    if "claude" not in roles or "codex" not in roles:
        raise RuntimeError("prompts/review.md must contain '### CLAUDE' and '### CODEX' sections")

    sections: list[str] = []
    handoff_source: str | None = None
    handoff_content: str | None = None
    repo_labels: list[str] = []

    for i, r in enumerate(repos, start=1):
        repo_path = r["repo"]
        branch = r["branch"]
        base = r["base"]
        repo_id = r.get("repo_url") or repo_path
        info(f"loading repo {i}/{len(repos)}: {repo_id} ({branch} vs {base})")

        sub_label, sub_payload, _, _ = _branch_payload(
            repo=repo_path, branch=branch, base=base,
            only_globs=only_globs or [], include_all=include_all,
            summary_only=summary_only,
        )
        repo_labels.append(repo_id)
        sections.append(
            f"# Repo {i}: `{repo_id}`\n\n"
            f"_({sub_label})_\n\n"
            f"{sub_payload}"
        )

        # First handoff found wins.
        if handoff_content is None:
            h = _load_handoff(branch, repo_path, handoff_path, no_handoff)
            if h:
                handoff_source = f"{repo_id} @ {h[0]}"
                handoff_content = h[1]

    combined_payload = "\n\n---\n\n".join(sections)
    label = f"multi-repo feature across {len(repos)} repos: " + ", ".join(repo_labels)

    if len(combined_payload) > MAX_REVIEW_CHARS:
        raise RuntimeError(
            f"Combined diff is {len(combined_payload):,} chars (cap {MAX_REVIEW_CHARS:,}). "
            f"Use 'only' globs, set 'summary_only': true, or split into separate reviews."
        )

    parts: list[str] = []
    if handoff_content:
        info(f"using handoff from {handoff_source} ({len(handoff_content):,} chars)")
        parts.append(f"## Implementer's handoff (from `{handoff_source}`)\n\n{handoff_content}")
    if context:
        parts.append(f"## Inline note\n\n{context.strip()}")
    parts.append(
        "## Multi-repo review instructions\n\n"
        "This feature spans the repositories listed below. When reviewing, "
        "explicitly look for **cross-repo consistency issues** that single-repo review "
        "cannot catch: API contracts that change in one repo but not its consumers, "
        "schema/model field changes that the UI doesn't reflect, auth/permission rules "
        "applied unevenly, version/migration ordering, and naming drift between layers."
    )
    extra_context = "\n\n---\n\n".join(parts).strip()

    info(f"mode=review (multi-repo)  total payload {len(combined_payload):,} chars")
    if prior_result:
        info("prior iteration context present — re-review mode")
    if test_results_by_repo:
        passed = sum(1 for r in test_results_by_repo.values() if r.get("status") == "passed")
        info(f"test results provided: {passed}/{len(test_results_by_repo)} repos passed")
    result = _run_review_pipeline(
        roles, label, combined_payload, extra_context,
        prior_result=prior_result, test_results_by_repo=test_results_by_repo,
        progress_cb=progress_cb,
    )
    result["handoff_source"] = handoff_source
    result["request"] = {
        "repos": [
            {"repo_url": r.get("repo_url"), "branch": r["branch"], "base": r["base"]}
            for r in repos
        ],
        "only_globs": only_globs or [],
        "include_all": include_all,
        "summary_only": summary_only,
    }
    return result


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
    prior_result: dict | None = None,
    test_results_by_repo: dict | None = None,
    progress_cb=None,
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
    result = _run_review_pipeline(
        roles, label, payload, extra_context,
        prior_result=prior_result, test_results_by_repo=test_results_by_repo,
        progress_cb=progress_cb,
    )
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
                    text, _ = call_claude(role, user_msg)
                else:
                    text, _ = call_codex(role, user_msg)
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
