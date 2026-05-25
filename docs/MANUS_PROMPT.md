# Prompt for the implementing AI (Manus / equivalent)

> Drop-in instructions for the client AI that submits work to this review
> server. Fill in `<owner>`, repo names, `<base>` per repo, and the
> `project`/`feature` labels. Set `REVIEW_SERVER_API_KEY` in the AI's
> runtime env — never inline in the prompt. The dashboard at
> `https://review.<your-domain>/` shows every review live.

---

The feature you were working on is implemented. Before you mark it done, run it through the cross-model review pipeline at `https://review.<your-domain>`. Execute the steps in order. Do not skip steps. Do not declare the feature complete until the review returns no blocking findings (or I explicitly override one).

## 1. Confirm branch state in every repo the feature touches

A feature may span one or several repos, each with its own feature branch and base branch. In each touched repo:
```bash
git status
git rev-parse --abbrev-ref HEAD
git log --oneline <base>..HEAD
```
Verify in each: working tree clean, on the feature branch, the commits are the intended ones, branch is pushed to origin. If any check fails, stop and tell me.

## 2. Ensure each repo has `.ai-collab/test.sh`

The server runs `.ai-collab/test.sh` from each repo's root on the feature branch and feeds the result into the review. **If a repo's feature branch lacks it, create and commit one.** It must exit `0` on success, non-zero on failure.

Test databases are provided as environment variables — **never hardcode connection strings or credentials**:
- `TEST_MSSQL_CONNECTION` — SQL Server connection string
- `TEST_POSTGRES_CONNECTION` — PostgreSQL connection string

**Backend (.NET) needing SQL Server:**
```bash
#!/usr/bin/env bash
set -euo pipefail
cd <project-dir>
export ConnectionStrings__DefaultConnection="$TEST_MSSQL_CONNECTION"
dotnet test --configuration Release --logger "console;verbosity=minimal"
```

**Backend (.NET) needing PostgreSQL** — same, with `$TEST_POSTGRES_CONNECTION`. Adjust the `ConnectionStrings__` key to whatever the app's `appsettings` actually uses.

**Frontend (Next.js), no database:**
```bash
#!/usr/bin/env bash
set -euo pipefail
npm ci --no-audit --no-fund
npm run build
npm test -- --watchAll=false --passWithNoTests
```

**Test isolation — important.** The test databases are *shared and persistent* — every run hits the same database. A test suite that migrates, seeds, or mutates data will leave residue that can break the next run. Each suite must isolate itself: create a uniquely-named database per run and drop it after, wrap each test in a transaction that rolls back, or use unique table/schema prefixes. Do not write tests that mutate shared state and assume a clean slate.

`chmod +x .ai-collab/test.sh`, commit and push it on the feature branch.

## 3. Write / update the handoff file

The handoff lives in **one** repo (the most central — usually the backend), on its feature branch, at:
```
.ai-collab/handoffs/<feature>.md
```
where `<feature>` is the exact `feature` label you'll send in step 4 (e.g. feature `weekly-challenge-feature-flag` → `.ai-collab/handoffs/weekly-challenge-feature-flag.md`). Use a per-feature file — never a shared `HANDOFF.md` — so a stale handoff from a previous feature can't be inherited via a reused branch.

Fill every section concretely:

```markdown
# Handoff: <feature-name>

## Goal
One or two sentences.

## Repositories touched
- `<owner>/<repo>` (branch `<name>`, base `<name>`) — what changed here

## In scope
Concrete bullet list, grouped by repo.

## Deliberately out of scope
Things you did NOT do that a reviewer might expect.

## Design decisions
- **Decision:** ...  **Why:** ...
- **Cross-repo decision:** the contract/schema you settled on and where it's defined.

## Implementation notes — where to look hard
Per repo: migrations, auth, concurrency, anything touching data/money.

## Cross-repo consistency
API contract, auth/permissions, versioning, schema propagation — confirm each.

## Risks & known limitations
Honest list.

## Testing done
- What `.ai-collab/test.sh` covers in each repo
- Manual / end-to-end scenarios you ran
- Gaps

## Changes since last review
ONLY on iterations after the first. Every fix since the previous review pass,
keyed to the finding it addresses, with repo/file. If iteration 1, write
"First review pass."

## Open questions for reviewers
Specific questions.
```

Commit and push the handoff file.

## 4. Submit the review

