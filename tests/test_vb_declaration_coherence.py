"""Regression tests: single canonical VB declaration doc + collision validator.

A greenfield autopilot run could fail the implement-stage VB coverage gate
because the generator emitted TWO test docs that mint the SAME ``VB-NN`` numeric
namespace with DIFFERENT semantics (acceptance_criteria.md coarse VB-01..15 and
test_strategy.md granular VB-01..45). Some behaviors got two ids; the implementer
can only mark one, so the duplicate stayed permanently uncovered → 100% coverage
was structurally impossible.

The fix makes the generator role-aware (only ``docs/test/test_strategy.md``
declares VBs; other test docs reference them), adds a deterministic
declaration-coherence validator (generator post-check + ``codd check``), and
keeps coverage SEMANTICS untouched (no alias tables, no similarity auto-cover).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

import codd.generator as generator_module
from codd.cli import main
from codd.generator import _build_test_doc_block
from codd.project_types import ProjectCapabilities
from codd.verifiable_behavior_audit import (
    VerifiableBehavior,
    is_canonical_vb_doc,
    parse_vb_table,
    validate_vb_declarations,
)

CLI_CAPS = ProjectCapabilities(
    user_interface=False,
    network_surface="none",
    e2e_modality="cli",
    long_running_service=False,
)


def _join(lines: list[str]) -> str:
    return "\n".join(lines)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Canonical-doc detection
# ---------------------------------------------------------------------------


def test_is_canonical_vb_doc_matches_test_strategy_by_node_or_path():
    assert is_canonical_vb_doc(node_id="test:test-strategy")
    assert is_canonical_vb_doc(output_path="docs/test/test_strategy.md")
    assert is_canonical_vb_doc(output_path="/abs/project/docs/test/test_strategy.md")
    # Acceptance criteria (any spelling) is NOT canonical.
    assert not is_canonical_vb_doc(node_id="test:acceptance-criteria")
    assert not is_canonical_vb_doc(node_id="design:acceptance-criteria")
    assert not is_canonical_vb_doc(output_path="docs/test/acceptance_criteria.md")
    assert not is_canonical_vb_doc()  # nothing known


# ---------------------------------------------------------------------------
# Test 1: role-aware prompt builder selects the right head per node/path
# ---------------------------------------------------------------------------


def test_canonical_doc_head_declares_vb_first_column():
    text = _join(
        _build_test_doc_block(
            CLI_CAPS,
            node_id="test:test-strategy",
            output_path="docs/test/test_strategy.md",
        )
    )
    assert "canonical" in text.lower()
    assert "FIRST column is a stable verifiable-behavior id" in text
    # Canonical doc OWNS the namespace.
    assert "single canonical owner" in text


def test_reference_doc_head_forbids_first_column_vb():
    text = _join(
        _build_test_doc_block(
            CLI_CAPS,
            node_id="design:acceptance-criteria",
            output_path="docs/test/acceptance_criteria.md",
        )
    )
    assert "REFERENCE-ONLY" in text
    assert "MUST NOT declare VB ids" in text
    # Explicitly forbids VB in the first column; tells it to use AC-* instead.
    assert "first column" in text.lower()
    assert "AC-" in text
    # It must NOT instruct the model to make a first-column VB declaration table.
    assert "FIRST column is a stable verifiable-behavior id" not in text


def test_unknown_role_defaults_to_canonical_for_back_compat():
    # No node_id/output_path (legacy/single-doc callers) → canonical declaration.
    text = _join(_build_test_doc_block(CLI_CAPS))
    assert "FIRST column is a stable verifiable-behavior id" in text
    assert "canonical" in text.lower()


# ---------------------------------------------------------------------------
# Test 1 (integration): generated docs use the role-aware guidance
# ---------------------------------------------------------------------------


_PROJECT_YAML = """
ai_command: "mock-ai --print"
project:
  name: vb-coherence
  language: python
project_type: cli
scan:
  source_dirs: []
  doc_dirs:
    - "docs/requirements/"
    - "docs/test/"
  exclude: []
graph:
  store: jsonl
  path: codd/scan
wave_config:
  "1":
    - node_id: "test:test-strategy"
      output: "docs/test/test_strategy.md"
      title: "Test Strategy"
      depends_on:
        - id: "req:app"
          relation: derives_from
    - node_id: "test:acceptance-criteria"
      output: "docs/test/acceptance_criteria.md"
      title: "Acceptance Criteria"
      depends_on:
        - id: "req:app"
          relation: derives_from
