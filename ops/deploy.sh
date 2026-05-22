#!/usr/bin/env bash
# Safe deploy for the AI Review Server.
#
# A `systemctl restart` kills any in-flight review (the startup
# reconciliation then marks it errored). This script refuses to deploy
# while a job is running/queued, unless --force is given.
#
# Usage:
#   ops/deploy.sh            pull + restart, but only if no job is active
#   ops/deploy.sh --force    pull + restart even if a job is running
set -euo pipefail

ROOT="/home/ubuntu/ai-review-server"
FORCE="${1:-}"

active=$(python3 - "$ROOT/jobs" <<'PYEOF'
import json, sys, os, glob
jobs_dir = sys.argv[1]
n = 0
for f in glob.glob(os.path.join(jobs_dir, "*.json")):
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception:
        continue
    if d.get("status") in ("running", "queued"):
        n += 1
        print(f"  {d['job_id'][:12]}  {d.get('status')}  "
              f"{d.get('feature') or '-'}  {d.get('progress') or ''}",
              file=sys.stderr)
print(n)
PYEOF
)

if [ "$active" -gt 0 ] && [ "$FORCE" != "--force" ]; then
    echo "REFUSING TO DEPLOY: $active job(s) running/queued (shown above)."
    echo "Wait for them to finish, or re-run with --force to deploy anyway"
    echo "(forcing will kill the in-flight review)."
    exit 1
fi

if [ "$active" -gt 0 ]; then
    echo "WARNING: --force given; $active in-flight job(s) will be killed."
fi

cd "$ROOT"
echo "pulling..."
git pull --ff-only
echo "restarting service..."
sudo systemctl restart ai-review-server
sleep 3
systemctl is-active ai-review-server
curl -fsS http://127.0.0.1:8080/healthz && echo
echo "deploy complete"
