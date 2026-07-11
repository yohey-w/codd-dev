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

    def fake_run(command, *, input, capture_output, text, check, **kwargs):
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
    assert "Operational Behavior Model before implementation planning" in prompt
    assert "not an E2E test artifact" in prompt
    assert "actor-facing surface/copy obligations before implementation planning" in prompt
    assert "allowed and forbidden actions/navigation" in prompt
    assert "job-to-be-done language" in prompt


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

    def fake_run(command, *, input, capture_output, text, check, **kwargs):
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
    (extracted_dir / "system-context.md").write_text("# extracted\n", encoding="utf-8")
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

    result = runner.invoke(main, ["plan", "--path", str(project), "--format", "json"])

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
    # The AI returned waves 1-2 (no canonical VB doc); the planner GUARANTEES the
    # canonical VB registry by force-injecting it as a final wave (3).
    assert sorted(config["wave_config"]) == ["1", "2", "3"]
    assert config["wave_config"]["3"][0]["node_id"] == "test:test-strategy"
    assert config["wave_config"]["3"][0]["output"] == "docs/test/test_strategy.md"
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


# --- Issue #27: AI-output stabilization for noisy wave_config responses ----
# AI backends wrap structured output in markdown fences, conversational prose,
# or a tool-call JSON envelope. The parser must recover the YAML body from this
# noise in a backend-agnostic way (no model/tool-specific handling).

_NOISY_WAVE_CONFIG_BODY = """\
"1":
  - node_id: "design:acceptance-criteria"
    output: "docs/test/acceptance_criteria.md"
    title: "Acceptance Criteria"
"""


def _assert_recovered(payload):
    assert payload["1"][0]["node_id"] == "design:acceptance-criteria"
    assert payload["1"][0]["output"] == "docs/test/acceptance_criteria.md"


def test_parse_wave_config_output_recovers_fence_with_leading_and_trailing_prose():
    raw_output = (
        "Sure! Here is the wave_config you asked for:\n\n"
        "```yaml\n"
        f"{_NOISY_WAVE_CONFIG_BODY}"
        "```\n\n"
        "Let me know if you need anything else!\n"
    )

    # Sanity: the pre-fix narrow path (whole-string fence strip + fence-line
    # removal) leaves trailing prose in place and cannot parse this output.
    pre_fix_cleaned = planner_module._clean_wave_config_output(raw_output)
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(pre_fix_cleaned)

    _assert_recovered(planner_module._parse_wave_config_output(raw_output))


def test_parse_wave_config_output_recovers_bare_fence_with_trailing_prose():
    raw_output = (
        "Here you go:\n\n"
        "```\n"
        f"{_NOISY_WAVE_CONFIG_BODY}"
        "```\n"
        "trailing note\n"
    )

    pre_fix_cleaned = planner_module._clean_wave_config_output(raw_output)
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(pre_fix_cleaned)

    _assert_recovered(planner_module._parse_wave_config_output(raw_output))


def test_parse_wave_config_output_recovers_yaml_from_tool_call_json_envelope():
    fenced_payload = "```yaml\n" + _NOISY_WAVE_CONFIG_BODY + "```"
    raw_output = json.dumps({"type": "tool_result", "content": fenced_payload})

    # Sanity: parsing the JSON envelope text directly as YAML yields a mapping
    # whose keys are not wave numbers, so the pre-fix path rejected it.
    envelope_payload = yaml.safe_load(planner_module._clean_wave_config_output(raw_output))
    assert isinstance(envelope_payload, dict)
    assert "1" not in envelope_payload

    _assert_recovered(planner_module._parse_wave_config_output(raw_output))


def test_parse_wave_config_output_prefers_yaml_tagged_fence_over_other_blocks():
    # A prose block fenced as text precedes the real yaml block. The yaml-tagged
    # fence must win regardless of position.
    raw_output = (
        "Reasoning:\n\n"
        "```text\n"
        "I considered the dependency order and grouping.\n"
        "```\n\n"
        "Result:\n\n"
        "```yaml\n"
        f"{_NOISY_WAVE_CONFIG_BODY}"
        "```\n"
    )

    _assert_recovered(planner_module._parse_wave_config_output(raw_output))


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


def test_plan_init_brownfield_prompt_keeps_operational_model_in_design(tmp_path, mock_brownfield_ai):
    project = _setup_project(tmp_path, include_wave_config=False)
    _write_extracted_docs(project)

    planner_module.plan_init(project)

    prompt = mock_brownfield_ai[0]["input"]
    assert "Operational Behavior Model before implementation planning" in prompt
    assert "not an E2E test artifact" in prompt
    assert "actor-facing surface/copy obligations before implementation planning" in prompt
    assert "forbidden copy patterns" in prompt


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

    def fake_run(command, *, input, capture_output, text, check, **kwargs):
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


