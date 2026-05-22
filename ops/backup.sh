#!/usr/bin/env bash
# Nightly backup of the AI Review Server's irreplaceable state to S3.
# Backs up: jobs/ (full review history) + a config bundle (env files).
# Code is in GitHub; Postgres is throwaway; git clones are re-cloneable —
# none of those are backed up.
set -euo pipefail

# Site config — set BACKUP_BUCKET in ~/.ai-review-alerts.env to
# s3://<your-bucket>/ai-review-server (or any S3 prefix you choose).
ALERT_ENV="/home/ubuntu/.ai-review-alerts.env"
# shellcheck disable=SC1090
[ -f "$ALERT_ENV" ] && . "$ALERT_ENV"

BUCKET="${BACKUP_BUCKET:-}"
if [ -z "$BUCKET" ]; then
    echo "BACKUP_BUCKET not set in $ALERT_ENV — refusing to run." >&2
    echo "Add e.g. BACKUP_BUCKET=s3://my-bucket/ai-review-server" >&2
    exit 1
fi

ROOT="/home/ubuntu/ai-review-server"
HOME_DIR="/home/ubuntu"

ts() { date -u +%FT%TZ; }
echo "[$(ts)] backup starting"

# 1. Review history — append-only, so sync without --delete.
aws s3 sync "$ROOT/jobs/" "$BUCKET/jobs/" --only-show-errors
job_count=$(find "$ROOT/jobs" -name '*.json' 2>/dev/null | wc -l)
echo "[$(ts)] jobs/ synced ($job_count job files)"

# 2. Config bundle. Contains secrets (API key, DB creds) — the bucket
#    must stay private. S3 encrypts at rest by default.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
tar -czf "$TMP/config.tar.gz" -C "$HOME_DIR" \
  .ai-review-server.env .ai-collab-test.env
aws s3 cp "$TMP/config.tar.gz" "$BUCKET/config-latest.tar.gz" --only-show-errors
echo "[$(ts)] config uploaded"

echo "[$(ts)] backup complete"