"""


def _setup_cli_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True)
    (codd_dir / "codd.yaml").write_text(_PROJECT_YAML, encoding="utf-8")
    _write(
        project / "docs" / "requirements" / "app.md",
        "---\ncodd:\n  node_id: \"req:app\"\n  type: \"requirement\"\n---\n\n"
        "# App\n\n## Scope\n\nA CLI todo app.\n",
    )
    return project


@pytest.fixture
def role_aware_mock_ai(monkeypatch):
    """Mock AI that emits canonical VB rows ONLY when the prompt asks it to.

    Mirrors a well-behaved model: the canonical doc declares first-column VBs;
    the reference-only doc emits an AC-* mapping table with VB ids in a later
    column. The branch keys off the role-aware guidance in the prompt.
    """

    import subprocess

    def fake_run(command, *, input, capture_output, text, check, **kwargs):
        if "single canonical owner" in input:
            stdout = (
                "# Test Strategy\n\n## Traceability\n\n"
                "| VB | Behavior | Scenario |\n| --- | --- | --- |\n"
                "| VB-01 | add creates a task | test_add |\n"
                "| VB-02 | done exits nonzero on missing id | test_missing |\n"
            )
        elif "REFERENCE-ONLY" in input:
            stdout = (
                "# Acceptance Criteria\n\n## Mapping\n\n"
                "| AC ID | Acceptance criterion | Canonical VBs |\n| --- | --- | --- |\n"
                "| AC-01 | A task can be added | VB-01 |\n"
                "| AC-02 | Missing id errors out | VB-02 |\n"
            )
        else:
            stdout = "# Doc\n\n## Overview\n\nContent.\n"
        return subprocess.CompletedProcess(args=command, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)
    return fake_run


def test_generated_acceptance_has_no_first_column_vb_strategy_is_sole_declarer(
    tmp_path, role_aware_mock_ai
):
    project = _setup_cli_project(tmp_path)
    result = CliRunner().invoke(main, ["generate", "--wave", "1", "--path", str(project)])
    assert result.exit_code == 0, result.output

    strategy = (project / "docs" / "test" / "test_strategy.md").read_text(encoding="utf-8")
    acceptance = (project / "docs" / "test" / "acceptance_criteria.md").read_text(encoding="utf-8")

    # test_strategy.md is the sole canonical declarer.
    strategy_vbs = parse_vb_table(strategy)
    assert {b.vb_id for b in strategy_vbs} == {"VB-01", "VB-02"}
    # acceptance_criteria.md has NO first-column VB-* rows (references only).
    assert parse_vb_table(acceptance) == []
    assert "VB-01" in acceptance  # still references canonical ids (later column)
    assert "AC-01" in acceptance


# ---------------------------------------------------------------------------
# Test 4: generator post-check (per-doc + cross-doc)
# ---------------------------------------------------------------------------


def test_generation_fails_when_noncanonical_doc_declares_first_column_vb(tmp_path, monkeypatch):
    """A rogue model that puts VB-* in acceptance_criteria's first column FAILs."""
    import subprocess

    project = _setup_cli_project(tmp_path)

    def rogue_run(command, *, input, capture_output, text, check, **kwargs):
        # BOTH docs mint first-column VB tables (the original bug).
        if "single canonical owner" in input:
            stdout = (
                "# Test Strategy\n\n## T\n\n| VB | B |\n| --- | --- |\n| VB-01 | a |\n"
            )
        else:
            stdout = (
                "# Acceptance Criteria\n\n## T\n\n| VB | B |\n| --- | --- |\n| VB-01 | a |\n"
            )
        return subprocess.CompletedProcess(args=command, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(generator_module.subprocess, "run", rogue_run)
    result = CliRunner().invoke(main, ["generate", "--wave", "1", "--path", str(project)])
    assert result.exit_code != 0
    combined = result.output + (str(result.exception) if result.exception else "")
    assert "VB" in combined


def test_validator_collision_same_id_different_desc_is_error():
    by_doc = {
        "docs/test/acceptance_criteria.md": [
            VerifiableBehavior("VB-01", "creates a new task", "docs/test/acceptance_criteria.md")
        ],
        "docs/test/test_strategy.md": [
            VerifiableBehavior("VB-01", "todo add exits with code 0", "docs/test/test_strategy.md")
        ],
    }
    issues = validate_vb_declarations(by_doc, strict=False)
    collisions = [i for i in issues if i.kind == "collision"]
    assert len(collisions) == 1
    assert collisions[0].severity == "error"
    assert collisions[0].vb_id == "VB-01"


def test_validator_same_id_same_desc_is_warning_not_error():
    by_doc = {
        "docs/test/a.md": [VerifiableBehavior("VB-01", "creates a task", "docs/test/a.md")],
        "docs/test/test_strategy.md": [
            VerifiableBehavior("VB-01", "creates a task", "docs/test/test_strategy.md")
        ],
    }
    issues = validate_vb_declarations(by_doc, strict=False)
    assert [i.kind for i in issues].count("duplicate") == 1
    duplicate = next(i for i in issues if i.kind == "duplicate")
    assert duplicate.severity == "warning"
    assert not any(i.severity == "error" for i in issues)


def test_validator_noncanonical_first_column_strict_error_else_warning():
    by_doc = {
        "docs/test/acceptance_criteria.md": [
            VerifiableBehavior("VB-07", "missing id errors", "docs/test/acceptance_criteria.md")
        ],
    }
    strict = validate_vb_declarations(by_doc, strict=True)
    assert any(i.kind == "noncanonical_declaration" and i.severity == "error" for i in strict)

    lenient = validate_vb_declarations(by_doc, strict=False)
    noncanon = [i for i in lenient if i.kind == "noncanonical_declaration"]
    assert len(noncanon) == 1
    assert noncanon[0].severity == "warning"
    assert "test_strategy.md" in noncanon[0].message  # migration guidance


def test_validator_clean_canonical_only_has_no_issues():
    by_doc = {
        "docs/test/test_strategy.md": [
            VerifiableBehavior("VB-01", "a", "docs/test/test_strategy.md"),
            VerifiableBehavior("VB-02", "b", "docs/test/test_strategy.md"),
        ],
        "docs/test/acceptance_criteria.md": [],  # references only, no VB rows
    }
    assert validate_vb_declarations(by_doc, strict=True) == []


def test_validator_does_not_alias_sibling_vbs():
    """Anti-false-green: VB-14 and VB-39 (semantic siblings) are NOT merged.

    Different ids with different descriptions in different docs are independent
    declarations — the validator must never treat one as covering the other.
    """
    by_doc = {
        "docs/test/acceptance_criteria.md": [
            VerifiableBehavior("VB-14", "no GUI/browser/web/API", "docs/test/acceptance_criteria.md")
        ],
        "docs/test/test_strategy.md": [
            VerifiableBehavior("VB-39", "no Selenium/Playwright/HTTP", "docs/test/test_strategy.md")
        ],
    }
    issues = validate_vb_declarations(by_doc, strict=False)
    # No collision (different ids), but the non-canonical first-column smell IS flagged.
    assert not any(i.kind == "collision" for i in issues)
    assert any(i.kind == "noncanonical_declaration" for i in issues)


# ---------------------------------------------------------------------------
# Test 5: verifier-side validation via `codd check`
# ---------------------------------------------------------------------------


def _check_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / "codd").mkdir(parents=True)
    (project / "codd" / "codd.yaml").write_text(
        "project_name: demo\nscan:\n  doc_dirs:\n    - docs/\n", encoding="utf-8"
    )
    return project


