import verify


def test_verify_annotates_and_downranks_without_dropping():
    findings = [
        {"id": "A", "confidence": "high", "title": "both models agree"},
        {"id": "B", "confidence": "low",  "title": "shaky one"},
        {"id": "C", "confidence": "low",  "title": "real one"},
    ]
    # Refute votes: B refuted 2/3 (contested), C refuted 1/3 (survives).
    votes = {"B": [True, True, False], "C": [False, False, True]}
    calls = {"n": 0}

    def refute(finding, i):
        calls["n"] += 1
        return votes[finding["id"]][i]

    kept = verify.verify_findings(findings, refute_fn=refute, passes=3)
    ids = [f["id"] for f in kept]
    assert set(ids) == {"A", "B", "C"}      # NOTHING dropped — non-destructive
    assert calls["n"] == 6                  # A skipped (high conf); B,C x3 each
    byid = {f["id"]: f for f in kept}
    assert byid["A"]["verified"] == "by-agreement"
    assert byid["C"]["verified"] == "survived-refutation" and byid["C"]["refute_votes"] == 1
    assert byid["B"]["verified"] == "contested" and byid["B"]["refute_votes"] == 2
    assert ids[-1] == "B"                    # contested down-ranked to the end
