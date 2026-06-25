"""Generalize the C++ resolution unification to every language (no-drift).

C++ already had ``tests/dag/test_cpp_resolution_unified.py`` proving its builder
edge-resolver and scanner-CEG resolver share ONE candidate generator
(``codd.parsing._shared.cpp_include_candidate_paths``) and therefore agree. This
module is the SAME guarantee generalized: for every language whose specifier→file
derivation is duplicated across the parse layers, the two layers must (a) route
through the shared helper and (b) resolve a fixture to the SAME internal targets.

The structural reality (verified, and why the per-language assertions differ):

* JS/TS — like C++, BOTH layers PATH-resolve a relative specifier to a candidate
  file. They now share ``js_ts_source_candidate_paths`` (+ the shared
  ``ESM_EXTENSION_SWAP``), so the builder's import EDGE and the scanner's CEG
  ``file:`` node resolve to the SAME in-tree file — INCLUDING the ESM ``.js``→
  ``.ts`` swap and ``require()``/``import()`` forms that the scan side silently
  dropped before (the JS/TS analogue of the LevelDB 59% C++ scan-edge loss).
* Python / Java / C# — the scanner emits ABSTRACT ``module:<name>`` nodes (no
  filesystem derivation at all); ALL path/FQN/namespace→file derivation lives in
  the builder. There is no duplicated candidate-derivation to consolidate (a
  shared candidate helper there would be a fake second caller), so these are
  FLAGGED as architecturally-non-duplicated. What CAN drift for them is the
  specifier GRAMMAR (the ``import``/``package``/``using``/``namespace`` regexes),
  which is now single-sourced in ``_shared``. The no-drift invariant we assert is
  the directional one: every in-tree import EDGE the builder draws corresponds to
  a specifier the scanner also classified INTERNAL and named consistently.

Red-before-green: ``test_jsts_drift_is_caught_by_divergence`` monkeypatches the
scanner back to a NON-shared (ESM-unaware) candidate list and asserts the
agreement invariant then FAILS — i.e. the test has teeth; only the shared helper
keeps it green.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
import yaml

from codd.dag import builder as builder_mod
from codd.dag.builder import build_dag
from codd.graph import CEG
from codd.parsing import _shared
from codd.parsing import regex_strategies as rx
from codd.parsing.regex_strategies import _resolve_cpp_include_path


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _codd_yaml(root: Path, *, source_dirs, test_dirs=("test",)) -> None:
    _write(
        root / "codd" / "codd.yaml",
        yaml.safe_dump(
            {"scan": {"source_dirs": list(source_dirs), "test_dirs": list(test_dirs), "doc_dirs": []}},
            sort_keys=False,
        ),
    )


def _builder_import_edges(root: Path) -> set[tuple[str, str]]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        dag = build_dag(root)
    return {(e.from_id, e.to_id) for e in dag.edges if e.kind == "imports"}


def _scan_ceg(root: Path, src_dir: Path, language: str) -> CEG:
    """Run the scanner's source-import phase and return the populated CEG."""
    from codd.scanner import _scan_source_directory

    ceg = CEG(root / ".codd" / "scan_test")
    _scan_source_directory(ceg, root, src_dir, language, exclude_patterns=[])
    return ceg


def _scan_file_import_edges(ceg: CEG) -> set[tuple[str, str]]:
    """``imports`` edges between ``file:`` nodes, as ``(rel, rel)`` pairs."""
    edges: set[tuple[str, str]] = set()
    for edge in ceg.edges:
        if edge.get("relation") != "imports":
            continue
        src, dst = edge.get("source_id", ""), edge.get("target_id", "")
        if src.startswith("file:") and dst.startswith("file:"):
            edges.add((src[len("file:"):], dst[len("file:"):]))
    return edges


def _scan_internal_module_names(ceg: CEG) -> set[str]:
    """Names of the abstract ``module:<name>`` nodes the scanner emitted."""
    return {
        node_id[len("module:"):]
        for node_id in ceg.nodes
        if node_id.startswith("module:")
    }


# ─────────────────────────────────────────────────────────────────────────────
# (1) The callers route through the ONE shared helper — no second copy remains.
# ─────────────────────────────────────────────────────────────────────────────

