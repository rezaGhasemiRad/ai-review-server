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
