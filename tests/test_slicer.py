import pytest
import slicer


@pytest.fixture
def graph():
    """A tiny two-feature repo:

    Controllers/AdminQuestionController.cs
        MoveQuestions [endpoint]  (L10-20)  -> MoveQuestionsToCategoryAsync
    Services/QuestionService.cs
        MoveQuestionsToCategoryAsync (L10-30) -> GetCategoryByIdAsync, MoveQuestionsCategoryAsync
    Repositories/QuestionRepository.cs
        GetCategoryByIdAsync (L10-15)      [leaf]
        MoveQuestionsCategoryAsync (L20-28)[leaf]
    Jobs/SalesImportJob.cs
        ExecuteAsync [job] (L10-25)        -> InsertSalesDetails
    Services/SalesService.cs
        InsertSalesDetails (L10-40)        -> BulkInsert
    Repositories/SalesRepository.cs
        BulkInsert (L10-18)                [leaf]
    Util/StringHelpers.cs
        Slugify (L5-9)                     [orphan: no callers, calls nothing]
    """
    def d(name, file, ls, le, kind="method", cls="", sig=""):
        return {"name": name, "kind": kind, "line_start": ls, "line_end": le,
                "signature": sig, "enclosing_class": cls}

    def r(name, file, line, caller, fs, fe, receiver="_x"):
        return {"name": name, "line": line, "ref_kind": "call", "receiver": receiver,
                "enclosing_function_name": caller,
                "enclosing_function_start": fs, "enclosing_function_end": fe}

    ctrl = "Controllers/AdminQuestionController.cs"
    svc = "Services/QuestionService.cs"
    repo = "Repositories/QuestionRepository.cs"
    job = "Jobs/SalesImportJob.cs"
    ssvc = "Services/SalesService.cs"
    srepo = "Repositories/SalesRepository.cs"
    util = "Util/StringHelpers.cs"

    files = {
        ctrl: {"language": "csharp",
               "defs": [d("MoveQuestions", ctrl, 10, 20, cls="AdminQuestionController",
                          sig='[HttpPost("move-category")] public async Task<IActionResult> MoveQuestions()')],
               "refs": [r("MoveQuestionsToCategoryAsync", ctrl, 15, "MoveQuestions", 10, 20)]},
        svc: {"language": "csharp",
              "defs": [d("MoveQuestionsToCategoryAsync", svc, 10, 30, cls="QuestionService")],
              "refs": [r("GetCategoryByIdAsync", svc, 14, "MoveQuestionsToCategoryAsync", 10, 30),
                       r("MoveQuestionsCategoryAsync", svc, 22, "MoveQuestionsToCategoryAsync", 10, 30)]},
        repo: {"language": "csharp",
               "defs": [d("GetCategoryByIdAsync", repo, 10, 15, cls="QuestionRepository"),
                        d("MoveQuestionsCategoryAsync", repo, 20, 28, cls="QuestionRepository")],
               "refs": []},
        job: {"language": "csharp",
              "defs": [d("ExecuteAsync", job, 10, 25, cls="SalesImportJob",
                         sig="public class SalesImportJob : BackgroundService")],
              "refs": [r("InsertSalesDetails", job, 18, "ExecuteAsync", 10, 25)]},
        ssvc: {"language": "csharp",
               "defs": [d("InsertSalesDetails", ssvc, 10, 40, cls="SalesService")],
               "refs": [r("BulkInsert", ssvc, 30, "InsertSalesDetails", 10, 40)]},
        srepo: {"language": "csharp",
                "defs": [d("BulkInsert", srepo, 10, 18, cls="SalesRepository")],
                "refs": []},
        util: {"language": "csharp",
               "defs": [d("Slugify", util, 5, 9, cls="StringHelpers")],
               "refs": []},
    }
    return {"files": files, "symbol_index": _index(files)}


def _index(files):
    """Mirror caller_graph._rebuild_symbol_index for the fixture."""
    idx = {}
    for path, fdata in files.items():
        for de in fdata["defs"]:
            idx.setdefault(de["name"], {"defs": [], "refs": []})
            idx[de["name"]]["defs"].append({**de, "file": path})
        for re_ in fdata["refs"]:
            idx.setdefault(re_["name"], {"defs": [], "refs": []})
            idx[re_["name"]]["refs"].append({**re_, "file": path})
    return idx


