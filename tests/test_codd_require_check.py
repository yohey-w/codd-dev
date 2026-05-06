from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from click.testing import CliRunner
import pytest
import yaml

from codd.cli import main
from codd.dag import runner as dag_runner


@dataclass
class _CoverageResult:
    passed: bool
    message: str = "C8 implementation_coverage PASS"
    check_name: str = "implementation_coverage"
    violations: list[dict] = field(default_factory=list)


def _write_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True)
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "demo", "language": "python"},
                "scan": {"source_dirs": ["src/"], "doc_dirs": ["docs/"], "config_files": [], "exclude": []},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return project


def test_require_check_runs_implementation_coverage_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = _write_project(tmp_path)
    calls: list[dict] = []

    def fake_run_all_checks(project_root, settings=None, check_names=None):
        calls.append({"project_root": project_root, "check_names": check_names})
        return [_CoverageResult(passed=True)]

    monkeypatch.setattr(dag_runner, "run_all_checks", fake_run_all_checks)

    result = CliRunner().invoke(main, ["require", "--path", str(project), "--check"])

    assert result.exit_code == 0, result.output
    assert calls == [{"project_root": project.resolve(), "check_names": ["implementation_coverage"]}]
    assert "PASS: implementation_coverage" in result.output
    assert "Requirement check complete: implementation_coverage PASS" in result.output


def test_require_check_failure_exits_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = _write_project(tmp_path)

    monkeypatch.setattr(
        dag_runner,
        "run_all_checks",
        lambda *args, **kwargs: [
            _CoverageResult(
                passed=False,
                message="C8 implementation_coverage found 1 missing expected artifact(s)",
                violations=[{"type": "missing_implementation", "path_hint": "src/service.py"}],
            )
        ],
    )

    result = CliRunner().invoke(main, ["require", "--path", str(project), "--check"])

    assert result.exit_code == 1
    assert "FAIL: implementation_coverage" in result.output
    assert "src/service.py" in result.output


def test_require_check_does_not_generate_requirements(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = _write_project(tmp_path)
    monkeypatch.setattr(dag_runner, "run_all_checks", lambda *args, **kwargs: [_CoverageResult(passed=True)])

    def fail_run_require(*args, **kwargs):
        raise AssertionError("run_require should not be called for --check")

    monkeypatch.setattr("codd.require.run_require", fail_run_require)

    result = CliRunner().invoke(main, ["require", "--path", str(project), "--check"])

    assert result.exit_code == 0, result.output


def test_require_check_reports_runner_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = _write_project(tmp_path)

    def raise_runner_error(*args, **kwargs):
        raise ValueError("DAG unavailable")

    monkeypatch.setattr(dag_runner, "run_all_checks", raise_runner_error)

    result = CliRunner().invoke(main, ["require", "--path", str(project), "--check"])

    assert result.exit_code == 1
    assert "Error: DAG unavailable" in result.output
