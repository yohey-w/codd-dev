"""FX3 — closing the verify false-green with execution evidence.

The originating incident (2026-06 real-AI greenfield dogfood):
``run_standalone_verify`` returned ``passed=True`` for a project containing
10 syntactically broken Python files, because only structural DAG checks ran
— no test command was configured or detected, ``runtime_results`` was empty,
and "nothing was executed" silently counted as PASS.

These tests pin the fix:

* source-integrity parse check (deterministic, stdlib-only) FAILS verification
  naming the broken files — this alone catches the dogfood disaster;
* a detected/configured test command is actually RUN; nonzero exit fails
  verification with a repair-loop-consumable failure shape;
* the honesty rule: "passed but executed nothing" is a pass-WITH-WARNING for
  plain verify (brownfield/CI configs may be intentionally structural-only)
  and a stage FAILURE for the greenfield autopilot;
* ``verify.allow_structural_only: true`` is the explicit opt-out.
"""

from __future__ import annotations

import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from codd.dag import DAG
from codd.greenfield.pipeline import StageError, _certify_verify_executed
from codd.repair import verify_runner as verify_runner_module
from codd.repair.loop import _violations_from_verify_result
from codd.repair.schema import VerificationFailureReport
from codd.repair.verify_runner import (
    STRUCTURAL_ONLY_WARNING,
    VerificationResult,
    VerifyRunner,
    structural_only_allowed,
)


@dataclass
class _CheckResult:
    check_name: str
    severity: str = "red"
    passed: bool = True
    message: str = ""


def _patch_dag_pipeline_green(monkeypatch) -> None:
    """Structural DAG checks all green — the dogfood precondition."""
    monkeypatch.setattr(verify_runner_module, "load_dag_settings", lambda project_root, settings: settings)
    monkeypatch.setattr(verify_runner_module, "build_dag", lambda project_root, settings: DAG())
    monkeypatch.setattr(
        verify_runner_module,
        "run_checks",
        lambda *args, **kwargs: [_CheckResult("node_completeness")],
    )


def _settings(**verify: object) -> dict:
    settings: dict = {"project": {"type": "generic"}, "scan": {"source_dirs": ["src"]}}
    if verify:
        settings["verify"] = dict(verify)
    return settings


PYTEST_COMMAND = f"{sys.executable} -m pytest --tb=short -q -p no:cacheprovider"


# ═══════════════════════════════════════════════════════════
# Source integrity (deterministic parse check)
# ═══════════════════════════════════════════════════════════

def test_broken_python_source_fails_verify_naming_the_file(tmp_path: Path, monkeypatch) -> None:
    _patch_dag_pipeline_green(monkeypatch)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "broken.py").write_text("def broken(:\n", encoding="utf-8")

    result = VerifyRunner(tmp_path, _settings()).run()

    assert result.passed is False
    integrity = [item for item in result.failures if item.check_name == "source_integrity"]
    assert len(integrity) == 1
    assert "src/broken.py" in integrity[0].message
    assert "not valid PY" in integrity[0].message
    # repair-loop mapping: the broken impl file lands in failed_nodes
    assert "src/broken.py" in result.failure.failed_nodes
    assert result.source_integrity.startswith("1 parse error(s)")


def test_dogfood_scenario_structural_green_but_sources_broken_fails(tmp_path: Path, monkeypatch) -> None:
    """The reproduced incident: DAG checks green + broken sources => FAIL."""
    _patch_dag_pipeline_green(monkeypatch)
    src = tmp_path / "src"
    src.mkdir()
    for index in range(10):
        (src / f"module_{index}.py").write_text(f"def f_{index}(:\n    pass\n", encoding="utf-8")
    (src / "data.json").write_text("{not json", encoding="utf-8")
    (src / "settings.yaml").write_text("key: [unclosed\n", encoding="utf-8")

    result = VerifyRunner(tmp_path, _settings()).run()

    assert result.passed is False
    named = {node for failure in result.failures for node in failure.details.get("failed_nodes", [])}
    assert {f"src/module_{index}.py" for index in range(10)} <= named
    assert "src/data.json" in named
    assert "src/settings.yaml" in named
    # the failure shape is consumable by the repair loop
    violations = _violations_from_verify_result(result, fallback=result.failure)
    assert violations and all(isinstance(item, VerificationFailureReport) for item in violations)
    assert any("module_0.py" in node for violation in violations for node in violation.failed_nodes)


