"""C# ``using`` edges + reachability in the DAG (Increment 1, Piece 1).

Before this increment the DAG builder extracted C# *nodes* but every C# ``using``
produced NO ``imports`` edge, so each compilation unit was an island
(unreachable from code-entry roots — the brownfield reachability fix can do
nothing with a zero-edge graph). These tests pin that C# usings now resolve to
in-tree files and form edges.

The key divergence from Java (FQN→path synthesis) and C++ (PATH-based include):
C# resolution is NAMESPACE-based via a reverse-index. A namespace is NOT
directory-tied — ``namespace Dapper;`` is declared by many files across different
directories — so a first pass builds a namespace→declaring-files index, and a
``using Dapper.Lib`` resolves to the files declaring ``namespace Dapper.Lib``
(EXACT match). A ``using System.*`` / ``Microsoft.*`` is framework and forms NO
edge.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from codd.dag.builder import build_dag


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _seed_csharp_project(root: Path, source_dirs: list[str] | None = None) -> None:
    # A ``*.csproj`` makes ``_detect_project_type`` return ``csharp`` (so the .cs
    # suffix defaults apply); the scan section scopes node globs.
    _write(root / "App.csproj", "<Project></Project>\n")
    _write(
        root / "codd" / "codd.yaml",
        yaml.safe_dump(
            {
                "scan": {
                    "source_dirs": source_dirs or ["app", "lib"],
                    "test_dirs": ["test"],
                    "doc_dirs": [],
                }
            },
            sort_keys=False,
        ),
    )


def _imports_edges(dag) -> set[tuple[str, str]]:
    return {(edge.from_id, edge.to_id) for edge in dag.edges if edge.kind == "imports"}


def test_csharp_using_resolves_via_namespace_index(tmp_path):
    """``using Lib.B`` resolves to the file declaring ``namespace Lib.B``.

    The two files live in DIFFERENT directories (app/ and lib/) — the resolution
    is by NAMESPACE, not by path, so the directory split is irrelevant. This is
    the canonical red-before-green case (0 edges before the namespace index).
    """
    _seed_csharp_project(tmp_path)
    _write(
        tmp_path / "app" / "A.cs",
        "namespace App;\n"
        "using Lib.B;\n"
        "using System.Text;\n"
        "public class A { }\n",
    )
    _write(
        tmp_path / "lib" / "B.cs",
        "namespace Lib.B;\npublic class B { }\n",
    )

    dag = build_dag(tmp_path)
    edges = _imports_edges(dag)

    a = "app/A.cs"
    b = "lib/B.cs"
    assert (a, b) in edges, f"namespace-index using edge missing; edges={edges}"
    # ``using System.Text;`` is framework/BCL → no in-tree edge.
    assert not any(
        target.lower().endswith("text") or "system" in target.lower()
        for _src, target in edges
    ), f"framework using System.Text wrongly produced an edge; edges={edges}"


def test_csharp_using_of_framework_forms_no_edge(tmp_path):
    """``using System.*`` / ``Microsoft.*`` are framework and never form edges."""
    _seed_csharp_project(tmp_path)
    _write(
        tmp_path / "app" / "Only.cs",
        "namespace App;\n"
        "using System;\n"
        "using System.Collections.Generic;\n"
        "using Microsoft.Extensions.Logging;\n"
        "public class Only { }\n",
    )

    dag = build_dag(tmp_path)
    edges = _imports_edges(dag)
    assert edges == set(), f"framework usings must form no edges; edges={edges}"


def test_csharp_block_scoped_namespace_declaration(tmp_path):
    """Block-form ``namespace X { }`` is indexed the same as file-scoped ``X;``.

    Dapper uses BOTH forms; the reverse-index must recognize the block form so a
    ``using`` of a block-declared namespace still resolves.
    """
    _seed_csharp_project(tmp_path)
    _write(
        tmp_path / "app" / "Consumer.cs",
        "namespace App\n{\n    using Lib.Core;\n    public class Consumer { }\n}\n",
    )
    _write(
        tmp_path / "lib" / "Core.cs",
        "namespace Lib.Core\n{\n    public class Core { }\n}\n",
    )

    dag = build_dag(tmp_path)
    edges = _imports_edges(dag)

    consumer = "app/Consumer.cs"
    core = "lib/Core.cs"
    assert (consumer, core) in edges, (
        f"block-scoped namespace using edge missing; edges={edges}"
    )


def test_csharp_using_static_resolves_to_owning_namespace(tmp_path):
    """``using static Lib.Calc`` (a TYPE) resolves to the type's owning namespace.

    A ``using static`` names a type member-holder, not a namespace; its declaring
    file lives in the parent namespace (``Lib``), so the resolver falls back to
    the parent namespace when the full path is not itself a namespace.
    """
    _seed_csharp_project(tmp_path)
    _write(
        tmp_path / "app" / "User.cs",
        "namespace App;\nusing static Lib.Calc;\npublic class User { }\n",
    )
    _write(
        tmp_path / "lib" / "Calc.cs",
        "namespace Lib;\npublic static class Calc { public static int Add() => 0; }\n",
    )

    dag = build_dag(tmp_path)
    edges = _imports_edges(dag)

    user = "app/User.cs"
    calc = "lib/Calc.cs"
    assert (user, calc) in edges, (
        f"using-static → owning-namespace edge missing; edges={edges}"
    )


def test_csharp_using_does_not_pull_child_namespaces(tmp_path):
    """``using Lib`` resolves to ``namespace Lib`` files only, NOT ``Lib.Sub``.

    Exact-match resolution is the primary explosion guard: a parent-namespace
    using must not transitively pull in every child-namespace file (in C# each
    child namespace needs its own ``using``).
    """
    _seed_csharp_project(tmp_path)
    _write(
        tmp_path / "app" / "Main.cs",
        "namespace App;\nusing Lib;\npublic class Main { }\n",
    )
    _write(
        tmp_path / "lib" / "Root.cs",
        "namespace Lib;\npublic class Root { }\n",
    )
    _write(
        tmp_path / "lib" / "sub" / "Deep.cs",
        "namespace Lib.Sub;\npublic class Deep { }\n",
    )

    dag = build_dag(tmp_path)
    edges = _imports_edges(dag)

    main = "app/Main.cs"
    assert (main, "lib/Root.cs") in edges, f"exact-namespace edge missing; edges={edges}"
    assert (main, "lib/sub/Deep.cs") not in edges, (
        f"``using Lib`` must NOT pull child namespace Lib.Sub; edges={edges}"
    )


def test_csharp_using_fanout_to_many_same_namespace_files(tmp_path):
    """One ``using`` resolves to ALL files declaring the target namespace.

    Same-namespace fan-out is correct (C# namespaces are not directory-tied), so
    a using of a namespace declared by several files yields an edge to each.
    """
    _seed_csharp_project(tmp_path)
    _write(
        tmp_path / "app" / "Caller.cs",
        "namespace App;\nusing Lib.Data;\npublic class Caller { }\n",
    )
    for i in range(3):
        _write(
            tmp_path / "lib" / f"Part{i}.cs",
            f"namespace Lib.Data;\npublic class Part{i} {{ }}\n",
        )

    dag = build_dag(tmp_path)
    edges = _imports_edges(dag)

    caller = "app/Caller.cs"
    for i in range(3):
        assert (caller, f"lib/Part{i}.cs") in edges, (
            f"same-namespace fan-out edge to Part{i} missing; edges={edges}"
        )


def _tested_by_edges(dag) -> set[tuple[str, str]]:
    return {(edge.from_id, edge.to_id) for edge in dag.edges if edge.kind == "tested_by"}


def test_csharp_test_file_using_namespace_under_test(tmp_path):
    """A test ``.cs`` using a project namespace links impl↔test via the index.

    Test files form ``tested_by`` edges (impl → test direction), the same as
    Python/Java/C++; the C# namespace resolver lets the test's ``using`` resolve
    to the implementation file so the relationship is discovered.
    """
    _seed_csharp_project(tmp_path)
    _write(
        tmp_path / "lib" / "Service.cs",
        "namespace Lib.Services;\npublic class Service { }\n",
    )
    _write(
        tmp_path / "test" / "ServiceTests.cs",
        "namespace Tests;\nusing Lib.Services;\npublic class ServiceTests { }\n",
    )

    dag = build_dag(tmp_path)
    tested_by = _tested_by_edges(dag)

    test = "test/ServiceTests.cs"
    service = "lib/Service.cs"
    assert (service, test) in tested_by, (
        f"impl→test (tested_by) edge from resolved using missing; "
        f"tested_by={tested_by}"
    )
