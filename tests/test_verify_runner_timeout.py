from __future__ import annotations

import itertools
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

        def execute(self, command: str, cwd=None) -> ProviderVerificationResult:
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

        def execute(self, command: str, cwd=None) -> ProviderVerificationResult:
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

        def execute(self, command: str, cwd=None) -> ProviderVerificationResult:
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

        def execute(self, command: str, cwd=None) -> ProviderVerificationResult:
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

        def execute(self, command: str, cwd=None) -> ProviderVerificationResult:
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


# ─────────────────────────────────────────────────────────────────────────────
# Run-scoped overrides + verification-node coverage.
#
# The committed timeout is a permanent policy; "run everything once" is a
# run-scoped intent. T07-T11 prove the second can be expressed without editing
# the first. T12-T18 prove a run that skipped nodes nobody asked it to skip is
# visible always, and RED on request.
# ─────────────────────────────────────────────────────────────────────────────

_TIMEOUT_ENV_VARS = (
    "CODD_VERIFICATION_TIMEOUT_TOTAL_SECONDS",
    "CODD_VERIFICATION_TIMEOUT_PER_NODE_SECONDS",
    "CODD_VERIFY_REQUIRE_COMPLETE_VERIFICATION",
)


def _clear_run_scoped_env(monkeypatch) -> None:
    """No ambient value from the developer's shell may reach a test."""
    for name in _TIMEOUT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class _PassingTemplate:
    """Records every command it is asked to run; always green."""

    executed: list[str] = []

    def __init__(self, timeout: float | None = None) -> None:
        self.timeout = timeout

    def generate_test_command(self, runtime_state, test_kind: str) -> str:
        return str(runtime_state.identifier)

    def execute(self, command: str, cwd=None) -> ProviderVerificationResult:
        type(self).executed.append(command)
        return ProviderVerificationResult(True, "ok")


def _passing_template(monkeypatch) -> type[_PassingTemplate]:
    template = type("RecordingTemplate", (_PassingTemplate,), {"executed": []})
    monkeypatch.setitem(verify_runner_module.VERIFICATION_TEMPLATES, "fake", template)
    return template


def _five_node_run(monkeypatch, tmp_path: Path, settings: dict, *, step: int = 3):
    nodes = [_verification_node(f"verification:e2e:{index}") for index in range(5)]
    _patch_pipeline(monkeypatch, _dag(*nodes))
    clock = itertools.count(0, step)
    monkeypatch.setattr(verify_runner_module.time, "monotonic", lambda: next(clock))
    template = _passing_template(monkeypatch)
    return VerifyRunner(tmp_path, settings).run(), template


def test_t07_committed_total_budget_still_applies_when_env_is_unset(tmp_path: Path, monkeypatch):
    """Control for T08: without the override the committed budget clips the run."""
    _clear_run_scoped_env(monkeypatch)

    result, template = _five_node_run(
        monkeypatch, tmp_path, {"verify": {"verification_timeout": {"total_seconds": 5}}}
    )

    assert template.executed == ["verification:e2e:0"]
    assert sum(1 for item in result.runtime_results if item["skipped"]) == 4


def test_t08_env_total_seconds_overrides_the_committed_budget_for_one_run(tmp_path: Path, monkeypatch):
    """The whole point: full coverage WITHOUT editing the project's config."""
    _clear_run_scoped_env(monkeypatch)
    monkeypatch.setenv("CODD_VERIFICATION_TIMEOUT_TOTAL_SECONDS", "3600")

    settings = {"verify": {"verification_timeout": {"total_seconds": 5}}}
    result, template = _five_node_run(monkeypatch, tmp_path, settings)

    assert len(template.executed) == 5
    assert sum(1 for item in result.runtime_results if item["skipped"]) == 0
    assert result.passed is True
    # The committed policy is untouched — the caller's mapping is not mutated.
    assert settings == {"verify": {"verification_timeout": {"total_seconds": 5}}}


