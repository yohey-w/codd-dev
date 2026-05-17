from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from click.testing import CliRunner

from codd.cli import _CliVerificationResult, main
from codd.dag import DAG, Node
from codd.deployment.providers import VerificationResult as ProviderVerificationResult
from codd.repair import verify_runner as verify_runner_module
from codd.repair.verify_runner import VerifyRunner


@dataclass
class _CheckResult:
    check_name: str
    severity: str = "red"
    passed: bool = True


def _dag(*nodes: Node) -> DAG:
    dag = DAG()
    for node in nodes:
        dag.add_node(node)
    return dag


def _verification_node(node_id: str, template_ref: str = "fake") -> Node:
    return Node(node_id, "verification_test", attributes={"kind": "e2e", "template_ref": template_ref})


def _patch_pipeline(monkeypatch, dag: DAG) -> None:
    monkeypatch.setattr(verify_runner_module, "load_dag_settings", lambda project_root, settings: settings)
    monkeypatch.setattr(verify_runner_module, "build_dag", lambda project_root, settings: dag)
    monkeypatch.setattr(verify_runner_module, "run_checks", lambda *args, **kwargs: [_CheckResult("node_completeness")])


def test_t01_no_verification_timeout_preserves_default_template_behavior(tmp_path: Path, monkeypatch):
    seen: list[float | None] = []

    class FakeTemplate:
        def __init__(self, timeout: float | None = None) -> None:
            seen.append(timeout)

        def generate_test_command(self, runtime_state, test_kind: str) -> str:
            return "ok"

        def execute(self, command: str) -> ProviderVerificationResult:
            return ProviderVerificationResult(True, "ok")

    _patch_pipeline(monkeypatch, _dag(_verification_node("verification:e2e:flow")))
    monkeypatch.setitem(verify_runner_module.VERIFICATION_TEMPLATES, "fake", FakeTemplate)

    result = VerifyRunner(tmp_path, {"project": {"type": "generic"}}).run()

    assert result.passed is True
    assert seen == [None]
    assert result.runtime_results[0]["passed"] is True
    assert result.runtime_results[0]["skipped"] is False


def test_t02_per_node_seconds_caps_template_timeout(tmp_path: Path, monkeypatch):
    seen: list[float | None] = []

    class FakeTemplate:
        def __init__(self, timeout: float | None = None) -> None:
            seen.append(timeout)

        def generate_test_command(self, runtime_state, test_kind: str) -> str:
            return "ok"

        def execute(self, command: str) -> ProviderVerificationResult:
            return ProviderVerificationResult(True, "ok")

    _patch_pipeline(monkeypatch, _dag(_verification_node("verification:e2e:flow")))
    monkeypatch.setitem(verify_runner_module.VERIFICATION_TEMPLATES, "fake", FakeTemplate)

    VerifyRunner(
        tmp_path,
        {
            "verify": {"verification_timeout": {"per_node_seconds": 10}},
            "verification": {"templates": {"fake": {"timeout": 60000}}},
        },
    ).run()

    assert seen == [10]


def test_t03_single_node_timeout_does_not_abort_remaining_nodes(tmp_path: Path, monkeypatch):
    executed: list[str] = []

    class FakeTemplate:
        def generate_test_command(self, runtime_state, test_kind: str) -> str:
            return str(runtime_state.identifier)

        def execute(self, command: str) -> ProviderVerificationResult:
            executed.append(command)
            if "bad" in command:
                raise subprocess.TimeoutExpired(command, 10)
            return ProviderVerificationResult(True, "ok")

    _patch_pipeline(
        monkeypatch,
        _dag(_verification_node("verification:e2e:bad"), _verification_node("verification:e2e:good")),
    )
    monkeypatch.setitem(verify_runner_module.VERIFICATION_TEMPLATES, "fake", FakeTemplate)

    result = VerifyRunner(tmp_path, {"project": {"type": "generic"}}).run()

    assert executed == ["verification:e2e:bad", "verification:e2e:good"]
    assert result.passed is False
    assert result.runtime_results[0]["passed"] is False
    assert result.runtime_results[1]["passed"] is True
    assert result.failures[0].message.startswith("[TIMEOUT] verification_test: verification:e2e:bad")


