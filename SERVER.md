# AI Review Server — Deployment Guide

A self-hosted HTTP service that orchestrates cross-model code reviews. A client AI (e.g. Manus) submits a feature for review; the server checks out the branches, runs Tier-1 tests, asks Claude and Codex to review independently, has them reconcile, and returns structured findings + cost.

This guide walks through standing up a fresh copy from scratch.

## What you'll end up with

```
                          internet
                              │ HTTPS
                              ▼
           ┌──────────────────────────────────────┐
           │  Caddy (TLS via Let's Encrypt)       │
           │  https://review.<your-domain>        │
           └──────────────┬───────────────────────┘
                          │ proxy → 127.0.0.1:8080
           ┌──────────────▼───────────────────────┐
           │  ai-review-server (FastAPI, systemd) │
           │  /reviews, /telegram/webhook, ...    │
           │  Worker thread (one job at a time)   │
           └──┬───────────────┬───────────────┬───┘
              │ subprocess    │ subprocess    │ git
              ▼               ▼               ▼
        claude -p         codex exec       git clone/fetch
        (CLI, OAuth)      (CLI, OAuth)     (SSH key on account)

           ┌────────────────────────────────────────┐
           │ Optional add-ons                       │
           │   nightly S3 backup (jobs/ + config)   │
           │   15-min healthcheck → Telegram alerts │
           │   Telegram /token /rotate commands     │
           └────────────────────────────────────────┘
```

## Prerequisites

Accounts and services:

- **An Ubuntu 22.04+ Linux host** with a public IP, ≥ 4 GB RAM, ≥ 40 GB disk. AWS Lightsail's 4 GB plan is what this guide assumes; any provider works.
- **A domain name** with an A record you can point at the host (for TLS).
- **Anthropic** account with Claude Code (Pro/Max subscription, or API credits).
- **OpenAI** account with Codex CLI access (Plus/Pro subscription, or API credits).
- **GitHub** account that owns (or has access to) the repos that will be reviewed.
- **Telegram** account *(optional but recommended)* — for health alerts and `/token` retrieval from your phone.
- **AWS account with an S3 bucket** *(optional)* — for nightly backups.

The host should have outbound HTTPS to: `api.anthropic.com`, `api.openai.com`, `github.com`, `api.telegram.org`, and your S3 endpoint.

---

## Step 1 — Provision the host

Standard Lightsail Ubuntu 22.04 4 GB instance is fine. The rest of this guide assumes:

- You can SSH in as `ubuntu`
- A **static IP** is attached (so DNS doesn't drift)
- The provider firewall has **22, 80, 443** open to any IPv4
- DNS A record `review.<your-domain>` → the static IP, low TTL during setup

Once the host is reachable, do the rest from inside it (SSH in).

## Step 2 — Base packages

```bash
sudo apt-get update
sudo apt-get install -y \
  python3-pip python3-venv build-essential \
  curl ca-certificates gnupg git unzip

# Node.js 20 (for the Claude + Codex CLIs)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

node --version && python3 --version
```

**Optional, only if you'll run .NET test suites on the box:**
```bash
sudo apt-get install -y dotnet-sdk-6.0 dotnet-sdk-7.0 dotnet-sdk-8.0
# .NET 9 isn't in Ubuntu's apt; install via the official script if needed:
# curl -sSL https://dot.net/v1/dotnet-install.sh | sudo bash -s -- --channel 9.0 --install-dir /usr/lib/dotnet
```

**Optional, only if test suites use Testcontainers / docker:**
```bash
sudo apt-get install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
# log out + back in for the group to take effect
```

**Optional, only if tests need a local Postgres** (vs an external server — see Step 7):
```bash
sudo apt-get install -y postgresql
sudo systemctl enable --now postgresql
```

## Step 3 — Install the Claude + Codex CLIs

```bash
sudo npm install -g @anthropic-ai/claude-code @openai/codex
claude --version
codex --version
```

Now **log in to both.** Two paths:

**A. OAuth (uses your existing subscription).** Run each command and follow the device-code flow (it prints a URL + code; open the URL on any device, paste the code).
```bash
claude /login
codex login
```

**B. API keys.** Set environment variables before launching the server:
```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
```
*(API billing is separate from any Pro/Plus subscription. With a subscription, the OAuth path is cheaper but rate-limits are shared with your other use.)*

Quick smoke test:
```bash
claude -p "Reply with one word: ready"
codex exec --skip-git-repo-check --ephemeral "Reply with one word: ready"
```
Both should print `ready`.

## Step 4 — Clone the server repo

Pick how the host's `git` authenticates to GitHub:

- **Account-level SSH key (one key, all your repos, read+write).** Easiest, single key covers any repo on your account.
- **Per-repo deploy keys** (read-only, one key per repo). More setup; can't be reused across repos.
- **Fine-grained PAT** (HTTPS, scoped + read-only). Best least-privilege.

For the account-key path:
```bash
ssh-keygen -t ed25519 -C "review-server@$(hostname)" -f ~/.ssh/account_key -N ""
cat ~/.ssh/account_key.pub
# Add the printed public key at https://github.com/settings/keys
cat > ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/account_key
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
EOF
chmod 600 ~/.ssh/config

# Verify
ssh -T git@github.com  # should greet you by your username
```

Then clone this repo:
```bash
cd ~
git clone git@github.com:<you>/ai-review-server.git
cd ai-review-server
```

## Step 5 — Python dependencies

```bash
cd ~/ai-review-server
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

## Step 6 — Server config (`~/.ai-review-server.env`)

```bash
KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
umask 077
cat > ~/.ai-review-server.env <<EOF
REVIEW_SERVER_API_KEY=$KEY
REVIEW_SERVER_HOST=0.0.0.0
REVIEW_SERVER_PORT=8080
EOF
chmod 600 ~/.ai-review-server.env
echo "Save this bearer token: $KEY"
unset KEY
```

The bearer token is what client AIs (Manus) send in the `Authorization: Bearer …` header. Store it somewhere (or use the Telegram `/token` command after Step 10 to retrieve it on demand).

## Step 7 — Test databases *(optional, only if Tier-1 tests need one)*

The server's Tier-1 test phase runs `.ai-collab/test.sh` from each repo's feature branch. The script inherits any env vars in `~/.ai-collab-test.env`. Pick one approach:

**A. External shared databases.** Reference connection strings from `test.sh`:
```bash
read -rsp "SQL Server connection string: " MSSQL; echo
read -rsp "Postgres connection string: " PG; echo
umask 077
printf '# External test databases\nTEST_MSSQL_CONNECTION=%s\nTEST_POSTGRES_CONNECTION=%s\n' \
  "$MSSQL" "$PG" > ~/.ai-collab-test.env
chmod 600 ~/.ai-collab-test.env
unset MSSQL PG
```
Then `test.sh` does e.g. `export ConnectionStrings__DefaultConnection="$TEST_MSSQL_CONNECTION"`.

**B. Local Postgres on the box** (lighter; each test run creates its own throwaway DB):
```bash
PW=$(python3 -c "import secrets;print(secrets.token_urlsafe(18))")
cd /tmp && sudo -u postgres psql -c "CREATE USER test_user WITH PASSWORD '$PW' CREATEDB LOGIN;"
umask 077
cat > ~/.ai-collab-test.env <<EOF
PGHOST=127.0.0.1
PGPORT=5432
PGUSER=test_user
PGPASSWORD=$PW
EOF
chmod 600 ~/.ai-collab-test.env
unset PW
```

**C. Testcontainers in the test suites themselves.** Requires Docker (Step 2). Each test project spins up its own DB container. No env file needed; `test.sh` is just `dotnet test` / `npm test`.

**Tradeoff:** external shared DBs are convenient but tests must be written to isolate themselves (the DB is persistent and shared across runs). Testcontainers gives clean per-run isolation but adds dependencies to the test projects. Local Postgres with per-run DB creation is the middle ground.

## Step 8 — Reverse proxy + TLS (Caddy)

```bash
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list > /dev/null
sudo apt-get update && sudo apt-get install -y caddy

sudo tee /etc/caddy/Caddyfile > /dev/null <<EOF
review.<your-domain> {
    encode gzip
    reverse_proxy localhost:8080 {
        header_up X-Real-IP {remote_host}
    }
}
EOF
sudo systemctl reload caddy
```

Caddy fetches a Let's Encrypt cert on first request and renews it automatically. Port **80** must be open (cert challenge) and **443** must be open (HTTPS).

## Step 9 — systemd service

```bash
mkdir -p ~/ai-review-server/logs
sudo tee /etc/systemd/system/ai-review-server.service > /dev/null <<'EOF'
[Unit]
Description=AI Review Server (Claude + Codex orchestrator)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/ai-review-server
EnvironmentFile=/home/ubuntu/.ai-review-server.env
ExecStart=/home/ubuntu/ai-review-server/.venv/bin/python /home/ubuntu/ai-review-server/server.py
Restart=on-failure
RestartSec=5
StandardOutput=append:/home/ubuntu/ai-review-server/logs/out.log
StandardError=append:/home/ubuntu/ai-review-server/logs/err.log

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ai-review-server.service
systemctl is-active ai-review-server.service
curl -fsS http://127.0.0.1:8080/healthz && echo
```

The server is now reachable at `https://review.<your-domain>/healthz`.

## Step 10 — Telegram alerts + `/token` / `/rotate` commands *(optional)*

In the Telegram app:
1. Message **@BotFather**, `/newbot`, get a **bot token**.
2. Message **@userinfobot**, get your numeric **chat ID**.
3. Open your new bot and send `/start` (a bot can't message you until you've messaged it).

On the host:
```bash
read -rsp "Bot token: " BOT; echo
read -rp  "Chat ID: " CHAT
SEC=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
umask 077
cat > ~/.ai-review-alerts.env <<EOF
TELEGRAM_BOT_TOKEN=$BOT
TELEGRAM_CHAT_ID=$CHAT
TELEGRAM_WEBHOOK_SECRET=$SEC
EOF
chmod 600 ~/.ai-review-alerts.env
unset BOT CHAT SEC
sudo systemctl restart ai-review-server  # reload alerts config
```

Install the healthcheck timer (15-min state-transition alerts):
```bash
cd ~/ai-review-server
chmod +x ops/healthcheck.sh
sudo cp ops/ai-review-healthcheck.service ops/ai-review-healthcheck.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-review-healthcheck.timer
~/ai-review-server/ops/healthcheck.sh --test   # should land a Telegram message
```

Register the Telegram webhook (so the bot can receive `/token` and `/rotate`):
```bash
BOT=$(grep ^TELEGRAM_BOT_TOKEN ~/.ai-review-alerts.env | cut -d= -f2-)
SEC=$(grep ^TELEGRAM_WEBHOOK_SECRET ~/.ai-review-alerts.env | cut -d= -f2-)
curl -sS "https://api.telegram.org/bot${BOT}/setWebhook" \
  --data-urlencode "url=https://review.<your-domain>/telegram/webhook" \
  --data-urlencode "secret_token=${SEC}"
curl -sS "https://api.telegram.org/bot${BOT}/getWebhookInfo" | jq .
```

Test from your phone — message the bot `/token` or `/rotate`.

## Step 11 — Nightly S3 backup *(optional)*

In AWS:
1. Create an S3 bucket (any name, private).
2. Create an IAM user with this least-privilege policy (replace `YOUR-BUCKET`):
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       { "Effect": "Allow", "Action": ["s3:ListBucket"],
         "Resource": "arn:aws:s3:::YOUR-BUCKET" },
       { "Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"],
         "Resource": "arn:aws:s3:::YOUR-BUCKET/ai-review-server/*" }
     ]
   }
   ```
3. Create an access key for that user.

On the host:
```bash
sudo apt-get install -y awscli
mkdir -p ~/.aws && cat > ~/.aws/config <<EOF
[default]
region = <bucket-region>
output = json
EOF
chmod 600 ~/.aws/config

