import reducer


def test_fingerprint_is_stable_across_line_and_model_changes():
    a = {"file": "Services/QuestionService.cs", "symbol": "MoveQuestionsToCategoryAsync",
         "category": "null-handling", "title": "Guid required is a no-op", "line": 472}
    b = dict(a, line=468, found_by="codex")  # different line + model, same bug
    assert reducer.fingerprint("Thabbit", a) == reducer.fingerprint("Thabbit", b)


def test_fingerprint_handles_null_fields():
    # A finding with file/symbol/title present-but-None (real reviewer output
    # for a file-less finding) must not crash.
    f = {"file": None, "symbol": None, "category": None, "title": None}
    fp = reducer.fingerprint("Thabbit", f)
    assert fp.startswith("Thabbit:unknown:general#")
    # reduce() must survive such findings too.
    out = reducer.reduce("Thabbit", [{"slice": "s1", "verdict": "comment",
                                       "claude_findings": [f], "codex_findings": []}])
    assert len(out["findings"]) == 1


def test_fingerprint_differs_for_different_bug():
    a = {"file": "svc.cs", "symbol": "Foo", "category": "null-handling", "title": "X"}
    b = {"file": "svc.cs", "symbol": "Foo", "category": "auth", "title": "Y"}
    assert reducer.fingerprint("Thabbit", a) != reducer.fingerprint("Thabbit", b)


def test_fingerprint_is_readable_slug():
    f = {"file": "Services/QuestionService.cs", "symbol": "MoveQuestionsToCategoryAsync",
         "category": "null-handling", "title": "Guid required is a no-op"}
    fp = reducer.fingerprint("Thabbit", f)
    assert fp.startswith("Thabbit:MoveQuestionsToCategoryAsync:null-handling#")


def test_reduce_dedups_and_records_provenance_and_rolls_up_verdict():
    f = lambda title, cat="null-handling", sym="Foo", sev="concern": {
        "file": "svc.cs", "symbol": sym, "category": cat, "title": title, "severity": sev}
    slice_results = [
        {"slice": "s1", "verdict": "request-changes",
         "claude_findings": [f("Guid required no-op")],
         "codex_findings": [f("Guid required no-op")]},          # same bug, both models
        {"slice": "s2", "verdict": "approve",
         "claude_findings": [f("Guid required no-op")],          # same bug again (shared code)
         "codex_findings": [f("missing tests", cat="test-coverage", sym="Bar")]},
    ]
    out = reducer.reduce("Thabbit", slice_results)
    assert out["verdict"] == "request-changes"   # worst-of
    # The Guid bug is one finding, found_by both, seen in both slices.
    guid = [x for x in out["findings"] if x["category"] == "null-handling"]
    assert len(guid) == 1
    assert set(guid[0]["found_by"]) == {"claude", "codex"}
    assert set(guid[0]["slices"]) == {"s1", "s2"}
    assert guid[0]["confidence"] == "high"        # both models
    # Ranked: high-confidence first.
    assert out["findings"][0]["category"] == "null-handling"
    # Back-compat: per-model columns for the UI. The shared bug (both models)
    # appears in both; the codex-only "missing tests" only in codex.
    assert out["reviewers"] == ["claude", "codex"]
    assert any(f["category"] == "null-handling" for f in out["claude_findings"])
    assert any(f["category"] == "null-handling" for f in out["codex_findings"])
    assert any(f["category"] == "test-coverage" for f in out["codex_findings"])
    assert not any(f["category"] == "test-coverage" for f in out["claude_findings"])


def test_iteration_diff_classifies_by_fingerprint():
    prior = {"findings": [{"id": "R:Foo:null#aaaa"}, {"id": "R:Bar:auth#bbbb"}]}
    current = {"findings": [{"id": "R:Foo:null#aaaa"}, {"id": "R:Baz:perf#cccc"}]}
    d = reducer.iteration_diff(current, prior)
    assert d["still_present"] == ["R:Foo:null#aaaa"]
    assert d["resolved"] == ["R:Bar:auth#bbbb"]
    assert d["newly_introduced"] == ["R:Baz:perf#cccc"]
