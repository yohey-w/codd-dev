"""GENERIC FIX 4 — first-line / encoding robustness (BOM) across all languages.

A source file written with a UTF-8 BOM (``\\ufeff``) carries that codepoint as the
very first character. Any first-line declaration parser anchored with ``^\\s*`` (a
C# ``namespace``, a Java ``package``, a Python ``from``/``import`` on line 1, …)
silently FAILS to match because the BOM is not ``\\s`` — so the declaration is
orphaned and the file becomes an island in the DAG (no namespace → no edge).

The fix is GENERIC (not C#-specific): strip a leading BOM (+ leading whitespace)
before ANY first-line declaration match, applied at the parsing boundary for every
language. These tests pin that a BOM on line 1 no longer drops edges, for C#
(namespace), Java (package), and Python (import) alike.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from codd.dag.builder import build_dag

BOM = "﻿"


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _imports_edges(dag) -> set[tuple[str, str]]:
    return {(edge.from_id, edge.to_id) for edge in dag.edges if edge.kind == "imports"}


def _seed(root: Path, source_dirs: list[str], project_type: str, marker_file: str) -> None:
    _write(root / marker_file, "x\n")
    _write(
        root / "codd" / "codd.yaml",
        yaml.safe_dump(
            {
                "scan": {"source_dirs": source_dirs, "doc_dirs": []},
                "required_artifacts": {"project_type": project_type},
            },
            sort_keys=False,
        ),
    )


def test_csharp_namespace_with_bom_on_first_line_still_resolves(tmp_path):
    """A ``.cs`` file whose ``namespace`` is on line 1 *behind a BOM* still links."""
    _seed(tmp_path, ["src"], "generic", "Demo.sln")
    # importer: using Demo.Lib;  (namespace on a later line, no BOM here)
    _write(
        tmp_path / "src" / "App.cs",
        "using Demo.Lib;\nnamespace Demo.App;\npublic class App {}\n",
    )
    # target: BOM right before ``namespace Demo.Lib;`` on the FIRST line.
    _write(
        tmp_path / "src" / "Lib.cs",
        f"{BOM}namespace Demo.Lib;\npublic class Lib {{}}\n",
    )

    dag = build_dag(tmp_path)
    edges = _imports_edges(dag)
    app = "src/App.cs"
    lib = "src/Lib.cs"
    assert (app, lib) in edges, (
        f"C# using → BOM-prefixed namespace edge missing (BOM orphaned the "
        f"namespace decl); edges={edges}"
    )


def test_java_package_with_bom_on_first_line_still_resolves(tmp_path):
    """A ``.java`` file whose ``package`` is on line 1 behind a BOM still links.

    The package line is what makes same-package siblings resolvable; if the BOM
    orphans it, ``A`` cannot find ``B`` in the same package.
    """
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
        f"{BOM}package com.a;\nimport com.a.B;\npublic class A {{ B b; }}\n",
    )
    _write(
        base / "com" / "a" / "B.java",
        f"{BOM}package com.a;\npublic class B {{}}\n",
    )

    dag = build_dag(tmp_path)
    edges = _imports_edges(dag)
    a = "src/main/java/com/a/A.java"
    b = "src/main/java/com/a/B.java"
    assert (a, b) in edges, (
        f"Java BOM-prefixed package/import edge missing; edges={edges}"
    )


def test_python_import_with_bom_on_first_line_still_resolves(tmp_path):
    """A ``.py`` file whose ``from`` import is on line 1 behind a BOM still links."""
    _seed(tmp_path, ["."], "generic", "pyproject.toml")
    _write(
        tmp_path / "main.py",
        f"{BOM}from helper import work\n\ndef main():\n    return work()\n",
    )
    _write(tmp_path / "helper.py", "def work():\n    return 1\n")

    dag = build_dag(tmp_path)
    edges = _imports_edges(dag)
    assert ("main.py", "helper.py") in edges, (
        f"Python BOM-prefixed first-line import edge missing; edges={edges}"
    )
