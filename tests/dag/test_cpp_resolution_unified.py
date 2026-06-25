"""GENERIC FIX 3 — unify C++ include resolution (kill the scan/builder drift).

The DAG builder's C++ ``#include`` resolver and the scanner-CEG resolver drifted:
the builder harvested header-node parent dirs (so it resolved includes rooted at
an UNCONVENTIONAL include root such as LevelDB's ``db/`` / ``util/`` directly under
the project root), while the scanner-CEG resolver (``_resolve_cpp_include_path``)
only tried relative-to-file + the conventional ``include``/``src``/``inc`` roots.
On LevelDB this lost ~59% of scan edges. Both now share ONE resolution path so a
root-scoped include (``#include "db/version_edit.h"`` from ``db/version_set.cc``,
include root == project root) resolves IDENTICALLY in both.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from codd.dag.builder import build_dag
from codd.parsing.regex_strategies import _resolve_cpp_include_path


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _seed_leveldb_like(root: Path) -> None:
    """A LevelDB-style layout: sources directly under root in db/ util/ table/,
    includes rooted at the PROJECT ROOT (``#include "db/foo.h"``)."""
    _write(root / "CMakeLists.txt", "project(leveldb)\n")
    _write(
        root / "codd" / "codd.yaml",
        yaml.safe_dump(
            {"scan": {"source_dirs": ["."], "test_dirs": ["test"], "doc_dirs": []}},
            sort_keys=False,
        ),
    )
    _write(
        root / "db" / "version_set.cc",
        '#include "db/version_edit.h"\n#include "util/coding.h"\n'
        "namespace leveldb { class VersionSet {}; }\n",
    )
    _write(root / "db" / "version_edit.h", "#pragma once\nnamespace leveldb { class VersionEdit {}; }\n")
    _write(root / "util" / "coding.h", "#pragma once\nnamespace leveldb { void PutFixed32(); }\n")


def _imports_edges(dag) -> set[tuple[str, str]]:
    return {(edge.from_id, edge.to_id) for edge in dag.edges if edge.kind == "imports"}


def test_scan_resolver_resolves_root_scoped_include(tmp_path):
    """The scanner-CEG resolver must resolve a project-root-rooted include.

    ``db/version_set.cc`` includes ``"db/version_edit.h"`` — not relative to the
    file, and the include ROOT is the PROJECT ROOT (not include/src/inc). Before
    the unification this returned ``None`` (lost edge); now it resolves.
    """
    _seed_leveldb_like(tmp_path)
    root = tmp_path.resolve()
    version_set = root / "db" / "version_set.cc"

    assert (
        _resolve_cpp_include_path("db/version_edit.h", root, version_set)
        == "db/version_edit.h"
    ), "scan resolver dropped a project-root-rooted include (drift from builder)"
    assert (
        _resolve_cpp_include_path("util/coding.h", root, version_set)
        == "util/coding.h"
    ), "scan resolver dropped a cross-dir root-rooted include (drift from builder)"


def test_builder_and_scan_agree_on_root_scoped_includes(tmp_path):
    """Builder import EDGES and scanner resolution agree on the same layout."""
    _seed_leveldb_like(tmp_path)
    root = tmp_path.resolve()

    dag = build_dag(tmp_path)
    edges = _imports_edges(dag)
    assert ("db/version_set.cc", "db/version_edit.h") in edges, edges
    assert ("db/version_set.cc", "util/coding.h") in edges, edges

    # Scan resolution of the SAME includes yields the SAME in-tree targets.
    version_set = root / "db" / "version_set.cc"
    scan_targets = {
        _resolve_cpp_include_path("db/version_edit.h", root, version_set),
        _resolve_cpp_include_path("util/coding.h", root, version_set),
    }
    assert scan_targets == {"db/version_edit.h", "util/coding.h"}


def test_out_of_tree_include_yields_no_resolution(tmp_path):
    """An include escaping the project tree resolves to None (no false node)."""
    _seed_leveldb_like(tmp_path)
    root = tmp_path.resolve()
    version_set = root / "db" / "version_set.cc"
    # ``../../etc/passwd`` style escape — must not resolve to anything in-tree.
    assert _resolve_cpp_include_path("../../outside.h", root, version_set) is None
