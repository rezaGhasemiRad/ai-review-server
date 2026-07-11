# Seam-Centric Review — Phase 4: Adversarial Verify + Inter-Slice Seam Pass

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** (a) Cut false positives — refute-test the low-confidence (single-model) findings so only survivors ship; (b) catch bugs that live *between* slices — review each cross-slice cut edge using the caller's site + the callee's contract card.

**Architecture:** Two new pieces, both with **injectable LLM functions** so they unit-test without a model. `verify.py` refute-tests findings whose `confidence != "high"` (both-model findings are already high-confidence → skipped). An inter-slice pass turns each cross-slice seam edge into a finding via an edge-review function, tagged as a synthetic `inter-slice` slice so the reducer dedups/ranks it with the rest. The orchestrator runs the inter-slice pass before `reduce()`, then verifies the reduced findings.

**Tech Stack:** Python 3.10+, `pytest`, existing `ai_chat.py` (`call_claude`), `slicer.py`, `reducer.py`, `contract_cards.py`.

**Depends on:** Phases 1–3 (merged to main).

**Reference:** spec §5.4 (adversarial verify) + §5.5 (inter-slice seam pass). Confirmed defaults: refute only low-confidence findings, 3 passes, majority-keep.

---

## File structure
- **Create:** `verify.py` — adversarial verification (pure gate + injectable refute fn).
- **Create:** `interslice.py` — build seam edges + review them (injectable edge fn) → findings.
- **Create:** `tests/test_verify.py`, `tests/test_interslice.py`.
- **Modify:** `ai_chat.py` — orchestrator runs inter-slice pass (appended as a slice_result) then verifies reduced findings; attach `verification` summary to the result.
- **Modify:** `tests/test_sliced_review.py`.

---

## Task 1: `verify.py` — refute low-confidence findings, majority-keep

**Files:** Create `verify.py`, `tests/test_verify.py`

- [ ] **Step 1: Write the failing test**

```python
import verify


def test_verify_skips_high_confidence_and_refutes_low(monkeypatch):
    findings = [
        {"id": "A", "confidence": "high", "title": "both models agree"},
        {"id": "B", "confidence": "low",  "title": "shaky one"},
        {"id": "C", "confidence": "low",  "title": "real one"},
    ]
    # Refute votes: B refuted by 2/3 (drop), C refuted by 1/3 (keep).
    votes = {"B": [True, True, False], "C": [False, False, True]}
    calls = {"n": 0}
    def refute(finding, i):
        calls["n"] += 1
        return votes[finding["id"]][i]

    kept = verify.verify_findings(findings, refute_fn=refute, passes=3)
    ids = [f["id"] for f in kept]
    assert "A" in ids                       # high-confidence never refuted
    assert "C" in ids and "B" not in ids    # C survives, B dropped
    # A skipped (0 refute calls); B and C each got 3 -> 6 total.
    assert calls["n"] == 6
    a = next(f for f in kept if f["id"] == "A")
    assert a["verified"] == "by-agreement"
    c = next(f for f in kept if f["id"] == "C")
    assert c["verified"] == "survived-refutation" and c["refute_votes"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_verify.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'verify'`).

- [ ] **Step 3: Implement `verify.py`**

```python
"""Adversarial verification of review findings.

Findings both models already agreed on are high-confidence and pass through.
Single-model (low-confidence) findings get N independent skeptic passes that
try to REFUTE them; a finding is dropped only if a majority of passes refute
it. Spends verification budget only where confidence is low.
"""
from __future__ import annotations


def _refute_default(finding: dict, i: int) -> bool:
    """One skeptic pass via ai_chat.call_claude. Returns True if it argues the
    finding is NOT real. Imported lazily (tests inject a fake)."""
    import ai_chat
    prompt = ("A reviewer claims this finding. Try to REFUTE it — is it a false "
              "positive? Answer strictly 'REFUTED' or 'STANDS' on the first line, "
              "then one sentence.\n\nFINDING:\n"
              f"{finding.get('title','')}\n{finding.get('detail','') or ''}\n\n"
              f"LOCATION: {finding.get('file','?')}")
    text, _ = ai_chat.call_claude("You are a skeptical code-review verifier.", prompt)
    return text.strip().upper().startswith("REFUTED")


def verify_findings(findings: list[dict], refute_fn=_refute_default,
                    passes: int = 3) -> list[dict]:
    """Return the findings that survive. High-confidence findings pass through
    (annotated verified='by-agreement'). Low-confidence findings are refuted
    `passes` times and kept only if refutations are a minority."""
    majority = passes // 2 + 1
    kept: list[dict] = []
    for f in findings:
        if f.get("confidence") == "high":
            kept.append({**f, "verified": "by-agreement", "refute_votes": 0})
            continue
        votes = sum(1 for i in range(passes) if refute_fn(f, i))
        if votes < majority:
            kept.append({**f, "verified": "survived-refutation", "refute_votes": votes})
        # else: dropped (majority refuted)
    return kept
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_verify.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add verify.py tests/test_verify.py
git commit -m "feat(verify): adversarial refutation of low-confidence findings"
```

