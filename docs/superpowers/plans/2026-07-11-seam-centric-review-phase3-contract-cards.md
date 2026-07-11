# Seam-Centric Review — Phase 3: Contract Cards (Seam Lens) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each per-slice review the *contract* on the far side of every cross-slice seam — a cached, LLM-generated behavioral summary of the callee (returns / null behavior / throws / side effects) — so the reviewer judges the handoff, not just the changed lines.

**Architecture:** A new `contract_cards.py` generates a card per public method **once** (LLM), caches it in the graph-cache dir keyed by file mtime, and renders a markdown block. The sliced orchestrator collects the callees that sit on cross-slice seams, fetches their cards, and injects them into each per-slice review's context. Card generation uses an **injectable LLM function** so everything unit-tests without a model.

**Tech Stack:** Python 3.10+, `pytest`, existing `caller_graph.py`, `ai_chat.py` (`call_claude`), `slicer.py`.

**Depends on:** Phases 1, 2, 2.1 (merged to main).

**Reference:** spec §5.2. Confirmed there: option **C** (cached contract cards), refreshed on file mtime; adaptive full-body escalation deferred.

---

## File structure

- **Create:** `contract_cards.py` — card schema, cache (load/save/staleness), generation (injectable), rendering, and `cards_for_symbols()`.
- **Create:** `tests/test_contract_cards.py`.
- **Modify:** `ai_chat.py` — in `review_branch_sliced_structured`, compute cross-slice seam callees, fetch their cards, and pass a `seam_cards_md` string into each per-slice `review_fn` (as `context` addendum).
- **Modify:** `tests/test_sliced_review.py` — assert seam cards are gathered + passed.

Cache file: `GRAPH_CACHE_ROOT/<repo>/contract-cards.json` (sibling of `symbol-graph.json`).

---

## Task 1: Card schema + cache load/save + staleness

**Files:**
- Create: `contract_cards.py`
- Create: `tests/test_contract_cards.py`

- [ ] **Step 1: Write the failing test**

```python
import json
import contract_cards as cc


def test_cache_roundtrip_and_staleness(tmp_path):
    cache = tmp_path / "contract-cards.json"
    store = cc.CardStore(cache_path=str(cache))
    assert store.get("Foo", mtime=100.0) is None            # empty
    store.put("Foo", mtime=100.0, card={"returns": "null when not found"})
    store.save()

    reloaded = cc.CardStore(cache_path=str(cache))
    assert reloaded.get("Foo", mtime=100.0) == {"returns": "null when not found"}
    # Stale when the source file's mtime moved on.
    assert reloaded.get("Foo", mtime=200.0) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_contract_cards.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'contract_cards'`.

- [ ] **Step 3: Implement the store in `contract_cards.py`**

```python
"""Contract cards — cached, LLM-generated behavioral summaries of public
methods, used as the cross-slice "seam lens" in sliced reviews.

A card answers: what does this method return, does it return null, what does it
throw, what are its side effects. Generated once per method and cached keyed by
the defining file's mtime, so it's cheap at review time.
"""
from __future__ import annotations

import json
import os


CARD_FIELDS = ("returns", "null_behavior", "throws", "side_effects")


class CardStore:
    """mtime-keyed cache of contract cards, persisted as JSON."""

    def __init__(self, cache_path: str) -> None:
        self.cache_path = cache_path
        self._data: dict[str, dict] = {}
        if os.path.exists(cache_path):
            try:
                self._data = json.load(open(cache_path, encoding="utf-8"))
            except Exception:
                self._data = {}

    def get(self, symbol: str, mtime: float) -> dict | None:
        e = self._data.get(symbol)
        if not e:
            return None
        # Fresh only if the source file hasn't changed since the card was made.
        if abs(float(e.get("mtime", -1)) - float(mtime)) > 1e-6:
            return None
        return e.get("card")

    def put(self, symbol: str, mtime: float, card: dict) -> None:
        self._data[symbol] = {"mtime": float(mtime), "card": card}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
        json.dump(self._data, open(self.cache_path, "w", encoding="utf-8"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_contract_cards.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add contract_cards.py tests/test_contract_cards.py
git commit -m "feat(contract-cards): mtime-keyed card cache store"
```

---

## Task 2: Card generation (injectable LLM) + `card_for`

**Files:**
- Modify: `contract_cards.py`
- Modify: `tests/test_contract_cards.py`

- [ ] **Step 1: Write the failing test**