def test_load_requirement_documents_accepts_plural_type(tmp_path):
    # Hand-authored requirement docs (and the artifact-catalog vocabulary) use
    # `type: requirements`, while `codd init --requirements` stamps the
    # singular `requirement`. Discovery must accept both — a real greenfield
    # autopilot run failed at `plan --init` because the user's doc used the
    # plural form (2026-06-11 dogfood).
    project = _setup_project(tmp_path, include_wave_config=False)
    _write_doc(
        project,
        "docs/requirements/requirements.md",
        node_id="req:plural-form",
        doc_type="requirements",
    )

    documents = planner_module._load_requirement_documents(
        project, {"scan": {"doc_dirs": ["docs/"]}}
    )

    assert [doc.node_id for doc in documents] == ["req:plural-form"]


def test_load_requirement_documents_accepts_singular_type(tmp_path):
    project = _setup_project(tmp_path, include_wave_config=False)
    _write_requirement(project)

    documents = planner_module._load_requirement_documents(
        project, {"scan": {"doc_dirs": ["docs/"]}}
    )

    assert [doc.node_id for doc in documents] == ["req:project-requirements"]


# ---------------------------------------------------------------------------
# wave_config bounded parse-feedback retry (weakest-model robustness): a weak
# model that emits malformed YAML must not kill the plan stage — the parse error
# is fed back and the AI re-prompted (dogfood finding: Spark failed plan --init on
# a stray `-> ` arrow while gpt-5.5 passed the same spec).
# ---------------------------------------------------------------------------

# A YAML body that mirrors the real Spark failure: a stray `-> ` arrow + a key at a
# bad indent — a hard yaml.YAMLError that no textual sanitization can repair.
_MALFORMED_WAVE_CONFIG = (
    '"1":\n'
    '  - node_id: "design:x"\n'
    '    output: "docs/x.md"\n'
    '    title: "X"\n'
    "      -> \n"
    '    reason: "bad"\n'
)


def _seq_invoke(monkeypatch, responses):
    """Monkeypatch _invoke_ai_command to return/raise successive responses; record prompts."""
    prompts: list[str] = []
    it = iter(responses)

    def fake_invoke(command, prompt):
        prompts.append(prompt)
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("codd.generator._invoke_ai_command", fake_invoke)
    return prompts


def test_invoke_and_parse_wave_config_retries_malformed_then_succeeds(monkeypatch):
    prompts = _seq_invoke(monkeypatch, [_MALFORMED_WAVE_CONFIG, PLAN_INIT_AI_OUTPUT])
    result = planner_module._invoke_and_parse_wave_config("ai-cmd", "BASE PROMPT", max_retries=2)
    assert result["1"][0]["node_id"] == "design:acceptance-criteria"
    assert len(prompts) == 2  # one retry
    assert prompts[0] == "BASE PROMPT"  # first attempt is the bare prompt
    assert "REJECTED" in prompts[1] and "valid wave_config" in prompts[1]  # feedback carried


def test_invoke_and_parse_wave_config_infra_failure_not_retried(monkeypatch):
    prompts = _seq_invoke(monkeypatch, [ValueError("AI command failed: boom")])
    with pytest.raises(ValueError, match="AI command failed"):
        planner_module._invoke_and_parse_wave_config("ai-cmd", "BASE", max_retries=2)
    assert len(prompts) == 1  # infra failure short-circuits — no wasted retries


def test_invoke_and_parse_wave_config_exhausts_retries(monkeypatch):
    prompts = _seq_invoke(monkeypatch, [_MALFORMED_WAVE_CONFIG] * 3)
    with pytest.raises(ValueError, match="invalid wave_config YAML"):
        planner_module._invoke_and_parse_wave_config("ai-cmd", "BASE", max_retries=2)
    assert len(prompts) == 3  # initial + 2 retries


def test_invoke_and_parse_wave_config_retries_disabled(monkeypatch):
    prompts = _seq_invoke(monkeypatch, [_MALFORMED_WAVE_CONFIG, PLAN_INIT_AI_OUTPUT])
    with pytest.raises(ValueError, match="invalid wave_config YAML"):
        planner_module._invoke_and_parse_wave_config("ai-cmd", "BASE", max_retries=0)
    assert len(prompts) == 1  # disabled: single attempt


