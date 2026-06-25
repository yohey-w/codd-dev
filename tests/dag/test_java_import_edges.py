"""Java import edges + reachability in the DAG (Increment 1, Piece 1).

Before this increment the DAG builder only extracted Java *nodes* (via the
polyglot suffix work); Java ``import`` statements produced NO ``imports`` edges,
so every Java impl file was unreachable from code-entry roots (the brownfield
reachability fix could not help a graph with zero structural edges). These tests
pin that Java FQN imports now resolve to in-tree files and form impl→impl edges.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from codd.dag.builder import build_dag


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _seed_java_project(root: Path) -> None:
    _write(root / "pom.xml", "<project />\n")
    _write(
        root / "codd" / "codd.yaml",
        yaml.safe_dump(
            {"scan": {"source_dirs": ["src/main/java"], "test_dirs": ["src/test/java"], "doc_dirs": []}},
            sort_keys=False,
        ),
    )


def _imports_edges(dag) -> set[tuple[str, str]]:
    return {(edge.from_id, edge.to_id) for edge in dag.edges if edge.kind == "imports"}


def test_java_cross_package_import_forms_impl_edge(tmp_path):
    _seed_java_project(tmp_path)
    base = tmp_path / "src" / "main" / "java"
    _write(
        base / "com" / "a" / "A.java",
        "package com.a;\n"
        "import com.a.B;\n"
        "import com.c.D;\n"
        "public class A {\n"
        "  B b;\n"
        "  D d;\n"
        "}\n",
    )
    _write(base / "com" / "a" / "B.java", "package com.a;\npublic class B {}\n")
    _write(base / "com" / "c" / "D.java", "package com.c;\npublic class D {}\n")

    dag = build_dag(tmp_path)
    edges = _imports_edges(dag)

    a = "src/main/java/com/a/A.java"
    b = "src/main/java/com/a/B.java"
    d = "src/main/java/com/c/D.java"
    assert (a, b) in edges, f"same-package import edge missing; edges={edges}"
    assert (a, d) in edges, f"cross-package import edge missing; edges={edges}"


def test_java_static_and_wildcard_imports_resolve(tmp_path):
    _seed_java_project(tmp_path)
    base = tmp_path / "src" / "main" / "java"
    _write(
        base / "com" / "a" / "A.java",
        "package com.a;\n"
        "import static com.util.Helpers.build;\n"
        "import com.pkg.*;\n"
        "public class A {}\n",
    )
    # static import → owning class file Helpers.java
    _write(base / "com" / "util" / "Helpers.java", "package com.util;\npublic class Helpers {}\n")
    # wildcard import → a file in the com.pkg package
    _write(base / "com" / "pkg" / "Widget.java", "package com.pkg;\npublic class Widget {}\n")

    dag = build_dag(tmp_path)
    edges = _imports_edges(dag)

    a = "src/main/java/com/a/A.java"
    helpers = "src/main/java/com/util/Helpers.java"
    widget = "src/main/java/com/pkg/Widget.java"
    assert (a, helpers) in edges, f"static import → owning class edge missing; edges={edges}"
    assert (a, widget) in edges, f"wildcard import → package file edge missing; edges={edges}"


def test_java_stdlib_imports_do_not_create_edges(tmp_path):
    _seed_java_project(tmp_path)
    base = tmp_path / "src" / "main" / "java"
    _write(
        base / "com" / "a" / "A.java",
        "package com.a;\nimport java.util.List;\npublic class A {}\n",
    )

    dag = build_dag(tmp_path)
    # java.util.List is not an in-tree node → no import edge, no crash.
    assert _imports_edges(dag) == set()


def test_java_imports_make_files_reachable(tmp_path):
    """The reachability payoff: an imported Java file is reachable via the edge.

    ``B`` has no other inbound edge; only ``A``'s import connects it. So if the
    import edge exists, ``B`` is in ``A``'s forward reachable set.
    """
    _seed_java_project(tmp_path)
    base = tmp_path / "src" / "main" / "java"
    _write(
        base / "com" / "a" / "A.java",
        "package com.a;\nimport com.a.B;\npublic class A { B b; }\n",
    )
    _write(base / "com" / "a" / "B.java", "package com.a;\npublic class B {}\n")

    dag = build_dag(tmp_path)

    a = "src/main/java/com/a/A.java"
    b = "src/main/java/com/a/B.java"
    reachable = dag.reachable_from(a) if hasattr(dag, "reachable_from") else None
    if reachable is not None:
        assert b in reachable
    else:
        # Fallback: assert the edge directly (reachability derives from it).
        assert (a, b) in _imports_edges(dag)
