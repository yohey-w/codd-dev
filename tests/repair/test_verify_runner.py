from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from codd.dag import DAG, Node
from codd.deployment.providers import VerificationResult as ProviderVerificationResult
from codd.repair import verify_runner as verify_runner_module
from codd.repair.verify_runner import DEFAULT_CHECKS, VerifyRunner, VerificationResult


@dataclass
class _CheckResult:
    check_name: str
    severity: str = "red"
    passed: bool = True
    message: str = ""
    missing_impl_files: list[str] = field(default_factory=list)
    violations: list[dict] = field(default_factory=list)


def _write_codd_yaml(project_root: Path, payload: dict | None = None) -> None:
    codd_dir = project_root / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(payload or {"project": {"type": "generic"}}), encoding="utf-8")


def _dag(*nodes: Node) -> DAG:
    dag = DAG()
    for node in nodes:
        dag.add_node(node)
    return dag


def _patch_verify_pipeline(monkeypatch, dag: DAG, results: list[_CheckResult], calls: dict | None = None) -> None:
    def fake_load_dag_settings(project_root, settings):
        if calls is not None:
            calls.setdefault("settings", []).append(settings)
        return {"loaded": True, **dict(settings or {})}

    def fake_build_dag(project_root, settings):
        if calls is not None:
            calls.setdefault("builds", []).append((project_root, settings))
        return dag

    def fake_run_checks(dag_arg, project_root, settings, check_names=None):
        if calls is not None:
            calls.setdefault("checks", []).append(check_names)
        return results

    monkeypatch.setattr(verify_runner_module, "load_dag_settings", fake_load_dag_settings)
    monkeypatch.setattr(verify_runner_module, "build_dag", fake_build_dag)
    monkeypatch.setattr(verify_runner_module, "run_checks", fake_run_checks)


def test_verify_runner_run_resets_dag_cache_before_checks(tmp_path, monkeypatch):
    calls: list[str] = []
    _patch_verify_pipeline(monkeypatch, _dag(), [_CheckResult("node_completeness")])
    monkeypatch.setattr(verify_runner_module, "reset_dag_cache", lambda project_root: calls.append(str(project_root)))

    result = VerifyRunner(tmp_path, {"project": {"type": "generic"}}).run()

    assert result.passed is True
    assert calls == [str(tmp_path.resolve())]


def test_verify_runner_runs_default_dag_checks(tmp_path, monkeypatch):
    calls: dict = {}
    _patch_verify_pipeline(monkeypatch, _dag(), [], calls)

    VerifyRunner(tmp_path, {"project": {"type": "generic"}}).run()

    assert calls["checks"][0] == DEFAULT_CHECKS
    assert len(calls["checks"][0]) == 8
    assert calls["checks"][0][-2:] == ("user_journey_coherence", "environment_coverage")


def test_verify_runner_pass_returns_verification_result(tmp_path, monkeypatch):
    _patch_verify_pipeline(monkeypatch, _dag(), [_CheckResult("edge_validity")])

    result = VerifyRunner(tmp_path, {"project": {"type": "generic"}}).run()

    assert isinstance(result, VerificationResult)
    assert result.passed is True
    assert result.failures == []


def test_verify_runner_red_failure_populates_failures_and_repair_report(tmp_path, monkeypatch):
    _patch_verify_pipeline(
        monkeypatch,
        _dag(),
        [
            _CheckResult(
                "node_completeness",
                passed=False,
                missing_impl_files=["src/missing.py"],
            )
        ],
    )

    result = VerifyRunner(tmp_path, {"project": {"type": "generic"}}).run()

    assert result.passed is False
    assert result.failures[0].check_name == "node_completeness"
    assert "src/missing.py" in result.failure.failed_nodes


def test_verify_runner_amber_warning_does_not_fail_repair_verify(tmp_path, monkeypatch):
    _patch_verify_pipeline(
        monkeypatch,
        _dag(),
        [_CheckResult("transitive_closure", severity="amber", passed=False, message="orphan warning")],
    )

    result = VerifyRunner(tmp_path, {"project": {"type": "generic"}}).run()

    assert result.passed is True
    assert result.failures == []


def test_verify_runner_missing_codd_yaml_fails_gracefully(tmp_path):
    result = VerifyRunner(tmp_path, {}).run()

    assert result.passed is False
    assert result.failures[0].check_name == "codd_config"
    assert "codd.yaml not found" in result.failures[0].message


