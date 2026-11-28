"""Tests for wave-based template generation."""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import subprocess

from click.testing import CliRunner
import pytest
import yaml

import codd.generator as generator_module
from codd.cli import main
from codd.design_consistency import DesignConsistencyError
from codd.generator import generate_wave
from codd.project_types import ProjectCapabilities
from codd.scanner import _extract_frontmatter


CoDD_YAML = """
ai_command: "mock-ai --print"
project:
  name: test-project
  language: python
scan:
  source_dirs: []
  doc_dirs:
    - "docs/requirements/"
    - "docs/design/"
    - "docs/detailed_design/"
    - "docs/plan/"
    - "docs/governance/"
    - "docs/test/"
  exclude: []
graph:
  store: jsonl
  path: codd/scan
conventions:
  - targets:
      - "db:rls_policies"
    reason: "Global invariant"
wave_config:
  "1":
    - node_id: "design:acceptance-criteria"
      output: "docs/test/acceptance_criteria.md"
      title: "Acceptance Criteria"
      depends_on:
        - id: "req:lms-requirements-v2.0"
          relation: derives_from
      conventions:
        - targets:
            - "db_table:audit_logs"
          reason: "Artifact invariant"
    - node_id: "governance:decisions"
      output: "docs/governance/decisions.md"
      title: "Decisions"
      depends_on:
        - id: "req:lms-requirements-v2.0"
          relation: derives_from
  "2":
    - node_id: "design:system-design"
      output: "docs/design/system_design.md"
      title: "System Design"
      depends_on:
        - id: "design:acceptance-criteria"
          relation: constrained_by
        - id: "governance:decisions"
          relation: informed_by
  "3":
    - node_id: "design:implementation-plan"
      output: "docs/plan/implementation_plan.md"
      title: "Implementation Plan"
      depends_on:
        - id: "design:acceptance-criteria"
          relation: validates
"""


def _setup_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(CoDD_YAML, encoding="utf-8")
    requirement_path = project / "docs" / "requirements" / "lms_requirements_v2.0.md"
    requirement_path.parent.mkdir(parents=True, exist_ok=True)
    requirement_path.write_text(
        """---
codd:
  node_id: "req:lms-requirements-v2.0"
  type: "requirement"
---

# LMS Requirements

## Scope

Tenant isolation and audit logging are mandatory.
""",
        encoding="utf-8",
    )
    return project


RECONCILIATION_OWNER = "docs/governance/decisions.md"
RECONCILIATION_PEER = "docs/design/system_design.md"
RECONCILIATION_REPORTER = "docs/plan/implementation_plan.md"
RECONCILIATION_LOG = ".codd/audit/design-reconciliations.jsonl"
ACCEPTANCE_CRITERIA = "docs/test/acceptance_criteria.md"
CERTIFICATE_SENTENCE = (
    "A test-only TLS certificate that tests expect to be valid (for a local HTTPS or mail server the tests start) "
    "MUST become valid well before it is issued (for example, a day earlier) and stay valid beyond the run, "
    "so a system clock adjusted during the run cannot make it not yet valid; "
    "certificates whose validity a test deliberately limits or breaks (invalid, expired, short-lived or rotated) are exempt."
)
CERTIFICATE_FRAGMENTS = (
    "Test-only certificates",
    "A test-only TLS certificate",
    "become valid well before it is issued",
    "stay valid beyond the run",
    "a system clock adjusted during the run",
    "cannot make it not yet valid",
    "certificates a test deliberately makes invalid or expired are exempt",
    "certificates whose validity a test deliberately limits or breaks",
    "The readiness check connects with the same trust",
)
DISPUTED_POLICY = "The system design does not follow the approved import size limit."
UNRESTRICTED_IMPORT = "Validation accepts files of any size."
BOUNDED_IMPORT = "Validation rejects files larger than 100 bytes."


def _reconciliation_document(rule, *, spec=None, disagreement=None):
    quote = disagreement[0] if disagreement else None
    declarations = {
        "owns": {spec: rule} if spec else {},
        "cases": [],
        "unresolved": [{"documents": list(disagreement[1]), "quote": quote}] if disagreement else [],
    }
    paragraphs = [rule, quote] if quote else [rule]
    return (
        "# File Import\n\n## Overview\n\n"
        + "\n\n".join(paragraphs)
        + f"\n\n```codd-behavior\n{yaml.safe_dump(declarations)}```\n"
    )


def _write_reconciliation_document(project, output, content):
    path = project / output
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _report_import_disagreement():
    return _reconciliation_document(
        "The implementation plan relies on the approved import size limit.",
        disagreement=(DISPUTED_POLICY, (RECONCILIATION_OWNER, RECONCILIATION_PEER)),
    )


@pytest.fixture
def reconciliation_project(tmp_path, monkeypatch):
    project = _setup_project(tmp_path)
    config = yaml.safe_load(CoDD_YAML)
    config["generate"] = {"design_consistency": True, "body_retry": {"max_retries": 1}}
    config["wave_config"]["2"][0]["depends_on"] = ["governance:decisions"]
    config["wave_config"]["3"][0]["depends_on"] = ["design:system-design"]
    (project / "codd/codd.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    _write_reconciliation_document(project, RECONCILIATION_OWNER, _reconciliation_document(
        "The approved import size limit is 100 bytes.", spec="import-policy",
    ))
    _write_reconciliation_document(project, RECONCILIATION_PEER, _reconciliation_document(
        UNRESTRICTED_IMPORT, spec="import-validation",
    ))
    calls = []

    def generate(_command, prompt):
        if "Select reconciliation edit locations" in prompt:
            # The invalid target in this fixture has an additional unresolved
            # report. Only the eligibility regression should ever reach it.
            lines = (project / RECONCILIATION_PEER).read_text(encoding="utf-8").splitlines()
            return json.dumps({"replace_lines": list(range(5, len(lines))), "insert_after": []})
        if "Node ID: design:system-design" in prompt:
            calls.append("rewrite")
            return _reconciliation_document(BOUNDED_IMPORT, spec="import-validation")
        assert "Node ID: design:implementation-plan" in prompt
        calls.append("reporter")
        if BOUNDED_IMPORT in prompt:
            return _reconciliation_document("The implementation plan enforces the 100 byte limit.")
        return _report_import_disagreement()

    monkeypatch.setattr(generator_module, "_invoke_ai_command", generate)
    return project, calls


