"""CoDD planner — compute wave readiness from configured artifacts."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

import codd.generator as generator_module
from codd.generator import WaveArtifact, _load_project_config, _load_wave_artifacts
from codd.validator import _iter_doc_files, _parse_codd_frontmatter, validate_project


STATUS_DONE = "DONE"
STATUS_READY = "READY"
STATUS_BLOCKED = "BLOCKED"
STATUS_ERROR = "ERROR"

ICON_BY_STATUS = {
    STATUS_DONE: "✅",
    STATUS_READY: "🔵",
    STATUS_BLOCKED: "🔴",
    STATUS_ERROR: "⚠️",
}

MECE_DOCUMENT_STRUCTURE = """\
docs/
├── requirements/      # What  — source-of-truth requirements
├── design/            # How   — overview architecture and cross-cutting design
├── detailed_design/   # How   — module ownership, flows, and implementation-ready diagrams
├── plan/              # When  — implementation sequencing and milestones
├── governance/        # Why   — decisions, ADRs, change requests
├── test/              # Verify — acceptance criteria and test strategy
└── operations/        # Run   — runbooks, monitoring, incident handling
"""

STANDARD_V_MODEL_PATTERNS = """\
Typical wave patterns:
- Wave 1: acceptance criteria and decision records derived directly from requirements
- Wave 2: overview/system design that depends on requirements and wave 1 outputs
- Wave 3-4: domain design such as API, database, auth, UX, and integration design
- Wave 5: detailed design artifacts under docs/detailed_design/ with Mermaid diagrams, ownership boundaries, and runtime flows
- Wave 6: implementation planning that depends on the approved overview + detailed design set
"""


@dataclass(frozen=True)
class PlannedArtifact:
    """Planner view of one wave-configured artifact."""

    wave: int
    node_id: str
    path: str
    status: str
    depends_on: list[str]
    blocked_by: list[str]
    validation_errors: list[str]


@dataclass(frozen=True)
class PlannedWave:
    """Planner view of one wave."""

    wave: int
    status: str
    nodes: list[PlannedArtifact]


@dataclass(frozen=True)
class PlanResult:
    """Serializable planner output."""

    project_root: str
    summary: dict[str, int]
    next_wave: int | None
    waves: list[PlannedWave]


@dataclass(frozen=True)
class RequirementDocument:
    """Requirement document used to synthesize wave_config."""

    node_id: str
    path: str
    content: str


@dataclass(frozen=True)
class PlanInitResult:
    """Result of initializing wave_config from requirements."""

    project_root: str
    config_path: str
    requirement_paths: list[str]
    wave_config: dict[str, list[dict[str, Any]]]


@dataclass(frozen=True)
class _ExternalNode:
    path: str
    status: str


def plan_init(
    project_root: Path,
    *,
    force: bool = False,
    ai_command: str | None = None,
) -> PlanInitResult:
    """Initialize wave_config from requirement documents."""
    project_root = project_root.resolve()
    config = _load_project_config(project_root)

    if config.get("wave_config") and not force:
        raise FileExistsError("codd.yaml already contains wave_config")

    requirement_documents = _load_requirement_documents(project_root, config)
    if not requirement_documents:
        raise ValueError("no requirement documents with CoDD frontmatter were found under configured doc_dirs")

    resolved_ai_command = generator_module._resolve_ai_command(config, ai_command)
    prompt = _build_plan_init_prompt(config, requirement_documents)
    raw_wave_config = generator_module._invoke_ai_command(resolved_ai_command, prompt)
    wave_config = _parse_wave_config_output(raw_wave_config)

    config["wave_config"] = wave_config
    config_path = project_root / "codd" / "codd.yaml"
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    return PlanInitResult(
        project_root=str(project_root),
        config_path=str(config_path),
        requirement_paths=[document.path for document in requirement_documents],
        wave_config=wave_config,
    )


def build_plan(project_root: Path) -> PlanResult:
    """Build wave execution status for a CoDD project."""
    project_root = project_root.resolve()
    config = _load_project_config(project_root)
    artifacts = _load_wave_artifacts(config)
    artifacts_by_node = {artifact.node_id: artifact for artifact in artifacts}
    ordered_node_ids = _topological_order(artifacts)

    validation = validate_project(project_root, project_root / "codd")
    errors_by_location = _group_validation_errors(validation.issues)
    external_nodes = _index_external_nodes(project_root, config, errors_by_location, set(artifacts_by_node))

    planned_nodes: dict[str, PlannedArtifact] = {}
    for node_id in ordered_node_ids:
        artifact = artifacts_by_node[node_id]
        location = Path(artifact.output).as_posix()
        doc_path = project_root / artifact.output
        validation_errors = sorted(set(errors_by_location.get(location, [])))
        depends_on = _dependency_ids(artifact)

        if doc_path.exists():
            status = STATUS_ERROR if validation_errors else STATUS_DONE
            blocked_by: list[str] = []
        else:
            blocked_by = [
                dependency_id
                for dependency_id in depends_on
                if _dependency_status(dependency_id, planned_nodes, external_nodes) != STATUS_DONE
            ]
            status = STATUS_READY if not blocked_by else STATUS_BLOCKED

        planned_nodes[node_id] = PlannedArtifact(
            wave=artifact.wave,
            node_id=artifact.node_id,
            path=location,
            status=status,
            depends_on=depends_on,
            blocked_by=blocked_by,
            validation_errors=validation_errors,
        )

    waves = [
        PlannedWave(
            wave=wave,
            status=_wave_status([planned_nodes[artifact.node_id] for artifact in artifacts if artifact.wave == wave]),
            nodes=[planned_nodes[artifact.node_id] for artifact in artifacts if artifact.wave == wave],
        )
        for wave in sorted({artifact.wave for artifact in artifacts})
    ]

    summary = {
        "done": sum(1 for node in planned_nodes.values() if node.status == STATUS_DONE),
        "ready": sum(1 for node in planned_nodes.values() if node.status == STATUS_READY),
        "blocked": sum(1 for node in planned_nodes.values() if node.status == STATUS_BLOCKED),
        "error": sum(1 for node in planned_nodes.values() if node.status == STATUS_ERROR),
    }
    next_wave = next((wave.wave for wave in waves if any(node.status == STATUS_READY for node in wave.nodes)), None)

    return PlanResult(
        project_root=str(project_root),
        summary=summary,
        next_wave=next_wave,
        waves=waves,
    )


def render_plan_text(plan: PlanResult) -> str:
    """Render a human-readable wave plan."""
    lines: list[str] = []

    for index, wave in enumerate(plan.waves):
        if index:
            lines.append("")
        lines.append(f"Wave {wave.wave}: {wave.status}")
        for node in wave.nodes:
            lines.append(f"  {ICON_BY_STATUS[node.status]} {node.node_id}  [{node.status}] {node.path}")
            if node.status == STATUS_READY and node.depends_on:
                lines.append(f"     depends_on: {', '.join(node.depends_on)}")
            elif node.status == STATUS_BLOCKED:
                blocked_text = ", ".join(node.blocked_by) if node.blocked_by else "(unknown)"
                lines.append(f"     blocked_by: {blocked_text}")
            elif node.status == STATUS_ERROR:
                for message in node.validation_errors:
                    lines.append(f"     error: {message}")

    lines.append("")
    lines.append(
        "Summary: "
        f"{plan.summary['done']} DONE, "
        f"{plan.summary['ready']} READY, "
        f"{plan.summary['blocked']} BLOCKED, "
        f"{plan.summary['error']} ERROR"
    )
    if plan.next_wave is not None:
        lines.append(f"Next action: codd generate --wave {plan.next_wave}")
    elif plan.summary["error"]:
        lines.append("Next action: resolve validation errors")
    else:
        lines.append("Next action: all waves DONE")

    return "\n".join(lines)


def plan_to_dict(plan: PlanResult) -> dict:
    """Convert planner output to plain Python data for JSON serialization."""
    return asdict(plan)


def _load_requirement_documents(project_root: Path, config: dict[str, Any]) -> list[RequirementDocument]:
    documents: list[RequirementDocument] = []

    for doc_path in _iter_doc_files(project_root, config):
        parsed = _parse_codd_frontmatter(doc_path)
        if parsed.error:
            continue

        codd = parsed.codd or {}
        if codd.get("type") != "requirement":
            continue

        node_id = codd.get("node_id")
        if not isinstance(node_id, str) or not node_id.strip():
            continue

        documents.append(
            RequirementDocument(
                node_id=node_id.strip(),
                path=doc_path.relative_to(project_root).as_posix(),
                content=doc_path.read_text(encoding="utf-8"),
            )
        )

    return documents


def _build_plan_init_prompt(config: dict[str, Any], requirement_documents: list[RequirementDocument]) -> str:
    project = config.get("project") or {}
    scan = config.get("scan") or {}
    doc_dirs = scan.get("doc_dirs") or []
    project_name = project.get("name") or "(unknown)"
    language = project.get("language") or "(unknown)"

    lines = [
        "You are initializing CoDD wave_config from requirement documents.",
        f"Project name: {project_name}",
        f"Primary language: {language}",
        "Configured doc_dirs: " + (", ".join(str(item) for item in doc_dirs) if doc_dirs else "(none)"),
        "",
        "MECE Document Structure (7 categories):",
        MECE_DOCUMENT_STRUCTURE.rstrip(),
        "",
        "Standard V-model artifact patterns:",
        STANDARD_V_MODEL_PATTERNS.rstrip(),
        "",
        "Instructions:",
        "- Read the requirement documents below and decide the minimum complete document set needed for this project.",
        "- Output ONLY YAML for the wave_config mapping. Do not emit prose or Markdown fences.",
        "- Use string wave numbers as the top-level keys.",
        "- Each artifact entry must include node_id, output, title, depends_on, and conventions.",
        "- Insert a dedicated detailed design wave between overview design and implementation planning when the project has multiple modules, integrations, workflows, or shared domain concepts.",
        "- Detailed design artifacts must live under docs/detailed_design/ and stay Markdown + Mermaid (text-first, no binary diagrams).",
        "- Decide which detailed design artifacts are necessary from the project context; do not hardcode a fixed set. Good candidates include shared domain ownership, component dependency maps, ER/CRUD views, key sequence diagrams, and state machines.",
        "- conventions are release-blocking constraints. If a convention is violated, the project is not releasable.",
        "- Extract conventions from the requirement documents for these categories:",
        "  security constraints (tenant isolation, authentication, authorization, auditability),",
        "  technical constraints (required stack, forbidden libraries, mandated integrations),",
        "  legal/regulatory requirements (privacy, GDPR, APPI, contractual obligations), and",
        "  non-functional requirements (SLA, latency, throughput, availability, recovery thresholds).",
        "- Assign the relevant conventions to each artifact entry. Use conventions: [] only when an artifact truly has no release-blocking constraints.",
        "- Do not add requirement documents themselves to wave_config.",
        "- Keep output paths under docs/design/, docs/detailed_design/, docs/plan/, docs/governance/, docs/test/, or docs/operations/.",
        "- Set dependencies so earlier waves unlock later waves in a realistic order.",
        "- Do not emit explanatory headings or summaries such as 'Key conventions extracted:' or 'Notes:' before the YAML.",
        "",
        "Required schema (JSON notation):",
        "{",
        '  "<wave-number>": [',
        "    {",
        '      "node_id": "category:name",',
        '      "output": "docs/.../file.md",',
        '      "title": "Document Title",',
        '      "depends_on": [{"id": "node:id", "relation": "derives_from", "semantic": "governance"}],',
        '      "conventions": [{"targets": ["node:id"], "reason": "release-blocking constraint"}]',
        "    }",
        "  ]",
        "}",
        "",
        "Example output shape (YAML mapping only; do not wrap it in a top-level wave_config key):",
        '"1":',
        '  - node_id: "design:acceptance-criteria"',
        '    output: "docs/test/acceptance_criteria.md"',
        '    title: "Acceptance Criteria"',
        "    depends_on:",
        '      - id: "req:project-requirements"',
        '        relation: "derives_from"',
        '        semantic: "governance"',
        "    conventions:",
        "      - targets:",
        '          - "db:rls_policies"',
        '          - "module:auth"',
        '        reason: "Tenant isolation and authenticated access are release-blocking constraints."',
        '"2":',
        '  - node_id: "design:system-design"',
        '    output: "docs/design/system_design.md"',
        '    title: "System Design"',
        "    depends_on:",
        '      - id: "design:acceptance-criteria"',
        '        relation: "constrained_by"',
        '        semantic: "governance"',
        "    conventions:",
        "      - targets:",
        '          - "db:rls_policies"',
        '          - "service:auth"',
        '        reason: "Security, privacy, and access-control constraints must be reflected explicitly."',
        '"3":',
        '  - node_id: "design:shared-domain-model"',
        '    output: "docs/detailed_design/shared_domain_model.md"',
        '    title: "Shared Domain Model"',
        "    depends_on:",
        '      - id: "design:system-design"',
        '        relation: "depends_on"',
        '        semantic: "technical"',
        "    conventions:",
        "      - targets:",
        '          - "module:auth"',
        '          - "db:rls_policies"',
        '        reason: "Canonical ownership of shared types and tenant boundaries must be implementation-ready before coding begins."',
        "",
        "User instruction:",
        "以下の要件定義書を読み、このプロジェクトに必要な設計成果物・依存順序・artifactごとのconventionsを判断し、wave_config形式のYAMLを出力せよ。",
        "conventionsは『違反したらリリース不可の制約』として抽出し、各artifactへ必ず割り当てること。",
        "詳細設計waveが必要な場合は docs/detailed_design/ 配下に Mermaid 図を含む artifact を提案せよ。",
        "",
        "Requirement documents:",
    ]

    for document in requirement_documents:
        lines.extend(
            [
                f"--- BEGIN REQUIREMENT {document.path} ({document.node_id}) ---",
                document.content.rstrip(),
                f"--- END REQUIREMENT {document.path} ---",
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def _parse_wave_config_output(raw_output: str) -> dict[str, list[dict[str, Any]]]:
    cleaned_output = _clean_wave_config_output(raw_output)
    if not cleaned_output:
        raise ValueError("AI command returned empty wave_config output")

    try:
        payload = yaml.safe_load(cleaned_output)
    except yaml.YAMLError as exc:
        trimmed_output = _clean_wave_config_output(_trim_to_wave_config_mapping(cleaned_output))
        if trimmed_output == cleaned_output:
            raise ValueError(f"AI command returned invalid wave_config YAML: {exc}") from exc

        try:
            payload = yaml.safe_load(trimmed_output)
        except yaml.YAMLError as trimmed_exc:
            raise ValueError(f"AI command returned invalid wave_config YAML: {trimmed_exc}") from trimmed_exc

    if isinstance(payload, dict) and isinstance(payload.get("wave_config"), dict):
        payload = payload["wave_config"]

    if not isinstance(payload, dict):
        raise ValueError("AI command must return a YAML mapping of wave numbers to artifact lists")

    try:
        artifacts = _load_wave_artifacts({"wave_config": payload})
    except ValueError as exc:
        trimmed_output = _clean_wave_config_output(_trim_to_wave_config_mapping(cleaned_output))
        if trimmed_output == cleaned_output:
            raise

        try:
            trimmed_payload = yaml.safe_load(trimmed_output)
        except yaml.YAMLError as trimmed_exc:
            raise ValueError(f"AI command returned invalid wave_config YAML: {trimmed_exc}") from trimmed_exc

        if isinstance(trimmed_payload, dict) and isinstance(trimmed_payload.get("wave_config"), dict):
            trimmed_payload = trimmed_payload["wave_config"]
        if not isinstance(trimmed_payload, dict):
            raise ValueError("AI command must return a YAML mapping of wave numbers to artifact lists") from exc

        artifacts = _load_wave_artifacts({"wave_config": trimmed_payload})

    return _serialize_wave_config(artifacts)


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    fenced = re.match(r"^```(?:yaml|yml)?\s*\n(?P<body>.*)\n```$", stripped, re.DOTALL)
    if fenced:
        return fenced.group("body")
    return stripped


def _clean_wave_config_output(text: str) -> str:
    stripped = _strip_code_fences(text).strip()
    lines = [line for line in stripped.splitlines() if not re.match(r"^\s*```(?:yaml|yml)?\s*$", line)]
    return "\n".join(lines).strip()


def _trim_to_wave_config_mapping(text: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if re.match(r'^\s*(?:wave_config|["\']?\d+["\']?)\s*:\s*(?:#.*)?$', line):
            return "\n".join(lines[index:]).strip()
    return text


def _serialize_wave_config(artifacts: list[WaveArtifact]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for artifact in artifacts:
        entry: dict[str, Any] = {
            "node_id": artifact.node_id,
            "output": artifact.output,
            "title": artifact.title,
        }
        if artifact.depends_on:
            entry["depends_on"] = artifact.depends_on
        if artifact.conventions:
            entry["conventions"] = artifact.conventions
        grouped[str(artifact.wave)].append(entry)

    return {wave: grouped[wave] for wave in sorted(grouped, key=int)}


def _group_validation_errors(issues) -> dict[str, list[str]]:
    errors_by_location: dict[str, list[str]] = defaultdict(list)
    for issue in issues:
        if issue.level != STATUS_ERROR:
            continue
        errors_by_location[issue.location].append(issue.message)
    return dict(errors_by_location)


def _index_external_nodes(
    project_root: Path,
    config: dict,
    errors_by_location: dict[str, list[str]],
    planned_nodes: set[str],
) -> dict[str, _ExternalNode]:
    nodes: dict[str, _ExternalNode] = {}

    for doc_path in _iter_doc_files(project_root, config):
        relative_path = doc_path.relative_to(project_root).as_posix()
        parsed = _parse_codd_frontmatter(doc_path)
        if parsed.error:
            continue

        codd = parsed.codd or {}
        node_id = codd.get("node_id")
        if not isinstance(node_id, str) or node_id in planned_nodes:
            continue

        status = STATUS_ERROR if errors_by_location.get(relative_path) else STATUS_DONE
        nodes[node_id] = _ExternalNode(path=relative_path, status=status)

    return nodes


def _dependency_ids(artifact: WaveArtifact) -> list[str]:
    return [entry["id"] for entry in artifact.depends_on]


def _dependency_status(
    dependency_id: str,
    planned_nodes: dict[str, PlannedArtifact],
    external_nodes: dict[str, _ExternalNode],
) -> str:
    if dependency_id.startswith("req:"):
        return STATUS_DONE
    if dependency_id in planned_nodes:
        return planned_nodes[dependency_id].status
    if dependency_id in external_nodes:
        return external_nodes[dependency_id].status
    return STATUS_BLOCKED


def _wave_status(nodes: list[PlannedArtifact]) -> str:
    statuses = {node.status for node in nodes}
    if STATUS_ERROR in statuses:
        return STATUS_ERROR
    if statuses == {STATUS_DONE}:
        return STATUS_DONE
    if STATUS_READY in statuses:
        return STATUS_READY
    return STATUS_BLOCKED


def _topological_order(artifacts: list[WaveArtifact]) -> list[str]:
    artifacts_by_node = {artifact.node_id: artifact for artifact in artifacts}
    indegree = {artifact.node_id: 0 for artifact in artifacts}
    adjacency = {artifact.node_id: set() for artifact in artifacts}

    for artifact in artifacts:
        for dependency_id in _dependency_ids(artifact):
            if dependency_id not in indegree:
                continue
            if artifact.node_id in adjacency[dependency_id]:
                continue
            adjacency[dependency_id].add(artifact.node_id)
            indegree[artifact.node_id] += 1

    ready = sorted(
        [node_id for node_id, degree in indegree.items() if degree == 0],
        key=lambda node_id: (artifacts_by_node[node_id].wave, node_id),
    )
    order: list[str] = []

    while ready:
        node_id = ready.pop(0)
        order.append(node_id)

        for child_id in sorted(adjacency[node_id], key=lambda child: (artifacts_by_node[child].wave, child)):
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                ready.append(child_id)
        ready.sort(key=lambda candidate: (artifacts_by_node[candidate].wave, candidate))

    if len(order) != len(artifacts):
        cycle_nodes = sorted(node_id for node_id, degree in indegree.items() if degree > 0)
        raise ValueError(f"wave_config contains a dependency cycle: {', '.join(cycle_nodes)}")

    return order
