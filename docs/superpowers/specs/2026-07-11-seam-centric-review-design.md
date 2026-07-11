# Seam-Centric Review — Design Spec

- **Date:** 2026-07-11
- **Status:** Approved design (pre-implementation)
- **Track:** Review-depth (stage 1b/1c of the larger "verification gauntlet")
- **Scope:** The AI Review Server (`ai_chat.py` + `server.py`), backend (single-repo) review path.

---

## 1. Problem

The reviewer is **diff-centric**: it ingests raw changed lines and hands them to two models (Claude + Codex) to review independently, then reconcile. This has two coupled failure modes:

1. **Big features are unreviewable cross-model.** Review cost scales with churn, not with what needs checking. The mixed Claude+Codex cap is `MAX_REVIEW_CHARS = 1,100,000` (bounded by Codex's ~400K-token window); Claude-only is `MAX_REVIEW_CHARS_CLAUDE_ONLY = 3,000,000`.
2. **It is structurally weak at seams.** The bugs that matter most in a layered architecture live in the *contracts between units* — controller→service→repo, and "what each layer does with the result." A diff shows *changed lines*, not the full contract on either side of a seam. The existing caller-graph feature (`caller_graph.py`, task #10) is a bolt-on compensating for this.

### Evidence (measured 2026-07-10)

Reviewing the whole **Quick Duel** feature in Thabbit (59 commits, 150 files, ~52K insertions):

```
Diff is still 2,176,183 chars after filtering (raw was 8,022,842, cap is 1,100,000).
```

The whole feature is ~2× over the cross-model cap → it **cannot** get the review we actually want. Today's only options are all bad: chunk it *horizontally by layer* (which cuts straight through the seams we care about), `--summary-only` (shallow), or Claude-only (loses the second opinion — the signal that catches real bugs by agreement).

### Root cause

Horizontal chunking (all repos → all services → all controllers) is the worst possible cut for seam bugs: it slices exactly along the controller→service→repo boundary and reviews each layer blind to the ones it talks to. The size cap and the seam-weakness share one root: a **diff-centric**, not **structure-aware**, reviewer.

---

## 2. Goal

North star: *develop and verify features without worrying about bugs.* This spec covers the **review-depth** track only — making the existing review step catch far more, and dissolving the size cap — by moving from diff-centric to **seam-centric, structure-aware** review built on the symbol graph we already have.

**In scope:** slicing, seam context, per-slice cross-model review, adversarial verification, inter-slice seam pass, deterministic reduction + iteration diffing, token observability.

**Out of scope (later tracks):** test generation + execution, client↔backend cross-repo contract checks, runtime/integration smoke, merge/deploy gates.

---

## 3. Division of labor (the governing principle)

**Code partitions and aggregates deterministically; LLMs judge.**

- **Developer AI (caller):** submits *scope only* — "review feature X (branch/range)". Never thinks about slices.
- **Python (server-side, deterministic):** slicing, seam-context assembly, reduction, fingerprinting, iteration diff, token accounting.
- **LLMs (Claude + Codex):** the actual reviewing (per slice), adversarial verification, and one synthesis pass.

Determinism is not aesthetic — it is what makes reviews reproducible, unit-testable, cheap, and (critically) what keeps the **iteration loop** honest: the same code sliced the same way, findings with stable identities, so fixes can be diffed across iterations.

---

## 4. Pipeline overview

```
scope (branch/range)
   │
   ▼
[build symbol graph]      ── caller_graph.py (exists), 0 tokens
   │
   ▼
[slice]                   ── slicer.py (NEW), 0 tokens
   │   roots → vertical slices → orphan sweep → sub-split → classify
   ▼
[assemble seam context]   ── contract cards (NEW), cached; card gen = LLM (one-time per method)
   │
   ▼
[per-slice review]  ×N    ── existing Claude+Codex pipeline, once per slice
   │
   ▼
[adversarial verify]      ── refute low-confidence findings (LLM)
   │
   ▼
[inter-slice seam pass]   ── review cut edges between slices (LLM, contract-cards only)
   │
   ▼
[reduce]                  ── dedup by fingerprint, verdict rollup, rank, iteration diff (code, 0 tokens)
   │
   ▼
[synthesis]               ── one Opus pass over the distilled list (LLM)
   │
   ▼
final report + token ledger
```

---

## 5. Components

### 5.1 Slicer (`slicer.py`, NEW — deterministic)

**Input:** the diff (changed files + changed symbols) + the symbol graph from `caller_graph.py`.

**Algorithm:**
1. Mark changed symbols.
2. **Find roots** = changed/reachable symbols with **no in-repo callers** (plus framework markers — see classification). These are the framework/runtime-invoked entry points.
3. For each root, **DFS down** the call graph (through changed + adjacent symbols) → the slice's file/symbol set + the list of seams on the path.
4. **Orphan sweep:** any changed symbol not claimed by a slice becomes its own slice (with its callers/callees pulled in as context). **Guarantee: the union of all slices covers 100% of the diff** — slicing can never silently drop a file.
5. **Size guard:** if a slice exceeds `MAX_REVIEW_CHARS`, **sub-split by sub-root** — descend one level and re-root on the slice's children, recursively, until every piece fits. Seams between sub-roots become inter-slice seams.

**Output:** a list of slice specs `{ files, symbols, seams, root_type }`, all under the cap, covering the whole diff.

**Notes / known limits:** the graph resolves edges by symbol name + receiver, not full type resolution — overloads / same-named methods are approximate. Policy: **over-include** (a spurious extra symbol in a slice is harmless; the review and reducer dedup it).

#### Root classification (v1)

After a root is found, label its type via C# markers, for legible reports and (in v2) type-specific checklists:

| Root type | C# markers |
|---|---|
| HTTP endpoint | `[ApiController]` / `: ControllerBase` / `[HttpGet/Post/Put/Delete/...]` |
| SignalR hub method | class `: Hub` |
| Background job | `: BackgroundService` / `IHostedService` / `IJob` (Hangfire/Quartz) |
| Message/event consumer | implements a known handler interface |

Detection of *a* root is graph-only (no in-repo callers); classification only *labels* it. **v1 = classify + one generic seam checklist for all root types. v2 = type-specific checklists** (jobs → idempotency / partial-failure / overlapping-run / poison-record; endpoints → auth / validation / status codes / client contract).

### 5.2 Seam lens — contract cards (NEW)

A **contract card** is a cached, LLM-generated behavioral summary of a public method: what it returns, null behavior, exceptions, side effects. Example: *"returns null when not found; throws InvalidOperationException if soft-deleted; writes to wallet ledger."*

- Stored in the graph cache alongside the symbol graph; **refreshed on file mtime change** (built once, reused across every slice and every review that touches the method).
- **Intra-slice seams:** both sides are already in the slice — the model reads them directly.
- **Cross-slice seams:** inject the **contract card** (not the full callee body). Cheap at review time, semantically rich enough to answer "does it return null?".
- *Deferred (not v1):* adaptive escalation to full callee body for heuristically-risky seams.

### 5.3 Per-slice review

Run the **existing** Claude+Codex pipeline (independent review → reconcile) **once per slice**, feeding the slice's file subset + its seam-context (contract cards for cross-slice seams). Reuses `_run_review_pipeline` and role prompts largely as-is.

### 5.4 Adversarial verification (1c)

Purpose: kill false-positive / hallucinated findings **within a single run**, before they reach the report. (Unrelated to cross-iteration behavior.)

- Findings **both models agree on** → high-confidence, kept, **no refutation**.
- **Single-model** findings → **3 independent skeptic passes**, each prompted to *prove the finding wrong*; **majority-keep** (survives if ≥2 of 3 say "real / can't refute").
- Rationale: spend verification budget only where confidence is low; never second-guess strong (agreed) signals.
- *Status: parameters (pass count, threshold) to be revisited after seeing real results.*

### 5.5 Inter-slice seam pass

Targets seams that cross **between** slices (a caller in slice A, its callee anchoring slice B) — the one place a single slice review can't see both sides.

- For each **cut edge**, assemble `{ caller site + callee contract card }` and ask: *"does the caller's assumption match the callee's actual contract?"*
- Boundaries only (contract cards, not bodies) → cheap.
- Findings flow into the same reducer/fingerprinting as all others.

### 5.6 Reducer (code, deterministic) + fingerprint IDs

Collects all findings (per-slice + inter-slice) and mechanically:

1. **Dedup by fingerprint ID.** The ID is a readable slug derived only from **stable identity** components:
   ```
   thabbit:QuestionService.MoveQuestionsToCategoryAsync:null-handling#a3f9
     repo :        enclosing symbol (location)        : category  # short hash of normalized message
   ```
   - **Never keyed on line number** (lines shift as code is edited → same bug would falsely read as "resolved + new").
   - **Never keyed on the model** (which model found it is volatile provenance, and can be a *set* when both agree).
   - Line number is stored for display only.
2. **Metadata as sibling fields:** `found_by: [claude, codex]`, `severity`, `confidence` (derived from `found_by` × slice count), `slices`, `lines`.
3. **Verdict rollup:** overall = worst-of (any slice `request-changes` → overall `request-changes`).
4. **Rank** by severity × confidence.
5. **Iteration diff** (when `previous_job_id` is set): compute fingerprints for the new run, diff against the prior job's fingerprints →
   - id in old ∩ new → **still present** (not fixed)
   - id in old, not new → **resolved ✓**
   - id in new, not old → **newly introduced**

### 5.7 Synthesis (one Opus pass)

Operates over the **distilled** (deduped, ranked) findings list — never the raw N reports — so it is cheap and cannot re-blow the cap. Single-model on purpose (cross-model signal was already captured per finding). Produces:

- **Systemic patterns across slices** (e.g., "4 of 6 slices don't handle repo-returns-null") — the highest-value output, invisible to per-slice review.
- **Semantic dedup** the fingerprint hash missed (same bug moved to a differently-named method).
- **Contradictions** between slice suggestions.
- **Executive summary** + priority narrative.

### 5.8 Final report shape

```
Executive summary + systemic patterns   ← AI synthesis
Overall verdict                          ← code rollup
Iteration diff (resolved / present / new)← code (when previous_job_id)
Ranked findings (severity × confidence)  ← code; each: id · location · slice(s) · found_by · suggested fix
Per-slice appendix (raw, drill-down)     ← code
Token ledger                             ← code (see 5.9)
```

---

## 5.9 Token observability (first-class requirement)

No budget cap is enforced (we want to observe real behavior) — but every review produces a **detailed token ledger** so it is always answerable *where the tokens went*.

- **Broken down by stage:** contract-card generation · per-slice review · reconcile · refute passes · inter-slice pass · synthesis. (Graph build, slicing, reduce = code → 0 tokens.)
- **Dimensions:** by slice × by model (Claude/Codex) × by phase, with a **running cumulative total** and **% of the 5h Opus window** (`OPUS_5H_TOKEN_BUDGET`).
- **Emitted:** live to the server log as each stage completes; stored on the job record (API-retrievable); plus a final summary in the report.
- **Mid-review exhaustion (no cap → let it run):** on a token/usage/rate failure, mark the job `error` with a reason naming the **slice + phase + model** that ran dry and tokens-consumed-vs-window; **preserve completed slices**; log + Telegram + job record.

---

## 6. Reused vs new

**Reused (largely as-is):**
- `caller_graph.py` — symbol graph, `get_or_build_graph`, `find_refs` (C#/TS/Python via tree-sitter).
- The cross-model review pipeline (`_run_review_pipeline`) — run once per slice.
- `previous_job_id` iteration memory, the HTTP API, Telegram alerts.

**New:**
- `slicer.py` — the graph → slices algorithm (+ orphan sweep, sub-split, classification).
- Contract-card generation + cache.
- Fan-out orchestration over N slices; the deterministic reducer + fingerprinting + iteration diff.
- Adversarial-verify pass; inter-slice seam pass; synthesis pass.
- Token ledger + exhaustion handling.

---

## 7. Build order

1. **`slicer.py` + coverage guarantee** — testable in isolation against Quick Duel: assert slices are all under the cap and cover 100% of the diff. No LLM needed to validate.
2. **Wire slices into the existing pipeline** — Quick Duel goes from "unreviewable" to ~6 cross-reviewed slices. *(Steps 1–2 alone dissolve today's pain.)*
3. **Contract cards + seam-context injection** — the quality jump.
4. **Adversarial verify + inter-slice seam pass** — precision + between-slice bugs.
5. **(v2) Root-type-specific checklists.**

---

## 8. Deferred / open (revisit after real results)

- Adaptive escalation from contract card → full callee body for risky seams.
- Adversarial-verify tuning (pass count, keep/kill threshold).
- Root-type-specific checklists (v2).
- Any budget/throttling policy (intentionally none for now).
- Client↔backend cross-repo contract checks (separate verification track).

---

## 9. Success criteria

- Quick Duel (and any feature over the cap) is reviewable **cross-model** with no human chunking, no silent truncation, 100% diff coverage.
- Slicing is deterministic and unit-tested (same input → same slices).
- Findings carry stable fingerprint IDs; a re-submitted iteration correctly reports resolved / still-present / newly-introduced.
- Every review yields a per-stage, per-slice, per-model token ledger.
- Seam bugs (layer contract mismatches) are surfaced with both sides of the contract in view.