```python
def test_card_for_generates_once_then_serves_cache(tmp_path):
    calls = {"n": 0}

    def fake_llm(symbol, signature, source):
        calls["n"] += 1
        return {"returns": "Category or null", "null_behavior": "null when missing",
                "throws": "none", "side_effects": "none"}

    store = cc.CardStore(cache_path=str(tmp_path / "c.json"))
    card1 = cc.card_for(store, symbol="GetCategoryByIdAsync",
                        signature="Task<Category?> GetCategoryByIdAsync(Guid id)",
                        source="public async Task<Category?> ... { return await ...; }",
                        mtime=100.0, generate_fn=fake_llm)
    assert card1["null_behavior"] == "null when missing"
    assert calls["n"] == 1
    # Second call with same mtime -> served from cache, no re-generation.
    cc.card_for(store, symbol="GetCategoryByIdAsync", signature="x", source="y",
                mtime=100.0, generate_fn=fake_llm)
    assert calls["n"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_contract_cards.py -k card_for -v`
Expected: FAIL (`card_for` not defined).

- [ ] **Step 3: Implement `card_for` + the default generator in `contract_cards.py`**

```python
_CARD_PROMPT = (
    "You are documenting a method's CONTRACT for a code reviewer. Read the "
    "method and answer ONLY as minified JSON with keys: returns, null_behavior, "
    "throws, side_effects. Be terse and factual; if unknown, say \"unknown\". "
    "No prose outside the JSON.\n\nSIGNATURE:\n{sig}\n\nSOURCE:\n{src}\n"
)


def default_generate(symbol: str, signature: str, source: str) -> dict:
    """Generate a card via ai_chat.call_claude. Imported lazily so this module
    stays usable without the CLI (tests inject a fake)."""
    import json as _json
    import ai_chat
    prompt = _CARD_PROMPT.format(sig=signature or symbol, src=source or "")
    text, _usage = ai_chat.call_claude("You output only minified JSON.", prompt)
    try:
        obj = _json.loads(text.strip().strip("`"))
    except Exception:
        obj = {}
    return {k: str(obj.get(k, "unknown")) for k in CARD_FIELDS}


def card_for(store: "CardStore", symbol: str, signature: str, source: str,
             mtime: float, generate_fn=default_generate) -> dict:
    """Return a fresh cached card or generate+cache one."""
    cached = store.get(symbol, mtime)
    if cached is not None:
        return cached
    card = generate_fn(symbol, signature, source)
    store.put(symbol, mtime, card)
    return card
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_contract_cards.py -k card_for -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add contract_cards.py tests/test_contract_cards.py
git commit -m "feat(contract-cards): generate-once card_for with injectable LLM"
```

---

## Task 3: `cards_for_symbols` (graph-driven) + `render_cards_md`

Given a graph and a set of callee symbol names, resolve each one's defining file/signature/source and mtime, build/fetch its card, and render a compact markdown block for injection.

**Files:**
- Modify: `contract_cards.py`
- Modify: `tests/test_contract_cards.py`

- [ ] **Step 1: Write the failing test**

```python
def _graph_with_def():
    d = {"name": "GetCategoryByIdAsync", "file": "Repositories/QuestionRepository.cs",
         "line_start": 10, "line_end": 15, "signature": "Task<Category?> GetCategoryByIdAsync(Guid id)",
         "enclosing_class": "QuestionRepository"}
    return {"symbol_index": {"GetCategoryByIdAsync": {"defs": [d], "refs": []}}}


def test_cards_for_symbols_and_render(tmp_path, monkeypatch):
    graph = _graph_with_def()
    store = cc.CardStore(cache_path=str(tmp_path / "c.json"))

    # Stub source + mtime resolution (no real git/fs).
    monkeypatch.setattr(cc, "_source_and_mtime",
                        lambda repo, ref, file, ls, le: ("SRC", 100.0))
    gen = lambda s, sig, src: {"returns": "Category or null", "null_behavior": "null when missing",
                               "throws": "none", "side_effects": "none"}

    cards = cc.cards_for_symbols(store, "/repo", "REF", graph,
                                 {"GetCategoryByIdAsync"}, generate_fn=gen)
    assert "GetCategoryByIdAsync" in cards
    md = cc.render_cards_md(cards)
    assert "GetCategoryByIdAsync" in md and "null when missing" in md
    assert md.startswith("### Seam contracts")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_contract_cards.py -k cards_for_symbols -v`
Expected: FAIL (`cards_for_symbols` not defined).

- [ ] **Step 3: Implement in `contract_cards.py`**

```python
import subprocess


