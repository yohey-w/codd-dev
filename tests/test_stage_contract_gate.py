"""Phase 3: stage completion gate wired into plan/generate/implement/verify.

A stage's COMPLETION is redefined: when a project enables an artifact contract
that declares a stage, that stage's command only succeeds if the stage's
required artifacts actually exist and validate. These tests prove the gate is:

* opt-in / non-breaking (disabled or absent contract = zero behavior change),
* satisfied = a one-line confirmation + normal exit,
* unsatisfied = a clear failure message + non-zero exit,
* escapable (per-command --no-contract-gate flag AND config gate_stages:false).

All fixtures are synthetic and project-agnostic (no project/domain names).
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
import yaml

import codd.generator as generator_module
import codd.implementer as implementer_module
import codd.planner as planner_module
from codd.artifact_contract import enforce_stage_completion, load_catalog
from codd.cli import main


# ---------------------------------------------------------------------------
# project scaffolding
# ---------------------------------------------------------------------------
def _write_project(tmp_path: Path, *, contract: dict | None = None) -> Path:
    project = tmp_path / "project"
    (project / "codd").mkdir(parents=True)
    config: dict = {
        "project": {"name": "demo", "language": "typescript"},
        "ai_command": "mock-ai --print",
        "wave_config": {"1": [{"id": "a", "path": "docs/design/a.md"}]},
        "scan": {"source_dirs": ["src/"], "doc_dirs": ["docs/design/"], "config_files": [], "exclude": []},
    }
    if contract is not None:
        config["artifact_contract"] = contract
    (project / "codd" / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    return project


def _write_source(project: Path) -> None:
    src = project / "src" / "mod.ts"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("export const x = 1;\n", encoding="utf-8")


def _write_design(project: Path) -> None:
    doc = project / "docs" / "design" / "a.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("---\ncodd:\n  node_id: design:a\n---\n\n# A\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# core helper: enforce_stage_completion (reuses Phase 1 verify_contract)
# ---------------------------------------------------------------------------
def test_helper_noop_when_contract_absent(tmp_path):
    project = _write_project(tmp_path, contract=None)
    config = {}
    assert enforce_stage_completion(project, "implement", config=config) is None


def test_helper_noop_when_disabled(tmp_path):
    config = {"artifact_contract": {"enabled": False, "stages": {"implement": ["source"]}}}
    assert enforce_stage_completion(tmp_path, "implement", config=config) is None


def test_helper_noop_when_stage_not_declared(tmp_path):
    config = {"artifact_contract": {"enabled": True, "stages": {"generate": ["design_spec"]}}}
    assert enforce_stage_completion(tmp_path, "implement", config=config) is None


def test_helper_returns_report_when_enabled_and_declared(tmp_path):
    _write_source(tmp_path)
    config = {"artifact_contract": {"enabled": True, "stages": {"implement": ["source"]}}}
    report = enforce_stage_completion(tmp_path, "implement", config=config)
    assert report is not None
    assert report.stage == "implement"
    assert report.passed is True


def test_helper_reports_failure_for_missing_artifact(tmp_path):
    config = {"artifact_contract": {"enabled": True, "stages": {"implement": ["source"]}}}
    report = enforce_stage_completion(tmp_path, "implement", config=config)
    assert report is not None
    assert report.passed is False
    assert [c.artifact_id for c in report.failures] == ["source"]


def test_helper_reuses_phase1_verify(tmp_path):
    # Same verdict as verify_contract directly = it reuses Phase 1 machinery.
    _write_source(tmp_path)
    from codd.artifact_contract import ArtifactContract, verify_contract

    catalog = load_catalog()
    contract = ArtifactContract(enabled=True, stages={"implement": ("source",)})
    direct = verify_contract(catalog, contract, tmp_path, stage="implement").stages[0]
    via_helper = enforce_stage_completion(
        tmp_path,
        "implement",
        config={"artifact_contract": {"enabled": True, "stages": {"implement": ["source"]}}},
    )
    assert via_helper.passed == direct.passed


# ---------------------------------------------------------------------------
# implement command
# ---------------------------------------------------------------------------
def _patch_implement(monkeypatch):
    monkeypatch.setattr(implementer_module, "implement_tasks", lambda *a, **k: [])


def test_implement_no_behavior_change_when_contract_absent(tmp_path, monkeypatch):
    project = _write_project(tmp_path, contract=None)
    _patch_implement(monkeypatch)
    result = CliRunner().invoke(main, ["implement", "--path", str(project)])
    assert result.exit_code == 0, result.output
    assert "contract" not in result.output.lower()


def test_implement_no_behavior_change_when_disabled(tmp_path, monkeypatch):
    project = _write_project(
        tmp_path, contract={"enabled": False, "stages": {"implement": ["source"]}}
    )
    _patch_implement(monkeypatch)
    result = CliRunner().invoke(main, ["implement", "--path", str(project)])
    assert result.exit_code == 0, result.output
    assert "contract satisfied" not in result.output


def test_implement_passes_when_artifact_present(tmp_path, monkeypatch):
    project = _write_project(
        tmp_path, contract={"enabled": True, "stages": {"implement": ["source"]}}
    )
    _write_source(project)
    _patch_implement(monkeypatch)
    result = CliRunner().invoke(main, ["implement", "--path", str(project)])
    assert result.exit_code == 0, result.output
    assert "stage 'implement' contract satisfied: 1/1 artifact(s)" in result.output


def test_implement_fails_when_artifact_missing(tmp_path, monkeypatch):
    project = _write_project(
        tmp_path, contract={"enabled": True, "stages": {"implement": ["source"]}}
    )
    _patch_implement(monkeypatch)
    result = CliRunner().invoke(main, ["implement", "--path", str(project)])
    assert result.exit_code == 1, result.output
    assert "INCOMPLETE" in result.output
    assert "source" in result.output


def test_implement_escape_hatch_flag(tmp_path, monkeypatch):
    project = _write_project(
        tmp_path, contract={"enabled": True, "stages": {"implement": ["source"]}}
    )
    _patch_implement(monkeypatch)
    result = CliRunner().invoke(
        main, ["implement", "--path", str(project), "--no-contract-gate"]
    )
    assert result.exit_code == 0, result.output
    assert "INCOMPLETE" not in result.output


def test_implement_escape_hatch_config(tmp_path, monkeypatch):
    project = _write_project(
        tmp_path,
        contract={"enabled": True, "gate_stages": False, "stages": {"implement": ["source"]}},
    )
    _patch_implement(monkeypatch)
    result = CliRunner().invoke(main, ["implement", "--path", str(project)])
    assert result.exit_code == 0, result.output
    assert "INCOMPLETE" not in result.output


# ---------------------------------------------------------------------------
# generate command
# ---------------------------------------------------------------------------
def _patch_generate(monkeypatch, project: Path):
    monkeypatch.setattr(
        generator_module,
        "_load_project_config",
        lambda root: {"wave_config": {"1": [{"id": "a"}]}},
    )
    monkeypatch.setattr(generator_module, "generate_wave", lambda *a, **k: [])


def test_generate_no_behavior_change_when_absent(tmp_path, monkeypatch):
    project = _write_project(tmp_path, contract=None)
    _patch_generate(monkeypatch, project)
    result = CliRunner().invoke(main, ["generate", "--wave", "1", "--path", str(project)])
    assert result.exit_code == 0, result.output
    assert "contract" not in result.output.lower()


def test_generate_passes_when_design_present(tmp_path, monkeypatch):
    project = _write_project(
        tmp_path, contract={"enabled": True, "stages": {"generate": ["design_spec"]}}
    )
    _write_design(project)
    _patch_generate(monkeypatch, project)
    result = CliRunner().invoke(main, ["generate", "--wave", "1", "--path", str(project)])
    assert result.exit_code == 0, result.output
    assert "stage 'generate' contract satisfied" in result.output


def test_generate_fails_when_design_missing(tmp_path, monkeypatch):
    project = _write_project(
        tmp_path, contract={"enabled": True, "stages": {"generate": ["design_spec"]}}
    )
    _patch_generate(monkeypatch, project)
    result = CliRunner().invoke(main, ["generate", "--wave", "1", "--path", str(project)])
    assert result.exit_code == 1, result.output
    assert "INCOMPLETE" in result.output
    assert "design_spec" in result.output


def test_generate_escape_hatch_flag(tmp_path, monkeypatch):
    project = _write_project(
        tmp_path, contract={"enabled": True, "stages": {"generate": ["design_spec"]}}
    )
    _patch_generate(monkeypatch, project)
    result = CliRunner().invoke(
        main, ["generate", "--wave", "1", "--path", str(project), "--no-contract-gate"]
    )
    assert result.exit_code == 0, result.output
    assert "INCOMPLETE" not in result.output


# ---------------------------------------------------------------------------
# plan --init command
# ---------------------------------------------------------------------------
def _patch_plan_init(monkeypatch, project: Path):
    class _Res:
        config_path = str(project / "codd" / "codd.yaml")
        requirement_paths = ["docs/requirements.md"]
        wave_config = {"1": [{"id": "a"}]}

    monkeypatch.setattr(planner_module, "plan_init", lambda *a, **k: _Res())


def _write_requirements(project: Path) -> None:
    doc = project / "docs" / "requirements.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# Requirements\n\n- do a thing\n", encoding="utf-8")


def test_plan_init_no_behavior_change_when_absent(tmp_path, monkeypatch):
    project = _write_project(tmp_path, contract=None)
    _patch_plan_init(monkeypatch, project)
    result = CliRunner().invoke(main, ["plan", "--init", "--path", str(project)])
    assert result.exit_code == 0, result.output
    assert "contract" not in result.output.lower()


def test_plan_init_passes_when_requirements_present(tmp_path, monkeypatch):
    project = _write_project(
        tmp_path, contract={"enabled": True, "stages": {"plan": ["requirements"]}}
    )
    _write_requirements(project)
    _patch_plan_init(monkeypatch, project)
    result = CliRunner().invoke(main, ["plan", "--init", "--path", str(project)])
    assert result.exit_code == 0, result.output
    assert "stage 'plan' contract satisfied" in result.output


def test_plan_init_fails_when_requirements_missing(tmp_path, monkeypatch):
    project = _write_project(
        tmp_path, contract={"enabled": True, "stages": {"plan": ["requirements"]}}
    )
    _patch_plan_init(monkeypatch, project)
    result = CliRunner().invoke(main, ["plan", "--init", "--path", str(project)])
    assert result.exit_code == 1, result.output
    assert "INCOMPLETE" in result.output
    assert "requirements" in result.output


def test_plan_init_escape_hatch_flag(tmp_path, monkeypatch):
    project = _write_project(
        tmp_path, contract={"enabled": True, "stages": {"plan": ["requirements"]}}
    )
    _patch_plan_init(monkeypatch, project)
    result = CliRunner().invoke(
        main, ["plan", "--init", "--path", str(project), "--no-contract-gate"]
    )
    assert result.exit_code == 0, result.output
    assert "INCOMPLETE" not in result.output


# ---------------------------------------------------------------------------
# verify command (verify stage only gated when declared)
# ---------------------------------------------------------------------------
def _patch_verify_pass(monkeypatch):
    import codd.cli as cli_module

    class _VR:
        passed = True
        exit_code = 0
        check_results = []
        runtime_results = []

    monkeypatch.setattr(cli_module, "_run_verify_once", lambda **k: _VR())


def test_verify_no_behavior_change_when_verify_stage_absent(tmp_path, monkeypatch):
    # Contract enabled but declares no verify stage -> gate is a no-op.
    project = _write_project(
        tmp_path, contract={"enabled": True, "stages": {"implement": ["source"]}}
    )
    _patch_verify_pass(monkeypatch)
    result = CliRunner().invoke(main, ["verify", "--path", str(project)])
    assert result.exit_code == 0, result.output
    assert "contract satisfied" not in result.output


def test_verify_passes_when_artifact_present(tmp_path, monkeypatch):
    project = _write_project(
        tmp_path, contract={"enabled": True, "stages": {"verify": ["coverage_report"]}}
    )
    cov = project / "coverage.json"
    cov.write_text('{"ok": true}', encoding="utf-8")
    _patch_verify_pass(monkeypatch)
    result = CliRunner().invoke(main, ["verify", "--path", str(project)])
    assert result.exit_code == 0, result.output
    assert "stage 'verify' contract satisfied" in result.output


def test_verify_fails_when_artifact_missing(tmp_path, monkeypatch):
    project = _write_project(
        tmp_path, contract={"enabled": True, "stages": {"verify": ["coverage_report"]}}
    )
    _patch_verify_pass(monkeypatch)
    result = CliRunner().invoke(main, ["verify", "--path", str(project)])
    assert result.exit_code == 1, result.output
    assert "INCOMPLETE" in result.output
    assert "coverage_report" in result.output


def test_verify_escape_hatch_flag(tmp_path, monkeypatch):
    project = _write_project(
        tmp_path, contract={"enabled": True, "stages": {"verify": ["coverage_report"]}}
    )
    _patch_verify_pass(monkeypatch)
    result = CliRunner().invoke(
        main, ["verify", "--path", str(project), "--no-contract-gate"]
    )
    assert result.exit_code == 0, result.output
    assert "INCOMPLETE" not in result.output
