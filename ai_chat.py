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
from concurrent.futures import Future, ThreadPoolExecutor

# Model-call resilience (issue #3): each claude/codex invocation is bounded
# by a timeout and retried on transient failure.
MODEL_CALL_TIMEOUT_S = 600   # 10 min — a single review call should never exceed this
MODEL_CALL_RETRIES = 1       # total attempts = RETRIES + 1
MODEL_CALL_BACKOFF_S = 10

# Hard cap on diff payload — model context limits.
MAX_REVIEW_CHARS = 1_100_000
# Claude-only reviews aren't bound by Codex's ~400K-token window, so they
# can take a much larger diff (Opus 4.8 holds ~1M tokens ≈ ~4M chars). Used
# as the cap when a submission selects Claude as the sole reviewer — lets
# big cases (e.g. a whole multi-repo feature) be reviewed in one pass.
MAX_REVIEW_CHARS_CLAUDE_ONLY = 3_000_000

# Path patterns that almost never carry signal in a feature-branch review.
# The filter runs only when the caller didn't pass explicit `only` globs;
# `only` is treated as an intentional override.
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
    # EF Core auto-generated migration scaffolding. The hand-written
    # migration `.cs` (Up/Down) is kept; only the mechanical companions
    # — Designer file + the full model snapshot — are dropped.
    "*.Designer.cs", "*ModelSnapshot.cs",
    # Conventional generated-source markers (Roslyn / source generators,
    # TS/JS codegen, OpenAPI clients, gRPC, etc.).
    "*.generated.*", "*.g.cs", "*.g.ts", "*.g.js",
    # Handoff is loaded by the server via path, not via the diff —
    # stripping it from the diff doesn't hide it from reviewers.
    ".ai-collab",
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


# Resolve the CLIs at import, but tolerate their absence so the module can be
# imported for its pure helpers (slicing/reduction orchestration, tests) on a
# box without the CLIs. call_claude/call_codex raise a clear error if actually
# invoked while unresolved. On the server both resolve normally (unchanged).
try:
    CLAUDE_BIN = _resolve("claude")
    CODEX_BIN = _resolve("codex")
except SystemExit:
    CLAUDE_BIN = None
    CODEX_BIN = None

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
    if CLAUDE_BIN is None:
        raise RuntimeError("claude CLI not found on PATH")
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
    if CODEX_BIN is None:
        raise RuntimeError("codex CLI not found on PATH")
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
    # Codex sometimes omits the closing ``` fence, so make it optional. The
    # greedy {.*} still grabs through the final }; trailing whitespace / a
    # stray fence at the end are tolerated.
    m = re.search(r"```json\s*(\{.*\})\s*(?:```)?\s*$", txt, re.DOTALL)
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


def _matches_glob_list(path: str, globs: list[str]) -> bool:
    """fnmatch a path against a list of globs. A bare token (no slash, no
    dot, no wildcard) is treated as a directory-component match — so the
    pattern `node_modules` excludes anything under any `node_modules/` dir,
    not just a file literally named `node_modules`."""
    if not globs:
        return False
    name = pathlib.Path(path).name
    parts = path.replace("\\", "/").split("/")
    for pat in globs:
        if "/" not in pat and "." not in pat and "*" not in pat:
            if pat in parts:
                return True
            continue
        if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(name, pat):
            return True
    return False


def _is_noise(path: str) -> bool:
    return _matches_glob_list(path, NOISE_PATTERNS)


def _read_exclude_globs(repo: str | None, branch: str | None) -> list[str]:
    """Read per-repo `.ai-collab/review-exclude.txt` at the given branch.
    Returns a list of non-blank, non-comment glob lines. Missing file,
    binary file, or any git error yields []. Reading via `git show` makes
    the exclusion list move with the feature branch — the coder AI commits
    its noise globs alongside the code, no out-of-band server config."""
    if not repo or not branch:
        return []
    r = _git(repo, "show", f"{branch}:.ai-collab/review-exclude.txt")
    if r.returncode != 0:
        return []  # file not present on this branch — normal case
    out: list[str] = []
    for line in r.stdout.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def _filter_diff(
    diff: str,
    only_globs: list[str],
    include_all: bool,
    extra_exclude_globs: list[str] | None = None,
) -> tuple[str, dict]:
    """Filter a unified diff to the files that should be reviewed.

    `extra_exclude_globs` is the per-repo `.ai-collab/review-exclude.txt`
    list. It only applies in the default-noise-filtering mode (when neither
    `only_globs` nor `include_all` is set). The noise check runs first, so
    `repo_excluded_*` counts only files that would have been kept otherwise
    — that gives the UI a meaningful "extra files filtered by this repo's
    own list" number, separate from the baseline noise count.
    """
    extra_exclude_globs = extra_exclude_globs or []
    chunks = _split_diff_by_file(diff)
    kept: list[tuple[str, str]] = []
    skipped: list[tuple[str, int]] = []
    repo_excluded: list[tuple[str, int]] = []
    for path, chunk in chunks:
        if only_globs:
            keep = any(
                fnmatch.fnmatch(path, g) or fnmatch.fnmatch(pathlib.Path(path).name, g)
                for g in only_globs
            )
            (kept if keep else skipped).append(
                (path, chunk) if keep else (path, len(chunk))
            )
            continue
        if include_all:
            kept.append((path, chunk))
            continue
        # Default mode: noise filter first, then per-repo extras.
        if _is_noise(path):
            skipped.append((path, len(chunk)))
        elif _matches_glob_list(path, extra_exclude_globs):
            repo_excluded.append((path, len(chunk)))
        else:
            kept.append((path, chunk))
    body = "".join(c for _, c in kept)
    stats = {
        "total_files": len(chunks),
        "kept_files": len(kept),
        # Backward-compatible totals — callers reading skipped_files expect
        # "everything that didn't make it in", regardless of reason.
        "skipped_files": len(skipped) + len(repo_excluded),
        "skipped_chars": sum(b for _, b in skipped) + sum(b for _, b in repo_excluded),
        "kept_paths": [p for p, _ in kept],
        "skipped_paths": [p for p, _ in skipped] + [p for p, _ in repo_excluded],
        # New: just the per-repo extras, for UI display and operator visibility.
        "repo_excluded_files": len(repo_excluded),
        "repo_excluded_paths": [p for p, _ in repo_excluded],
    }
    return body, stats


def _trim_payload_to_files(payload: str, kept_files: set[str]) -> tuple[str, dict]:
    """For the reconcile phase, replace each ```diff …``` block in the
    payload with just the file chunks whose paths are in `kept_files`.

    The reconciler is told to focus on disagreements; the initial reviews
    already cite line numbers, so the full diff is mostly dead weight at
    that point. We keep only the files the two reviewers actually pointed
    at — the reconciler can still fact-check those citations against the
    real diff.

    Returns (trimmed_payload, stats). Stats let the caller decide whether
    the trim was worth applying.
    """
    if not kept_files:
        return payload, {"applied": False, "reason": "no kept files"}

    total_files = 0
    kept_count = 0
    chars_before = 0
    chars_after = 0

    def _trim_block(m: re.Match) -> str:
        nonlocal total_files, kept_count, chars_before, chars_after
        body = m.group(1)
        chars_before += len(body)
        chunks = _split_diff_by_file(body)
        total_files += len(chunks)
        kept = [(p, c) for p, c in chunks if p in kept_files]
        kept_count += len(kept)
        if not kept:
            # No mentioned files in this block; drop it entirely with a
            # one-line note so the reconciler knows the diff was intentional
            # rather than missing.
            note = (
                "```diff\n"
                "(this diff section was elided — neither reviewer cited "
                "any file in it. Their reviews cover all flagged content.)\n"
                "```"
            )
            chars_after += len(note) - len("```diff\n\n```")
            return note
        new_body = "".join(c for _, c in kept)
        chars_after += len(new_body)
        return f"```diff\n{new_body}```"

    trimmed = re.sub(r"```diff\n(.*?)```", _trim_block, payload, flags=re.DOTALL)
    return trimmed, {
        "applied": True,
        "total_files": total_files,
        "kept_files": kept_count,
        "chars_before": chars_before,
        "chars_after": chars_after,
    }


def _findings_files(findings: list) -> set[str]:
    """Collect non-empty `file` paths from a structured findings list."""
    out: set[str] = set()
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        path = f.get("file")
        if isinstance(path, str) and path.strip():
            out.add(path.strip())
    return out


def _findings_from_block(block: dict | None) -> list:
    if isinstance(block, dict) and isinstance(block.get("findings"), list):
        return block["findings"]
    return []


