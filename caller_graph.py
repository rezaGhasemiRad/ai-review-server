"""Per-repo symbol graph for the AI Review Server.

Mechanically extracts definitions and references from source files using
tree-sitter, then surfaces "potentially impacted callers" for any diff at
review time. The graph lives on the server, queried surgically — it is
NEVER sent to any AI in full. Reviewers only see the small caller-context
slice relevant to the diff being reviewed.

This module ships phase 1 + 6 of task #10: graph infrastructure for
C# / TypeScript / Python. Phase 2 (diff analysis + lookup + AST-aware
snippets) and phase 3 (review-pipeline integration) layer on top of this.

Cache layout (server-internal, OUTSIDE the repo clones to avoid any risk
of polluting working trees):

    /home/ubuntu/ai-review-server/graph-cache/<repo-name>/symbol-graph.json

Graph shape:

    {
        "version": 1,
        "built_at": "<iso-8601>",
        "repo_path": "<absolute>",
        "file_count": <int>,
        "files": {
            "<rel-path>": {
                "language": "csharp|typescript|tsx|python",
                "mtime": <float>,
                "defs": [{name, kind, line_start, line_end, signature, enclosing_class}],
                "refs": [{name, line, ref_kind, receiver, enclosing_function_name,
                          enclosing_function_start, enclosing_function_end}]
            }
        },
        "symbol_index": {
            "<symbol-name>": {
                "defs": [{file, line_start, line_end, kind, enclosing_class}],
                "refs": [{file, line, ref_kind, receiver, enclosing_function_name,
                          enclosing_function_start, enclosing_function_end}]
            }
        }
    }

CLI usage:

    python caller_graph.py build <repo_path>
    python caller_graph.py lookup <repo_path> <symbol_name>
    python caller_graph.py stats <repo_path>

Importable from server.py / ai_chat.py:

    from caller_graph import get_or_build_graph, find_refs
    g = get_or_build_graph("/home/ubuntu/ai-review-server/repos/Thabbit")
    refs = find_refs(g, "GetCategoryByIdAsync")
"""

from __future__ import annotations

import dataclasses
import fnmatch
import json
import logging
import os
import pathlib
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional

import tree_sitter
import tree_sitter_c_sharp as ts_cs
import tree_sitter_python as ts_py
import tree_sitter_typescript as ts_ts

log = logging.getLogger("caller_graph")

GRAPH_CACHE_ROOT = pathlib.Path(
    os.environ.get(
        "GRAPH_CACHE_ROOT",
        "/home/ubuntu/ai-review-server/graph-cache",
    )
)
GRAPH_VERSION = 1

# Reuse the same noise filter as the review pipeline — what's noise for
# review is noise for the graph. Avoids dragging generated code, lockfiles,
# vendored deps, etc. into the symbol index.
NOISE_PATTERNS = [
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
    "poetry.lock", "Gemfile.lock", "composer.lock", "Cargo.lock", "go.sum",
    "*.lock",
    "*.min.js", "*.min.css", "*.min.map", "*.map",
    "dist", "build", "out", "node_modules", ".next", ".nuxt", "coverage",
    "vendor", "__pycache__", ".venv", "venv", "target", "bin", "obj",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.ico", "*.pdf",
    "*.woff", "*.woff2", "*.ttf", "*.otf", "*.eot",
    "*.zip", "*.tar", "*.gz", "*.7z",
    "*.snap",
    "*.Designer.cs", "*ModelSnapshot.cs",
    "*.generated.*", "*.g.cs", "*.g.ts", "*.g.js",
    ".ai-collab",
]


# --------------------------------------------------------------------------- #
# Languages
# --------------------------------------------------------------------------- #

CSHARP_LANG = tree_sitter.Language(ts_cs.language())
TS_LANG = tree_sitter.Language(ts_ts.language_typescript())
TSX_LANG = tree_sitter.Language(ts_ts.language_tsx())
PYTHON_LANG = tree_sitter.Language(ts_py.language())


