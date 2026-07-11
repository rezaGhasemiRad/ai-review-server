"""Inter-slice seam pass: review each cross-slice cut edge (a caller in one
slice invoking a callee owned by another) against the callee's contract card.
Catches "each slice was fine alone, but A misuses B" bugs.
"""
from __future__ import annotations


def _edge_review_default(caller: str, callee: str, card: dict) -> list[dict]:
    """Review one edge via ai_chat.call_claude. Returns a (possibly empty) list
    of finding dicts. Imported lazily (tests inject a fake)."""
    import ai_chat
    import json
    prompt = ("Does the caller correctly use the callee, given the callee's "
              "contract? Report ONLY real handoff bugs as a minified JSON array "
              "of {title, detail, severity}. Empty array if fine.\n\n"
              f"CALLER (slice root): {caller}\nCALLEE: {callee}\n"
              f"CALLEE CONTRACT: {json.dumps(card)}\n")
    text, _ = ai_chat.call_claude("You output only a minified JSON array.", prompt)
    try:
        arr = json.loads(text.strip().strip("`"))
        return arr if isinstance(arr, list) else []
    except Exception:
        return []


def review_seam_edges(seams_by_slice: dict[str, set[str]], cards: dict[str, dict],
                      edge_review_fn=_edge_review_default) -> list[dict]:
    """For every (caller-slice -> callee) cut edge, review the handoff. Findings
    are tagged as the synthetic 'inter-slice' slice, single-model, so the
    reducer folds them in with per-slice findings."""
    out: list[dict] = []
    for caller in sorted(seams_by_slice):
        for callee in sorted(seams_by_slice[caller]):
            for f in edge_review_fn(caller, callee, cards.get(callee, {})) or []:
                out.append({**f, "slice": "inter-slice",
                            "symbol": f.get("symbol") or callee,
                            "category": f.get("category") or "seam",
                            "found_by": ["claude"]})
    return out
