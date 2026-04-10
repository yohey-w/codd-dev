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
from codd.planner import STATUS_BLOCKED, STATUS_DONE, STATUS_ERROR, STATUS_READY, build_plan, render_plan_text


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

    # Baseline extract is READY when all waves DONE but no extracted docs
    assert plan.baseline_status == STATUS_READY
    text = render_plan_text(plan)
    assert "Baseline Extract: READY" in text
    assert "codd extract" in text

    # After creating extracted docs, baseline becomes DONE
    extracted_dir = project / "codd" / "extracted"
    extracted_dir.mkdir(parents=True)
    (extracted_dir / "system-context.md").write_text("# extracted\n")
    plan2, _ = _plan_by_node(project)
    assert plan2.baseline_status == STATUS_DONE
    text2 = render_plan_text(plan2)
    assert "Baseline Extract: DONE" in text2
    assert "ready for maintenance" in text2


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


def test_wave_config_modules_field_parsed_and_rendered(tmp_path):
    """modules field in wave_config is preserved through load -> render -> serialize."""
    project = _setup_project(tmp_path)
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text(encoding="utf-8"))
    # Add modules to existing wave_config
    config["wave_config"]["1"][0]["modules"] = ["auth", "users"]
    config["wave_config"]["2"][0]["modules"] = ["auth", "api"]
    (project / "codd" / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    from codd.generator import _load_wave_artifacts, _render_document
    from codd.planner import _serialize_wave_config
    artifacts = _load_wave_artifacts(config)

    # Parsed correctly
    ac = next(a for a in artifacts if a.node_id == "design:acceptance-criteria")
    assert ac.modules == ["auth", "users"]

    sd = next(a for a in artifacts if a.node_id == "design:system-design")
    assert sd.modules == ["auth", "api"]

    # Serialization preserves modules
    serialized = _serialize_wave_config(artifacts)
    assert serialized["1"][0]["modules"] == ["auth", "users"]
    assert serialized["2"][0]["modules"] == ["auth", "api"]

    # Render includes modules in frontmatter
    rendered = _render_document(
        artifact=ac,
        global_conventions=[],
        depended_by=[],
        body="# Test\n\n## Overview\n\nContent here.\n",
    )
    assert "modules:" in rendered
    assert "auth" in rendered
    assert "users" in rendered


def test_wave_config_without_modules_field_defaults_to_empty(tmp_path):
    """Existing wave_config without modules field continues to work."""
    project = _setup_project(tmp_path)
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text(encoding="utf-8"))

    from codd.generator import _load_wave_artifacts, _render_document
    from codd.planner import _serialize_wave_config
    artifacts = _load_wave_artifacts(config)

    ac = next(a for a in artifacts if a.node_id == "design:acceptance-criteria")
    assert ac.modules == []

    # Serialization does not include empty modules
    serialized = _serialize_wave_config(artifacts)
    assert "modules" not in serialized["1"][0]

    # Render does not include modules in frontmatter when empty
    rendered = _render_document(
        artifact=ac,
        global_conventions=[],
        depended_by=[],
        body="# Test\n\n## Overview\n\nContent here.\n",
    )
    assert "modules:" not in rendered


def test_plan_init_prompt_includes_modules_field(tmp_path, mock_plan_init_ai):
    """plan_init prompt includes modules in schema documentation."""
    project = _setup_project(tmp_path, include_wave_config=False)
    _write_requirement(project)

    planner_module.plan_init(project)

    prompt = mock_plan_init_ai[0]["input"]
    assert "modules" in prompt
    assert "source modules" in prompt.lower()


# ---------------------------------------------------------------------------
# Brownfield plan_init tests
# ---------------------------------------------------------------------------

BROWNFIELD_AI_OUTPUT = '''```yaml
"1":
  - node_id: "design:acceptance-criteria"
    output: "docs/test/acceptance_criteria.md"
    title: "Acceptance Criteria"
    modules: ["auth", "tasks"]
    depends_on:
      - id: "design:extract:system-context"
        relation: "derives_from"
        semantic: "technical"
    conventions:
      - targets:
          - "module:auth"
        reason: "Authentication is release-blocking."
"2":
  - node_id: "design:system-design"
    output: "docs/design/system_design.md"
    title: "System Design"
    modules: ["auth", "tasks"]
    depends_on:
      - id: "design:acceptance-criteria"
        relation: "constrained_by"
        semantic: "governance"
    conventions: []
```'''


