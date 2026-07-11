# Seam-Centric Review — Phase 1: Slicer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `slicer.py` — a deterministic module that turns a feature diff into call-graph-root-anchored vertical slices that each fit the review size cap and together cover 100% of the diff.

**Architecture:** Pure Python over the existing `caller_graph.py` symbol graph. No LLM. Input = a unified diff + the repo's symbol graph; output = a list of `Slice` objects. Everything is unit-tested against a hand-authored synthetic graph fixture, then validated end-to-end against the real Quick Duel diff.

**Tech Stack:** Python 3.10+, `pytest`, `dataclasses`, existing `caller_graph.py` (tree-sitter symbol graph).

**Scope (this plan):** slicing core only — diff parsing, changed-symbol mapping, root detection, vertical-slice DFS, orphan sweep + coverage guarantee, size measurement, sub-split-by-sub-root, and minimal root classification. **Not in this plan** (later phases, per spec §7): wiring slices into the review pipeline, contract cards, adversarial verify, inter-slice pass, reducer/fingerprints, synthesis, token ledger.

**Reference:** `docs/superpowers/specs/2026-07-11-seam-centric-review-design.md` §5.1.

---

## File structure

- **Create:** `slicer.py` — the whole slicing core (one module, one responsibility).
- **Create:** `tests/test_slicer.py` — unit tests + the shared synthetic graph fixture.
- **Create:** `tests/test_slicer_quickduel.py` — end-to-end validation against the real repo (deps-gated).
- **Depends on (read-only):** `caller_graph.py` — `get_or_build_graph`, `find_refs`, graph shape (`files{}`, `symbol_index{}`).

### Graph shape (from `caller_graph.py`, for reference)

```
graph = {
  "files": { "<rel-path>": {
      "language": "csharp|...",
      "defs": [ {name, kind, line_start, line_end, signature, enclosing_class} ],
      "refs": [ {name, line, ref_kind, receiver, enclosing_function_name,
                 enclosing_function_start, enclosing_function_end} ],
  }},
  "symbol_index": { "<symbol-name>": { "defs": [...], "refs": [...] } },
}
```
- A **ref** is a *use* of `name` at a location; `enclosing_function_name` is the function the use sits inside (i.e., the caller).
- `find_refs(graph, name)` → all refs *to* `name` (its call sites). Empty ⇒ nobody in-repo calls it.

---

## Task 1: Module scaffold, `Slice` dataclass, and the test fixture

**Files:**
- Create: `slicer.py`
- Create: `tests/test_slicer.py`

- [ ] **Step 1: Create `slicer.py` with constants and the `Slice` dataclass**

```python
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
# Imported lazily so the slicer stays unit-testable standalone. ai_chat.py
# calls sys.exit() at import when the claude/codex CLIs are absent, and
# SystemExit is NOT an Exception subclass — so catch it too.
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
```

- [ ] **Step 2: Create `tests/test_slicer.py` with the synthetic graph fixture**

