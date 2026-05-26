# Definition of Done — paste this section into each project's CLAUDE.md

Append the block below to the repo's `CLAUDE.md` (the user-instructions file
Claude Code reads automatically). Replace the three angle-bracket values at
the top with this project's specifics, then commit.

A feature is **not done** until it has been submitted to the AI review
server and the latest review returned `approve` (with Tier-1 tests
passing). This applies to every feature branch — UI, backend, migrations,
docs that ship as part of a feature.

---

```markdown
## Definition of Done (AI Review Server)

This project uses an AI code-review server. A feature is **not** done until
the latest commit on the feature branch has been submitted for review AND
the response is `approve` with passing Tier-1 tests.

**Project-specific values for the submission:**
- `repo_url`: <git URL, e.g. `git@github.com:rezaRadBaramJK/Thabbit.git`>
- `base`: <default integration branch, e.g. `main` or `develop`>
- Review server: `https://review.befalta.com`

**When you finish a feature:**

1. **Write the handoff.** Put a markdown file at
   `.ai-collab/handoffs/<feature-name>.md` describing what changed, what
   you intentionally deferred, what edge cases you considered, and any
   test results. The reviewer reads this — write it like you're briefing a
   peer who hasn't seen your conversation.

2. **Submit for review.** From the feature branch, with `$REVIEW_SERVER_API_KEY`
   set in your environment:

   ```bash
   curl -X POST -H "Authorization: Bearer $REVIEW_SERVER_API_KEY" \
        -H "Content-Type: application/json" \
        -d '{
              "repo_url": "<repo_url from above>",
              "branch": "<current feature branch>",
              "base":   "<base from above>",
              "feature":"<feature-name>"
            }' \
        "$REVIEW_SERVER_URL/reviews"
   ```

   The response includes a `job_id` and `status_url`. Poll the status URL
   every 30s until `status` is `complete` or `error`.

3. **Mark the submission.** Once the POST succeeds (HTTP 202), record the
   SHA you submitted so the Stop hook stops advising:

   ```bash
   git rev-parse HEAD > .ai-collab/last-submitted-sha
   ```

   Don't commit `last-submitted-sha` — it's a local marker. Add
   `.ai-collab/last-submitted-sha` to `.gitignore` if it isn't already.

4. **Read the result.** GET the lean payload (default) for findings; only
   pull `?full=true` if you specifically need the transcript:

   ```bash
   curl -s -H "Authorization: Bearer $REVIEW_SERVER_API_KEY" \
        "$REVIEW_SERVER_URL/reviews/<job_id>" | jq '.result | {
          claude_verdict, codex_verdict,
          claude_findings, codex_findings,
          claude_peer_assessment, codex_peer_assessment
        }'
   ```

5. **Act on the findings.** Use the tier on each finding to decide who
   fixes:
   - **Tier A** → dispatch a Sonnet subagent (`Task` tool, model=sonnet) per fix
   - **Tier B** → fix it yourself (Opus)
   - **Tier C** → surface to the human, don't fix unilaterally

   When all in-scope findings are addressed, write a new handoff entry and
   resubmit (step 2) with `previous_job_id` set to the prior job's id.

6. **Done = both reviewers `approve` AND tests pass.** Until then, keep
   iterating. If a reviewer raises a finding you disagree with, address
   it in the handoff (explain why deferred) — don't just silently ignore.

**Don't** consider a feature "shipped" just because tests pass locally —
the AI review server catches concurrency bugs, contract violations, and
fragile patterns that pass-fail tests don't.

**`.ai-collab/review-exclude.txt`** — if this repo has files the reviewer
should always skip (generated code, vendored libs, fixtures), add them as
globs in `.ai-collab/review-exclude.txt` on the default branch. See the
review-server docs for format. The reviewer applies them automatically.
```