# Reconcile is skipped (saving a full second Claude pass) when the two
# reviewers' verdicts match AND their findings overlap at least this much —
# i.e. they already agree, so there's nothing to reconcile. Jaccard over
# (file, severity) keys. Tunable but conservative: at/above this, the
# disagreement-resolution value of reconcile is near zero.
RECONCILE_SKIP_CONCORDANCE = 0.5


def _finding_concordance(claude_findings: list, codex_findings: list) -> float:
    """How much the two reviewers' findings overlap, 0..1 (Jaccard over
    (file-or-'<general>', severity) keys).

    1.0  = both found nothing, OR identical (file,severity) sets.
    0.0  = one found issues and the other found none (genuine divergence),
           or no shared keys at all.
    Coarse on purpose: it answers "do the reviewers broadly agree on WHERE
    and HOW SERIOUS the problems are?", which is exactly what decides whether
    a reconcile pass can add anything.
    """
    def keys(findings: list) -> set:
        out = set()
        for f in findings or []:
            if not isinstance(f, dict):
                continue
            path = f.get("file")
            path = path.strip() if isinstance(path, str) and path.strip() else "<general>"
            sev = str(f.get("severity") or "?").lower()
            out.add((path, sev))
        return out

    kc, kx = keys(claude_findings), keys(codex_findings)
    if not kc and not kx:
        return 1.0          # both clean → fully concordant
    if not kc or not kx:
        return 0.0          # one flagged, the other didn't → reconcile
    inter = len(kc & kx)
    union = len(kc | kx)
    return inter / union if union else 1.0


def _verdict_from_block_or_text(block: dict | None, review_md: str) -> str | None:
    if isinstance(block, dict) and block.get("verdict"):
        return str(block["verdict"]).lower()
    m = re.search(r"\*{0,2}Verdict:?\*{0,2}\s*`?([A-Za-z\-]+)`?", review_md)
    return m.group(1).lower() if m else None


def _format_findings_for_reconcile(label: str, findings: list) -> str:
    """Render a peer reviewer's findings as a numbered markdown list for the
    reconcile prompt. The #N IDs let the *other* reviewer reference them
    unambiguously in their `peer_assessment` JSON output.
    """
    if not findings:
        return f"# {label}'s findings\n_(none — they raised no concrete findings)_\n\n"
    out = [f"# {label}'s findings (numbered — use these IDs in peer_assessment)"]
    for i, f in enumerate(findings, start=1):
        if not isinstance(f, dict):
            out.append(f"- **#{i}** (malformed entry)")
            continue
        sev = f.get("severity") or "?"
        repo = (f.get("repo") or "").strip() if isinstance(f.get("repo"), str) else ""
        file = (f.get("file") or "").strip() if isinstance(f.get("file"), str) else ""
        lines_field = (f.get("lines") or "").strip() if isinstance(f.get("lines"), str) else ""
        loc_parts = [x for x in (repo, file) if x]
        loc = "/".join(loc_parts)
        if loc and lines_field:
            loc = f"{loc}:{lines_field}"
        title = f.get("title") or "(no title)"
        detail = (f.get("detail") or "").strip() if isinstance(f.get("detail"), str) else ""
        head = f"- **#{i}** [{sev}]"
        if loc:
            head += f" `{loc}`"
        head += f" — {title}"
        out.append(head)
        if detail:
            out.append(f"  {detail}")
    return "\n".join(out) + "\n\n"


def _extract_peer_block(text: str) -> tuple[str, list]:
    """Pull a trailing ```json {"peer_assessment": [...]}``` block off a
    reconcile response. Returns (clean_markdown, normalized_entries).
    Malformed / missing block yields an empty list — never raises.
    """
    txt = (text or "").strip()
    if not txt:
        return text, []
    # Same shape as _extract_findings_block — closing fence optional, since
    # Codex sometimes omits it. The trailing-block constraint is safe because
    # reconcile prose doesn't otherwise contain fenced JSON.
    m = re.search(r"```json\s*(\{.*\})\s*(?:```)?\s*$", txt, re.DOTALL)
    if not m:
        return text, []
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return text, []
    if not isinstance(data, dict):
        return text, []
    arr = data.get("peer_assessment")
    if not isinstance(arr, list):
        return text, []
    clean = txt[: m.start()].rstrip()
    return clean, _normalize_peer_entries(arr)


def _normalize_peer_entries(entries: list) -> list:
    """Coerce peer_assessment entries to a uniform shape. Drops entries that
    can't be coerced; clamps stance to {agree, disagree, unsure}."""
    valid = {"agree", "disagree", "unsure"}
    out: list[dict] = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        fid_raw = e.get("finding_id")
        if isinstance(fid_raw, bool):
            continue
        if isinstance(fid_raw, int):
            fid = fid_raw
        else:
            try:
                fid = int(str(fid_raw).strip())
            except (TypeError, ValueError):
                continue
        stance = str(e.get("stance") or "").strip().lower()
        if stance not in valid:
            stance = "unsure"
        note = e.get("note")
        note = note.strip() if isinstance(note, str) else ""
        out.append({"finding_id": fid, "stance": stance, "note": note})
    return out


def _resolve_diff_range(
    branch: str,
    base: str,
    since_commit: str | None,
    to_commit: str | None,
    narrow_to_recent_commits: bool,
) -> dict:
    """Resolve which commit range a review covers. Pure (no git calls) so
    load_code_input AND the caller-context paths can share one source of
    truth for the range.

    Returns {diff, log, end, kind}:
      - diff: the `git diff` argument for the line-level diff (and the
        caller-context analysis — they MUST match so the symbol graph sees
        the same changed lines the reviewer sees).
      - log:  the `git log` 2-dot range for the commit listing.
      - end:  the commit whose line frame the diff is in — used as the
        symbol-graph build ref (task #10's graph_ref fix).
      - kind: explicit_range | feature_up_to | narrow | full.

    Modes (task #9 + #13):
      explicit_range : since..to     — review commits A..B (exclude A, 2-dot)
      feature_up_to  : base...to     — feature as of commit B (3-dot)
      narrow         : since..branch — commits since prior review (2-dot)
      full           : base...branch — whole feature (3-dot, default)
    """
    if since_commit and to_commit:
        return {"diff": f"{since_commit}..{to_commit}",
                "log": f"{since_commit}..{to_commit}",
                "end": to_commit, "kind": "explicit_range"}
    if to_commit:
        return {"diff": f"{base}...{to_commit}",
                "log": f"{base}..{to_commit}",
                "end": to_commit, "kind": "feature_up_to"}
    if narrow_to_recent_commits and since_commit:
        return {"diff": f"{since_commit}..{branch}",
                "log": f"{since_commit}..{branch}",
                "end": branch, "kind": "narrow"}
    return {"diff": f"{base}...{branch}",
            "log": f"{base}..{branch}",
            "end": branch, "kind": "full"}