```python
import pytest
import slicer


@pytest.fixture
def graph():
    """A tiny two-feature repo:

    Controllers/AdminQuestionController.cs
        MoveQuestions [endpoint]  (L10-20)  -> MoveQuestionsToCategoryAsync
    Services/QuestionService.cs
        MoveQuestionsToCategoryAsync (L10-30) -> GetCategoryByIdAsync, MoveQuestionsCategoryAsync
    Repositories/QuestionRepository.cs
        GetCategoryByIdAsync (L10-15)      [leaf]
        MoveQuestionsCategoryAsync (L20-28)[leaf]
    Jobs/SalesImportJob.cs
        ExecuteAsync [job] (L10-25)        -> InsertSalesDetails
    Services/SalesService.cs
        InsertSalesDetails (L10-40)        -> BulkInsert
    Repositories/SalesRepository.cs
        BulkInsert (L10-18)                [leaf]
    Util/StringHelpers.cs
        Slugify (L5-9)                     [orphan: no callers, calls nothing]
    """
    def d(name, file, ls, le, kind="method", cls="", sig=""):
        return {"name": name, "kind": kind, "line_start": ls, "line_end": le,
                "signature": sig, "enclosing_class": cls}

    def r(name, file, line, caller, fs, fe, receiver="_x"):
        return {"name": name, "line": line, "ref_kind": "call", "receiver": receiver,
                "enclosing_function_name": caller,
                "enclosing_function_start": fs, "enclosing_function_end": fe}

    ctrl = "Controllers/AdminQuestionController.cs"
    svc = "Services/QuestionService.cs"
    repo = "Repositories/QuestionRepository.cs"
    job = "Jobs/SalesImportJob.cs"
    ssvc = "Services/SalesService.cs"
    srepo = "Repositories/SalesRepository.cs"
    util = "Util/StringHelpers.cs"

    files = {
        ctrl: {"language": "csharp",
               "defs": [d("MoveQuestions", ctrl, 10, 20, cls="AdminQuestionController",
                          sig='[HttpPost("move-category")] public async Task<IActionResult> MoveQuestions()')],
               "refs": [r("MoveQuestionsToCategoryAsync", ctrl, 15, "MoveQuestions", 10, 20)]},
        svc: {"language": "csharp",
              "defs": [d("MoveQuestionsToCategoryAsync", svc, 10, 30, cls="QuestionService")],
              "refs": [r("GetCategoryByIdAsync", svc, 14, "MoveQuestionsToCategoryAsync", 10, 30),
                       r("MoveQuestionsCategoryAsync", svc, 22, "MoveQuestionsToCategoryAsync", 10, 30)]},
        repo: {"language": "csharp",
               "defs": [d("GetCategoryByIdAsync", repo, 10, 15, cls="QuestionRepository"),
                        d("MoveQuestionsCategoryAsync", repo, 20, 28, cls="QuestionRepository")],
               "refs": []},
        job: {"language": "csharp",
              "defs": [d("ExecuteAsync", job, 10, 25, cls="SalesImportJob",
                         sig="public class SalesImportJob : BackgroundService")],
              "refs": [r("InsertSalesDetails", job, 18, "ExecuteAsync", 10, 25)]},
        ssvc: {"language": "csharp",
               "defs": [d("InsertSalesDetails", ssvc, 10, 40, cls="SalesService")],
               "refs": [r("BulkInsert", ssvc, 30, "InsertSalesDetails", 10, 40)]},
        srepo: {"language": "csharp",
                "defs": [d("BulkInsert", srepo, 10, 18, cls="SalesRepository")],
                "refs": []},
        util: {"language": "csharp",
               "defs": [d("Slugify", util, 5, 9, cls="StringHelpers")],
               "refs": []},
    }
    return {"files": files, "symbol_index": _index(files)}


def _index(files):
    """Mirror caller_graph._rebuild_symbol_index for the fixture."""
    idx = {}
    for path, fdata in files.items():
        for de in fdata["defs"]:
            idx.setdefault(de["name"], {"defs": [], "refs": []})
            idx[de["name"]]["defs"].append({**de, "file": path})
        for re_ in fdata["refs"]:
            idx.setdefault(re_["name"], {"defs": [], "refs": []})
            idx[re_["name"]]["refs"].append({**re_, "file": path})
    return idx


def test_slice_dataclass_normalizes_collections():
    s = slicer.Slice(root="MoveQuestions", root_type="endpoint",
                     symbols=["MoveQuestions"], files=["a.cs"])
    assert s.symbols == {"MoveQuestions"}
    assert s.files == {"a.cs"}
```

- [ ] **Step 3: Run the test to verify the module imports and the dataclass works**

Run: `pytest tests/test_slicer.py -v`
Expected: PASS (1 test: `test_slice_dataclass_normalizes_collections`).

- [ ] **Step 4: Commit**

```bash
git add slicer.py tests/test_slicer.py
git commit -m "feat(slicer): module scaffold, Slice dataclass, test fixture"
```

---

## Task 2: `parse_unified_diff` — changed files, line ranges, per-file text

**Files:**
- Modify: `slicer.py`
- Modify: `tests/test_slicer.py`

- [ ] **Step 1: Write the failing test**