The bearer token is in `$REVIEW_SERVER_API_KEY` — never put it in code, commits, logs, or chat. If unset, stop and ask me.

**First iteration** — tag it with `project` and `feature` (the `feature` value must match the handoff filename from step 3; on later iterations both are inherited automatically):
```bash
RESP=$(curl -sS -X POST https://review.<your-domain>/reviews \
  -H "Authorization: Bearer $REVIEW_SERVER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "project": "<short project label>",
    "feature": "<feature label — must match .ai-collab/handoffs/<feature>.md>",
    "repos": [
      {"repo_url": "git@github.com:<owner>/<repo-backend>.git", "branch": "<feature>", "base": "<base>"},
      {"repo_url": "git@github.com:<owner>/<repo-admin>.git",   "branch": "<feature>", "base": "<base>"},
      {"repo_url": "git@github.com:<owner>/<repo-web>.git",     "branch": "<feature>", "base": "<base>"}
    ],
    "context": "<optional cross-repo notes>"
  }')
JOB_ID=$(echo "$RESP" | jq -r .job_id)
STATUS_URL=$(echo "$RESP" | jq -r .status_url)
```
**Save `JOB_ID`** — you need it for the next iteration.

A single-repo feature can use the flat shape: `{"project":"...","feature":"...","repo_url":"...","branch":"...","base":"..."}`.