def load_code_input(
    file_path: str | None,
    diff_ref: str | None,
    branch: str | None,
    base: str,
    repo: str | None,
    only_globs: list[str] | None = None,
    include_all: bool = False,
    summary_only: bool = False,
    since_commit: str | None = None,
    to_commit: str | None = None,
    narrow_to_recent_commits: bool = False,
    max_chars: int | None = None,
) -> tuple[str, str]:
    """Return (label, content) for the review subject.

    `max_chars` overrides the diff size cap (default MAX_REVIEW_CHARS).
    Claude-only reviews pass the larger MAX_REVIEW_CHARS_CLAUDE_ONLY.

    Task #9 — commit-range review:
    - `since_commit` is the anchor SHA for "review only commits since X".
      Always recorded in the payload as context (so reviewers know iter-N's
      cut point).
    - `narrow_to_recent_commits` is the opt-in flag that ACTUALLY changes
      the diff range from `<base>...<branch>` (full feature) to
      `<since>..<branch>` (only the new commits). When false (default),
      the diff stays full and `since_commit` is used only for the
      annotation header.
    """
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

        # Validate since_commit / to_commit if given.
        def _verify_commit(c: str, what: str) -> None:
            rr = _git(repo, "rev-parse", "--verify", c)
            if rr.returncode != 0:
                sys.exit(f"{what} not found: {c}\n{rr.stderr.strip()}")

        if to_commit:
            _verify_commit(to_commit, "to_commit")
            # to_commit must be on the branch's history ("in a branch").
            rr = _git(repo, "merge-base", "--is-ancestor", to_commit, branch)
            if rr.returncode != 0:
                sys.exit(f"to_commit {to_commit} is not reachable from {branch}")
        # Effective end of the review range (to_commit caps it; else branch).
        _range_end_for_validation = to_commit or branch
        if since_commit:
            _verify_commit(since_commit, "since_commit")
            # since must be an ancestor of the range end, else A..B is nonsense.
            rr = _git(repo, "merge-base", "--is-ancestor",
                      since_commit, _range_end_for_validation)
            if rr.returncode != 0:
                tgt = f"to_commit {to_commit}" if to_commit else branch
                sys.exit(f"since_commit {since_commit} is not an ancestor of {tgt}")

        # Resolve the diff range (task #9 since_commit + task #13 to_commit).
        _rng = _resolve_diff_range(
            branch, base, since_commit, to_commit, narrow_to_recent_commits
        )
        diff_range_3dot = _rng["diff"]   # the actual diff (and --stat) range
        range_kind = _rng["kind"]
        # explicit_range and narrow are SUB-RANGES of the branch — code
        # outside the range isn't visible, so the reviewer needs the
        # cross-range warning. feature_up_to and full start from base, so
        # nothing earlier is hidden.
        range_desc_narrow = range_kind in ("explicit_range", "narrow")

        log = _git(repo, "log", "--pretty=format:%h %s", _rng["log"])
        if log.returncode != 0:
            sys.exit(f"git log failed: {log.stderr.strip()}")
        commits = log.stdout.strip() or "(no commits in range)"

        # Stage-1 annotation: when since_commit is set but we're showing the
        # FULL feature diff (not a sub-range), list the new commits since the
        # prior anchor so reviewers know where to focus.
        new_commits_since_prior = ""
        if since_commit and range_kind == "full":
            r = _git(repo, "log", "--pretty=format:%h %s",
                     f"{since_commit}..{branch}")
            if r.returncode == 0:
                new_commits_since_prior = r.stdout.strip()

        stat = _git(repo, "diff", "--stat", diff_range_3dot)
        if stat.returncode != 0:
            sys.exit(f"git diff --stat failed: {stat.stderr.strip()}")

        # Human-readable scope descriptor, shared by the summary note, the
        # label suffix, and the branch-section header.
        if range_kind == "explicit_range":
            scope_desc = (f"explicit commit range `{since_commit[:12]}..{to_commit[:12]}` "
                          f"on branch `{branch}`")
        elif range_kind == "feature_up_to":
            scope_desc = f"branch `{branch}` as of commit `{to_commit[:12]}` (vs base `{base}`)"
        elif range_kind == "narrow":
            scope_desc = f"`{branch}` narrowed to commits since `{since_commit[:12]}`"
        else:
            scope_desc = f"`{branch}` compared to base `{base}`"

        if summary_only:
            label = f"branch {branch} vs {base} (summary)"
            payload = (
                f"## Scope\n{scope_desc}\n\n"
                f"## Commits on this branch (oldest first)\n"
                f"```\n{commits}\n```\n\n"
                f"## File-level changes\n"
                f"```\n{stat.stdout.strip()}\n```\n\n"
                f"## Note\nNo line-level diff was provided. Review the design and "
                f"scope from the commits and file list; flag risks you'd want to verify "
                f"by reading the diff."
            )
            return (label, payload)

        diff = _git(repo, "diff", diff_range_3dot)
        if diff.returncode != 0:
            sys.exit(f"git diff failed: {diff.stderr.strip()}")
        if not diff.stdout.strip():
            sys.exit(f"No changes in range {diff_range_3dot} — nothing to review.")

        raw_chars = len(diff.stdout)
        # Per-repo additive exclusion list — committed by the coder AI to
        # the feature branch at .ai-collab/review-exclude.txt. Only applies
        # in default-filter mode; `only`/`include_all` are explicit overrides
        # that bypass it.
        repo_excludes = (
            _read_exclude_globs(repo, branch)
            if not only_globs and not include_all
            else []
        )
        filtered, fstats = _filter_diff(
            diff.stdout, only_globs or [], include_all, repo_excludes
        )
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
        if fstats.get("repo_excluded_files"):
            preview = ", ".join(fstats["repo_excluded_paths"][:5])
            extra = (
                ""
                if fstats["repo_excluded_files"] <= 5
                else f", +{fstats['repo_excluded_files'] - 5} more"
            )
            info(
                f"excluded by .ai-collab/review-exclude.txt "
                f"({len(repo_excludes)} globs): {preview}{extra}"
            )

        cap = max_chars if max_chars else MAX_REVIEW_CHARS
        if len(filtered) > cap:
            kept_preview = "\n  ".join(fstats["kept_paths"][:30])
            extra = "" if fstats["kept_files"] <= 30 else f"\n  ... +{fstats['kept_files'] - 30} more"
            sys.exit(
                f"Diff is still {len(filtered):,} chars after filtering "
                f"(raw was {raw_chars:,}, cap is {cap:,}).\n\n"
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
        if range_kind == "explicit_range":
            label += f" — range {since_commit[:12]}..{to_commit[:12]}"
        elif range_kind == "feature_up_to":
            label += f" — up to {to_commit[:12]}"
        elif range_kind == "narrow":
            label += f" — NARROWED to commits since {since_commit[:12]}"

        # Branch-section header per range kind. explicit_range and narrow
        # carry the "REVIEW SCOPE IS A SUBSET" warning (code outside the
        # range isn't visible); the marker text is what _run_review_pipeline
        # keys off to add the cross-range reviewer instruction.
        if range_kind == "explicit_range":
            branch_section = (
                f"## Scope\n**EXPLICIT COMMIT RANGE — REVIEW SCOPE IS A SUBSET.** "
                f"Reviewing commits `{since_commit[:12]}..{to_commit[:12]}` on branch "
                f"`{branch}` (base `{base}`). The diff below covers ONLY this range — "
                f"commits before `{since_commit[:12]}` and after `{to_commit[:12]}` are "
                f"NOT visible. See the task-instructions section for the implications.\n\n"
            )
        elif range_kind == "feature_up_to":
            branch_section = (
                f"## Scope\nReviewing branch `{branch}` **as of commit "
                f"`{to_commit[:12]}`** (diff vs base `{base}`). Commits after "
                f"`{to_commit[:12]}` are not included in this review.\n\n"
            )
        elif range_kind == "narrow":
            branch_section = (
                f"## Branch\n`{branch}` is on top of base `{base}`. "
                f"**NARROWED REVIEW SCOPE:** the diff below covers only "
                f"commits since `{since_commit[:12]}` (the prior review's "
                f"reviewed SHA), NOT the full feature. Earlier commits in "
                f"this feature branch are NOT visible in the diff. See the "
                f"task-instructions section for the implications.\n\n"
            )
        else:
            branch_section = f"## Branch\n`{branch}` compared to base `{base}`\n\n"

        # Stage-1 annotation block: when since_commit is set but we're NOT
        # narrowing, list the new commits since the prior review as a focus
        # hint. The full diff is still shown; the reviewer just knows where
        # to concentrate attention.
        focus_block = ""
        if since_commit and not range_desc_narrow and new_commits_since_prior:
            focus_block = (
                f"## Commits since prior review (focus here)\n\n"
                f"Prior reviewed SHA: `{since_commit[:12]}` &nbsp;·&nbsp; "
                f"Current HEAD: `{branch}`\n\n"
                f"The full feature diff is shown below, but THESE are the "
                f"commits added since the prior review approved at "
                f"`{since_commit[:12]}`:\n\n"
                f"```\n{new_commits_since_prior}\n```\n\n"
                f"Concentrate scrutiny on these new commits. Earlier commits "
                f"in the same feature branch were reviewed in the prior "
                f"iteration — only re-flag them if a new commit changes "
                f"something they depended on.\n\n"
            )

        payload = (
            f"{branch_section}"
            f"## Commits on this branch (oldest first)\n"
            f"```\n{commits}\n```\n\n"
            f"## File-level changes\n"
            f"```\n{stat.stdout.strip()}\n```\n\n"
            f"{focus_block}"
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
_SAFE_FEATURE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _load_handoff(
    branch: str | None,
    repo: str | None,
    handoff_path: str | None,
    no_handoff: bool,
    feature: str | None = None,
) -> tuple[str, str] | None:
    """Return (source_label, content) if a handoff was found, else None.

    Discovery order when no explicit handoff_path is given:
      1. .ai-collab/handoffs/<feature>.md   (per-feature — preferred)
      2. HANDOFF.md / .ai-collab/HANDOFF.md / docs/HANDOFF.md  (legacy fallback)

    Per-feature files prevent a stale handoff from one feature being
    inherited by the next via a reused branch.
    """
    if no_handoff or not branch:
        return None
    if handoff_path:
        candidates = [handoff_path]
    else:
        candidates = []
        if feature and _SAFE_FEATURE_RE.match(feature):
            candidates.append(f".ai-collab/handoffs/{feature}.md")
        candidates += HANDOFF_AUTO_PATHS
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
    tests_future: Future | None = None,
    caller_context_md: str = "",
    reviewers: list[str] | None = None,
) -> dict:
    """Two-phase review pipeline shared by the CLI and the HTTP server.

    `reviewers` selects which models review (default both Claude + Codex).
    Pass ["claude"] for a Claude-only review (used for big diffs that exceed
    Codex's context window). With a single reviewer there's no second opinion
    to reconcile against, so the reconcile phase + disagreement matrix are
    skipped and the single phase-1 review is final.

    `tests_future`, if set, is awaited just before the reconcile phase. This
    lets the server run tests in the background while the initial reviews are
    already in flight — the reconcile phase still sees the results, but the
    review phase no longer waits on tests. When set, `test_results_by_repo`
    is ignored and reviewers don't see test results during the initial pass;
    the reconcile prompt picks them up instead.

    `caller_context_md`, if non-empty, is the symbol-graph-derived
    "Potentially impacted callers" section (task #10). It's injected between
    the diff and the task instructions in the phase-1 prompt only —
    reconcile reviewers don't need to re-discover callers, they're focused
    on resolving disagreements about findings already raised.
    """
    cb = progress_cb if callable(progress_cb) else (lambda _m: None)
    # Normalize reviewers: keep only known reviewers, preserve order, default
    # to both. Empty/garbage falls back to both so a bad request still runs.
    reviewers = [r for r in (reviewers or []) if r in ("claude", "codex")] or ["claude", "codex"]
    run_claude = "claude" in reviewers
    run_codex = "codex" in reviewers
    single_reviewer = len(reviewers) < 2
    context_block = f"# Context\n{extra_context}\n\n" if extra_context else ""

    prior_block = _build_prior_iteration_section(prior_result)
    if prior_block:
        prior_block = prior_block + "\n\n"

    # Caller-context: lives in its own section between the diff and the task
    # instructions. Trailing blank line keeps the prompt visually clean.
    caller_block = f"{caller_context_md.rstrip()}\n\n" if caller_context_md else ""

    # Sub-range detection (task #9 narrow + task #13 explicit range).
    # load_code_input stamps a marker into the payload when the diff covers
    # only a SUBSET of the branch (narrow-since-prior, or an explicit
    # from..to range). Either way the reviewer can't see code outside the
    # range and needs the cross-range caveat. Detect from the marker rather
    # than threading another parameter — scope is a payload-shape thing.
    narrow_mode_active = (
        "NARROWED REVIEW SCOPE" in payload
        or "REVIEW SCOPE IS A SUBSET" in payload
    )

    # Tests-in-parallel mode: defer the test block until reconcile so the
    # initial reviews can start immediately without waiting for `next build`.
    tests_deferred = tests_future is not None
    if tests_deferred:
        test_block = ""  # filled in before reconcile
    else:
        test_block = _build_test_section(test_results_by_repo)
        if test_block:
            test_block = test_block + "\n\n"

    initial_msg = (
        f"{context_block}"
        f"{prior_block}"
        f"{test_block}"
        f"# Code under review ({label})\n\n"
        f"{payload}\n\n"
        f"{caller_block}"
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
        + (
            " The 'Potentially impacted callers' section lists places elsewhere in the "
            "codebase that call symbols changed in the diff (mechanically extracted via "
            "a symbol graph). Use it to verify the diff doesn't break callers you wouldn't "
            "otherwise see. Direct callers deserve high scrutiny; indirect callers may or "
            "may not be affected depending on whether the intermediate function fully "
            "encapsulates the changed contract — verify case-by-case. If you can't tell "
            "from the snippet, flag it as a finding asking the implementer to verify "
            "manually rather than silently approving."
            if caller_block else ""
        )
        + (
            " The diff has been NARROWED to only the commits added since the prior "
            "review iteration — earlier commits in the same feature branch are NOT in "
            "the diff you see. You CANNOT detect cross-iteration regressions where a "
            "new commit breaks code touched in an earlier commit (because the earlier "
            "code isn't visible). If you spot any contract / interface / nullability / "
            "exception-shape change in the new commits, raise a finding asking the "
            "implementer to manually verify it against earlier-iter files that depended "
            "on the old contract — don't silently approve. Findings within the visible "
            "diff retain full scrutiny."
            if narrow_mode_active else ""
        )
        + "\n\n# Machine-readable findings\n"
        "After your markdown review, append a fenced ```json code block — and NOTHING "
        "after it — matching this exact shape:\n"
        '{"verdict": "approve|request-changes|blocking", "findings": ['
        '{"severity": "bug|concern|nit", "repo": "<repo name or null>", '
        '"file": "<path or null>", "lines": "<range or null>", '
        '"title": "<one-line summary>", "detail": "<short explanation>", '
        '"tier": "A|B|C|null"}]}\n'
        "The findings array must mirror the issues in your markdown review — one entry "
        "per concrete issue. If you found nothing in a category, simply omit it. The "
        "markdown review above the block stays the human-readable version; the JSON is "
        "the machine copy.\n\n"
        "## On the `tier` field — fix-complexity routing\n"
        "The implementer runs on a strong model by default; some findings can be "
        "delegated to a cheaper model, some can't, some need a human decision. Suggest "
        "a tier per finding so the implementer's router has a hint. Tier is not about "
        "your confidence in the finding — only about the *fix* it will require:\n"
        "- **A** (Sonnet-OK): mechanical, locally-scoped, one obvious correct answer. "
        "Rename, missing null check on an obvious nullable, typo, missing index/FK on "
        "an obvious column, pageSize clamp, hard-coded i18n string, dedupe a helper. "
        "If a cheap model got this wrong, the next review pass would catch it cheaply.\n"
        "- **B** (needs the strong model): high-judgment, cross-cutting, or design-"
        "bearing. Concurrency / race fixes, isolation-level decisions, cross-repo "
        "contract changes that must move in lockstep, auth / permission model changes, "
        "migration design with backfill or production-data invariants, architectural "
        "refactors. A cheap-model attempt could land syntactically clean but "
        "semantically wrong in a way the next review wouldn't catch.\n"
        "- **C** (ask the human): the finding requires a *decision*, not a *fix*. The "
        "right scope is unclear, the finding contradicts an explicit design decision in "
        "the handoff, or it's a cost/benefit call the implementer can't make alone.\n"
        "- **null**: you're unsure. Omit the field or set it to null — the implementer "
        "will classify it from its own rubric.\n"
        "Default to suggesting *up*, not *down*: when in doubt between A and B, "
        "say B. The implementer's structural rules (anything both reviewers tag "
        "`blocking`, anything touching concurrency or cross-repo contracts) will "
        "override a reviewer-suggested A regardless, so under-tiering costs more "
        "than over-tiering."
    )

    history: list[dict] = [{"speaker": "system", "text": f"Subject: {label}\n\n{payload}"}]
    usage_acc = {"claude": dict(EMPTY_USAGE), "codex": dict(EMPTY_USAGE)}

    def _add(who: str, u: dict) -> None:
        for k in ("input_tokens", "output_tokens", "usd"):
            usage_acc[who][k] += u.get(k, 0) or 0

    # Phase 1 — independent reviews. The selected reviewers run concurrently.
    # Each CLI call is bounded by MODEL_CALL_TIMEOUT_S so there's no risk of
    # one wedging the other indefinitely.
    who = " + ".join(r.capitalize() for r in reviewers)
    info(f"phase 1/2 — independent reviews ({who})")
    cb(f"phase 1/2 · {who} reviewing")
    # Defaults for any reviewer that doesn't run this job.
    claude_review, claude_block, u_c = "", None, dict(EMPTY_USAGE)
    codex_review, codex_block, u_x = "", None, dict(EMPTY_USAGE)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="review") as ex:
        fut_c = ex.submit(call_claude, roles["claude"], initial_msg) if run_claude else None
        fut_x = ex.submit(call_codex, roles["codex"], initial_msg) if run_codex else None
        if fut_c is not None:
            claude_review, u_c = fut_c.result()
        if fut_x is not None:
            codex_review, u_x = fut_x.result()
    if run_claude:
        _add("claude", u_c)
        claude_review, claude_block = _extract_findings_block(claude_review)
        speak("claude", claude_review)
        history.append({"speaker": "claude (initial)", "text": claude_review})
    if run_codex:
        _add("codex", u_x)
        codex_review, codex_block = _extract_findings_block(codex_review)
        speak("codex", codex_review)
        history.append({"speaker": "codex (initial)", "text": codex_review})

    # If tests were deferred, await them now so the reconcile prompt can
    # include them. We block here at the boundary between phase 1 and 2 —
    # tests have had the entire review-phase wall time to make progress.
    if tests_deferred:
        cb("awaiting tests (tests ran in parallel with reviews)")
        try:
            test_results_by_repo = tests_future.result()
        except Exception as e:
            info(f"tests_future raised: {type(e).__name__}: {e}")
            test_results_by_repo = None
        test_block = _build_test_section(test_results_by_repo)
        if test_block:
            test_block = test_block + "\n\n"

    # --- Skip reconcile when there's nothing to reconcile ----------------
    # If both reviewers approved with zero findings AND no failing tests,
    # there are no disagreements to resolve. Reconcile is pure overhead in
    # that case — drop the two model calls and synthesize a trivial summary.
    claude_verdict_p1 = _verdict_from_block_or_text(claude_block, claude_review)
    codex_verdict_p1 = _verdict_from_block_or_text(codex_block, codex_review)
    claude_findings_p1 = _findings_from_block(claude_block)
    codex_findings_p1 = _findings_from_block(codex_block)
    test_failures = any(
        (tr or {}).get("status") not in ("passed", "skipped", None)
        for tr in (test_results_by_repo or {}).values()
    )
    # Reconcile-skip decision. Reconcile's only job is to resolve
    # DISAGREEMENTS between the two reviewers. When they already agree there's
    # nothing to reconcile, so we drop the second (expensive) Claude pass.
    # "Agree" = matching verdicts AND high finding concordance, with no
    # unflagged test failures to surface. This subsumes the old both-approve-
    # clean case (empty findings → concordance 1.0).
    verdicts_match = (
        claude_verdict_p1 is not None and claude_verdict_p1 == codex_verdict_p1
    )
    concordance = _finding_concordance(claude_findings_p1, codex_findings_p1)
    both_clean = (
        claude_verdict_p1 == "approve"
        and codex_verdict_p1 == "approve"
        and not claude_findings_p1
        and not codex_findings_p1
        and not test_failures
    )
    high_concordance_skip = (
        not single_reviewer
        and verdicts_match
        and not test_failures
        and concordance >= RECONCILE_SKIP_CONCORDANCE
    )
    if single_reviewer:
        # One reviewer — its phase-1 review IS the final review. There's no
        # second opinion to reconcile against, so skip phase 2 entirely.
        sole = reviewers[0]
        info(f"phase 2/2 — skipped (single-reviewer mode: {sole} only)")
        cb(f"phase 2/2 · skipped ({sole}-only review)")
        skip_note = (
            f"Single-reviewer review ({sole} only) — no second reviewer to "
            f"reconcile against. The review above is final. (Used for large "
            f"diffs that exceed the other reviewer's context window.)"
        )
        claude_reconcile = skip_note if run_claude else ""
        codex_reconcile = skip_note if run_codex else ""
        claude_peer_assessment: list = []
        codex_peer_assessment: list = []
        if run_claude:
            history.append({"speaker": "claude (final)", "text": claude_reconcile})
        if run_codex:
            history.append({"speaker": "codex (final)", "text": codex_reconcile})
        cb("finalizing")
    elif high_concordance_skip:
        pct = round(concordance * 100)
        if both_clean:
            info("phase 2/2 — skipped (both approved, no findings, tests clean)")
            cb("phase 2/2 · skipped (clean review)")
            skip_note = (
                "Reconcile skipped — both reviewers returned `approve` with no "
                "findings and Tier-1 tests passed. Nothing to reconcile."
            )
        else:
            info(f"phase 2/2 — skipped (reviewers concur: verdict "
                 f"'{claude_verdict_p1}', {pct}% finding overlap)")
            cb("phase 2/2 · skipped (reviewers concur)")
            skip_note = (
                f"Reconcile skipped — both reviewers independently reached the "
                f"same verdict (`{claude_verdict_p1}`) with {pct}% finding "
                f"overlap (file+severity). They agree on where and how serious "
                f"the issues are, so there was no disagreement to reconcile. "
                f"Both reviews' findings stand as-is; dedupe them downstream."
            )
        claude_reconcile = skip_note
        codex_reconcile = skip_note
        # No reconcile pass → no model-generated peer assessment.
        claude_peer_assessment: list = []
        codex_peer_assessment: list = []
        history.append({"speaker": "claude (reconcile)", "text": claude_reconcile})
        history.append({"speaker": "codex (reconcile)", "text": codex_reconcile})
        cb("finalizing")
    else:
        # --- Surgical reconcile payload --------------------------------------
        # Trim the diff to only files that either reviewer's findings cite.
        # The reconciler can still fact-check contested citations against the
        # real diff, but doesn't re-pay for the whole branch context.
        # Safety net: if any finding lacks a specific file (general concerns
        # like "no test coverage"), fall back to the full diff so the
        # reconciler keeps full context for that general claim.
        cited_files = _findings_files(claude_findings_p1) | _findings_files(codex_findings_p1)
        any_general = any(
            (not isinstance(f, dict)) or not (isinstance(f.get("file"), str) and f["file"].strip())
            for f in (claude_findings_p1 + codex_findings_p1)
        )
        if cited_files and not any_general:
            trimmed_payload, trim_stats = _trim_payload_to_files(payload, cited_files)
            if trim_stats.get("applied") and trim_stats.get("chars_after", 0) < trim_stats.get("chars_before", 0):
                info(
                    f"reconcile diff trimmed: {trim_stats['kept_files']}/{trim_stats['total_files']} "
                    f"files kept, {trim_stats['chars_before']:,} → {trim_stats['chars_after']:,} chars"
                )
                reconcile_payload = trimmed_payload
                trim_note = (
                    " The diff below has been trimmed to only the files cited by either "
                    "reviewer's findings — the reconciler's job is to verify those "
                    "citations and resolve disagreements, not re-scan the whole branch. "
                    "If you need to verify a citation against a file that's elided, say "
                    "so in your output rather than guessing."
                )
            else:
                reconcile_payload = payload
                trim_note = ""
        else:
            reconcile_payload = payload
            trim_note = ""

        info("phase 2/2 — reconciling disagreements (Claude + Codex in parallel)")
        tests_in_reconcile_note = (
            " The 'Test results' section above was not visible to the reviewers "
            "during their initial pass (tests ran in parallel with reviews). If "
            "any repo's tests failed and NEITHER reviewer flagged it, surface "
            "that failure as a NEW finding in your reconciliation — failing "
            "Tier-1 tests are a hard block regardless of how the diff looks."
            if (tests_deferred and test_block) else ""
        )
        claude_findings_section = _format_findings_for_reconcile(
            "Reviewer A (Claude)", claude_findings_p1
        )
        codex_findings_section = _format_findings_for_reconcile(
            "Reviewer B (Codex)", codex_findings_p1
        )

        # Disagreement-matrix instructions. Same message goes to both
        # reviewers; the role prompt (system message) tells each model who
        # they are, and the wording below tells each which numbered list to
        # assess. This keeps the prompt cacheable across the two parallel
        # reconcile calls.
        peer_instruction = (
            "\n\n## Peer assessment (disagreement matrix)\n"
            "In addition to your reconcile prose above, you must end your "
            "response with a fenced ```json code block — and NOTHING after it — "
            "matching this exact shape:\n"
            '{"peer_assessment": [{"finding_id": <N>, "stance": '
            '"agree|disagree|unsure", "note": "<short reason>"}]}\n\n'
            "Rules:\n"
            "- If you are **Reviewer A (Claude)**, the `finding_id` values "
            "refer to **Reviewer B (Codex)'s** numbered findings above. "
            "Do NOT grade your own findings.\n"
            "- If you are **Reviewer B (Codex)**, the `finding_id` values "
            "refer to **Reviewer A (Claude)'s** numbered findings above. "
            "Do NOT grade your own findings.\n"
            "- Cover EVERY finding the other reviewer raised. One entry per "
            "finding, in order.\n"
            "- Use `disagree` only when you can name a concrete reason "
            "(false positive, misread of the code, contradicted by the diff). "
            "`unsure` is fine if you can't verify against the trimmed diff.\n"
            "- `note` is one short sentence — your rationale. Empty string is "
            "OK for plain `agree`.\n"
            "- If the other reviewer raised zero findings, emit "
            '`{"peer_assessment": []}` and nothing else.'
        )

        reconcile_msg = (
            f"{context_block}"
            f"{prior_block}"
            f"{test_block}"
            f"# Code under review ({label})\n\n{reconcile_payload}\n\n"
            f"# Reviewer A — Claude's review\n{claude_review}\n\n"
            f"{claude_findings_section}"
            f"# Reviewer B — Codex's review\n{codex_review}\n\n"
            f"{codex_findings_section}"
            f"# Your task\n"
            f"You have just seen both independent reviews above. Focus ONLY on points where the "
            f"two reviews disagree, or where one reviewer caught something the other missed. "
            f"For each such point, take a clear position: which reviewer is right (or are both "
            f"partially right, or both wrong)? Be brief — one short paragraph per disagreement. "
            f"If both reviews fully agree, say so in one sentence and stop. Do not re-summarize "
            f"points both reviews already agree on."
            + trim_note
            + tests_in_reconcile_note
            + peer_instruction
        )

        cb("phase 2/2 · Claude+Codex reconciling in parallel")
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="reconcile") as ex:
            fut_c = ex.submit(call_claude, roles["claude"], reconcile_msg)
            fut_x = ex.submit(call_codex, roles["codex"], reconcile_msg)
            claude_reconcile, u_c = fut_c.result()
            codex_reconcile, u_x = fut_x.result()
        _add("claude", u_c)
        _add("codex", u_x)
        # Peel the peer-assessment JSON off each reconcile output before
        # storing/displaying. claude_peer_assessment is Claude's stances on
        # Codex's findings (and vice versa).
        claude_reconcile, claude_peer_assessment = _extract_peer_block(claude_reconcile)
        codex_reconcile, codex_peer_assessment = _extract_peer_block(codex_reconcile)
        info(
            f"peer assessments: claude→codex {len(claude_peer_assessment)} entries, "
            f"codex→claude {len(codex_peer_assessment)} entries"
        )
        speak("claude", claude_reconcile)
        speak("codex", codex_reconcile)
        history.append({"speaker": "claude (reconcile)", "text": claude_reconcile})
        history.append({"speaker": "codex (reconcile)", "text": codex_reconcile})
        cb("finalizing")

    transcript_path = save_transcript("review", label, history)
    transcript_md = transcript_path.read_text(encoding="utf-8")

    claude_findings = _findings_from_block(claude_block)
    codex_findings = _findings_from_block(codex_block)
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
        # Disagreement matrix: each reviewer's stance on the OTHER's
        # findings. `claude_peer_assessment` = Claude's stances on Codex's
        # findings; `codex_peer_assessment` = Codex's stances on Claude's
        # findings. `finding_id` refers to the index (1-based) into the
        # corresponding peer's findings array.
        "claude_peer_assessment": claude_peer_assessment,
        "codex_peer_assessment": codex_peer_assessment,
        "claude_verdict": _verdict_from_block_or_text(claude_block, claude_review) if run_claude else None,
        "codex_verdict": _verdict_from_block_or_text(codex_block, codex_review) if run_codex else None,
        # Which reviewers actually ran (default both). Consumers/UI use this
        # to distinguish "reviewer didn't run" from "reviewer found nothing".
        "reviewers": reviewers,
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
    since_commit: str | None = None,
    narrow_to_recent_commits: bool = False,
    max_chars: int | None = None,
) -> tuple[str, str, dict, str]:
    """Build the payload for a single (repo, branch, base) triple.

    Returns (short_label, payload_markdown, fstats, raw_diff_text).
    The raw diff text is what feeds into caller-context analysis
    (caller_lookup.build_caller_context) — empty string in summary_only
    mode where we deliberately don't compute a line-level diff.
    Caller is responsible for the size cap when combining multiple repos.

    Task #9 — when `narrow_to_recent_commits=True` AND `since_commit` is
    set, the diff scope narrows from base...branch to since..branch.
    """
    label, payload = load_code_input(
        None, None, branch, base, repo,
        only_globs=only_globs, include_all=include_all, summary_only=summary_only,
        since_commit=since_commit,
        narrow_to_recent_commits=narrow_to_recent_commits,
        max_chars=max_chars,
    )
    # Raw diff for caller-context analysis. Must use the SAME range as
    # load_code_input so the symbol-graph analysis sees the same set of
    # changed lines the reviewer sees. (Multi-repo doesn't use to_commit —
    # it's single-repo-only — so to_commit is None here.)
    rng = _resolve_diff_range(branch, base, since_commit, None, narrow_to_recent_commits)
    raw_diff = ""
    graph_ref = rng["end"]
    if not summary_only:
        r = _git(repo, "diff", rng["diff"])
        if r.returncode == 0:
            raw_diff = r.stdout
    return label, payload, {}, raw_diff, graph_ref


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
    feature: str | None = None,
    tests_future: Future | None = None,
    caller_depth: int = 2,
    prior_head_shas: dict | None = None,
    since_commit: str | None = None,
    to_commit: str | None = None,
    narrow_to_recent_commits: bool = False,
    reviewers: list[str] | None = None,
) -> dict:
    """Run a single review whose subject is changes across multiple repos.

    Each entry in `repos` is {repo: <local path>, repo_url: <url>, branch: <name>,
    base: <name>}. The server is responsible for cloning/fetching before calling.

    `reviewers` (default both) selects which models review. ["claude"] runs a
    Claude-only review with a larger size cap — for big multi-repo features
    that exceed Codex's context window.

    `to_commit` (task #13) is single-repo-only — an explicit end SHA is
    ambiguous across multiple repos — so it is IGNORED here. Use a
    single-repo submission to review an explicit from..to range.

    `caller_depth` controls the symbol-graph caller-context analysis
    (task #10): 1 = direct callers only, 2 = one hop of indirect callers
    tagged separately. Depth>1 is gated by the server-side approval flow.
    """
    if not repos:
        raise RuntimeError("repos must be non-empty")

    roles = load_roles("review")
    if "claude" not in roles or "codex" not in roles:
        raise RuntimeError("prompts/review.md must contain '### CLAUDE' and '### CODEX' sections")

    # Effective size cap: Claude-only reviews aren't bound by Codex's window,
    # so they get the larger cap (big multi-repo features in one pass).
    reviewers_norm = [r for r in (reviewers or []) if r in ("claude", "codex")] or ["claude", "codex"]
    eff_cap = MAX_REVIEW_CHARS_CLAUDE_ONLY if reviewers_norm == ["claude"] else MAX_REVIEW_CHARS

    sections: list[str] = []
    caller_context_sections: list[str] = []
    caller_context_meta: list[dict] = []
    handoff_source: str | None = None
    handoff_content: str | None = None
    repo_labels: list[str] = []

    # Per-repo since_commit anchor: explicit override wins; otherwise
    # auto-resolve from prior_head_shas (which the server filled from
    # previous_job_id). Auto-anchor enables stage-1 annotation by default.
    prior_head_shas = prior_head_shas or {}

    for i, r in enumerate(repos, start=1):
        repo_path = r["repo"]
        branch = r["branch"]
        base = r["base"]
        repo_id = r.get("repo_url") or repo_path
        info(f"loading repo {i}/{len(repos)}: {repo_id} ({branch} vs {base})")

        # Per-repo since-anchor. Explicit since_commit only makes sense
        # for single-repo submissions; for multi-repo it's ambiguous which
        # repo it refers to, so we ignore it here and rely on auto-anchor
        # via prior_head_shas (which has one SHA per repo).
        per_repo_since = prior_head_shas.get(repo_id)
        if per_repo_since:
            info(f"  since anchor: {per_repo_since[:12]} (from prior iteration)")

        sub_label, sub_payload, _, raw_diff, sub_graph_ref = _branch_payload(
            repo=repo_path, branch=branch, base=base,
            only_globs=only_globs or [], include_all=include_all,
            summary_only=summary_only,
            since_commit=per_repo_since,
            narrow_to_recent_commits=narrow_to_recent_commits and bool(per_repo_since),
            max_chars=eff_cap,
        )
        repo_labels.append(repo_id)
        sections.append(
            f"# Repo {i}: `{repo_id}`\n\n"
            f"_({sub_label})_\n\n"
            f"{sub_payload}"
        )

        # Per-repo caller-context. Skip on summary-only (no diff to analyze)
        # and silently swallow any failures — caller-context is a best-effort
        # enhancement, never a blocker.
        if raw_diff and not summary_only:
            try:
                from caller_lookup import build_caller_context
                # graph_ref=branch builds the symbol graph from the feature
                # branch so def line numbers align with the diff hunk lines
                # (option A — avoids origin/main line-drift false negatives).
                cc = build_caller_context(
                    repo_path, raw_diff, depth=caller_depth, graph_ref=sub_graph_ref,
                )
                if cc.get("section_md"):
                    caller_context_sections.append(
                        f"### Caller context for `{repo_id}`\n\n{cc['section_md']}"
                    )
                caller_context_meta.append({"repo": repo_id, **{
                    k: cc[k] for k in (
                        "depth_used", "changed_symbols", "callers_found",
                        "callers_included", "callers_truncated", "bytes", "note"
                    )
                }})
                info(f"  caller-context: {cc['note']}")
            except Exception as e:
                info(f"  caller-context skipped: {type(e).__name__}: {e}")

        # First handoff found wins.
        if handoff_content is None:
            h = _load_handoff(branch, repo_path, handoff_path, no_handoff, feature)
            if h:
                handoff_source = f"{repo_id} @ {h[0]}"
                handoff_content = h[1]

    combined_payload = "\n\n---\n\n".join(sections)
    label = f"multi-repo feature across {len(repos)} repos: " + ", ".join(repo_labels)

    if len(combined_payload) > eff_cap:
        hint = ("Use 'only' globs, set 'summary_only': true, or split into separate reviews."
                if reviewers_norm != ["claude"]
                else "Even for a Claude-only review this exceeds the cap; split or use 'only' globs.")
        raise RuntimeError(
            f"Combined diff is {len(combined_payload):,} chars (cap {eff_cap:,}). {hint}"
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

    caller_context_md = "\n\n---\n\n".join(caller_context_sections).strip()
    if caller_context_md:
        info(f"caller-context total: {sum(m['callers_included'] for m in caller_context_meta)} "
             f"caller(s) across {len(caller_context_meta)} repo(s), "
             f"{len(caller_context_md):,} chars")

    info(f"mode=review (multi-repo)  total payload {len(combined_payload):,} chars")
    if prior_result:
        info("prior iteration context present — re-review mode")
    if test_results_by_repo:
        passed = sum(1 for r in test_results_by_repo.values() if r.get("status") == "passed")
        info(f"test results provided: {passed}/{len(test_results_by_repo)} repos passed")
    result = _run_review_pipeline(
        roles, label, combined_payload, extra_context,
        prior_result=prior_result, test_results_by_repo=test_results_by_repo,
        progress_cb=progress_cb, tests_future=tests_future,
        caller_context_md=caller_context_md,
        reviewers=reviewers_norm,
    )
    result["handoff_source"] = handoff_source
    # Persist the full handoff text so the UI can render it on the detail
    # page (collapsible "Submission" card). Stripped from the lean API
    # response — only the dashboard's ?full=true fetch needs it.
    result["handoff_content"] = handoff_content
    # Caller-context summary (task #10) — per-repo metadata + aggregate.
    result["caller_context"] = {
        "depth_used": caller_depth,
        "per_repo": caller_context_meta,
        "section_md": caller_context_md,
        "total_callers_included": sum(m["callers_included"] for m in caller_context_meta),
        "total_bytes": len(caller_context_md),
    }
    # Commit-range summary (task #9) — what range each repo was reviewed
    # at. The UI uses this to surface "reviewed since SHA X" on the detail
    # page; the AI consumers ignore it.
    cr_per_repo = [
        {
            "repo_url": r.get("repo_url"),
            "branch": r["branch"],
            "base": r["base"],
            "since_anchor": prior_head_shas.get(r.get("repo_url")),
            "narrowed": narrow_to_recent_commits and bool(
                prior_head_shas.get(r.get("repo_url"))
            ),
            # Task #11: narrow was asked for, but THIS repo had no anchor
            # (e.g. it was added to the feature after the prior iteration,
            # so the prior job recorded no SHA for it). Surfaced so the
            # human sees this repo silently fell back to full-diff.
            "narrow_fallback": narrow_to_recent_commits and not bool(
                prior_head_shas.get(r.get("repo_url"))
            ),
        }
        for r in repos
    ]
    def _short(url: str) -> str:
        name = (url or "").rstrip("/").rsplit("/", 1)[-1]
        return name[:-4] if name.endswith(".git") else name
    fallback_repos = [
        _short(p["repo_url"]) for p in cr_per_repo if p["narrow_fallback"]
    ]
    if to_commit:
        info("to_commit is ignored for multi-repo reviews (single-repo only)")
    result["commit_range"] = {
        "narrow_to_recent_commits": narrow_to_recent_commits,
        "explicit_since_commit": since_commit,
        "explicit_to_commit": None,  # ignored for multi-repo
        "per_repo": cr_per_repo,
        "narrow_fallback_reason": (
            f"narrow_to_recent_commits was requested, but {len(fallback_repos)} "
            f"repo(s) had no since-anchor from the prior iteration and were "
            f"reviewed at FULL diff scope: {', '.join(fallback_repos)}. "
            f"(They were likely added to the feature after the prior review.)"
            if fallback_repos else None
        ),
    }
    result["request"] = {
        "repos": [
            {"repo_url": r.get("repo_url"), "branch": r["branch"], "base": r["base"]}
            for r in repos
        ],
        "only_globs": only_globs or [],
        "include_all": include_all,
        "summary_only": summary_only,
        "caller_depth": caller_depth,
        "since_commit": since_commit,
        "to_commit": None,  # ignored for multi-repo
        "narrow_to_recent_commits": narrow_to_recent_commits,
        "reviewers": reviewers_norm,
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
    feature: str | None = None,
    tests_future: Future | None = None,
    caller_depth: int = 2,
    prior_head_shas: dict | None = None,
    since_commit: str | None = None,
    to_commit: str | None = None,
    narrow_to_recent_commits: bool = False,
    reviewers: list[str] | None = None,
) -> dict:
    """Server-friendly entrypoint. Runs a branch review and returns a structured result.

    Raises RuntimeError on validation/load failures (instead of sys.exit). The HTTP
    server catches these and reports them on the job record.

    `caller_depth` controls the symbol-graph caller-context analysis
    (task #10): 1 = direct callers only, 2 = one-hop indirect.

    `to_commit` (task #13) caps the review's END at an explicit commit.
    With since_commit it reviews the range since..to; alone it reviews the
    feature up to that commit (base...to).

    `reviewers` (default both) selects which models review. ["claude"] runs a
    Claude-only review with a larger size cap — for big diffs that exceed
    Codex's context window.
    """
    roles = load_roles("review")
    if "claude" not in roles or "codex" not in roles:
        raise RuntimeError("prompts/review.md must contain '### CLAUDE' and '### CODEX' sections")

    # Effective size cap: Claude-only gets the larger cap (Codex's window
    # doesn't bind when Codex isn't reviewing).
    reviewers_norm = [r for r in (reviewers or []) if r in ("claude", "codex")] or ["claude", "codex"]
    eff_cap = MAX_REVIEW_CHARS_CLAUDE_ONLY if reviewers_norm == ["claude"] else MAX_REVIEW_CHARS

    # Resolve since-anchor for stage-1 annotation / stage-2 narrowing
    # (task #9). Explicit since_commit wins; otherwise look up the prior
    # iteration's SHA (single-repo: this branch's repo_url isn't always
    # known here, so fall back to first-value-in-dict heuristic).
    effective_since = since_commit
    if not effective_since and prior_head_shas:
        effective_since = next(iter(prior_head_shas.values()), None)

    # Effective narrow flag: an explicit to_commit also produces a sub-range
    # diff, independent of narrow_to_recent_commits.
    effective_narrow = narrow_to_recent_commits and bool(effective_since)

    label, payload = load_code_input(
        None, None, branch, base, repo,
        only_globs=only_globs or [],
        include_all=include_all,
        summary_only=summary_only,
        since_commit=effective_since,
        to_commit=to_commit,
        narrow_to_recent_commits=effective_narrow,
        max_chars=eff_cap,
    )

    # The diff range + the commit whose line frame it's in (for graph_ref).
    _rng = _resolve_diff_range(branch, base, effective_since, to_commit, effective_narrow)

    parts: list[str] = []
    handoff = _load_handoff(branch, repo, handoff_path, no_handoff, feature)
    handoff_source = None
    handoff_content = None
    if handoff:
        handoff_source, content = handoff
        handoff_content = content
        info(f"loaded handoff from {handoff_source} ({len(content):,} chars)")
        parts.append(f"## Implementer's handoff (from `{handoff_source}`)\n\n{content}")
    if context:
        parts.append(f"## Inline note\n\n{context.strip()}")
    extra_context = "\n\n---\n\n".join(parts).strip()

    # Caller-context analysis (task #10). Best-effort; never blocks the review.
    caller_context_md = ""
    caller_context_meta: dict = {
        "depth_used": caller_depth, "per_repo": [], "section_md": "",
        "total_callers_included": 0, "total_bytes": 0,
    }
    if not summary_only:
        try:
            # Use the SAME diff range as load_code_input so caller-context
            # analysis sees the same changed lines (task #9/#13 ranges).
            r = _git(repo, "diff", _rng["diff"])
            if r.returncode == 0 and r.stdout:
                from caller_lookup import build_caller_context
                # graph_ref = the range END so def line numbers match the
                # diff hunks (option A line-alignment; for an explicit
                # range the end is to_commit, not the branch HEAD).
                cc = build_caller_context(
                    repo, r.stdout, depth=caller_depth, graph_ref=_rng["end"],
                )
                if cc.get("section_md"):
                    caller_context_md = cc["section_md"]
                caller_context_meta["per_repo"].append({"repo": repo, **{
                    k: cc[k] for k in (
                        "depth_used", "changed_symbols", "callers_found",
                        "callers_included", "callers_truncated", "bytes", "note"
                    )
                }})
                caller_context_meta["section_md"] = caller_context_md
                caller_context_meta["total_callers_included"] = cc.get("callers_included", 0)
                caller_context_meta["total_bytes"] = len(caller_context_md)
                info(f"caller-context: {cc['note']}")
        except Exception as e:
            info(f"caller-context skipped: {type(e).__name__}: {e}")

    info(f"mode=review (server)  subject={label}  size={len(payload)} chars")
    result = _run_review_pipeline(
        roles, label, payload, extra_context,
        prior_result=prior_result, test_results_by_repo=test_results_by_repo,
        progress_cb=progress_cb, tests_future=tests_future,
        caller_context_md=caller_context_md,
        reviewers=reviewers_norm,
    )
    result["caller_context"] = caller_context_meta
    result["handoff_source"] = handoff_source
    result["handoff_content"] = handoff_content
    # Fallback only applies to the narrow path; an explicit to_commit
    # always produces a real range (no anchor needed).
    _single_fallback = (
        narrow_to_recent_commits and not to_commit and not bool(effective_since)
    )
    result["commit_range"] = {
        "narrow_to_recent_commits": narrow_to_recent_commits,
        "explicit_since_commit": since_commit,
        "explicit_to_commit": to_commit,
        "range_kind": _rng["kind"],
        "per_repo": [{
            "repo_url": repo,
            "branch": branch,
            "base": base,
            "since_anchor": effective_since,
            "to_commit": to_commit,
            "narrowed": _rng["kind"] in ("explicit_range", "narrow"),
            "narrow_fallback": _single_fallback,
        }],
        "narrow_fallback_reason": (
            "narrow_to_recent_commits was requested but no since-anchor was "
            "resolvable (no explicit since_commit and no prior-iteration SHA). "
            "Reviewed at FULL diff scope."
            if _single_fallback else None
        ),
    }
    result["request"] = {
        "repo": repo,
        "branch": branch,
        "base": base,
        "only_globs": only_globs or [],
        "include_all": include_all,
        "summary_only": summary_only,
        "handoff_path": handoff_path,
        "no_handoff": no_handoff,
        "caller_depth": caller_depth,
        "since_commit": since_commit,
        "to_commit": to_commit,
        "narrow_to_recent_commits": narrow_to_recent_commits,
        "reviewers": reviewers_norm,
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


# ---------------------------------------------------------------------------
# Seam-centric sliced review (Phase 2). Slice an over-cap feature, review each
# packed slice with the existing pipeline SEQUENTIALLY, and reduce to one
# fingerprinted report with a token ledger. Heavy deps are imported lazily so
# this module stays importable without tree-sitter.
# ---------------------------------------------------------------------------

def _build_graph_at_branch(repo: str, branch: str) -> dict:
    # Phase 1 finding: build at the branch head, NOT the cached base — the
    # feature's new code does not exist at base.
    import caller_graph
    return caller_graph.build_graph(repo, branch)


def _feature_diff_text(repo: str, branch: str, base: str, only_globs=None,
                       diff_spec: str | None = None) -> str:
    # diff_spec (e.g. "since..to" or "base...branch") wins when given, so a
    # commit-range review scopes the feature diff to exactly that range.
    spec = diff_spec or f"{base}...{branch}"
    args = ["git", "-C", repo, "diff", spec]
    if only_globs:
        args += ["--", *only_globs]
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", check=True).stdout


def _worst_slice_verdict(r: dict) -> str:
    order = {"request-changes": 0, "comment": 1, "approve": 2}
    vs = [v for v in (r.get("claude_verdict"), r.get("codex_verdict")) if v]
    return min(vs, key=lambda v: order.get(v, 3)) if vs else "comment"


def review_branch_sliced_structured(repo, branch, base="main", *,
                                    review_fn=None, repo_label=None,
                                    prior_result=None, progress_cb=None,
                                    window_budget=0,
                                    pack_target=None, **common):
    """Slice an over-cap feature, review each packed slice with `review_fn`
    (default review_branch_structured) SEQUENTIALLY, and reduce to one report
    with a token ledger and (if prior_result) an iteration diff."""
    import os
    import slicer
    import reducer
    import ledger as _ledger
    import contract_cards

    review_fn = review_fn or review_branch_structured
    label = repo_label or repo.rsplit("/", 1)[-1]
    if pack_target is None:
        pack_target = int(slicer.MAX_REVIEW_CHARS * 0.7)

    # Commit-range scoping. Pulled from `common` (not explicit params) so the
    # server can forward the whole review kwargs blob via **common without a
    # duplicate-keyword collision. Threaded into BOTH the feature diff (what we
    # slice) AND every per-slice review (so review_fn scopes its own diff to the
    # same range — otherwise it re-derives base...branch and picks up unrelated
    # changes to shared files).
    since_commit = common.pop("since_commit", None)
    to_commit = common.pop("to_commit", None)
    narrow = common.pop("narrow_to_recent_commits", False)
    rng = _resolve_diff_range(branch, base, since_commit, to_commit, narrow)

    graph = _build_graph_at_branch(repo, rng["end"])
    diff_files = slicer.parse_unified_diff(
        _feature_diff_text(repo, branch, base, diff_spec=rng["diff"]))
    raw = slicer.slice_feature(graph, diff_files)
    packed = slicer.pack_slices(raw, diff_files, target=pack_target)

    # Seam lens (Phase 3): for each slice, the callees it invokes that live in
    # OTHER slices. Their contract cards are injected so the reviewer sees the
    # far side of every cross-slice handoff.
    seams_by_slice = slicer.cross_slice_callees(graph, packed)
    # Cards live beside the symbol-graph cache. Derive the root from the same
    # env var caller_graph uses, WITHOUT importing caller_graph (it needs
    # tree-sitter, absent off-server).
    _graph_cache_root = os.environ.get("GRAPH_CACHE_ROOT",
                                       "/home/ubuntu/ai-review-server/graph-cache")
    card_store = contract_cards.CardStore(
        cache_path=os.path.join(_graph_cache_root, repo.rsplit("/", 1)[-1],
                                "contract-cards.json"))
    base_context = common.pop("context", None)

    L = _ledger.TokenLedger(window_budget=window_budget)
    slice_results = []
    for i, sl in enumerate(packed, 1):
        if progress_cb:
            progress_cb(f"slice {i}/{len(packed)} ({sl.root_type}, {rng['kind']}) "
                        f"— {len(sl.files)} files")
        seam_syms = seams_by_slice.get(sl.root) or set()
        cards = (contract_cards.cards_for_symbols(card_store, repo, rng["end"], graph, seam_syms)
                 if seam_syms else {})
        cards_md = contract_cards.render_cards_md(cards)
        slice_context = "\n\n".join(x for x in (base_context, cards_md) if x) or None
        try:
            r = review_fn(repo=repo, branch=branch, base=base,
                          only_globs=sorted(sl.files), context=slice_context,
                          since_commit=since_commit, to_commit=to_commit,
                          narrow_to_recent_commits=narrow, **common)
        except SystemExit as e:
            raise RuntimeError(
                f"slice {i}/{len(packed)} root={sl.root} failed: {e} "
                f"[tokens so far: {L.total_tokens():,}]") from e
        usage = r.get("usage") or {}
        for model in ("claude", "codex"):
            L.add(sl.root, model, "review", usage.get(model, {}))
        slice_results.append({
            "slice": sl.root,
            "verdict": _worst_slice_verdict(r),
            "claude_findings": r.get("claude_findings") or [],
            "codex_findings": r.get("codex_findings") or [],
        })

    card_store.save()   # persist any cards generated this run
    reduced = reducer.reduce(label, slice_results)
    if prior_result:
        reduced["iteration"] = reducer.iteration_diff(reduced, prior_result)
    reduced["slice_count"] = len(packed)
    reduced["token_ledger"] = {
        "total_tokens": L.total_tokens(),
        "by_model": L.by_model(), "by_slice": L.by_slice(), "by_phase": L.by_phase(),
        "pct_of_window": L.pct_of_window(), "render": L.render(),
    }
    if progress_cb:
        progress_cb(L.render())
    return reduced


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