def test_zero_reconciliation_budget_does_not_reread_cited_documents(reconciliation_project, monkeypatch):
    project, calls = reconciliation_project
    config_path = project / "codd/codd.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["generate"]["body_retry"]["max_retries"] = 0
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    read_bytes = Path.read_bytes
    snapshots = []

    def record_cited_snapshot(path):
        if path in {project / RECONCILIATION_OWNER, project / RECONCILIATION_PEER}:
            snapshots.append(path.relative_to(project).as_posix())
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", record_cited_snapshot)

    with pytest.raises(DesignConsistencyError, match=DISPUTED_POLICY):
        generate_wave(project, 3)

    assert calls == ["reporter"]
    assert snapshots == []
    assert not (project / RECONCILIATION_REPORTER).exists()
    assert not (project / RECONCILIATION_LOG).exists()


def test_reconciliation_cannot_rewrite_a_document_later_in_the_same_wave(reconciliation_project):
    project, calls = reconciliation_project
    config_path = project / "codd/codd.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["wave_config"]["2"] = config["wave_config"].pop("3") + config["wave_config"]["2"]
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    peer = project / RECONCILIATION_PEER
    before = peer.read_bytes()

    with pytest.raises(DesignConsistencyError, match=DISPUTED_POLICY):
        generate_wave(project, 2)

    assert calls == ["reporter"]
    assert peer.read_bytes() == before
    assert not (project / RECONCILIATION_REPORTER).exists()
    assert not (project / RECONCILIATION_LOG).exists()


def test_reconciliation_cannot_rewrite_a_cited_document_with_its_own_disagreement(reconciliation_project):
    project, calls = reconciliation_project
    _write_reconciliation_document(project, ACCEPTANCE_CRITERIA, _reconciliation_document(
        "The acceptance criteria exercise the approved import size limit.",
    ))
    _write_reconciliation_document(project, RECONCILIATION_PEER, _reconciliation_document(
        UNRESTRICTED_IMPORT, spec="import-validation",
        disagreement=(
            "The decisions and acceptance criteria disagree about empty input files.",
            (RECONCILIATION_OWNER, ACCEPTANCE_CRITERIA),
        ),
    ))
    peer = project / RECONCILIATION_PEER
    before = peer.read_bytes()

    with pytest.raises(DesignConsistencyError, match=DISPUTED_POLICY):
        generate_wave(project, 3)

    assert calls == ["reporter"]
    assert peer.read_bytes() == before
    assert not (project / RECONCILIATION_REPORTER).exists()
    assert not (project / RECONCILIATION_LOG).exists()


@pytest.mark.parametrize("failure", ["document", "audit"])
def test_reconciliation_restores_documents_when_publication_fails(reconciliation_project, monkeypatch, failure):
    project, calls = reconciliation_project
    peer = project / RECONCILIATION_PEER
    reporter = project / RECONCILIATION_REPORTER
    log = project / RECONCILIATION_LOG
    before = peer.read_bytes()
    previous_log = b'{"review_dependents": []}\n'
    log.parent.mkdir(parents=True)
    log.write_bytes(previous_log)
    write_bytes = Path.write_bytes
    temporary_file = generator_module.NamedTemporaryFile

    def partial_document_write(path, data):
        if path == reporter:
            write_bytes(path, data[:10])
            raise OSError("publication failed")
        return write_bytes(path, data)

    @contextmanager
    def partial_audit_write(**kwargs):
        with temporary_file(**kwargs) as handle:
            write = handle.write

            def fail_after_prefix(data):
                assert data.startswith(previous_log)
                write(data[:len(previous_log) + 10])
                raise OSError("publication failed")

            monkeypatch.setattr(handle, "write", fail_after_prefix)
            yield handle

    if failure == "document":
        monkeypatch.setattr(Path, "write_bytes", partial_document_write)
    else:
        monkeypatch.setattr(generator_module, "NamedTemporaryFile", partial_audit_write)

    with pytest.raises(OSError, match="publication failed"):
        generate_wave(project, 3)

    assert calls == ["reporter", "rewrite", "reporter"]
    assert peer.read_bytes() == before
    assert not reporter.exists()
    assert log.read_bytes() == previous_log
    assert list(log.parent.iterdir()) == [log]


def test_complete_reconciliation_rejects_staged_documents_when_a_planned_document_is_missing(reconciliation_project):
    project, calls = reconciliation_project
    _write_reconciliation_document(project, RECONCILIATION_REPORTER, _report_import_disagreement())
    peer = project / RECONCILIATION_PEER
    reporter = project / RECONCILIATION_REPORTER
    peer_before = peer.read_bytes()
    reporter_before = reporter.read_bytes()

    with pytest.raises(DesignConsistencyError, match=f"{ACCEPTANCE_CRITERIA}: planned document is missing"):
        generator_module.reconcile_generated_design(project)

    assert calls == ["rewrite", "reporter"]
    assert peer.read_bytes() == peer_before
    assert reporter.read_bytes() == reporter_before
    assert not (project / RECONCILIATION_LOG).exists()


@pytest.fixture
def mock_ai_cli(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_run(command, *, input, capture_output, text, check, **kwargs):
        stdout = "# Generated Content\n\n## Overview\n\nThis document contains concrete content.\n"
        if "docs/detailed_design/" in input:
            stdout = (
                "# Generated Content\n\n"
                "## Overview\n\n"
                "This document contains concrete content.\n\n"
                "```mermaid\n"
                "flowchart TD\n"
                "    A[Source] --> B[Target]\n"
                "```\n"
            )
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
            stdout=stdout,
            stderr="",
        )

    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)
    return calls


def test_generate_command_creates_wave_documents_from_config(tmp_path, mock_ai_cli):
    project = _setup_project(tmp_path)
    runner = CliRunner()

    result = runner.invoke(main, ["generate", "--wave", "1", "--path", str(project)])

    assert result.exit_code == 0
    assert (project / "docs" / "test" / "acceptance_criteria.md").exists()
    assert (project / "docs" / "governance" / "decisions.md").exists()
    assert "Generated: docs/test/acceptance_criteria.md" in result.output
    assert "Generated: docs/governance/decisions.md" in result.output
    assert mock_ai_cli[0]["command"] == ["mock-ai", "--print"]