# Per-language tree-sitter query for definitions + references. Each query
# yields five "kinds" of captures:
#   @class_def     — full class node, with @class_name for the identifier
#   @method_def    — full method/function node, with @method_name + (optional)
#                    @enclosing_class on the method_def itself for lookup
#   @receiver      — the receiver expression of a member call
#   @member        — the member name of a member call
#   @call          — the full call/invocation node (for line + range)
#   @direct_name   — the function identifier for a direct call
#   @direct_call   — the full direct-call node
#
# Per-language node names differ, but the capture shape is uniform so the
# downstream walker doesn't need per-language branches.

CSHARP_QUERY_TEXT = """
(class_declaration name: (identifier) @class_name) @class_def
(interface_declaration name: (identifier) @class_name) @class_def
(struct_declaration name: (identifier) @class_name) @class_def
(record_declaration name: (identifier) @class_name) @class_def

(method_declaration name: (identifier) @method_name) @method_def
(constructor_declaration name: (identifier) @method_name) @method_def
(local_function_statement name: (identifier) @method_name) @method_def

(invocation_expression
    function: (member_access_expression
        expression: (_) @receiver
        name: (identifier) @member)) @call

(invocation_expression
    function: (identifier) @direct_name) @direct_call
"""

TYPESCRIPT_QUERY_TEXT = """
(class_declaration name: (type_identifier) @class_name) @class_def
(interface_declaration name: (type_identifier) @class_name) @class_def
(abstract_class_declaration name: (type_identifier) @class_name) @class_def

(method_definition name: (property_identifier) @method_name) @method_def
(function_declaration name: (identifier) @method_name) @method_def
(function_signature name: (identifier) @method_name) @method_def

(call_expression
    function: (member_expression
        object: (_) @receiver
        property: (property_identifier) @member)) @call

(call_expression
    function: (identifier) @direct_name) @direct_call
"""

PYTHON_QUERY_TEXT = """
(class_definition name: (identifier) @class_name) @class_def
(function_definition name: (identifier) @method_name) @method_def

(call
    function: (attribute
        object: (_) @receiver
        attribute: (identifier) @member)) @call

(call
    function: (identifier) @direct_name) @direct_call
"""


@dataclass(frozen=True)
class LanguageSpec:
    name: str
    language: tree_sitter.Language
    query_text: str
    extensions: tuple[str, ...]


LANGUAGES: dict[str, LanguageSpec] = {
    "csharp": LanguageSpec("csharp", CSHARP_LANG, CSHARP_QUERY_TEXT, (".cs",)),
    "typescript": LanguageSpec("typescript", TS_LANG, TYPESCRIPT_QUERY_TEXT, (".ts",)),
    "tsx": LanguageSpec("tsx", TSX_LANG, TYPESCRIPT_QUERY_TEXT, (".tsx",)),
    "python": LanguageSpec("python", PYTHON_LANG, PYTHON_QUERY_TEXT, (".py",)),
}

EXT_TO_LANG: dict[str, str] = {
    ext: name for name, spec in LANGUAGES.items() for ext in spec.extensions
}

# Lazily-built parser + query per language (Query objects are expensive to
# construct — reuse across files).
_PARSER_CACHE: dict[str, tree_sitter.Parser] = {}
_QUERY_CACHE: dict[str, tree_sitter.Query] = {}


def _get_parser(lang_name: str) -> tree_sitter.Parser:
    if lang_name not in _PARSER_CACHE:
        _PARSER_CACHE[lang_name] = tree_sitter.Parser(LANGUAGES[lang_name].language)
    return _PARSER_CACHE[lang_name]


def _get_query(lang_name: str) -> tree_sitter.Query:
    if lang_name not in _QUERY_CACHE:
        spec = LANGUAGES[lang_name]
        _QUERY_CACHE[lang_name] = tree_sitter.Query(spec.language, spec.query_text)
    return _QUERY_CACHE[lang_name]


# --------------------------------------------------------------------------- #
# Noise filtering
# --------------------------------------------------------------------------- #

