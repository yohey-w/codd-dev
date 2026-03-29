"""Tests for codd plan."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest
import yaml
from click.testing import CliRunner

import codd.planner as planner_module
from codd.cli import main
from codd.planner import STATUS_BLOCKED, STATUS_DONE, STATUS_ERROR, STATUS_READY, build_plan


WAVE_CONFIG = {
    "1": [
        {
            "node_id": "design:acceptance-criteria",
            "output": "docs/test/acceptance_criteria.md",
            "title": "Acceptance Criteria",
            "depends_on": [{"id": "req:project-requirements", "relation": "derives_from"}],
        },
        {
            "node_id": "governance:decisions",
            "output": "docs/governance/decisions.md",
            "title": "Decisions",
            "depends_on": [{"id": "req:project-requirements", "relation": "derives_from"}],
        },
    ],
    "2": [
        {
            "node_id": "design:system-design",
            "output": "docs/design/system_design.md",
            "title": "System Design",
            "depends_on": [
                {"id": "design:acceptance-criteria", "relation": "constrained_by"},
                {"id": "governance:decisions", "relation": "informed_by"},
            ],
        }
    ],
}

BASE_CONFIG = {
    "version": "0.1.0",
    "project": {"name": "test-project", "language": "python"},
    "ai_command": "mock-ai --print",
    "scan": {
        "source_dirs": [],
        "test_dirs": [],
        "doc_dirs": [
            "docs/requirements/",
            "docs/design/",
            "docs/detailed_design/",
            "docs/governance/",
            "docs/test/",
        ],
        "config_files": [],
        "exclude": [],
    },
    "graph": {"store": "jsonl", "path": "codd/scan"},
    "bands": {
        "green": {"min_confidence": 0.90, "min_evidence_count": 2},
        "amber": {"min_confidence": 0.50},
    },
    "propagation": {"max_depth": 10},
    "wave_config": WAVE_CONFIG,
}


PLAN_INIT_AI_OUTPUT = """```yaml
"1":
  - node_id: "design:acceptance-criteria"
    output: "docs/test/acceptance_criteria.md"
    title: "Acceptance Criteria"
    depends_on:
      - id: "req:project-requirements"
        relation: "derives_from"
    conventions:
      - targets:
          - "db:rls_policies"
          - "module:auth"
        reason: "Tenant isolation and authentication are release-blocking constraints."
  - node_id: "governance:decisions"
    output: "docs/governance/decisions.md"
    title: "Decisions"
    depends_on:
      - id: "req:project-requirements"
        relation: "derives_from"
    conventions:
      - targets:
          - "policy:privacy"
        reason: "Privacy obligations must be captured before release."
"2":
  - node_id: "design:system-design"
    output: "docs/design/system_design.md"
    title: "System Design"
    depends_on:
      - id: "design:acceptance-criteria"
        relation: "constrained_by"
      - id: "governance:decisions"
        relation: "informed_by"
    conventions:
      - targets:
          - "db:rls_policies"
          - "service:auth"
        reason: "Security and compliance constraints must be implemented explicitly."
```"""


def _setup_project(tmp_path: Path, *, include_wave_config: bool = True) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    config = deepcopy(BASE_CONFIG)
    if not include_wave_config:
        config.pop("wave_config", None)
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return project


def _write_doc(
    project: Path,
    relative_path: str,
    *,
    node_id: str,
    doc_type: str,
    depends_on: list[dict] | None = None,
    depended_by: list[dict] | None = None,
):
    doc_path = project / relative_path
    doc_path.parent.mkdir(parents=True, exist_ok=True)

    codd = {"node_id": node_id, "type": doc_type}
    if depends_on is not None:
        codd["depends_on"] = depends_on
    if depended_by is not None:
        codd["depended_by"] = depended_by

    frontmatter = yaml.safe_dump({"codd": codd}, sort_keys=False, allow_unicode=True)
    doc_path.write_text(f"---\n{frontmatter}---\n\n# {node_id}\n", encoding="utf-8")


def _write_requirement(project: Path):
    _write_doc(
        project,
        "docs/requirements/requirements.md",
        node_id="req:project-requirements",
        doc_type="requirement",
    )


def _plan_by_node(project: Path):
    plan = build_plan(project)
    nodes = {node.node_id: node for wave in plan.waves for node in wave.nodes}
    return plan, nodes


@pytest.fixture
def mock_plan_init_ai(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_run(command, *, input, capture_output, text, check):
        calls.append(
            {
                "command": command,
                "input": input,
                "capture_output": capture_output,
                "text": text,
                "check": check,
            }
        )
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=PLAN_INIT_AI_OUTPUT,
            stderr="",
        )

    monkeypatch.setattr(planner_module.generator_module.subprocess, "run", fake_run)
    return calls


def test_plan_marks_only_first_wave_ready_when_no_outputs_exist(tmp_path):
    project = _setup_project(tmp_path)
    _write_requirement(project)

    plan, nodes = _plan_by_node(project)

    assert plan.summary == {"done": 0, "ready": 2, "blocked": 1, "error": 0}
    assert plan.next_wave == 1
    assert plan.waves[0].status == STATUS_READY
    assert nodes["design:acceptance-criteria"].status == STATUS_READY
    assert nodes["governance:decisions"].status == STATUS_READY
    assert nodes["design:system-design"].status == STATUS_BLOCKED
    assert set(nodes["design:system-design"].blocked_by) == {
        "design:acceptance-criteria",
        "governance:decisions",
    }


def test_plan_init_prompt_mentions_detailed_design_wave(tmp_path, mock_plan_init_ai):
    project = _setup_project(tmp_path, include_wave_config=False)
    _write_requirement(project)

    planner_module.plan_init(project)

    prompt = mock_plan_init_ai[0]["input"]
    assert "docs/detailed_design/" in prompt
    assert "Insert a dedicated detailed design wave" in prompt
    assert "Markdown + Mermaid" in prompt
    assert "shared domain ownership" in prompt


def test_plan_init_accepts_detailed_design_artifacts_from_ai(tmp_path, monkeypatch):
    project = _setup_project(tmp_path, include_wave_config=False)
    _write_requirement(project)

    detailed_design_output = """\
