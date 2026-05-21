# Review Server — deployment & API

HTTP service that lets a remote agent (e.g. Manus) trigger a Claude + Codex review of a feature branch. Async + poll model: the agent submits a job and polls a status URL until the review finishes.

## Architecture

```
Manus AI                          Review Server (Windows Server)
   │                                       │
   │  1. POST /reviews                     │
   │     Authorization: Bearer <key>       │
   │     { repo_url, branch, base, ... }  │
   │ ─────────────────────────────────────▶│
   │                                       │  ┌────────────────────────┐
   │  2. 202 Accepted                      │  │ git clone / fetch      │
   │     { job_id, status_url }           │  │ git diff base...branch │
   │ ◀─────────────────────────────────── │  │ Claude review          │
   │                                       │  │ Codex review           │
   │  3. GET /reviews/{job_id}             │  │ Reconciliation         │
   │     ... (polls every 30s)            │  └────────────────────────┘
   │ ◀─────────────────────────────────── │
   │  4. status: complete                  │
   │     { result: {...} }                 │
```

## Prerequisites

On the Windows Server:

1. **Python 3.10+** on PATH (`python --version`)
2. **Git for Windows** on PATH (`git --version`) — needs network access to GitHub
3. **Claude Code CLI** installed and logged in (`claude --version`)
4. **OpenAI Codex CLI** installed and logged in (`codex --version`)
5. Outbound HTTPS to `api.anthropic.com`, `api.openai.com`, and `github.com`

## One-time install

```powershell
cd C:\projects\AiWorkflow\Firstverion-AisComunicate
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configuration

Set environment variables (use System Properties → Environment Variables for persistence, or a `.env`-like shim):

| Variable | Purpose | Default |
|---|---|---|
| `REVIEW_SERVER_API_KEY` | Shared secret bearer token. **Required.** Use ≥32 random chars. | — |
| `REVIEW_SERVER_HOST` | Bind address | `0.0.0.0` |
| `REVIEW_SERVER_PORT` | TCP port | `8080` |

Generate a strong key (PowerShell):
```powershell
[Convert]::ToBase64String((1..32 | %{ Get-Random -Maximum 256 }))
```

## Running

**Quick start (foreground, for testing):**
```powershell
.venv\Scripts\Activate.ps1
$env:REVIEW_SERVER_API_KEY = "<paste-key>"
python server.py
```

**Production — as a Windows service via NSSM:**
1. Install [NSSM](https://nssm.cc/).
2. Register the service:
   ```cmd
   nssm install AIReviewServer "C:\projects\AiWorkflow\Firstverion-AisComunicate\.venv\Scripts\python.exe" "C:\projects\AiWorkflow\Firstverion-AisComunicate\server.py"
   nssm set AIReviewServer AppDirectory "C:\projects\AiWorkflow\Firstverion-AisComunicate"
   nssm set AIReviewServer AppEnvironmentExtra "REVIEW_SERVER_API_KEY=<paste-key>"
   nssm set AIReviewServer AppStdout "C:\projects\AiWorkflow\Firstverion-AisComunicate\logs\out.log"
   nssm set AIReviewServer AppStderr "C:\projects\AiWorkflow\Firstverion-AisComunicate\logs\err.log"
   nssm start AIReviewServer
   ```

**TLS (recommended for anything beyond localhost):**
Put the service behind a reverse proxy that terminates HTTPS — IIS with URL Rewrite + ARR, Caddy, or nginx. The FastAPI app itself speaks plain HTTP.

**Firewall:** open the listening port (default 8080) only to Manus's source IP if possible.

## Private repo access

For GitHub private repos, the server's `git` needs credentials:

- **HTTPS + PAT** (simplest): include the token in the URL stored in your client (NOT in the request body):
  - Configure Git Credential Manager on the server, then `git clone` once manually so credentials are cached.
- **SSH key**: place a deploy key on the server, use `git@github.com:...git` URLs.

The server runs `git clone --no-checkout` on first use and `git fetch --all --prune` on subsequent calls; both inherit the server's git credentials.

## API

All `/reviews*` endpoints require:
```
Authorization: Bearer <REVIEW_SERVER_API_KEY>
```

### `POST /reviews` — submit a review job

Request body:
```json
{
  "repo_url": "https://github.com/owner/repo.git",
  "branch": "feature/foo",
  "base": "develop",
  "only": ["src/**/*.cs"],
  "include_all": false,
  "summary_only": false,
  "handoff_path": null,
  "no_handoff": false,
  "context": "Focus on auth and migrations."
}
```

| Field | Type | Required | Notes |
|---|---|:--:|---|
| `repo_url` | string | yes | HTTPS or SSH git URL, must end in `.git` |
| `branch` | string | yes | branch name on the remote (no `origin/` prefix) |
| `base` | string | no | default `main` |
| `only` | string[] | no | glob filter for paths to review |
| `include_all` | bool | no | disable default noise filter |
| `summary_only` | bool | no | skip the diff, review commits + file stats only |
| `handoff_path` | string | no | override handoff auto-discovery |
| `no_handoff` | bool | no | disable handoff loading entirely |
| `context` | string | no | freeform instructions for the reviewers |

Response: `202 Accepted`
```json
{
  "job_id": "5f2a...",
  "status": "queued",
  "status_url": "https://server/reviews/5f2a..."
}
```

### `GET /reviews/{job_id}` — poll for status / result

Status values: `queued` → `running` → `complete` | `error`.

When `complete`, the response includes:
```json
{
  "job_id": "5f2a...",
  "status": "complete",
  "created_at": "2026-05-21T16:55:00Z",
  "started_at": "2026-05-21T16:55:02Z",
  "completed_at": "2026-05-21T16:58:14Z",
  "request": { ...echoed... },
  "result": {
    "label": "branch origin/feature/foo vs origin/develop",
    "handoff_source": "origin/feature/foo:.ai-collab/HANDOFF.md",
    "claude_review": "...markdown...",
    "codex_review": "...markdown...",
    "claude_reconcile": "...markdown...",
    "codex_reconcile": "...markdown...",
    "transcript_path": "C:/.../transcripts/2026-05-21_165814_review.md",
    "transcript_md": "...full transcript as markdown..."
  }
}
```

When `error`:
```json
{ "status": "error", "error": "git fetch failed: ..." }
```

### `GET /reviews?limit=20` — list recent jobs

Lightweight summary list, newest first.

### `GET /healthz` — liveness probe (no auth)

Returns `{"status": "ok"}`.

## Example: Manus client (curl, then poll)

```bash
# Submit
RESP=$(curl -sS -X POST https://review.internal/reviews \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "repo_url": "https://github.com/rezaRadBaramJK/Thabbit.git",
    "branch": "multiple-choice-questions",
    "base": "main"
  }')
