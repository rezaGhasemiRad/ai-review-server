"""HTTP server that lets a remote agent (e.g. Manus) trigger an AI review.

Endpoints:
    POST /reviews            submit a review job
    GET  /reviews/{job_id}   poll for status / result
    GET  /reviews            list recent jobs
    GET  /healthz            no-auth health probe

Auth: bearer token in `Authorization: Bearer <key>` header, validated against
the REVIEW_SERVER_API_KEY environment variable. The server refuses to start
if the env var is unset.

Run:
    set REVIEW_SERVER_API_KEY=<long-random-string>
    uvicorn server:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import datetime
import hmac
import json
import logging
import os
import pathlib
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator

import urllib.parse
import urllib.request

import ai_chat  # the structured review function lives here

log = logging.getLogger("review_server")

ROOT = pathlib.Path(__file__).parent.resolve()
REPOS_DIR = ROOT / "repos"
JOBS_DIR = ROOT / "jobs"
REPOS_DIR.mkdir(exist_ok=True)
JOBS_DIR.mkdir(exist_ok=True)

API_KEY = os.environ.get("REVIEW_SERVER_API_KEY", "").strip()
if not API_KEY:
    sys.exit(
        "REVIEW_SERVER_API_KEY environment variable is required. "
        "Set it to a long random string before starting the server."
    )
if len(API_KEY) < 24:
    log.warning("REVIEW_SERVER_API_KEY is shorter than 24 chars — consider rotating.")

# Persisted location of the API key — rewritten by the /rotate Telegram
# command so a rotation survives a restart.
SERVER_ENV = pathlib.Path.home() / ".ai-review-server.env"
KEY_LOCK = threading.Lock()

# Telegram bot config (bot token, chat id, webhook secret) lives in the
# alerts env file. Read once at startup; the webhook endpoint is inert
# until all three are present.
ALERTS_ENV = pathlib.Path.home() / ".ai-review-alerts.env"


def _load_env_file(path: pathlib.Path) -> dict:
    cfg: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


TELEGRAM = _load_env_file(ALERTS_ENV)

# Subscription-window budget for the per-job % display. The user's coder AI
# and the review server's Claude calls share the same Anthropic account's
# 5-hour Opus quota, so the cost of each review is meaningfully read against
# this budget. Defaults are tuned for Claude Code Max 5×; tweak via env to
# match your actual plan tier.
try:
    OPUS_5H_BUDGET_USD = float(os.environ.get("OPUS_5H_BUDGET_USD", "75"))
except ValueError:
    OPUS_5H_BUDGET_USD = 75.0
OPUS_PLAN_LABEL = os.environ.get("OPUS_PLAN_LABEL", "Max 5×").strip() or "Max 5×"

# Default caller-context depth for symbol-graph analysis (task #10).
# Submissions above this trigger the approval gate. Currently the
# validator caps depth at 2, so depth>DEFAULT never happens — the gate
# code is preserved for if depth=3+ is added later.
DEFAULT_CALLER_DEPTH = 2


def _rolling_review_usd_5h(now: float | None = None, exclude_job_id: str | None = None) -> tuple[float, int]:
    """Sum the Claude USD spent across this server's reviews completed in the
    last 5 hours. This is the *review server's* contribution to the user's
    Anthropic-account 5h Opus window — coder AI sessions and other projects
    on the same account stay invisible and must be added mentally.

    Returns (total_usd, job_count). Scans in-memory jobs first; that's
    typically enough since completed jobs stay in JOBS until restart.
    """
    import time as _time
    if now is None:
        now = _time.time()
    horizon = now - 5 * 3600
    total = 0.0
    count = 0
    with JOBS_LOCK:
        snapshot = list(JOBS.items())
    for jid, job in snapshot:
        if exclude_job_id is not None and jid == exclude_job_id:
            continue
        if not isinstance(job, dict) or job.get("status") != "complete":
            continue
        # completed_at is an ISO-8601 string ("YYYY-MM-DDTHH:MM:SSZ"); parse.
        ct = job.get("completed_at") or ""
        try:
            import datetime as _dt
            ts = _dt.datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            continue
        if ts < horizon:
            continue
        claude_usd = ((job.get("result") or {}).get("usage") or {}).get("claude", {}).get("usd")
        try:
            total += float(claude_usd or 0)
            if claude_usd:
                count += 1
        except (TypeError, ValueError):
            continue
    return round(total, 4), count


def _window_pct(claude_usd: float | int | None, job_id: str | None = None) -> dict | None:
    """Compute this job's share of the user's 5h Opus subscription window,
    plus the rolling 5h aggregate from this server.

    Returns None when there's nothing meaningful to show (no Claude cost,
    or budget disabled). Otherwise a dict the UI and Telegram notification
    both render — single source of truth so the % matches everywhere.

    The rolling aggregate includes this job (when it's in JOBS) PLUS every
    other review completed in the trailing 5h. The disclosure note makes
    explicit that coder AI sessions and other-project Claude usage on the
    same Anthropic account aren't counted — we can't see them.
    """
    try:
        usd = float(claude_usd or 0)
    except (TypeError, ValueError):
        return None
    if usd <= 0 or OPUS_5H_BUDGET_USD <= 0:
        return None
    # Rolling 5h is computed from the in-memory job table. We pass the
    # current job_id to dedupe — if this job is already in JOBS (the normal
    # case at GET time), we add `usd` separately to avoid double-counting.
    other_usd, other_count = _rolling_review_usd_5h(exclude_job_id=job_id)
    rolling_usd = round(usd + other_usd, 4)
    return {
        "claude_usd": round(usd, 4),
        "budget_usd": OPUS_5H_BUDGET_USD,
        "plan_label": OPUS_PLAN_LABEL,
        "pct": round(usd / OPUS_5H_BUDGET_USD * 100, 2),
        "rolling_5h_usd": rolling_usd,
        "rolling_5h_pct": round(rolling_usd / OPUS_5H_BUDGET_USD * 100, 2),
        "rolling_5h_job_count": other_count + 1,
        "note": (
            "Rolling 5h reflects ONLY this review server's contribution. "
            "Coder AI sessions, claude.ai chats, and other projects on the "
            "same Anthropic account aren't counted — add them mentally."
        ),
    }


def _telegram_reply(text: str) -> None:
    """Send a message back to the configured Telegram chat."""
    tok = TELEGRAM.get("TELEGRAM_BOT_TOKEN")
    chat = TELEGRAM.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=15
        )
    except Exception:
        log.exception("telegram reply failed")


def _job_link(job_id: str) -> str:
    """Dashboard deep-link for a job. Empty if PUBLIC_URL isn't configured."""
    base = (TELEGRAM.get("PUBLIC_URL") or "").rstrip("/")
    if base.endswith("/healthz"):
        base = base[: -len("/healthz")]
    return f"{base}/#job/{job_id}" if base else ""


def _notify_job_done(job_id: str) -> None:
    """Send a Telegram summary when a job reaches a terminal state."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        job = _load_from_disk(job_id) or {}
    status = job.get("status")
    if status not in ("complete", "error"):
        return

    pf = (job.get("project") or "—") + " / " + (job.get("feature") or "—")
    link = _job_link(job_id)
    result = job.get("result") or {}

    if status == "complete":
        cv = result.get("claude_verdict") or "?"
        xv = result.get("codex_verdict") or "?"
        cf = len(result.get("claude_findings") or [])
        xf = len(result.get("codex_findings") or [])
        usage = result.get("usage") or {}
        tin = usage.get("total_input_tokens") or 0
        tout = usage.get("total_output_tokens") or 0
        claude_usd = (usage.get("claude") or {}).get("usd")
        w = _window_pct(claude_usd, job_id=job_id)
        body = (
            f"Review COMPLETE — {pf}\n"
            f"Claude: {cv} ({cf} findings)\n"
            f"Codex:  {xv} ({xf} findings)\n"
            f"Tokens: {tin:,} in / {tout:,} out\n"
        )
        if w:
            body += (
                f"Window: {w['pct']:.2f}% this job · "
                f"{w['rolling_5h_pct']:.1f}% last 5h "
                f"({w['rolling_5h_job_count']} review(s), "
                f"{w['plan_label']}, excludes other Claude usage)\n"
            )
        # Task #12: surface test skips so the wall-time saving is visible.
        tr_all = result.get("test_results") or {}
        skipped_nri = [
            k for k, v in tr_all.items()
            if isinstance(v, dict) and v.get("skipped_no_runtime_impact")
        ]
        if skipped_nri and tr_all:
            body += (
                f"Tests: {len(skipped_nri)}/{len(tr_all)} repo(s) skipped "
                f"(no runtime impact)\n"
            )
    else:
        err = (job.get("error") or "").strip()
        body = f"Review FAILED — {pf}\nError: {err[:300]}\n"

    if link:
        body += link
    _telegram_reply(body)


def _notify_awaiting_approval(job_id: str) -> None:
    """Telegram nudge when a job lands in 'awaiting_approval' state.
    Different shape from _notify_job_done — actionable, not a completion."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        return
    pf = (job.get("project") or "—") + " / " + (job.get("feature") or "—")
    req = job.get("request", {}) or {}
    depth = req.get("caller_depth", 1)
    link = _job_link(job_id)
    body = (
        f"Review AWAITING APPROVAL — {pf}\n"
        f"Coder AI requested caller_depth={depth} "
        f"(deeper symbol-graph analysis; ~10-25K extra tokens, "
        f"~85% catch rate of cross-cut bugs).\n"
        f"Approve or reject in the dashboard."
    )
    if link:
        body += "\n" + link
    _telegram_reply(body)


def _rotate_api_key() -> str:
    """Generate a new API key, persist it to SERVER_ENV, swap it in live."""
    global API_KEY
    new = secrets.token_urlsafe(32)
    with KEY_LOCK:
        lines, found = [], False
        if SERVER_ENV.exists():
            for line in SERVER_ENV.read_text(encoding="utf-8").splitlines():
                if line.startswith("REVIEW_SERVER_API_KEY="):
                    lines.append(f"REVIEW_SERVER_API_KEY={new}")
                    found = True
                else:
                    lines.append(line)
        if not found:
            lines.append(f"REVIEW_SERVER_API_KEY={new}")
        SERVER_ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(SERVER_ENV, 0o600)
        API_KEY = new
    log.warning("API key rotated via Telegram command")
    return new

# One review at a time. Reviews are token-expensive and the underlying CLIs
# (claude / codex) are not designed for high concurrency.
EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reviewer")
# Tests for a single review run on this executor — they overlap with the
# review pipeline so wall time is max(tests, reviews) instead of tests + reviews.
# One worker is enough: EXECUTOR is also serialized, so we never have two
# jobs' tests in flight at the same time.
TESTS_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tests")

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

REPO_URL_RE = re.compile(r"^(https://|git@)[\w.@:/\-~]+\.git$")
SAFE_REF_RE = re.compile(r"^[A-Za-z0-9_./\-]+$")
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

def require_bearer(authorization: Optional[str] = Header(default=None)) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(None, 1)[1].strip()
    if not hmac.compare_digest(token.encode(), API_KEY.encode()):
        raise HTTPException(status_code=403, detail="invalid bearer token")


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class RepoSpec(BaseModel):
    """One repository participating in a multi-repo review."""
    repo_url: str = Field(..., description="HTTPS or SSH git URL ending in .git")
    branch: str
    base: str = "main"

    @field_validator("repo_url")
    @classmethod
    def _check_repo_url(cls, v: str) -> str:
        if not REPO_URL_RE.match(v):
            raise ValueError("repo_url must be an HTTPS or SSH git URL ending in .git")
        return v

    @field_validator("branch", "base")
    @classmethod
    def _check_ref(cls, v: str) -> str:
        if not SAFE_REF_RE.match(v):
            raise ValueError("ref names must match [A-Za-z0-9_./-]+")
        return v


class ReviewRequest(BaseModel):
    # Single-repo (legacy / convenience). Either these three OR `repos` — not both.
    repo_url: Optional[str] = None
    branch: Optional[str] = None
    base: Optional[str] = None

    # Multi-repo: a feature spanning several repositories reviewed as one.
    repos: Optional[list[RepoSpec]] = None

    only: list[str] = []
    include_all: bool = False
    summary_only: bool = False
    handoff_path: Optional[str] = None
    no_handoff: bool = False
    context: Optional[str] = None

    # Iteration memory — when set, the prior job's reconciled findings are
    # included in the reviewer context to avoid re-raising resolved issues.
    previous_job_id: Optional[str] = None

    # Tier-1 test execution: if true (default), the server runs
    # `.ai-collab/test.sh` per repo on the feature branch and feeds the
    # results into the reviewer context.
    run_tests: bool = True
    test_timeout_s: int = 900  # 15 min per repo (4 GB / 2 vCPU box)

    # Organization labels — group jobs for history/filtering. On an
    # iteration (previous_job_id set) these are inherited from the prior
    # job if omitted, so the implementer only tags iteration 1.
    project: Optional[str] = None
    feature: Optional[str] = None

    # Symbol-graph caller-context analysis (task #10).
    # 1 = direct callers only (~3-8K extra tokens, ~70% catch rate
    #     of cross-cut bugs).
    # 2 = direct + one hop of indirect callers tagged separately
    #     (~10-25K extra tokens, ~85% catch rate). NEW DEFAULT.
    # Approval gate (status=awaiting_approval) only triggers when
    # caller_depth > DEFAULT_CALLER_DEPTH — kept in place for if
    # depth>2 is ever added, but no current submission hits it.
    caller_depth: int = 2

    # Commit-range review (task #9). Two related fields:
    # - since_commit: explicit anchor for "diff me only from this SHA to
    #   HEAD". Optional — when omitted but previous_job_id is set, the
    #   server auto-resolves the prior job's recorded HEAD SHA as anchor
    #   (for the annotation-only path).
    # - narrow_to_recent_commits: when true, the diff scope ACTUALLY
    #   narrows to <since_commit>..HEAD instead of the default
    #   base...branch. This is the cost-aggressive path (10-20x reduction
    #   on long-iter features) at the cost of ~3-8% catch-rate loss on
    #   cross-iter regressions. When false (default), the diff stays full
    #   and the since_commit is used only as the annotation anchor.
    since_commit: Optional[str] = None
    narrow_to_recent_commits: bool = False

    @field_validator("project", "feature")
    @classmethod
    def _check_label(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not LABEL_RE.match(v):
            raise ValueError("project/feature must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
        return v

    @field_validator("repo_url")
    @classmethod
    def _check_repo_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not REPO_URL_RE.match(v):
            raise ValueError("repo_url must be an HTTPS or SSH git URL ending in .git")
        return v

    @field_validator("branch", "base")
    @classmethod
    def _check_ref(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not SAFE_REF_RE.match(v):
            raise ValueError("ref names must match [A-Za-z0-9_./-]+")
        return v

    @field_validator("caller_depth")
    @classmethod
    def _check_caller_depth(cls, v: int) -> int:
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ValueError("caller_depth must be an integer (1 or 2)")
        if n not in (1, 2):
            raise ValueError("caller_depth must be 1 or 2 (depth>2 is not supported)")
        return n

    @field_validator("since_commit")
    @classmethod
    def _check_since_commit(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            return None
        # Accept 7+ hex chars (short SHA) up to 40 (full SHA). Reachability
        # validation happens at review time when we have the repo handy.
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", v):
            raise ValueError("since_commit must be a git SHA (7-40 hex chars)")
        return v.lower()

    def normalized_repos(self) -> list[RepoSpec]:
        """Always return a non-empty list of RepoSpec, validating the either/or shape."""
        if self.repos:
            if self.repo_url:
                raise ValueError("send either 'repos' or single-repo fields, not both")
            if len(self.repos) == 0:
                raise ValueError("'repos' must contain at least one entry")
            return self.repos
        if not self.repo_url or not self.branch:
            raise ValueError("provide either 'repos' or both 'repo_url' and 'branch'")
        return [RepoSpec(repo_url=self.repo_url, branch=self.branch, base=self.base or "main")]


class JobSubmitted(BaseModel):
    job_id: str
    status: str
    status_url: str


# --------------------------------------------------------------------------- #
# Job storage
# --------------------------------------------------------------------------- #

def _now() -> str:
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _persist(job: dict) -> None:
    # Drop in-memory-only keys (Future, etc.) before serializing.
    clean = {k: v for k, v in job.items() if not k.startswith("_")}
    path = JOBS_DIR / f"{job['job_id']}.json"
    path.write_text(json.dumps(clean, indent=2, default=str), encoding="utf-8")


def _update(job_id: str, **changes) -> dict:
    with JOBS_LOCK:
        job = JOBS[job_id]
        job.update(changes)
        snapshot = dict(job)
    _persist(snapshot)
    return snapshot


def _load_from_disk(job_id: str) -> Optional[dict]:
    path = JOBS_DIR / f"{job_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("failed to read job %s", job_id)
        return None


# --------------------------------------------------------------------------- #
# Repo management
# --------------------------------------------------------------------------- #

def _repo_dir_for(url: str) -> pathlib.Path:
    """Stable on-disk path for a given remote URL."""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    # Defend against weird names
    name = re.sub(r"[^A-Za-z0-9_.\-]", "_", name) or "repo"
    return REPOS_DIR / name


TEST_ENV_FILE = pathlib.Path.home() / ".ai-collab-test.env"


def _test_env() -> dict:
    """Base environment for test.sh runs: inherited env + DB creds from the env file.

    The review server's own secret (REVIEW_SERVER_API_KEY) is stripped so a
    test script cannot read it.
    """
    env = dict(os.environ)
    env.pop("REVIEW_SERVER_API_KEY", None)
    if TEST_ENV_FILE.exists():
        for line in TEST_ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env["CI"] = "true"  # most test runners honor this for non-interactive mode
    return env


def _run_repo_tests(repo_path: str, ref: str, timeout_s: int) -> dict:
    """Check out `ref` in a temp worktree and run .ai-collab/test.sh if present.

    The test script runs in its own process session so that on timeout the
    ENTIRE process tree (bash + node/dotnet children) is killed — otherwise
    orphan children survive, keep file handles open, and leak the worktree.
    """
    wt = pathlib.Path(repo_path).parent / f"wt-{uuid.uuid4().hex[:8]}"
    started = time.monotonic()
    try:
        r = subprocess.run(
            ["git", "-C", repo_path, "worktree", "add", "--detach", str(wt), ref],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return {"status": "error", "reason": f"worktree add failed: {r.stderr.strip()}"}

        script = wt / ".ai-collab" / "test.sh"
        if not script.exists():
            return {"status": "skipped", "reason": "no .ai-collab/test.sh on this branch"}

        subprocess.run(["chmod", "+x", str(script)], capture_output=True)
        proc = subprocess.Popen(
            ["bash", str(script)],
            cwd=str(wt),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=_test_env(),
            start_new_session=True,  # own process group → killable as a tree
        )
        try:
            out, err = proc.communicate(timeout=timeout_s)
            rc = proc.returncode
            return {
                "status": "passed" if rc == 0 else "failed",
                "exit_code": rc,
                "duration_s": int(time.monotonic() - started),
                "stdout_tail": (out or "")[-8000:],
                "stderr_tail": (err or "")[-4000:],
            }
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                out, err = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                out, err = "", ""
            return {
                "status": "timeout",
                "timeout_s": timeout_s,
                "duration_s": int(time.monotonic() - started),
                "stdout_tail": (out or "")[-8000:],
                "stderr_tail": (err or "")[-4000:],
            }
    except Exception as e:
        return {"status": "error", "reason": f"{type(e).__name__}: {e}",
                "duration_s": int(time.monotonic() - started)}
    finally:
        # Belt-and-braces cleanup: drop the git worktree registration, then
        # force-delete any files left behind (timed-out builds leave artifacts
        # git's own remove can't unlink), then prune dangling metadata.
        subprocess.run(
            ["git", "-C", repo_path, "worktree", "remove", "--force", str(wt)],
            capture_output=True, text=True,
        )
        shutil.rmtree(wt, ignore_errors=True)
        subprocess.run(
            ["git", "-C", repo_path, "worktree", "prune"],
            capture_output=True, text=True,
        )


# --------------------------------------------------------------------------- #
# Task #12 — skip Tier-1 tests on repos whose diff has no runtime impact.
# --------------------------------------------------------------------------- #

_TEST_IRRELEVANT_SUFFIXES = (".md", ".markdown", ".txt", ".rst")
_TEST_IRRELEVANT_NAMES = (".gitignore", ".gitattributes", ".editorconfig")
_TEST_IRRELEVANT_BASENAME_PREFIXES = ("README", "CHANGELOG", "LICENSE", "CONTRIBUTING")


def _path_is_test_irrelevant(path: str) -> bool:
    """True for files whose changes can't affect test outcomes — docs,
    markdown, license/readme, .ai-collab metadata, editor config."""
    p = path.replace("\\", "/")
    if p.startswith(".ai-collab/") or "/.ai-collab/" in p:
        return True
    base = p.rsplit("/", 1)[-1]
    if base in _TEST_IRRELEVANT_NAMES:
        return True
    low = base.lower()
    if any(low.endswith(s) for s in _TEST_IRRELEVANT_SUFFIXES):
        return True
    up = base.upper()
    if any(up.startswith(x) for x in _TEST_IRRELEVANT_BASENAME_PREFIXES):
        return True
    return False


def _repo_runtime_change_check(repo_path: str, range_spec: str) -> tuple[bool, str]:
    """Return (has_runtime_changes, reason).

    Uses `git diff -w --numstat <range>`. The -w flag ignores in-line
    whitespace, and crucially `--numstat` reports 0/0 (and thus omits
    entirely) files whose ONLY change nets to whitespace — including
    trailing-newline-at-EOF fixes, which `--name-only` would wrongly keep
    (git treats the EOF newline as content for name-only but numstat
    correctly nets it to zero under -w). Files surviving that are then
    filtered against the docs/config allowlist. If nothing runtime-relevant
    remains, tests can be safely skipped. Any git error → (True, "") so
    tests run (fail safe).
    """
    r = subprocess.run(
        ["git", "-C", repo_path, "diff", "-w", "--numstat", range_spec],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return True, ""
    changed: list[str] = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        # Binary files report "-\t-" — can't reason about them, treat as a
        # real change so tests run.
        if added == "-" or removed == "-":
            changed.append(path)
            continue
        try:
            if int(added) > 0 or int(removed) > 0:
                changed.append(path)
        except ValueError:
            changed.append(path)  # unparseable → be safe
    if not changed:
        return False, "no non-whitespace line changes"
    runtime = [f for f in changed if not _path_is_test_irrelevant(f)]
    if not runtime:
        return False, f"only docs/config/whitespace changed ({len(changed)} file(s))"
    return True, ""


def _compute_test_skips(
    prepared: list[dict],
    prior_head_shas: dict[str, str],
    prior_result: Optional[dict],
) -> dict[str, str]:
    """Per-repo decision: can Tier-1 tests be skipped? Returns
    {repo_url: skip_reason} for repos that can skip; absent = run tests.

    Safe to skip when EITHER:
      (a) there's a prior PASSING test run for this repo AND nothing
          runtime-relevant changed since that SHA (anchor..branch), OR
      (b) the whole feature (base...branch) has no runtime-relevant change.
    Anything uncertain → not in the dict → tests run.
    """
    out: dict[str, str] = {}
    prior_tests = (prior_result or {}).get("test_results", {}) if prior_result else {}
    for p in prepared:
        repo_url = p["repo_url"]
        anchor = prior_head_shas.get(repo_url)
        prior_ok = (prior_tests.get(repo_url) or {}).get("status") == "passed"
        # (a) Incremental: nothing runtime changed since a prior passing run.
        if anchor and prior_ok:
            has_rt, reason = _repo_runtime_change_check(
                p["repo"], f"{anchor}..{p['branch']}"
            )
            if not has_rt:
                out[repo_url] = f"{reason} since prior passing test run ({anchor[:12]})"
                continue
        # (b) Whole-feature: the entire base..branch contribution is non-runtime.
        has_rt, reason = _repo_runtime_change_check(
            p["repo"], f"{p['base']}...{p['branch']}"
        )
        if not has_rt:
            out[repo_url] = f"{reason} (whole feature vs base)"
    return out


def _load_prior_result(job_id: str) -> Optional[dict]:
    """Return the `result` field of a prior complete job, or None."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        job = _load_from_disk(job_id)
    if job is None or job.get("status") != "complete":
        return None
    return job.get("result")


def _load_prior_head_shas(job_id: str) -> dict[str, str]:
    """Return the {repo_url: head_sha} map from a prior complete job, or {}.
    This is what auto-resolves the since-commit anchor for stage 1
    annotations and stage 2 narrowing (task #9). Empty dict signals
    "no anchor available" — review proceeds in normal full-branch mode."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        job = _load_from_disk(job_id)
    if job is None:
        return {}
    out = job.get("head_sha_by_repo")
    return out if isinstance(out, dict) else {}


def _resolve_head_shas(prepared: list[dict]) -> dict[str, str]:
    """Resolve the current HEAD SHA of each prepared repo's branch.
    Called right after `prepared` is built — every refs has been fetched
    fresh by _ensure_repo, so origin/<branch> is up-to-date."""
    out: dict[str, str] = {}
    for p in prepared:
        # `branch` is already prefixed with origin/ at this point (server.py
        # adds the prefix before calling ai_chat).
        r = subprocess.run(
            ["git", "-C", p["repo"], "rev-parse", "--verify", p["branch"]],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            sha = r.stdout.strip()
            if sha:
                out[p["repo_url"]] = sha
    return out


def _ensure_repo(url: str) -> pathlib.Path:
    """Clone the repo on first use, otherwise fetch latest refs from origin."""
    target = _repo_dir_for(url)
    if not target.exists():
        log.info("cloning %s -> %s", url, target)
        r = subprocess.run(
            ["git", "clone", "--no-checkout", url, str(target)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"git clone failed: {r.stderr.strip() or r.stdout.strip()}")
    else:
        log.info("fetching %s", target)
        r = subprocess.run(
            ["git", "-C", str(target), "fetch", "--all", "--prune"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"git fetch failed: {r.stderr.strip() or r.stdout.strip()}")
    return target


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #

def _run_job(job_id: str) -> None:
    def progress(msg: str) -> None:
        _update(job_id, progress=msg)

    try:
        with JOBS_LOCK:
            job_rec = JOBS[job_id]
            req = dict(job_rec["request"])
            job_feature = job_rec.get("feature")
        _update(job_id, status="running", started_at=_now(), progress="cloning repositories")

        repos_in = req.get("repos") or []
        if not repos_in:
            # Legacy single-repo shape — convert.
            repos_in = [{
                "repo_url": req["repo_url"],
                "branch": req["branch"],
                "base": req.get("base") or "main",
            }]

        # Clone/fetch each repo, build the list passed to ai_chat.
        prepared: list[dict] = []
        for r in repos_in:
            repo_path = _ensure_repo(r["repo_url"])
            prepared.append({
                "repo": str(repo_path),
                "repo_url": r["repo_url"],
                "branch": f"origin/{r['branch']}",
                "base": f"origin/{r['base']}",
            })

        # Resolve and persist the HEAD SHA of each repo's reviewed branch.
        # Future iterations with previous_job_id pointing here will use
        # these SHAs as the since-commit anchor for the "Commits since
        # prior review" annotation (and for opt-in stage-2 narrowing).
        head_sha_by_repo = _resolve_head_shas(prepared)
        _update(job_id, head_sha_by_repo=head_sha_by_repo)

        # Prior iteration context (optional). When set, also fetch the
        # prior job's recorded head SHAs so review_*_structured can build
        # the "Commits since prior review" annotation.
        prior_result = None
        prior_head_shas: dict[str, str] = {}
        if req.get("previous_job_id"):
            prior_result = _load_prior_result(req["previous_job_id"])
            if prior_result is None:
                log.warning("previous_job_id %s did not resolve to a completed job; ignoring",
                            req["previous_job_id"])
            prior_head_shas = _load_prior_head_shas(req["previous_job_id"])
            if prior_head_shas:
                log.info("prior iteration head SHAs: %s",
                         ", ".join(f"{_short_repo(u)}={s[:7]}"
                                   for u, s in prior_head_shas.items()))

        # Tests run on a background thread so the review pipeline can start
        # in parallel. Tests are still SEQUENTIAL across repos within the
        # thread (Tier-1 builds can be ~1.5 GB RAM each — running N at once
        # OOMs the box). The thread fills a Future that ai_chat awaits just
        # before the reconcile phase, so reviewers don't see test results
        # during their initial pass but the reconciler does.
        test_results_by_repo: dict = {}
        tests_future: Future | None = None
        if req.get("run_tests", True):
            timeout_s = int(req.get("test_timeout_s") or 600)
            # Task #12: decide which repos can skip tests (no runtime impact).
            test_skip_by_repo = _compute_test_skips(prepared, prior_head_shas, prior_result)
            if test_skip_by_repo:
                log.info("test-skip: %d/%d repo(s) — %s",
                         len(test_skip_by_repo), len(prepared),
                         "; ".join(f"{_short_repo(u)}: {r}"
                                   for u, r in test_skip_by_repo.items()))
            log.info("running tests in BACKGROUND across %d repo(s), "
                     "sequential within thread, timeout=%ds each",
                     len(prepared) - len(test_skip_by_repo), timeout_s)

            def _run_tests_bg() -> dict:
                results: dict = {}
                for i, p in enumerate(prepared, 1):
                    repo_url = p["repo_url"]
                    short = _short_repo(repo_url)
                    skip_reason = test_skip_by_repo.get(repo_url)
                    if skip_reason:
                        results[repo_url] = {
                            "status": "skipped",
                            "reason": f"tests skipped — {skip_reason}",
                            "skipped_no_runtime_impact": True,
                        }
                        log.info("test SKIP %s: %s", repo_url, skip_reason)
                        progress(f"Tier-1 tests ({i}/{len(prepared)}): "
                                 f"{short} skipped (no runtime impact)")
                        continue
                    progress(f"Tier-1 tests ({i}/{len(prepared)}): {short}")
                    tr = _run_repo_tests(p["repo"], p["branch"], timeout_s)
                    results[repo_url] = tr
                    log.info("test result %s: status=%s exit=%s dur=%ss",
                             repo_url, tr.get("status"), tr.get("exit_code"),
                             tr.get("duration_s"))
                # Stash on enclosing-scope dict too, so the outer code can
                # read results for persistence even on the happy path.
                test_results_by_repo.update(results)
                return results

            tests_future = TESTS_EXECUTOR.submit(_run_tests_bg)

        progress("starting review (tests running in background)" if tests_future
                 else "starting review")
        common = dict(
            only_globs=req.get("only", []),
            include_all=req.get("include_all", False),
            summary_only=req.get("summary_only", False),
            handoff_path=req.get("handoff_path"),
            no_handoff=req.get("no_handoff", False),
            context=req.get("context"),
            prior_result=prior_result,
            # When tests run in the background, ai_chat consumes them via
            # tests_future and ignores test_results_by_repo. Pass None to
            # make that intent explicit.
            test_results_by_repo=None if tests_future else (test_results_by_repo or None),
            progress_cb=progress,
            feature=job_feature,   # enables .ai-collab/handoffs/<feature>.md discovery
            tests_future=tests_future,
            # Symbol-graph caller-context analysis (task #10). Default is 2
            # (depth + one hop of indirect callers tagged). Anything above
            # the default goes through the approval gate before this point.
            caller_depth=int(req.get("caller_depth", DEFAULT_CALLER_DEPTH)),
            # Commit-range review (task #9). prior_head_shas is auto-
            # resolved from previous_job_id (when set) and feeds the
            # "Commits since prior review" annotation. since_commit
            # explicit-override + narrow_to_recent_commits are stage 2 —
            # the actual diff range narrows when narrow=true.
            prior_head_shas=prior_head_shas,
            since_commit=req.get("since_commit"),
            narrow_to_recent_commits=bool(req.get("narrow_to_recent_commits", False)),
        )

        if len(prepared) == 1:
            p = prepared[0]
            result = ai_chat.review_branch_structured(
                repo=p["repo"], branch=p["branch"], base=p["base"], **common,
            )
        else:
            result = ai_chat.review_multi_repo_structured(
                repos=prepared, **common,
            )

        if test_results_by_repo:
            result["test_results"] = test_results_by_repo
        _update(job_id, status="complete", completed_at=_now(), result=result,
                progress="done")
        log.info("job %s complete (%d repo(s))", job_id, len(prepared))
        _notify_job_done(job_id)
    except SystemExit as e:
        # ai_chat helpers use sys.exit for input errors; convert to job failure.
        _update(job_id, status="error", completed_at=_now(),
                error=str(e) or "input validation failed")
        log.warning("job %s failed (validation): %s", job_id, e)
        _notify_job_done(job_id)
    except Exception as e:
        _update(job_id, status="error", completed_at=_now(),
                error=f"{type(e).__name__}: {e}",
                error_trace=traceback.format_exc())
        _notify_job_done(job_id)
        log.exception("job %s failed", job_id)
    finally:
        # If tests were still running when the review failed, drain the
        # future before returning. TESTS_EXECUTOR has a single worker that
        # the next job will queue on — leaving a future in flight here would
        # stall the next job at the test phase.
        try:
            if 'tests_future' in locals() and tests_future is not None \
                    and not tests_future.done():
                log.info("draining background tests after job %s exit", job_id)
                tests_future.result(timeout=900)
        except Exception:
            log.exception("draining tests_future failed (continuing)")


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

def _reconcile_on_startup() -> None:
    """Recover from an unclean shutdown.

    1. Any job still marked running/queued was orphaned when the process died
       (its worker thread is gone). Mark it errored so clients stop polling.
    2. Delete stale `wt-*` test-worktree directories left by interrupted runs.
    3. Prune dangling worktree metadata from each repo clone.
    """
    orphaned = 0
    for p in JOBS_DIR.glob("*.json"):
        try:
            job = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if job.get("status") in ("running", "queued"):
            job["status"] = "error"
            job["error"] = "interrupted by server restart"
            job["completed_at"] = _now()
            p.write_text(json.dumps(job, indent=2, default=str), encoding="utf-8")
            orphaned += 1
    if orphaned:
        log.warning("startup: marked %d orphaned job(s) as error", orphaned)

    swept = 0
    for wt in REPOS_DIR.glob("wt-*"):
        if wt.is_dir():
            shutil.rmtree(wt, ignore_errors=True)
            swept += 1
    if swept:
        log.warning("startup: swept %d stale worktree dir(s)", swept)

    for repo in REPOS_DIR.iterdir():
        if repo.is_dir() and not repo.name.startswith("wt-") and (repo / ".git").exists():
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "prune"],
                capture_output=True, text=True,
            )


_reconcile_on_startup()


app = FastAPI(
    title="AI Review Server",
    description="Triggers Claude+Codex reviews of feature branches.",
)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "time": _now()}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Telegram bot command endpoint — handles /token and /rotate.

    Auth: the Telegram-supplied secret-token header must match
    TELEGRAM_WEBHOOK_SECRET, AND the message must come from the
    configured chat id. No bearer token (Telegram has none).
    """
    secret = TELEGRAM.get("TELEGRAM_WEBHOOK_SECRET")
    if not secret or request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token") != secret:
        raise HTTPException(status_code=403, detail="forbidden")

    try:
        update = await request.json()
    except Exception:
        return {"ok": True}

    msg = update.get("message") or update.get("edited_message") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    authorized = str(TELEGRAM.get("TELEGRAM_CHAT_ID", ""))

    # Silently ignore anyone who isn't the configured operator.
    if not chat_id or chat_id != authorized:
        return {"ok": True}

    cmd = text.split()[0].split("@")[0].lower() if text else ""
    if cmd == "/token":
        _telegram_reply(
            "Current REVIEW_SERVER_API_KEY:\n\n" + API_KEY +
            "\n\n(Use this to supply the existing key. /rotate makes a new one.)"
        )
    elif cmd == "/rotate":
        new = _rotate_api_key()
        _telegram_reply(
            "API key rotated. New REVIEW_SERVER_API_KEY:\n\n" + new +
            "\n\nThe old key is now dead — update anything using it (e.g. Manus)."
        )
    else:
        _telegram_reply(
            "AI Review Server bot. Commands:\n"
            "/token  - show the current API key\n"
            "/rotate - generate a new API key (invalidates the old one)"
        )
    return {"ok": True}


UI_FILE = ROOT / "ui" / "index.html"


@app.get("/", response_class=HTMLResponse)
def dashboard():
    """Serve the single-page dashboard. The page itself carries no data —
    it prompts for the API key and calls the authenticated endpoints."""
    if UI_FILE.exists():
        return UI_FILE.read_text(encoding="utf-8")
    return HTMLResponse("<h1>AI Review Server</h1><p>Dashboard not installed.</p>",
                        status_code=200)


@app.post("/reviews", response_model=JobSubmitted, status_code=202,
          dependencies=[Depends(require_bearer)])
def submit_review(req: ReviewRequest, request: Request) -> JobSubmitted:
    # Validate the either/or shape up-front so the worker can't get a malformed job.
    try:
        repos = req.normalized_repos()
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Fail loud when narrowing is requested but no anchor is resolvable
    # (task #11). Narrowing needs a since-commit. It can come from an
    # explicit since_commit OR be auto-resolved from previous_job_id's
    # recorded head_sha_by_repo. If neither is available, the old behavior
    # was to silently fall back to a full-diff review at full token cost —
    # the coder AI thought it was narrowing but wasn't. Reject instead so
    # the caller knows and can decide.
    if req.narrow_to_recent_commits and not req.since_commit:
        if not req.previous_job_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "narrow_to_recent_commits requires an anchor. Pass an "
                    "explicit since_commit, or a previous_job_id whose "
                    "head_sha_by_repo was recorded."
                ),
            )
        prior_shas = _load_prior_head_shas(req.previous_job_id)
        if not prior_shas:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"narrow_to_recent_commits requested but previous_job_id "
                    f"'{req.previous_job_id}' has no recorded head_sha_by_repo "
                    f"(it either errored before the SHA was captured, or ran "
                    f"before head-SHA persistence was deployed). Options: "
                    f"(1) remove narrow_to_recent_commits, (2) pass an explicit "
                    f"since_commit, or (3) run one non-narrowed iteration first "
                    f"to seed the anchor for future iterations."
                ),
            )

    # Resolve project/feature: explicit values win; otherwise inherit from
    # the prior job in an iteration chain.
    project, feature = req.project, req.feature
    if req.previous_job_id and (project is None or feature is None):
        prior = None
        with JOBS_LOCK:
            prior = JOBS.get(req.previous_job_id)
        if prior is None:
            prior = _load_from_disk(req.previous_job_id)
        if prior:
            project = project or prior.get("project")
            feature = feature or prior.get("feature")

    job_id = uuid.uuid4().hex
    # Approval gate for caller_depth above the default. Default is 2, so
    # only depth>2 (currently not allowed by the validator — kept for
    # future depth=3+ implementation) requires explicit approval. depth=1
    # and depth=2 both queue immediately.
    needs_approval = req.caller_depth > DEFAULT_CALLER_DEPTH
    initial_status = "awaiting_approval" if needs_approval else "queued"
    job = {
        "job_id": job_id,
        "status": initial_status,
        "created_at": _now(),
        "project": project,
        "feature": feature,
        "previous_job_id": req.previous_job_id,
        "request": req.model_dump(),
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    _persist(job)

    summary = ", ".join(f"{r.repo_url}@{r.branch}" for r in repos)
    base_url = str(request.base_url).rstrip("/")

    if needs_approval:
        log.info(
            "job %s queued AWAITING APPROVAL [project=%s feature=%s caller_depth=%d] across %d repo(s): %s",
            job_id, project, feature, req.caller_depth, len(repos), summary,
        )
        # Telegram nudge so the human knows there's an approval to make.
        _notify_awaiting_approval(job_id)
        return JobSubmitted(
            job_id=job_id, status=initial_status,
            status_url=f"{base_url}/reviews/{job_id}",
        )

    fut = EXECUTOR.submit(_run_job, job_id)
    # Hold the Future in-memory so /cancel can call .cancel() on a queued job.
    # The leading underscore keeps _persist from trying to serialize it.
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id]["_future"] = fut
    log.info("queued job %s [project=%s feature=%s] across %d repo(s): %s",
             job_id, project, feature, len(repos), summary)
    return JobSubmitted(
        job_id=job_id, status="queued", status_url=f"{base_url}/reviews/{job_id}"
    )


# --------------------------------------------------------------------------- #
# Lean payload — keep AI consumers from re-billing the full response on every
# turn of their conversation. The heavy fields (full reviewer transcript and
# raw test output) move behind explicit opt-in via ?full=true or the dedicated
# /transcript route. Default GET /reviews/{id} returns a payload focused on
# what a consumer *acts on*: verdicts, structured findings, reconcile notes,
# usage, and a per-repo test summary.
# --------------------------------------------------------------------------- #

# Known test-summary lines, ordered by specificity. We search backwards through
# stdout for the FIRST match — that's the runner's final summary in almost all
# real outputs. Tuned for the runners this server actually sees today.
_TEST_SUMMARY_PATTERNS = [
    # .NET — `dotnet test` end-of-run line
    re.compile(r"^(Passed!.*$)", re.MULTILINE),
    re.compile(r"^(Failed!.*$)", re.MULTILINE),
    re.compile(r"^(Test Run Successful\.?)\s*$", re.MULTILINE),
    re.compile(r"^(Test Run Failed\.?)\s*$", re.MULTILINE),
    # Jest / Vitest — multi-line summary block; we anchor on "Tests:"
    re.compile(r"^(Tests?:\s+.*$)", re.MULTILINE),
    # pytest — boxed summary "===== N passed in X.Xs ====="
    re.compile(r"^(=+\s*\d+\s+passed.*=+)\s*$", re.MULTILINE),
    re.compile(r"^(=+\s*\d+\s+failed.*=+)\s*$", re.MULTILINE),
    # npm test fallback — the post-test summary
    re.compile(r"^(Test Suites:.*$)", re.MULTILINE),
]

# .NET MSB3277 dependency-conflict warnings repeat per project reference and
# carry no actionable signal. Filter them from the failed-tail before showing.
_NOISE_LINE_PATTERNS = [
    re.compile(r"^.*: warning MSB3277:.*$", re.MULTILINE),
    re.compile(r"^.*: warning MSB3243:.*$", re.MULTILINE),  # version mismatch sibling
    re.compile(r"^.*: warning NU\d+:.*$", re.MULTILINE),    # NuGet warnings
]


def _lean_test_output(stdout_tail: str, status: str) -> str:
    """Compress a raw stdout tail into the smallest version a consumer can
    still act on.

    - passed → just the runner's summary line (often a single line, ~100 bytes)
    - failed/timeout → tail with known-noise lines stripped, capped at 3 KB
    - other (skipped/error) → unchanged (already small)
    """
    if not stdout_tail:
        return stdout_tail
    if status == "passed":
        for pat in _TEST_SUMMARY_PATTERNS:
            m = pat.search(stdout_tail)
            if m:
                return m.group(1).rstrip()
        # No known summary pattern — keep the last few non-empty lines so the
        # consumer at least sees the runner finished cleanly.
        lines = [ln for ln in stdout_tail.splitlines() if ln.strip()]
        return "\n".join(lines[-5:])
    if status in ("failed", "timeout"):
        filtered = stdout_tail
        for pat in _NOISE_LINE_PATTERNS:
            filtered = pat.sub("", filtered)
        # Collapse runs of blank lines created by the noise removal.
        filtered = re.sub(r"\n{3,}", "\n\n", filtered)
        # Cap at 3 KB tail — failure context lives at the bottom.
        if len(filtered) > 3000:
            filtered = "... (older output trimmed) ...\n" + filtered[-3000:]
        return filtered.rstrip()
    return stdout_tail


def _lean_result(result: dict, job_id: str) -> dict:
    """Strip / compress heavy fields for the default GET response.

    Removes:
      - transcript_md (full reviewer transcript — usually the largest field)
      - per-repo stderr_tail (failure context lives in stdout_tail; stderr
        is almost always shell warnings on success and a dup of stdout on
        failure)

    Compresses:
      - test_results[*].stdout_tail → runner summary line on passed runs,
        noise-stripped tail on failed runs (see _lean_test_output)

    Adds:
      - transcript_url pointing to the dedicated transcript route
    """
    out = dict(result)  # shallow copy — we'll only replace specific keys
    if "transcript_md" in out:
        del out["transcript_md"]
    # handoff_content is multi-KB markdown the consumer already has on disk
    # (they wrote it before submitting). Keep handoff_source as a pointer;
    # the dashboard's ?full=true fetch retrieves the body when humans need it.
    if "handoff_content" in out:
        del out["handoff_content"]
    out["transcript_url"] = f"/reviews/{job_id}/transcript"
    # Surface "this job consumed X% of your 5h Opus window" plus a rolling
    # 5h aggregate from this server — clearly disclosed as NOT including
    # coder AI sessions or other-project Claude usage on the same account.
    w = _window_pct(
        ((out.get("usage") or {}).get("claude") or {}).get("usd"),
        job_id=job_id,
    )
    if w is not None:
        out["subscription_window"] = w

    tr_by_repo = out.get("test_results")
    if isinstance(tr_by_repo, dict):
        compressed: dict[str, dict] = {}
        for repo, tr in tr_by_repo.items():
            if not isinstance(tr, dict):
                compressed[repo] = tr
                continue
            tr2 = dict(tr)
            status = tr2.get("status") or ""
            if "stdout_tail" in tr2:
                tr2["stdout_tail"] = _lean_test_output(tr2["stdout_tail"], status)
            tr2.pop("stderr_tail", None)
            compressed[repo] = tr2
        out["test_results"] = compressed

    return out


@app.get("/reviews/{job_id}", dependencies=[Depends(require_bearer)])
def get_review(job_id: str, full: bool = False):
    """Return a job record. Defaults to a lean shape (heavy fields stripped /
    compressed) to keep AI consumers from re-billing the same bytes on every
    turn of their conversation. Pass `?full=true` to get the raw record —
    used by the dashboard and by humans debugging the reviewer itself.
    """
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        job = _load_from_disk(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    # Strip in-memory-only keys (e.g. _future from /cancel) — they aren't
    # JSON-serializable and FastAPI would 500 otherwise.
    cleaned = {k: v for k, v in job.items() if not k.startswith("_")}
    result = cleaned.get("result")
    if full:
        # Full path keeps transcript_md / handoff_content but still gets the
        # subscription_window field so UI + consumers don't have to compute
        # it themselves regardless of which endpoint flavor they fetched.
        if isinstance(result, dict):
            w = _window_pct(
                ((result.get("usage") or {}).get("claude") or {}).get("usd"),
                job_id=job_id,
            )
            if w is not None:
                result = dict(result)
                result["subscription_window"] = w
                cleaned["result"] = result
        return cleaned
    if isinstance(result, dict):
        cleaned["result"] = _lean_result(result, job_id)
    return cleaned


@app.get("/reviews/{job_id}/transcript", dependencies=[Depends(require_bearer)])
def get_review_transcript(job_id: str):
    """Return the full reviewer transcript as plain markdown. Pulled out of
    the default review response because it's the largest field and most
    consumers don't act on it — they read findings, not transcripts.
    """
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        job = _load_from_disk(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    transcript = ((job.get("result") or {}).get("transcript_md")) or ""
    if not transcript:
        # Either still running, errored before any review work, or older
        # job that predates transcript capture. 404 keeps the contract
        # honest: there is nothing here.
        raise HTTPException(status_code=404, detail="no transcript available")
    return PlainTextResponse(transcript, media_type="text/markdown; charset=utf-8")


def _iter_jobs():
    """Yield every persisted job dict, newest first."""
    files = sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files:
        try:
            yield json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue


def _repo_names(job: dict) -> list[str]:
    """Compact list of repo identifiers from a job's request."""
    req = job.get("request", {})
    if req.get("repos"):
        return [r.get("repo_url", "?") for r in req["repos"]]
    if req.get("repo_url"):
        return [req["repo_url"]]
    return []


def _short_repo(url: str) -> str:
    """`git@github.com:owner/myrepo.git` -> `myrepo`."""
    name = (url or "").rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name


def _extract_verdict(review_md: Optional[str]) -> Optional[str]:
    """Best-effort pull of the `Verdict:` line from a structured review."""
    if not review_md:
        return None
    m = re.search(r"\*{0,2}Verdict:?\*{0,2}\s*`?([A-Za-z\-]+)`?", review_md)
    return m.group(1).lower() if m else None


def _job_summary(job: dict) -> dict:
    result = job.get("result") or {}
    findings_total = len(result.get("claude_findings") or []) \
        + len(result.get("codex_findings") or [])
    usage = result.get("usage") or {}
    return {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "progress": job.get("progress"),
        "project": job.get("project"),
        "feature": job.get("feature"),
        "previous_job_id": job.get("previous_job_id"),
        "archived": bool(job.get("archived")),
        "repos": _repo_names(job),
        "created_at": job.get("created_at"),
        "completed_at": job.get("completed_at"),
        "claude_verdict": result.get("claude_verdict")
        or _extract_verdict(result.get("claude_review")),
        "codex_verdict": result.get("codex_verdict")
        or _extract_verdict(result.get("codex_review")),
        "findings_total": findings_total,
        "cost_usd": usage.get("total_usd"),
        "error": job.get("error"),
    }


@app.get("/reviews", dependencies=[Depends(require_bearer)])
def list_reviews(limit: int = 25, offset: int = 0,
                 project: Optional[str] = None, feature: Optional[str] = None,
                 repo: Optional[str] = None, archived: bool = False):
    """Paginated job list, newest first.

    Filters: project, feature, repo (case-insensitive substring of any
    participating repo URL). `archived` selects the archive (default: only
    non-archived jobs). Returns `total` for client-side paging.
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    repo_q = repo.lower() if repo else None
    matched = []
    for j in _iter_jobs():
        if bool(j.get("archived")) != archived:
            continue
        if project is not None and j.get("project") != project:
            continue
        if feature is not None and j.get("feature") != feature:
            continue
        if repo_q is not None:
            names = _repo_names(j)
            shorts = [_short_repo(n).lower() for n in names]
            # exact short-name match (dropdown) OR substring of a full URL
            if repo_q not in shorts and not any(repo_q in n.lower() for n in names):
                continue
        matched.append(j)
    page = matched[offset:offset + limit]
    return {
        "total": len(matched),
        "offset": offset,
        "limit": limit,
        "count": len(page),
        "jobs": [_job_summary(j) for j in page],
    }


@app.get("/repos", dependencies=[Depends(require_bearer)])
def list_repos():
    """Distinct repositories that appear across all jobs (for the filter UI)."""
    seen: dict[str, str] = {}
    for j in _iter_jobs():
        for url in _repo_names(j):
            seen.setdefault(_short_repo(url), url)
    repos = sorted(
        ({"name": n, "url": u} for n, u in seen.items()),
        key=lambda r: r["name"].lower(),
    )
    return {"repos": repos}


def _set_archived(job_id: str, value: bool) -> dict:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        job = _load_from_disk(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    job["archived"] = value
    with JOBS_LOCK:
        JOBS[job_id] = job
    _persist(job)
    return job


@app.post("/reviews/{job_id}/archive", dependencies=[Depends(require_bearer)])
def archive_job(job_id: str):
    _set_archived(job_id, True)
    return {"job_id": job_id, "archived": True}


@app.post("/reviews/{job_id}/unarchive", dependencies=[Depends(require_bearer)])
def unarchive_job(job_id: str):
    _set_archived(job_id, False)
    return {"job_id": job_id, "archived": False}


@app.post("/reviews/{job_id}/cancel", dependencies=[Depends(require_bearer)])
def cancel_job(job_id: str):
    """Cancel a queued review. Idempotent for terminal jobs.

    Running jobs cannot be cancelled cleanly (the worker thread can't be
    killed safely from outside) — returns 409 for those; restart the
    service if you really need to stop a running job.
    """
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        job = _load_from_disk(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")

    status = job.get("status")
    if status in ("complete", "error"):
        return {"job_id": job_id, "status": status, "note": "already terminal"}

    if status == "running":
        raise HTTPException(
            status_code=409,
            detail="cannot cancel a running job; let it finish or restart the server",
        )

    # status == "queued" OR "awaiting_approval": if queued, try to remove
    # from the executor's queue. awaiting_approval has no future yet (the
    # job was never submitted to the executor), so the cancel is purely a
    # state transition.
    fut = job.get("_future") if isinstance(job, dict) else None
    cancelled = fut.cancel() if fut else False
    with JOBS_LOCK:
        jr = JOBS.get(job_id)
        if jr is not None:
            jr["status"] = "error"
            jr["error"] = "cancelled by user"
            jr["completed_at"] = _now()
            _persist(jr)
            snapshot = dict(jr)
        else:
            snapshot = {"job_id": job_id, "status": "error",
                        "error": "cancelled by user", "completed_at": _now()}
            _persist(snapshot)
    log.info("job %s cancelled (future.cancel=%s)", job_id, cancelled)
    _notify_job_done(job_id)
    return {"job_id": job_id, "status": "error",
            "future_cancelled": cancelled,
            "note": "queued job removed from queue" if cancelled
                    else "future could not be cancelled (already running?) — marked errored"}


@app.post("/reviews/{job_id}/approve-depth", dependencies=[Depends(require_bearer)])
def approve_review_depth(job_id: str):
    """Approve a caller_depth>1 review and start it running.

    Idempotent for already-queued or terminal jobs (returns 200 with note).
    Returns 409 if the job isn't in 'awaiting_approval' state — that
    catches double-approves and approvals of jobs that were cancelled
    while waiting."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        job = _load_from_disk(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
    status = job.get("status")
    if status in ("queued", "running", "complete", "error"):
        return {"job_id": job_id, "status": status,
                "note": f"already {status}; nothing to approve"}
    if status != "awaiting_approval":
        raise HTTPException(
            status_code=409,
            detail=f"cannot approve job in state '{status}'",
        )
    # Transition to queued + submit to executor.
    with JOBS_LOCK:
        jr = JOBS.get(job_id)
        if jr is None:
            raise HTTPException(status_code=404, detail="job vanished from registry")
        jr["status"] = "queued"
        jr["approved_at"] = _now()
        _persist(jr)
    fut = EXECUTOR.submit(_run_job, job_id)
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id]["_future"] = fut
    log.info("job %s approved and queued for execution", job_id)
    return {"job_id": job_id, "status": "queued",
            "note": "approved; submitted to executor"}


@app.post("/reviews/{job_id}/reject-depth", dependencies=[Depends(require_bearer)])
def reject_review_depth(job_id: str, payload: Optional[dict] = None):
    """Reject a caller_depth>1 review with an optional reason. Marks the
    job as errored; the coder AI can resubmit with caller_depth=1 to bypass
    the approval gate.

    Body: {"reason": "<short string>"} — optional. Reason gets surfaced in
    the job's error field + Telegram notification."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        job = _load_from_disk(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
    status = job.get("status")
    if status in ("complete", "error"):
        return {"job_id": job_id, "status": status,
                "note": "already terminal"}
    if status != "awaiting_approval":
        raise HTTPException(
            status_code=409,
            detail=f"cannot reject job in state '{status}'; "
                   f"only awaiting_approval jobs can be rejected",
        )
    reason = ""
    if isinstance(payload, dict):
        reason = str(payload.get("reason") or "").strip()
    if not reason:
        reason = "caller_depth rejected by user"
    with JOBS_LOCK:
        jr = JOBS.get(job_id)
        if jr is None:
            raise HTTPException(status_code=404, detail="job vanished from registry")
        jr["status"] = "error"
        jr["error"] = reason[:300]
        jr["completed_at"] = _now()
        _persist(jr)
    log.info("job %s depth rejected: %s", job_id, reason[:200])
    _notify_job_done(job_id)
    return {"job_id": job_id, "status": "error", "note": "rejected", "reason": reason}


@app.get("/features/{project}/{feature}", dependencies=[Depends(require_bearer)])
def feature_timeline(project: str, feature: str):
    """Ordered iteration history for one feature (oldest iteration first)."""
    matches = [
        j for j in _iter_jobs()
        if j.get("project") == project and j.get("feature") == feature
    ]
    if not matches:
        raise HTTPException(status_code=404,
                            detail=f"no jobs for project={project} feature={feature}")
    matches.sort(key=lambda j: j.get("created_at") or "")
    iterations = []
    for i, j in enumerate(matches, start=1):
        s = _job_summary(j)
        s["iteration"] = i
        iterations.append(s)
    return {
        "project": project,
        "feature": feature,
        "iteration_count": len(iterations),
        "iterations": iterations,
    }


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


if __name__ == "__main__":
    # Convenience entrypoint: `python server.py` instead of uvicorn CLI.
    import uvicorn
    host = os.environ.get("REVIEW_SERVER_HOST", "0.0.0.0")
    port = int(os.environ.get("REVIEW_SERVER_PORT", "8080"))
    log.info("starting on %s:%s", host, port)
    uvicorn.run("server:app", host=host, port=port, log_level="info")
