import ledger


def test_ledger_accumulates_and_reports_by_dimension():
    L = ledger.TokenLedger(window_budget=1_000_000)
    L.add(slice_="s1", model="claude", phase="review",
          usage={"input_tokens": 100, "output_tokens": 20})
    L.add(slice_="s1", model="codex", phase="review",
          usage={"input_tokens": 50, "output_tokens": 10})
    L.add(slice_="s2", model="claude", phase="reconcile",
          usage={"input_tokens": 200, "output_tokens": 40})
    assert L.total_tokens() == 100 + 20 + 50 + 10 + 200 + 40
    assert L.by_model()["claude"] == 100 + 20 + 200 + 40
    assert L.by_slice()["s1"] == 180
    assert round(L.pct_of_window(), 2) == round(420 / 1_000_000 * 100, 2)
    report = L.render()
    assert "claude" in report and "s1" in report
