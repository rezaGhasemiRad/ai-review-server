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
    """Annotate every finding with a verification status and DOWN-RANK the
    contested ones — never drop. Silently deleting a finding risks discarding a
    true positive (the refuter can be wrong, especially without full context),
    which is worse than surfacing a low-priority note. Statuses:

      by-agreement       — both models flagged it (high confidence); not refuted
      survived-refutation — single-model, refutations a minority
      contested          — single-model, majority refuted (kept, ranked last)

    Contested findings are pushed to the end (stable sort preserves the
    reducer's ordering within each group)."""
    majority = passes // 2 + 1
    out: list[dict] = []
    for f in findings:
        if f.get("confidence") == "high":
            out.append({**f, "verified": "by-agreement", "refute_votes": 0})
            continue
        votes = sum(1 for i in range(passes) if refute_fn(f, i))
        status = "contested" if votes >= majority else "survived-refutation"
        out.append({**f, "verified": status, "refute_votes": votes})
    out.sort(key=lambda f: 1 if f.get("verified") == "contested" else 0)
    return out