JOB=$(echo "$RESP" | jq -r .job_id)

# Poll every 30s until terminal
while :; do
  STATE=$(curl -sS -H "Authorization: Bearer $KEY" \
    https://review.internal/reviews/$JOB | jq -r .status)
  case "$STATE" in
    complete|error) break ;;
  esac
  sleep 30
done

# Fetch full result
curl -sS -H "Authorization: Bearer $KEY" \
  https://review.internal/reviews/$JOB > review.json
```

## Operational notes

- **One review at a time.** The worker pool is `max_workers=1` to avoid hammering Claude/Codex API limits. Submit-rate isn't limited at the HTTP layer — the queue absorbs bursts.
- **Job persistence.** Each job is written to `jobs/<job_id>.json` and survives restarts. The in-memory dict is rebuilt lazily as jobs are queried.
- **Repo storage.** Clones live in `repos/<repo-name>/`. Disk usage grows with each unique repo. Wipe `repos/` to force fresh clones.
- **Logs.** Use NSSM's `AppStdout` / `AppStderr` settings to capture. Rotate manually or via Task Scheduler.
- **Security review:**
  - Token comparison uses `hmac.compare_digest` (constant-time).
  - `repo_url`, `branch`, `base` are validated against strict regexes before reaching `git`.
  - The server never executes anything from the repo — only `git clone`, `git fetch`, `git show`, `git diff`, `git log`.

## Failure modes & troubleshooting

| Symptom | Likely cause |
|---|---|
| `401 missing bearer token` | `Authorization` header not sent or malformed |
| `403 invalid bearer token` | Key mismatch; verify env var on server matches client |
| Job status `error` with `git ref not found` | Branch name wrong, or server didn't fetch yet (transient — retry) |
| Job status `error` with `git clone failed` | Server lacks repo access; configure credentials |
| Job hangs in `running` for >10 min | Claude or Codex CLI is hung; check server logs |
| `Diff is still N chars after filtering` | Branch too large; client should retry with `only` or `summary_only` |
