import verify


def test_verify_skips_high_confidence_and_refutes_low():
    findings = [
        {"id": "A", "confidence": "high", "title": "both models agree"},
        {"id": "B", "confidence": "low",  "title": "shaky one"},
        {"id": "C", "confidence": "low",  "title": "real one"},
    ]
    # Refute votes: B refuted by 2/3 (drop), C refuted by 1/3 (keep).
    votes = {"B": [True, True, False], "C": [False, False, True]}
    calls = {"n": 0}

    def refute(finding, i):
        calls["n"] += 1
        return votes[finding["id"]][i]

    kept = verify.verify_findings(findings, refute_fn=refute, passes=3)
    ids = [f["id"] for f in kept]
    assert "A" in ids                       # high-confidence never refuted
    assert "C" in ids and "B" not in ids    # C survives, B dropped
    assert calls["n"] == 6                  # A skipped; B,C x3 each
    a = next(f for f in kept if f["id"] == "A")
    assert a["verified"] == "by-agreement"
    c = next(f for f in kept if f["id"] == "C")
    assert c["verified"] == "survived-refutation" and c["refute_votes"] == 1