def test_t04_total_seconds_budget_skips_remaining_nodes(tmp_path: Path, monkeypatch):
    executed: list[str] = []
    times = iter([0, 0, 3, 6, 6, 6])

    class FakeTemplate:
        def generate_test_command(self, runtime_state, test_kind: str) -> str:
            return str(runtime_state.identifier)

        def execute(self, command: str) -> ProviderVerificationResult:
            executed.append(command)
            return ProviderVerificationResult(True, "ok")

    nodes = [_verification_node(f"verification:e2e:{index}") for index in range(5)]
    _patch_pipeline(monkeypatch, _dag(*nodes))
    monkeypatch.setattr(verify_runner_module.time, "monotonic", lambda: next(times))
    monkeypatch.setitem(verify_runner_module.VERIFICATION_TEMPLATES, "fake", FakeTemplate)

    result = VerifyRunner(tmp_path, {"verify": {"verification_timeout": {"total_seconds": 5}}}).run()

    assert executed == ["verification:e2e:0", "verification:e2e:1"]
    assert [item["skipped"] for item in result.runtime_results] == [False, False, True, True, True]
    assert {item["skip_reason"] for item in result.runtime_results if item["skipped"]} == {"total_timeout_exceeded"}
    assert result.passed is True


def test_t05_all_nodes_pass_within_total_seconds(tmp_path: Path, monkeypatch):
    class FakeTemplate:
        def generate_test_command(self, runtime_state, test_kind: str) -> str:
            return "ok"

        def execute(self, command: str) -> ProviderVerificationResult:
            return ProviderVerificationResult(True, "ok")

    nodes = [_verification_node(f"verification:e2e:{index:02d}") for index in range(22)]
    _patch_pipeline(monkeypatch, _dag(*nodes))
    monkeypatch.setattr(verify_runner_module.time, "monotonic", lambda: 0)
    monkeypatch.setitem(verify_runner_module.VERIFICATION_TEMPLATES, "fake", FakeTemplate)

    result = VerifyRunner(tmp_path, {"verify": {"verification_timeout": {"total_seconds": 60}}}).run()

    assert result.passed is True
    assert len(result.runtime_results) == 22
    assert sum(1 for item in result.runtime_results if item["passed"] is True) == 22
    assert sum(1 for item in result.runtime_results if item["skipped"]) == 0


def test_t06_runtime_skip_verification_test_bypasses_nodes_and_cli_accepts_choice(tmp_path: Path, monkeypatch):
    class FakeTemplate:
        def generate_test_command(self, runtime_state, test_kind: str) -> str:
            raise AssertionError("verification-test skip must bypass template execution")

    _patch_pipeline(
        monkeypatch,
        _dag(_verification_node("verification:e2e:first"), _verification_node("verification:e2e:second")),
    )
    monkeypatch.setitem(verify_runner_module.VERIFICATION_TEMPLATES, "fake", FakeTemplate)

    result = VerifyRunner(
        tmp_path,
        {"project": {"type": "generic"}},
        runtime_skip=("verification-test",),
    ).run()

    assert result.passed is True
    assert [item["skipped"] for item in result.runtime_results] == [True, True]
    assert all("Skipped: verification-test" in item["output"] for item in result.runtime_results)

    verify_calls: list[tuple[str, ...]] = []
    smoke_calls: list[tuple[str, ...]] = []

    def fake_verify_once(**kwargs):
        verify_calls.append(kwargs["runtime_skip"])
        return _CliVerificationResult(passed=True, exit_code=0, runtime_results=result.runtime_results)

    def fake_smoke_gate(path: str, runtime_base_url: str | None, runtime_skip: tuple[str, ...]) -> None:
        smoke_calls.append(runtime_skip)

    monkeypatch.setattr("codd.cli._run_verify_once", fake_verify_once)
    monkeypatch.setattr("codd.cli._run_runtime_smoke_gate", fake_smoke_gate)

    cli_result = CliRunner().invoke(
        main,
        ["verify", "--path", str(tmp_path), "--runtime", "--runtime-skip", "verification-test"],
    )

    assert cli_result.exit_code == 0
    assert verify_calls == [("verification-test",)]
    assert smoke_calls == [()]
    assert "Verification tests: 0 PASS / 0 FAIL / 2 SKIP" in cli_result.output
    assert "Skipped: verification-test (2 nodes by user request)" in cli_result.output
