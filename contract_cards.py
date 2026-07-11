"""Contract cards — cached, LLM-generated behavioral summaries of public
methods, used as the cross-slice "seam lens" in sliced reviews.

A card answers: what does this method return, does it return null, what does it
throw, what are its side effects. Generated once per method and cached keyed by
the defining file's mtime, so it's cheap at review time.
"""
from __future__ import annotations

import json
import os
import subprocess


CARD_FIELDS = ("returns", "null_behavior", "throws", "side_effects")


class CardStore:
    """mtime-keyed cache of contract cards, persisted as JSON."""

    def __init__(self, cache_path: str) -> None:
        self.cache_path = cache_path
        self._data: dict[str, dict] = {}
        if os.path.exists(cache_path):
            try:
                self._data = json.load(open(cache_path, encoding="utf-8"))
            except Exception:
                self._data = {}

    def get(self, symbol: str, mtime: float) -> dict | None:
        e = self._data.get(symbol)
        if not e:
            return None
        # Fresh only if the source file hasn't changed since the card was made.
        if abs(float(e.get("mtime", -1)) - float(mtime)) > 1e-6:
            return None
        return e.get("card")

    def put(self, symbol: str, mtime: float, card: dict) -> None:
        self._data[symbol] = {"mtime": float(mtime), "card": card}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
        json.dump(self._data, open(self.cache_path, "w", encoding="utf-8"))


_CARD_PROMPT = (
    "You are documenting a method's CONTRACT for a code reviewer. Read the "
    "method and answer ONLY as minified JSON with keys: returns, null_behavior, "
    "throws, side_effects. Be terse and factual; if unknown, say \"unknown\". "
    "No prose outside the JSON.\n\nSIGNATURE:\n{sig}\n\nSOURCE:\n{src}\n"
)


def default_generate(symbol: str, signature: str, source: str) -> dict:
    """Generate a card via ai_chat.call_claude. Imported lazily so this module
    stays usable without the CLI (tests inject a fake)."""
    import ai_chat
    prompt = _CARD_PROMPT.format(sig=signature or symbol, src=source or "")
    text, _usage = ai_chat.call_claude("You output only minified JSON.", prompt)
    try:
        obj = json.loads(text.strip().strip("`"))
    except Exception:
        obj = {}
    return {k: str(obj.get(k, "unknown")) for k in CARD_FIELDS}


def card_for(store: "CardStore", symbol: str, signature: str, source: str,
             mtime: float, generate_fn=default_generate) -> dict:
    """Return a fresh cached card or generate+cache one."""
    cached = store.get(symbol, mtime)
    if cached is not None:
        return cached
    card = generate_fn(symbol, signature, source)
    store.put(symbol, mtime, card)
    return card


def _source_and_mtime(repo: str, ref: str, file: str, ls: int, le: int) -> tuple[str, float]:
    """Method source (line range at `ref`) and a change key. Uses the file's
    last-commit epoch at `ref` as the mtime proxy (stable across clones,
    unlike filesystem mtime)."""
    blob = subprocess.run(["git", "-C", repo, "show", f"{ref}:{file}"],
                          capture_output=True, text=True, encoding="utf-8")
    lines = blob.stdout.splitlines()
    src = "\n".join(lines[max(ls - 1, 0):le])
    ts = subprocess.run(["git", "-C", repo, "log", "-1", "--format=%ct", ref, "--", file],
                        capture_output=True, text=True, encoding="utf-8")
    mtime = float(ts.stdout.strip() or 0)
    return src, mtime


def cards_for_symbols(store: CardStore, repo: str, ref: str, graph: dict,
                      symbols: set[str], generate_fn=default_generate) -> dict[str, dict]:
    out: dict[str, dict] = {}
    idx = graph.get("symbol_index") or {}
    for sym in sorted(symbols):
        defs = (idx.get(sym) or {}).get("defs") or []
        if not defs:
            continue
        d = defs[0]
        src, mtime = _source_and_mtime(repo, ref, d["file"], d["line_start"], d["line_end"])
        out[sym] = card_for(store, sym, d.get("signature", ""), src, mtime, generate_fn)
    return out


def render_cards_md(cards: dict[str, dict]) -> str:
    if not cards:
        return ""
    lines = ["### Seam contracts (callees on the boundary of this slice)", ""]
    for sym in sorted(cards):
        c = cards[sym]
        lines.append(f"- **{sym}** — returns: {c.get('returns', 'unknown')}; "
                     f"null: {c.get('null_behavior', 'unknown')}; "
                     f"throws: {c.get('throws', 'unknown')}; "
                     f"side effects: {c.get('side_effects', 'unknown')}")
    return "\n".join(lines)