def _write_extracted_docs(project: Path):
    """Create extracted docs for a fictional TaskBoard app."""
    extracted_dir = project / "codd" / "extracted"
    extracted_dir.mkdir(parents=True, exist_ok=True)
    modules_dir = extracted_dir / "modules"
    modules_dir.mkdir(exist_ok=True)

    # System context
    sc_frontmatter = yaml.safe_dump({
        "codd": {
            "node_id": "design:extract:system-context",
            "type": "design",
            "source": "extracted",
            "confidence": 0.65,
            "last_extracted": "2026-03-31",
        }
    }, sort_keys=False)
    (extracted_dir / "system-context.md").write_text(
        f"---\n{sc_frontmatter}---\n\n# TaskBoard System Context\n\n3 modules, 1,200 lines\n",
        encoding="utf-8",
    )

    # Module: auth
    auth_fm = yaml.safe_dump({
        "codd": {
            "node_id": "design:extract:auth",
            "type": "design",
            "source": "extracted",
            "confidence": 0.75,
            "last_extracted": "2026-03-31",
            "source_files": ["src/auth/service.py", "src/auth/models.py"],
        }
    }, sort_keys=False)
    (modules_dir / "auth.md").write_text(
        f"---\n{auth_fm}---\n\n# auth\n\n## Symbol Inventory\n\n| Kind | Name |\n|------|------|\n| class | AuthService |\n| class | User |\n",
        encoding="utf-8",
    )

    # Module: tasks
    tasks_fm = yaml.safe_dump({
        "codd": {
            "node_id": "design:extract:tasks",
            "type": "design",
            "source": "extracted",
            "confidence": 0.70,
            "last_extracted": "2026-03-31",
            "source_files": ["src/tasks/service.py", "src/tasks/models.py"],
        }
    }, sort_keys=False)
    (modules_dir / "tasks.md").write_text(
        f"---\n{tasks_fm}---\n\n# tasks\n\n## Symbol Inventory\n\n| Kind | Name |\n|------|------|\n| class | TaskService |\n| class | Task |\n",
        encoding="utf-8",
    )


@pytest.fixture
def mock_brownfield_ai(monkeypatch):
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
            stdout=BROWNFIELD_AI_OUTPUT,
            stderr="",
        )

    monkeypatch.setattr(planner_module.generator_module.subprocess, "run", fake_run)
    return calls


def test_plan_init_brownfield_uses_extracted_docs(tmp_path, mock_brownfield_ai):
    """plan_init falls back to extracted docs when no requirement docs exist."""
    project = _setup_project(tmp_path, include_wave_config=False)
    _write_extracted_docs(project)

    result = planner_module.plan_init(project)

    # wave_config is created
    assert sorted(result.wave_config) == ["1", "2"]
    assert result.wave_config["1"][0]["node_id"] == "design:acceptance-criteria"
    assert result.wave_config["2"][0]["node_id"] == "design:system-design"

    # prompt contains BROWNFIELD and extracted doc content
    prompt = mock_brownfield_ai[0]["input"]
    assert "BROWNFIELD" in prompt
    assert "--- BEGIN EXTRACTED" in prompt
    assert "--- END EXTRACTED" in prompt

    # requirement_paths contains extracted doc paths
    assert any("extracted" in p for p in result.requirement_paths)
    assert len(result.requirement_paths) == 3  # system-context + auth + tasks


def test_plan_init_brownfield_prompt_includes_extracted_content(tmp_path, mock_brownfield_ai):
    """Brownfield prompt contains extracted doc content and module node_ids."""
    project = _setup_project(tmp_path, include_wave_config=False)
    _write_extracted_docs(project)

    planner_module.plan_init(project)

    prompt = mock_brownfield_ai[0]["input"]
    assert "TaskBoard System Context" in prompt
    assert "design:extract:system-context" in prompt
    assert "design:extract:auth" in prompt
    assert "design:extract:tasks" in prompt
    assert "modules" in prompt
    assert '"modules": ["module_name_1", "module_name_2"]' in prompt


def test_plan_init_prefers_requirements_over_extracted(tmp_path, mock_plan_init_ai):
    """When both requirement docs and extracted docs exist, greenfield (requirements) is used."""
    project = _setup_project(tmp_path, include_wave_config=False)
    _write_requirement(project)
    _write_extracted_docs(project)

    result = planner_module.plan_init(project)

    # Greenfield prompt is used (not brownfield)
    prompt = mock_plan_init_ai[0]["input"]
    assert "BROWNFIELD" not in prompt
    assert "--- BEGIN REQUIREMENT" in prompt
    assert "req:project-requirements" in prompt

    # requirement_paths contains requirement doc paths, not extracted
    assert any("requirements" in p for p in result.requirement_paths)
    assert not any("extracted" in p for p in result.requirement_paths)


def test_plan_init_raises_when_no_requirements_and_no_extracted(tmp_path):
    """plan_init raises ValueError with helpful message when neither doc type exists."""
    project = _setup_project(tmp_path, include_wave_config=False)

    with pytest.raises(ValueError, match="no requirement documents or extracted documents found"):
        planner_module.plan_init(project)

    # Also check the message mentions both options
    with pytest.raises(ValueError, match="codd extract"):
        planner_module.plan_init(project)

    with pytest.raises(ValueError, match="greenfield"):
        planner_module.plan_init(project)


def test_plan_init_prompt_includes_framework_conventions(tmp_path, mock_plan_init_ai):
    """When frameworks are configured, the plan prompt includes them and asks for implicit conventions."""
    project = _setup_project(tmp_path, include_wave_config=False)
    _write_requirement(project)

    # Inject frameworks into config
    config_path = project / "codd" / "codd.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["project"]["frameworks"] = ["Next.js", "Prisma"]
    config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")

    planner_module.plan_init(project)

    prompt = mock_plan_init_ai[0]["input"]
    assert "Next.js" in prompt
    assert "Prisma" in prompt
    assert "framework implicit conventions" in prompt


def test_plan_init_prompt_shows_none_when_no_frameworks(tmp_path, mock_plan_init_ai):
    """When no frameworks are configured, the prompt shows (none) and still mentions the category."""
    project = _setup_project(tmp_path, include_wave_config=False)
    _write_requirement(project)

    planner_module.plan_init(project)

    prompt = mock_plan_init_ai[0]["input"]
    assert "Detected/configured frameworks: (none)" in prompt
    assert "framework implicit conventions" in prompt
