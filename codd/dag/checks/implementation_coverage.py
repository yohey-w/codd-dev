"""C8 implementation coverage check."""

from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
from pathlib import Path
from typing import Any

from codd.dag import DAG, Node
from codd.dag.checks import DagCheck, register_dag_check
from codd.llm.design_doc_extractor import ExpectedExtraction, ExpectedNode


@dataclass
class ImplementationCoverageResult:
    check_name: str = "implementation_coverage"
    severity: str = "red"
    status: str = "pass"
    message: str = ""
    block_deploy: bool = False
    violations: list[dict[str, Any]] = field(default_factory=list)
    passed: bool = True


@register_dag_check("implementation_coverage")
class ImplementationCoverageCheck(DagCheck):
    """Compare design-derived expected artifacts with the project DAG."""

    check_name = "implementation_coverage"
    severity = "red"
    block_deploy = False

    def run(
        self,
        dag: DAG | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
    ) -> ImplementationCoverageResult:
        target_dag = dag if dag is not None else self.dag
        if target_dag is None:
            raise ValueError("dag is required for implementation_coverage check")
        if project_root is not None:
            self.project_root = Path(project_root)
        if settings is not None:
            self.settings = settings
        root = self.project_root or Path.cwd()

        violations: list[dict[str, Any]] = []
        expected_impl_nodes: list[ExpectedNode] = []
        for design_doc in _design_doc_nodes(target_dag):
            expected = _expected_extraction(design_doc)
            if expected is None:
                continue
            for expected_node in expected.expected_nodes:
                if expected_node.kind == "impl_file":
                    expected_impl_nodes.append(expected_node)
                if _matches_any_artifact(target_dag, expected_node, root):
                    continue
                violations.append(
                    {
                        "type": "missing_implementation",
                        "design_doc": design_doc.id,
                        "expected_kind": expected_node.kind,
                        "path_hint": expected_node.path_hint,
                        "rationale": expected_node.rationale,
                        "source_design_section": expected_node.source_design_section,
                        "severity": "red",
                    }
                )

        if expected_impl_nodes:
            for impl_node in _nodes_by_kind(target_dag, "impl_file"):
                if any(_hint_matches_node(expected.path_hint, impl_node, root) for expected in expected_impl_nodes):
                    continue
                violations.append(
                    {
                        "type": "additional_implementation",
                        "impl_file": impl_node.id,
                        "severity": "amber",
                    }
                )

        red_count = sum(1 for item in violations if item.get("severity") == "red")
        amber_count = sum(1 for item in violations if item.get("severity") == "amber")
        status = "fail" if red_count else ("warn" if amber_count else "pass")
        severity = "red" if red_count else ("amber" if amber_count else "info")
        if red_count:
            message = f"C8 implementation_coverage found {red_count} missing expected artifact(s)"
        elif amber_count:
            message = f"C8 implementation_coverage found {amber_count} additional implementation artifact(s)"
        else:
            message = "C8 implementation_coverage PASS"

        return ImplementationCoverageResult(
            severity=severity,
            status=status,
            message=message,
            block_deploy=self.block_deploy,
            violations=violations,
            passed=red_count == 0,
        )


def _design_doc_nodes(dag: DAG) -> list[Node]:
    return _nodes_by_kind(dag, "design_doc")


def _nodes_by_kind(dag: DAG, kind: str) -> list[Node]:
    return [node for node in sorted(dag.nodes.values(), key=lambda item: item.id) if node.kind == kind]


def _expected_extraction(design_doc: Node) -> ExpectedExtraction | None:
    payload = design_doc.attributes.get("expected_extraction")
    if isinstance(payload, ExpectedExtraction):
        return payload
    if isinstance(payload, dict):
        return ExpectedExtraction.from_dict(payload)
    return None


def _matches_any_artifact(dag: DAG, expected_node: ExpectedNode, project_root: Path) -> bool:
    if expected_node.kind == "config_file":
        hint_path = project_root / _normalize_hint(expected_node.path_hint)
        if hint_path.is_file():
            return True
    for candidate in _candidate_nodes(dag, expected_node.kind):
        if _hint_matches_node(expected_node.path_hint, candidate, project_root):
            return True
    return False


def _candidate_nodes(dag: DAG, expected_kind: str) -> list[Node]:
    if expected_kind == "test_file":
        return _nodes_by_kind(dag, "test_file")
    if expected_kind == "config_file":
        return [
            node
            for node in sorted(dag.nodes.values(), key=lambda item: item.id)
            if node.kind in {"config_file", "deployment_doc"}
        ]
    return _nodes_by_kind(dag, "impl_file")


def _hint_matches_node(path_hint: str, node: Node, project_root: Path | None = None) -> bool:
    hint = _normalize_hint(path_hint)
    if not hint:
        return False
    candidates = [_normalize_hint(value) for value in (node.id, node.path) if value]
    for candidate in candidates:
        if hint == candidate or hint == Path(candidate).name:
            return True
        if fnmatch.fnmatchcase(candidate, hint):
            return True
        if _soft_path_match(hint, candidate):
            return True
    if project_root is not None and any(char in hint for char in "*?[]"):
        return any((project_root / match).is_file() for match in fnmatch.filter(_project_files(project_root), hint))
    return False


def _soft_path_match(hint: str, candidate: str) -> bool:
    if any(char in hint for char in "*?[]"):
        return False
    hint_lower = hint.lower()
    candidate_lower = candidate.lower()
    if hint_lower in candidate_lower:
        return True
    return Path(hint_lower).stem == Path(candidate_lower).stem


def _normalize_hint(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def _project_files(project_root: Path) -> list[str]:
    return [path.relative_to(project_root).as_posix() for path in project_root.rglob("*") if path.is_file()]


__all__ = ["ImplementationCoverageCheck", "ImplementationCoverageResult"]