def test_invoke_and_parse_wave_config_valid_first_no_retry(monkeypatch):
    prompts = _seq_invoke(monkeypatch, [PLAN_INIT_AI_OUTPUT])
    result = planner_module._invoke_and_parse_wave_config("ai-cmd", "BASE", max_retries=2)
    assert result["1"][0]["node_id"] == "design:acceptance-criteria"
    assert len(prompts) == 1  # no wasted retry on a good first response


def test_plan_init_max_retries_config_override():
    assert planner_module._plan_init_max_retries({}) == planner_module.DEFAULT_PLAN_INIT_MAX_RETRIES
    assert planner_module._plan_init_max_retries({"plan": {"init_retry": {"max_retries": 5}}}) == 5
    assert planner_module._plan_init_max_retries({"plan": {"init_retry": {"max_retries": 0}}}) == 0
    # Malformed config falls back to the default (never crashes).
    assert planner_module._plan_init_max_retries({"plan": "nope"}) == planner_module.DEFAULT_PLAN_INIT_MAX_RETRIES


# ---------------------------------------------------------------------------
# Canonical VB doc GUARANTEE (greenfield SSOT): if the AI's wave_config omits
# test:test-strategy / docs/test/test_strategy.md, the planner force-injects it (when
# the coverage gate is on) so the VB coverage/authenticity SSOT is never silently
# absent (a missing registry lets the coverage gate trivially pass 0/0 = false-GREEN).
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from codd.verifiable_behavior_audit import wave_config_plans_canonical_vb_doc  # noqa: E402

_REQ_DOCS = [SimpleNamespace(node_id="req:project-requirements")]


def test_ensure_canonical_vb_doc_injected_when_ai_omits_it():
    wave_config = {
        "1": [{"node_id": "design:x", "output": "docs/design/x.md", "title": "X", "modules": ["module:x"]}],
        "2": [{"node_id": "test:acceptance-criteria", "output": "docs/test/acceptance_criteria.md", "title": "AC"}],
    }
    out = planner_module._ensure_canonical_vb_doc_planned({}, wave_config, _REQ_DOCS)
    assert wave_config_plans_canonical_vb_doc({"wave_config": out})
    injected = [e for ents in out.values() for e in ents if e.get("node_id") == "test:test-strategy"]
    assert len(injected) == 1
    assert injected[0]["output"] == "docs/test/test_strategy.md"
    assert "3" in out  # appended as a NEW final wave (existing waves untouched)
    dep_ids = {d["id"] for d in injected[0]["depends_on"]}
    assert "req:project-requirements" in dep_ids
    assert {"design:x", "test:acceptance-criteria"} <= dep_ids


def test_ensure_canonical_vb_doc_no_duplicate_when_ai_includes_it():
    wave_config = {"1": [{"node_id": "test:test-strategy", "output": "docs/test/test_strategy.md", "title": "TS"}]}
    out = planner_module._ensure_canonical_vb_doc_planned({}, wave_config, _REQ_DOCS)
    injected = [e for ents in out.values() for e in ents if e.get("node_id") == "test:test-strategy"]
    assert len(injected) == 1  # no duplicate
    assert out == wave_config  # unchanged


def test_ensure_canonical_vb_doc_respects_explicit_test_coverage_docs():
    wave_config = {"1": [{"node_id": "test:acceptance-criteria", "output": "docs/test/acceptance_criteria.md", "title": "AC"}]}
    config = {"test_coverage": {"docs": ["docs/test/custom_vb.md"]}}
    out = planner_module._ensure_canonical_vb_doc_planned(config, wave_config, _REQ_DOCS)
    assert len([e for ents in out.values() for e in ents if e.get("node_id") == "test:test-strategy"]) == 0
    assert out == wave_config


def test_ensure_canonical_vb_doc_skipped_when_coverage_gate_off():
    """Coverage gate explicitly off (owner opt-out) → no VB SSOT needed → no injection."""
    wave_config = {"1": [{"node_id": "test:acceptance-criteria", "output": "docs/test/acceptance_criteria.md", "title": "AC"}]}
    config = {"test_coverage": {"gate": False}}
    out = planner_module._ensure_canonical_vb_doc_planned(config, wave_config, _REQ_DOCS)
    assert len([e for ents in out.values() for e in ents if e.get("node_id") == "test:test-strategy"]) == 0
    assert out == wave_config


# ---------------------------------------------------------------------------
# VB → task coverage-closure synthesis (plan-stage). `_ensure_canonical_vb_doc_planned`
# only guarantees the registry DOCUMENT is planned; this extends the guarantee by one
# level — every DECLARED VB must be CLAIMABLE by some derived task (a task whose declared
# test outputs own the VB's declared owner test file). VBs no task can claim (cross-cutting
# suite-level / static-source / universally-quantified invariants that map to no module) are
# RESIDUAL, and a single cross-cutting test-authoring task is synthesized to own exactly them.
# Reproduces the 2026-07 S3 StockRoom-mini burn: 10 declared VBs left with no owning task.
# ---------------------------------------------------------------------------

