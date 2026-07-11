# Seam-Centric Review — Phase 2: Fan-out + Reducer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an over-cap feature (e.g. Quick Duel) reviewable cross-model: slice it, pack the slices, review each with the existing pipeline, and reduce the N reports into one fingerprinted, ranked report with an iteration diff and a token ledger.

**Architecture:** A new `ai_chat.review_branch_sliced_structured()` builds the symbol graph **at the branch head**, calls `slicer.slice_feature` + `slicer.pack_slices`, then runs the existing `review_branch_structured(..., only_globs=slice.files)` **sequentially** per packed slice, and passes the per-slice results to a new deterministic `reducer`. `server._run_job` routes to it when the feature exceeds the single-review cap. Confirmed decisions (2026-07-11): new ai_chat function · sequential per-slice · pack to ~70% of cap.

**Tech Stack:** Python 3.10+, `pytest`, existing `slicer.py`, `caller_graph.py`, `ai_chat.py`, `server.py`.

**Depends on:** Phase 1 (`slicer.py`) — merged to main.

**Reference:** `docs/superpowers/specs/2026-07-11-seam-centric-review-design.md` §5.3–5.9, and the Phase 1 findings (graph-at-branch-head; packing needed).

---

## File structure

- **Modify:** `slicer.py` — add `pack_slices()` (bin-pack to a char target, preserve coverage).
- **Create:** `reducer.py` — `fingerprint()`, `reduce()`, `iteration_diff()` (deterministic; no LLM).
- **Create:** `ledger.py` — token accounting accumulator + formatting.
- **Modify:** `ai_chat.py` — add `review_branch_sliced_structured()` (orchestration; injectable review fn for tests) and a synthesis pass hook (stubbed; real synthesis is Phase 4).
- **Modify:** `server.py` — route `_run_job` to the sliced path when the diff exceeds the mixed cap (or `req["sliced"]` is set); persist the token ledger on the job record.
- **Test:** `tests/test_packer.py`, `tests/test_reducer.py`, `tests/test_ledger.py`, `tests/test_sliced_review.py`, `tests/test_sliced_quickduel.py`.

---

## Task 1: `pack_slices` — bin-pack slices to ~70% of the cap

**Files:**
- Modify: `slicer.py`
- Create: `tests/test_packer.py`

- [ ] **Step 1: Write the failing test**

```python
import slicer


def _sl(root, files, syms=None):
    return slicer.Slice(root=root, root_type="unknown",
                        symbols=set(syms or [root]), files=set(files))


def _df(path, n):
    return slicer.DiffFile(path=path, text="x" * n, changed_ranges=[(1, 1)])


def test_pack_slices_merges_up_to_target_and_preserves_coverage():
    diff_files = {f"f{i}.cs": _df(f"f{i}.cs", 300) for i in range(5)}
    slices = [_sl(f"r{i}", [f"f{i}.cs"], [f"s{i}"]) for i in range(5)]
    # target 800 -> each file 300, so up to 2 files (600) per pack, not 3 (900).
    packed = slicer.pack_slices(slices, diff_files, target=800)
    for p in packed:
        assert slicer.slice_char_size(p, diff_files) <= 800
    # Coverage preserved: every original symbol still present.
    covered = set().union(*(p.symbols for p in packed))
    assert covered == {f"s{i}" for i in range(5)}
    # Actually packed (fewer than 5).
    assert len(packed) < 5


def test_pack_slices_keeps_oversized_slice_alone():
    diff_files = {"big.cs": _df("big.cs", 900), "small.cs": _df("small.cs", 100)}
    slices = [_sl("big", ["big.cs"]), _sl("small", ["small.cs"])]
    packed = slicer.pack_slices(slices, diff_files, target=800)
    # big.cs (900) already over target -> stays its own pack (no split here;
    # sub_split handled that upstream). small stays separate or merges only if
    # it fits with another; here it ends up alone too.
    sizes = sorted(slicer.slice_char_size(p, diff_files) for p in packed)
    assert sizes == [100, 900]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_packer.py -v`
Expected: FAIL with `AttributeError: module 'slicer' has no attribute 'pack_slices'`.