def _is_noise(rel_path: str) -> bool:
    """Mirrors ai_chat._matches_glob_list / _is_noise — same patterns."""
    name = pathlib.PurePosixPath(rel_path).name
    parts = rel_path.replace("\\", "/").split("/")
    for pat in NOISE_PATTERNS:
        if "/" not in pat and "." not in pat and "*" not in pat:
            if pat in parts:
                return True
            continue
        if fnmatch.fnmatch(rel_path, pat) or fnmatch.fnmatch(name, pat):
            return True
    return False


# --------------------------------------------------------------------------- #
# Per-file extraction
# --------------------------------------------------------------------------- #

def _enclosing_function(node: tree_sitter.Node, lang_name: str) -> Optional[tree_sitter.Node]:
    """Walk up the AST to find the nearest function/method node containing
    `node`. Returns None if the call is at module level."""
    if lang_name == "csharp":
        kinds = ("method_declaration", "constructor_declaration",
                 "local_function_statement", "operator_declaration")
    elif lang_name in ("typescript", "tsx"):
        kinds = ("method_definition", "function_declaration",
                 "function_signature", "arrow_function", "function_expression")
    elif lang_name == "python":
        kinds = ("function_definition",)
    else:
        return None
    cur = node.parent
    while cur is not None:
        if cur.type in kinds:
            return cur
        cur = cur.parent
    return None


def _function_name(fn_node: tree_sitter.Node, lang_name: str) -> str:
    """Best-effort extraction of a function/method node's name."""
    # Most languages use `field: name` — query directly via child_by_field_name.
    name_node = fn_node.child_by_field_name("name")
    if name_node is not None:
        return name_node.text.decode("utf-8", "replace")
    # Arrow functions / function expressions don't have a name field; if the
    # parent is a variable_declarator, the declarator's identifier is the name.
    parent = fn_node.parent
    if parent is not None:
        for child in parent.children:
            if child.type in ("identifier", "property_identifier"):
                return child.text.decode("utf-8", "replace")
    return "<anonymous>"


def _enclosing_class(node: tree_sitter.Node, lang_name: str) -> Optional[str]:
    """Walk up the AST to find the nearest enclosing class/interface name."""
    if lang_name == "csharp":
        kinds = ("class_declaration", "interface_declaration",
                 "struct_declaration", "record_declaration")
    elif lang_name in ("typescript", "tsx"):
        kinds = ("class_declaration", "interface_declaration",
                 "abstract_class_declaration")
    elif lang_name == "python":
        kinds = ("class_definition",)
    else:
        return None
    cur = node.parent
    while cur is not None:
        if cur.type in kinds:
            name_node = cur.child_by_field_name("name")
            if name_node is not None:
                return name_node.text.decode("utf-8", "replace")
            return None
        cur = cur.parent
    return None


def _signature_line(source: bytes, line_zero_based: int, max_len: int = 200) -> str:
    """Extract the first line of a definition as its 'signature' for display."""
    lines = source.split(b"\n")
    if 0 <= line_zero_based < len(lines):
        s = lines[line_zero_based].decode("utf-8", "replace").strip()
        if len(s) > max_len:
            s = s[:max_len].rstrip() + " ..."
        return s
    return ""