def test_verify_runner_uses_project_codd_yaml_when_mapping_not_provided(tmp_path, monkeypatch):
    _write_codd_yaml(tmp_path, {"dag": {"enabled_checks": ["node_completeness"]}})
    calls: dict = {}
    _patch_verify_pipeline(monkeypatch, _dag(), [], calls)

    result = VerifyRunner(tmp_path, {}).run()

    assert result.passed is True
    assert calls["settings"][0]["dag"]["enabled_checks"] == ["node_completeness"]


def test_verify_runner_does_not_call_subprocess_for_static_checks(tmp_path, monkeypatch):
    _patch_verify_pipeline(monkeypatch, _dag(), [_CheckResult("node_completeness")])

    def fail_subprocess(*args, **kwargs):
        raise AssertionError("subprocess.run must not be used by VerifyRunner")

    monkeypatch.setattr("subprocess.run", fail_subprocess)

    result = VerifyRunner(tmp_path, {"project": {"type": "generic"}}).run()

    assert result.passed is True


def test_reset_dag_cache_method_delegates_to_public_dag_api(tmp_path, monkeypatch):
    calls: list[Path] = []
    monkeypatch.setattr(verify_runner_module, "reset_dag_cache", lambda project_root: calls.append(project_root))

    VerifyRunner(tmp_path, {"project": {"type": "generic"}}).reset_dag_cache()

    assert calls == [tmp_path.resolve()]


def test_verify_runner_resets_dag_cache_on_each_attempt(tmp_path, monkeypatch):
    calls: list[Path] = []
    build_count = 0

    def fake_build_dag(project_root, settings):
        nonlocal build_count
        build_count += 1
        return _dag()

    monkeypatch.setattr(verify_runner_module, "reset_dag_cache", lambda project_root: calls.append(project_root))
    monkeypatch.setattr(verify_runner_module, "load_dag_settings", lambda project_root, settings: {})
    monkeypatch.setattr(verify_runner_module, "build_dag", fake_build_dag)
    monkeypatch.setattr(verify_runner_module, "run_checks", lambda *args, **kwargs: [])

    runner = VerifyRunner(tmp_path, {"project": {"type": "generic"}})
    assert runner.run().passed is True
    assert runner.run().passed is True
    assert len(calls) == 2
    assert build_count == 2


def test_verify_runner_executes_cdp_browser_template_by_python_import(tmp_path, monkeypatch):
    import codd.deployment.providers.verification  # noqa: F401

    calls: list[dict] = []

    class FakeCdpBrowser:
        def __init__(self, config=None):
            self.config = config

        def generate_test_command(self, runtime_state, test_kind: str) -> str:
            calls.append({"config": self.config, "steps": runtime_state.steps, "kind": test_kind})
            return "journey-command"

        def execute(self, command: str, cwd=None) -> ProviderVerificationResult:
            calls.append({"command": command})
            return ProviderVerificationResult(True, "journey ok")

    dag = _dag(
        Node(
            "verification:cdp_browser:login",
            "verification_test",
            attributes={
                "kind": "e2e",
                "template_ref": "cdp_browser",
                "target": "/login",
                "expected_outcome": {"journey": {"steps": [{"action": "navigate", "target": "/login"}]}},
            },
        )
    )
    _patch_verify_pipeline(monkeypatch, dag, [])
    monkeypatch.setitem(verify_runner_module.VERIFICATION_TEMPLATES, "cdp_browser", FakeCdpBrowser)

    result = VerifyRunner(
        tmp_path,
        {"verification": {"templates": {"cdp_browser": {"browser": {"engine": "fake"}}}}},
    ).run()

    assert result.passed is True
    assert calls[0]["config"] == {"browser": {"engine": "fake"}}
    assert calls[0]["steps"] == [{"action": "navigate", "target": "/login"}]
    assert calls[1]["command"] == "journey-command"