def test_slice_dataclass_normalizes_collections():
    s = slicer.Slice(root="MoveQuestions", root_type="endpoint",
                     symbols=["MoveQuestions"], files=["a.cs"])
    assert s.symbols == {"MoveQuestions"}
    assert s.files == {"a.cs"}


DIFF = """diff --git a/Services/QuestionService.cs b/Services/QuestionService.cs
index 111..222 100644
--- a/Services/QuestionService.cs
+++ b/Services/QuestionService.cs
@@ -12,3 +12,5 @@ public class QuestionService
 existing line
+added line one
+added line two
 tail line
diff --git a/Repositories/QuestionRepository.cs b/Repositories/QuestionRepository.cs
index 333..444 100644
--- a/Repositories/QuestionRepository.cs
+++ b/Repositories/QuestionRepository.cs
@@ -20,0 +21,2 @@ public class QuestionRepository
+new repo line
+another repo line
"""


def test_parse_unified_diff_extracts_files_ranges_and_text():
    parsed = slicer.parse_unified_diff(DIFF)
    assert set(parsed) == {"Services/QuestionService.cs",
                           "Repositories/QuestionRepository.cs"}
    svc = parsed["Services/QuestionService.cs"]
    # New-file hunk starts at 12, spans 5 lines -> [12, 16]
    assert svc.changed_ranges == [(12, 16)]
    assert svc.char_count == len(svc.text) > 0
    repo = parsed["Repositories/QuestionRepository.cs"]
    assert repo.changed_ranges == [(21, 22)]


def test_changed_symbols_maps_ranges_to_enclosing_defs(graph):
    # A change at lines 22-23 of QuestionService is inside
    # MoveQuestionsToCategoryAsync (L10-30).
    changed = {"Services/QuestionService.cs": [(22, 23)]}
    assert slicer.changed_symbols(graph, changed) == {"MoveQuestionsToCategoryAsync"}


def test_changed_symbols_multiple_files_and_defs(graph):
    changed = {
        "Repositories/QuestionRepository.cs": [(12, 12), (25, 25)],  # both repo methods
        "Util/StringHelpers.cs": [(6, 6)],                            # Slugify
    }
    assert slicer.changed_symbols(graph, changed) == {
        "GetCategoryByIdAsync", "MoveQuestionsCategoryAsync", "Slugify"}


def test_callees_finds_downstream_symbols(graph):
    assert slicer.callees(graph, "MoveQuestionsToCategoryAsync") == {
        "GetCategoryByIdAsync", "MoveQuestionsCategoryAsync"}
    assert slicer.callees(graph, "BulkInsert") == set()  # leaf


def test_callers_finds_upstream_symbols(graph):
    assert slicer.callers(graph, "MoveQuestionsToCategoryAsync") == {"MoveQuestions"}
    assert slicer.callers(graph, "MoveQuestions") == set()  # framework-invoked


def test_is_root_true_for_framework_invoked(graph):
    assert slicer.is_root(graph, "MoveQuestions") is True      # endpoint
    assert slicer.is_root(graph, "ExecuteAsync") is True       # job
    assert slicer.is_root(graph, "Slugify") is True            # orphan (no callers)
    assert slicer.is_root(graph, "MoveQuestionsToCategoryAsync") is False


def test_classify_root_from_signature(graph):
    assert slicer.classify_root(graph, "MoveQuestions") == "endpoint"
    assert slicer.classify_root(graph, "ExecuteAsync") == "job"
    assert slicer.classify_root(graph, "Slugify") == "unknown"


def test_build_slice_collects_vertical_path(graph):
    s = slicer.build_slice(graph, "MoveQuestions")
    assert s.root == "MoveQuestions"
    assert s.root_type == "endpoint"
    assert s.symbols == {"MoveQuestions", "MoveQuestionsToCategoryAsync",
                         "GetCategoryByIdAsync", "MoveQuestionsCategoryAsync"}
    assert s.files == {"Controllers/AdminQuestionController.cs",
                       "Services/QuestionService.cs",
                       "Repositories/QuestionRepository.cs"}


def test_build_slice_job_root(graph):
    s = slicer.build_slice(graph, "ExecuteAsync")
    assert s.root_type == "job"
    assert s.symbols == {"ExecuteAsync", "InsertSalesDetails", "BulkInsert"}