def extract_file(source: bytes, lang_name: str) -> dict:
    """Run the per-language query against `source`, return {defs, refs}."""
    parser = _get_parser(lang_name)
    query = _get_query(lang_name)
    tree = parser.parse(source)
    cursor = tree_sitter.QueryCursor(query)
    matches = cursor.matches(tree.root_node)

    defs: list[dict] = []
    refs: list[dict] = []

    for _pattern_idx, caps in matches:
        # Definitions
        for class_def_node in caps.get("class_def", []):
            name_nodes = [
                n for n in caps.get("class_name", [])
                if class_def_node.start_byte <= n.start_byte and n.end_byte <= class_def_node.end_byte
            ]
            if not name_nodes:
                continue
            name = name_nodes[0].text.decode("utf-8", "replace")
            kind = class_def_node.type  # e.g. class_declaration / interface_declaration
            defs.append({
                "name": name,
                "kind": kind,
                "line_start": class_def_node.start_point[0] + 1,
                "line_end": class_def_node.end_point[0] + 1,
                "signature": _signature_line(source, class_def_node.start_point[0]),
                "enclosing_class": None,
            })

        for method_def_node in caps.get("method_def", []):
            name_nodes = [
                n for n in caps.get("method_name", [])
                if method_def_node.start_byte <= n.start_byte and n.end_byte <= method_def_node.end_byte
            ]
            if not name_nodes:
                continue
            name = name_nodes[0].text.decode("utf-8", "replace")
            defs.append({
                "name": name,
                "kind": method_def_node.type,
                "line_start": method_def_node.start_point[0] + 1,
                "line_end": method_def_node.end_point[0] + 1,
                "signature": _signature_line(source, method_def_node.start_point[0]),
                "enclosing_class": _enclosing_class(method_def_node, lang_name),
            })

        # References — member call (receiver.member)
        for call_node in caps.get("call", []):
            member_nodes = [
                m for m in caps.get("member", [])
                if call_node.start_byte <= m.start_byte and m.end_byte <= call_node.end_byte
            ]
            receiver_nodes = [
                r for r in caps.get("receiver", [])
                if call_node.start_byte <= r.start_byte and r.end_byte <= call_node.end_byte
            ]
            if not member_nodes:
                continue
            name = member_nodes[0].text.decode("utf-8", "replace")
            receiver = None
            if receiver_nodes:
                rec_text = receiver_nodes[0].text.decode("utf-8", "replace")
                # Truncate long receiver expressions (e.g. chained calls)
                receiver = rec_text.replace("\n", " ")
                if len(receiver) > 80:
                    receiver = receiver[:77] + "..."
            enclosing = _enclosing_function(call_node, lang_name)
            refs.append({
                "name": name,
                "line": call_node.start_point[0] + 1,
                "ref_kind": "method_call",
                "receiver": receiver,
                "enclosing_function_name": (
                    _function_name(enclosing, lang_name) if enclosing else None
                ),
                "enclosing_function_start": (
                    enclosing.start_point[0] + 1 if enclosing else None
                ),
                "enclosing_function_end": (
                    enclosing.end_point[0] + 1 if enclosing else None
                ),
            })

        # References — direct call (no receiver: foo())
        for direct_call in caps.get("direct_call", []):
            name_nodes = [
                n for n in caps.get("direct_name", [])
                if direct_call.start_byte <= n.start_byte and n.end_byte <= direct_call.end_byte
            ]
            if not name_nodes:
                continue
            name = name_nodes[0].text.decode("utf-8", "replace")
            enclosing = _enclosing_function(direct_call, lang_name)
            refs.append({
                "name": name,
                "line": direct_call.start_point[0] + 1,
                "ref_kind": "direct_call",
                "receiver": None,
                "enclosing_function_name": (
                    _function_name(enclosing, lang_name) if enclosing else None
                ),
                "enclosing_function_start": (
                    enclosing.start_point[0] + 1 if enclosing else None
                ),
                "enclosing_function_end": (
                    enclosing.end_point[0] + 1 if enclosing else None
                ),
            })

    return {"defs": defs, "refs": refs}


def _detect_language(file_path: str) -> Optional[str]:
    ext = pathlib.PurePosixPath(file_path).suffix.lower()
    return EXT_TO_LANG.get(ext)


# --------------------------------------------------------------------------- #
# Git helpers — read files from a ref, not the working tree.
# --------------------------------------------------------------------------- #
# The server clones some repos bare-ish (no working tree). All file access
# goes through `git show <ref>:<path>` so we work uniformly across:
#   - full clones (working tree present, may be stale relative to refs)
#   - bare-ish clones (no working tree at all)
#   - sub-repos nested inside a parent (git won't traverse into them)

def _git(repo_path: pathlib.Path, *args: str, capture_bytes: bool = False):
    """Run git in `repo_path`. Returns CompletedProcess.
    Stdout is bytes when capture_bytes=True (for binary-safe `git show`),
    otherwise text decoded as UTF-8 with replacement for any bad sequences.
    """
    cmd = ["git", "-C", str(repo_path), *args]
    kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if not capture_bytes:
        kwargs.update(text=True, encoding="utf-8", errors="replace")
    return subprocess.run(cmd, **kwargs)


