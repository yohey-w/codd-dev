"""Tests for the aggregated ``codd check`` health entry point (RF3)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

import codd.cli as cli_module
from codd.cli import main
from codd.dag import auto_repair as auto_repair_module
from codd.dag import runner as dag_runner

_CLICK_VERSION = tuple(int(part) for part in click.__version__.split(".")[:2])


def _split_stream_runner() -> CliRunner:
    """CliRunner that keeps stdout/stderr separate across the supported click range."""
    if _CLICK_VERSION < (8, 2):
        return CliRunner(mix_stderr=False)
    return CliRunner()


@dataclass
class _CheckResult:
    check_name: str
    severity: str = "red"
    passed: bool = True
    missing_impl_files: list[str] = field(default_factory=list)
    unreachable_nodes: list[str] = field(default_factory=list)


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    (tmp_path / "codd").mkdir()
    (tmp_path / "codd" / "codd.yaml").write_text("project_name: demo\n", encoding="utf-8")
    return tmp_path


def _patch_dag(monkeypatch, results):
    monkeypatch.setattr(
        dag_runner,
        "run_all_checks",
        lambda project_root, settings=None, check_names=None: results,
    )


def _patch_doctor(monkeypatch, warnings):
    monkeypatch.setattr(cli_module, "_doctor_warnings", lambda project_root: list(warnings))


def test_check_green_project_exits_zero(project: Path, monkeypatch) -> None:
    _patch_doctor(monkeypatch, [])
    _patch_dag(monkeypatch, [_CheckResult("node_completeness"), _CheckResult("edge_validity")])

    result = CliRunner().invoke(main, ["check", "--path", str(project)])

    assert result.exit_code == 0
    assert "PASS — no warnings" in result.output
    assert "PASS  node_completeness [red]" in result.output
    assert "Summary: 0 gate(s) failed, 0 advisory finding(s)" in result.output


def test_check_red_dag_failure_exits_nonzero(project: Path, monkeypatch) -> None:
    _patch_doctor(monkeypatch, [])
    _patch_dag(
        monkeypatch,
        [_CheckResult("node_completeness", passed=False, missing_impl_files=["app/page.tsx"])],
    )

    result = CliRunner().invoke(main, ["check", "--path", str(project)])

    assert result.exit_code == 1
    assert "FAIL  node_completeness [red]" in result.output
    assert "Run 'codd dag verify' for details." in result.output
    assert "Summary: 1 gate(s) failed, 0 advisory finding(s)" in result.output


def test_check_advisory_findings_keep_exit_zero(project: Path, monkeypatch) -> None:
    _patch_doctor(monkeypatch, ["mutating endpoint without outcome target"])
    _patch_dag(
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

    result = CliRunner().invoke(main, ["check", "--path", str(project)])

    assert result.exit_code == 0
    assert "WARNING: mutating endpoint without outcome target" in result.output
    assert "Run 'codd doctor' for details." in result.output
    assert "Summary: 0 gate(s) failed, 2 advisory finding(s)" in result.output


def test_check_disabled_contract_is_noop_section(project: Path, monkeypatch) -> None:
    _patch_doctor(monkeypatch, [])
    _patch_dag(monkeypatch, [_CheckResult("edge_validity")])

    result = CliRunner().invoke(main, ["check", "--path", str(project)])

    assert result.exit_code == 0
    assert "skipped: artifact_contract is disabled (opt-in)" in result.output


def test_check_format_json_parses(project: Path, monkeypatch) -> None:
    _patch_doctor(monkeypatch, ["one advisory"])
    _patch_dag(monkeypatch, [_CheckResult("edge_validity")])
    runner = _split_stream_runner()

    result = runner.invoke(main, ["check", "--path", str(project), "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["doctor"] == ["one advisory"]
    assert payload["dag"][0]["check_name"] == "edge_validity"
    assert payload["contract"]["status"] == "skipped"
    assert payload["summary"] == {"gates_failed": 0, "advisories": 1}


def test_check_format_json_red_failure_reported_in_summary(project: Path, monkeypatch) -> None:
    _patch_doctor(monkeypatch, [])
    _patch_dag(monkeypatch, [_CheckResult("ci_health", passed=False)])
    runner = _split_stream_runner()

    result = runner.invoke(main, ["check", "--path", str(project), "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["summary"]["gates_failed"] == 1


def test_check_full_skips_unconfigured_policy_and_coverage(project: Path, monkeypatch) -> None:
    _patch_doctor(monkeypatch, [])
    _patch_dag(monkeypatch, [_CheckResult("edge_validity")])

    result = CliRunner().invoke(main, ["check", "--path", str(project), "--full"])

    assert result.exit_code == 0
    assert "skipped: no policies configured in codd.yaml" in result.output
    assert "skipped: no coverage.thresholds configured in codd.yaml" in result.output
    assert "Summary: 0 gate(s) failed" in result.output


def test_check_full_json_includes_policy_and_coverage_sections(project: Path, monkeypatch) -> None:
    _patch_doctor(monkeypatch, [])
    _patch_dag(monkeypatch, [_CheckResult("edge_validity")])
    runner = _split_stream_runner()

    result = runner.invoke(main, ["check", "--path", str(project), "--full", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["policy"]["status"] == "skipped"
    assert payload["coverage"]["status"] == "skipped"


def test_check_fix_runs_auto_repair_in_apply_mode(project: Path, monkeypatch) -> None:
    _patch_doctor(monkeypatch, [])
    _patch_dag(monkeypatch, [_CheckResult("edge_validity")])

    @dataclass
    class _Outcome:
        applied: list = field(default_factory=list)
        skipped: list = field(default_factory=list)

    calls: dict[str, object] = {}

    def fake_apply_auto_repair(project_root, results, dry_run=True):
        calls["project_root"] = project_root
        calls["dry_run"] = dry_run
        return _Outcome()

    monkeypatch.setattr(auto_repair_module, "apply_auto_repair", fake_apply_auto_repair)

    result = CliRunner().invoke(main, ["check", "--path", str(project), "--fix"])

    assert result.exit_code == 0
    assert calls["dry_run"] is False
    assert calls["project_root"] == project.resolve()
    assert "Applied 0 auto-repair(s):" in result.output


def test_check_requires_codd_dir(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["check", "--path", str(tmp_path)])

    assert result.exit_code == 1
    assert "Run 'codd init' first" in result.output


def test_main_help_points_to_check() -> None:
    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0
    assert "Health: codd check (start here)" in result.output


def test_check_help_mentions_preflight_stays_separate() -> None:
    result = CliRunner().invoke(main, ["check", "--help"])

    assert result.exit_code == 0
    assert "codd preflight" in result.output
