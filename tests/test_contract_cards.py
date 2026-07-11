import contract_cards as cc


def test_cache_roundtrip_and_staleness(tmp_path):
    cache = tmp_path / "contract-cards.json"
    store = cc.CardStore(cache_path=str(cache))
    assert store.get("Foo", mtime=100.0) is None            # empty
    store.put("Foo", mtime=100.0, card={"returns": "null when not found"})
    store.save()

    reloaded = cc.CardStore(cache_path=str(cache))
    assert reloaded.get("Foo", mtime=100.0) == {"returns": "null when not found"}
    # Stale when the source file's mtime moved on.
    assert reloaded.get("Foo", mtime=200.0) is None


def test_card_for_generates_once_then_serves_cache(tmp_path):
    calls = {"n": 0}

    def fake_llm(symbol, signature, source):
        calls["n"] += 1
        return {"returns": "Category or null", "null_behavior": "null when missing",
                "throws": "none", "side_effects": "none"}

    store = cc.CardStore(cache_path=str(tmp_path / "c.json"))
    card1 = cc.card_for(store, symbol="GetCategoryByIdAsync",
                        signature="Task<Category?> GetCategoryByIdAsync(Guid id)",
                        source="public async Task<Category?> ... { return await ...; }",
                        mtime=100.0, generate_fn=fake_llm)
    assert card1["null_behavior"] == "null when missing"
    assert calls["n"] == 1
    # Second call with same mtime -> served from cache, no re-generation.
    cc.card_for(store, symbol="GetCategoryByIdAsync", signature="x", source="y",
                mtime=100.0, generate_fn=fake_llm)
    assert calls["n"] == 1


def _graph_with_def():
    d = {"name": "GetCategoryByIdAsync", "file": "Repositories/QuestionRepository.cs",
         "line_start": 10, "line_end": 15, "signature": "Task<Category?> GetCategoryByIdAsync(Guid id)",
         "enclosing_class": "QuestionRepository"}
    return {"symbol_index": {"GetCategoryByIdAsync": {"defs": [d], "refs": []}}}


def test_cards_for_symbols_and_render(tmp_path, monkeypatch):
    graph = _graph_with_def()
    store = cc.CardStore(cache_path=str(tmp_path / "c.json"))

    # Stub source + mtime resolution (no real git/fs).
    monkeypatch.setattr(cc, "_source_and_mtime",
                        lambda repo, ref, file, ls, le: ("SRC", 100.0))
    gen = lambda s, sig, src: {"returns": "Category or null", "null_behavior": "null when missing",
                               "throws": "none", "side_effects": "none"}

    cards = cc.cards_for_symbols(store, "/repo", "REF", graph,
                                 {"GetCategoryByIdAsync"}, generate_fn=gen)
    assert "GetCategoryByIdAsync" in cards
    md = cc.render_cards_md(cards)
    assert "GetCategoryByIdAsync" in md and "null when missing" in md
    assert md.startswith("### Seam contracts")