def test_verify_runner_prefers_cdp_steps_over_conceptual_journey_steps(tmp_path, monkeypatch):
    class FakeCdpBrowser:
        def __init__(self, config=None):
            self.config = config

        def generate_test_command(self, runtime_state, test_kind: str) -> str:
            assert runtime_state.source == "tests/e2e/login.spec.ts"
            assert runtime_state.steps == [{"action": "navigate", "target": "/login"}]
            return "journey-command"

        def execute(self, command: str, cwd=None) -> ProviderVerificationResult:
            return ProviderVerificationResult(True, "journey ok")

    dag = _dag(
        Node(
            "verification:cdp_browser:login",
            "verification_test",
            attributes={
                "kind": "e2e",
                "template_ref": "cdp_browser",
                "expected_outcome": {
                    "source": "tests/e2e/login.spec.ts",
                    "journey": {
                        "steps": [{"action": "login", "role": "admin"}],
                        "cdp_steps": [{"action": "navigate", "target": "/login"}],
                    },
                },
            },
        )
    )
    _patch_verify_pipeline(monkeypatch, dag, [])
    monkeypatch.setitem(verify_runner_module.VERIFICATION_TEMPLATES, "cdp_browser", FakeCdpBrowser)

    result = VerifyRunner(
        tmp_path,
        {"verification": {"templates": {"cdp_browser": {"browser": {"engine": "fake"}}}}},
    ).run()

    assert result.passed is True


def test_verify_runner_runtime_failure_becomes_verification_failure(tmp_path, monkeypatch):
    class FailingTemplate:
        def generate_test_command(self, runtime_state, test_kind: str) -> str:
            return "runtime-command"

        def execute(self, command: str, cwd=None) -> ProviderVerificationResult:
            return ProviderVerificationResult(False, "runtime failed")

    dag = _dag(Node("verification:e2e:flow", "verification_test", attributes={"kind": "e2e", "template_ref": "fake"}))
    _patch_verify_pipeline(monkeypatch, dag, [])
    monkeypatch.setitem(verify_runner_module.VERIFICATION_TEMPLATES, "fake", FailingTemplate)

    result = VerifyRunner(tmp_path, {"project": {"type": "generic"}}).run()

    assert result.passed is False
    assert result.failures[0].source == "verification_test_runtime"
    assert "runtime failed" in result.failure.error_messages


def test_verify_runner_unknown_runtime_template_fails_without_crashing(tmp_path, monkeypatch):
    dag = _dag(Node("verification:e2e:flow", "verification_test", attributes={"kind": "e2e", "template_ref": "missing"}))
    _patch_verify_pipeline(monkeypatch, dag, [])

    result = VerifyRunner(tmp_path, {"project": {"type": "generic"}}).run()

    assert result.passed is False
    assert "not registered" in result.failures[0].message


def test_verify_runner_output_can_be_used_as_repair_loop_verify_callable(tmp_path, monkeypatch):
    _patch_verify_pipeline(monkeypatch, _dag(), [_CheckResult("node_completeness")])
    runner = VerifyRunner(tmp_path, {"project": {"type": "generic"}})

    verify_callable = runner.run
    result = verify_callable()

    assert callable(verify_callable)
    assert result.passed is True
    assert result.failure is None


def test_verify_runner_keeps_runtime_state_inside_project_root(tmp_path, monkeypatch):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("unchanged", encoding="utf-8")

    class InspectingTemplate:
        seen_root: Path | None = None

        def generate_test_command(self, runtime_state, test_kind: str) -> str:
            type(self).seen_root = runtime_state.project_root
            return "ok"

        def execute(self, command: str, cwd=None) -> ProviderVerificationResult:
            return ProviderVerificationResult(True, "ok")

    dag = _dag(Node("verification:e2e:flow", "verification_test", attributes={"kind": "e2e", "template_ref": "safe"}))
    _patch_verify_pipeline(monkeypatch, dag, [])
    monkeypatch.setitem(verify_runner_module.VERIFICATION_TEMPLATES, "safe", InspectingTemplate)

    assert VerifyRunner(tmp_path, {"project": {"type": "generic"}}).run().passed is True
    assert InspectingTemplate.seen_root == tmp_path.resolve()
    assert outside.read_text(encoding="utf-8") == "unchanged"


# ── v3.15.0 fold: env-channel projection to the verification-template surface ──


class _EnvCapturingTemplate:
    """Records the ``env`` the runner threads into ``execute`` (accepts the seam)."""

    seen_env: object = "unset"

    def generate_test_command(self, runtime_state, test_kind: str) -> str:
        return "true"

    def execute(self, command: str, cwd=None, env=None) -> ProviderVerificationResult:
        type(self).seen_env = env
        return ProviderVerificationResult(True, "ok")