def test_generate_frontmatter_infers_depended_by_and_inherits_conventions(tmp_path, mock_ai_cli):
    project = _setup_project(tmp_path)

    generate_wave(project, 1)

    codd = _extract_frontmatter(project / "docs" / "test" / "acceptance_criteria.md")
    assert codd is not None
    assert codd["node_id"] == "design:acceptance-criteria"
    assert codd["type"] == "test"
    assert codd["depends_on"] == [
        {
            "id": "req:lms-requirements-v2.0",
            "relation": "derives_from",
            "semantic": "governance",
        }
    ]

    depended_by = {entry["id"]: entry for entry in codd["depended_by"]}
    assert depended_by["design:system-design"]["relation"] == "constrained_by"
    assert depended_by["design:system-design"]["semantic"] == "governance"
    assert depended_by["design:implementation-plan"]["relation"] == "validates"

    reasons = {entry["reason"] for entry in codd["conventions"]}
    assert "Global invariant" in reasons
    assert "Artifact invariant" in reasons


def test_generate_skips_existing_files_without_force(tmp_path, mock_ai_cli):
    project = _setup_project(tmp_path)
    target = project / "docs" / "test" / "acceptance_criteria.md"
    target.parent.mkdir(parents=True)
    target.write_text("custom content\n", encoding="utf-8")

    results = generate_wave(project, 1)

    status_by_path = {result.path.relative_to(project).as_posix(): result.status for result in results}
    assert status_by_path["docs/test/acceptance_criteria.md"] == "skipped"
    assert target.read_text(encoding="utf-8") == "custom content\n"


def test_generate_force_overwrites_existing_files(tmp_path, mock_ai_cli):
    project = _setup_project(tmp_path)
    target = project / "docs" / "test" / "acceptance_criteria.md"
    target.parent.mkdir(parents=True)
    target.write_text("custom content\n", encoding="utf-8")

    results = generate_wave(project, 1, force=True)

    status_by_path = {result.path.relative_to(project).as_posix(): result.status for result in results}
    assert status_by_path["docs/test/acceptance_criteria.md"] == "generated"

    content = target.read_text(encoding="utf-8")
    assert content.startswith("---\ncodd:\n")
    assert "custom content" not in content
    assert "Generated Content" in content
    assert "TODO" not in content
    assert _extract_frontmatter(target)["node_id"] == "design:acceptance-criteria"


def test_generate_uses_dependency_documents_as_ai_context(tmp_path, mock_ai_cli):
    project = _setup_project(tmp_path)

    generate_wave(project, 1)

    prompt = mock_ai_cli[0]["input"]
    assert "ABSOLUTE PROHIBITION" in prompt
    assert "**Do not emit** YAML frontmatter" in prompt
    assert "CRITICAL ERROR" in prompt
    assert "release-blocking constraint" in prompt
    assert "Start directly with the document content." in prompt
    assert "Treat requirement documents as the source of truth" in prompt
    assert "Before finalizing, self-check that every capability and constraint mentioned in the depends_on documents is represented" in prompt
    assert "Use concrete tool names, framework names, services, table names, endpoints, thresholds, counts, and timelines" in prompt
    assert "Never use vague placeholders such as '推奨なし', '要検討', or 'TBD'." in prompt
    assert "Non-negotiable conventions:" in prompt
    assert "These are release-blocking constraints." in prompt
    assert "Explicitly state how the document complies with each convention and invariant listed below." in prompt
    assert "Targets: db:rls_policies" in prompt
    assert "Targets: db_table:audit_logs" in prompt
    assert "Global invariant" in prompt
    assert "Artifact invariant" in prompt
    assert "tenant isolation in security/data model sections" in prompt
    assert "--- BEGIN DEPENDENCY docs/requirements/lms_requirements_v2.0.md (req:lms-requirements-v2.0) ---" in prompt
    assert "Tenant isolation and audit logging are mandatory." in prompt
    assert "Prefer a structure that covers: Overview, Acceptance Criteria, Failure Criteria, E2E Test Generation Meta-Prompt." in prompt
    assert "Mutating test data:" in prompt
    assert "per-run unique identifiers" in prompt
    assert "explicit cleanup/idempotent teardown" in prompt
    assert "Scenario fixtures:" in prompt
    assert "do not trust mutable shared seed state" in prompt


def test_unmanaged_generation_prompts_match_legacy_bytes(tmp_path, mock_ai_cli):
    project = _setup_project(tmp_path)
    for wave in (1, 2, 3):
        generate_wave(project, wave)

    prompts = [call["input"] for call in mock_ai_cli]
    # Lines added on purpose (the test-environment guidance and the browser runtime rules);
    # everything else must stay byte-identical.
    for added in (
        "- Do not write a `codd-test-environment` block in this document.\n",
        "- Bind the server under test to a loopback port chosen at runtime (an unused ephemeral port), "
        "never a fixed port number, so a leftover or concurrent server cannot collide with it.\n",
        "- The readiness check MUST prove that the server answering is the one this run started "
        "(for example, a per-run token the server reports, or the address the started process itself reports), "
        "not merely that some server answers on that address.\n",
        f"- {CERTIFICATE_SENTENCE}\n",
    ):
        assert prompts[0].count(added) == 1
        prompts[0] = prompts[0].replace(added, "", 1)
    # Captured from d943740 with the same project and mocked AI responses.
    assert [hashlib.sha256(prompt.encode("utf-8")).hexdigest() for prompt in prompts] == [
        "d21ea2838648c5bf380efa56dd2ec425933fded255899e49b19dc4a24473d973",
        "1912abf5245f42b134ef03cc1f532274711782e1f4ddc9609ee1df40841a6f55",
        "4077b16fcbcc7e2047a061f6b836c6ae62e31dbe582d162d27a1654585483a03",
        "ab9849784d3adda9519520c31d709f33a4eaa7204e67bf298b2cd9dd053cbb42",
    ]


