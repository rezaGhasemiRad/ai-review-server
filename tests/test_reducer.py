import reducer


def test_fingerprint_is_stable_across_line_and_model_changes():
    a = {"file": "Services/QuestionService.cs", "symbol": "MoveQuestionsToCategoryAsync",
         "category": "null-handling", "title": "Guid required is a no-op", "line": 472}
    b = dict(a, line=468, found_by="codex")  # different line + model, same bug
    assert reducer.fingerprint("Thabbit", a) == reducer.fingerprint("Thabbit", b)


def test_fingerprint_differs_for_different_bug():
    a = {"file": "svc.cs", "symbol": "Foo", "category": "null-handling", "title": "X"}
    b = {"file": "svc.cs", "symbol": "Foo", "category": "auth", "title": "Y"}
    assert reducer.fingerprint("Thabbit", a) != reducer.fingerprint("Thabbit", b)


def test_fingerprint_is_readable_slug():
    f = {"file": "Services/QuestionService.cs", "symbol": "MoveQuestionsToCategoryAsync",
         "category": "null-handling", "title": "Guid required is a no-op"}
    fp = reducer.fingerprint("Thabbit", f)
    assert fp.startswith("Thabbit:MoveQuestionsToCategoryAsync:null-handling#")
