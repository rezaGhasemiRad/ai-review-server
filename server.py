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
import subprocess
import sys
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

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

# One review at a time. Reviews are token-expensive and the underlying CLIs
# (claude / codex) are not designed for high concurrency.
EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reviewer")

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

REPO_URL_RE = re.compile(r"^(https://|git@)[\w.@:/\-~]+\.git$")
SAFE_REF_RE = re.compile(r"^[A-Za-z0-9_./\-]+$")


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
    path = JOBS_DIR / f"{job['job_id']}.json"
    path.write_text(json.dumps(job, indent=2, default=str), encoding="utf-8")


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
    try:
        with JOBS_LOCK:
            req = dict(JOBS[job_id]["request"])
        _update(job_id, status="running", started_at=_now())

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

        if len(prepared) == 1:
            p = prepared[0]
            result = ai_chat.review_branch_structured(
                repo=p["repo"],
                branch=p["branch"],
                base=p["base"],
                only_globs=req.get("only", []),
                include_all=req.get("include_all", False),
                summary_only=req.get("summary_only", False),
                handoff_path=req.get("handoff_path"),
                no_handoff=req.get("no_handoff", False),
                context=req.get("context"),
            )
        else:
            result = ai_chat.review_multi_repo_structured(
                repos=prepared,
                only_globs=req.get("only", []),
                include_all=req.get("include_all", False),
                summary_only=req.get("summary_only", False),
                handoff_path=req.get("handoff_path"),
                no_handoff=req.get("no_handoff", False),
                context=req.get("context"),
            )

        _update(job_id, status="complete", completed_at=_now(), result=result)
        log.info("job %s complete (%d repo(s))", job_id, len(prepared))
    except SystemExit as e:
        # ai_chat helpers use sys.exit for input errors; convert to job failure.
        _update(job_id, status="error", completed_at=_now(),
                error=str(e) or "input validation failed")
        log.warning("job %s failed (validation): %s", job_id, e)
    except Exception as e:
        _update(job_id, status="error", completed_at=_now(),
                error=f"{type(e).__name__}: {e}",
                error_trace=traceback.format_exc())
        log.exception("job %s failed", job_id)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

app = FastAPI(
    title="AI Review Server",
    description="Triggers Claude+Codex reviews of feature branches.",
)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "time": _now()}


@app.post("/reviews", response_model=JobSubmitted, status_code=202,
          dependencies=[Depends(require_bearer)])
def submit_review(req: ReviewRequest, request: Request) -> JobSubmitted:
    # Validate the either/or shape up-front so the worker can't get a malformed job.
    try:
        repos = req.normalized_repos()
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "status": "queued",
        "created_at": _now(),
        "request": req.model_dump(),
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    _persist(job)
    EXECUTOR.submit(_run_job, job_id)
    summary = ", ".join(f"{r.repo_url}@{r.branch}" for r in repos)
    log.info("queued job %s across %d repo(s): %s", job_id, len(repos), summary)
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
    return job


@app.get("/reviews", dependencies=[Depends(require_bearer)])
def list_reviews(limit: int = 20):
    files = sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files[: max(1, min(limit, 100))]:
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
            out.append({
                "job_id": j.get("job_id"),
                "status": j.get("status"),
                "branch": j.get("request", {}).get("branch"),
                "created_at": j.get("created_at"),
                "completed_at": j.get("completed_at"),
            })
        except Exception:
            continue
    return {"jobs": out}


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