def test_explicitly_disabled_generation_prompts_match_legacy_bytes(tmp_path, mock_ai_cli):
    project = _setup_project(tmp_path)
    config = yaml.safe_load(CoDD_YAML)
    config["generate"] = {"design_consistency": False}
    # Stale metadata and an existing declaration must not change the design prompt.
    config["wave_config"]["2"][0].update(behavior_owners=["spec"], owns_operation_flow=True)
    (project / "codd/codd.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    system_design = project / "docs/design/system_design.md"
    system_design.parent.mkdir(parents=True)
    system_design.write_text("# Old design\n\n```codd-behavior\nowns: {}\ncases: []\n```\n", encoding="utf-8")

    generate_wave(project, 1)
    generate_wave(project, 2, force=True)

    # Captured from d943740 with the same project and mocked AI responses.
    assert hashlib.sha256(mock_ai_cli[2]["input"].encode("utf-8")).hexdigest() == (
        "460ff884952052ae7d29c16566970d3e3bde1fd9c031527072bc03466aac816a"
    )


def test_managed_generation_prompt_covers_each_listed_convention(tmp_path, monkeypatch):
    project = _setup_project(tmp_path)
    config_path = project / "codd/codd.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["wave_config"]["1"][0]["behavior_owners"] = []
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    prompts = []

    def generate(_command, prompt):
        prompts.append(prompt)
        return (
            "# Generated Content\n\n## Overview\n\n"
            "This document contains concrete content.\n\n"
            "```codd-behavior\nowns: {}\ncases: []\nunresolved: []\n```\n"
        )

    monkeypatch.setattr(generator_module, "_invoke_ai_command", generate)
    generate_wave(project, 1)

    prompt = prompts[0]
    assert "Behavior ownership and concrete examples:" in prompt
    assert "Cover the requirements assigned to this document." in prompt
    assert "Cite the relevant owning section" in prompt
    assert "For each listed convention and invariant" in prompt
    assert "give the local concrete design and verification measure or cite its owning section" in prompt
    assert "Do not omit any listed item." in prompt
    assert "Targets: db:rls_policies" in prompt
    assert "Targets: db_table:audit_logs" in prompt
    assert "Global invariant" in prompt
    assert "Artifact invariant" in prompt
    assert "Behavior ownership and concrete examples:" not in prompts[1]


def test_regeneration_cannot_remove_existing_behavior_declarations(tmp_path, monkeypatch):
    project = _setup_project(tmp_path)
    body = "# Generated Content\n\n## Overview\n\nThis document contains concrete content.\n"
    original = body + "\n```codd-behavior\nowns: {}\ncases: []\nunresolved: []\n```\n"
    path = project / "docs/test/acceptance_criteria.md"
    path.parent.mkdir()
    path.write_text(original, encoding="utf-8")
    prompts = []

    def omit_declarations(_command, prompt):
        prompts.append(prompt)
        return body

    monkeypatch.setattr(generator_module, "_invoke_ai_command", omit_declarations)
    with pytest.raises(ValueError, match="missing codd-behavior declarations"):
        generator_module.regenerate_artifact(project, node_id="design:acceptance-criteria")

    assert "Behavior ownership and concrete examples:" in prompts[0]
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("role", "owner_elsewhere", "reference_only"),
    [(False, False, False), (None, True, False), (False, True, True), (True, False, False)],
)
def test_managed_design_prompt_obeys_operation_flow_assignment(
    tmp_path, monkeypatch, role, owner_elsewhere, reference_only,
):
    project = _setup_project(tmp_path)
    config_path = project / "codd/codd.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["generate"] = {"design_consistency": True}
    design = config["wave_config"]["2"][0]
    design["depends_on"] = []
    if role is not None:
        design["owns_operation_flow"] = role
    if owner_elsewhere:
        config["wave_config"]["3"].append({
            "node_id": "design:workflow",
            "output": "docs/design/workflow.md",
            "title": "Workflow",
            "owns_operation_flow": True,
        })
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    prompts = []

    def generate(_command, prompt):
        prompts.append(prompt)
        body = (
            "# Generated Content\n\n## Overview\n\n"
            "This document contains concrete content.\n\n"
            "```codd-behavior\nowns: {}\ncases: []\nunresolved: []\n```\n"
        )
        if role is True:
            body += "\n```yaml\noperation_flow:\n  operations:\n    - id: process-input\n```\n"
        return body

    monkeypatch.setattr(generator_module, "_invoke_ai_command", generate)
    generate_wave(project, 2)

    prompt, = prompts
    assert ("Operational Behavior Model (DESIGN-TIME, CRITICAL)" in prompt) is not reference_only
    assert ("Do not emit operation_flow here." in prompt) is reference_only
    assert ("Emit operation_flow for the operations assigned here." in prompt) is (role is True)


@pytest.mark.parametrize("managed", [False, True])
def test_generate_applies_path_confinement_to_managed_artifacts(tmp_path, monkeypatch, managed):
    project = _setup_project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    design = project / "docs/design"
    design.mkdir()
    (design / "linked").symlink_to(outside, target_is_directory=True)
    output = "docs/design/linked/subdir/system.md"
    config_path = project / "codd/codd.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["wave_config"]["2"][0]["output"] = output
    config["wave_config"]["2"][0]["depends_on"] = []
    if managed:
        config["wave_config"]["2"][0]["behavior_owners"] = []
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    prompts = []

    def generate(_command, prompt):
        prompts.append(prompt)
        assert (outside / "subdir").is_dir()
        return "# Generated Content\n\n## Overview\n\nThis document contains concrete content.\n"

    monkeypatch.setattr(generator_module, "_invoke_ai_command", generate)
    if managed:
        with pytest.raises(ValueError, match="design output escapes the project"):
            generate_wave(project, 2)
        assert not prompts
        assert not (outside / "subdir").exists()
    else:
        result, = generate_wave(project, 2)
        assert result.path == project / output
        assert len(prompts) == 1
        assert (outside / "subdir/system.md").is_file()


def test_design_prompt_requires_design_time_operational_behavior_model(tmp_path, mock_ai_cli):
    project = _setup_project(tmp_path)

    generate_wave(project, 1)
    mock_ai_cli.clear()
    generate_wave(project, 2)

    prompt = mock_ai_cli[0]["input"]
    assert "Operational Behavior Model (DESIGN-TIME, CRITICAL)" in prompt
    assert "Do not postpone operational workflow discovery to E2E generation" in prompt
    assert "operation_flow.operations" in prompt
    assert "measured/observed events" in prompt
    assert "derived or aggregate read models" in prompt
    assert "measurement_source" in prompt
    assert "threshold/boundary behavior" in prompt
    assert "dod_obligations" in prompt
    assert "machine-checkable" in prompt
    assert "This is not an E2E scenario list" in prompt
    assert "Actor-Facing Surface/Copy Obligations (DESIGN-TIME, CRITICAL)" in prompt
    assert "allowed actions/navigation" in prompt
    assert "forbidden actions/navigation" in prompt
    assert "job-to-be-done language" in prompt
    assert "pre-authentication surfaces must not expose ambiguous role-resolved or protected navigation" in prompt


