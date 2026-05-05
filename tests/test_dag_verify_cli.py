from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from click.testing import CliRunner

from codd.cli import main
from codd.dag import runner as dag_runner


@dataclass
class _CheckResult:
    check_name: str
    severity: str = "red"
    passed: bool = True
    missing_impl_files: list[str] = field(default_factory=list)
    unreachable_nodes: list[str] = field(default_factory=list)


def _patch_results(monkeypatch, results, calls=None):
    def fake_run_all_checks(project_root: Path, settings=None, check_names=None):
        if calls is not None:
            calls.append(
                {
                    "project_root": project_root,
                    "settings": settings,
                    "check_names": check_names,
                }
            )
        return results

    monkeypatch.setattr(dag_runner, "run_all_checks", fake_run_all_checks)


def test_verify_all_checks_pass(tmp_path, monkeypatch):
    _patch_results(
        monkeypatch,
        [
            _CheckResult("node_completeness"),
            _CheckResult("edge_validity"),
            _CheckResult("depends_on_consistency"),
        ],
    )

    result = CliRunner().invoke(main, ["dag", "verify", "--project-path", str(tmp_path)])

    assert result.exit_code == 0
    assert "PASS  node_completeness [red]" in result.output


def test_verify_red_fail_exits_1(tmp_path, monkeypatch):
    _patch_results(
        monkeypatch,
        [_CheckResult("node_completeness", passed=False, missing_impl_files=["app/admin/page.tsx"])],
    )

    result = CliRunner().invoke(main, ["dag", "verify", "--project-path", str(tmp_path)])

    assert result.exit_code == 1
    assert "FAIL  node_completeness [red]" in result.output
    assert "1 check(s) FAILED" in result.output


def test_verify_amber_warn_exits_0(tmp_path, monkeypatch):
    _patch_results(
        monkeypatch,
        [
            _CheckResult(
                "transitive_closure",
                severity="amber",
                passed=True,
                unreachable_nodes=["src/orphan.ts"],
            )
        ],
    )

    result = CliRunner().invoke(main, ["dag", "verify", "--project-path", str(tmp_path)])

    assert result.exit_code == 0
    assert "WARN (severity=amber, deploy allowed)" in result.output


def test_verify_specific_check_only(tmp_path, monkeypatch):
    calls = []
    _patch_results(monkeypatch, [_CheckResult("node_completeness")], calls)

    result = CliRunner().invoke(
        main,
        ["dag", "verify", "--project-path", str(tmp_path), "--check", "node_completeness"],
    )

    assert result.exit_code == 0
    assert calls[0]["check_names"] == ["node_completeness"]


def test_verify_json_format(tmp_path, monkeypatch):
    _patch_results(monkeypatch, [_CheckResult("edge_validity")])

    result = CliRunner().invoke(
        main,
        ["dag", "verify", "--project-path", str(tmp_path), "--format", "json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.output)[0]["check_name"] == "edge_validity"


def test_verify_empty_dag(tmp_path, monkeypatch):
    _patch_results(monkeypatch, [])

    result = CliRunner().invoke(main, ["dag", "verify", "--project-path", str(tmp_path)])

    assert result.exit_code == 0
    assert "FAILED" not in result.output


def test_verify_output_shows_check_names(tmp_path, monkeypatch):
    _patch_results(monkeypatch, [_CheckResult("task_completion")])

    result = CliRunner().invoke(main, ["dag", "verify", "--project-path", str(tmp_path)])

    assert result.exit_code == 0
    assert "task_completion" in result.output


def test_verify_multiple_check_filter(tmp_path, monkeypatch):
    calls = []
    _patch_results(
        monkeypatch,
        [_CheckResult("node_completeness"), _CheckResult("edge_validity")],
        calls,
    )

    result = CliRunner().invoke(
        main,
        [
            "dag",
            "verify",
            "--project-path",
            str(tmp_path),
            "--check",
            "node_completeness",
            "--check",
            "edge_validity",
        ],
    )

    assert result.exit_code == 0
    assert calls[0]["check_names"] == ["node_completeness", "edge_validity"]


def test_verify_nonexistent_project_error(tmp_path):
    missing = tmp_path / "missing"

    result = CliRunner().invoke(main, ["dag", "verify", "--project-path", str(missing)])

    assert result.exit_code == 1
    assert "project root not found" in result.output


def test_verify_runner_called_with_project_root(tmp_path, monkeypatch):
    calls = []
    _patch_results(monkeypatch, [_CheckResult("node_completeness")], calls)

    result = CliRunner().invoke(main, ["dag", "verify", "--project-path", str(tmp_path)])

    assert result.exit_code == 0
    assert calls[0]["project_root"] == tmp_path.resolve()