"1":
  - node_id: "design:acceptance-criteria"
    output: "docs/test/acceptance_criteria.md"
    title: "Acceptance Criteria"
    depends_on:
      - id: "req:project-requirements"
        relation: "derives_from"
    conventions: []
"2":
  - node_id: "design:system-design"
    output: "docs/design/system_design.md"
    title: "System Design"
    depends_on:
      - id: "design:acceptance-criteria"
        relation: "constrained_by"
    conventions: []
"3":
  - node_id: "design:shared-domain-model"
    output: "docs/detailed_design/shared_domain_model.md"
    title: "Shared Domain Model"
    depends_on:
      - id: "design:system-design"
        relation: "depends_on"
        semantic: "technical"
    conventions:
      - targets:
          - "module:auth"
          - "db:rls_policies"
        reason: "Canonical ownership of shared types must be decided before implementation."
"4":
  - node_id: "plan:implementation-plan"
    output: "docs/plan/implementation_plan.md"
    title: "Implementation Plan"
    depends_on:
      - id: "design:shared-domain-model"
        relation: "depends_on"
        semantic: "technical"
    conventions: []
"""

    def fake_run(command, *, input, capture_output, text, check):
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=detailed_design_output,
            stderr="",
        )

    monkeypatch.setattr(planner_module.generator_module.subprocess, "run", fake_run)

    result = planner_module.plan_init(project)

    assert result.wave_config["3"][0]["output"] == "docs/detailed_design/shared_domain_model.md"
    written = yaml.safe_load((project / "codd" / "codd.yaml").read_text(encoding="utf-8"))
    assert written["wave_config"]["3"][0]["node_id"] == "design:shared-domain-model"


def test_plan_marks_next_wave_ready_when_previous_wave_is_done(tmp_path):
    project = _setup_project(tmp_path)
    _write_requirement(project)
    _write_doc(
        project,
        "docs/test/acceptance_criteria.md",
        node_id="design:acceptance-criteria",
        doc_type="test",
        depends_on=[{"id": "req:project-requirements", "relation": "derives_from"}],
    )
    _write_doc(
        project,
        "docs/governance/decisions.md",
        node_id="governance:decisions",
        doc_type="governance",
        depends_on=[{"id": "req:project-requirements", "relation": "derives_from"}],
    )

    plan, nodes = _plan_by_node(project)

    assert plan.summary == {"done": 2, "ready": 1, "blocked": 0, "error": 0}
    assert plan.next_wave == 2
    assert plan.waves[0].status == STATUS_DONE
    assert plan.waves[1].status == STATUS_READY
    assert nodes["design:acceptance-criteria"].status == STATUS_DONE
    assert nodes["governance:decisions"].status == STATUS_DONE
    assert nodes["design:system-design"].status == STATUS_READY


def test_plan_keeps_next_wave_blocked_until_all_dependencies_are_done(tmp_path):
    project = _setup_project(tmp_path)
    _write_requirement(project)
    _write_doc(
        project,
        "docs/test/acceptance_criteria.md",
        node_id="design:acceptance-criteria",
        doc_type="test",
        depends_on=[{"id": "req:project-requirements", "relation": "derives_from"}],
    )

    plan, nodes = _plan_by_node(project)

    assert plan.summary == {"done": 1, "ready": 1, "blocked": 1, "error": 0}
    assert nodes["design:acceptance-criteria"].status == STATUS_DONE
    assert nodes["governance:decisions"].status == STATUS_READY
    assert nodes["design:system-design"].status == STATUS_BLOCKED
    assert nodes["design:system-design"].blocked_by == ["governance:decisions"]


def test_plan_marks_all_waves_done_when_all_outputs_validate(tmp_path):
    project = _setup_project(tmp_path)
    _write_requirement(project)
    _write_doc(
        project,
        "docs/test/acceptance_criteria.md",
        node_id="design:acceptance-criteria",
        doc_type="test",
        depends_on=[{"id": "req:project-requirements", "relation": "derives_from"}],
    )
    _write_doc(
        project,
        "docs/governance/decisions.md",
        node_id="governance:decisions",
        doc_type="governance",
        depends_on=[{"id": "req:project-requirements", "relation": "derives_from"}],
    )
    _write_doc(
        project,
        "docs/design/system_design.md",
        node_id="design:system-design",
        doc_type="design",
        depends_on=[
            {"id": "design:acceptance-criteria", "relation": "constrained_by"},
            {"id": "governance:decisions", "relation": "informed_by"},
        ],
    )

    plan, nodes = _plan_by_node(project)

    assert plan.summary == {"done": 3, "ready": 0, "blocked": 0, "error": 0}
    assert plan.next_wave is None
    assert all(node.status == STATUS_DONE for node in nodes.values())
    assert [wave.status for wave in plan.waves] == [STATUS_DONE, STATUS_DONE]


def test_plan_keeps_wave_config_forward_reference_out_of_error_state(tmp_path):
    project = _setup_project(tmp_path)
    _write_requirement(project)
    _write_doc(
        project,
        "docs/test/acceptance_criteria.md",
        node_id="design:acceptance-criteria",
        doc_type="test",
        depends_on=[{"id": "req:project-requirements", "relation": "derives_from"}],
        depended_by=[{"id": "design:system-design", "relation": "validates"}],
    )

    plan, nodes = _plan_by_node(project)

    assert plan.summary == {"done": 1, "ready": 1, "blocked": 1, "error": 0}
    assert nodes["design:acceptance-criteria"].status == STATUS_DONE
    assert nodes["design:acceptance-criteria"].validation_errors == []
    assert nodes["design:system-design"].status == STATUS_BLOCKED
    assert nodes["design:system-design"].blocked_by == ["governance:decisions"]


def test_plan_still_marks_existing_invalid_artifact_as_error_for_unknown_reference(tmp_path):
    project = _setup_project(tmp_path)
    _write_requirement(project)
    _write_doc(
        project,
        "docs/test/acceptance_criteria.md",
        node_id="design:acceptance-criteria",
        doc_type="test",
        depends_on=[{"id": "req:project-requirements", "relation": "derives_from"}],
        depended_by=[{"id": "design:missing-design", "relation": "validates"}],
    )

    plan, nodes = _plan_by_node(project)

    assert plan.summary == {"done": 0, "ready": 1, "blocked": 1, "error": 1}
    assert nodes["design:acceptance-criteria"].status == STATUS_ERROR
    assert any(
        "undefined node 'design:missing-design'" in message
        for message in nodes["design:acceptance-criteria"].validation_errors
    )


def test_plan_command_json_output_has_expected_schema(tmp_path):
    project = _setup_project(tmp_path)
    _write_requirement(project)
    runner = CliRunner()

    result = runner.invoke(main, ["plan", "--path", str(project), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["project_root"] == str(project.resolve())
    assert payload["summary"] == {"done": 0, "ready": 2, "blocked": 1, "error": 0}
    assert payload["next_wave"] == 1
    assert [wave["wave"] for wave in payload["waves"]] == [1, 2]
    assert payload["waves"][0]["nodes"][0]["node_id"] == "design:acceptance-criteria"
    assert payload["waves"][0]["nodes"][0]["status"] == STATUS_READY
    assert payload["waves"][1]["nodes"][0]["blocked_by"] == [
        "design:acceptance-criteria",
        "governance:decisions",
    ]


def test_plan_command_init_generates_wave_config_from_requirement_docs(tmp_path, mock_plan_init_ai):
    project = _setup_project(tmp_path, include_wave_config=False)
    _write_requirement(project)
    runner = CliRunner()

    result = runner.invoke(main, ["plan", "--init", "--path", str(project)])

    assert result.exit_code == 0
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text(encoding="utf-8"))
    assert sorted(config["wave_config"]) == ["1", "2"]
    assert config["wave_config"]["1"][0]["node_id"] == "design:acceptance-criteria"
    assert config["wave_config"]["1"][0]["depends_on"][0]["semantic"] == "governance"
    assert config["wave_config"]["1"][0]["conventions"][0]["targets"] == ["db:rls_policies", "module:auth"]
    assert config["wave_config"]["2"][0]["depends_on"][0]["relation"] == "constrained_by"
    assert mock_plan_init_ai[0]["command"] == ["mock-ai", "--print"]
    assert "MECE Document Structure (7 categories):" in mock_plan_init_ai[0]["input"]
    assert "Typical wave patterns:" in mock_plan_init_ai[0]["input"]
    assert "docs/detailed_design/" in mock_plan_init_ai[0]["input"]
    assert "release-blocking constraints" in mock_plan_init_ai[0]["input"]
    assert "security constraints (tenant isolation, authentication, authorization, auditability)" in mock_plan_init_ai[0]["input"]
    assert '"conventions": [{"targets": ["node:id"], "reason": "release-blocking constraint"}]' in mock_plan_init_ai[0]["input"]
    assert "req:project-requirements" in mock_plan_init_ai[0]["input"]
    assert "Initialized wave_config in codd/codd.yaml" in result.output


def test_parse_wave_config_output_ignores_leading_summary_before_yaml():
    raw_output = """Key conventions extracted:
- Tenant isolation is mandatory.

"1":
  - node_id: "design:acceptance-criteria"
    output: "docs/test/acceptance_criteria.md"
    title: "Acceptance Criteria"
    depends_on:
      - id: "req:project-requirements"
        relation: "derives_from"
    conventions:
      - targets:
          - "db:rls_policies"
        reason: "Tenant isolation remains release-blocking."
"""

    payload = planner_module._parse_wave_config_output(raw_output)

    assert payload["1"][0]["node_id"] == "design:acceptance-criteria"
    assert payload["1"][0]["conventions"][0]["targets"] == ["db:rls_policies"]


def test_parse_wave_config_output_ignores_summary_and_trailing_code_fence():
    raw_output = """Key conventions extracted:
- Tenant isolation is mandatory.

```yaml
"1":
  - node_id: "design:acceptance-criteria"
    output: "docs/test/acceptance_criteria.md"
    title: "Acceptance Criteria"
    depends_on:
      - id: "req:project-requirements"
        relation: "derives_from"
    conventions:
      - targets:
          - "db:rls_policies"
        reason: "Tenant isolation remains release-blocking."
```
"""

    payload = planner_module._parse_wave_config_output(raw_output)

    assert payload["1"][0]["node_id"] == "design:acceptance-criteria"
    assert payload["1"][0]["conventions"][0]["targets"] == ["db:rls_policies"]


def test_plan_command_init_prompts_before_overwriting_existing_wave_config(tmp_path, mock_plan_init_ai):
    project = _setup_project(tmp_path)
    _write_requirement(project)
    original = (project / "codd" / "codd.yaml").read_text(encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(main, ["plan", "--init", "--path", str(project)], input="n\n")

    assert result.exit_code == 1
    assert "Overwrite it?" in result.output
    assert "Aborted: existing wave_config preserved." in result.output
    assert (project / "codd" / "codd.yaml").read_text(encoding="utf-8") == original
    assert mock_plan_init_ai == []


def test_plan_command_init_force_overwrites_existing_wave_config(tmp_path, mock_plan_init_ai):
    project = _setup_project(tmp_path)
    _write_requirement(project)
    runner = CliRunner()

    result = runner.invoke(main, ["plan", "--init", "--path", str(project), "--force"])

    assert result.exit_code == 0
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text(encoding="utf-8"))
    assert config["wave_config"]["2"][0]["node_id"] == "design:system-design"
    assert config["wave_config"]["2"][0]["depends_on"][1]["relation"] == "informed_by"
    assert mock_plan_init_ai[0]["command"] == ["mock-ai", "--print"]