- [ ] **Step 3: Implement `pack_slices` in `slicer.py`** (append after `sub_split`)

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_packer.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add slicer.py tests/test_packer.py
git commit -m "feat(slicer): bin-pack slices to a char target, preserving coverage"
```

---

## Task 2: `reducer.fingerprint` — stable per-finding identity

**Files:**
- Create: `reducer.py`
- Create: `tests/test_reducer.py`

Finding dicts (from `ai_chat`) carry at least: `severity`, `file`, `title`, `detail`. The fingerprint keys on **stable identity only** — repo + enclosing symbol (falling back to file) + category + a short hash of the normalized title. **Never** line number, **never** model.

- [ ] **Step 1: Write the failing test**

```python
import reducer


def test_fingerprint_is_stable_across_line_and_model_changes():
    a = {"file": "Services/QuestionService.cs", "symbol": "MoveQuestionsToCategoryAsync",
         "category": "null-handling", "title": "Guid required is a no-op", "line": 472}
    b = dict(a, line=468, found_by="codex")  # different line + model, same bug
    assert reducer.fingerprint("Thabbit", a) == reducer.fingerprint("Thabbit", b)


def test_fingerprint_differs_for_different_bug():
    a = {"file": "svc.cs", "symbol": "Foo", "category": "null-handling", "title": "X"}
    b = {"file": "svc.cs", "symbol": "Foo", "category": "auth", "title": "Y"}
    assert reducer.fingerprint("Thabbit", a) != reducer.fingerprint("Thabbit", b)


def test_fingerprint_is_readable_slug():
    f = {"file": "Services/QuestionService.cs", "symbol": "MoveQuestionsToCategoryAsync",
         "category": "null-handling", "title": "Guid required is a no-op"}
    fp = reducer.fingerprint("Thabbit", f)
    assert fp.startswith("Thabbit:MoveQuestionsToCategoryAsync:null-handling#")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_reducer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'reducer'`.

- [ ] **Step 3: Implement `reducer.py`**

```python
"""Deterministic reduction of per-slice review findings into one report.

No LLM. Dedup by stable fingerprint, roll up verdicts, rank, and diff against
a prior iteration. The narrative/synthesis LLM pass (Phase 4) runs on the
distilled output of this module.
"""
from __future__ import annotations

import hashlib
import re


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def fingerprint(repo: str, finding: dict) -> str:
    """Readable, stable identity slug. Keys on repo + enclosing symbol (or file
    basename) + category + a short hash of the normalized title. Excludes line
    number and model on purpose."""
    symbol = finding.get("symbol") or (finding.get("file", "").rsplit("/", 1)[-1])
    category = finding.get("category") or "general"
    h = hashlib.sha1(_norm(finding.get("title", "")).encode("utf-8")).hexdigest()[:4]
    return f"{repo}:{symbol}:{category}#{h}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_reducer.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add reducer.py tests/test_reducer.py
git commit -m "feat(reducer): stable readable finding fingerprints"
```

---

## Task 3: `reducer.reduce` — dedup, provenance, verdict rollup, ranking

**Files:**
- Modify: `reducer.py`
- Modify: `tests/test_reducer.py`

Input: a list of per-slice results, each `{"slice": <root>, "verdict": <str>, "claude_findings": [...], "codex_findings": [...]}`. Output: one `{"verdict": <rollup>, "findings": [ranked, deduped]}`.

- [ ] **Step 1: Write the failing test**

```python
def test_reduce_dedups_and_records_provenance_and_rolls_up_verdict():
    f = lambda title, cat="null-handling", sym="Foo", sev="concern": {
        "file": "svc.cs", "symbol": sym, "category": cat, "title": title, "severity": sev}
    slice_results = [
        {"slice": "s1", "verdict": "request-changes",
         "claude_findings": [f("Guid required no-op")],
         "codex_findings": [f("Guid required no-op")]},          # same bug, both models
        {"slice": "s2", "verdict": "approve",
         "claude_findings": [f("Guid required no-op")],          # same bug again (shared code)
         "codex_findings": [f("missing tests", cat="test-coverage", sym="Bar")]},
    ]
    out = reducer.reduce("Thabbit", slice_results)
    assert out["verdict"] == "request-changes"   # worst-of
    # The Guid bug is one finding, found_by both, seen in both slices.
    guid = [x for x in out["findings"] if x["category"] == "null-handling"]
    assert len(guid) == 1
    assert set(guid[0]["found_by"]) == {"claude", "codex"}
    assert set(guid[0]["slices"]) == {"s1", "s2"}
    assert guid[0]["confidence"] == "high"        # both models
    # Ranked: high-confidence first.
    assert out["findings"][0]["category"] == "null-handling"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_reducer.py -k reduce -v`
Expected: FAIL with `AttributeError: module 'reducer' has no attribute 'reduce'`.

- [ ] **Step 3: Implement `reduce` in `reducer.py`**

```python
_SEV_RANK = {"bug": 0, "concern": 1, "nit": 2, "": 3}
_VERDICT_RANK = {"request-changes": 0, "comment": 1, "approve": 2, None: 3}


