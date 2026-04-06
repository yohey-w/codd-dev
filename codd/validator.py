"""CoDD validator — verify frontmatter integrity before scan/impact."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from codd.bridge import load_bridge_registry


NODE_ID_PATTERN = re.compile(r"^(?P<prefix>[a-z_]+):(?P<name>.+)$")
ALLOWED_NODE_PREFIXES = {
    "config",
    "db",
    "db_column",
    "db_table",
    "design",
    "detail",
    "detailed",
    "doc",
    "endpoint",
    "file",
    "governance",
    "infra",
    "module",
    "operations",
    "ops",
    "plan",
    "req",
    "test",
}
LEVEL_ERROR = "ERROR"
LEVEL_BLOCKED = "BLOCKED"
LEVEL_WARNING = "WARNING"
IMPLEMENTATION_NODE_PREFIXES = {
    "config",
    "db",
    "db_column",
    "db_table",
    "endpoint",
    "file",
    "infra",
    "test",
}
IMPLEMENTATION_DESIGN_SUFFIXES = ("-service", "-integration", "-delivery")


@dataclass(frozen=True)
class ValidationIssue:
    level: str
    code: str
    location: str
    message: str


@dataclass
class ValidationResult:
    documents_checked: int = 0
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for issue in self.issues if issue.level == LEVEL_ERROR)

    @property
    def blocked_count(self) -> int:
        return sum(1 for issue in self.issues if issue.level == LEVEL_BLOCKED)

    @property
    def warning_count(self) -> int:
        return sum(1 for issue in self.issues if issue.level == LEVEL_WARNING)

    @property
    def exit_code(self) -> int:
        return 1 if self.error_count else 0

    def add(self, level: str, code: str, location: str, message: str):
        self.issues.append(ValidationIssue(level=level, code=code, location=location, message=message))

    def status(self) -> str:
        if self.error_count:
            return LEVEL_ERROR
        if self.blocked_count:
            return LEVEL_BLOCKED
        if self.warning_count:
            return LEVEL_WARNING
        return "OK"

    def sorted_issues(self) -> list[ValidationIssue]:
        level_order = {LEVEL_ERROR: 0, LEVEL_BLOCKED: 1, LEVEL_WARNING: 2}
        return sorted(
            self.issues,
            key=lambda issue: (level_order.get(issue.level, 99), issue.location, issue.code, issue.message),
        )


@dataclass
class DocumentRecord:
    path: str
    node_id: str
    doc_type: str
    depends_on: list[str]
    depended_by: list[str]
    conventions: list[str]


def run_validate(project_root: Path, codd_dir: Path) -> int:
    """Validate CoDD documents and print a human-readable report."""
    result = validate_project(project_root, codd_dir)

    if result.status() == "OK":
        print(f"OK: validated {result.documents_checked} Markdown files under configured doc_dirs")
        return 0

    summary = (
        f"{result.status()}: {result.error_count} error(s), "
        f"{result.blocked_count} blocked issue(s), "
        f"{result.warning_count} warning(s), {result.documents_checked} Markdown files checked"
    )
    print(summary)
    for issue in result.sorted_issues():
        print(f"[{issue.level}] {issue.location}: {issue.message}")
    return result.exit_code


def _validate_project_oss(project_root: Path, codd_dir: Path | None = None) -> ValidationResult:
    """Validate CoDD frontmatter, references, wave config, and dependency cycles."""
    codd_dir = codd_dir or (project_root / "codd")
    config_path = codd_dir / "codd.yaml"
    config = yaml.safe_load(config_path.read_text()) or {}

    result = ValidationResult()
    documents: dict[str, DocumentRecord] = {}
    wave_expectations = _extract_wave_config_expectations(config)
    wave_defined_nodes = set(wave_expectations)
    scanned_node_ids = _load_scanned_node_ids(project_root, config)
    service_boundary_modules = _extract_service_boundary_modules(config)

    for doc_path in _iter_doc_files(project_root, config):
        result.documents_checked += 1
        relative_path = doc_path.relative_to(project_root).as_posix()
        frontmatter = _parse_codd_frontmatter(doc_path)
        if frontmatter.error:
            result.add("ERROR", frontmatter.error["code"], relative_path, frontmatter.error["message"])
            continue

        codd = frontmatter.codd or {}
        node_id = codd.get("node_id")
        if not isinstance(node_id, str) or not _is_valid_node_id(node_id):
            result.add(
                "ERROR",
                "invalid_node_id",
                relative_path,
                f"node_id must follow CoDD naming rules (<prefix>:<name>), got {node_id!r}",
            )
            continue

        depends_on = _extract_reference_ids(codd.get("depends_on"))
        depended_by = _extract_reference_ids(codd.get("depended_by"))
        conventions = _extract_convention_targets(codd.get("conventions"))

        existing = documents.get(node_id)
        if existing:
            result.add(
                "ERROR",
                "duplicate_node_id",
                relative_path,
                f"node_id {node_id!r} is already defined in {existing.path}",
            )
            continue

        documents[node_id] = DocumentRecord(
            path=relative_path,
            node_id=node_id,
            doc_type=str(codd.get("type") or ""),
            depends_on=depends_on,
            depended_by=depended_by,
            conventions=conventions,
        )

    defined_nodes = set(documents)
    known_convention_targets = defined_nodes | scanned_node_ids

    for record in documents.values():
        for target_id in record.depends_on:
            if target_id not in defined_nodes:
                level, message = _classify_missing_reference(
                    target_id,
                    relation="depends_on",
                    source_doc_type=record.doc_type,
                    wave_defined_nodes=wave_defined_nodes,
                    service_boundary_modules=service_boundary_modules,
                )
                result.add(level, "dangling_depends_on", record.path, message)
        for source_id in record.depended_by:
            if source_id not in defined_nodes:
                level, message = _classify_missing_reference(
                    source_id,
                    relation="depended_by",
                    source_doc_type=record.doc_type,
                    wave_defined_nodes=wave_defined_nodes,
                    service_boundary_modules=service_boundary_modules,
                )
                result.add(level, "dangling_depended_by", record.path, message)
        for target_id in record.conventions:
            if target_id not in known_convention_targets:
                result.add(
                    LEVEL_WARNING,
                    "dangling_convention",
                    record.path,
                    f"conventions references undefined node {target_id!r}",
                )

    for record in documents.values():
        for target_id in record.depends_on:
            target = documents.get(target_id)
            if target and record.node_id not in set(target.depended_by):
                result.add(
                    LEVEL_WARNING,
                    "missing_depended_by",
                    target.path,
                    f"depended_by is missing reciprocal reference to {record.node_id!r}",
                )

    for node_id, expected_depends in wave_expectations.items():
        record = documents.get(node_id)
        if not record:
            result.add(
                LEVEL_BLOCKED,
                "wave_config_missing_node",
                config_path.relative_to(project_root).as_posix(),
                f"wave_config defines {node_id!r}, but the document has not been generated yet",
            )
            continue

        actual_depends = set(record.depends_on)
        if actual_depends != expected_depends:
            missing = sorted(expected_depends - actual_depends)
            unexpected = sorted(actual_depends - expected_depends)
            details = []
            if missing:
                details.append(f"missing {missing}")
            if unexpected:
                details.append(f"unexpected {unexpected}")
            detail_text = ", ".join(details) if details else "dependency mismatch"
            result.add(
                LEVEL_ERROR,
                "wave_config_mismatch",
                record.path,
                f"wave_config mismatch for {node_id!r}: {detail_text}",
            )

    adjacency = _build_adjacency(documents)
    for cycle in _find_cycles(adjacency):
        cycle_text = " -> ".join(list(cycle) + [cycle[0]])
        location = documents[cycle[0]].path if cycle[0] in documents else config_path.relative_to(project_root).as_posix()
        result.add(LEVEL_ERROR, "circular_dependency", location, f"circular dependency detected: {cycle_text}")

    return result


def validate_project(project_root: Path, codd_dir: Path | None = None) -> ValidationResult:
    """Validate the project, delegating to a Pro bridge when registered."""
    handler = load_bridge_registry().validator_handler
    if handler is not None:
        return handler(project_root, codd_dir, _validate_project_oss)
    return _validate_project_oss(project_root, codd_dir)


@dataclass
class FrontmatterParseResult:
    codd: dict[str, Any] | None = None
    error: dict[str, str] | None = None


def _iter_doc_files(project_root: Path, config: dict[str, Any]):
    doc_dirs = ((config.get("scan") or {}).get("doc_dirs") or [])
    for doc_dir in doc_dirs:
        full_path = project_root / doc_dir
        if not full_path.exists():
            continue
        for file_path in sorted(full_path.rglob("*.md")):
            if file_path.is_file():
                yield file_path


def _parse_codd_frontmatter(file_path: Path) -> FrontmatterParseResult:
    try:
        content = file_path.read_text(errors="ignore")
    except Exception as exc:
        return FrontmatterParseResult(
            error={
                "code": "read_error",
                "message": f"failed to read file: {exc}",
            }
        )

    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return FrontmatterParseResult(
            error={
                "code": "missing_frontmatter",
                "message": "missing CoDD YAML frontmatter",
            }
        )

    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return FrontmatterParseResult(
            error={
                "code": "invalid_frontmatter",
                "message": f"invalid YAML frontmatter: {exc}",
            }
        )

    if not isinstance(frontmatter, dict) or not isinstance(frontmatter.get("codd"), dict):
        return FrontmatterParseResult(
            error={
                "code": "missing_frontmatter",
                "message": "missing CoDD YAML frontmatter",
            }
        )

    return FrontmatterParseResult(codd=frontmatter["codd"])


def _is_valid_node_id(node_id: str) -> bool:
    match = NODE_ID_PATTERN.match(node_id.strip())
    if not match:
        return False
    return match.group("prefix") in ALLOWED_NODE_PREFIXES


def _extract_reference_ids(entries: Any) -> list[str]:
    if not entries:
        return []

    refs = []
    for entry in entries:
        if isinstance(entry, str):
            refs.append(entry)
            continue
        if isinstance(entry, dict):
            ref_id = entry.get("id") or entry.get("node_id")
            if isinstance(ref_id, str):
                refs.append(ref_id)
    return refs


def _extract_convention_targets(entries: Any) -> list[str]:
    if not entries:
        return []

    targets = []
    for entry in entries:
        if isinstance(entry, str):
            targets.append(entry)
            continue
        if not isinstance(entry, dict):
            continue
        value = entry.get("targets", [])
        if isinstance(value, str):
            targets.append(value)
        elif isinstance(value, list):
            targets.extend(item for item in value if isinstance(item, str))
    return targets


def _classify_missing_reference(
    target_id: str,
    *,
    relation: str,
    source_doc_type: str,
    wave_defined_nodes: set[str],
    service_boundary_modules: set[str],
) -> tuple[str, str]:
    if target_id in wave_defined_nodes:
        return (
            LEVEL_BLOCKED,
            f"{relation} references planned node {target_id!r} from wave_config, but it has not been generated yet",
        )

    if relation == "depends_on" and source_doc_type == "requirement":
        if _is_requirement_phase_reference(target_id, service_boundary_modules):
            return (
                LEVEL_WARNING,
                f"{relation} references implementation-phase node {target_id!r}; define it later via docs or scan",
            )

    return LEVEL_ERROR, f"{relation} references undefined node {target_id!r}"


def _is_requirement_phase_reference(target_id: str, service_boundary_modules: set[str]) -> bool:
    match = NODE_ID_PATTERN.match(target_id.strip())
    if not match:
        return False

    prefix = match.group("prefix")
    name = match.group("name")

    if prefix in IMPLEMENTATION_NODE_PREFIXES:
        return True

    if prefix == "module":
        return not service_boundary_modules or name in service_boundary_modules

    if prefix == "design":
        return name.endswith(IMPLEMENTATION_DESIGN_SUFFIXES)

    return False


def _extract_service_boundary_modules(config: dict[str, Any]) -> set[str]:
    boundaries = config.get("service_boundaries")
    if not isinstance(boundaries, list):
        return set()

    modules: set[str] = set()
    for entry in boundaries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if isinstance(name, str) and name:
            modules.add(name)
    return modules


def _load_scanned_node_ids(project_root: Path, config: dict[str, Any]) -> set[str]:
    graph_config = config.get("graph")
    graph_path = "codd/scan"
    if isinstance(graph_config, dict):
        configured_path = graph_config.get("path")
        if isinstance(configured_path, str) and configured_path.strip():
            graph_path = configured_path

    scan_dir = Path(graph_path)
    if not scan_dir.is_absolute():
        scan_dir = project_root / scan_dir

    nodes_path = scan_dir / "nodes.jsonl"
    if not nodes_path.exists():
        return set()

    node_ids: set[str] = set()
    for line in nodes_path.read_text().splitlines():
        payload = line.strip()
        if not payload:
            continue
        try:
            node = json.loads(payload)
        except json.JSONDecodeError:
            continue
        node_id = node.get("id")
        if isinstance(node_id, str) and node_id:
            node_ids.add(node_id)
    return node_ids


def _extract_wave_config_expectations(config: dict[str, Any]) -> dict[str, set[str]]:
    wave_config = config.get("wave_config")
    if not wave_config:
        return {}

    expectations: dict[str, set[str]] = {}
    for node_id, depends_on in _walk_wave_entries(wave_config):
        expectations.setdefault(node_id, set()).update(depends_on)
    return expectations


def _walk_wave_entries(node: Any):
    if isinstance(node, list):
        for item in node:
            yield from _walk_wave_entries(item)
        return

    if isinstance(node, dict):
        node_id = node.get("node_id") or node.get("id")
        depends_on = node.get("depends_on")
        if isinstance(node_id, str):
            yield node_id, set(_extract_reference_ids(depends_on))
            return

        for key in ("nodes", "documents", "artifacts", "waves", "items"):
            if key in node:
                yield from _walk_wave_entries(node[key])

        for key, value in node.items():
            if key in {"nodes", "documents", "artifacts", "waves", "items", "depends_on"}:
                continue
            if isinstance(value, (dict, list)):
                yield from _walk_wave_entries(value)


def _build_adjacency(documents: dict[str, DocumentRecord]) -> dict[str, set[str]]:
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in documents}
    for record in documents.values():
        for target_id in record.depends_on:
            if target_id in documents:
                adjacency[record.node_id].add(target_id)
        for source_id in record.depended_by:
            if source_id in documents:
                adjacency.setdefault(source_id, set()).add(record.node_id)
    return adjacency


def _find_cycles(adjacency: dict[str, set[str]]) -> list[tuple[str, ...]]:
    cycles: set[tuple[str, ...]] = set()
    visited: set[str] = set()
    visiting: dict[str, int] = {}
    stack: list[str] = []

    def dfs(node: str):
        visiting[node] = len(stack)
        stack.append(node)

        for neighbor in adjacency.get(node, set()):
            if neighbor in visiting:
                cycle = stack[visiting[neighbor]:]
                cycles.add(_canonicalize_cycle(cycle))
                continue
            if neighbor in visited:
                continue
            dfs(neighbor)

        stack.pop()
        visiting.pop(node, None)
        visited.add(node)

    for node in sorted(adjacency):
        if node not in visited:
            dfs(node)

    return sorted(cycles)


def _canonicalize_cycle(nodes: list[str]) -> tuple[str, ...]:
    if not nodes:
        return tuple()
    rotations = [tuple(nodes[index:] + nodes[:index]) for index in range(len(nodes))]
    return min(rotations)