def _source_and_mtime(repo: str, ref: str, file: str, ls: int, le: int) -> tuple[str, float]:
    """Method source (line range at `ref`) and a change key. Uses the file's
    last-commit epoch at `ref` as the mtime proxy (stable across clones,
    unlike filesystem mtime)."""
    blob = subprocess.run(["git", "-C", repo, "show", f"{ref}:{file}"],
                          capture_output=True, text=True, encoding="utf-8")
    lines = blob.stdout.splitlines()
    src = "\n".join(lines[max(ls - 1, 0):le])
    ts = subprocess.run(["git", "-C", repo, "log", "-1", "--format=%ct", ref, "--", file],
                        capture_output=True, text=True, encoding="utf-8")
    mtime = float(ts.stdout.strip() or 0)
    return src, mtime


def cards_for_symbols(store: "CardStore", repo: str, ref: str, graph: dict,
                      symbols: set[str], generate_fn=default_generate) -> dict[str, dict]:
    out: dict[str, dict] = {}
    idx = graph.get("symbol_index") or {}
    for sym in sorted(symbols):
        defs = (idx.get(sym) or {}).get("defs") or []
        if not defs:
            continue
        d = defs[0]
        src, mtime = _source_and_mtime(repo, ref, d["file"], d["line_start"], d["line_end"])
        out[sym] = card_for(store, sym, d.get("signature", ""), src, mtime, generate_fn)
    return out