read -rp  "AWS Access Key ID: " AK
read -rsp "AWS Secret Access Key: " SK; echo
cat > ~/.aws/credentials <<EOF
[default]
aws_access_key_id = $AK
aws_secret_access_key = $SK
EOF
chmod 600 ~/.aws/credentials
unset AK SK

# Edit ops/backup.sh and set BUCKET="s3://YOUR-BUCKET/ai-review-server"
nano ops/backup.sh

chmod +x ops/backup.sh
sudo cp ops/ai-review-backup.service ops/ai-review-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-review-backup.timer
sudo systemctl start ai-review-backup.service   # run once now to verify
aws s3 ls s3://YOUR-BUCKET/ai-review-server/jobs/
```

## Step 12 — Memory / swap *(optional, recommended on 4 GB)*

If you'll run Tier-1 tests that include heavy builds (Next.js, .NET projects, SQL Server containers), add a swap file as safety net:
```bash
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
sudo sysctl -q vm.swappiness=10
```

## Step 13 — End-to-end smoke test

Pick a small public-ish repo and submit a review:
```bash
KEY=$(grep ^REVIEW_SERVER_API_KEY ~/.ai-review-server.env | cut -d= -f2-)
curl -sS -X POST "https://review.<your-domain>/reviews" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "project": "smoke",
    "feature": "first-run",
    "repo_url": "git@github.com:<you>/<small-repo>.git",
    "branch": "<branch-with-changes>",
    "base": "main",
    "run_tests": false
  }'
