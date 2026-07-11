"""Deterministic feature-diff slicer for the AI Review Server.

Turns a unified diff + a caller_graph symbol graph into call-graph-root-
anchored vertical slices. Each slice is a controller/job/hub -> service ->
repo path. The union of slices covers 100% of the changed symbols; each
slice's changed-file diff fits under the review size cap.

No LLM. Pure graph traversal — reproducible and unit-testable.
"""
from __future__ import annotations

import dataclasses
import re

# Keep in sync with ai_chat.MAX_REVIEW_CHARS (mixed Claude+Codex cap).
# Imported lazily so the slicer stays unit-testable standalone.
# NOTE: ai_chat.py calls sys.exit() at import when the `claude`/`codex` CLIs
# are absent (SystemExit is NOT an Exception subclass), so catch it too.
try:
    from ai_chat import MAX_REVIEW_CHARS
except (Exception, SystemExit):  # pragma: no cover - fallback for isolated tests
    MAX_REVIEW_CHARS = 1_100_000


@dataclasses.dataclass
class Slice:
    """One vertical review unit."""
    root: str                       # anchoring symbol name (or "orphan:<file>::<sym>")
    root_type: str                  # endpoint|hub|job|consumer|unknown|orphan
    symbols: set[str] = dataclasses.field(default_factory=set)
    files: set[str] = dataclasses.field(default_factory=set)

    def __post_init__(self) -> None:
        self.symbols = set(self.symbols)
        self.files = set(self.files)


@dataclasses.dataclass
class DiffFile:
    path: str
    text: str = ""
    changed_ranges: list[tuple[int, int]] = dataclasses.field(default_factory=list)

    @property
    def char_count(self) -> int:
        return len(self.text)


_DIFF_GIT = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+?)\s*$")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<len>\d+))? @@")


def parse_unified_diff(diff_text: str) -> dict[str, DiffFile]:
    """Parse `git diff` output into {new_path: DiffFile}. changed_ranges are
    new-file line spans (start, start+len-1) from each hunk header; text is the
    raw per-file diff block (used for size measurement)."""
    files: dict[str, DiffFile] = {}
    current: DiffFile | None = None
    for line in diff_text.splitlines(keepends=True):
        m = _DIFF_GIT.match(line.rstrip("\n"))
        if m:
            path = m.group("b")
            current = DiffFile(path=path)
            files[path] = current
            current.text += line
            continue
        if current is None:
            continue
        current.text += line
        h = _HUNK.match(line.rstrip("\n"))
        if h:
            start = int(h.group("start"))
            length = int(h.group("len") or "1")
            end = start + max(length, 1) - 1
            current.changed_ranges.append((start, end))
    return files


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def changed_symbols(graph: dict, changed: dict[str, list[tuple[int, int]]]) -> set[str]:
    """Names of definitions whose [line_start, line_end] intersects a changed
    range in the same file."""
    out: set[str] = set()
    files = graph.get("files") or {}
    for path, ranges in changed.items():
        fdata = files.get(path)
        if not fdata:
            continue
        for de in fdata.get("defs", []):
            span = (de["line_start"], de["line_end"])
            if any(_overlaps(span, rng) for rng in ranges):
                out.add(de["name"])
    return out


def _has_def(graph: dict, name: str) -> bool:
    entry = (graph.get("symbol_index") or {}).get(name)
    return bool(entry and entry.get("defs"))


def callees(graph: dict, symbol: str) -> set[str]:
    """In-repo symbols called from within `symbol`'s body. A callee is any
    defined symbol S that has a ref whose enclosing_function_name == symbol."""
    out: set[str] = set()
    for name, entry in (graph.get("symbol_index") or {}).items():
        if name == symbol or not entry.get("defs"):
            continue
        if any(r.get("enclosing_function_name") == symbol for r in entry.get("refs", [])):
            out.add(name)
    return out


def callers(graph: dict, symbol: str) -> set[str]:
    """In-repo functions that call `symbol` (defined enclosing functions of its
    refs)."""
    out: set[str] = set()
    entry = (graph.get("symbol_index") or {}).get(symbol) or {}
    for r in entry.get("refs", []):
        caller = r.get("enclosing_function_name")
        if caller and _has_def(graph, caller):
            out.add(caller)
    return out


def is_root(graph: dict, symbol: str) -> bool:
    """A symbol with no in-repo callers (framework/runtime-invoked, or orphan)."""
    return len(callers(graph, symbol)) == 0
