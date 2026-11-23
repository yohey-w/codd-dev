"""Import edges originating from test artifacts are first-class DAG evidence."""

from __future__ import annotations

from pathlib import Path

from codd.dag.builder import build_dag
from codd.dag.checks.implementation_coverage import ImplementationCoverageCheck
from codd.dag.checks.transitive_closure import TransitiveClosureCheck
from codd.dag.checks.unresolved_import_residue import UnresolvedImportResidueCheck
from codd.llm.design_doc_extractor import ExpectedExtraction, ExpectedNode


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _seed_typescript_library(
    root: Path,
    *,
    helper_import: str = "./helper",
) -> None:
    _write(root / "package.json", '{"name":"generic-library","private":true}\n')
    _write(
        root / "codd" / "codd.yaml",
        """project:
  name: generic-library
  language: typescript
scan:
  source_dirs: ["src/"]
  test_dirs: ["tests/"]
  doc_dirs: ["docs/design/"]
""",
    )
    _write(
        root / "docs" / "design" / "spec.md",
        """---
codd:
  node_id: design:library
  type: design
---
# Generic library
Owned implementation: `src/library.ts`
""",
    )
    _write(root / "src" / "library.ts", "export const value = 1;\n")
    _write(
        root / "tests" / "library.test.ts",
        "import { value } from '../src/library';\n"
        f"import {{ fixture }} from '{helper_import}';\n"
        "void value; void fixture;\n",
    )


def _edge_tuples(dag) -> set[tuple[str, str, str]]:
    return {(edge.from_id, edge.to_id, edge.kind) for edge in dag.edges}


def test_test_to_test_import_edge_makes_helper_reachable(tmp_path: Path) -> None:
    _seed_typescript_library(tmp_path)
    _write(tmp_path / "tests" / "helper.ts", "export const fixture = 2;\n")

    dag = build_dag(tmp_path)

    assert (
        "tests/library.test.ts",
        "tests/helper.ts",
        "imports",
    ) in _edge_tuples(dag)
    check = TransitiveClosureCheck()
    assert set(check._code_entry_roots(dag)) == {"tests/library.test.ts"}
    result = check.run(dag)
    assert result.checked_count == 4
    assert result.unreachable_nodes == []
    assert "tests/helper.ts" not in result.unreachable_nodes


def test_common_test_helper_keeps_import_edge_for_impact_trace(tmp_path: Path) -> None:
    _seed_typescript_library(tmp_path)
    _write(tmp_path / "tests" / "helper.ts", "export const fixture = 2;\n")

    dag = build_dag(
        tmp_path,
        {"common_node_patterns": ["tests/helper.ts"]},
    )

    assert dag.nodes["tests/helper.ts"].kind == "common"
    assert (
        "tests/library.test.ts",
        "tests/helper.ts",
        "imports",
    ) in _edge_tuples(dag)


def test_missing_internal_test_helper_is_unresolved_residue(tmp_path: Path) -> None:
    _seed_typescript_library(tmp_path, helper_import="./missing-helper")

    dag = build_dag(tmp_path)
    report = dag.import_residue_report

    assert "tests/library.test.ts: ./missing-helper" in report.residue
    result = UnresolvedImportResidueCheck().run(dag)
    assert result.status == "warn"
    assert "tests/library.test.ts: ./missing-helper" in result.findings


def test_test_to_impl_keeps_only_canonical_tested_by_direction(tmp_path: Path) -> None:
    _seed_typescript_library(tmp_path)
    _write(tmp_path / "tests" / "helper.ts", "export const fixture = 2;\n")

    dag = build_dag(tmp_path)
    edges = _edge_tuples(dag)

    assert ("src/library.ts", "tests/library.test.ts", "tested_by") in edges
    assert ("tests/library.test.ts", "src/library.ts", "imports") not in edges
    assert not any(
        {edge.from_id, edge.to_id} == {"src/library.ts", "tests/library.test.ts"}
        and edge.kind == "imports"
        for edge in dag.edges
    )
    assert dag.detect_cycles() == []


def test_duplicate_test_import_adds_one_edge(tmp_path: Path) -> None:
    _seed_typescript_library(tmp_path)
    _write(tmp_path / "tests" / "helper.ts", "export const fixture = 2;\n")
    _write(
        tmp_path / "tests" / "library.test.ts",
        "import { fixture } from './helper';\n"
        "import { fixture as sameFixture } from './helper';\n"
        "void fixture; void sameFixture;\n",
    )

    dag = build_dag(tmp_path)
    matching_edges = [
        edge
        for edge in dag.edges
        if (
            edge.from_id,
            edge.to_id,
            edge.kind,
        )
        == ("tests/library.test.ts", "tests/helper.ts", "imports")
    ]

    assert len(matching_edges) == 1


def test_missing_declared_test_helper_stays_red_in_implementation_coverage(
    tmp_path: Path,
) -> None:
    _seed_typescript_library(tmp_path, helper_import="./missing-helper")
    dag = build_dag(tmp_path)
    dag.nodes["docs/design/spec.md"].attributes["expected_extraction"] = (
        ExpectedExtraction(
            expected_nodes=[
                ExpectedNode(
                    kind="test_file",
                    path_hint="tests/missing-helper.ts",
                    rationale="declared test support artifact",
                    source_design_section="Tests",
                )
            ],
            expected_edges=[],
            source_design_doc="docs/design/spec.md",
        )
    )

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})
    missing = [
        violation
        for violation in result.violations
        if violation.get("type") == "missing_implementation"
    ]

    assert result.status == "fail"
    assert result.checked_count == 1
    assert [violation["path_hint"] for violation in missing] == [
        "tests/missing-helper.ts"
    ]


def test_python_test_to_test_import_uses_existing_suffix_resolver(tmp_path: Path) -> None:
    _write(
        tmp_path / "codd" / "codd.yaml",
        """project:
  name: generic-python-library
  language: python
scan:
  source_dirs: ["src/"]
  test_dirs: ["tests/"]
  doc_dirs: ["docs/design/"]
""",
    )
    _write(tmp_path / "pyproject.toml", '[project]\nname="generic-library"\nversion="0.1.0"\n')
    _write(
        tmp_path / "docs" / "design" / "spec.md",
        "---\ncodd:\n  node_id: design:library\n  type: design\n---\n`src/library.py`\n",
    )
    _write(tmp_path / "src" / "library.py", "VALUE = 1\n")
    _write(tmp_path / "tests" / "test_helper.py", "FIXTURE = 2\n")
    _write(
        tmp_path / "tests" / "test_library.py",
        "from .test_helper import FIXTURE\nfrom src.library import VALUE\n",
    )

    dag = build_dag(tmp_path)
    edges = _edge_tuples(dag)

    assert ("tests/test_library.py", "tests/test_helper.py", "imports") in edges
    assert ("src/library.py", "tests/test_library.py", "tested_by") in edges
    assert ("tests/test_library.py", "src/library.py", "imports") not in edges
