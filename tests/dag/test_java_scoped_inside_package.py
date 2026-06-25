"""GENERIC FIX 2c — Java source root scoped INSIDE a package tree (Guava BUG 1).

When CoDD is pointed at a directory that is itself part-way DOWN a package tree
(e.g. extracting Guava's ``guava/src/com/google/common`` as the project root, or
vendoring ``com/google/common`` directly), the on-disk layout under the root is
``base/Preconditions.java`` / ``collect/ImmutableList.java`` but the declared FQN
is ``com.google.common.base.Preconditions``. The Java FQN→path synthesis rooted at
the project root produced ``<root>/com/google/common/base/Preconditions.java`` — a
DOUBLE package prefix that exists nowhere → silently 0 import edges (the whole
graph inert).

The fix is GENERIC: the source root is inferred by stripping the leading
FQN/namespace segments that match the root's own trailing path. ``<root>`` ends in
``com/google/common``; an import ``com.google.common.base.Preconditions`` shares
that prefix, so the effective root is ``<root>`` with the shared prefix consumed →
``<root>/base/Preconditions.java`` (which exists). No per-OSS branch — it is the
shared ``_java_source_roots`` derivation reading the file-set.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from codd.dag.builder import build_dag


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _imports_edges(dag) -> set[tuple[str, str]]:
    return {(edge.from_id, edge.to_id) for edge in dag.edges if edge.kind == "imports"}


def _seed_scoped_inside(root: Path) -> None:
    """Project root == the inside of the ``com/google/common`` package tree."""
    _write(
        root / "codd" / "codd.yaml",
        yaml.safe_dump(
            {
                "scan": {"source_dirs": ["."], "doc_dirs": []},
                "required_artifacts": {"project_type": "generic"},
            },
            sort_keys=False,
        ),
    )
    _write(
        root / "collect" / "ImmutableList.java",
        "package com.google.common.collect;\n"
        "import com.google.common.base.Preconditions;\n"
        "public class ImmutableList { Preconditions p; }\n",
    )
    _write(
        root / "base" / "Preconditions.java",
        "package com.google.common.base;\npublic class Preconditions {}\n",
    )


def test_java_scoped_inside_package_resolves_cross_package_edge(tmp_path):
    _seed_scoped_inside(tmp_path)

    dag = build_dag(tmp_path)
    edges = _imports_edges(dag)

    importer = "collect/ImmutableList.java"
    target = "base/Preconditions.java"
    assert (importer, target) in edges, (
        f"scoped-inside Java import edge missing (double package-prefix bug); "
        f"edges={edges}"
    )


def test_java_scoped_inside_does_not_regress_conventional_layout(tmp_path):
    """A conventional ``src/main/java/com/...`` layout still resolves (no regression)."""
    _write(tmp_path / "pom.xml", "<project />\n")
    _write(
        tmp_path / "codd" / "codd.yaml",
        yaml.safe_dump(
            {"scan": {"source_dirs": ["src/main/java"], "doc_dirs": []}},
            sort_keys=False,
        ),
    )
    base = tmp_path / "src" / "main" / "java"
    _write(
        base / "com" / "a" / "A.java",
        "package com.a;\nimport com.c.D;\npublic class A { D d; }\n",
    )
    _write(base / "com" / "c" / "D.java", "package com.c;\npublic class D {}\n")

    dag = build_dag(tmp_path)
    edges = _imports_edges(dag)
    a = "src/main/java/com/a/A.java"
    d = "src/main/java/com/c/D.java"
    assert (a, d) in edges, f"conventional Java layout regressed; edges={edges}"