def _default_ref(repo_path: pathlib.Path) -> str:
    """Pick a stable representative ref for graph-building.
    Order: origin/main → origin/master → origin/develop → HEAD.
    Stable base branches give a graph that reflects "what's in production-ish"
    — feature branches' callers will show up in the diff itself."""
    for candidate in ("origin/main", "origin/master", "origin/develop", "HEAD"):
        r = _git(repo_path, "rev-parse", "--verify", candidate)
        if r.returncode == 0 and r.stdout.strip():
            return candidate
    # Last resort: caller will see the build fail gracefully when listing.
    return "HEAD"


def _git_ls_files(repo_path: pathlib.Path, ref: str) -> list[str]:
    """All file paths in the tree at `ref`. Relative POSIX paths."""
    r = _git(repo_path, "ls-tree", "-r", "--name-only", ref)
    if r.returncode != 0:
        log.warning("git ls-tree failed in %s @ %s: %s",
                    repo_path.name, ref, r.stderr.strip()[:200])
        return []
    return [line for line in r.stdout.splitlines() if line]


def _git_show_bytes(repo_path: pathlib.Path, ref: str, path: str) -> Optional[bytes]:
    """Read a file's bytes at a specific ref. Returns None if it doesn't
    exist at that ref (deleted, or never existed)."""
    r = _git(repo_path, "show", f"{ref}:{path}", capture_bytes=True)
    if r.returncode != 0:
        return None
    return r.stdout


# --------------------------------------------------------------------------- #
# Repo walk + graph build
# --------------------------------------------------------------------------- #

def _iter_source_files(repo_path: pathlib.Path, ref: str) -> Iterator[tuple[str, str]]:
    """Yield (rel_path, lang_name) for source files at `ref`, skipping noise.
    Paths come from `git ls-tree`, so nested sub-repos are naturally
    excluded (git won't recurse into them)."""
    for rel in _git_ls_files(repo_path, ref):
        ext = pathlib.PurePosixPath(rel).suffix.lower()
        lang = EXT_TO_LANG.get(ext)
        if lang is None:
            continue
        if _is_noise(rel):
            continue
        yield rel, lang


def build_graph(repo_path: str | pathlib.Path, ref: Optional[str] = None) -> dict:
    """Build the symbol graph for `repo_path` at `ref` (default: auto-detect
    a stable base branch). Returns the in-memory graph dict; does NOT write
    to disk — call save_graph(...) afterwards."""
    repo = pathlib.Path(repo_path).resolve()
    if ref is None:
        ref = _default_ref(repo)
    started = time.time()

    files: dict[str, dict] = {}
    skipped_parse_errors = 0
    skipped_missing_bytes = 0

    for rel, lang_name in _iter_source_files(repo, ref):
        source = _git_show_bytes(repo, ref, rel)
        if source is None:
            skipped_missing_bytes += 1
            continue
        try:
            extracted = extract_file(source, lang_name)
        except Exception as e:
            log.warning("skipping %s: %s", rel, e)
            skipped_parse_errors += 1
            continue
        files[rel] = {
            "language": lang_name,
            # mtime not meaningful for git-ref reads; store the ref instead
            # so incremental_update can decide what to re-fetch.
            "mtime": 0.0,
            "defs": extracted["defs"],
            "refs": extracted["refs"],
        }

    graph = {
        "version": GRAPH_VERSION,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo_path": str(repo),
        "ref": ref,
        "file_count": len(files),
        "skipped_parse_errors": skipped_parse_errors,
        "skipped_missing_bytes": skipped_missing_bytes,
        "build_seconds": round(time.time() - started, 2),
        "files": files,
        "symbol_index": _rebuild_symbol_index(files),
    }
    return graph