def _write_exec_env_state(project_root: Path, *prepend_dirs: Path) -> None:
    state = project_root / ".codd" / "verify" / "exec_env.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps({"path_prepend_dirs": [str(d) for d in prepend_dirs]}),
        encoding="utf-8",
    )


def test_verification_template_surface_receives_prepended_env_from_state(tmp_path, monkeypatch):
    """The THIRD verify spawn surface (template.execute) gets the SAME harness PATH
    prepend the contract executor + evidence command already use: the runner reads
    the state artifact, builds the env, and threads it in. RED before this seam
    existed (execute got no env → the pytest_http ``python`` argv was unresolvable
    on a python3-only host = the dogfood's 5 real failures)."""
    prepend = tmp_path / "provisioned_bin"
    prepend.mkdir()
    _write_exec_env_state(tmp_path, prepend)
    _EnvCapturingTemplate.seen_env = "unset"
    dag = _dag(Node("verification:e2e:flow", "verification_test", attributes={"kind": "e2e", "template_ref": "envcap"}))
    _patch_verify_pipeline(monkeypatch, dag, [])
    monkeypatch.setitem(verify_runner_module.VERIFICATION_TEMPLATES, "envcap", _EnvCapturingTemplate)

    VerifyRunner(tmp_path, {"project": {"type": "generic"}}).run()

    seen = _EnvCapturingTemplate.seen_env
    assert isinstance(seen, Mapping), f"runner did not thread a spawn env: {seen!r}"
    assert seen["PATH"].split(os.pathsep)[0] == str(prepend)


def test_verification_template_env_is_none_without_state_artifact(tmp_path, monkeypatch):
    """Byte-identity: with NO provisioning state (brownfield / other language /
    manual), the runner threads ``env=None`` so the template inherits the ambient
    environment exactly as before this seam existed."""
    _EnvCapturingTemplate.seen_env = "unset"
    dag = _dag(Node("verification:e2e:flow", "verification_test", attributes={"kind": "e2e", "template_ref": "envcap"}))
    _patch_verify_pipeline(monkeypatch, dag, [])
    monkeypatch.setitem(verify_runner_module.VERIFICATION_TEMPLATES, "envcap", _EnvCapturingTemplate)

    VerifyRunner(tmp_path, {"project": {"type": "generic"}}).run()

    assert _EnvCapturingTemplate.seen_env is None


def test_forged_state_dir_is_dropped_no_env_threaded(tmp_path, monkeypatch):
    """Adversarial: a state artifact pointing at a non-existent dir is dropped
    (existence filter), so no env is threaded — a bogus dir can never silently
    redirect a template spawn; it falls through to ambient resolution."""
    _write_exec_env_state(tmp_path, tmp_path / "does_not_exist")
    _EnvCapturingTemplate.seen_env = "unset"
    dag = _dag(Node("verification:e2e:flow", "verification_test", attributes={"kind": "e2e", "template_ref": "envcap"}))
    _patch_verify_pipeline(monkeypatch, dag, [])
    monkeypatch.setitem(verify_runner_module.VERIFICATION_TEMPLATES, "envcap", _EnvCapturingTemplate)

    VerifyRunner(tmp_path, {"project": {"type": "generic"}}).run()

    assert _EnvCapturingTemplate.seen_env is None


def test_old_signature_template_called_without_env(tmp_path, monkeypatch):
    """An out-of-tree template on the legacy two-arg ``execute(command, cwd)`` is
    called WITHOUT ``env`` (signature-checked shim) even when state exists, so it
    keeps working byte-identically instead of raising ``unexpected keyword env``."""
    prepend = tmp_path / "provisioned_bin"
    prepend.mkdir()
    _write_exec_env_state(tmp_path, prepend)

    class _LegacyTemplate:
        called = False

        def generate_test_command(self, runtime_state, test_kind: str) -> str:
            return "true"

        def execute(self, command: str, cwd=None) -> ProviderVerificationResult:  # no env
            type(self).called = True
            return ProviderVerificationResult(True, "ok")

    dag = _dag(Node("verification:e2e:flow", "verification_test", attributes={"kind": "e2e", "template_ref": "legacy"}))
    _patch_verify_pipeline(monkeypatch, dag, [])
    monkeypatch.setitem(verify_runner_module.VERIFICATION_TEMPLATES, "legacy", _LegacyTemplate)

    result = VerifyRunner(tmp_path, {"project": {"type": "generic"}}).run()

    assert _LegacyTemplate.called is True
    assert result.passed is True