def test_slice_feature_covers_all_changed_symbols_via_roots_and_orphans(graph):
    # Change touches: the endpoint path (svc), the job path (job), and an
    # orphan (Slugify) whose caller is not in the diff.
    diff_files = {
        "Services/QuestionService.cs": slicer.DiffFile(
            "Services/QuestionService.cs", text="x" * 100, changed_ranges=[(22, 23)]),
        "Jobs/SalesImportJob.cs": slicer.DiffFile(
            "Jobs/SalesImportJob.cs", text="y" * 100, changed_ranges=[(18, 18)]),
        "Util/StringHelpers.cs": slicer.DiffFile(
            "Util/StringHelpers.cs", text="z" * 100, changed_ranges=[(6, 6)]),
    }
    slices = slicer.slice_feature(graph, diff_files)
    roots = {s.root for s in slices}
    # The changed service symbol is reached via its endpoint root, not itself.
    assert "MoveQuestions" in roots
    assert "ExecuteAsync" in roots
    # Slugify is a root itself (no callers) -> its own slice.
    assert "Slugify" in roots

    # COVERAGE GUARANTEE: every changed symbol appears in at least one slice.
    covered = set().union(*(s.symbols for s in slices))
    changed = slicer.changed_symbols(graph, {p: d.changed_ranges
                                             for p, d in diff_files.items()})
    assert changed <= covered


def test_slice_feature_orphan_when_caller_not_changed(graph):
    # Only the leaf repo method changed; its caller (the service) is unchanged
    # and out of the diff. It must still be covered — via its endpoint root,
    # which is discoverable through the graph even though unchanged.
    diff_files = {
        "Repositories/QuestionRepository.cs": slicer.DiffFile(
            "Repositories/QuestionRepository.cs", text="x" * 100,
            changed_ranges=[(21, 22)]),  # MoveQuestionsCategoryAsync
    }
    slices = slicer.slice_feature(graph, diff_files)
    covered = set().union(*(s.symbols for s in slices))
    assert "MoveQuestionsCategoryAsync" in covered


def test_slice_char_size_sums_changed_file_diffs(graph):
    diff_files = {
        "Services/QuestionService.cs": slicer.DiffFile(
            "Services/QuestionService.cs", text="a" * 300, changed_ranges=[(22, 23)]),
        "Controllers/AdminQuestionController.cs": slicer.DiffFile(
            "Controllers/AdminQuestionController.cs", text="b" * 200, changed_ranges=[(15, 15)]),
    }
    s = slicer.build_slice(graph, "MoveQuestions")
    # Only files present in BOTH the slice and the diff count.
    assert slicer.slice_char_size(s, diff_files) == 500


def test_sub_split_descends_when_over_cap(graph):
    big = {
        "Controllers/AdminQuestionController.cs": slicer.DiffFile(
            "Controllers/AdminQuestionController.cs", text="c" * 10, changed_ranges=[(15, 15)]),
        "Services/QuestionService.cs": slicer.DiffFile(
            "Services/QuestionService.cs", text="c" * 100, changed_ranges=[(22, 22)]),
        "Repositories/QuestionRepository.cs": slicer.DiffFile(
            "Repositories/QuestionRepository.cs", text="c" * 100, changed_ranges=[(21, 21)]),
    }
    parent = slicer.build_slice(graph, "MoveQuestions")
    # Force a split with a tiny cap.
    parts = slicer.sub_split(graph, parent, big, cap=150)
    # Descends to the root's callees as new sub-roots.
    assert len(parts) >= 2
    for p in parts:
        assert slicer.slice_char_size(p, big) <= 150
    # Coverage preserved across the split.
    assert set().union(*(p.symbols for p in parts)) >= {
        "MoveQuestionsToCategoryAsync", "GetCategoryByIdAsync", "MoveQuestionsCategoryAsync"}


def test_slice_feature_respects_cap(graph):
    diff_files = {
        "Controllers/AdminQuestionController.cs": slicer.DiffFile(
            "Controllers/AdminQuestionController.cs", text="c" * 10, changed_ranges=[(15, 15)]),
        "Services/QuestionService.cs": slicer.DiffFile(
            "Services/QuestionService.cs", text="c" * 100, changed_ranges=[(22, 22)]),
        "Repositories/QuestionRepository.cs": slicer.DiffFile(
            "Repositories/QuestionRepository.cs", text="c" * 100, changed_ranges=[(21, 21)]),
    }
    slices = slicer.slice_feature(graph, diff_files, cap=150)
    for s in slices:
        assert slicer.slice_char_size(s, diff_files) <= 150
    covered = set().union(*(s.symbols for s in slices))
    changed = slicer.changed_symbols(graph, {p: d.changed_ranges for p, d in diff_files.items()})
    assert changed <= covered
