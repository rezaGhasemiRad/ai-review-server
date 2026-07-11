"""Per-stage / per-slice / per-model token ledger for a sliced review."""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class _Entry:
    slice_: str
    model: str
    phase: str
    tokens: int


class TokenLedger:
    def __init__(self, window_budget: int = 0) -> None:
        self.window_budget = window_budget
        self.entries: list[_Entry] = []

    def add(self, slice_: str, model: str, phase: str, usage: dict) -> None:
        toks = int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)
        self.entries.append(_Entry(slice_, model, phase, toks))

    def total_tokens(self) -> int:
        return sum(e.tokens for e in self.entries)

    def _group(self, key) -> dict:
        out: dict = {}
        for e in self.entries:
            out[key(e)] = out.get(key(e), 0) + e.tokens
        return out

    def by_model(self) -> dict:
        return self._group(lambda e: e.model)

    def by_slice(self) -> dict:
        return self._group(lambda e: e.slice_)

    def by_phase(self) -> dict:
        return self._group(lambda e: e.phase)

    def pct_of_window(self) -> float:
        return (self.total_tokens() / self.window_budget * 100) if self.window_budget else 0.0

    def render(self) -> str:
        lines = [f"token ledger: {self.total_tokens():,} tokens "
                 f"({self.pct_of_window():.1f}% of 5h window)"]
        for label, grp in (("by model", self.by_model()),
                           ("by phase", self.by_phase()),
                           ("by slice", self.by_slice())):
            lines.append(f"  {label}: " +
                         ", ".join(f"{k}={v:,}" for k, v in sorted(grp.items())))
        return "\n".join(lines)