def test_healthy_sources_pass_and_report_checked_count(tmp_path: Path, monkeypatch) -> None:
    _patch_dag_pipeline_green(monkeypatch)
    src = tmp_path / "src"
    src.mkdir()
    (src / "ok.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
    (src / "ok.json").write_text('{"a": 1}\n', encoding="utf-8")

    result = VerifyRunner(tmp_path, _settings()).run()

    assert result.passed is True
    assert result.source_integrity == "checked 2 file(s)"


def test_source_integrity_opt_out_skips_the_check(tmp_path: Path, monkeypatch) -> None:
    _patch_dag_pipeline_green(monkeypatch)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "broken.py").write_text("def broken(:\n", encoding="utf-8")

    result = VerifyRunner(tmp_path, _settings(source_integrity=False)).run()

    assert result.passed is True
    assert result.source_integrity == "disabled"


def test_source_integrity_is_bounded_by_file_count(tmp_path: Path, monkeypatch) -> None:
    _patch_dag_pipeline_green(monkeypatch)
    monkeypatch.setattr(verify_runner_module, "SOURCE_INTEGRITY_MAX_FILES", 1)
    src = tmp_path / "src"
    src.mkdir()
    (src / "a_first.py").write_text("ok = 1\n", encoding="utf-8")
    (src / "z_broken.py").write_text("def broken(:\n", encoding="utf-8")

    result = VerifyRunner(tmp_path, _settings()).run()

    assert result.passed is True  # bound reached before the broken file
    assert "bounded at 1" in result.source_integrity


# ═══════════════════════════════════════════════════════════
# Test-command execution evidence
# ═══════════════════════════════════════════════════════════

def test_passing_test_command_marks_tests_executed(tmp_path: Path, monkeypatch) -> None:
    _patch_dag_pipeline_green(monkeypatch)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )

    result = VerifyRunner(tmp_path, _settings(test_command=PYTEST_COMMAND)).run()

    assert result.passed is True
    assert result.tests_executed is True
    assert result.test_command == PYTEST_COMMAND
    assert "passed" in result.tests_summary
    assert result.executed_anything is True
    assert STRUCTURAL_ONLY_WARNING not in result.warnings


def test_detected_pytest_config_resolves_and_runs_without_explicit_command(tmp_path: Path, monkeypatch) -> None:
    """RF2 heuristics: a pytest.ini alone makes verify actually run pytest."""
    _patch_dag_pipeline_green(monkeypatch)
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -p no:cacheprovider\n", encoding="utf-8")
    (tmp_path / "test_detected.py").write_text(
        "def test_detected():\n    assert True\n", encoding="utf-8"
    )

    result = VerifyRunner(tmp_path, _settings()).run()

    assert result.passed is True
    assert result.tests_executed is True
    assert result.test_command == "pytest --tb=short -q"


def test_failing_test_command_fails_verify_with_repairable_shape(tmp_path: Path, monkeypatch) -> None:
    _patch_dag_pipeline_green(monkeypatch)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_broken.py").write_text(
        "def test_broken():\n    assert False, 'intentional failure'\n", encoding="utf-8"
    )

    result = VerifyRunner(tmp_path, _settings(test_command=PYTEST_COMMAND)).run()

    assert result.passed is False
    assert result.tests_executed is True  # an observed failure IS execution evidence
    failures = [item for item in result.failures if item.check_name == "test_command"]
    assert len(failures) == 1
    assert "test command failed (exit 1)" in failures[0].message
    assert "intentional failure" in failures[0].message
    assert failures[0].details["exit_code"] == 1
    assert failures[0].details["command"] == PYTEST_COMMAND
    # consumable by the repair loop
    assert result.failure is not None
    assert any("test command failed" in message for message in result.failure.error_messages)
    violations = _violations_from_verify_result(result, fallback=result.failure)
    assert violations and isinstance(violations[0], VerificationFailureReport)


def test_pytest_collecting_no_tests_is_not_execution_evidence(tmp_path: Path, monkeypatch) -> None:
    """pytest exit code 5 = nothing collected: the runner started, nothing ran."""
    _patch_dag_pipeline_green(monkeypatch)
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -p no:cacheprovider\n", encoding="utf-8")

    result = VerifyRunner(tmp_path, _settings()).run()

    assert result.passed is True
    assert result.tests_executed is False
    assert "collected no tests" in result.tests_summary
    assert STRUCTURAL_ONLY_WARNING in result.warnings


def test_hanging_test_command_times_out_and_fails(tmp_path: Path, monkeypatch) -> None:
    _patch_dag_pipeline_green(monkeypatch)

    result = VerifyRunner(
        tmp_path, _settings(test_command="sleep 30", test_timeout_seconds=1)
    ).run()

    assert result.passed is False
    assert result.tests_executed is True
    assert "[TIMEOUT]" in result.failures[0].message
    assert "timed out after 1s" == result.tests_summary