```python
DIFF = """diff --git a/Services/QuestionService.cs b/Services/QuestionService.cs
index 111..222 100644
--- a/Services/QuestionService.cs
+++ b/Services/QuestionService.cs
@@ -12,3 +12,5 @@ public class QuestionService
 existing line
+added line one
+added line two
 tail line
diff --git a/Repositories/QuestionRepository.cs b/Repositories/QuestionRepository.cs
index 333..444 100644
--- a/Repositories/QuestionRepository.cs
+++ b/Repositories/QuestionRepository.cs
@@ -20,0 +21,2 @@ public class QuestionRepository
+new repo line
+another repo line
"""


def test_parse_unified_diff_extracts_files_ranges_and_text():
    parsed = slicer.parse_unified_diff(DIFF)
    assert set(parsed) == {"Services/QuestionService.cs",
                           "Repositories/QuestionRepository.cs"}
    svc = parsed["Services/QuestionService.cs"]
    # New-file hunk starts at 12, spans 5 lines -> [12, 16]
    assert svc.changed_ranges == [(12, 16)]
    assert svc.char_count == len(svc.text) > 0
    repo = parsed["Repositories/QuestionRepository.cs"]
    assert repo.changed_ranges == [(21, 22)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_slicer.py::test_parse_unified_diff_extracts_files_ranges_and_text -v`
Expected: FAIL with `AttributeError: module 'slicer' has no attribute 'parse_unified_diff'`.

- [ ] **Step 3: Implement `DiffFile` + `parse_unified_diff` in `slicer.py`**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_slicer.py::test_parse_unified_diff_extracts_files_ranges_and_text -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add slicer.py tests/test_slicer.py
git commit -m "feat(slicer): parse unified diff into files/ranges/text"
```

---

## Task 3: `changed_symbols` — map changed lines to enclosing definitions

**Files:**
- Modify: `slicer.py`
- Modify: `tests/test_slicer.py`

- [ ] **Step 1: Write the failing test**

```python
def test_changed_symbols_maps_ranges_to_enclosing_defs(graph):
    # A change at lines 22-23 of QuestionService is inside
    # MoveQuestionsToCategoryAsync (L10-30).
    changed = {"Services/QuestionService.cs": [(22, 23)]}
    assert slicer.changed_symbols(graph, changed) == {"MoveQuestionsToCategoryAsync"}


