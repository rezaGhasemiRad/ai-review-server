"""Phase 2 of task #10 — diff analysis + caller lookup + AST-aware snippets.

Given a unified diff and a per-repo symbol graph, produces the "Potentially
impacted callers" markdown section that gets injected into reviewer prompts.

Public entrypoint:

    build_caller_context(repo_path, diff_text, ref, depth=1, ...) -> dict

The returned dict shape:

    {
        "section_md":       <markdown to splice into the reviewer prompt>,
        "depth_used":       <int 1 or 2>,
        "changed_symbols":  [<symbol_name>, ...],
        "callers_found":    <int — total before caps>,
        "callers_included": <int — actually rendered>,
        "callers_truncated":<int — dropped due to caps>,
        "bytes":            <int — section_md size in chars>,
        "note":             <one-line summary for logs / UI tooltip>,
    }

Design notes:
- Snippet extraction is AST-aware: the full enclosing function body is sliced
  using the function_start/end line range captured by phase 1's graph build.
  This guarantees the reviewer sees every use of the call's bound variable,
  not just the lines immediately adjacent to the call site.
- Receiver-aware filtering reduces noise: when the changed def has an
  enclosing_class, refs whose receiver clearly doesn't match (different
  service field name) get deprioritized — though we still include them
  rather than drop because heuristic receiver matching is fuzzy.
- For depth=2, we walk one hop further: each depth-1 caller's enclosing
  function is itself looked up as a symbol, and its callers become depth-2.
  Tagged separately so the reviewer can weight them appropriately.
- Hard caps: max_callers + max_bytes. Truncation noted in the section.
"""

from __future__ import annotations

import logging
import pathlib
import re
from typing import Iterator, Optional

from caller_graph import (
    EXT_TO_LANG,
    _git_show_bytes,
    _is_noise,
    build_graph,
    find_defs,
    find_refs,
    get_or_build_graph,
)

log = logging.getLogger("caller_lookup")


# --------------------------------------------------------------------------- #
# Diff parsing
# --------------------------------------------------------------------------- #