def test_generate_supports_detailed_design_documents_with_mermaid_guidance(tmp_path, mock_ai_cli):
    project = _setup_project(tmp_path)
    config_path = project / "codd" / "codd.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["wave_config"]["3"].append(
        {
            "node_id": "design:shared-domain-model",
            "output": "docs/detailed_design/shared_domain_model.md",
            "title": "Shared Domain Model",
            "depends_on": [
                {
                    "id": "design:system-design",
                    "relation": "depends_on",
                    "semantic": "technical",
                }
            ],
            "conventions": [
                {
                    "targets": ["module:auth", "db:rls_policies"],
                    "reason": "Canonical ownership of shared types must be explicit before implementation.",
                }
            ],
        }
    )
    config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")

    generate_wave(project, 1)
    generate_wave(project, 2)
    generate_wave(project, 3)

    detailed_doc = project / "docs" / "detailed_design" / "shared_domain_model.md"
    assert detailed_doc.exists()
    assert _extract_frontmatter(detailed_doc)["type"] == "design"

    prompt = mock_ai_cli[-1]["input"]
    assert "docs/detailed_design/" in prompt
    assert "downstream-ready detailed design document" in prompt
    assert "Use Mermaid diagrams" in prompt
    assert "prevent reimplementation drift" in prompt


def test_generate_command_allows_ai_cmd_override(tmp_path, mock_ai_cli):
    project = _setup_project(tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["generate", "--wave", "1", "--path", str(project), "--ai-cmd", "custom-ai --print"],
    )

    assert result.exit_code == 0
    assert mock_ai_cli[0]["command"] == ["custom-ai", "--print"]


def test_sanitize_generated_body_removes_meta_preamble_code_fence_and_duplicate_title():
    raw_body = """```markdown
The docs/design directory doesn't exist yet. Since the user asked me to write the document body (not save to a file), I'll output the complete Markdown document body directly.

---

# External Integration Design (LINE/Stripe/Bunny Stream)

## Overview

Concrete design content.
```"""

    sanitized = generator_module._sanitize_generated_body(
        "External Integration Design (LINE/Stripe/Bunny Stream)",
        raw_body,
    )

    assert sanitized == (
        "# External Integration Design (LINE/Stripe/Bunny Stream)\n\n"
        "## Overview\n\n"
        "Concrete design content.\n"
    )


def test_sanitize_generated_body_removes_leading_meta_line_without_heading():
    sanitized = generator_module._sanitize_generated_body(
        "System Architecture Design",
        "I'll write the complete Markdown body directly.\n\n## Overview\n\nDetails.\n",
    )

    assert sanitized == "# System Architecture Design\n\n## Overview\n\nDetails.\n"


def test_sanitize_generated_body_removes_inline_context_meta_preamble():
    raw_body = """
The dependency documents provided inline contain all the context needed. Let me now write the external integrations design document.

# External Integration Design

## Overview

Concrete design content.
"""

    sanitized = generator_module._sanitize_generated_body("External Integration Design", raw_body)

    assert sanitized == "# External Integration Design\n\n## Overview\n\nConcrete design content.\n"


def test_sanitize_generated_body_removes_docs_directory_meta_preamble():
    raw_body = """
The docs directory exists but is empty (the dependency documents were provided as inline context). Now let me write the implementation plan. I'll output the document body directly as requested.

## Overview

Milestones.
"""

    sanitized = generator_module._sanitize_generated_body("Implementation Plan", raw_body)

    assert sanitized == "# Implementation Plan\n\n## Overview\n\nMilestones.\n"


def test_sanitize_generated_body_removes_heres_meta_preamble():
    raw_body = """
Here's the architecture decisions document:

# Architecture Decision Record

## Overview

Concrete content.
"""

    sanitized = generator_module._sanitize_generated_body("Architecture Decision Record", raw_body)

    assert sanitized == "# Architecture Decision Record\n\n## Overview\n\nConcrete content.\n"


def test_sanitize_generated_body_removes_meta_line_and_duplicate_title_after_heading():
    raw_body = """
# UX/UI Design

No existing file found. I'll now write the UX/UI design document based on the dependency documents.

# UX/UI Design

## Overview

Concrete content.
"""

    sanitized = generator_module._sanitize_generated_body("UX/UI Design", raw_body)

    assert sanitized == "# UX/UI Design\n\n## Overview\n\nConcrete content.\n"


def test_sanitize_generated_body_removes_meta_line_inside_body_section():
    raw_body = """
# UX/UI Design

## Overview

No existing file found. I'll now write the UX/UI design document based on the dependency documents.

Concrete content.
"""

    sanitized = generator_module._sanitize_generated_body("UX/UI Design", raw_body)

    assert sanitized == "# UX/UI Design\n\n## Overview\n\nConcrete content.\n"


def test_sanitize_generated_body_removes_codex_existing_file_meta_block():
    raw_body = """
# ADR: Multi-tenant Strategy

The existing file already has complete content with YAML frontmatter. I need to write just the document body.

The existing document already covers all the key areas from the requirements.

- Author権限問題 → covered
- テナント間データ分離 → covered

# ADR: Multi-tenant Strategy

## Overview

Concrete content.
"""

    sanitized = generator_module._sanitize_generated_body("ADR: Multi-tenant Strategy", raw_body)

    assert sanitized == "# ADR: Multi-tenant Strategy\n\n## Overview\n\nConcrete content.\n"


def test_sanitize_generated_body_removes_japanese_created_file_meta_line():
    raw_body = """
# 共有ドメインモデル詳細設計書

`docs/detailed_design/shared_domain_model.md` を作成しました。

## 1. Overview

Concrete content.

```mermaid
flowchart TD
    A[Auth] --> B[Tenant]
```
"""

    sanitized = generator_module._sanitize_generated_body(
        "共有ドメインモデル詳細設計書",
        raw_body,
        output_path="docs/detailed_design/shared_domain_model.md",
    )

    assert sanitized == (
        "# 共有ドメインモデル詳細設計書\n\n"
        "## 1. Overview\n\n"
        "Concrete content.\n\n"
        "```mermaid\n"
        "flowchart TD\n"
        "    A[Auth] --> B[Tenant]\n"
        "```\n"
    )


