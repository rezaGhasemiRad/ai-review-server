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
