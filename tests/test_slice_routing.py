import slice_routing


def test_should_slice_when_diff_over_cap(monkeypatch):
    monkeypatch.setattr(slice_routing, "feature_diff_size", lambda repo, branch, base: 2_000_000)
    assert slice_routing.should_slice({}, "/r", "origin/develop", "origin/main", cap=1_100_000) is True

    monkeypatch.setattr(slice_routing, "feature_diff_size", lambda repo, branch, base: 10_000)
    assert slice_routing.should_slice({}, "/r", "origin/develop", "origin/main", cap=1_100_000) is False


def test_explicit_opt_in_always_slices():
    assert slice_routing.should_slice({"sliced": True}, "/r", "b", "c", cap=1_100_000) is True


def test_diff_size_error_defaults_to_not_slicing(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("git failed")
    monkeypatch.setattr(slice_routing, "feature_diff_size", boom)
    assert slice_routing.should_slice({}, "/r", "b", "c", cap=1_100_000) is False
