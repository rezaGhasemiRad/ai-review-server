# Auto-trigger review submission on feature done

Two-pronged mechanism that nudges (later: enforces) AI code review
submission when a feature looks finished.

## How it works

1. **Per-project `CLAUDE.md` "Definition of Done"** — primary path. Each
   project's `CLAUDE.md` tells the coder AI exactly what "done" means
   (submit, poll, fix, resubmit, mark SHA). This is what catches it in
   normal flow.

2. **Global Claude Code `Stop` hook** — backstop. When a session ends
   with a feature-shaped repo state (ahead of base + handoff exists +
   SHA not yet submitted), an advisory message prints. The user sees it
   immediately; the next AI session sees it in the transcript.

Hook is **strictly advisory** for the initial rollout — never blocks,
never auto-submits. Escalation to blocking is a later phase if advisories
keep being ignored.

## One-time machine setup

### Step 1 — Add the env block to `~/.claude/settings.json`

Open `C:\Users\Rad\.claude\settings.json` and merge the `env` keys below
into the existing `env` map (do not replace, merge):

```json
{
  "env": {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "REVIEW_SERVER_API_KEY": "<paste the bearer token here>",
    "REVIEW_SERVER_URL": "https://review.befalta.com"
  }
}
```

The token is the same one the dashboard uses (`REVIEW_SERVER_API_KEY` in
`/home/ubuntu/.ai-review-server.env` on the box). Never commit it.

### Step 2 — Install the Stop hook

Copy `ops/hooks/check-review-submission.sh` from this repo to your
Claude Code hooks dir:

```bash
mkdir -p ~/.claude/hooks
cp ops/hooks/check-review-submission.sh ~/.claude/hooks/
chmod +x ~/.claude/hooks/check-review-submission.sh
```

Then register the hook in `~/.claude/settings.json` by adding a `hooks`
key (alongside the existing `model`, `env`, etc.):

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/check-review-submission.sh"
          }
        ]
      }
    ]
  }
}
```

`matcher` is empty (fires for every Stop event). The hook itself does
the gating — it exits immediately if the repo hasn't opted in.

### Step 3 — Verify

Restart Claude Code (or `/restart`). The hook now runs at the end of
every session, but is a no-op unless all five conditions are met
(see the script header).

## Per-project rollout

For each project you want this to apply to:

1. **Add `.ai-collab/` to the repo** if it doesn't exist:
   ```bash
   mkdir -p .ai-collab/handoffs
   echo "last-submitted-sha" >> .gitignore  # if not already
   git add .ai-collab .gitignore
   git commit -m "chore: opt into AI-Collab review workflow"
   ```

2. **Add the Definition-of-Done section to the project's `CLAUDE.md`.**
   Paste the block from `ops/templates/PROJECT_CLAUDE_MD_DOD.md`,
   replacing the three angle-bracket project-specific values
   (`repo_url`, `base`, project-specific notes).

3. **(Optional) Add `.ai-collab/review-exclude.txt`** with project-noise
   globs the reviewer should skip. See the review-server docs.

4. **Commit and push.** The hook silently no-ops on any branch that
   doesn't meet all conditions (e.g. on `main`, on branches without
   handoffs, on SHAs already marked submitted).

### Suggested order

- **Thabbit** — first, validate the workflow end-to-end here
- **Falta** — second, once Thabbit is smooth

## What the advisory looks like

When the Stop hook fires, the user sees:

```
[AI-Collab review-submission advisory]

Branch 'feat/foo' is 3 commit(s) ahead of 'main', a handoff file exists at
.ai-collab/handoffs/foo.md, but HEAD (abc1234...) has not yet been
submitted to the AI review server.

If this feature is ready for review, submit it with:

  curl -X POST -H "Authorization: Bearer $REVIEW_SERVER_API_KEY" \
       -H "Content-Type: application/json" \
       -d '{"repo_url":"...","branch":"feat/foo","base":"main","feature":"foo"}' \
       "https://review.befalta.com/reviews"

After the POST succeeds, write the SHA so this advisory stops firing:

  git rev-parse HEAD > .ai-collab/last-submitted-sha
```

The next session's AI sees this in the transcript and can act on it.

## Silencing

If you intentionally don't want a branch reviewed (e.g. exploratory
work, a hotfix that goes straight to production by other gates):

```bash
echo "skip" > .ai-collab/last-submitted-sha
```

Any non-matching value silences the advisory until the SHA changes again.

## Later phases (deferred)

- **Auto-submit** instead of advise — once the advisory has proven
  trustworthy across two or three projects. Hook reads
  `$REVIEW_SERVER_API_KEY` and POSTs directly, writes the marker on
  success.
- **Blocking escalation** — Stop hook returns `{"decision": "block",
  "reason": "..."}` JSON when advisory has been ignored ≥3 times for
  the same SHA. Forces the human or AI to either submit or write the
  skip marker.
- **Dedicated `submit-review` skill** — wraps the curl + poll + parse
  loop into a single skill invocation so the AI doesn't have to
  re-derive the command shape each time.