def _rollup_verdict(verdicts: list[str]) -> str:
    present = [v for v in verdicts if v]
    if not present:
        return "comment"
    return min(present, key=lambda v: _VERDICT_RANK.get(v, 3))


def reduce(repo: str, slice_results: list[dict]) -> dict:
    """Dedup findings by fingerprint across slices; record found_by / slices /
    confidence; roll up the verdict (worst-of); rank by severity x confidence."""
    merged: dict[str, dict] = {}
    for sr in slice_results:
        slice_name = sr.get("slice")
        for model in ("claude", "codex"):
            for finding in sr.get(f"{model}_findings", []) or []:
                fp = fingerprint(repo, finding)
                m = merged.get(fp)
                if m is None:
                    m = {**finding, "id": fp, "found_by": set(), "slices": set()}
                    merged[fp] = m
                m["found_by"].add(model)
                m["slices"].add(slice_name)

    findings = []
    for m in merged.values():
        m["found_by"] = sorted(m["found_by"])
        m["slices"] = sorted(m["slices"])
        m["confidence"] = ("high" if len(m["found_by"]) > 1 or len(m["slices"]) > 1
                           else "low")
        findings.append(m)

    findings.sort(key=lambda x: (0 if x["confidence"] == "high" else 1,
                                 _SEV_RANK.get(x.get("severity", ""), 3),
                                 x["id"]))
    return {"verdict": _rollup_verdict([sr.get("verdict") for sr in slice_results]),
            "findings": findings}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_reducer.py -k reduce -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add reducer.py tests/test_reducer.py
