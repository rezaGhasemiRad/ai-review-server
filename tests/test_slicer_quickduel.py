"""End-to-end slicer validation against the real Thabbit Quick Duel diff.

Skipped unless:
  - caller_graph deps import (tree-sitter), and
  - THABBIT_REPO env var points at a Thabbit clone.

Run on the review server (deps + clone present):
  THABBIT_REPO=/home/ubuntu/ai-review-server/repos/Thabbit \
  QD_BASE=origin/main QD_BRANCH=origin/develop \
  pytest tests/test_slicer_quickduel.py -v -s
"""
import os
import subprocess

import pytest

import slicer

caller_graph = pytest.importorskip("caller_graph")
REPO = os.environ.get("THABBIT_REPO")
if not REPO or not os.path.isdir(REPO):
    pytest.skip("THABBIT_REPO not set to a clone", allow_module_level=True)

BASE = os.environ.get("QD_BASE", "origin/main")
BRANCH = os.environ.get("QD_BRANCH", "origin/develop")


def _quick_duel_diff() -> str:
    # Files touched by any quick-duel commit (union), diffed base...branch.
    log = subprocess.run(
        ["git", "-C", REPO, "log", "-i", "-E",
         "--grep=duel|\\(qd\\)|quick", f"{BASE}..{BRANCH}",
         "--name-only", "--pretty=format:"],
        capture_output=True, text=True, encoding="utf-8", check=True)
    files = sorted({l.strip() for l in log.stdout.splitlines() if l.strip()})
    assert files, "no quick-duel files found; check QD_BASE/QD_BRANCH"
    diff = subprocess.run(
        ["git", "-C", REPO, "diff", f"{BASE}...{BRANCH}", "--", *files],
        capture_output=True, text=True, encoding="utf-8", check=True)
    return diff.stdout


def test_quick_duel_is_fully_covered_and_under_cap():
    # Build the graph at the BRANCH head, not the cached base — the feature's
    # new code does not exist at base, so a base graph makes coverage vacuous.
    graph = caller_graph.build_graph(REPO, BRANCH)
    diff_files = slicer.parse_unified_diff(_quick_duel_diff())
    assert diff_files, "empty diff"

    slices = slicer.slice_feature(graph, diff_files)

    # 1. Every slice fits the cross-model cap.
    oversized = [(s.root, slicer.slice_char_size(s, diff_files))
                 for s in slices if slicer.slice_char_size(s, diff_files) > slicer.MAX_REVIEW_CHARS]
    assert not oversized, f"slices over cap: {oversized}"

    # 2. Every changed symbol the graph knows about is covered by some slice.
    changed = slicer.changed_symbols(
        graph, {p: d.changed_ranges for p, d in diff_files.items()})
    # Guard against a vacuous pass: if the graph were built at base (pre-feature),
    # only a handful of pre-existing modified symbols would map. Quick Duel is a
    # large feature — expect many changed symbols.
    assert len(changed) > 100, (
        f"only {len(changed)} changed symbols mapped — graph likely built at "
        f"the wrong ref (should be {BRANCH})")
    covered = set().union(*(s.symbols for s in slices)) if slices else set()
    missing = changed - covered
    assert not missing, f"uncovered changed symbols: {sorted(missing)[:20]}"

    print(f"\nQuick Duel -> {len(slices)} slices, "
          f"largest {max(slicer.slice_char_size(s, diff_files) for s in slices):,} chars "
          f"(cap {slicer.MAX_REVIEW_CHARS:,})")