def test_t09_env_per_node_seconds_overrides_the_committed_cap(tmp_path: Path, monkeypatch):
    _clear_run_scoped_env(monkeypatch)
    monkeypatch.setenv("CODD_VERIFICATION_TIMEOUT_PER_NODE_SECONDS", "45")
    seen: list[float | None] = []

    class FakeTemplate:
        def __init__(self, timeout: float | None = None) -> None:
            seen.append(timeout)

        def generate_test_command(self, runtime_state, test_kind: str) -> str:
            return "ok"

        def execute(self, command: str, cwd=None) -> ProviderVerificationResult:
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

    assert seen == [45]


def test_t10_blank_env_value_is_inert(tmp_path: Path, monkeypatch):
    """An exported-but-empty variable must not read as "no timeout"."""
    _clear_run_scoped_env(monkeypatch)
    monkeypatch.setenv("CODD_VERIFICATION_TIMEOUT_TOTAL_SECONDS", "   ")

    result, template = _five_node_run(
        monkeypatch, tmp_path, {"verify": {"verification_timeout": {"total_seconds": 5}}}
    )

    assert template.executed == ["verification:e2e:0"]
    assert sum(1 for item in result.runtime_results if item["skipped"]) == 4


def test_t11_malformed_env_value_fails_honestly_instead_of_falling_back(tmp_path: Path, monkeypatch):
    """No silent fallback: a typo'd budget must not quietly run the old policy."""
    _clear_run_scoped_env(monkeypatch)
    monkeypatch.setenv("CODD_VERIFICATION_TIMEOUT_TOTAL_SECONDS", "1h")

    result, _template = _five_node_run(
        monkeypatch, tmp_path, {"verify": {"verification_timeout": {"total_seconds": 5}}}
    )

    assert result.passed is False
    assert "CODD_VERIFICATION_TIMEOUT_TOTAL_SECONDS" in result.failures[0].message


def test_t12_involuntary_skips_warn_even_when_the_run_passes(tmp_path: Path, monkeypatch):
    """Visibility is not opt-in: green-by-absence announces itself."""
    _clear_run_scoped_env(monkeypatch)

    result, _template = _five_node_run(
        monkeypatch, tmp_path, {"verify": {"verification_timeout": {"total_seconds": 5}}}
    )

    assert result.passed is True  # unchanged default behavior (F6)
    warning = next(text for text in result.warnings if "skipped without being asked" in text)
    assert "4 of 5 verification node(s)" in warning
    assert "total_timeout_exceeded=4" in warning
    assert "only 1 actually executed" in warning


def test_t13_require_complete_verification_turns_mass_skip_red(tmp_path: Path, monkeypatch):
    _clear_run_scoped_env(monkeypatch)

    result, _template = _five_node_run(
        monkeypatch,
        tmp_path,
        {
            "verify": {
                "verification_timeout": {"total_seconds": 5},
                "require_complete_verification": True,
            }
        },
    )

    assert result.passed is False
    failure = next(item for item in result.failures if item.check_name == "verification_coverage")
    assert failure.details["skip_reasons"] == {"total_timeout_exceeded": 4}
    assert failure.details["failed_nodes"] == [f"verification:e2e:{index}" for index in range(1, 5)]
    # An unexecuted node is not a code defect — never a repair target.
    assert failure.details["code_addressable"] is False


def test_t14_require_complete_verification_can_be_demanded_per_run(tmp_path: Path, monkeypatch):
    """Run-scoped entry, so proving the rule needs no committed-config change."""
    _clear_run_scoped_env(monkeypatch)
    monkeypatch.setenv("CODD_VERIFY_REQUIRE_COMPLETE_VERIFICATION", "1")

    settings = {"verify": {"verification_timeout": {"total_seconds": 5}}}
    result, _template = _five_node_run(monkeypatch, tmp_path, settings)

    assert result.passed is False
    assert any(item.check_name == "verification_coverage" for item in result.failures)
    assert settings == {"verify": {"verification_timeout": {"total_seconds": 5}}}


def test_t15_env_false_overrides_a_committed_true(tmp_path: Path, monkeypatch):
    _clear_run_scoped_env(monkeypatch)
    monkeypatch.setenv("CODD_VERIFY_REQUIRE_COMPLETE_VERIFICATION", "off")

    result, _template = _five_node_run(
        monkeypatch,
        tmp_path,
        {
            "verify": {
                "verification_timeout": {"total_seconds": 5},
                "require_complete_verification": True,
            }
        },
    )

    assert result.passed is True


