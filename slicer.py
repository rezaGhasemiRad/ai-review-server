"""Deterministic feature-diff slicer for the AI Review Server.

Turns a unified diff + a caller_graph symbol graph into call-graph-root-
anchored vertical slices. Each slice is a controller/job/hub -> service ->
repo path. The union of slices covers 100% of the changed symbols; each
slice's changed-file diff fits under the review size cap.

No LLM. Pure graph traversal — reproducible and unit-testable.
"""
from __future__ import annotations

import dataclasses
import re

# Keep in sync with ai_chat.MAX_REVIEW_CHARS (mixed Claude+Codex cap).
# Imported lazily so the slicer stays unit-testable standalone.
# NOTE: ai_chat.py calls sys.exit() at import when the `claude`/`codex` CLIs
# are absent (SystemExit is NOT an Exception subclass), so catch it too.
try:
    from ai_chat import MAX_REVIEW_CHARS
except (Exception, SystemExit):  # pragma: no cover - fallback for isolated tests
    MAX_REVIEW_CHARS = 1_100_000


@dataclasses.dataclass
class Slice:
    """One vertical review unit."""
    root: str                       # anchoring symbol name (or "orphan:<file>::<sym>")
    root_type: str                  # endpoint|hub|job|consumer|unknown|orphan
    symbols: set[str] = dataclasses.field(default_factory=set)
    files: set[str] = dataclasses.field(default_factory=set)

    def __post_init__(self) -> None:
        self.symbols = set(self.symbols)
        self.files = set(self.files)