def test_sanitize_generated_body_rejects_unstructured_detailed_design_summary():
    raw_body = """
# 共有ドメインモデル詳細設計書

`docs/detailed_design/shared_domain_model.md` を作成しました。

主要な構成:

1. **共有 Enum/型定義**
2. **共有契約**
"""

    with pytest.raises(ValueError, match="missing section headings"):
        generator_module._sanitize_generated_body(
            "共有ドメインモデル詳細設計書",
            raw_body,
            output_path="docs/detailed_design/shared_domain_model.md",
        )


def test_sanitize_generated_body_rejects_detailed_design_without_mermaid():
    raw_body = """
# リクエストライフサイクル詳細設計書

## 1. Overview

Concrete content without diagrams.
"""

    with pytest.raises(ValueError, match="without Mermaid diagrams"):
        generator_module._sanitize_generated_body(
            "リクエストライフサイクル詳細設計書",
            raw_body,
            output_path="docs/detailed_design/request_lifecycle_sequences.md",
        )


# --- _resolve_ai_command per-command tests ---


def test_resolve_ai_command_uses_default_when_no_config():
    result = generator_module._resolve_ai_command({}, None)
    assert result == generator_module.DEFAULT_AI_COMMAND


def test_resolve_ai_command_cli_override_takes_precedence():
    config = {
        "ai_command": "global-ai",
        "ai_commands": {"generate": "per-cmd-ai"},
    }
    result = generator_module._resolve_ai_command(config, "cli-override", command_name="generate")
    assert result == "cli-override"


def test_resolve_ai_command_per_command_overrides_global():
    config = {
        "ai_command": "global-ai",
        "ai_commands": {"generate": "opus-for-design"},
    }
    result = generator_module._resolve_ai_command(config, None, command_name="generate")
    assert result == "opus-for-design"


def test_resolve_ai_command_falls_back_to_global_when_command_not_in_ai_commands():
    config = {
        "ai_command": "global-ai",
        "ai_commands": {"generate": "opus-for-design"},
    }
    result = generator_module._resolve_ai_command(config, None, command_name="implement")
    assert result == "global-ai"


def test_resolve_ai_command_falls_back_to_default_when_no_ai_commands_dict():
    config = {"ai_command": "global-ai"}
    result = generator_module._resolve_ai_command(config, None, command_name="generate")
    assert result == "global-ai"


def test_resolve_ai_command_rejects_empty_string():
    with pytest.raises(ValueError, match="ai_command must be a non-empty string"):
        generator_module._resolve_ai_command({"ai_command": ""}, None)


def test_resolve_ai_command_per_command_rejects_empty_string():
    config = {"ai_commands": {"generate": "  "}}
    with pytest.raises(ValueError, match="ai_command must be a non-empty string"):
        generator_module._resolve_ai_command(config, None, command_name="generate")


# --- _normalize_section_headings tests ---


def test_normalize_section_headings_noop_when_h2_exists():
    body = "# Title\n\n## Overview\n\nContent.\n"
    assert generator_module._normalize_section_headings(body) == body


def test_normalize_section_headings_promotes_h3_to_h2():
    body = "# Title\n\n### 1. Overview\n\nContent.\n\n### 2. Scope\n\nMore content.\n"
    result = generator_module._normalize_section_headings(body)
    assert "## 1. Overview" in result
    assert "## 2. Scope" in result
    assert "### " not in result


def test_normalize_section_headings_demotes_non_title_h1_to_h2():
    body = "# Title\n\n# Overview\n\nContent.\n\n# Architecture\n\nMore.\n"
    result = generator_module._normalize_section_headings(body)
    assert result.startswith("# Title\n")  # title preserved
    assert "## Overview" in result
    assert "## Architecture" in result


def test_normalize_section_headings_promotes_bold_pseudo_headings():
    body = "# Title\n\n**1. Overview**\n\nContent.\n\n**2. Acceptance Criteria**\n\nCriteria.\n"
    result = generator_module._normalize_section_headings(body)
    assert "## 1. Overview" in result
    assert "## 2. Acceptance Criteria" in result
    assert "**1." not in result


def test_normalize_section_headings_preserves_fenced_code_blocks():
    body = "# Title\n\n### Overview\n\n```bash\n### this is a comment\n```\n\n### Scope\n\nEnd.\n"
    result = generator_module._normalize_section_headings(body)
    assert "## Overview" in result
    assert "## Scope" in result
    assert "### this is a comment" in result  # inside fence, unchanged


def test_normalize_section_headings_returns_unchanged_when_no_patterns():
    body = "# Title\n\nJust plain text without any section structure.\n"
    assert generator_module._normalize_section_headings(body) == body


def test_sanitize_normalizes_h3_before_validation():
    """Integration: _sanitize_generated_body no longer rejects h3-only output."""
    raw_body = "# Acceptance Criteria\n\n### 1. Overview\n\nContent.\n\n### 2. Acceptance Criteria\n\nCriteria.\n\n### 3. Failure Criteria\n\nFailures.\n"
    result = generator_module._sanitize_generated_body("Acceptance Criteria", raw_body)
    assert "## 1. Overview" in result
    assert "## 2. Acceptance Criteria" in result
    assert "## 3. Failure Criteria" in result


# -- Test code output detection --

def test_is_test_code_output_detects_spec_ts():
    assert generator_module._is_test_code_output("tests/e2e/auth.spec.ts") is True
    assert generator_module._is_test_code_output("tests/e2e/browser-nav.spec.js") is True
    assert generator_module._is_test_code_output("tests/test_auth.test.py") is True
    # ``.e2e.ts`` is a genuine e2e test-output naming convention.
    assert generator_module._is_test_code_output("tests/e2e/tempconv.e2e.ts") is True
    assert generator_module._is_test_code_output("tests/e2e/flow.e2e.js") is True


def test_is_test_code_output_rejects_non_test_files():
    assert generator_module._is_test_code_output("docs/test/strategy.md") is False
    assert generator_module._is_test_code_output("tests/e2e/helpers/auth.ts") is False
    assert generator_module._is_test_code_output("src/app.ts") is False