def test_codd_check_errors_on_colliding_vb_declarations(tmp_path):
    project = _check_project(tmp_path)
    _write(
        project / "docs" / "test" / "acceptance_criteria.md",
        "| ID | D |\n| --- | --- |\n| VB-01 | creates a task |\n",
    )
    _write(
        project / "docs" / "test" / "test_strategy.md",
        "| ID | D |\n| --- | --- |\n| VB-01 | add exits with code 0 |\n",
    )
    result = CliRunner().invoke(main, ["check", "--path", str(project)])
    assert result.exit_code != 0
    assert "vb declarations" in result.output
    assert "incoherent" in result.output.lower() or "different descriptions" in result.output.lower()


def test_codd_check_warns_on_brownfield_noncanonical_declaration(tmp_path):
    project = _check_project(tmp_path)
    # Only a non-canonical doc declares VBs (no collision) → WARNING, gate passes.
    _write(
        project / "docs" / "test" / "acceptance_criteria.md",
        "| ID | D |\n| --- | --- |\n| VB-07 | missing id errors |\n",
    )
    result = CliRunner().invoke(main, ["check", "--path", str(project)])
    # The non-canonical-declaration smell is advisory (doctor) with migration
    # guidance, NOT the VB-declarations gate error.
    assert "not the canonical VB document" in result.output
    assert "FAIL — 1 colliding verifiable-behavior" not in result.output
    assert "PASS — coherent VB declarations" in result.output  # gate passes


def test_codd_check_passes_clean_canonical_project(tmp_path):
    project = _check_project(tmp_path)
    _write(
        project / "docs" / "test" / "test_strategy.md",
        "| ID | D |\n| --- | --- |\n| VB-01 | a |\n| VB-02 | b |\n",
    )
    result = CliRunner().invoke(main, ["check", "--path", str(project)])
    assert "vb declarations" in result.output
    assert "PASS — coherent VB declarations" in result.output
