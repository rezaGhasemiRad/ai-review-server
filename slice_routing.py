"""Decide whether a feature review should go through the sliced path.

Kept as a tiny standalone module (no FastAPI / no server import) so the
routing decision is unit-testable off-server.
"""
from __future__ import annotations

import subprocess


def feature_diff_size(repo: str, branch: str, base: str) -> int:
    """Char size of `git diff base...branch` — the payload a single review
    would have to ingest."""
    out = subprocess.run(["git", "-C", repo, "diff", f"{base}...{branch}"],
                         capture_output=True, text=True, encoding="utf-8", check=True)
    return len(out.stdout)


def should_slice(req: dict, repo: str, branch: str, base: str, cap: int) -> bool:
    """Slice when the caller opts in (req['sliced']) or the feature diff exceeds
    the mixed-model cap. On any error measuring size, fall back to NOT slicing
    (preserve today's single-review behavior)."""
    if req.get("sliced"):
        return True
    try:
        return feature_diff_size(repo, branch, base) > cap
    except Exception:
        return False
