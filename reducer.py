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
    # Fields can be present-but-null (e.g. a file-less "no test coverage"
    # finding), so coerce with `or ""` — .get(k, "") does NOT help when the
    # value is None.
    symbol = finding.get("symbol") or (finding.get("file") or "").rsplit("/", 1)[-1] or "unknown"
    category = finding.get("category") or "general"
    h = hashlib.sha1(_norm(finding.get("title") or "").encode("utf-8")).hexdigest()[:4]
    return f"{repo}:{symbol}:{category}#{h}"


_SEV_RANK = {"bug": 0, "concern": 1, "nit": 2, "": 3}
_VERDICT_RANK = {"request-changes": 0, "comment": 1, "approve": 2, None: 3}


def _rollup_verdict(verdicts: list[str]) -> str:
    present = [v for v in verdicts if v]
    if not present:
        return "comment"
    return min(present, key=lambda v: _VERDICT_RANK.get(v, 3))


def reduce(repo: str, slice_results: list[dict]) -> dict:
    """Dedup findings by fingerprint across slices; record found_by / slices /
    confidence; roll up the verdict (worst-of); rank by severity x confidence."""
    merged: dict[str, dict] = {}
    for sr in slice_results:
        slice_name = sr.get("slice")
        for model in ("claude", "codex"):
            for finding in sr.get(f"{model}_findings", []) or []:
                fp = fingerprint(repo, finding)
                m = merged.get(fp)
                if m is None:
                    m = {**finding, "id": fp, "found_by": set(), "slices": set()}
                    merged[fp] = m
                m["found_by"].add(model)
                m["slices"].add(slice_name)

    findings = []
    for m in merged.values():
        m["found_by"] = sorted(m["found_by"])
        m["slices"] = sorted(m["slices"])
        m["confidence"] = ("high" if len(m["found_by"]) > 1 or len(m["slices"]) > 1
                           else "low")
        findings.append(m)

    findings.sort(key=lambda x: (0 if x["confidence"] == "high" else 1,
                                 _SEV_RANK.get(x.get("severity", ""), 3),
                                 x["id"]))
    # Back-compat with the UI/API, which render per-model columns: split the
    # merged findings by `found_by` (a finding flagged by both appears in both,
    # exactly like the single-review pipeline). Keep merged `findings` too.
    claude_findings = [f for f in findings if "claude" in f["found_by"]]
    codex_findings = [f for f in findings if "codex" in f["found_by"]]
    return {"verdict": _rollup_verdict([sr.get("verdict") for sr in slice_results]),
            "findings": findings,
            "claude_findings": claude_findings,
            "codex_findings": codex_findings,
            "reviewers": ["claude", "codex"]}


def iteration_diff(current: dict, prior: dict | None) -> dict:
    """Classify current findings against a prior reduced report by fingerprint."""
    cur = {f["id"] for f in current.get("findings", [])}
    old = {f["id"] for f in (prior or {}).get("findings", [])}
    return {
        "still_present": sorted(cur & old),
        "resolved": sorted(old - cur),
        "newly_introduced": sorted(cur - old),
    }
