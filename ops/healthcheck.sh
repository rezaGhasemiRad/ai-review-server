#!/usr/bin/env bash
# Health check for the AI Review Server. Run every ~15 min by a systemd
# timer. Alerts to Telegram only on a healthy<->unhealthy transition, so
# one incident produces exactly two messages (down, then recovered) —
# not one every 15 minutes.
#
# Usage:
#   healthcheck.sh           run the checks
#   healthcheck.sh --test    send a test Telegram message and exit
set -u

ALERT_ENV="/home/ubuntu/.ai-review-alerts.env"
STATE_FILE="/home/ubuntu/.ai-review-health-state"
ROOT="/home/ubuntu/ai-review-server"
PUBLIC_URL="https://review.befalta.com/healthz"

# shellcheck disable=SC1090
[ -f "$ALERT_ENV" ] && . "$ALERT_ENV"

send_telegram() {
  local msg="$1"
  [ -n "${TELEGRAM_BOT_TOKEN:-}" ] || { echo "no telegram token; skipping"; return 0; }
  curl -fsS -m 15 \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID:-}" \
    --data-urlencode "text=${msg}" >/dev/null 2>&1 \
    && echo "telegram sent" || echo "telegram send failed"
}

if [ "${1:-}" = "--test" ]; then
  send_telegram "AI Review Server — health alert test ($(hostname)). If you see this, alerting works."
  exit 0
fi

problems=()

# 1. App reachable on localhost
curl -fsS -m 10 http://127.0.0.1:8080/healthz >/dev/null 2>&1 \
  || problems+=("API server not responding on localhost:8080")

# 2. Public HTTPS endpoint (catches Caddy / TLS / proxy faults)
curl -fsS -m 15 "$PUBLIC_URL" >/dev/null 2>&1 \
  || problems+=("public endpoint $PUBLIC_URL unreachable (Caddy/TLS?)")

# 3. Services active
for svc in ai-review-server caddy postgresql; do
  systemctl is-active --quiet "$svc" || problems+=("service '$svc' is not active")
done

# 4. Disk
disk_pct=$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')
[ -n "$disk_pct" ] && [ "$disk_pct" -ge 90 ] \
  && problems+=("root disk ${disk_pct}% full")

# 5 & 6. Job analysis — silent failure + wedged job (via python for JSON)
job_problems=$(python3 - "$ROOT/jobs" <<'PYEOF'
import json, sys, os, glob, datetime
jobs_dir = sys.argv[1]
files = sorted(glob.glob(os.path.join(jobs_dir, "*.json")),
               key=os.path.getmtime, reverse=True)[:30]
now = datetime.datetime.utcnow()
terminal, problems = [], []
for f in files:
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception:
        continue
    st = d.get("status")
    if st in ("complete", "error"):
        terminal.append(st)
    elif st == "running":
        s = (d.get("started_at") or d.get("created_at") or "").replace("Z", "")
        try:
            t = datetime.datetime.fromisoformat(s)
            if (now - t).total_seconds() > 10800:   # >3h
                problems.append("a job has been running >3h (likely wedged)")
        except Exception:
            pass
last3 = terminal[:3]
if len(last3) >= 3 and all(s == "error" for s in last3):
    problems.append("last 3 completed jobs all errored "
                     "(Claude/Codex/git auth likely broken)")
for p in dict.fromkeys(problems):   # dedupe, keep order
    print(p)
PYEOF
)
if [ -n "$job_problems" ]; then
  while IFS= read -r line; do
    [ -n "$line" ] && problems+=("$line")
  done <<< "$job_problems"
fi

# --- transition-based alerting ---
if [ "${#problems[@]}" -eq 0 ]; then
  new_state="healthy"
else
  new_state="unhealthy"
fi
old_state=$(cat "$STATE_FILE" 2>/dev/null || echo "healthy")
host=$(hostname)

if [ "$new_state" != "$old_state" ]; then
  if [ "$new_state" = "unhealthy" ]; then
    body="AI Review Server: UNHEALTHY ($host)"$'\n'
    for p in "${problems[@]}"; do body+="- ${p}"$'\n'; done
    send_telegram "$body"
  else
    send_telegram "AI Review Server: recovered ($host) — all checks pass."
  fi
else
  echo "state unchanged ($new_state)"
fi

echo "$new_state" > "$STATE_FILE"
[ "$new_state" = "healthy" ] && echo "healthy" || printf 'unhealthy: %s\n' "${problems[*]}"
exit 0