def test_changed_symbols_multiple_files_and_defs(graph):
    changed = {
        "Repositories/QuestionRepository.cs": [(12, 12), (25, 25)],  # both repo methods
        "Util/StringHelpers.cs": [(6, 6)],                            # Slugify
    }
    assert slicer.changed_symbols(graph, changed) == {
        "GetCategoryByIdAsync", "MoveQuestionsCategoryAsync", "Slugify"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_slicer.py -k changed_symbols -v`
Expected: FAIL with `AttributeError: module 'slicer' has no attribute 'changed_symbols'`.

- [ ] **Step 3: Implement `changed_symbols` in `slicer.py`**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_slicer.py -k changed_symbols -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add slicer.py tests/test_slicer.py
git commit -m "feat(slicer): map changed line ranges to enclosing symbols"
```

---

## Task 4: `callees`, `callers`, `is_root`

**Files:**
- Modify: `slicer.py`
- Modify: `tests/test_slicer.py`

- [ ] **Step 1: Write the failing test**

```python
def test_callees_finds_downstream_symbols(graph):
    assert slicer.callees(graph, "MoveQuestionsToCategoryAsync") == {
        "GetCategoryByIdAsync", "MoveQuestionsCategoryAsync"}
    assert slicer.callees(graph, "BulkInsert") == set()  # leaf


def test_callers_finds_upstream_symbols(graph):
    assert slicer.callers(graph, "MoveQuestionsToCategoryAsync") == {"MoveQuestions"}
    assert slicer.callers(graph, "MoveQuestions") == set()  # framework-invoked


def test_is_root_true_for_framework_invoked(graph):
    assert slicer.is_root(graph, "MoveQuestions") is True      # endpoint
    assert slicer.is_root(graph, "ExecuteAsync") is True       # job
    assert slicer.is_root(graph, "Slugify") is True            # orphan (no callers)
    assert slicer.is_root(graph, "MoveQuestionsToCategoryAsync") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_slicer.py -k "callees or callers or is_root" -v`
Expected: FAIL with `AttributeError: module 'slicer' has no attribute 'callees'`.

- [ ] **Step 3: Implement in `slicer.py`**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_slicer.py -k "callees or callers or is_root" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add slicer.py tests/test_slicer.py
git commit -m "feat(slicer): callees/callers/is_root graph queries"
```

---

## Task 5: `classify_root` + `build_slice` (root-anchored DFS)

**Files:**
- Modify: `slicer.py`
- Modify: `tests/test_slicer.py`

- [ ] **Step 1: Write the failing test**

```python
def test_classify_root_from_signature(graph):
    assert slicer.classify_root(graph, "MoveQuestions") == "endpoint"
    assert slicer.classify_root(graph, "ExecuteAsync") == "job"
    assert slicer.classify_root(graph, "Slugify") == "unknown"


def test_build_slice_collects_vertical_path(graph):
    s = slicer.build_slice(graph, "MoveQuestions")
    assert s.root == "MoveQuestions"
    assert s.root_type == "endpoint"
    assert s.symbols == {"MoveQuestions", "MoveQuestionsToCategoryAsync",
                         "GetCategoryByIdAsync", "MoveQuestionsCategoryAsync"}
    assert s.files == {"Controllers/AdminQuestionController.cs",
                       "Services/QuestionService.cs",
                       "Repositories/QuestionRepository.cs"}


def test_build_slice_job_root(graph):
    s = slicer.build_slice(graph, "ExecuteAsync")
    assert s.root_type == "job"
    assert s.symbols == {"ExecuteAsync", "InsertSalesDetails", "BulkInsert"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_slicer.py -k "classify_root or build_slice" -v`
Expected: FAIL with `AttributeError: module 'slicer' has no attribute 'classify_root'`.

- [ ] **Step 3: Implement in `slicer.py`**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_slicer.py -k "classify_root or build_slice" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add slicer.py tests/test_slicer.py
git commit -m "feat(slicer): root classification + vertical-slice DFS"
```

---

## Task 6: `slice_feature` — roots → slices → orphan sweep + coverage guarantee

**Files:**
- Modify: `slicer.py`
- Modify: `tests/test_slicer.py`

- [ ] **Step 1: Write the failing test**

```python
def test_slice_feature_covers_all_changed_symbols_via_roots_and_orphans(graph):
    # Change touches: the endpoint path (svc), the job path (job), and an
    # orphan (Slugify) whose caller is not in the diff.
    diff_files = {
        "Services/QuestionService.cs": slicer.DiffFile(
            "Services/QuestionService.cs", text="x" * 100, changed_ranges=[(22, 23)]),
        "Jobs/SalesImportJob.cs": slicer.DiffFile(
            "Jobs/SalesImportJob.cs", text="y" * 100, changed_ranges=[(18, 18)]),
        "Util/StringHelpers.cs": slicer.DiffFile(
            "Util/StringHelpers.cs", text="z" * 100, changed_ranges=[(6, 6)]),
    }
    slices = slicer.slice_feature(graph, diff_files)
    roots = {s.root for s in slices}
    # The changed service symbol is reached via its endpoint root, not itself.
    assert "MoveQuestions" in roots
    assert "ExecuteAsync" in roots
    # Slugify is a root itself (no callers) -> its own slice.
    assert "Slugify" in roots

    # COVERAGE GUARANTEE: every changed symbol appears in at least one slice.
    covered = set().union(*(s.symbols for s in slices))
    changed = slicer.changed_symbols(graph, {p: d.changed_ranges
                                             for p, d in diff_files.items()})
    assert changed <= covered


def test_slice_feature_orphan_when_caller_not_changed(graph):
    # Only the leaf repo method changed; its caller (the service) is unchanged
    # and out of the diff. It must still be covered — via its endpoint root,
    # which is discoverable through the graph even though unchanged.
    diff_files = {
        "Repositories/QuestionRepository.cs": slicer.DiffFile(
            "Repositories/QuestionRepository.cs", text="x" * 100,
            changed_ranges=[(21, 22)]),  # MoveQuestionsCategoryAsync
    }
    slices = slicer.slice_feature(graph, diff_files)
    covered = set().union(*(s.symbols for s in slices))
    assert "MoveQuestionsCategoryAsync" in covered
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_slicer.py -k slice_feature -v`
Expected: FAIL with `AttributeError: module 'slicer' has no attribute 'slice_feature'`.

- [ ] **Step 3: Implement `slice_feature` in `slicer.py`**

```python
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


def slice_feature(graph: dict, diff_files: dict[str, "DiffFile"]) -> list[Slice]:
    """Turn a parsed diff into vertical slices. Every changed symbol is covered
    by at least one slice (via its root, or as its own orphan slice)."""
    changed = changed_symbols(graph, {p: d.changed_ranges for p, d in diff_files.items()})

    # 1. Roots that reach at least one changed symbol.
    roots: set[str] = set()
    for sym in changed:
        roots |= _root_of(graph, sym)

    slices = [build_slice(graph, r) for r in sorted(roots)]

    # 2. Orphan sweep: any changed symbol not covered by a root slice becomes
    #    its own slice (defensive — _root_of already returns the symbol itself
    #    when it has no callers, but a cycle or missing edge could drop one).
    covered: set[str] = set().union(*(s.symbols for s in slices)) if slices else set()
    for sym in sorted(changed - covered):
        slices.append(Slice(root=f"orphan::{sym}", root_type="orphan",
                            symbols={sym}, files=_def_files(graph, sym)))
    return slices
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_slicer.py -k slice_feature -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add slicer.py tests/test_slicer.py
git commit -m "feat(slicer): slice_feature with root-walk + orphan-sweep coverage"
```

---

## Task 7: Size measurement + `sub_split` by sub-root

**Files:**
- Modify: `slicer.py`
- Modify: `tests/test_slicer.py`

- [ ] **Step 1: Write the failing test**

```python
def test_slice_char_size_sums_changed_file_diffs(graph):
    diff_files = {
        "Services/QuestionService.cs": slicer.DiffFile(
            "Services/QuestionService.cs", text="a" * 300, changed_ranges=[(22, 23)]),
        "Controllers/AdminQuestionController.cs": slicer.DiffFile(
            "Controllers/AdminQuestionController.cs", text="b" * 200, changed_ranges=[(15, 15)]),
    }
    s = slicer.build_slice(graph, "MoveQuestions")
    # Only files present in BOTH the slice and the diff count.
    assert slicer.slice_char_size(s, diff_files) == 500


def test_sub_split_descends_when_over_cap(graph):
    big = {
        "Controllers/AdminQuestionController.cs": slicer.DiffFile(
            "Controllers/AdminQuestionController.cs", text="c" * 10, changed_ranges=[(15, 15)]),
        "Services/QuestionService.cs": slicer.DiffFile(
            "Services/QuestionService.cs", text="c" * 100, changed_ranges=[(22, 22)]),
        "Repositories/QuestionRepository.cs": slicer.DiffFile(
            "Repositories/QuestionRepository.cs", text="c" * 100, changed_ranges=[(21, 21)]),
    }
    parent = slicer.build_slice(graph, "MoveQuestions")
    # Force a split with a tiny cap.
    parts = slicer.sub_split(graph, parent, big, cap=150)
    # Descends to the root's callees as new sub-roots.
    assert len(parts) >= 2
    for p in parts:
        assert slicer.slice_char_size(p, big) <= 150
    # Coverage preserved across the split.
    assert set().union(*(p.symbols for p in parts)) >= {
        "MoveQuestionsToCategoryAsync", "GetCategoryByIdAsync", "MoveQuestionsCategoryAsync"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_slicer.py -k "char_size or sub_split" -v`
Expected: FAIL with `AttributeError: module 'slicer' has no attribute 'slice_char_size'`.

- [ ] **Step 3: Implement in `slicer.py`**

```python
def slice_char_size(slc: Slice, diff_files: dict[str, "DiffFile"]) -> int:
    """Review-payload size proxy: total diff chars of the slice's changed
    files (files that are both in the slice and in the diff)."""
    return sum(diff_files[p].char_count for p in slc.files if p in diff_files)


def sub_split(graph: dict, slc: Slice, diff_files: dict[str, "DiffFile"],
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_slicer.py -k "char_size or sub_split" -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Wire `sub_split` into `slice_feature` and add a regression test**

In `slicer.py`, change the `slices = [build_slice(...) ...]` line in `slice_feature` to apply the cap, and add a `cap` parameter:

```python
def slice_feature(graph: dict, diff_files: dict[str, "DiffFile"],
                  cap: int = MAX_REVIEW_CHARS) -> list[Slice]:
    changed = changed_symbols(graph, {p: d.changed_ranges for p, d in diff_files.items()})
    roots: set[str] = set()
    for sym in changed:
        roots |= _root_of(graph, sym)
    slices: list[Slice] = []
    for r in sorted(roots):
        slices.extend(sub_split(graph, build_slice(graph, r), diff_files, cap))
    covered: set[str] = set().union(*(s.symbols for s in slices)) if slices else set()
    for sym in sorted(changed - covered):
        slices.append(Slice(root=f"orphan::{sym}", root_type="orphan",
                            symbols={sym}, files=_def_files(graph, sym)))
    return slices
```

Add the test:

```python
def test_slice_feature_respects_cap(graph):
    diff_files = {
        "Controllers/AdminQuestionController.cs": slicer.DiffFile(
            "Controllers/AdminQuestionController.cs", text="c" * 10, changed_ranges=[(15, 15)]),
        "Services/QuestionService.cs": slicer.DiffFile(
            "Services/QuestionService.cs", text="c" * 100, changed_ranges=[(22, 22)]),
        "Repositories/QuestionRepository.cs": slicer.DiffFile(
            "Repositories/QuestionRepository.cs", text="c" * 100, changed_ranges=[(21, 21)]),
    }
    slices = slicer.slice_feature(graph, diff_files, cap=150)
    for s in slices:
        assert slicer.slice_char_size(s, diff_files) <= 150
    covered = set().union(*(s.symbols for s in slices))
    changed = slicer.changed_symbols(graph, {p: d.changed_ranges for p, d in diff_files.items()})
    assert changed <= covered
```

- [ ] **Step 6: Run the full unit suite**

Run: `pytest tests/test_slicer.py -v`
Expected: PASS (all tests, including `test_slice_feature_respects_cap`).

- [ ] **Step 7: Commit**

```bash
git add slicer.py tests/test_slicer.py
git commit -m "feat(slicer): size measurement + sub-split-by-sub-root under cap"
```

---

## Task 8: End-to-end validation against the real Quick Duel diff

This task proves the spec's success criteria on real data: **100% diff coverage** and **every slice ≤ cap**. It builds the real symbol graph via `caller_graph.py`, so it needs the tree-sitter deps and a Thabbit clone. It is skipped automatically when either is absent.

**Files:**
- Create: `tests/test_slicer_quickduel.py`

- [ ] **Step 1: Write the integration test**

```python
"""End-to-end slicer validation against the real Thabbit Quick Duel diff.

Skipped unless:
  - caller_graph deps import (tree-sitter), and
  - THABBIT_REPO env var points at a Thabbit clone.

Run on the review server (deps + clone present):
  THABBIT_REPO=/home/ubuntu/ai-review-server/repos/Thabbit \
  QD_BASE=origin/main QD_BRANCH=origin/develop \
  pytest tests/test_slicer_quickduel.py -v -s
"""
import os
import subprocess

import pytest

import slicer

caller_graph = pytest.importorskip("caller_graph")
REPO = os.environ.get("THABBIT_REPO")
if not REPO or not os.path.isdir(REPO):
    pytest.skip("THABBIT_REPO not set to a clone", allow_module_level=True)

BASE = os.environ.get("QD_BASE", "origin/main")
BRANCH = os.environ.get("QD_BRANCH", "origin/develop")


def _quick_duel_diff() -> str:
    # Files touched by any quick-duel commit (union), diffed base...branch.
    log = subprocess.run(
        ["git", "-C", REPO, "log", "-i", "-E",
         "--grep=duel|\\(qd\\)|quick", f"{BASE}..{BRANCH}",
         "--name-only", "--pretty=format:"],
        capture_output=True, text=True, encoding="utf-8", check=True)
    files = sorted({l.strip() for l in log.stdout.splitlines() if l.strip()})
    assert files, "no quick-duel files found; check QD_BASE/QD_BRANCH"
    diff = subprocess.run(
        ["git", "-C", REPO, "diff", f"{BASE}...{BRANCH}", "--", *files],
        capture_output=True, text=True, encoding="utf-8", check=True)
    return diff.stdout


def test_quick_duel_is_fully_covered_and_under_cap():
    graph = caller_graph.get_or_build_graph(REPO)
    diff_files = slicer.parse_unified_diff(_quick_duel_diff())
    assert diff_files, "empty diff"

    slices = slicer.slice_feature(graph, diff_files)

    # 1. Every slice fits the cross-model cap.
    oversized = [(s.root, slicer.slice_char_size(s, diff_files))
                 for s in slices if slicer.slice_char_size(s, diff_files) > slicer.MAX_REVIEW_CHARS]
    assert not oversized, f"slices over cap: {oversized}"

    # 2. Every changed symbol the graph knows about is covered by some slice.
    changed = slicer.changed_symbols(
        graph, {p: d.changed_ranges for p, d in diff_files.items()})
    covered = set().union(*(s.symbols for s in slices)) if slices else set()
    missing = changed - covered
    assert not missing, f"uncovered changed symbols: {sorted(missing)[:20]}"

    print(f"\nQuick Duel -> {len(slices)} slices, "
          f"largest {max(slicer.slice_char_size(s, diff_files) for s in slices):,} chars "
          f"(cap {slicer.MAX_REVIEW_CHARS:,})")
```

- [ ] **Step 2: Run it where deps + clone exist (the review server)**

Run (on the server):
```bash
cd /home/ubuntu/ai-review-server
THABBIT_REPO=/home/ubuntu/ai-review-server/repos/Thabbit \
pytest tests/test_slicer_quickduel.py -v -s
```
Expected: PASS, printing something like `Quick Duel -> 6 slices, largest ~800,000 chars (cap 1,100,000)`. If any slice is over cap, `sub_split` needs a deeper descent — investigate before proceeding to Phase 2.

- [ ] **Step 3: Run it locally (should skip cleanly if deps/clone absent)**

Run: `pytest tests/test_slicer_quickduel.py -v`
Expected: SKIPPED (module-level) when tree-sitter or `THABBIT_REPO` is absent — confirms the guard works.

- [ ] **Step 4: Commit**

```bash
git add tests/test_slicer_quickduel.py
git commit -m "test(slicer): Quick Duel end-to-end coverage + cap validation"
```

---

## Known limitations (acceptable for Phase 1)

- **Classification is best-effort.** `classify_root` reads the def's signature line; class-level markers (`: BackgroundService`, `: Hub`) and attribute lines (`[HttpPost]`) frequently sit on *other* lines, so real roots may classify as `unknown`. This only affects report *labels* — slicing, coverage, and the cap are unaffected. Richer classification (reading the class declaration + attribute lines above the def from source) is deferred to Phase 2, alongside the type-specific checklists that would actually consume the label.
- **Graph edges are name+receiver based** (per `caller_graph.py`), so overloads / same-named methods over-include. Policy is deliberate over-inclusion; the downstream reducer dedups.

## Done criteria for Phase 1

- `pytest tests/test_slicer.py -v` all green.
- On the server, `test_slicer_quickduel.py` shows Quick Duel splitting into N slices, each ≤ `MAX_REVIEW_CHARS`, with 100% changed-symbol coverage.
- `slicer.py` exposes: `parse_unified_diff`, `changed_symbols`, `callees`, `callers`, `is_root`, `classify_root`, `build_slice`, `slice_feature`, `slice_char_size`, `sub_split`, and the `Slice` / `DiffFile` dataclasses — the surface Phase 2 (pipeline wiring) will call.

## Findings from the Quick Duel validation (2026-07-11)

Real-data validation on the review server surfaced two things that shape Phase 2:

1. **The graph MUST be built at the feature branch head, not the base.** The cached graph was at `origin/main` (pre-Quick-Duel), so the feature's ~150 new files were absent and coverage was vacuous (only 27 pre-existing modified symbols mapped). Rebuilt at `origin/develop` (807 files / 3401 symbols): **655 changed symbols, 655 covered, 0 missing, all 966 slices under the 1.1M cap.** Phase 2 must build/refresh the graph at the branch being reviewed. The integration test now calls `build_graph(REPO, BRANCH)` and guards with `len(changed) > 100` to catch a wrong-ref regression.

2. **Slice count needs bin-packing in Phase 2.** The slicer produced **966 slices** (largest 335K chars) — correct (100% coverage, all under cap) but far from the "~6 reviewable chunks" vision. Many changed DTOs/models/enums have no in-repo callers → each becomes its own root/orphan slice. Phase 2 needs a **packing step**: merge small sibling slices up to the cap to minimize the review count (and thus fan-out cost). Slice *correctness* is done here; slice *economy* is Phase 2.

**Validation note:** the prod venv has no `pytest`, so `test_slicer_quickduel.py`'s logic was validated via an equivalent standalone runner using the server venv Python (`build_graph` at branch + `slice_feature` + the same assertions). Locally the test skips (no tree-sitter).

## Handoff to Phase 2 (separate plan)

Phase 2 wires `slice_feature` into the review pipeline: build the graph **at the branch head**, **pack** slices up to the cap, then for each packed `Slice` run the existing Claude+Codex review over the slice's changed files, and feed results into the (also-new) reducer. Written after Phase 1 lands (now) and the Quick Duel numbers are confirmed (done: 655 symbols → 966 slices, all under cap).