def test_t16_malformed_boolean_env_value_fails_honestly(tmp_path: Path, monkeypatch):
    _clear_run_scoped_env(monkeypatch)
    monkeypatch.setenv("CODD_VERIFY_REQUIRE_COMPLETE_VERIFICATION", "maybe")

    result, _template = _five_node_run(
        monkeypatch, tmp_path, {"verify": {"verification_timeout": {"total_seconds": 5}}}
    )

    assert result.passed is False
    assert "CODD_VERIFY_REQUIRE_COMPLETE_VERIFICATION" in result.failures[0].message


def test_t16b_malformed_boolean_is_reported_even_with_nothing_skipped(tmp_path: Path, monkeypatch):
    """Otherwise an operator who believes the rule is armed gets an unevaluated green."""
    _clear_run_scoped_env(monkeypatch)
    monkeypatch.setenv("CODD_VERIFY_REQUIRE_COMPLETE_VERIFICATION", "maybe")
    monkeypatch.setenv("CODD_VERIFICATION_TIMEOUT_TOTAL_SECONDS", "3600")

    result, template = _five_node_run(
        monkeypatch, tmp_path, {"verify": {"verification_timeout": {"total_seconds": 5}}}
    )

    assert len(template.executed) == 5  # nothing was skipped
    assert result.passed is False
    assert "CODD_VERIFY_REQUIRE_COMPLETE_VERIFICATION" in result.failures[0].message


def test_t17_declared_runtime_skip_is_a_contract_not_a_hole(tmp_path: Path, monkeypatch):
    """`--runtime-skip` states an intent; the coverage rule must not punish it."""
    _clear_run_scoped_env(monkeypatch)
    monkeypatch.setenv("CODD_VERIFY_REQUIRE_COMPLETE_VERIFICATION", "1")

    class FakeTemplate:
        def generate_test_command(self, runtime_state, test_kind: str) -> str:
            raise AssertionError("declared skip must bypass template execution")

    _patch_pipeline(
        monkeypatch,
        _dag(_verification_node("verification:e2e:first"), _verification_node("verification:e2e:second")),
    )
    monkeypatch.setitem(verify_runner_module.VERIFICATION_TEMPLATES, "fake", FakeTemplate)

    result = VerifyRunner(tmp_path, {"project": {"type": "generic"}}, runtime_skip=("verification-test",)).run()

    assert result.passed is True
    assert not any("skipped without being asked" in text for text in result.warnings)


def test_t18_complete_run_passes_the_coverage_rule_and_stays_silent(tmp_path: Path, monkeypatch):
    _clear_run_scoped_env(monkeypatch)
    monkeypatch.setenv("CODD_VERIFY_REQUIRE_COMPLETE_VERIFICATION", "true")
    monkeypatch.setenv("CODD_VERIFICATION_TIMEOUT_TOTAL_SECONDS", "3600")

    result, template = _five_node_run(
        monkeypatch, tmp_path, {"verify": {"verification_timeout": {"total_seconds": 5}}}
    )

    assert len(template.executed) == 5
    assert result.passed is True
    assert not any("skipped without being asked" in text for text in result.warnings)


def test_t19_cli_surfaces_the_coverage_warning_and_the_skip_reason(tmp_path: Path, monkeypatch):
    """The operator must see the hole in the terminal, not only in the object."""
    _clear_run_scoped_env(monkeypatch)
    project_root = tmp_path / "project"
    (project_root / "codd").mkdir(parents=True)
    (project_root / "codd" / "codd.yaml").write_text(
        "project:\n  type: generic\nverify:\n  verification_timeout:\n    total_seconds: 5\n",
        encoding="utf-8",
    )

    nodes = [_verification_node(f"verification:e2e:{index}") for index in range(5)]
    _patch_pipeline(monkeypatch, _dag(*nodes))
    clock = itertools.count(0, 3)
    monkeypatch.setattr(verify_runner_module.time, "monotonic", lambda: next(clock))
    _passing_template(monkeypatch)

    cli_result = CliRunner().invoke(main, ["verify", "--path", str(project_root)])

    assert "skipped without being asked" in cli_result.output
    assert "total_timeout_exceeded=4" in cli_result.output
