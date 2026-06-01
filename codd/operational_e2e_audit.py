"""Adapter-neutral audit for operational E2E coverage."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from codd.claude_cli import DEFAULT_CLAUDE_EFFORT, DEFAULT_CLAUDE_MODEL
from codd.e2e_extractor import ScenarioCollection, ScenarioExtractor, UserScenario
from codd.e2e_generator import load_scenarios_from_markdown


SUPPORTED_RUNNER_BACKENDS = (
    "local-playwright",
    "ci-shard",
    "agent-workflow",
    "claude-dynamic-workflow",
)
AUDIT_CONTRACT_VERSION = "operational-e2e-audit/v1"

_COVER_MARKER_RE = re.compile(
    r"codd:\s*covers\s+"
    r"(?:operation|source_operation)\s*=\s*(?P<operation>[^\s]+)\s+"
    r"(?:axis|coverage_axis)\s*=\s*(?P<axis>[A-Za-z0-9_.:-]+)",
    re.IGNORECASE,
)
_TEST_SUFFIXES = (
    ".spec.ts",
    ".test.ts",
    ".spec.tsx",
    ".test.tsx",
    ".spec.js",
    ".test.js",
    ".spec.jsx",
    ".test.jsx",
    ".cy.ts",
    ".cy.js",
    ".py",
)


@dataclass(frozen=True)
class TestEvidence:
    """A test file claim that can cover one operational scenario."""

    path: str
    operation: str
    axis: str


@dataclass
class ScenarioAuditRow:
    """Audit row for one operational scenario."""

    scenario_name: str
    kind: str
    actor: str
    coverage_axis: str
    source_operation: str
    risk_level: str
    coverage_status: str
    matched_tests: list[str] = field(default_factory=list)
    heuristic_matches: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)
    suggested_next_action: str = ""


@dataclass
class OperationalE2EAuditReport:
    """Serializable adapter-neutral operational E2E audit report."""

    version: str
    runner_backend: str
    summary: dict[str, int | str]
    runner_contract: dict[str, object]
    failure_taxonomy: list[str]
    repair_policy: list[str]
    rows: list[ScenarioAuditRow]


@dataclass
class AgentWorkflowShard:
    """A bounded set of uncovered scenarios suitable for one autonomous worker."""

    shard_id: str
    title: str
    runner_backend: str
    scenario_count: int
    source_operations: list[str]
    scenarios: list[dict[str, str]]
    acceptance_criteria: list[str]
    evidence_outputs: list[str]
    recommended_prompt: str


@dataclass
class AgentWorkflowPlan:
    """Adapter-neutral work plan for parallel E2E evidence collection and repair."""

    version: str
    audit_version: str
    runner_backend: str
    summary: dict[str, int | str]
    shard_policy: dict[str, int | str]
    runner_invocation: dict[str, object]
    shards: list[AgentWorkflowShard]


def build_operational_e2e_audit(
    project_root: Path | str,
    *,
    scenarios_path: Path | str | None = None,
    test_dirs: Iterable[Path | str] | None = None,
    runner_backend: str = "local-playwright",
) -> OperationalE2EAuditReport:
    """Build an operational E2E coverage audit without invoking a vendor agent."""
    project_root = Path(project_root).resolve()
    backend = _normalize_runner_backend(runner_backend)
    collection = _load_or_extract_operational_scenarios(project_root, scenarios_path=scenarios_path)
    evidence = _scan_test_evidence(project_root, test_dirs=test_dirs)
    heuristic_index = _scan_heuristic_text_matches(project_root, collection.scenarios, test_dirs=test_dirs)
    rows = [_audit_scenario(scenario, evidence, heuristic_index) for scenario in collection.scenarios]
    summary = _summarize(rows, runner_backend=backend)

    return OperationalE2EAuditReport(
        version=AUDIT_CONTRACT_VERSION,
        runner_backend=backend,
        summary=summary,
        runner_contract=_runner_contract(backend),
        failure_taxonomy=[
            "spec_gap",
            "implementation_defect",
            "test_data_or_seed_defect",
            "test_automation_defect",
            "environment_or_external_service",
            "flaky_or_timing",
        ],
        repair_policy=[
            "Run the selected E2E campaign to completion before repairing individual failures.",
            "Group failures by shared root cause before editing source code.",
            "Treat skipped tests, missing markers, and helper-only assertions as incomplete evidence.",
            "Verify each repaired group with the smallest relevant test first, then rerun the selected suite.",
        ],
        rows=rows,
    )


def build_agent_workflow_plan(
    project_root: Path | str,
    *,
    scenarios_path: Path | str | None = None,
    test_dirs: Iterable[Path | str] | None = None,
    runner_backend: str = "agent-workflow",
    max_scenarios_per_shard: int = 6,
    claude_dangerously_skip_permissions: bool = True,
) -> AgentWorkflowPlan:
    """Create bounded parallel work shards from uncovered operational E2E rows."""
    if max_scenarios_per_shard < 1:
        raise ValueError("max_scenarios_per_shard must be at least 1")

    report = build_operational_e2e_audit(
        project_root,
        scenarios_path=scenarios_path,
        test_dirs=test_dirs,
        runner_backend=runner_backend,
    )
    candidate_rows = [row for row in report.rows if row.coverage_status != "covered_by_e2e"]
    grouped = _group_rows_by_operation(candidate_rows)
    shards: list[AgentWorkflowShard] = []
    for operation_id, rows in grouped.items():
        for chunk in _chunk_rows(rows, max_scenarios_per_shard):
            shard_no = len(shards) + 1
            shards.append(
                AgentWorkflowShard(
                    shard_id=f"e2e-shard-{shard_no:03d}",
                    title=_shard_title(operation_id, chunk),
                    runner_backend=report.runner_backend,
                    scenario_count=len(chunk),
                    source_operations=sorted({row.source_operation for row in chunk}),
                    scenarios=[_scenario_payload(row) for row in chunk],
                    acceptance_criteria=[
                        "All listed scenarios are either covered by an explicit codd covers marker or classified with a concrete blocker.",
                        "Any implemented repair is verified with the smallest relevant test command before broader suite rerun.",
                        "Failure artifacts, commands, and root-cause classification use the audit failure taxonomy.",
                        "Unrelated source changes are left untouched.",
                    ],
                    evidence_outputs=[
                        "changed file list, if any",
                        "test command output summary",
                        "artifact paths for failures or screenshots, if any",
                        "remaining blocker list with taxonomy labels",
                    ],
                    recommended_prompt=_recommended_agent_prompt(chunk),
                )
            )

    return AgentWorkflowPlan(
        version="agent-workflow-plan/v1",
        audit_version=report.version,
        runner_backend=report.runner_backend,
        summary={
            **report.summary,
            "workflow_shards": len(shards),
            "workflow_candidate_scenarios": len(candidate_rows),
        },
        shard_policy={
            "grouping": "operation_id",
            "max_scenarios_per_shard": max_scenarios_per_shard,
            "coverage_gate": "explicit_codd_covers_marker_or_blocker_classification",
        },
        runner_invocation=_runner_invocation(
            report.runner_backend,
            claude_dangerously_skip_permissions=claude_dangerously_skip_permissions,
        ),
        shards=shards,
    )


def write_operational_e2e_audit(
    report: OperationalE2EAuditReport,
    output_path: Path | str,
    *,
    output_format: str = "md",
) -> Path:
    """Write an audit report as Markdown or JSON."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "json":
        payload = asdict(report)
        output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    elif output_format == "md":
        output_path.write_text(render_operational_e2e_audit_markdown(report), encoding="utf-8")
    else:
        raise ValueError(f"Unsupported operational E2E audit format: {output_format}")
    return output_path


