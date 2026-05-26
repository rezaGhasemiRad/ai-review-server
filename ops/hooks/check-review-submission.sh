#!/usr/bin/env bash
# ai-review-server :: Stop hook — auto-trigger advisor
#
# Fires at the end of a Claude Code session. If the current repo looks like
# a feature branch that's been finished but never submitted to the review
# server, print an advisory message so the human (or the next session)
# knows to submit. Strictly advisory — never blocks, never auto-submits.
#
# All five conditions must be true for the advisory to fire:
#   1. We're inside a git repo
#   2. The repo has a .ai-collab/ dir (opted into the workflow)
#   3. Current branch is NOT the base, AND HEAD is >=1 commit ahead of a
#      recognised base branch (main / develop / master)
#   4. At least one handoff file exists under .ai-collab/handoffs/*.md
#   5. .ai-collab/last-submitted-sha is missing OR != current HEAD sha
#
# Read by Claude Code as a Stop hook. Stdout becomes the advisory the user
# sees. We always exit 0; never set decision=block.

set -e

# Claude Code passes JSON on stdin. We don't need its contents (cwd is
# already correct for the session), so just drain it.
cat >/dev/null 2>&1 || true

# 1. Are we in a git repo?
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

# 2. Has this repo opted in (.ai-collab/ present)?
[ -d ".ai-collab" ] || exit 0

# 3a. Find the base branch. First match wins.
BASE=""
for candidate in main develop master; do
  if git show-ref --verify --quiet "refs/heads/$candidate"; then
    BASE="$candidate"
    break
  fi
done
[ -n "$BASE" ] || exit 0

# 3b. We must be on a feature branch, not the base itself.
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
[ "$CURRENT_BRANCH" != "$BASE" ] || exit 0
[ "$CURRENT_BRANCH" != "HEAD" ] || exit 0  # detached HEAD — skip

# 3c. HEAD strictly ahead of base?
AHEAD=$(git rev-list --count "$BASE..HEAD" 2>/dev/null || echo "0")
[ "$AHEAD" != "0" ] || exit 0

# 4. Any handoff file present?
HANDOFFS=$(find .ai-collab/handoffs -maxdepth 1 -name "*.md" 2>/dev/null || true)
[ -n "$HANDOFFS" ] || exit 0

# 5. Has this exact SHA already been submitted, OR has the user written a
#    silence marker? A valid 40-hex marker means "submitted" — compare for
#    equality. Any non-SHA-shaped content means "intentionally silenced"
#    (e.g. `echo skip > last-submitted-sha`) — silence unconditionally.
HEAD_SHA=$(git rev-parse HEAD)
LAST_MARKER=""
if [ -f ".ai-collab/last-submitted-sha" ]; then
  LAST_MARKER=$(tr -d ' \n\r\t' < .ai-collab/last-submitted-sha)
fi
if [ -n "$LAST_MARKER" ]; then
  if echo "$LAST_MARKER" | grep -qE '^[0-9a-fA-F]{40}$'; then
    # Real SHA — silence only if it matches HEAD.
    [ "$LAST_MARKER" != "$HEAD_SHA" ] || exit 0
  else
    # Non-SHA marker → user-requested silence.
    exit 0
  fi
fi

# All conditions met — emit the advisory.
# Pick the newest handoff for the example command.
LATEST_HANDOFF=$(ls -t .ai-collab/handoffs/*.md 2>/dev/null | head -1 || true)
FEATURE_NAME=""
if [ -n "$LATEST_HANDOFF" ]; then
  FEATURE_NAME=$(basename "$LATEST_HANDOFF" .md)
fi

REPO_URL=$(git config --get remote.origin.url 2>/dev/null || echo "<git-url>")
SERVER_URL="${REVIEW_SERVER_URL:-https://review.befalta.com}"

cat <<EOF
[AI-Collab review-submission advisory]

Branch '$CURRENT_BRANCH' is $AHEAD commit(s) ahead of '$BASE', a handoff
file exists at $LATEST_HANDOFF, but HEAD ($HEAD_SHA) has not yet been
submitted to the AI review server.

If this feature is ready for review, submit it with:

  curl -X POST -H "Authorization: Bearer \$REVIEW_SERVER_API_KEY" \\
       -H "Content-Type: application/json" \\
       -d '{"repo_url":"$REPO_URL","branch":"$CURRENT_BRANCH","base":"$BASE","feature":"$FEATURE_NAME"}' \\
       "$SERVER_URL/reviews"

After the POST succeeds, write the SHA so this advisory stops firing:

  git rev-parse HEAD > .ai-collab/last-submitted-sha

To silence this advisory without submitting (e.g. you intentionally don't
want this branch reviewed), write any non-matching marker:

  echo "skip" > .ai-collab/last-submitted-sha
EOF

exit 0
