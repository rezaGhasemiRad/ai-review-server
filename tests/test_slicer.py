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