---

## Task 2: `interslice.py` — review cross-slice seam edges

**Files:** Create `interslice.py`, `tests/test_interslice.py`

- [ ] **Step 1: Write the failing test**

```python
import interslice


def test_interslice_reviews_each_edge_and_tags_findings():
    # seams: slice "A" calls callees {"B"}; card for B provided.
    seams = {"A": {"B"}, "B": set()}
    cards = {"B": {"returns": "int", "null_behavior": "never", "throws": "none",
                   "side_effects": "writes db"}}
    seen = []
    def edge_review(caller, callee, card):
        seen.append((caller, callee))
        # pretend the caller mishandles B's contract
        return [{"title": f"{caller} ignores {callee} side effects",
                 "severity": "concern", "file": None}]

    findings = interslice.review_seam_edges(seams, cards, edge_review_fn=edge_review)
    assert seen == [("A", "B")]
    assert len(findings) == 1
    assert findings[0]["slice"] == "inter-slice"
    assert findings[0]["found_by"] == ["claude"]     # single-model by default


def test_interslice_no_edges_is_empty():
    assert interslice.review_seam_edges({"A": set()}, {}, edge_review_fn=lambda *a: []) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_interslice.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `interslice.py`**

```python
"""Inter-slice seam pass: review each cross-slice cut edge (a caller in one
slice invoking a callee owned by another) against the callee's contract card.
Catches "each slice was fine alone, but A misuses B" bugs.
"""
from __future__ import annotations


def _edge_review_default(caller: str, callee: str, card: dict) -> list[dict]:
    """Review one edge via ai_chat.call_claude. Returns a (possibly empty) list
    of finding dicts. Imported lazily (tests inject a fake)."""
    import ai_chat, json
    prompt = ("Does the caller correctly use the callee, given the callee's "
              "contract? Report ONLY real handoff bugs as a minified JSON array "
              "of {title, detail, severity}. Empty array if fine.\n\n"
              f"CALLER (slice root): {caller}\nCALLEE: {callee}\n"
              f"CALLEE CONTRACT: {json.dumps(card)}\n")
    text, _ = ai_chat.call_claude("You output only a minified JSON array.", prompt)
    try:
        arr = json.loads(text.strip().strip("`"))
        return arr if isinstance(arr, list) else []
    except Exception:
        return []


