import ai_chat


def _fake_review(**kw):
    # Echo which files were reviewed so we can assert per-slice dispatch.
    files = tuple(sorted(kw.get("only_globs") or []))
    svc = "Services/QuestionService.cs" in files
    return {
        "claude_verdict": "request-changes" if svc else "approve",
        "codex_verdict": "approve",
        "claude_findings": [{"file": files[0], "symbol": "Foo", "category": "null-handling",
                             "title": "bug", "severity": "concern"}] if files else [],
        "codex_findings": [],
        "usage": {"claude": {"input_tokens": 10, "output_tokens": 2},
                  "codex": {"input_tokens": 5, "output_tokens": 1}},
    }


def _two_slice_graph():
    def d(name, ls, le):
        return {"name": name, "kind": "method", "line_start": ls, "line_end": le,
                "signature": "", "enclosing_class": ""}
    files = {
        "Services/QuestionService.cs": {"language": "csharp", "defs": [d("A", 1, 9)], "refs": []},
        "Jobs/Job.cs": {"language": "csharp", "defs": [d("B", 1, 9)], "refs": []},
    }
    si = {
        "A": {"defs": [{**files["Services/QuestionService.cs"]["defs"][0],
                        "file": "Services/QuestionService.cs"}], "refs": []},
        "B": {"defs": [{**files["Jobs/Job.cs"]["defs"][0], "file": "Jobs/Job.cs"}], "refs": []},
    }
    return {"files": files, "symbol_index": si}


DIFF = ("diff --git a/Services/QuestionService.cs b/Services/QuestionService.cs\n"
        "@@ -1,1 +1,3 @@\n+x\n+y\n"
        "diff --git a/Jobs/Job.cs b/Jobs/Job.cs\n"
        "@@ -1,1 +1,3 @@\n+z\n+w\n")


def test_sliced_review_dispatches_per_slice_and_reduces(monkeypatch):
    monkeypatch.setattr(ai_chat, "_build_graph_at_branch", lambda repo, branch: _two_slice_graph())
    monkeypatch.setattr(ai_chat, "_feature_diff_text",
                        lambda repo, branch, base, only_globs=None: DIFF)

    out = ai_chat.review_branch_sliced_structured(
        repo="/x", branch="origin/develop", base="origin/main",
        review_fn=_fake_review, pack_target=1)   # tiny target -> no merge

    assert out["verdict"] == "request-changes"          # rolled up from the svc slice
    assert out["findings"], "reduced findings present"
    assert out["token_ledger"]["total_tokens"] > 0
    assert out["slice_count"] >= 2
    assert all(f.get("id") for f in out["findings"])


def test_sliced_review_iteration_diff_when_prior_given(monkeypatch):
    monkeypatch.setattr(ai_chat, "_build_graph_at_branch", lambda repo, branch: _two_slice_graph())
    monkeypatch.setattr(ai_chat, "_feature_diff_text",
                        lambda repo, branch, base, only_globs=None: DIFF)
    first = ai_chat.review_branch_sliced_structured(
        repo="/x", branch="b", base="c", review_fn=_fake_review, pack_target=1)
    second = ai_chat.review_branch_sliced_structured(
        repo="/x", branch="b", base="c", review_fn=_fake_review, pack_target=1,
        prior_result=first)
    # Same findings both runs -> all still present, none resolved/new.
    assert second["iteration"]["resolved"] == []
    assert second["iteration"]["newly_introduced"] == []
    assert second["iteration"]["still_present"]