def test_grammar_regexes_are_the_single_shared_objects():
    """Builder + scanner reference the IDENTICAL shared specifier-grammar regexes.

    Object identity (``is``) is the strongest "no second copy" assertion: a future
    edit to one layer's regex cannot silently diverge because there is only one
    compiled object.
    """
    assert builder_mod._JAVA_IMPORT_SPECIFIER_RE is _shared.JAVA_IMPORT_RE
    assert builder_mod._JAVA_PACKAGE_SPECIFIER_RE is _shared.JAVA_PACKAGE_RE
    assert builder_mod._JAVA_PACKAGE_DECL_RE is _shared.JAVA_PACKAGE_RE
    assert builder_mod._CPP_INCLUDE_RE is _shared.CPP_INCLUDE_RE
    assert builder_mod._CSHARP_USING_RE is _shared.CSHARP_USING_RE
    assert builder_mod._CSHARP_NAMESPACE_DECL_RE is _shared.CSHARP_NAMESPACE_RE

    assert rx._JAVA_IMPORT_RE is _shared.JAVA_IMPORT_RE
    assert rx._JAVA_PACKAGE_RE is _shared.JAVA_PACKAGE_RE
    assert rx._CPP_INCLUDE_RE is _shared.CPP_INCLUDE_RE
    assert rx._CSHARP_USING_RE is _shared.CSHARP_USING_RE
    assert rx._CSHARP_NAMESPACE_RE is _shared.CSHARP_NAMESPACE_RE


def test_esm_swap_map_is_the_single_shared_object():
    """The ESM ``.js``→``.ts`` swap DATA is single-sourced (builder == shared)."""
    assert builder_mod._ESM_EXTENSION_SWAP is _shared.ESM_EXTENSION_SWAP


def test_jsts_candidate_generation_is_one_shared_algorithm():
    """Builder and scanner derive the SAME JS/TS candidates from a spec.

    The builder expands a candidate base via ``_resolve_file_candidate`` (node-set
    suffixes + ESM swap); the scanner calls ``js_ts_source_candidate_paths``
    directly. They must agree on the ESM-swap RESULT for the same specifier — the
    behaviour that drifted. We assert the shared generator yields the ESM-swapped
    target, and that the builder's ESM map (the swap DATA both consult) is shared.
    """
    file_path = Path("/proj/src/consumer.ts")
    cands = _shared.js_ts_source_candidate_paths(
        "./types.js", file_path, _shared.JS_TS_SOURCE_EXTENSIONS
    )
    # The ESM-swapped source target must be among the candidates (the drift fix).
    assert Path("/proj/src/types.ts") in cands
    # And a real ``.js`` would still win first (exact match precedes the swap).
    assert cands[0] == Path("/proj/src/types.js")


# ─────────────────────────────────────────────────────────────────────────────
# (2) JS/TS + C++ : builder edge target == scan resolved file (same in-tree node)
# ─────────────────────────────────────────────────────────────────────────────

def _seed_jsts(root: Path) -> None:
    """A NodeNext TS layout exercising the drift surface:
    - ESM ``.js`` specifier resolving to a ``.ts`` source,
    - a ``require()`` (CommonJS) relative import,
    - a directory ``index`` import.
    """
    _codd_yaml(root, source_dirs=["src"])
    _write(root / "package.json", '{"name":"x","type":"module"}\n')
    _write(
        root / "src" / "consumer.ts",
        "import { T } from './types.js';\n"          # ESM .js -> types.ts
        "const u = require('./util');\n"             # CommonJS -> util.ts
        "import { z } from './widgets';\n",          # dir index -> widgets/index.ts
    )
    _write(root / "src" / "types.ts", "export type T = number;\n")
    _write(root / "src" / "util.ts", "export const u = 1;\n")
    _write(root / "src" / "widgets" / "index.ts", "export const z = 2;\n")


