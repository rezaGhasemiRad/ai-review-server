"""Adversarial verification of review findings.

Findings both models already agreed on are high-confidence and pass through.
Single-model (low-confidence) findings get N independent skeptic passes that
try to REFUTE them; a finding is dropped only if a majority of passes refute
it. Spends verification budget only where confidence is low.
"""
from __future__ import annotations


def _refute_default(finding: dict, i: int) -> bool:
    """One skeptic pass via ai_chat.call_claude. Returns True if it argues the
    finding is NOT real. Imported lazily (tests inject a fake)."""
    import ai_chat
    prompt = ("A reviewer claims this finding. Try to REFUTE it — is it a false "
              "positive? Answer strictly 'REFUTED' or 'STANDS' on the first line, "
              "then one sentence.\n\nFINDING:\n"
              f"{finding.get('title', '')}\n{finding.get('detail', '') or ''}\n\n"
              f"LOCATION: {finding.get('file', '?')}")
    text, _ = ai_chat.call_claude("You are a skeptical code-review verifier.", prompt)
    return text.strip().upper().startswith("REFUTED")


def verify_findings(findings: list[dict], refute_fn=_refute_default,
                    passes: int = 3) -> list[dict]:
    """Return the findings that survive. High-confidence findings pass through
    (annotated verified='by-agreement'). Low-confidence findings are refuted
    `passes` times and kept only if refutations are a minority."""
    majority = passes // 2 + 1
    kept: list[dict] = []
    for f in findings:
        if f.get("confidence") == "high":
            kept.append({**f, "verified": "by-agreement", "refute_votes": 0})
            continue
        votes = sum(1 for i in range(passes) if refute_fn(f, i))
        if votes < majority:
            kept.append({**f, "verified": "survived-refutation", "refute_votes": votes})
        # else: dropped (majority refuted)
    return kept
