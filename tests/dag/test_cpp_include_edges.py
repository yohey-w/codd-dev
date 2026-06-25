"""C++ ``#include`` edges + reachability in the DAG (Increment 1, Pieces 1-2).

Before this increment the DAG builder extracted C++ *nodes* but every C++
``#include`` produced NO ``imports`` edge, so each translation unit was an island
(unreachable from code-entry roots — the brownfield reachability fix can do
nothing with a zero-edge graph). These tests pin that C++ includes now resolve
to in-tree files and form edges.

The key divergence from the Java increment: C++ resolution is PATH-based, not
FQN-based. A quote-form ``#include "lib/b.h"`` already carries its extension and
relative path, so it resolves against the including file's directory FIRST, then
each include root (``include``/``src``/``inc`` + harvested header dirs). An
angle-form ``#include <vector>`` is system/STL and forms NO edge.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from codd.dag.builder import build_dag


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _seed_cpp_project(root: Path, source_dirs: list[str] | None = None) -> None:
    # ``CMakeLists.txt`` makes ``_detect_project_type`` return ``cpp_embedded``
    # (so the cpp suffix defaults apply); the scan section scopes node globs.
    _write(root / "CMakeLists.txt", "project(demo)\n")
    _write(
        root / "codd" / "codd.yaml",
        yaml.safe_dump(
            {
                "scan": {
                    "source_dirs": source_dirs or ["src", "include"],
                    "test_dirs": ["test"],
                    "doc_dirs": [],
                }
            },
            sort_keys=False,
        ),
    )


def _imports_edges(dag) -> set[tuple[str, str]]:
    return {(edge.from_id, edge.to_id) for edge in dag.edges if edge.kind == "imports"}


def test_cpp_quote_include_resolves_relative_to_file(tmp_path):
    """``#include "lib/b.h"`` resolves against the including file's directory."""
    _seed_cpp_project(tmp_path)
    _write(
        tmp_path / "src" / "a.cc",
        '#include "lib/b.h"\n'
        "#include <vector>\n"
        "int a() { return 0; }\n",
    )
    _write(tmp_path / "src" / "lib" / "b.h", "#pragma once\nint b();\n")

    dag = build_dag(tmp_path)
    edges = _imports_edges(dag)

    a = "src/a.cc"
    b = "src/lib/b.h"
    assert (a, b) in edges, f"quote-form #include edge missing; edges={edges}"
    # ``<vector>`` is system/STL → no edge to any in-tree node.
    assert not any(
        target.endswith("vector") for _src, target in edges
    ), f"system #include <vector> wrongly produced an edge; edges={edges}"


def test_cpp_angle_include_of_stdlib_forms_no_edge(tmp_path):
    """Angle-form includes are system/STL and never form in-tree edges."""
    _seed_cpp_project(tmp_path)
    _write(
        tmp_path / "src" / "only.cc",
        "#include <string>\n#include <memory>\nint only() { return 0; }\n",
    )

    dag = build_dag(tmp_path)
    edges = _imports_edges(dag)
    assert edges == set(), f"angle-form includes must form no edges; edges={edges}"


def test_cpp_include_via_include_root(tmp_path):
    """A header under ``include/`` resolves via the include-root probe.

    ``src/use.cc`` does ``#include "demo/core.h"`` and the header lives at
    ``include/demo/core.h`` — not relative to the .cc file, so it must be found
    by probing the ``include`` root.
    """
    _seed_cpp_project(tmp_path)
    _write(
        tmp_path / "src" / "use.cc",
        '#include "demo/core.h"\nint use() { return 0; }\n',
    )
    _write(tmp_path / "include" / "demo" / "core.h", "#pragma once\nint core();\n")

    dag = build_dag(tmp_path)
    edges = _imports_edges(dag)

    use = "src/use.cc"
    core = "include/demo/core.h"
    assert (use, core) in edges, f"include-root resolved edge missing; edges={edges}"


def test_cpp_header_to_header_include_edge(tmp_path):
    """A header including another header forms a header→header edge."""
    _seed_cpp_project(tmp_path)
    _write(
        tmp_path / "include" / "demo" / "top.h",
        '#pragma once\n#include "demo/base.h"\n',
    )
    _write(tmp_path / "include" / "demo" / "base.h", "#pragma once\nint base();\n")

    dag = build_dag(tmp_path)
    edges = _imports_edges(dag)

    top = "include/demo/top.h"
    base = "include/demo/base.h"
    assert (top, base) in edges, f"header→header #include edge missing; edges={edges}"


def _tested_by_edges(dag) -> set[tuple[str, str]]:
    return {(edge.from_id, edge.to_id) for edge in dag.edges if edge.kind == "tested_by"}


def test_cpp_test_file_includes_header_under_test(tmp_path):
    """A test ``.cc`` including a project header links impl↔test via its include.

    Test files form ``tested_by`` edges (impl → test direction), the same as
    Python/Java; the C++ resolver is what lets the test's ``#include`` resolve to
    the implementation header so the relationship is discovered.
    """
    _seed_cpp_project(tmp_path)
    _write(tmp_path / "include" / "demo" / "core.h", "#pragma once\nint core();\n")
    _write(
        tmp_path / "test" / "core-test.cc",
        '#include "demo/core.h"\nint main() { return core(); }\n',
    )

    dag = build_dag(tmp_path)
    tested_by = _tested_by_edges(dag)

    test = "test/core-test.cc"
    core = "include/demo/core.h"
    assert (core, test) in tested_by, (
        f"impl→test (tested_by) edge from resolved #include missing; "
        f"tested_by={tested_by}"
    )