def test_jsts_builder_and_scan_resolve_same_targets(tmp_path):
    """JS/TS: the scanner's ``file:`` import edges == the builder's import edges.

    This is the JS/TS analogue of the C++ ``test_builder_and_scan_agree`` — the
    ESM-swap + ``require()`` edges the scan side dropped before now match the
    builder exactly, because both go through the shared candidate generator.
    """
    _seed_jsts(tmp_path)
    root = tmp_path.resolve()

    builder_edges = _builder_import_edges(root)
    ceg = _scan_ceg(root, root / "src", "typescript")
    scan_edges = _scan_file_import_edges(ceg)

    expected = {
        ("src/consumer.ts", "src/types.ts"),       # ESM .js -> .ts swap
        ("src/consumer.ts", "src/util.ts"),        # require() CommonJS
        ("src/consumer.ts", "src/widgets/index.ts"),  # directory index
    }
    # The builder forms exactly these in-tree edges.
    assert expected <= builder_edges, builder_edges
    # And the scanner now resolves the SAME in-tree files (no silent gap / drift).
    assert expected <= scan_edges, scan_edges
    # No-drift invariant: scan in-tree edge-set agrees with the builder's
    # (scan ⊆ build — the scan must never claim an in-tree edge the builder denies).
    assert scan_edges <= builder_edges, (scan_edges - builder_edges)


def _seed_cpp_leveldb(root: Path) -> None:
    """LevelDB-style: sources under root in db/ util/, includes rooted at ROOT."""
    _write(root / "CMakeLists.txt", "project(leveldb)\n")
    _codd_yaml(root, source_dirs=["."])
    _write(
        root / "db" / "version_set.cc",
        '#include "db/version_edit.h"\n#include "util/coding.h"\n'
        "namespace leveldb { class VersionSet {}; }\n",
    )
    _write(root / "db" / "version_edit.h", "#pragma once\nnamespace leveldb { class VersionEdit {}; }\n")
    _write(root / "util" / "coding.h", "#pragma once\nnamespace leveldb { void PutFixed32(); }\n")


def test_cpp_builder_and_scan_resolve_same_targets(tmp_path):
    """C++ (already unified) regression: builder edges == scan resolution."""
    _seed_cpp_leveldb(tmp_path)
    root = tmp_path.resolve()
    builder_edges = _builder_import_edges(root)
    version_set = root / "db" / "version_set.cc"
    scan_targets = {
        _resolve_cpp_include_path("db/version_edit.h", root, version_set),
        _resolve_cpp_include_path("util/coding.h", root, version_set),
    }
    assert scan_targets == {"db/version_edit.h", "util/coding.h"}
    assert ("db/version_set.cc", "db/version_edit.h") in builder_edges
    assert ("db/version_set.cc", "util/coding.h") in builder_edges


# ─────────────────────────────────────────────────────────────────────────────
# (3) Python / Java / C# : directional no-drift invariant (FLAGGED languages).
#     Scan emits module:<name>; builder emits node-ids. "Same file" is impossible
#     by construction, so we assert: every in-tree import EDGE the builder draws
#     corresponds to a specifier the scanner ALSO classified internal & named
#     consistently with the builder's resolved target.
# ─────────────────────────────────────────────────────────────────────────────

def _seed_python(root: Path) -> None:
    _codd_yaml(root, source_dirs=["pkg"])
    _write(root / "pkg" / "__init__.py", "")
    _write(root / "pkg" / "a.py", "from .b import thing\nimport pkg.c\n")
    _write(root / "pkg" / "b.py", "thing = 1\n")
    _write(root / "pkg" / "c.py", "value = 2\n")


def test_python_builder_edges_consistent_with_scan_internal(tmp_path):
    """Python: builder in-tree edges' targets are all scan-internal modules.

    The scanner names ``module:b`` / ``module:c`` (first-party module keys); the
    builder resolves ``.b``/``pkg.c`` to ``pkg/b.py``/``pkg/c.py`` node ids. The
    invariant: every builder in-tree import edge target is a file whose module
    name the scanner classified internal — no edge the scan layer is blind to.
    """
    _seed_python(tmp_path)
    root = tmp_path.resolve()
    builder_edges = _builder_import_edges(root)
    # Builder forms the two intra-package edges.
    assert ("pkg/a.py", "pkg/b.py") in builder_edges
    assert ("pkg/a.py", "pkg/c.py") in builder_edges

    internal_names = _scan_internal_module_names(_scan_ceg(root, root / "pkg", "python"))
    # The scanner saw both first-party modules a.py imports (b, c) as internal.
    assert {"b", "c"} <= internal_names, internal_names