```
Poll the returned `status_url` until `complete`. Or watch live in the dashboard at `https://review.<your-domain>/` (enter the bearer token once when prompted).

---

## Operations

**Safe deploys.** Use the included script so a deploy never silently kills an in-flight review:
```bash
cd ~/ai-review-server
./ops/deploy.sh             # refuses if a job is running
./ops/deploy.sh --force     # deploys anyway (kills in-flight review)
```

**Key rotation.** From your phone via Telegram: `/rotate`. From the host:
```bash
NEW=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
sed -i "s|^REVIEW_SERVER_API_KEY=.*|REVIEW_SERVER_API_KEY=$NEW|" ~/.ai-review-server.env
sudo systemctl restart ai-review-server
echo "$NEW"
```

**Logs.**
```bash
sudo journalctl -u ai-review-server -f       # live tail
tail -f ~/ai-review-server/logs/out.log      # info/progress
tail -f ~/ai-review-server/logs/err.log      # errors
```

**Restore from backup.** Spin up a fresh host through Steps 1–9, then:
```bash
aws s3 sync s3://YOUR-BUCKET/ai-review-server/jobs/ ~/ai-review-server/jobs/
aws s3 cp s3://YOUR-BUCKET/ai-review-server/config-latest.tar.gz - | tar -xzC ~/
sudo systemctl restart ai-review-server
```

---

## Giving access to client AIs (Manus or similar)

A client AI submitting reviews needs:
1. **The bearer token** (`REVIEW_SERVER_API_KEY`) — set in the client's env, never in code/prompts.
2. **The prompt** at [`docs/MANUS_PROMPT.md`](./docs/MANUS_PROMPT.md) — instructs the AI on the full review workflow.
3. **Git access to the target repos from the server.** The server's git key (Step 4) must be able to read whatever repos it'll review. If the client works in repos outside your account, you'll need to grant the server's key access (deploy key per repo, or invite as collaborator).

The dashboard at `https://review.<your-domain>/` shows every job, live progress, findings, and cost.

---

## Choices and tradeoffs

**Subscription auth vs API keys.** OAuth uses your subscription (cheaper if you have one) but shares rate limits with your other usage. API keys give isolated billing but cost per token. Codex on subscription doesn't report USD cost; only Claude does — the dashboard's `total_usd` reflects Claude only.

**Account SSH key vs deploy keys vs PAT.** An account-level SSH key has read+write to all your repos — convenient but high blast radius if the host is compromised. Deploy keys are per-repo and can be read-only; PATs (fine-grained) are the cleanest least-privilege option for HTTPS. Pick the level of trust you have in your host.

**Memory on 4 GB.** Manageable for Postgres/Node tests and the model orchestration. SQL Server containers want ~2 GB just for themselves and will swap-thrash on 4 GB. If your projects need SQL Server in tests, prefer an external SQL Server or bump the host to 8 GB.

**Sequential test execution.** The server runs each repo's `test.sh` one at a time, not in parallel — because three parallel `next build`s on 4 GB OOM'd the box in early testing. If you bump to 8+ GB you can re-enable parallelism (server.py: edit the worker test loop).

**Single shared API key.** Currently one bearer token is the only auth. Anyone with it can submit a review against *any* repo this server's git key can read — including all your private repos. There is no per-user scoping. Multi-tenant deployment would need a real auth model (not built).

**The reviewers can't run arbitrary code.** Tier-1 runs the *existing* test suite. The reviewers themselves only read the diff + test output; they can't ask "run this specific test for me." That's a structural limit of the current design.

---

## File map for the operator

```
ai-review-server/
├── server.py                   FastAPI app, worker, API endpoints
├── ai_chat.py                  CLI + the structured review pipeline
├── prompts/review.md           reviewer role prompts (edit to tune behavior)
├── ui/index.html               dashboard
├── HANDOFF_TEMPLATE.md         the per-feature handoff shape
├── docs/MANUS_PROMPT.md        prompt to give to the client AI
├── ops/
│   ├── deploy.sh               safe-deploy (refuses if job in flight)
│   ├── healthcheck.sh          6-check health monitor → Telegram
│   ├── backup.sh               nightly S3 backup
│   ├── ai-review-server… (none — main service unit is inline above)
│   ├── ai-review-healthcheck.{service,timer}
│   └── ai-review-backup.{service,timer}
├── jobs/                       persisted job records (the irreplaceable data)
├── repos/                      managed git clones (re-cloneable)
└── logs/                       systemd-captured stdout/stderr
```
