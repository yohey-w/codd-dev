from __future__ import annotations

from dataclasses import dataclass, field

import yaml

from codd.coherence_engine import EventBus, use_coherence_bus
from codd.deployer import (
    DeployGateResult,
    _collect_dag_completeness_gate,
    _run_deploy_gates,
)
from codd.dag import runner as dag_runner


@dataclass
class _CheckResult:
    check_name: str
    severity: str = "red"
    passed: bool = True
    missing_impl_files: list[str] = field(default_factory=list)
    unreachable_nodes: list[str] = field(default_factory=list)


def _patch_results(monkeypatch, results):
    monkeypatch.setattr(dag_runner, "run_all_checks", lambda project_root, settings=None: results)


def _write_codd_yaml(project):
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "version": "0.1.0",
                "scan": {"source_dirs": [], "test_dirs": [], "doc_dirs": [], "config_files": []},
                "graph": {"store": "jsonl", "path": "codd/scan"},
                "warn_on_skip": False,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _disable_other_gates(monkeypatch):
    monkeypatch.setattr("codd.deployer._collect_validate_gate", lambda *args: None)
    monkeypatch.setattr("codd.deployer._collect_drift_gate", lambda *args: None)
    monkeypatch.setattr("codd.deployer._collect_drift_linker_gate", lambda *args: None)
    monkeypatch.setattr("codd.deployer._collect_coverage_gate", lambda *args: None)


def test_deploy_gate_passes_on_clean_dag(tmp_path, monkeypatch):
    _patch_results(monkeypatch, [_CheckResult("node_completeness")])
    result = DeployGateResult()

    _collect_dag_completeness_gate(tmp_path, {}, result)

    assert result.passed is True


def test_deploy_gate_blocks_on_red_fail(tmp_path, monkeypatch):
    _patch_results(
        monkeypatch,
        [_CheckResult("node_completeness", passed=False, missing_impl_files=["app/admin/page.tsx"])],
    )
    result = DeployGateResult()

    _collect_dag_completeness_gate(tmp_path, {}, result)

    assert result.passed is False
    assert result.failures[0].gate == "dag_completeness"


def test_deploy_gate_passes_on_amber_warn(tmp_path, monkeypatch):
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
    result = DeployGateResult()

    _collect_dag_completeness_gate(tmp_path, {}, result)

    assert result.passed is True
    assert result.warnings


def test_deploy_gate_error_handled_gracefully(tmp_path, monkeypatch):
    def raise_runner(project_root, settings=None):
        raise RuntimeError("dag exploded")

    monkeypatch.setattr(dag_runner, "run_all_checks", raise_runner)
    result = DeployGateResult()

    _collect_dag_completeness_gate(tmp_path, {}, result)

    assert result.passed is False
    assert "dag exploded" in result.failures[0].message


def test_deploy_gate_in_run_deploy_gates(tmp_path, monkeypatch):
    _write_codd_yaml(tmp_path)
    _disable_other_gates(monkeypatch)
    _patch_results(monkeypatch, [_CheckResult("node_completeness", passed=False)])

    result = _run_deploy_gates(tmp_path)

    assert result.passed is False
    assert any(failure.gate == "dag_completeness" for failure in result.failures)


def test_drift_event_published_on_red_fail(tmp_path, monkeypatch):
    _patch_results(monkeypatch, [_CheckResult("node_completeness", passed=False)])
    bus = EventBus()
    result = DeployGateResult()

    with use_coherence_bus(bus):
        _collect_dag_completeness_gate(tmp_path, {}, result)

    events = bus.published_events()
    assert [event.kind for event in events] == ["dag_completeness"]
    assert events[0].severity == "red"
