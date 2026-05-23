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
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
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
        body = (
            f"Review COMPLETE — {pf}\n"
            f"Claude: {cv} ({cf} findings)\n"
            f"Codex:  {xv} ({xf} findings)\n"
            f"Tokens: {tin:,} in / {tout:,} out\n"
        )
    else:
        err = (job.get("error") or "").strip()
        body = f"Review FAILED — {pf}\nError: {err[:300]}\n"

    if link:
        body += link
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


def _load_prior_result(job_id: str) -> Optional[dict]:
    """Return the `result` field of a prior complete job, or None."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        job = _load_from_disk(job_id)
    if job is None or job.get("status") != "complete":
        return None
    return job.get("result")


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

        # Prior iteration context (optional).
        prior_result = None
        if req.get("previous_job_id"):
            prior_result = _load_prior_result(req["previous_job_id"])
            if prior_result is None:
                log.warning("previous_job_id %s did not resolve to a completed job; ignoring",
                            req["previous_job_id"])

        # Run per-repo tests in parallel (default on).
        test_results_by_repo: dict = {}
        if req.get("run_tests", True):
            timeout_s = int(req.get("test_timeout_s") or 600)
            log.info("running tests SEQUENTIALLY across %d repo(s), timeout=%ds each",
                     len(prepared), timeout_s)
            # Sequential, not parallel: a Tier-1 run can be a full `next build`
            # (~1.5 GB RAM, CPU-bound). Running N of them at once overcommits
            # the box (load 9, OOM). One at a time, each build gets the whole
            # machine — faster per build and never thrashes.
            for i, p in enumerate(prepared, 1):
                short = _short_repo(p["repo_url"])
                progress(f"Tier-1 tests ({i}/{len(prepared)}): {short}")
                tr = _run_repo_tests(p["repo"], p["branch"], timeout_s)
                test_results_by_repo[p["repo_url"]] = tr
                log.info("test result %s: status=%s exit=%s dur=%ss",
                         p["repo_url"], tr.get("status"), tr.get("exit_code"),
                         tr.get("duration_s"))

        progress("starting review")
        common = dict(
            only_globs=req.get("only", []),
            include_all=req.get("include_all", False),
            summary_only=req.get("summary_only", False),
            handoff_path=req.get("handoff_path"),
            no_handoff=req.get("no_handoff", False),
            context=req.get("context"),
            prior_result=prior_result,
            test_results_by_repo=test_results_by_repo or None,
            progress_cb=progress,
            feature=job_feature,   # enables .ai-collab/handoffs/<feature>.md discovery
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
    job = {
        "job_id": job_id,
        "status": "queued",
        "created_at": _now(),
        "project": project,
        "feature": feature,
        "previous_job_id": req.previous_job_id,
        "request": req.model_dump(),
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    _persist(job)
    fut = EXECUTOR.submit(_run_job, job_id)
    # Hold the Future in-memory so /cancel can call .cancel() on a queued job.
    # The leading underscore keeps _persist from trying to serialize it.
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id]["_future"] = fut
    summary = ", ".join(f"{r.repo_url}@{r.branch}" for r in repos)
    log.info("queued job %s [project=%s feature=%s] across %d repo(s): %s",
             job_id, project, feature, len(repos), summary)
    base_url = str(request.base_url).rstrip("/")
    return JobSubmitted(
        job_id=job_id, status="queued", status_url=f"{base_url}/reviews/{job_id}"
    )


@app.get("/reviews/{job_id}", dependencies=[Depends(require_bearer)])
def get_review(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        job = _load_from_disk(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    # Strip in-memory-only keys (e.g. _future from /cancel) — they aren't
    # JSON-serializable and FastAPI would 500 otherwise.
    return {k: v for k, v in job.items() if not k.startswith("_")}


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

    # status == "queued": try to remove from the executor's queue, then mark errored.
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
