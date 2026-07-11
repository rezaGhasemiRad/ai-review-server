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


# v1 classification: match recognizable C# markers in the def signature and
# enclosing class declaration. Best-effort labels for report legibility;
# type-specific checklists are deferred to a later phase.
_ENDPOINT_MARKERS = ("[HttpGet", "[HttpPost", "[HttpPut", "[HttpDelete",
                     "[HttpPatch", "[Route", "[ApiController", ": ControllerBase",
                     ": Controller")
_HUB_MARKERS = (": Hub", ": Hub<")
_JOB_MARKERS = (": BackgroundService", "IHostedService", "IJob")


def classify_root(graph: dict, symbol: str) -> str:
    defs = (graph.get("symbol_index") or {}).get(symbol, {}).get("defs", [])
    sig = " ".join(d.get("signature", "") for d in defs)
    if any(m in sig for m in _JOB_MARKERS):
        return "job"
    if any(m in sig for m in _HUB_MARKERS):
        return "hub"
    if any(m in sig for m in _ENDPOINT_MARKERS):
        return "endpoint"
    return "unknown"


def _def_files(graph: dict, symbol: str) -> set[str]:
    return {d["file"] for d in (graph.get("symbol_index") or {}).get(symbol, {}).get("defs", [])}


def build_slice(graph: dict, root: str, root_type: str | None = None) -> Slice:
    """DFS down from `root` over callees, collecting all reachable symbols and
    the files that define them."""
    seen: set[str] = set()
    stack = [root]
    while stack:
        sym = stack.pop()
        if sym in seen:
            continue
        seen.add(sym)
        stack.extend(callees(graph, sym) - seen)
    files: set[str] = set()
    for sym in seen:
        files |= _def_files(graph, sym)
    return Slice(root=root, root_type=root_type or classify_root(graph, root),
                 symbols=seen, files=files)


def slice_char_size(slc: Slice, diff_files: dict[str, DiffFile]) -> int:
    """Review-payload size proxy: total diff chars of the slice's changed
    files (files that are both in the slice and in the diff)."""
    return sum(diff_files[p].char_count for p in slc.files if p in diff_files)


def sub_split(graph: dict, slc: Slice, diff_files: dict[str, DiffFile],
              cap: int = MAX_REVIEW_CHARS) -> list[Slice]:
    """If `slc` exceeds `cap`, re-root on the root's direct callees (one level
    down) and recurse. A `residual` slice keeps the root + any symbols not
    pulled into a child subtree, so coverage of the parent's symbols is fully
    preserved. Returns slices that each fit under `cap` (except a single symbol
    whose own diff exceeds the cap — a Phase 2 fallback decides what to do)."""
    if slice_char_size(slc, diff_files) <= cap:
        return [slc]
    children = sorted(callees(graph, slc.root))
    if not children:
        return [slc]  # single symbol over cap; caller decides fallback (Phase 2)

    child_slices = [build_slice(graph, c) for c in children]
    covered_by_children: set[str] = set().union(*(cs.symbols for cs in child_slices))

    out: list[Slice] = []
    # Residual: the root and anything not reachable through a child subtree.
    residual_syms = slc.symbols - covered_by_children
    if residual_syms:
        residual_files: set[str] = set()
        for s in residual_syms:
            residual_files |= _def_files(graph, s)
        out.append(Slice(root=slc.root, root_type=slc.root_type,
                        symbols=residual_syms, files=residual_files))
    for cs in child_slices:
        out.extend(sub_split(graph, cs, diff_files, cap))
    return out


def pack_slices(slices: list[Slice], diff_files: dict[str, DiffFile],
                target: int = int(MAX_REVIEW_CHARS * 0.7)) -> list[Slice]:
    """First-fit-decreasing bin-pack: merge slices whose combined changed-file
    size stays <= target, to minimize review count. A slice already over
    target is emitted alone (sub_split should have handled true over-cap).
    Coverage is preserved (union of symbols/files is unchanged). Merged slices
    are labeled root_type='packed'."""
    ordered = sorted(slices, key=lambda s: slice_char_size(s, diff_files), reverse=True)
    bins: list[Slice] = []
    for s in ordered:
        placed = False
        for b in bins:
            merged_files = b.files | s.files
            size = sum(diff_files[p].char_count for p in merged_files if p in diff_files)
            if size <= target:
                b.files = merged_files
                b.symbols |= s.symbols
                if b.root_type != "packed":
                    b.root = f"packed::{b.root}"
                    b.root_type = "packed"
                placed = True
                break
        if not placed:
            bins.append(Slice(root=s.root, root_type=s.root_type,
                             symbols=set(s.symbols), files=set(s.files)))
    return bins


def _root_of(graph: dict, symbol: str, _seen: set[str] | None = None) -> set[str]:
    """Walk up to the framework-invoked root(s) that can reach `symbol`.
    Returns the set of roots (usually one; multiple if shared code)."""
    _seen = _seen or set()
    if symbol in _seen:
        return set()
    _seen.add(symbol)
    ups = callers(graph, symbol)
    if not ups:
        return {symbol}                      # symbol is itself a root
    roots: set[str] = set()
    for up in ups:
        roots |= _root_of(graph, up, _seen)
    return roots


def slice_feature(graph: dict, diff_files: dict[str, DiffFile],
                  cap: int = MAX_REVIEW_CHARS) -> list[Slice]:
    """Turn a parsed diff into vertical slices. Every changed symbol is covered
    by at least one slice (via its root, or as its own orphan slice). Slices
    over `cap` are sub-split by sub-root."""
    changed = changed_symbols(graph, {p: d.changed_ranges for p, d in diff_files.items()})

    # 1. Roots that reach at least one changed symbol; sub-split any over cap.
    roots: set[str] = set()
    for sym in changed:
        roots |= _root_of(graph, sym)

    slices: list[Slice] = []
    for r in sorted(roots):
        slices.extend(sub_split(graph, build_slice(graph, r), diff_files, cap))

    # 2. Orphan sweep: any changed symbol not covered by a root slice becomes
    #    its own slice (defensive — _root_of already returns the symbol itself
    #    when it has no callers, but a cycle or missing edge could drop one).
    covered: set[str] = set().union(*(s.symbols for s in slices)) if slices else set()
    for sym in sorted(changed - covered):
        slices.append(Slice(root=f"orphan::{sym}", root_type="orphan",
                            symbols={sym}, files=_def_files(graph, sym)))
    return slices