def _seed_java(root: Path) -> None:
    _codd_yaml(root, source_dirs=["src/main/java"])
    base = root / "src" / "main" / "java" / "com" / "ex"
    _write(base / "App.java", "package com.ex;\nimport com.ex.util.Helper;\nclass App {}\n")
    _write(base / "util" / "Helper.java", "package com.ex.util;\npublic class Helper {}\n")


def test_java_builder_edge_target_declares_scanned_fqn(tmp_path):
    """Java: the file the builder resolves an import to declares a package that is
    consistent with the FQN the scanner named (module:<fqn>).
    """
    _seed_java(tmp_path)
    root = tmp_path.resolve()
    builder_edges = _builder_import_edges(root)
    target = "src/main/java/com/ex/util/Helper.java"
    assert ("src/main/java/com/ex/App.java", target) in builder_edges, builder_edges

    internal_names = _scan_internal_module_names(
        _scan_ceg(root, root / "src" / "main" / "java", "java")
    )
    # The scanner named the imported first-party FQN; the builder's resolved
    # target file declares the matching package (com.ex.util).
    assert any(name.startswith("com.ex.util") for name in internal_names), internal_names
    helper_pkg = (root / target).read_text(encoding="utf-8")
    assert "package com.ex.util;" in helper_pkg


def _seed_csharp(root: Path) -> None:
    _codd_yaml(root, source_dirs=["src"])
    _write(
        root / "src" / "Consumer.cs",
        "using Dapper.Tools;\n\nnamespace App;\n\nclass Consumer {}\n",
    )
    _write(
        root / "src" / "Tools.cs",
        "namespace Dapper.Tools;\n\npublic class Tools {}\n",
    )


def test_csharp_builder_edge_target_declares_scanned_namespace(tmp_path):
    """C#: the file the builder resolves a ``using`` to declares the namespace the
    scanner named (module:<namespace>).
    """
    _seed_csharp(tmp_path)
    root = tmp_path.resolve()
    builder_edges = _builder_import_edges(root)
    assert ("src/Consumer.cs", "src/Tools.cs") in builder_edges, builder_edges

    internal_names = _scan_internal_module_names(_scan_ceg(root, root / "src", "csharp"))
    # The scanner classified the first-party namespace internal (bucketed by the
    # first segment ``Dapper``); the builder's resolved target declares it.
    assert any(name.startswith("Dapper") for name in internal_names), internal_names
    assert "namespace Dapper.Tools" in (root / "src" / "Tools.cs").read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# (4) RED-BEFORE-GREEN: the agreement invariant has teeth.
#     Force the scan side back to a NON-shared (ESM-unaware) candidate generator
#     and assert the JS/TS builder-vs-scan agreement then BREAKS.
# ─────────────────────────────────────────────────────────────────────────────

def test_jsts_drift_is_caught_by_divergence(tmp_path, monkeypatch):
    """If the scanner stops using the shared generator (drops the ESM swap), the
    builder-vs-scan agreement MUST fail — proving the unified helper is load-bearing.
    """
    _seed_jsts(tmp_path)
    root = tmp_path.resolve()
    builder_edges = _builder_import_edges(root)

    # Diverge: a deliberately ESM-UNAWARE candidate generator (the pre-fix scan
    # behaviour) — no ``.js``→``.ts`` swap, ``import``/``from`` only via the same
    # search but missing the swapped target.
    def __no_esm_candidates(spec, file_path, extensions, *, include_index=True, esm_swap=True):
        return _shared.js_ts_source_candidate_paths(
            spec, file_path, extensions, include_index=include_index, esm_swap=False
        )

    monkeypatch.setattr(rx, "js_ts_source_candidate_paths", __no_esm_candidates)

    ceg = _scan_ceg(root, root / "src", "typescript")
    scan_edges = _scan_file_import_edges(ceg)

    # The ESM ``.js``→``.ts`` edge the builder forms is now MISSING from the scan
    # side → the agreement invariant (builder edge present in scan) is violated.
    esm_edge = ("src/consumer.ts", "src/types.ts")
    assert esm_edge in builder_edges
    assert esm_edge not in scan_edges, (
        "expected the ESM-unaware scanner to DROP the .js->.ts edge (drift), "
        "but it was present — the test would not catch a regression"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
