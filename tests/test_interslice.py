import interslice


def test_interslice_reviews_each_edge_and_tags_findings():
    seams = {"A": {"B"}, "B": set()}
    cards = {"B": {"returns": "int", "null_behavior": "never", "throws": "none",
                   "side_effects": "writes db"}}
    seen = []

    def edge_review(caller, callee, card):
        seen.append((caller, callee))
        return [{"title": f"{caller} ignores {callee} side effects",
                 "severity": "concern", "file": None}]

    findings = interslice.review_seam_edges(seams, cards, edge_review_fn=edge_review)
    assert seen == [("A", "B")]
    assert len(findings) == 1
    assert findings[0]["slice"] == "inter-slice"
    assert findings[0]["found_by"] == ["claude"]
    assert findings[0]["symbol"] == "B"
    assert findings[0]["category"] == "seam"


def test_interslice_no_edges_is_empty():
    assert interslice.review_seam_edges({"A": set()}, {}, edge_review_fn=lambda *a: []) == []