If the response has no `job_id`, surface it and stop. `422` = malformed payload (fix, don't blind-retry); `401`/`403` = token problem (stop). If the server reports the combined diff exceeds the cap, add an `"only"` array of feature-critical path globs, or set `"summary_only": true` for a first pass.

## 5. Poll until complete

```bash
while :; do
  sleep 30
  J=$(curl -sS -H "Authorization: Bearer $REVIEW_SERVER_API_KEY" "$STATUS_URL")
  STATE=$(echo "$J" | jq -r .status)
  echo "  $STATE — $(echo "$J" | jq -r '.progress // ""')"
  case "$STATE" in complete|error) break ;; esac
done
```
Poll no faster than every 30 s. Reviews take 3–15 minutes (tests run first, sequentially per repo, then the two-model review + reconciliation). The `progress` field shows the live phase. If still `running` after 30 minutes, surface it.

## 6. Read the result and decide

`status == "error"` → surface the `error` field and stop. Do not retry the same submission.

`status == "complete"` → the `result` object contains:
- `claude_review`, `codex_review`, `claude_reconcile`, `codex_reconcile` — markdown
- `claude_findings`, `codex_findings` — **structured** arrays of `{severity, repo, file, lines, title, detail}` where `severity` is `bug` / `concern` / `nit`
- `claude_verdict`, `codex_verdict` — `approve` / `request-changes` / `blocking`
- `test_results` — per-repo Tier-1 outcome
- `usage` — token + cost

**Use the structured `findings` arrays as your work list.** Read the two `*_reconcile` fields to resolve disagreements. Classify each finding:

| Pattern | Confidence | Action |
|---|---|---|
| Both reviewers' findings include the same issue | Very high | Fix |
| One reviewer raised it, the other concedes in reconciliation | High | Fix |
| One raised it, the other defends the code in reconciliation | Ambiguous | Surface to me — quote both positions, do NOT decide alone |
| Both verdicts `approve`, only `nit` findings | Safe | Proceed |

**Two finding types are non-negotiable blockers:**
- **Failing Tier-1 tests** — check `result.test_results`; any repo with `status` other than `passed`/`skipped` is a blocking finding regardless of how the diff looks.
- **Cross-repo inconsistencies** — contract/schema/auth drift between repos.

You do not get to dismiss findings as "stylistic" or "out of scope." If your handoff was clear and the finding still landed, it stands. The reviewers were told to treat your handoff as a claim to verify, not ground truth.

## 6b. Classify each finding by fix-complexity tier

You are running on Opus. Before you start fixing, classify each finding into one of three tiers so we route work efficiently — Opus stays focused on the fixes that actually need its judgment, Sonnet absorbs the mechanical ones, and anything that requires a decision I haven't already made comes back to me.

**Tier A — Sonnet-OK (delegate down):**
- Mechanical, locally-scoped, one obvious correct answer.
- Examples: rename, missing null/empty-check on an obvious nullable, typo, missing index or FK on an obvious column, pageSize / clamp / bounds, trim whitespace, `GroupBy.Count` → `COUNT(DISTINCT)`, accessibility attrs, dedupe a helper into a shared module, hard-coded English in a localized component.
- Test: *if I got this wrong, the next review pass would catch it cheaply and the blast radius is small.*

**Tier B — keep on Opus (you fix):**
- High-judgment, cross-cutting, or design-bearing. Worth Opus's attention because a Sonnet attempt might land syntactically clean but semantically wrong in a way the next review wouldn't catch.
- Examples: concurrency / race fixes (isolation levels, transaction boundaries, `SELECT FOR UPDATE`, optimistic locking), cross-repo contract changes that must move in lockstep, auth / permission model changes, migration design with backfill or production-data invariants, refactors that touch architectural patterns.
- **Both reviewers disagreed on it in `*_reconcile`** — that's the strongest Tier B signal. Two-model disagreement is exactly where high-judgment matters.
- **Both reviewers tagged it `blocking`** — default to Tier B regardless of how mechanical it looks.
- Test: *if I got this wrong, it could ship and bite in production before the next review pass would notice.*

**Tier C — ask me (don't fix, don't delegate):**
- The finding requires a decision I haven't made, not a fix I haven't written.
- Examples: the finding contradicts an explicit design decision in the handoff (something has to give — me, the reviewer, or the spec); the reviewer flagged a real issue but its scope is genuinely outside this feature (we may want to defer, not silently fix); a cost/benefit call ("add a safety net here or accept the limitation?"); a reviewer raised a finding both Sonnet and Opus would handle correctly *mechanically* but where the right product behaviour isn't actually settled.
- Test: *the question isn't "how do I fix this" — it's "should this be fixed at all, here, now."*

Apply the heuristic per finding, not per file. One finding can be Tier A while another in the same file is Tier B.

**Report your classification before fixing anything:**

```
Tier A — delegate to Sonnet:
  - [severity][repo:file] <title>
  - ...

Tier B — I (Opus) will fix:
  - [severity][repo:file] <title>
    Reason: <one line — why this is judgment-bearing, not mechanical>
  - ...

Tier C — need your decision:
  - [severity][repo:file] <title>
    Question: <one-sentence, decision-shaped — not "how" but "should we / which way / is this in scope">
  - ...

Ambiguous (your call):
  - [severity][repo:file] <title>
    Reason: <one line — what's unclear about which tier>
```

Wait for me to resolve Tier C and any Ambiguous items before resubmitting. If there are none, you can proceed straight to step 7. When dispatching Tier A, give the Sonnet worker the finding's `title`, `detail`, and `file` verbatim — don't paraphrase.

## 7. Iterate

If there are blocking findings:
1. Fix them in whichever repo(s) are affected.
2. Update `.ai-collab/handoffs/<feature>.md` → `## Changes since last review` — every fix, keyed to its finding, with repo/file.
3. Commit and push every affected repo.
4. **Resubmit, passing the prior `job_id` as `previous_job_id`** (the reviewers then see the prior findings and verify your fixes instead of re-discovering them; `project`/`feature` carry over automatically):
```bash
RESP=$(curl -sS -X POST https://review.<your-domain>/reviews \
  -H "Authorization: Bearer $REVIEW_SERVER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "repos": [ ...same as before... ],
    "previous_job_id": "'"$JOB_ID"'",
    "context": "Iteration N. See handoff Changes-since-last-review."
  }')
JOB_ID=$(echo "$RESP" | jq -r .job_id)
STATUS_URL=$(echo "$RESP" | jq -r .status_url)
```
Always carry the newest `JOB_ID` forward.

Repeat until no blocking findings, or I explicitly accept a flagged finding. Maximum 4 iterations — on a 5th, stop and surface; something deeper is wrong. (I may explicitly authorize one more.)

## 8. Report back

```
Feature: <name>
Repos touched: <list>
Review iterations: <N>   (job_ids: <id1> -> <id2> -> ...)
Tests (final pass): <pass/fail per repo>
Final verdict: clean / accepted-with-overrides
Total review cost: $<sum of usage.total_usd across iterations>
Resolved findings:
  - [severity][repo:file] ...
Findings escalated to you (unresolved):
  - [severity][repo:file] ...
Cross-repo issues caught:
  - ...
Open questions reviewers raised I couldn't answer:
  - ...
```
No long narrative. Do not declare the feature "ready to merge" — that's my call.