from codd.verifiable_behavior_audit import VerifiableBehavior  # noqa: E402

_VB_CFG = {"scan": {"source_dirs": ["src/"], "test_dirs": ["tests/"]}}


def _vb(vb_id, owner, desc="behaves"):
    """A declared VB whose registry row names ``owner`` as its owning test file(s)."""
    return VerifiableBehavior(vb_id=vb_id, description=desc, source_doc="docs/test/test_strategy.md", declared_scenarios=owner)


def test_vb_closure_synthesizes_task_owning_exactly_residual_vbs():
    behaviors = [
        _vb("VB-01", "`tests/mod_a/create.test.ts`"),          # authored by a task → claimable
        _vb("VB-06", "`tests/e2e/error-envelope.e2e.test.ts`"),  # no task authors it → residual
        _vb("VB-38", "`tests/architecture/dependency-manifest.test.ts`"),  # residual
    ]
    task_expected_outputs = [
        ["src/mod_a/create.ts", "tests/mod_a/create.test.ts"],  # authors VB-01's owner
        ["docs/test/test_strategy.md"],                          # the doc-only registry task (authors no test)
    ]
    closure = planner_module.synthesize_vb_coverage_closure_task(
        behaviors, task_expected_outputs, config=_VB_CFG
    )
    assert closure is not None
    # Owns EXACTLY the residual VBs — not the module-claimed one.
    assert set(closure.owned_vb_ids) == {"VB-06", "VB-38"}
    # Authors the residual VBs' declared owner test files (write target = test surface).
    assert "tests/e2e/error-envelope.e2e.test.ts" in closure.expected_outputs
    assert "tests/architecture/dependency-manifest.test.ts" in closure.expected_outputs
    assert all(".ts" in out for out in closure.expected_outputs)
    # Design node = the canonical registry doc so the implement prompt reads it.
    assert closure.design_node == "docs/test/test_strategy.md"
    # Prompt carries the residual VB ids so the implementer emits their `covers vb=` markers.
    assert "VB-06" in closure.description and "VB-38" in closure.description
    assert "VB-01" not in closure.description


def test_vb_closure_no_task_when_all_vbs_claimable():
    """GENERALITY: when every declared VB's owner test file is authored by some
    derived task, there is no residual — no closure task is synthesized."""
    behaviors = [
        _vb("VB-01", "`tests/mod_a/create.test.ts`"),
        _vb("VB-02", "`tests/mod_b/read.test.ts`"),
    ]
    task_expected_outputs = [
        ["src/mod_a/create.ts", "tests/mod_a/create.test.ts"],
        ["src/mod_b/read.ts", "tests/mod_b/read.test.ts"],
    ]
    closure = planner_module.synthesize_vb_coverage_closure_task(
        behaviors, task_expected_outputs, config=_VB_CFG
    )
    assert closure is None


def test_vb_closure_no_task_when_vbs_declare_no_owner_file():
    """No-regression: a registry with no owner-file column (VB rows name no test
    file) yields no residual — we never force-synthesize for VBs we cannot prove
    are orphaned (else every legacy project would gain a redundant task)."""
    behaviors = [
        _vb("VB-01", "scenario: happy path returns 200"),
        _vb("VB-02", "scenario: bad input returns 400"),
    ]
    closure = planner_module.synthesize_vb_coverage_closure_task(
        behaviors, [["src/app.ts", "tests/app.test.ts"]], config=_VB_CFG
    )
    assert closure is None


def test_vb_closure_glob_owner_is_residual():
    """A suite-level glob owner (`tests/e2e/*.e2e.test.ts`) names an owner intent
    but matches no single authored file → residual (this is the universally-
    quantified/harness-level VB shape that has no module owner)."""
    behaviors = [_vb("VB-37", "Every e2e file uses startEphemeralApp — `tests/e2e/*.e2e.test.ts`")]
    closure = planner_module.synthesize_vb_coverage_closure_task(
        behaviors, [["tests/mod_a/create.test.ts"]], config=_VB_CFG
    )
    assert closure is not None
    assert set(closure.owned_vb_ids) == {"VB-37"}


def test_vb_closure_skipped_when_coverage_gate_off():
    behaviors = [_vb("VB-06", "`tests/e2e/error-envelope.e2e.test.ts`")]
    closure = planner_module.synthesize_vb_coverage_closure_task(
        behaviors, [["docs/test/test_strategy.md"]], config={"test_coverage": {"gate": False}}
    )
    assert closure is None