def render_cards_md(cards: dict[str, dict]) -> str:
    if not cards:
        return ""
    lines = ["### Seam contracts (callees on the boundary of this slice)", ""]
    for sym in sorted(cards):
        c = cards[sym]
        lines.append(f"- **{sym}** — returns: {c.get('returns','unknown')}; "
                     f"null: {c.get('null_behavior','unknown')}; "
                     f"throws: {c.get('throws','unknown')}; "
                     f"side effects: {c.get('side_effects','unknown')}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_contract_cards.py -k cards_for_symbols -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add contract_cards.py tests/test_contract_cards.py
git commit -m "feat(contract-cards): graph-driven cards_for_symbols + render"
```

---

## Task 4: Cross-slice seam callees in the slicer

Add a pure helper that, given the packed slices, returns for each slice the set of callee symbols it calls that are defined **outside** that slice (its cross-slice seams).

**Files:**
- Modify: `slicer.py`
- Modify: `tests/test_slicer.py`

- [ ] **Step 1: Write the failing test**

```python
def test_cross_slice_callees(graph):
    # Two slices: endpoint path and job path. Force them apart with tiny cap so
    # they don't pack together, then a synthetic cross-call.
    # Here MoveQuestions' slice calls into a symbol owned by another slice.
    s_endpoint = slicer.build_slice(graph, "MoveQuestions")
    s_job = slicer.build_slice(graph, "ExecuteAsync")
    # Pretend the endpoint slice also references BulkInsert (owned by job slice).
    s_endpoint.symbols.discard("BulkInsert")
    seams = slicer.cross_slice_callees(graph, [s_endpoint, s_job])
    # No cross edges in the fixture by default -> empty sets.
    assert seams[s_endpoint.root] == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_slicer.py -k cross_slice -v`
Expected: FAIL (`cross_slice_callees` not defined).

- [ ] **Step 3: Implement in `slicer.py`**

```python
def cross_slice_callees(graph: dict, slices: list[Slice]) -> dict[str, set[str]]:
    """For each slice, the callee symbols it invokes that are defined in a
    DIFFERENT slice (its cross-slice seams). Symbols not owned by any slice are
    ignored (they're context, not seams)."""
    owner: dict[str, str] = {}
    for s in slices:
        for sym in s.symbols:
            owner.setdefault(sym, s.root)
    out: dict[str, set[str]] = {}
    for s in slices:
        seam: set[str] = set()
        for sym in s.symbols:
            for callee in callees(graph, sym):
                if owner.get(callee) not in (None, s.root):
                    seam.add(callee)
        out[s.root] = seam
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_slicer.py -k cross_slice -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add slicer.py tests/test_slicer.py
git commit -m "feat(slicer): cross_slice_callees (per-slice seam callees)"
```

---

## Task 5: Inject seam cards into per-slice reviews (orchestrator)

**Files:**
- Modify: `ai_chat.py` (`review_branch_sliced_structured`)
- Modify: `tests/test_sliced_review.py`

- [ ] **Step 1: Write the failing test**

```python
def test_sliced_review_injects_seam_cards(monkeypatch, tmp_path):
    seen_context = []

    def capture_review(**kw):
        seen_context.append(kw.get("context") or "")
        return {"claude_verdict": "approve", "codex_verdict": "approve",
                "claude_findings": [], "codex_findings": [], "usage": {}}

    monkeypatch.setattr(ai_chat, "_build_graph_at_branch", lambda repo, ref: _two_slice_graph())
    monkeypatch.setattr(ai_chat, "_feature_diff_text",
                        lambda repo, branch, base, only_globs=None, diff_spec=None: DIFF)
    # Force a seam + a stub card so no LLM/git is needed.
    import slicer, contract_cards
    monkeypatch.setattr(slicer, "cross_slice_callees",
                        lambda g, sls: {s.root: {"B"} for s in sls})
    monkeypatch.setattr(contract_cards, "cards_for_symbols",
                        lambda *a, **k: {"B": {"returns": "int", "null_behavior": "never",
                                              "throws": "none", "side_effects": "writes db"}})

    ai_chat.review_branch_sliced_structured(
        repo="/x", branch="develop", base="main",
        review_fn=capture_review, pack_target=1, context="orig")
    # Each slice's context carries the original note + the seam-card block.
    assert any("Seam contracts" in c and "writes db" in c for c in seen_context)
    assert all(c.startswith("orig") for c in seen_context)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sliced_review.py -k seam_cards -v`
Expected: FAIL (cards not injected; `context` unchanged).

- [ ] **Step 3: Implement in `ai_chat.py`**

In `review_branch_sliced_structured`, after `packed = slicer.pack_slices(...)` and before the loop, compute seams and a per-slice card block; append it to that slice's `context`. Lazy-import `contract_cards`; use a `CardStore` in the graph-cache dir.

```python
    import contract_cards
    import caller_graph as _cg
    seams_by_slice = slicer.cross_slice_callees(graph, packed)
    store = contract_cards.CardStore(
        cache_path=str(_cg.GRAPH_CACHE_ROOT / repo.rsplit("/", 1)[-1] / "contract-cards.json"))
    base_context = common.pop("context", None)
    ...
    for i, sl in enumerate(packed, 1):
        seam_syms = seams_by_slice.get(sl.root) or set()
        cards = contract_cards.cards_for_symbols(store, repo, rng["end"], graph, seam_syms) \
            if seam_syms else {}
        cards_md = contract_cards.render_cards_md(cards)
        slice_context = "\n\n".join(x for x in (base_context, cards_md) if x) or None
        ...
        r = review_fn(repo=repo, branch=branch, base=base,
                      only_globs=sorted(sl.files), context=slice_context,
                      since_commit=since_commit, to_commit=to_commit,
                      narrow_to_recent_commits=narrow, **common)
    store.save()
```

(Ensure `context` is popped from `common` so it isn't passed twice.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_sliced_review.py -k seam_cards -v`
Expected: PASS. Then full suite: `python -m pytest tests/ -q`.

- [ ] **Step 5: Commit**

```bash
git add ai_chat.py tests/test_sliced_review.py
git commit -m "feat(ai_chat): inject cross-slice contract cards into per-slice reviews"
```

---

## Task 6: End-to-end on the server (deps-gated, spends tokens)

- [ ] **Step 1:** Deploy to the server (`git pull` + restart — tree is clean).
- [ ] **Step 2:** Re-run the Quick Duel range review (job like Task 8). Because Quick Duel packs to a single slice, there are typically **no cross-slice seams**, so cards may be empty — that's correct. To exercise cards, run with a small `pack_target` (forces multiple slices → real cross-slice seams → cards generated) via a scoped runner, and confirm: `contract-cards.json` is written, a second run serves from cache (0 new card-gen tokens), and the review context contains a "Seam contracts" block.
- [ ] **Step 3:** Confirm cache reuse: run twice; second run's card-generation token count is 0 (all cached).

---

## Self-review checklist
- **Spec §5.2 coverage:** cached cards (Tasks 1–2) · graph-driven + render (Task 3) · mtime refresh (Task 1, via `_source_and_mtime` commit-epoch key) · cross-slice injection (Tasks 4–5). Adaptive full-body escalation deferred (spec says v-later).
- **Type consistency:** `Slice` reused; `cards_for_symbols` returns `{symbol: card}`; `render_cards_md` consumes it; orchestrator passes `context` (string) — matches `review_branch_structured(context=...)`.

## Known limitations (acceptable for Phase 3)
- **Single-slice features get no cards** (no cross-slice seams). Intra-slice seams are already fully visible in-context, so this is correct — cards matter once a feature spans multiple slices (or in Phase 4's inter-slice pass).
- **mtime proxy = last-commit epoch at ref** (not filesystem mtime), so cards invalidate when the defining file changes between reviewed refs — stable across clones.
- **Card quality depends on the callee's own source being in the graph**; overloads/same-name resolve to the first def (Phase 1 imprecision).

## Handoff to Phase 4
Phase 4 (adversarial verify + inter-slice seam pass) reuses `cross_slice_callees` + contract cards: the inter-slice pass reviews each cut edge using `{caller site + callee card}`, and adversarial verify refute-tests the low-confidence findings this pipeline produces.
