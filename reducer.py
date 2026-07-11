"""Deterministic reduction of per-slice review findings into one report.

No LLM. Dedup by stable fingerprint, roll up verdicts, rank, and diff against
a prior iteration. The narrative/synthesis LLM pass (Phase 4) runs on the
distilled output of this module.
"""
from __future__ import annotations

import hashlib
import re


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def fingerprint(repo: str, finding: dict) -> str:
    """Readable, stable identity slug. Keys on repo + enclosing symbol (or file
    basename) + category + a short hash of the normalized title. Excludes line
    number and model on purpose."""
    symbol = finding.get("symbol") or (finding.get("file", "").rsplit("/", 1)[-1])
    category = finding.get("category") or "general"
    h = hashlib.sha1(_norm(finding.get("title", "")).encode("utf-8")).hexdigest()[:4]
    return f"{repo}:{symbol}:{category}#{h}"