git commit -m "feat(reducer): dedup + provenance + verdict rollup + ranking"
```

---

## Task 4: `reducer.iteration_diff` — resolved / present / new

**Files:**
- Modify: `reducer.py`
- Modify: `tests/test_reducer.py`

- [ ] **Step 1: Write the failing test**

```python
def test_iteration_diff_classifies_by_fingerprint():
    prior = {"findings": [{"id": "R:Foo:null#aaaa"}, {"id": "R:Bar:auth#bbbb"}]}
    current = {"findings": [{"id": "R:Foo:null#aaaa"}, {"id": "R:Baz:perf#cccc"}]}
    d = reducer.iteration_diff(current, prior)
    assert d["still_present"] == ["R:Foo:null#aaaa"]
    assert d["resolved"] == ["R:Bar:auth#bbbb"]
    assert d["newly_introduced"] == ["R:Baz:perf#cccc"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_reducer.py -k iteration_diff -v`
Expected: FAIL with `AttributeError: module 'reducer' has no attribute 'iteration_diff'`.

- [ ] **Step 3: Implement `iteration_diff` in `reducer.py`**

```python
def iteration_diff(current: dict, prior: dict | None) -> dict:
    """Classify current findings against a prior reduced report by fingerprint."""
    cur = {f["id"] for f in current.get("findings", [])}
    old = {f["id"] for f in (prior or {}).get("findings", [])}
    return {
        "still_present": sorted(cur & old),
        "resolved": sorted(old - cur),
        "newly_introduced": sorted(cur - old),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_reducer.py -k iteration_diff -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add reducer.py tests/test_reducer.py
git commit -m "feat(reducer): iteration diff (resolved/present/new) by fingerprint"
```

---

## Task 5: `ledger.py` — per-stage/per-slice/per-model token accounting

**Files:**
- Create: `ledger.py`
- Create: `tests/test_ledger.py`

- [ ] **Step 1: Write the failing test**

```python
import ledger


def test_ledger_accumulates_and_reports_by_dimension():
    L = ledger.TokenLedger(window_budget=1_000_000)
    L.add(slice_="s1", model="claude", phase="review",
          usage={"input_tokens": 100, "output_tokens": 20})
    L.add(slice_="s1", model="codex", phase="review",
          usage={"input_tokens": 50, "output_tokens": 10})
    L.add(slice_="s2", model="claude", phase="reconcile",
          usage={"input_tokens": 200, "output_tokens": 40})
    assert L.total_tokens() == 100 + 20 + 50 + 10 + 200 + 40
    assert L.by_model()["claude"] == 100 + 20 + 200 + 40
    assert L.by_slice()["s1"] == 180
    assert round(L.pct_of_window(), 2) == round(420 / 1_000_000 * 100, 2)
    report = L.render()
    assert "claude" in report and "s1" in report
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ledger.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ledger'`.

- [ ] **Step 3: Implement `ledger.py`**

```python
"""Per-stage / per-slice / per-model token ledger for a sliced review."""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class _Entry:
    slice_: str
    model: str
    phase: str
    tokens: int


class TokenLedger:
    def __init__(self, window_budget: int = 0) -> None:
        self.window_budget = window_budget
        self.entries: list[_Entry] = []

    def add(self, slice_: str, model: str, phase: str, usage: dict) -> None:
        toks = int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)
        self.entries.append(_Entry(slice_, model, phase, toks))

    def total_tokens(self) -> int:
        return sum(e.tokens for e in self.entries)

    def _group(self, key) -> dict:
        out: dict = {}
        for e in self.entries:
            out[key(e)] = out.get(key(e), 0) + e.tokens
        return out

    def by_model(self) -> dict:
        return self._group(lambda e: e.model)

    def by_slice(self) -> dict:
        return self._group(lambda e: e.slice_)

    def by_phase(self) -> dict:
        return self._group(lambda e: e.phase)

    def pct_of_window(self) -> float:
        return (self.total_tokens() / self.window_budget * 100) if self.window_budget else 0.0

    def render(self) -> str:
        lines = [f"token ledger: {self.total_tokens():,} tokens "
                 f"({self.pct_of_window():.1f}% of 5h window)"]
        for label, grp in (("by model", self.by_model()),
                           ("by phase", self.by_phase()),
                           ("by slice", self.by_slice())):
            lines.append(f"  {label}: " +
                         ", ".join(f"{k}={v:,}" for k, v in sorted(grp.items())))
        return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ledger.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ledger.py tests/test_ledger.py
git commit -m "feat(ledger): per-stage/slice/model token accounting + render"
```

---

## Task 6: `ai_chat.review_branch_sliced_structured` — orchestration

Designed with an **injectable review function** so it unit-tests without any LLM: the default is `review_branch_structured`, tests pass a fake.

**Prerequisite (do first):** `ai_chat.py` `sys.exit`s at import when `claude`/`codex` are absent, which blocks `import ai_chat` on any box without the CLIs (e.g. a dev laptop / CI). Wrap the module-level `CLAUDE_BIN = _resolve("claude")` / `CODEX_BIN = _resolve("codex")` in `try/except SystemExit` (set both to `None`), and guard `call_claude`/`call_codex` to `raise RuntimeError` if their bin is `None`. Behavior on the server (CLIs present) is unchanged. Also: the orchestrator adds a `pack_target` param (default `int(MAX_REVIEW_CHARS*0.7)`) for tuning + testability, and imports the heavy deps (`caller_graph`, `slicer`, `reducer`, `ledger`) **lazily inside the function** so `ai_chat` stays importable without tree-sitter.

**Files:**
- Modify: `ai_chat.py`
- Create: `tests/test_sliced_review.py`

- [ ] **Step 1: Write the failing test** (with a fake review fn + monkeypatched graph)

```python
import ai_chat
import slicer


def _fake_review(**kw):
    # Echo which files were reviewed so we can assert per-slice dispatch.
    files = tuple(sorted(kw.get("only_globs") or []))
    return {"claude_verdict": "request-changes" if "Services/QuestionService.cs" in files else "approve",
            "codex_verdict": "approve",
            "claude_findings": [{"file": files[0], "symbol": "Foo", "category": "null-handling",
                                 "title": "bug", "severity": "concern"}] if files else [],
            "codex_findings": [],
            "usage": {"claude": {"input_tokens": 10, "output_tokens": 2},
                      "codex": {"input_tokens": 5, "output_tokens": 1}}}


def test_sliced_review_dispatches_per_slice_and_reduces(monkeypatch, tmp_path):
    # Minimal graph + diff so slice_feature yields 2 slices.
    from tests.test_slicer import _index
    # (reuse the fixture-builder pattern; here inline a 2-slice graph)
    graph = {"files": {
        "Services/QuestionService.cs": {"language": "csharp",
            "defs": [{"name": "A", "line_start": 1, "line_end": 9, "signature": "", "enclosing_class": ""}],
            "refs": []},
        "Jobs/Job.cs": {"language": "csharp",
            "defs": [{"name": "B", "line_start": 1, "line_end": 9, "signature": "", "enclosing_class": ""}],
            "refs": []}}}
    graph["symbol_index"] = _index(graph["files"])
    monkeypatch.setattr(ai_chat, "_build_graph_at_branch", lambda repo, branch: graph)

    diff = ("diff --git a/Services/QuestionService.cs b/Services/QuestionService.cs\n"
            "@@ -1,1 +1,3 @@\n+x\n+y\n"
            "diff --git a/Jobs/Job.cs b/Jobs/Job.cs\n"
            "@@ -1,1 +1,3 @@\n+z\n+w\n")
    monkeypatch.setattr(ai_chat, "_feature_diff_text", lambda repo, branch, base, **k: diff)

    out = ai_chat.review_branch_sliced_structured(
        repo="/x", branch="origin/develop", base="origin/main",
        review_fn=_fake_review)
    assert out["verdict"] == "request-changes"          # rolled up from the svc slice
    assert out["findings"], "reduced findings present"
    assert out["token_ledger"]["total_tokens"] > 0
    assert out["slice_count"] >= 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sliced_review.py -v`
Expected: FAIL (`review_branch_sliced_structured` / `_build_graph_at_branch` / `_feature_diff_text` not defined).

- [ ] **Step 3: Implement in `ai_chat.py`**

Add these helpers + the orchestrator. `_build_graph_at_branch` wraps `caller_graph.build_graph`; `_feature_diff_text` shells `git diff base...branch` (optionally restricted, matching existing diff plumbing). `review_branch_sliced_structured` ties Phase 1 + reducer + ledger together.

```python
import caller_graph
import slicer
import reducer
import ledger as _ledger


def _build_graph_at_branch(repo: str, branch: str) -> dict:
    # Phase 1 finding: MUST build at the branch head, not the cached base.
    return caller_graph.build_graph(repo, branch)


def _feature_diff_text(repo: str, branch: str, base: str, only_globs=None) -> str:
    args = ["git", "-C", repo, "diff", f"{base}...{branch}"]
    if only_globs:
        args += ["--", *only_globs]
    import subprocess
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", check=True).stdout


def review_branch_sliced_structured(repo, branch, base="main", *,
                                    review_fn=None, repo_label=None,
                                    prior_result=None, progress_cb=None,
                                    window_budget=0, **common):
    """Slice an over-cap feature, review each packed slice with `review_fn`
    (default review_branch_structured) SEQUENTIALLY, and reduce to one report
    with a token ledger and (if prior_result) an iteration diff."""
    review_fn = review_fn or review_branch_structured
    label = repo_label or repo.rsplit("/", 1)[-1]

    graph = _build_graph_at_branch(repo, branch)
    diff_files = slicer.parse_unified_diff(_feature_diff_text(repo, branch, base))
    raw = slicer.slice_feature(graph, diff_files)
    packed = slicer.pack_slices(raw, diff_files)

    L = _ledger.TokenLedger(window_budget=window_budget)
    slice_results = []
    for i, sl in enumerate(packed, 1):
        if progress_cb:
            progress_cb(f"slice {i}/{len(packed)} ({sl.root_type}) — {len(sl.files)} files")
        try:
            r = review_fn(repo=repo, branch=branch, base=base,
                          only_globs=sorted(sl.files), **common)
        except SystemExit as e:
            # Exhaustion / input error mid-review: record where and re-raise so
            # the server marks the job errored but keeps completed slices.
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


def _worst_slice_verdict(r: dict) -> str:
    order = {"request-changes": 0, "comment": 1, "approve": 2}
    vs = [v for v in (r.get("claude_verdict"), r.get("codex_verdict")) if v]
    return min(vs, key=lambda v: order.get(v, 3)) if vs else "comment"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_sliced_review.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ai_chat.py tests/test_sliced_review.py
git commit -m "feat(ai_chat): review_branch_sliced_structured (slice->review->reduce)"
```

---

## Task 7: Route `server._run_job` to the sliced path

**Files:**
- Modify: `server.py:999-1007` (the single-repo branch of `_run_job`)
- Modify: `tests/test_sliced_review.py` (add a routing unit test around a small helper)

- [ ] **Step 1: Write the failing test for a routing helper**

```python
import server


def test_should_slice_when_diff_over_cap(monkeypatch):
    monkeypatch.setattr(server, "_feature_diff_size", lambda repo, branch, base: 2_000_000)
    assert server._should_slice({}, "/r", "origin/develop", "origin/main") is True
    monkeypatch.setattr(server, "_feature_diff_size", lambda repo, branch, base: 10_000)
    assert server._should_slice({}, "/r", "origin/develop", "origin/main") is False
    # Explicit opt-in always slices.
    assert server._should_slice({"sliced": True}, "/r", "b", "c") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sliced_review.py -k should_slice -v`
Expected: FAIL (`server._should_slice` not defined).

- [ ] **Step 3: Implement the helper + wire the route**

Add to `server.py`:

```python
def _feature_diff_size(repo: str, branch: str, base: str) -> int:
    import subprocess
    out = subprocess.run(["git", "-C", repo, "diff", f"{base}...{branch}"],
                         capture_output=True, text=True, encoding="utf-8", check=True)
    return len(out.stdout)


def _should_slice(req: dict, repo: str, branch: str, base: str) -> bool:
    if req.get("sliced"):
        return True
    try:
        return _feature_diff_size(repo, branch, base) > ai_chat.MAX_REVIEW_CHARS
    except Exception:
        return False
```

Then in `_run_job`, replace the single-repo call (currently `server.py:999-1003`) with:

```python
        if len(prepared) == 1:
            p = prepared[0]
            if _should_slice(req, p["repo"], p["branch"], p["base"]):
                progress("feature over cap — slicing for cross-model review")
                result = ai_chat.review_branch_sliced_structured(
                    repo=p["repo"], branch=p["branch"], base=p["base"],
                    repo_label=_short_repo(p["repo_url"]),
                    prior_result=prior_result, progress_cb=progress,
                    window_budget=OPUS_5H_TOKEN_BUDGET,
                    **{k: v for k, v in common.items()
                       if k not in ("prior_result", "progress_cb")},
                )
            else:
                result = ai_chat.review_branch_structured(
                    repo=p["repo"], branch=p["branch"], base=p["base"], **common,
                )
        else:
            result = ai_chat.review_multi_repo_structured(repos=prepared, **common)
```

Persist the ledger for the API: after `_update(... result=result ...)`, the ledger already rides inside `result["token_ledger"]`, so no extra persistence is needed — but log it:

```python
        if isinstance(result, dict) and result.get("token_ledger"):
            log.info("job %s token ledger: %s", job_id,
                     result["token_ledger"].get("render", ""))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_sliced_review.py -k should_slice -v`
Expected: PASS.

- [ ] **Step 5: Full unit suite + commit**

Run: `python -m pytest tests/ -q` → all green (integration tests skip locally).

```bash
git add server.py tests/test_sliced_review.py
git commit -m "feat(server): route over-cap features to the sliced review path"
```

---

## Task 8: End-to-end Quick Duel sliced review (server, deps-gated)

Proves the whole path: an over-cap feature produces one reduced, fingerprinted report with a token ledger. **Costs real model tokens** — run deliberately on the server.

**Files:**
- Create: `tests/test_sliced_quickduel.py`

- [ ] **Step 1: Write the integration test**

```python
"""Live sliced review of Quick Duel. Deps-gated + opt-in (spends tokens).

Run on the server:
  RUN_LIVE=1 THABBIT_REPO=/home/ubuntu/ai-review-server/repos/Thabbit \
  python -m tests.run_sliced_quickduel   # (a plain runner; prod venv has no pytest)
"""
import os
import pytest

pytest.importorskip("caller_graph")
if os.environ.get("RUN_LIVE") != "1":
    pytest.skip("live token-spending test; set RUN_LIVE=1", allow_module_level=True)

import ai_chat


def test_quick_duel_sliced_review_produces_one_reduced_report():
    repo = os.environ["THABBIT_REPO"]
    out = ai_chat.review_branch_sliced_structured(
        repo=repo, branch="origin/develop", base="origin/main",
        window_budget=ai_chat.__dict__.get("OPUS_5H_TOKEN_BUDGET", 1_580_000),
        progress_cb=print)
    assert out["slice_count"] >= 1
    assert "verdict" in out and "findings" in out
    assert out["token_ledger"]["total_tokens"] > 0
    # Every finding has a stable fingerprint id.
    assert all(f.get("id") for f in out["findings"])
    print(f"\nverdict={out['verdict']} slices={out['slice_count']} "
          f"findings={len(out['findings'])} tokens={out['token_ledger']['total_tokens']:,}")
```

- [ ] **Step 2: Run on the server, guarded** (prod venv has no pytest → use a tiny runner or `python -c`)

On the server, drive `ai_chat.review_branch_sliced_structured(...)` directly with the venv Python and `RUN_LIVE=1`, printing the verdict / slice_count / findings / ledger. Confirm: one report, several slices, non-zero ledger, every finding has an `id`. **This is the moment Quick Duel becomes reviewable cross-model.**

- [ ] **Step 3: Commit**

```bash
git add tests/test_sliced_quickduel.py
git commit -m "test(sliced): live Quick Duel sliced-review end-to-end (opt-in)"
```

---

## Self-review checklist (run before execution)
- **Spec coverage:** §5.3 per-slice review (Task 6) · §5.6 reducer + fingerprints + iteration diff (Tasks 2–4) · §5.7 synthesis (stub hook in Task 6; real pass is Phase 4) · §5.9 token ledger (Task 5, wired Task 6–7) · packing + graph-at-branch (Tasks 1, 6). Adversarial verify + inter-slice pass are **Phase 4** (not here).
- **Type consistency:** `Slice` (Phase 1) reused; reducer input shape `{slice, verdict, claude_findings, codex_findings}` matches what Task 6 emits; ledger `usage` shape matches ai_chat's `{model: {input_tokens, output_tokens}}`.

## Known limitations (acceptable for Phase 2)
- **Synthesis is stubbed** — the LLM narrative/systemic-pattern pass is Phase 4; Phase 2 ships the deterministic ranked report only.
- **Finding `symbol`/`category` fields** may be absent if the reviewer prompt doesn't emit them; `fingerprint` falls back to file basename + "general". Phase 3/4 should tighten the review output schema so fingerprints are maximally stable.
- **Sequential** per confirmed decision — Quick Duel's many slices will be slow; the ledger shows progress. Concurrency is a later optimization.

## Handoff to Phase 3
Phase 3 adds contract cards + seam-context injection into each per-slice `review_fn` call, and tightens the finding schema (`symbol`, `category`) so fingerprints are precise.