def test_sanitize_generated_body_skips_markdown_checks_for_test_code():
    """Test code output should not be forced into Markdown structure."""
    code = 'import { test, expect } from "@playwright/test";\n\ntest("it works", async ({ page }) => {\n  await page.goto("/");\n});\n'
    result = generator_module._sanitize_generated_body("E2E Tests", code, output_path="tests/e2e/auth.spec.ts")
    assert "import" in result
    assert "# E2E Tests" not in result  # No Markdown heading injected


def test_render_document_uses_comment_headers_for_test_code():
    """Test code output should use // comment headers, not YAML frontmatter."""
    artifact = generator_module.WaveArtifact(
        wave=9,
        node_id="test:e2e-browser",
        output="tests/e2e/browser.spec.ts",
        title="Browser Tests",
        depends_on=[{"id": "design:auth", "relation": "depends_on"}],
        conventions=[],
    )
    body = 'import { test } from "@playwright/test";\n\ntest("smoke", async ({ page }) => {});\n'
    result = generator_module._render_document(artifact, [], [], body)
    assert result.startswith("// @generated-from: design:auth")
    assert "// @codd-node-id: test:e2e-browser" in result
    assert "---" not in result  # No YAML frontmatter
    assert "import" in result


def test_generate_test_document_includes_design_to_test_traceability(tmp_path, mock_ai_cli):
    """Test documents must include design-to-test traceability instructions.

    acceptance_criteria.md (node ``design:acceptance-criteria``) is NOT the
    canonical VB declarer (``docs/test/test_strategy.md``), so it receives the
    REFERENCE-ONLY traceability head: it must reference canonical VB ids rather
    than mint its own first-column ``VB-*`` table. The modality-driven E2E
    guidance below is unchanged.
    """
    project = _setup_project(tmp_path)

    generate_wave(project, 1)

    # Wave 1 includes acceptance_criteria.md which is a (reference-only) test doc.
    prompt = mock_ai_cli[0]["input"]
    assert "Design-to-test traceability" in prompt
    assert "REFERENCE-ONLY" in prompt
    assert "MUST NOT declare VB ids" in prompt
    assert "design-time `operation_flow` records as the authoritative source" in prompt
    assert "derived-state/read-model chain" in prompt
    assert "below/at/above-boundary assertions" in prompt
    assert "Actor-facing surface/copy coverage" in prompt
    assert "required visible labels/copy" in prompt
    assert "forbidden actions/links/copy patterns are absent" in prompt
    assert "idempotently reset those preconditions" in prompt
    assert "Run the whole selected suite, collect every failure" in prompt


def test_generated_browser_test_prompt_requires_surface_copy_assertions():
    artifact = generator_module.WaveArtifact(
        wave=9,
        node_id="test:browser-surface",
        output="tests/e2e/surface.browser.spec.ts",
        title="Browser Surface Tests",
        depends_on=[{"id": "design:ux", "relation": "validates"}],
        conventions=[],
    )
    dependency = generator_module.DependencyDocument(
        node_id="design:ux",
        path=Path("docs/design/ux.md"),
        content=(
            "## Actor-Facing Surface/Copy Obligations\n"
            "- Surface: sign-in\n"
            "- Required visible copy: operator console\n"
            "- Forbidden navigation: home link before authentication\n"
        ),
    )

    prompt = generator_module._build_generation_prompt(artifact, [dependency], [])

    assert "Actor-facing surface/copy coverage (CRITICAL)" in prompt
    assert "required visible labels/copy" in prompt
    assert "forbidden actions, links, or copy patterns are absent" in prompt
    assert "Do not accept generic smoke assertions" in prompt


def test_generation_prompt_states_project_language_for_non_test_docs():
    """Design/infra docs must explicitly anchor the target language.

    Regression test for a real greenfield dogfood failure: a Java project's
    generated infra/build_setup.md described a Python project (pyproject.toml,
    setuptools, importlib.metadata) because nothing in the prompt stated the
    target language outside the test-doc branch (_build_test_doc_block).
    """
    artifact = generator_module.WaveArtifact(
        wave=4,
        node_id="infra:build-setup",
        output="docs/infra/build_setup.md",
        title="Build & Test Infrastructure Setup",
        depends_on=[{"id": "design:system", "relation": "depends_on"}],
        conventions=[],
    )
    dependency = generator_module.DependencyDocument(
        node_id="design:system",
        path=Path("docs/design/system_design.md"),
        content="## Overview\nA dependency-free expression evaluator.\n",
    )

    prompt_java = generator_module._build_generation_prompt(
        artifact, [dependency], [], project_language="java"
    )
    prompt_unset = generator_module._build_generation_prompt(artifact, [dependency], [])

    assert "implementation language is java" in prompt_java
    assert "MUST use java conventions exclusively" in prompt_java
    assert "implementation language is" not in prompt_unset


def test_generation_prompt_states_test_framework_ground_truth_for_typescript():
    """Regression test for the 2026-07-03 TS ExprCalc greenfield dogfood failure:
    the generated docs/infra/build_setup.md confidently declared `node:test`
    "the target language's own standard unit-test runner" (reasoning from a
    "no third-party dependencies" project convention), even though the TS
    profile's scaffolded ``commands.verify`` always runs Vitest directly —
    which does NOT collect `node:test`-style files ("No test suite found in
    file ..."). The design doc must instead state Vitest as ground truth.
    """
    artifact = generator_module.WaveArtifact(
        wave=4,
        node_id="infra:build-setup",
        output="docs/infra/build_setup.md",
        title="Build & Test Infrastructure Setup",
        depends_on=[{"id": "design:system", "relation": "depends_on"}],
        conventions=[],
    )
    dependency = generator_module.DependencyDocument(
        node_id="design:system",
        path=Path("docs/design/system_design.md"),
        content="## Overview\nA dependency-free expression evaluator.\n",
    )

    prompt_ts = generator_module._build_generation_prompt(
        artifact, [dependency], [], project_language="typescript"
    )
    prompt_unset = generator_module._build_generation_prompt(artifact, [dependency], [])

    assert "test runner is Vitest" in prompt_ts
    assert 'import { describe, it, expect } from "vitest"' in prompt_ts
    assert "node:test" in prompt_ts  # named explicitly as what NOT to use
    assert "not a style preference" in prompt_ts
    assert "test runner is" not in prompt_unset