def write_agent_workflow_plan(
    plan: AgentWorkflowPlan,
    output_path: Path | str,
    *,
    output_format: str = "json",
) -> Path:
    """Write an agent workflow plan as JSON or Markdown."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "json":
        output_path.write_text(json.dumps(asdict(plan), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    elif output_format == "md":
        output_path.write_text(render_agent_workflow_plan_markdown(plan), encoding="utf-8")
    else:
        raise ValueError(f"Unsupported agent workflow plan format: {output_format}")
    return output_path


def render_operational_e2e_audit_markdown(report: OperationalE2EAuditReport) -> str:
    """Render the operational E2E audit report as Markdown."""
    lines = [
        "# Operational E2E Audit",
        "",
        f"- Contract: {report.version}",
        f"- Runner backend: {report.runner_backend}",
        f"- Scenarios: {report.summary['scenario_count']}",
        f"- Covered by E2E marker: {report.summary['covered_by_e2e']}",
        f"- Heuristic matches needing marker review: {report.summary['heuristic_matches']}",
        f"- Uncovered: {report.summary['uncovered']}",
        "",
        "## Runner Contract",
    ]
    for item in report.runner_contract["core_responsibilities"]:
        lines.append(f"- {item}")
    lines.extend(["", "## Adapter Boundary"])
    lines.append(str(report.runner_contract["adapter_boundary"]))
    lines.extend(["", "## Failure Taxonomy"])
    lines.extend(f"- {item}" for item in report.failure_taxonomy)
    lines.extend(["", "## Repair Policy"])
    lines.extend(f"- {item}" for item in report.repair_policy)
    lines.extend(["", "## Scenario Matrix"])
    lines.append("| Scenario | Actor | Axis | Source Operation | Status | Evidence | Next Action |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for row in report.rows:
        evidence = ", ".join(row.matched_tests) if row.matched_tests else ""
        if row.heuristic_matches:
            heuristic = ", ".join(row.heuristic_matches[:3])
            if len(row.heuristic_matches) > 3:
                heuristic += ", ..."
            evidence = f"heuristic: {heuristic}" if not evidence else f"{evidence}; heuristic: {heuristic}"
        lines.append(
            "| "
            + " | ".join(
                [
                    _md_cell(row.scenario_name),
                    _md_cell(row.actor),
                    _md_cell(row.coverage_axis),
                    _md_cell(row.source_operation),
                    _md_cell(row.coverage_status),
                    _md_cell(evidence or "-"),
                    _md_cell(row.suggested_next_action),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def render_agent_workflow_plan_markdown(plan: AgentWorkflowPlan) -> str:
    """Render an agent workflow plan for humans or orchestration systems."""
    lines = [
        "# Agent Workflow E2E Plan",
        "",
        f"- Contract: {plan.version}",
        f"- Audit contract: {plan.audit_version}",
        f"- Runner backend: {plan.runner_backend}",
        f"- Candidate scenarios: {plan.summary['workflow_candidate_scenarios']}",
        f"- Shards: {plan.summary['workflow_shards']}",
        f"- Max scenarios per shard: {plan.shard_policy['max_scenarios_per_shard']}",
        "",
        "## Runner Invocation",
        "",
        f"- Command prefix: `{plan.runner_invocation.get('command_prefix', '')}`",
        f"- Dangerous skip permissions: {str(plan.runner_invocation.get('dangerous_skip_permissions', False)).lower()}",
        f"- Safety note: {plan.runner_invocation.get('safety_note', '')}",
        "",
        "## Shards",
    ]
    for shard in plan.shards:
        lines.extend(
            [
                "",
                f"### {shard.shard_id}: {shard.title}",
                "",
                f"- Scenarios: {shard.scenario_count}",
                f"- Source operations: {', '.join(shard.source_operations)}",
                "",
                "Acceptance criteria:",
            ]
        )
        lines.extend(f"- {item}" for item in shard.acceptance_criteria)
        lines.extend(["", "Prompt:", "", "```text", shard.recommended_prompt, "```"])
    lines.append("")
    return "\n".join(lines)


def _load_or_extract_operational_scenarios(
    project_root: Path,
    *,
    scenarios_path: Path | str | None,
) -> ScenarioCollection:
    if scenarios_path is not None:
        path = Path(scenarios_path)
        if not path.is_absolute():
            path = project_root / path
        return load_scenarios_from_markdown(path)

    default_path = project_root / "docs" / "e2e" / "operational-scenarios.md"
    if default_path.exists():
        return load_scenarios_from_markdown(default_path)
    return ScenarioExtractor(project_root).extract_operational()


def _scan_test_evidence(
    project_root: Path,
    *,
    test_dirs: Iterable[Path | str] | None,
) -> list[TestEvidence]:
    evidence: list[TestEvidence] = []
    for path in _iter_test_files(project_root, test_dirs=test_dirs):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel_path = _rel_path(path, project_root)
        for match in _COVER_MARKER_RE.finditer(text):
            evidence.append(
                TestEvidence(
                    path=rel_path,
                    operation=match.group("operation").strip(),
                    axis=match.group("axis").strip(),
                )
            )
    return evidence


def _scan_heuristic_text_matches(
    project_root: Path,
    scenarios: list[UserScenario],
    *,
    test_dirs: Iterable[Path | str] | None,
) -> dict[tuple[str, str], list[str]]:
    needed = {
        (_operation_key(scenario), scenario.coverage_axis or "")
        for scenario in scenarios
        if scenario.operation_id and scenario.coverage_axis
    }
    if not needed:
        return {}

    index: dict[tuple[str, str], list[str]] = {key: [] for key in needed}
    for path in _iter_test_files(project_root, test_dirs=test_dirs):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel_path = _rel_path(path, project_root)
        for operation_key, axis in needed:
            operation_id = operation_key.rsplit("#", 1)[-1]
            if operation_id in text and axis in text:
                index[(operation_key, axis)].append(rel_path)
    return {key: paths for key, paths in index.items() if paths}


def _audit_scenario(
    scenario: UserScenario,
    evidence: list[TestEvidence],
    heuristic_index: dict[tuple[str, str], list[str]],
) -> ScenarioAuditRow:
    operation_key = _operation_key(scenario)
    axis = scenario.coverage_axis or "unspecified"
    matched = sorted(
        {
            item.path
            for item in evidence
            if item.axis == axis and _operation_matches(item.operation, operation_key, scenario.operation_id)
        }
    )
    heuristic = sorted(set(heuristic_index.get((operation_key, axis), [])) - set(matched))
    covered = bool(matched)
    status = "covered_by_e2e" if covered else "uncovered"
    next_action = _next_action(status=status, heuristic_matches=heuristic)
    return ScenarioAuditRow(
        scenario_name=scenario.name,
        kind=scenario.kind,
        actor=scenario.actor or "unspecified",
        coverage_axis=axis,
        source_operation=operation_key,
        risk_level=_risk_level(scenario.priority),
        coverage_status=status,
        matched_tests=matched,
        heuristic_matches=heuristic,
        required_evidence=[
            "actor-facing public trigger",
            "durable readback or downstream reflection",
            "scenario-owned or idempotently reset state",
        ],
        suggested_next_action=next_action,
    )


def _iter_test_files(project_root: Path, *, test_dirs: Iterable[Path | str] | None) -> Iterable[Path]:
    dirs = list(test_dirs or ("tests",))
    for item in dirs:
        root = Path(item)
        if not root.is_absolute():
            root = project_root / root
        if root.is_file() and _is_test_file(root):
            yield root
        elif root.is_dir():
            for path in sorted(root.rglob("*")):
                if path.is_file() and _is_test_file(path):
                    yield path


def _is_test_file(path: Path) -> bool:
    name = path.name
    return any(name.endswith(suffix) for suffix in _TEST_SUFFIXES)


def _operation_key(scenario: UserScenario) -> str:
    source = scenario.source or "unknown"
    operation_id = scenario.operation_id or "unknown"
    return f"{source}#{operation_id}"


def _operation_matches(marker_operation: str, operation_key: str, operation_id: str | None) -> bool:
    return marker_operation == operation_key or bool(operation_id and marker_operation == operation_id)


def _risk_level(priority: str) -> str:
    normalized = (priority or "").strip().lower()
    return {"high": "P0", "medium": "P1", "low": "P2"}.get(normalized, "P1")


def _next_action(*, status: str, heuristic_matches: list[str]) -> str:
    if status == "covered_by_e2e":
        return "Run selected suite and attach the latest artifact."
    if heuristic_matches:
        return "Review assertions, then add an explicit codd covers marker or split the test."
    return "Create or extend an E2E candidate with a codd covers marker."


def _summarize(rows: list[ScenarioAuditRow], *, runner_backend: str) -> dict[str, int | str]:
    covered = sum(1 for row in rows if row.coverage_status == "covered_by_e2e")
    heuristic = sum(1 for row in rows if row.heuristic_matches)
    uncovered = sum(1 for row in rows if row.coverage_status == "uncovered")
    return {
        "runner_backend": runner_backend,
        "scenario_count": len(rows),
        "covered_by_e2e": covered,
        "heuristic_matches": heuristic,
        "uncovered": uncovered,
    }


def _runner_contract(runner_backend: str) -> dict[str, object]:
    return {
        "backend": runner_backend,
        "supported_backends": list(SUPPORTED_RUNNER_BACKENDS),
        "core_responsibilities": [
            "derive the operational scenario matrix from project-owned metadata",
            "require explicit codd covers markers before marking E2E evidence as covered",
            "run the whole selected campaign before repair triage",
            "persist failure artifacts and classify them with the shared failure taxonomy",
            "keep CoDD core independent of any specific agent vendor or orchestration product",
        ],
        "adapter_boundary": (
            "CoDD owns the contract, markers, matrix, and taxonomy. Runner adapters may use local "
            "Playwright, CI shards, generic agent workflows, or Claude Dynamic Workflows, but those "
            "adapters must return the same contract-shaped results."
        ),
    }


def _runner_invocation(
    runner_backend: str,
    *,
    claude_dangerously_skip_permissions: bool,
) -> dict[str, object]:
    if runner_backend != "claude-dynamic-workflow":
        return {
            "backend": runner_backend,
            "command_prefix": "",
            "dangerous_skip_permissions": False,
            "safety_note": "Runner invocation is adapter-owned for this backend.",
        }

    command = [
        "claude",
        "-p",
        "--model",
        DEFAULT_CLAUDE_MODEL,
        "--effort",
        DEFAULT_CLAUDE_EFFORT,
    ]
    if claude_dangerously_skip_permissions:
        command.extend([
            "--permission-mode",
            "bypassPermissions",
            "--dangerously-skip-permissions",
        ])

    return {
        "backend": runner_backend,
        "command_prefix": " ".join(command),
        "dangerous_skip_permissions": claude_dangerously_skip_permissions,
        "permission_mode": "bypassPermissions" if claude_dangerously_skip_permissions else "default",
        "safety_note": (
            "Dangerous permission bypass is explicit opt-in. Use only in a trusted workspace "
            "after the operator has approved autonomous edits and test execution."
            if claude_dangerously_skip_permissions
            else "Default Claude permissions remain active. Add the CLI opt-in only for trusted autonomous runs."
        ),
    }


def _normalize_runner_backend(runner_backend: str) -> str:
    backend = (runner_backend or "local-playwright").strip()
    if backend not in SUPPORTED_RUNNER_BACKENDS:
        raise ValueError(f"Unsupported operational E2E runner backend: {runner_backend}")
    return backend


def _rel_path(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def _md_cell(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _group_rows_by_operation(rows: list[ScenarioAuditRow]) -> dict[str, list[ScenarioAuditRow]]:
    grouped: dict[str, list[ScenarioAuditRow]] = {}
    for row in sorted(rows, key=lambda item: (item.source_operation, item.coverage_axis, item.scenario_name)):
        grouped.setdefault(_operation_id_from_key(row.source_operation), []).append(row)
    return grouped


def _chunk_rows(rows: list[ScenarioAuditRow], chunk_size: int) -> Iterable[list[ScenarioAuditRow]]:
    for start in range(0, len(rows), chunk_size):
        yield rows[start : start + chunk_size]


def _shard_title(operation_key: str, rows: list[ScenarioAuditRow]) -> str:
    operation_id = _operation_id_from_key(operation_key)
    axes = ", ".join(sorted({row.coverage_axis for row in rows})[:3])
    return f"{operation_id} ({axes})"


def _scenario_payload(row: ScenarioAuditRow) -> dict[str, str]:
    return {
        "name": row.scenario_name,
        "actor": row.actor,
        "axis": row.coverage_axis,
        "source_operation": row.source_operation,
        "risk_level": row.risk_level,
        "status": row.coverage_status,
        "heuristic_matches": ", ".join(row.heuristic_matches),
        "next_action": row.suggested_next_action,
    }


def _recommended_agent_prompt(rows: list[ScenarioAuditRow]) -> str:
    scenario_lines = "\n".join(
        f"- {row.scenario_name} | actor={row.actor} | axis={row.coverage_axis} | "
        f"source={row.source_operation} | status={row.coverage_status}"
        for row in rows
    )
    return (
        "You are an autonomous E2E evidence and repair agent.\n"
        "Work only on the scenarios listed below. Do not touch unrelated files.\n"
        "Run the selected scenario set to completion before repairing individual failures.\n"
        "For every scenario, either add/verify an explicit marker in the form "
        "`codd: covers operation=<source_operation> axis=<coverage_axis>` or classify the blocker.\n"
        "Use this failure taxonomy: spec_gap, implementation_defect, test_data_or_seed_defect, "
        "test_automation_defect, environment_or_external_service, flaky_or_timing.\n"
        "After any repair, run the smallest relevant test command and report commands, results, "
        "changed files, artifacts, and remaining blockers.\n\n"
        "Scenarios:\n"
        f"{scenario_lines}"
    )


def _operation_id_from_key(operation_key: str) -> str:
    return operation_key.rsplit("#", 1)[-1]