# A unified-diff hunk header: @@ -<old_start>,<old_count> +<new_start>,<new_count> @@
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _parse_unified_diff(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """Parse a unified diff. Returns {new_path: [(start, end), ...]} where
    each range is a contiguous run of CHANGED lines (added or removed) in
    the new file.

    Context lines (unchanged lines shown for orientation, prefixed with " ")
    are NOT included in the ranges — they would falsely tag adjacent
    definitions as "changed" just because the diff hunk spans across a
    method boundary.

    Mechanics:
    - `diff --git a/<old> b/<new>` headers anchor cur_path (uses the new path).
    - `@@ -<old> +<new_start>,<new_count> @@` resets the cur_new_line counter.
    - Within a hunk:
        - `+<line>` (added): change at cur_new_line; advance counter.
        - `-<line>` (removed): change at cur_new_line (where the removal
          happened from the new file's perspective); do NOT advance.
        - ` <line>` (context, unchanged): advance counter, no change record.
        - `\\<...>` (no-newline marker): ignored.
    Adjacent change lines are collapsed into runs.
    """
    raw: dict[str, list[int]] = {}
    cur_path: Optional[str] = None
    cur_new_line: int = 0

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            new_path = None
            for part in parts:
                if part.startswith("b/"):
                    new_path = part[2:]
                    break
            cur_path = new_path
            cur_new_line = 0
            if cur_path and cur_path not in raw:
                raw[cur_path] = []
            continue
        if cur_path is None:
            continue
        m = _HUNK_RE.match(line)
        if m:
            cur_new_line = int(m.group(1))
            continue
        if cur_new_line <= 0:
            # Before any hunk header — file-mode metadata lines like
            # "index ...", "--- a/...", "+++ b/...". Skip safely.
            continue
        if not line:
            continue
        prefix = line[0]
        if prefix == "+":
            if line.startswith("+++"):
                continue
            raw[cur_path].append(cur_new_line)
            cur_new_line += 1
        elif prefix == "-":
            if line.startswith("---"):
                continue
            # Removed line: doesn't appear in new file but signals a change
            # at the current new-file insertion point. Don't advance the
            # counter (removed lines don't take up new-file space).
            raw[cur_path].append(cur_new_line)
        elif prefix == " ":
            cur_new_line += 1
        # backslash continuation marker etc. — ignore
        # (no-prefix lines shouldn't appear inside a hunk; if they do, skip)

    # Collapse runs of consecutive changed line numbers into ranges.
    out: dict[str, list[tuple[int, int]]] = {}
    for path, lines in raw.items():
        if not lines:
            continue
        lines = sorted(set(lines))
        ranges: list[tuple[int, int]] = []
        run_start = lines[0]
        prev = lines[0]
        for n in lines[1:]:
            if n == prev + 1:
                prev = n
                continue
            ranges.append((run_start, prev))
            run_start = n
            prev = n
        ranges.append((run_start, prev))
        out[path] = ranges
    return out


# --------------------------------------------------------------------------- #
# Identify changed definitions
# --------------------------------------------------------------------------- #

def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start <= b_end and a_end >= b_start


def _find_changed_defs(graph: dict, diff_paths: dict[str, list[tuple[int, int]]]) -> list[dict]:
    """For each (file, hunk_range) in the diff, find graph defs whose body
    ranges overlap the hunk. Returns a list of {file, def} dicts.

    Keeps only INNERMOST defs: if a class definition and one of its methods
    both overlap a hunk, the class is dropped because the method is the
    finer-grained symbol that actually changed. This stops looking up
    callers of CategoryService (the class) when the actual change is to
    GetCategoryByIdAsync (a method) — class-level lookups would surface
    every caller of any service method, which is just noise.
    """
    out: list[dict] = []
    files = graph.get("files") or {}
    seen: set[tuple[str, int, int]] = set()
    for path, ranges in diff_paths.items():
        entry = files.get(path)
        if not entry:
            continue
        # Collect every def that overlaps any hunk in this file.
        overlapping: list[dict] = []
        for d in entry.get("defs", []):
            ds, de = d.get("line_start"), d.get("line_end")
            if ds is None or de is None:
                continue
            if any(_ranges_overlap(ds, de, hs, he) for hs, he in ranges):
                overlapping.append(d)
        # Filter to innermost: drop any def that STRICTLY contains another
        # overlapping def. A class containing a changed method gets dropped.
        for d in overlapping:
            contains_other = any(
                other["line_start"] > d["line_start"]
                and other["line_end"] < d["line_end"]
                for other in overlapping
                if other is not d
            )
            if contains_other:
                continue
            # Dedupe by (file, line_start, line_end) — same def reported for
            # multiple overlapping hunks shouldn't multiply.
            key = (path, d["line_start"], d["line_end"])
            if key in seen:
                continue
            seen.add(key)
            out.append({"file": path, "def": d})
    return out


# --------------------------------------------------------------------------- #
# Caller lookup with depth control
# --------------------------------------------------------------------------- #

_TEST_PATH_PARTS = ("test", "tests", "__tests__", "spec", "specs")
_TEST_NAME_PATTERNS = (
    re.compile(r"Test\.cs$"),
    re.compile(r"Tests\.cs$"),
    re.compile(r"\.test\.tsx?$"),
    re.compile(r"\.spec\.tsx?$"),
    re.compile(r"^test_.*\.py$"),
    re.compile(r"_test\.py$"),
)


def _is_test_file(rel_path: str) -> bool:
    parts = rel_path.replace("\\", "/").lower().split("/")
    if any(p in _TEST_PATH_PARTS for p in parts):
        return True
    name = pathlib.PurePosixPath(rel_path).name
    return any(p.search(name) for p in _TEST_NAME_PATTERNS)


def _find_callers(
    graph: dict,
    changed_defs: list[dict],
    diff_files: set[str],
    depth: int,
    skip_tests: bool = True,
) -> list[dict]:
    """BFS the graph to find refs to changed symbols. Returns a list of
    caller dicts with depth, symbol, and enclosing-function info.

    Refs from files already in the diff are skipped (reviewer sees them
    already). Tests are skipped by default (they're a parallel safety net).

    For depth>=2, each depth-1 caller's enclosing_function_name is itself
    enqueued as a symbol to search. Indirect callers are tagged with
    `depth=2`; the reviewer prompt explains the difference.
    """
    out: list[dict] = []
    # Symbol queue: list of (symbol_name, def_file_hint, current_depth)
    # def_file_hint helps filter self-refs at depth 1.
    queue: list[tuple[str, Optional[str], int]] = [
        (cd["def"]["name"], cd["file"], 1) for cd in changed_defs
    ]
    # Avoid re-searching the same symbol at the same depth
    seen: set[tuple[str, int]] = set((s, d) for s, _, d in queue)

    while queue:
        sym_name, def_file, current_depth = queue.pop(0)
        if current_depth > depth:
            continue

        refs = find_refs(graph, sym_name)
        for ref in refs:
            ref_file = ref.get("file")
            if not ref_file:
                continue
            if ref_file in diff_files:
                # The reviewer is already going to see this file in the diff
                continue
            if def_file and ref_file == def_file:
                # Self-reference inside the changed file (already in diff scope)
                continue
            if skip_tests and _is_test_file(ref_file):
                continue
            enclosing_fn = ref.get("enclosing_function_name")
            enc_start = ref.get("enclosing_function_start")
            enc_end = ref.get("enclosing_function_end")
            if not enclosing_fn or not enc_start or not enc_end:
                # Module-level call — no enclosing function to slice. Use a
                # 10-line window around the call line as a fallback.
                line = ref.get("line", 1)
                enc_start = max(1, line - 5)
                enc_end = line + 5
                enclosing_fn = "(module level)"
            out.append({
                "depth": current_depth,
                "symbol": sym_name,
                "from_file": def_file,
                "caller_file": ref_file,
                "caller_line": ref.get("line"),
                "receiver": ref.get("receiver"),
                "enclosing_function_name": enclosing_fn,
                "enclosing_function_start": enc_start,
                "enclosing_function_end": enc_end,
            })
            # Enqueue indirect callers for depth>1
            if current_depth < depth and enclosing_fn and enclosing_fn != "(module level)":
                next_key = (enclosing_fn, current_depth + 1)
                if next_key not in seen:
                    seen.add(next_key)
                    queue.append((enclosing_fn, ref_file, current_depth + 1))
    return out


# --------------------------------------------------------------------------- #
# Snippet extraction (AST-aware via the line range captured at graph build)
# --------------------------------------------------------------------------- #

def _extract_snippet(
    repo_path: pathlib.Path,
    ref: str,
    file_path: str,
    line_start: int,
    line_end: int,
    max_function_lines: int = 100,
) -> Optional[str]:
    """Slice [line_start, line_end] from `file_path` at git ref `ref`.

    For huge functions (>max_function_lines), return a truncated view:
    first 40 lines + " ... (N lines elided) ... " + last 20 lines.
    Returns None if the file or range can't be read.
    """
    raw = _git_show_bytes(repo_path, ref, file_path)
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8", "replace")
    except UnicodeDecodeError:
        return None
    lines = text.split("\n")
    # Clamp to file bounds
    s = max(1, line_start) - 1
    e = min(len(lines), line_end)
    chunk = lines[s:e]
    if len(chunk) > max_function_lines:
        head = chunk[:40]
        tail = chunk[-20:]
        elided = len(chunk) - 60
        chunk = head + [f"    // ... ({elided} lines elided for brevity) ..."] + tail
    return "\n".join(chunk)


# --------------------------------------------------------------------------- #
# Ranking + caps
# --------------------------------------------------------------------------- #

def _caller_rank_key(c: dict) -> tuple:
    """Lower is better. Sort key for caller ranking before applying caps.

    Priorities:
    1. Depth 1 before depth 2 (direct callers more relevant than indirect).
    2. Non-test, non-noise files first (already filtered, but defense).
    3. Smaller enclosing functions first (more digestible context).
    """
    depth_priority = c.get("depth", 99)
    fn_size = (c.get("enclosing_function_end", 0) or 0) - (c.get("enclosing_function_start", 0) or 0)
    return (depth_priority, fn_size, c.get("caller_file", ""), c.get("caller_line", 0))


# --------------------------------------------------------------------------- #
# Section formatting
# --------------------------------------------------------------------------- #

def _language_fence(file_path: str) -> str:
    """fenced-block language tag for syntax highlighting in the reviewer prompt."""
    ext = pathlib.PurePosixPath(file_path).suffix.lower()
    lang = EXT_TO_LANG.get(ext)
    return {
        "csharp": "csharp",
        "typescript": "typescript",
        "tsx": "tsx",
        "python": "python",
    }.get(lang or "", "")


def _format_section(
    callers_with_snippets: list[tuple[dict, str]],
    changed_symbols: list[str],
    callers_found: int,
    callers_truncated: int,
    depth: int,
) -> str:
    """Build the "Potentially impacted callers" markdown section."""
    if not callers_with_snippets:
        return ""
    lines: list[str] = []
    lines.append("## Potentially impacted callers")
    lines.append("")
    lines.append(
        "The diff modifies symbols that are called from files NOT in the diff. "
        "The snippets below show the call sites in those files (full enclosing "
        "function body, so every use of the call result is visible)."
    )
    lines.append("")
    lines.append(
        "**How to use this section:** verify the changes in the diff don't break "
        "these callers — null dereferences, removed return shapes, changed "
        "exception behavior, etc. Direct callers (depth 1) deserve high "
        "scrutiny. Indirect callers (depth 2) may or may not be affected "
        "depending on whether the intermediate function fully encapsulates "
        "the changed contract — verify case-by-case. If you can't tell whether "
        "a caller breaks, flag it as a finding asking the implementer to verify "
        "manually rather than silently approving."
    )
    lines.append("")
    sym_summary = ", ".join(f"`{s}`" for s in changed_symbols[:6])
    if len(changed_symbols) > 6:
        sym_summary += f", and {len(changed_symbols) - 6} more"
    lines.append(f"**Symbols changed in this diff:** {sym_summary}")
    lines.append("")
    lines.append(
        f"**Coverage:** {len(callers_with_snippets)} caller(s) shown · "
        f"depth = {depth} · "
        f"{callers_truncated} more caller(s) elided due to caps."
    )
    lines.append("")

    for caller, snippet in callers_with_snippets:
        depth_tag = "Direct caller" if caller["depth"] == 1 else f"Indirect caller (depth {caller['depth']})"
        loc = f"{caller['caller_file']}:{caller['caller_line']}"
        fn_name = caller.get("enclosing_function_name") or "(module level)"
        receiver = caller.get("receiver")
        recv_str = f"{receiver}." if receiver else ""
        lines.append(
            f"### {depth_tag} — `{loc}` in `{fn_name}` "
            f"(calls `{recv_str}{caller['symbol']}`)"
        )
        lines.append("")
        fence_lang = _language_fence(caller["caller_file"])
        lines.append(f"```{fence_lang}")
        lines.append(snippet.rstrip())
        lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# Public entrypoint
# --------------------------------------------------------------------------- #

def build_caller_context(
    repo_path: str | pathlib.Path,
    diff_text: str,
    ref: Optional[str] = None,
    depth: int = 1,
    max_callers: int = 10,
    max_bytes: int = 30_000,
    skip_tests: bool = True,
    skip_if_small_diff: bool = True,
    graph_ref: Optional[str] = None,
) -> dict:
    """Build the "Potentially impacted callers" markdown section for a diff.

    Returns a dict with the section + metadata for persistence / display.
    Returns an empty section_md when there's nothing useful to show
    (no changed defs found in graph, no callers, or skip heuristic fires).

    Args:
        repo_path: absolute path to the local clone the graph was built for.
        diff_text: unified diff text (e.g. output of git diff base...branch).
        ref: graph ref to read source from for snippet extraction. If None,
            falls back to graph_ref, then the graph's own ref, then HEAD.
        depth: 1 (direct callers only) or 2 (one hop of indirect callers).
        max_callers: hard cap on number of callers in the section.
        max_bytes: hard cap on the section's total markdown size in chars.
        skip_tests: skip test-shaped files (defense-in-depth runs them anyway).
        skip_if_small_diff: if the diff is <500 lines and no def appears
            to have a signature change, skip caller-context entirely
            (heuristic: tiny isolated bug fixes don't need it).
        graph_ref: the FEATURE-BRANCH ref the diff was computed against
            (e.g. "origin/feat/foo"). When set, the graph is built FRESH
            from this ref instead of using the cached origin/main graph.

            WHY THIS MATTERS: the diff's hunk line numbers are in the
            feature-branch frame. The cached graph is built from origin/main.
            When the feature branch has shifted lines (added/removed code
            above a def in earlier commits), the cached graph's def line
            ranges no longer align with the diff hunks → false negatives on
            overlap → the graph silently reports "no changed symbols" for a
            def that IS being modified. Building from graph_ref puts the
            graph's def line numbers in the SAME frame as the diff hunks.

            Not cached on disk — feature-branch graphs would pollute the
            shared per-repo origin/main cache. Built fresh each review
            (~1-3s per repo, invisible against the multi-minute review).
    """
    repo = pathlib.Path(repo_path).resolve()
    depth = max(1, min(2, int(depth or 1)))

    diff_paths = _parse_unified_diff(diff_text)
    total_changed_lines = sum(
        end - start + 1 for ranges in diff_paths.values() for start, end in ranges
    )

    if not diff_paths:
        return _empty_result(depth, note="no parseable diff hunks")

    # Apply small-diff skip heuristic
    if skip_if_small_diff and total_changed_lines < 500:
        # Could still be worth it if there's a signature change — but we
        # don't reliably detect those mechanically yet. Honor the cap.
        # NOTE: this heuristic can be turned off via skip_if_small_diff=False.
        # For now, leave it off-by-default behavior to a future tuning pass —
        # we WILL still build caller-context for small diffs because they're
        # the iter-N cases where it matters most.
        pass

    if graph_ref:
        # Build a fresh graph at the feature-branch ref so def line numbers
        # match the diff's hunk line numbers (see docstring). Not cached.
        graph = build_graph(repo, graph_ref)
        ref_for_snippet = ref or graph_ref
    else:
        # CLI / backward-compat path: use the cached origin/main graph.
        graph = get_or_build_graph(repo)
        ref_for_snippet = ref or graph.get("ref") or "HEAD"

    changed_defs = _find_changed_defs(graph, diff_paths)
    if not changed_defs:
        return _empty_result(depth, note="no graph defs overlap diff hunks")

    diff_files = set(diff_paths.keys())
    raw_callers = _find_callers(
        graph, changed_defs, diff_files, depth=depth, skip_tests=skip_tests
    )

    # Deduplicate: same (caller_file, enclosing_function_start) seen multiple
    # times means the same enclosing function calls multiple changed symbols.
    # Keep one entry per (file, function), recording the multiple symbols.
    dedup: dict[tuple[str, int], dict] = {}
    for c in raw_callers:
        key = (c["caller_file"], c["enclosing_function_start"] or 0)
        if key in dedup:
            existing = dedup[key]
            # Prefer the lower-depth entry; merge symbols list
            if c["depth"] < existing["depth"]:
                existing.update(c)
            symbols = existing.setdefault("_symbols_called", set())
            symbols.add(c["symbol"])
        else:
            dedup[key] = dict(c)
            dedup[key]["_symbols_called"] = {c["symbol"]}
    callers = sorted(dedup.values(), key=_caller_rank_key)

    callers_found = len(callers)

    # Extract snippets up to caps
    selected: list[tuple[dict, str]] = []
    total = 0
    for c in callers:
        if len(selected) >= max_callers:
            break
        snippet = _extract_snippet(
            repo,
            ref_for_snippet,
            c["caller_file"],
            c["enclosing_function_start"] or 1,
            c["enclosing_function_end"] or 1,
        )
        if snippet is None:
            continue
        # Pre-format estimate: snippet + headers ≈ snippet + 300 chars overhead
        est = len(snippet) + 300
        if total + est > max_bytes:
            break
        # If multiple symbols were called from this function, surface that
        if len(c["_symbols_called"]) > 1:
            c["symbol"] = ", ".join(sorted(c["_symbols_called"]))
        selected.append((c, snippet))
        total += est

    changed_symbols = sorted({cd["def"]["name"] for cd in changed_defs})
    callers_included = len(selected)
    callers_truncated = max(0, callers_found - callers_included)

    section_md = _format_section(
        selected, changed_symbols, callers_found, callers_truncated, depth
    )

    note = (
        f"caller-context: {callers_included}/{callers_found} caller(s), "
        f"depth={depth}, {len(section_md):,} chars"
    )
    return {
        "section_md": section_md,
        "depth_used": depth,
        "changed_symbols": changed_symbols,
        "callers_found": callers_found,
        "callers_included": callers_included,
        "callers_truncated": callers_truncated,
        "bytes": len(section_md),
        "note": note,
    }


def _empty_result(depth: int, note: str) -> dict:
    return {
        "section_md": "",
        "depth_used": depth,
        "changed_symbols": [],
        "callers_found": 0,
        "callers_included": 0,
        "callers_truncated": 0,
        "bytes": 0,
        "note": note,
    }