# ═══════════════════════════════════════════════════════════
# Typecheck execution evidence
# ═══════════════════════════════════════════════════════════

def test_configured_typecheck_command_counts_as_execution(tmp_path: Path, monkeypatch) -> None:
    _patch_dag_pipeline_green(monkeypatch)

    result = VerifyRunner(tmp_path, _settings(typecheck_command="true")).run()

    assert result.passed is True
    assert result.typecheck_executed is True
    assert result.executed_anything is True
    assert STRUCTURAL_ONLY_WARNING not in result.warnings


def test_failing_typecheck_command_fails_verify(tmp_path: Path, monkeypatch) -> None:
    _patch_dag_pipeline_green(monkeypatch)

    result = VerifyRunner(tmp_path, _settings(typecheck_command="exit 7")).run()

    assert result.passed is False
    failures = [item for item in result.failures if item.check_name == "typecheck_command"]
    assert failures and failures[0].details["exit_code"] == 7


def test_typecheck_section_command_is_honored_when_enabled(tmp_path: Path, monkeypatch) -> None:
    _patch_dag_pipeline_green(monkeypatch)
    settings = _settings()
    settings["typecheck"] = {"enabled": True, "command": "true"}

    result = VerifyRunner(tmp_path, settings).run()

    assert result.typecheck_executed is True


# ═══════════════════════════════════════════════════════════
# The honesty rule (plain verify: pass-with-warning)
# ═══════════════════════════════════════════════════════════

def test_nothing_executed_passes_with_prominent_warning(tmp_path: Path, monkeypatch) -> None:
    _patch_dag_pipeline_green(monkeypatch)

    result = VerifyRunner(tmp_path, _settings()).run()

    assert result.passed is True  # plain verify must not break structural-only CI users
    assert result.tests_executed is False
    assert result.tests_summary == "no test command detected"
    assert result.executed_anything is False
    assert STRUCTURAL_ONLY_WARNING in result.warnings


def test_allow_structural_only_opt_out_silences_the_warning(tmp_path: Path, monkeypatch) -> None:
    _patch_dag_pipeline_green(monkeypatch)

    result = VerifyRunner(tmp_path, _settings(allow_structural_only=True)).run()

    assert result.passed is True
    assert result.warnings == []
    assert structural_only_allowed(_settings(allow_structural_only=True)) is True
    assert structural_only_allowed(_settings()) is False
    assert structural_only_allowed(None) is False


def test_runtime_verification_nodes_count_as_execution_evidence() -> None:
    executed = VerificationResult(
        True, runtime_results=[{"node_id": "v1", "passed": True, "skipped": False}]
    )
    skipped_only = VerificationResult(
        True, runtime_results=[{"node_id": "v1", "passed": None, "skipped": True}]
    )
    assert executed.executed_anything is True
    assert skipped_only.executed_anything is False
    assert VerificationResult(True).executed_anything is False


# ═══════════════════════════════════════════════════════════
# The greenfield half: stage failure instead of warning
# ═══════════════════════════════════════════════════════════

def _greenfield_project(tmp_path: Path, *, verify: dict | None = None) -> Path:
    import yaml

    project = tmp_path / "project"
    (project / "codd").mkdir(parents=True)
    config: dict = {"project": {"name": "demo", "type": "generic"}}
    if verify is not None:
        config["verify"] = verify
    (project / "codd" / "codd.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return project


def test_greenfield_certify_fails_when_verify_executed_nothing(tmp_path: Path) -> None:
    project = _greenfield_project(tmp_path)

    with pytest.raises(StageError, match="cannot certify an unexecuted build"):
        _certify_verify_executed(project, VerificationResult(True))


def test_greenfield_certify_passes_with_execution_evidence(tmp_path: Path) -> None:
    project = _greenfield_project(tmp_path)
    result = VerificationResult(True, tests_executed=True, test_command="pytest --tb=short -q")

    detail = _certify_verify_executed(project, result)

    assert detail == "verification passed (tests executed: pytest --tb=short -q)"


def test_greenfield_certify_honors_allow_structural_only(tmp_path: Path) -> None:
    project = _greenfield_project(tmp_path, verify={"allow_structural_only": True})

    detail = _certify_verify_executed(project, VerificationResult(True))

    assert "structural-only" in detail


# ═══════════════════════════════════════════════════════════
# Defaults documentation
# ═══════════════════════════════════════════════════════════

def test_defaults_yaml_declares_the_fx3_verify_keys() -> None:
    import yaml

    defaults = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "codd" / "defaults.yaml").read_text(encoding="utf-8")
    )
    verify = defaults["verify"]
    assert verify["source_integrity"] is True
    assert verify["allow_structural_only"] is False
    assert verify["test_timeout_seconds"] == 600