def review_seam_edges(seams_by_slice: dict[str, set[str]], cards: dict[str, dict],
                      edge_review_fn=_edge_review_default) -> list[dict]:
    """For every (caller-slice -> callee) cut edge, review the handoff. Findings
    are tagged as the synthetic 'inter-slice' slice, single-model, so the
    reducer folds them in with per-slice findings."""
    out: list[dict] = []
    for caller in sorted(seams_by_slice):
        for callee in sorted(seams_by_slice[caller]):
            for f in edge_review_fn(caller, callee, cards.get(callee, {})) or []:
                out.append({**f, "slice": "inter-slice",
                            "symbol": f.get("symbol") or callee,
                            "category": f.get("category") or "seam",
                            "found_by": ["claude"]})
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_interslice.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add interslice.py tests/test_interslice.py
git commit -m "feat(interslice): review cross-slice seam edges into findings"
```

---

## Task 3: Wire inter-slice pass + verification into the orchestrator

The orchestrator already computes `seams_by_slice` and fetches cards per slice (Phase 3). Extend it to: (1) gather ALL seam callees + their cards once, run the inter-slice pass, and append its findings as a synthetic slice_result; (2) after `reduce()`, run `verify.verify_findings` on the merged findings and store a verification summary.

**Files:** Modify `ai_chat.py`, `tests/test_sliced_review.py`

- [ ] **Step 1: Write the failing test**

```python
def test_sliced_review_runs_interslice_and_verify(monkeypatch, tmp_path):
    monkeypatch.setenv("GRAPH_CACHE_ROOT", str(tmp_path))

    def rv(**kw):
        return {"claude_verdict": "request-changes", "codex_verdict": "approve",
                "claude_findings": [{"file": "svc.cs", "symbol": "A", "category": "bug",
                                     "title": "low-conf finding", "severity": "concern"}],
                "codex_findings": [], "usage": {}}

    monkeypatch.setattr(ai_chat, "_build_graph_at_branch", lambda repo, ref: _two_slice_graph())
    monkeypatch.setattr(ai_chat, "_feature_diff_text",
                        lambda repo, branch, base, only_globs=None, diff_spec=None: DIFF)
    import slicer, contract_cards, interslice, verify
    monkeypatch.setattr(slicer, "cross_slice_callees", lambda g, sls: {s.root: {"B"} for s in sls})
    monkeypatch.setattr(contract_cards, "cards_for_symbols",
                        lambda *a, **k: {"B": {"returns": "int"}})
    # inter-slice finds one seam bug; verify refutes the per-slice low-conf one.
    monkeypatch.setattr(interslice, "review_seam_edges",
                        lambda seams, cards, **k: [{"title": "seam bug", "severity": "concern",
                                                    "symbol": "B", "category": "seam",
                                                    "slice": "inter-slice", "found_by": ["claude"]}])
    monkeypatch.setattr(verify, "verify_findings",
                        lambda findings, **k: [f for f in findings if "seam" in f.get("category","")])

    out = ai_chat.review_branch_sliced_structured(
        repo="/x", branch="develop", base="main", review_fn=rv, pack_target=1)
    cats = [f.get("category") for f in out["findings"]]
    assert "seam" in cats                       # inter-slice finding present + kept
    assert out.get("verification") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sliced_review.py -k interslice_and_verify -v`
Expected: FAIL (inter-slice/verify not wired).

- [ ] **Step 3: Implement in `ai_chat.py`** (inside `review_branch_sliced_structured`)

Add imports `import interslice`, `import verify`. After the per-slice loop builds `slice_results` and before `reduce()`:

```python
    # Inter-slice seam pass: gather all seam callees + cards, review each edge.
    all_seam_syms = set().union(*seams_by_slice.values()) if seams_by_slice else set()
    seam_cards = (contract_cards.cards_for_symbols(card_store, repo, rng["end"], graph, all_seam_syms)
                  if all_seam_syms else {})
    inter = interslice.review_seam_edges(seams_by_slice, seam_cards)
    if inter:
        # Fold in as a synthetic slice so reduce() dedups/ranks together.
        slice_results.append({"slice": "inter-slice", "verdict": "comment",
                              "claude_findings": inter, "codex_findings": []})

    card_store.save()
    reduced = reducer.reduce(label, slice_results)
    # Adversarial verification of the merged findings.
    verified = verify.verify_findings(reduced["findings"])
    dropped = len(reduced["findings"]) - len(verified)
    reduced["findings"] = verified
    reduced["verification"] = {"kept": len(verified), "dropped": dropped}
    # keep claude_findings/codex_findings in sync with the verified set
    reduced["claude_findings"] = [f for f in verified if "claude" in f.get("found_by", [])]
    reduced["codex_findings"] = [f for f in verified if "codex" in f.get("found_by", [])]
```

(Remove the now-duplicated `card_store.save()`/`reduce()` lines that preceded this block.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_sliced_review.py -k interslice_and_verify -v` then full `python -m pytest tests/ -q`.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ai_chat.py tests/test_sliced_review.py
git commit -m "feat(ai_chat): wire inter-slice seam pass + adversarial verify"
```

---

## Task 4: End-to-end on the server (deps-gated, spends tokens)

- [ ] Deploy (`git pull` + restart). Run a multi-slice review (small `pack_target` on the Quick Duel range) so cross-slice seams exist. Confirm: inter-slice findings appear (category `seam`), `verification` summary present (kept/dropped), and low-confidence findings that get refuted are dropped. Watch the token ledger for the added refute + inter-slice phases.

---

## Self-review checklist
- **Spec §5.4:** high-confidence skip + 3-pass majority-keep (Task 1). **§5.5:** cut-edge review with caller + callee card (Task 2), folded into reduce (Task 3).
- **Type consistency:** inter-slice findings carry `found_by`/`symbol`/`category` so `reducer.fingerprint`/`reduce` handle them; `verify_findings` reads `confidence` (set by reduce) — so verify runs AFTER reduce. Order in Task 3: inter-slice → reduce → verify. Correct.

## Known limitations
- **Refute + inter-slice are single-model (Claude)** by default — cheap; a diverse-lens version is a later refinement.
- **Ledger** doesn't yet meter refute/inter-slice calls (they bypass the per-slice `usage` accounting). Follow-up: thread their usage into the ledger under phases `refute`/`inter-slice`.

## Handoff
Phase 4 completes the review-depth track. Remaining program-level items: contract-card adaptive escalation, root-type checklists (v2), live per-phase logging, and the verification/loop-closure tracks (tests, client↔backend, merge gates).