def _rebuild_symbol_index(files: dict) -> dict:
    """Derive the secondary symbol → [{file, ...}] index from per-file data.
    This is what enables O(1) lookup by symbol name at review time."""
    index: dict[str, dict] = {}
    for rel, entry in files.items():
        for d in entry["defs"]:
            sym = index.setdefault(d["name"], {"defs": [], "refs": []})
            sym["defs"].append({
                "file": rel,
                "line_start": d["line_start"],
                "line_end": d["line_end"],
                "kind": d["kind"],
                "enclosing_class": d.get("enclosing_class"),
            })
        for r in entry["refs"]:
            sym = index.setdefault(r["name"], {"defs": [], "refs": []})
            sym["refs"].append({
                "file": rel,
                "line": r["line"],
                "ref_kind": r["ref_kind"],
                "receiver": r.get("receiver"),
                "enclosing_function_name": r.get("enclosing_function_name"),
                "enclosing_function_start": r.get("enclosing_function_start"),
                "enclosing_function_end": r.get("enclosing_function_end"),
            })
    return index


def incremental_update(
    repo_path: str | pathlib.Path,
    changed_files: list[str],
    ref: Optional[str] = None,
) -> dict:
    """Re-parse only `changed_files` (relative POSIX paths) at `ref` and
    update the existing graph in place. Saves and returns the new graph.

    If the existing graph is missing or malformed, falls back to a full
    build at `ref`. Files that no longer exist at `ref` (deleted) are
    removed from the graph; new files are added.
    """
    repo = pathlib.Path(repo_path).resolve()
    graph = load_graph(repo)
    if ref is None:
        ref = (graph or {}).get("ref") or _default_ref(repo)
    if graph is None:
        log.info("no existing graph; building from scratch at %s", ref)
        graph = build_graph(repo, ref)
        save_graph(repo, graph)
        return graph

    files: dict = graph.get("files") or {}
    for rel in changed_files:
        lang = _detect_language(rel)
        if lang is None or _is_noise(rel):
            files.pop(rel, None)
            continue
        source = _git_show_bytes(repo, ref, rel)
        if source is None:
            # Deleted at this ref
            files.pop(rel, None)
            continue
        try:
            extracted = extract_file(source, lang)
        except Exception as e:
            log.warning("skipping incremental update of %s: %s", rel, e)
            continue
        files[rel] = {
            "language": lang,
            "mtime": 0.0,
            "defs": extracted["defs"],
            "refs": extracted["refs"],
        }

    graph["files"] = files
    graph["file_count"] = len(files)
    graph["ref"] = ref
    graph["built_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    graph["symbol_index"] = _rebuild_symbol_index(files)
    save_graph(repo, graph)
    return graph


# --------------------------------------------------------------------------- #
# Cache I/O
# --------------------------------------------------------------------------- #

def _cache_path(repo_path: pathlib.Path) -> pathlib.Path:
    """One cache file per repo, keyed by the repo directory's basename.
    Multiple clones with the same name would collide — but the server
    only ever clones each repo to one fixed path under /repos/, so that's
    not a real risk."""
    return GRAPH_CACHE_ROOT / repo_path.name / "symbol-graph.json"


def load_graph(repo_path: str | pathlib.Path) -> Optional[dict]:
    """Load the cached graph for a repo. Returns None if missing or
    version-incompatible (signals "rebuild from scratch")."""
    repo = pathlib.Path(repo_path).resolve()
    path = _cache_path(repo)
    if not path.exists():
        return None
    try:
        graph = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("graph cache for %s is corrupt: %s", repo.name, e)
        return None
    if graph.get("version") != GRAPH_VERSION:
        log.info("graph cache for %s is version %s, need %s — discarding",
                 repo.name, graph.get("version"), GRAPH_VERSION)
        return None
    return graph


def save_graph(repo_path: str | pathlib.Path, graph: dict) -> pathlib.Path:
    """Persist the graph atomically (write to .tmp then rename)."""
    repo = pathlib.Path(repo_path).resolve()
    path = _cache_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(graph, indent=None, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    return path


def get_or_build_graph(repo_path: str | pathlib.Path) -> dict:
    """Lazy load: return cached graph if present, else build + persist."""
    repo = pathlib.Path(repo_path).resolve()
    graph = load_graph(repo)
    if graph is not None:
        return graph
    log.info("building graph for %s (first use)", repo.name)
    graph = build_graph(repo)
    save_graph(repo, graph)
    return graph


# --------------------------------------------------------------------------- #
# Lookup helpers (used by phase 2 + at the command line)
# --------------------------------------------------------------------------- #

def find_refs(graph: dict, symbol_name: str) -> list[dict]:
    """All known references to a symbol, across the whole repo."""
    return ((graph.get("symbol_index") or {}).get(symbol_name) or {}).get("refs", [])


def find_defs(graph: dict, symbol_name: str) -> list[dict]:
    """All known definitions of a symbol (a method may be defined in an
    interface plus an implementation, etc.)."""
    return ((graph.get("symbol_index") or {}).get(symbol_name) or {}).get("defs", [])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _format_stats(graph: dict) -> str:
    files = graph.get("files") or {}
    idx = graph.get("symbol_index") or {}
    by_lang: dict[str, int] = {}
    total_defs = 0
    total_refs = 0
    for f in files.values():
        by_lang[f["language"]] = by_lang.get(f["language"], 0) + 1
        total_defs += len(f["defs"])
        total_refs += len(f["refs"])
    lines = [
        f"repo:                 {graph.get('repo_path')}",
        f"ref:                  {graph.get('ref')}",
        f"built_at:             {graph.get('built_at')}",
        f"build_seconds:        {graph.get('build_seconds')}",
        f"files indexed:        {graph.get('file_count')}",
        f"  by language:        {', '.join(f'{k}={v}' for k, v in sorted(by_lang.items()))}",
        f"parse-error skips:    {graph.get('skipped_parse_errors')}",
        f"missing-bytes skips:  {graph.get('skipped_missing_bytes', 0)}",
        f"total defs:           {total_defs:,}",
        f"total refs:           {total_refs:,}",
        f"symbol_index size:    {len(idx):,} unique names",
    ]
    return "\n".join(lines)


def _cli_build(repo_path: str, ref: Optional[str] = None) -> int:
    repo = pathlib.Path(repo_path).resolve()
    if not (repo / ".git").exists():
        print(f"error: {repo} is not a git repo", file=sys.stderr)
        return 1
    print(f"Building graph for {repo} (ref={ref or 'auto'}) ...")
    graph = build_graph(repo, ref)
    save_graph(repo, graph)
    print()
    print(_format_stats(graph))
    print()
    print(f"cache file: {_cache_path(repo)}")
    return 0


def _cli_lookup(repo_path: str, symbol_name: str) -> int:
    graph = load_graph(repo_path)
    if graph is None:
        print(f"no graph for {repo_path} — run `build` first", file=sys.stderr)
        return 1
    defs = find_defs(graph, symbol_name)
    refs = find_refs(graph, symbol_name)
    print(f"=== {symbol_name} ===")
    print(f"  {len(defs)} def(s), {len(refs)} ref(s)")
    print()
    if defs:
        print("definitions:")
        for d in defs:
            cls = f" (in {d['enclosing_class']})" if d.get("enclosing_class") else ""
            print(f"  {d['file']}:{d['line_start']}-{d['line_end']} [{d['kind']}]{cls}")
        print()
    if refs:
        print("references:")
        for r in refs[:50]:
            rec = f"{r['receiver']}." if r.get("receiver") else ""
            fn = f" in {r['enclosing_function_name']}" if r.get("enclosing_function_name") else ""
            print(f"  {r['file']}:{r['line']}  {rec}{symbol_name}(){fn}")
        if len(refs) > 50:
            print(f"  ... +{len(refs) - 50} more")
    return 0


def _cli_stats(repo_path: str) -> int:
    graph = load_graph(repo_path)
    if graph is None:
        print(f"no graph for {repo_path} — run `build` first", file=sys.stderr)
        return 1
    print(_format_stats(graph))
    return 0


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "build":
        # Optional 4th arg: ref to build from (e.g. "origin/develop")
        ref = argv[3] if len(argv) >= 4 else None
        return _cli_build(argv[2], ref)
    if cmd == "lookup":
        if len(argv) < 4:
            print("usage: caller_graph.py lookup <repo_path> <symbol_name>", file=sys.stderr)
            return 2
        return _cli_lookup(argv[2], argv[3])
    if cmd == "stats":
        return _cli_stats(argv[2])
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