def test_generation_prompt_states_test_framework_ground_truth_for_python():
    artifact = generator_module.WaveArtifact(
        wave=1,
        node_id="test:test-strategy",
        output="docs/test/test_strategy.md",
        title="Test Strategy",
        depends_on=[],
        conventions=[],
    )
    dependency = generator_module.DependencyDocument(
        node_id="req:requirements",
        path=Path("docs/requirements/requirements.md"),
        content="## Overview\nRequirements.\n",
    )

    prompt = generator_module._build_generation_prompt(
        artifact, [dependency], [], project_language="python"
    )

    assert "test runner is pytest" in prompt
    assert "import pytest" in prompt


def test_render_document_strips_markdown_fences_from_test_code():
    """If AI wraps code in ```typescript fences, they should be stripped."""
    artifact = generator_module.WaveArtifact(
        wave=9,
        node_id="test:e2e",
        output="tests/e2e/auth.spec.ts",
        title="Auth Tests",
        depends_on=[],
        conventions=[],
    )
    body = '```typescript\nimport { test } from "@playwright/test";\ntest("ok", async () => {});\n```'
    result = generator_module._render_document(artifact, [], [], body)
    assert "```" not in result
    assert "import" in result


def test_render_document_lifts_operation_flow_yaml_block_into_frontmatter():
    artifact = generator_module.WaveArtifact(
        wave=2,
        node_id="design:operations",
        output="docs/design/operations.md",
        title="Operations",
        depends_on=[{"id": "req:system", "relation": "derives_from"}],
        conventions=[],
    )
    body = """# Operations

## 1. Overview

### Operational Behavior Model

```yaml
operation_flow:
  operations:
    - id: assign_item
      actor: operator
      verb: assign
      target: work_item
      preconditions: [workspace exists]
      expected_outcomes: [assignment persists]
      forbidden_actors: [viewer]
```
"""

    result = generator_module._render_document(artifact, [], [], body)
    frontmatter = yaml.safe_load(result.split("---", 2)[1])

    operation = frontmatter["codd"]["operation_flow"]["operations"][0]
    assert operation["id"] == "assign_item"
    assert operation["actor"] == "operator"
    assert operation["expected_outcomes"] == ["assignment persists"]


@pytest.mark.parametrize(
    ("node_id", "output", "modality"),
    [
        pytest.param("design:system", "docs/design/system_design.md", "browser", id="design"),
        pytest.param("test:test-strategy", "docs/test/test_strategy.md", "browser", id="browser"),
        pytest.param("test:test-strategy", "docs/test/test_strategy.md", "cli", id="cli"),
        pytest.param("test:test-strategy", "docs/test/test_strategy.md", "device", id="device"),
        pytest.param("test:e2e", "tests/e2e/test_reporter.py", "none", id="direct-test-code"),
    ],
)
def test_generation_prompt_omits_measurement_rule(node_id, output, modality):
    artifact = generator_module.WaveArtifact(
        wave=1,
        node_id=node_id,
        output=output,
        title="Reporter",
        depends_on=[],
        conventions=[],
    )
    prompt = generator_module._build_generation_prompt(
        artifact, [], [], capabilities=ProjectCapabilities(e2e_modality=modality),
    )

    for fragment in (
        "Elapsed-time measurements",
        "When a test measures elapsed time",
        "define for each measured target",
        "where it starts, completes and fails",
        "one monotonic clock",
        "check the measuring path against a response",
        "one that is cut short",
        "which events and times the verdict used",
    ):
        assert fragment not in prompt


@pytest.mark.parametrize("condition", ["expired", "invalid"])
def test_browser_strategy_prompt_allows_rejecting_an_untrusted_certificate(condition):
    requirement = (
        "Wait for the server to report readiness over its startup channel. "
        f"The only HTTPS test server presents a deliberately {condition} certificate. "
        "Verify that the client rejects the certificate on its first TLS connection."
    )
    dependency = generator_module.DependencyDocument(
        "req:tls", Path("docs/requirements/tls.md"), requirement,
    )
    artifact = generator_module.WaveArtifact(
        wave=1,
        node_id="test:test-strategy",
        output="docs/test/test_strategy.md",
        title="Test Strategy",
        depends_on=[{"id": dependency.node_id, "relation": "validates"}],
        conventions=[],
    )
    prompt = generator_module._build_generation_prompt(artifact, [dependency], [])

    heading = "E2E Runtime Environment rules:"
    runtime_section = prompt.split(f"{heading}\n", 1)[1].split("\n\n", 1)[0]
    rule = f"\n- {CERTIFICATE_SENTENCE}"
    assert prompt.count(heading) == 1
    assert prompt.count(CERTIFICATE_SENTENCE) == 1
    assert runtime_section.endswith(rule)
    assert requirement in prompt
    assert "(invalid, expired, short-lived or rotated) are exempt." in runtime_section
    assert "The readiness check connects with the same trust" not in prompt


def test_python_http_strategy_rules_include_the_certificate_sentence_once():
    lines = generator_module._python_http_e2e_doc_lines()

    assert lines.count(f"- {CERTIFICATE_SENTENCE}") == 1
    text = "\n".join(lines)
    assert text.count(CERTIFICATE_SENTENCE) == 1
    assert "The readiness check connects with the same trust" not in text


@pytest.mark.parametrize(
    ("node_id", "output", "modality"),
    [
        pytest.param("design:system", "docs/design/system_design.md", "browser", id="design"),
        pytest.param("test:test-strategy", "docs/test/test_strategy.md", "cli", id="cli"),
        pytest.param("test:test-strategy", "docs/test/test_strategy.md", "device", id="device"),
        pytest.param("test:test-strategy", "docs/test/test_strategy.md", "none", id="integration-only"),
        pytest.param("test:e2e", "tests/e2e/reporter.spec.ts", "browser", id="direct-test-code"),
    ],
)
def test_other_generation_prompts_omit_the_certificate_rule(node_id, output, modality):
    artifact = generator_module.WaveArtifact(
        wave=1,
        node_id=node_id,
        output=output,
        title="Reporter",
        depends_on=[],
        conventions=[],
    )
    prompt = generator_module._build_generation_prompt(
        artifact, [], [], capabilities=ProjectCapabilities(e2e_modality=modality),
    )

    for fragment in CERTIFICATE_FRAGMENTS:
        assert fragment not in prompt
