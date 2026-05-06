"""C8 implementation coverage check."""

from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
from functools import lru_cache
from pathlib import Path
import re
from typing import Any

from codd.dag import DAG, Node
from codd.dag.checks import DagCheck, register_dag_check
from codd.llm.design_doc_extractor import ExpectedExtraction, ExpectedNode


DEFAULT_PATH_PREFIX_TOLERANT = ("src/", "lib/", "app/")
_BRACKET_SEGMENT_RE = re.compile(r"\[[^\]]+\]")


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
                if _matches_any_artifact(target_dag, expected_node, root, self.settings):
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
                if any(
                    _hint_matches_node(expected.path_hint, impl_node, root, self.settings)
                    for expected in expected_impl_nodes
                ):
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


def _matches_any_artifact(
    dag: DAG,
    expected_node: ExpectedNode,
    project_root: Path,
    settings: dict[str, Any] | None = None,
) -> bool:
    if expected_node.kind == "config_file":
        hint_path = project_root / _normalize_hint(expected_node.path_hint)
        if hint_path.is_file():
            return True
    if expected_node.kind == "impl_file":
        return _matches_any_impl(dag, expected_node.path_hint, project_root, settings)
    for candidate in _candidate_nodes(dag, expected_node.kind):
        if _hint_matches_node(expected_node.path_hint, candidate, project_root, settings):
            return True
    return False


def _candidate_nodes(dag: DAG, expected_kind: str) -> list[Node]:
    if expected_kind == "test_file":
        return _nodes_by_kind(dag, "test_file")
    if expected_kind == "config_file":
        return [
            node
            for node in sorted(dag.nodes.values(), key=lambda item: item.id)
            if node.kind in {"config_file", "deployment_doc", "impl_file"}
        ]
    return _nodes_by_kind(dag, "impl_file")


def _matches_any_impl(
    dag: DAG,
    path_hint: str,
    project_root: Path | None = None,
    settings: dict[str, Any] | None = None,
) -> bool:
    hint = _normalize_hint(path_hint)
    if not hint:
        return False
    candidates = [candidate for node in _nodes_by_kind(dag, "impl_file") for candidate in _node_path_candidates(node)]

    if any(_exact_path_match(hint, candidate) for candidate in candidates):
        return True
    if any(_glob_path_match(hint, candidate) for candidate in candidates):
        return True
    if any(_bracket_path_match(hint, candidate) for candidate in candidates):
        return True
    if any(_match_with_src_prefix_tolerance(hint, candidate, settings) for candidate in candidates):
        return True
    if any(_soft_path_match(hint, candidate) for candidate in candidates):
        return True

    if project_root is not None and any(char in hint for char in "*?[]"):
        return any((project_root / match).is_file() for match in fnmatch.filter(_project_files(project_root), hint))
    return False


def _hint_matches_node(
    path_hint: str,
    node: Node,
    project_root: Path | None = None,
    settings: dict[str, Any] | None = None,
) -> bool:
    hint = _normalize_hint(path_hint)
    if not hint:
        return False
    candidates = _node_path_candidates(node)
    for candidate in candidates:
        if _exact_path_match(hint, candidate):
            return True
        if _glob_path_match(hint, candidate):
            return True
        if _bracket_path_match(hint, candidate):
            return True
        if _match_with_src_prefix_tolerance(hint, candidate, settings):
            return True
        if _soft_path_match(hint, candidate):
            return True
    if project_root is not None and any(char in hint for char in "*?[]"):
        return any((project_root / match).is_file() for match in fnmatch.filter(_project_files(project_root), hint))
    return False


def _node_path_candidates(node: Node) -> list[str]:
    candidates: list[str] = []
    for value in (node.path, node.id):
        candidate = _normalize_hint(value or "")
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _exact_path_match(hint: str, candidate: str) -> bool:
    return hint == candidate or hint == Path(candidate).name


def _glob_path_match(hint: str, candidate: str) -> bool:
    return fnmatch.fnmatchcase(candidate, hint) or fnmatch.fnmatchcase(candidate, _escape_literal_brackets(hint))


def _bracket_path_match(hint: str, candidate: str) -> bool:
    normalized_hint = _normalize_bracket_segments(hint)
    normalized_candidate = _normalize_bracket_segments(candidate)
    if normalized_hint == hint and normalized_candidate == candidate:
        return False
    if normalized_hint == normalized_candidate:
        return True
    return _glob_path_match(normalized_hint, normalized_candidate)


def _normalize_bracket_segments(path: str) -> str:
    return _BRACKET_SEGMENT_RE.sub("*", path)


def _match_with_src_prefix_tolerance(
    path_hint: str,
    impl_path: str,
    settings: dict[str, Any] | None = None,
) -> bool:
    prefixes = _path_prefix_tolerant(settings)
    if not prefixes:
        return False

    hint = _normalize_hint(path_hint)
    candidate = _normalize_hint(impl_path)
    for hint_variant in _prefix_variants(hint, prefixes):
        for candidate_variant in _prefix_variants(candidate, prefixes):
            if hint_variant == candidate_variant:
                return True
            if _glob_path_match(hint_variant, candidate_variant):
                return True
            if _bracket_path_match(hint_variant, candidate_variant):
                return True
    return False


def _path_prefix_tolerant(settings: dict[str, Any] | None = None) -> list[str]:
    coherence = settings.get("coherence", {}) if isinstance(settings, dict) else {}
    configured = coherence.get("path_prefix_tolerant") if isinstance(coherence, dict) else None
    if configured is None:
        return list(DEFAULT_PATH_PREFIX_TOLERANT)

    prefixes: list[str] = []
    values = [configured] if isinstance(configured, str) else configured
    if not isinstance(values, (list, tuple, set)):
        return list(DEFAULT_PATH_PREFIX_TOLERANT)
    for value in values:
        if not isinstance(value, str):
            continue
        prefix = _normalize_hint(value)
        if not prefix:
            continue
        if not prefix.endswith("/"):
            prefix = f"{prefix}/"
        if prefix not in prefixes:
            prefixes.append(prefix)
    return prefixes


def _prefix_variants(path: str, prefixes: list[str]) -> list[str]:
    variants = [path]
    for prefix in prefixes:
        if path.startswith(prefix):
            variants.append(path.removeprefix(prefix))
        else:
            variants.append(f"{prefix}{path}")
    deduped: list[str] = []
    for variant in variants:
        if variant and variant not in deduped:
            deduped.append(variant)
    return deduped


def _escape_literal_brackets(pattern: str) -> str:
    escaped: list[str] = []
    for char in pattern:
        if char == "[":
            escaped.append("[[]")
        elif char == "]":
            escaped.append("[]]")
        else:
            escaped.append(char)
    return "".join(escaped)


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


@lru_cache(maxsize=16)
def _project_files(project_root: Path) -> list[str]:
    excluded_parts = {
        ".codd",
        ".git",
        ".next",
        "__pycache__",
        "dist",
        "node_modules",
    }
    files: list[str] = []
    for path in project_root.rglob("*"):
        relative = path.relative_to(project_root)
        if excluded_parts.intersection(relative.parts):
            continue
        if path.is_file():
            files.append(relative.as_posix())
    return files


__all__ = ["ImplementationCoverageCheck", "ImplementationCoverageResult"]
