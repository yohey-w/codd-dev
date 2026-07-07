"""v3.15.1 — env/build failure defense (repair-thrash guard, D1-D4).

The Python greenfield dogfood showed the repair engine mutating known-good source
while flailing at an ENVIRONMENT failure (`/bin/sh: python: not found`) it could
not fix by editing code. The env-channel projection (v3.15.0) removes that
specific trigger, but the vulnerability CLASS remains: any env-shaped failure on
the runtime/verification-template surface (a missing tool, a refused connection,
an un-provisioned dependency) can still be force-routed to the engine. This is the
deterministic defense, using the EXISTING `environment_build_error` taxonomy — no
new classification name:

- D1: the runtime-verification failure carries an attribution (failure_class).
- D2: a shell command-not-found is classed `environment_build_error` on any stack.
- D3: `environment_build_error` is deterministically unrepairable (never the LLM).
- D4: the harness env-provision state artifact is fenced from repair edits.
"""

from __future__ import annotations

from codd.repair import auto_scope_guard
from codd.repair.repairability_classifier import NullClassifier, RepairabilityClassifier
from codd.repair.test_failure_attribution import attribute_command_failure
from codd.repair.verify_runner import VerifyRunner


# ── D2: shell command-resolution → environment_build_error (stack-agnostic) ───


def test_shell_not_found_classed_as_environment_over_pytest(tmp_path):
    """A `python -m pytest` command that dies `/bin/sh: python: not found` is an
    ENVIRONMENT failure — the shell adapter wins over the pytest adapter (whose
    predicate the command string also matches), so it is not mis-parsed as an
    `unknown` code failure."""
    attr = attribute_command_failure(
        command="python -m pytest -q tests/e2e/test_x.py",
        output="/bin/sh: 1: python: not found\n",
        project_root=tmp_path,
    )
    assert attr is not None
    assert attr.failure_class == "environment_build_error"
    assert attr.code_addressable is False
    assert attr.failed_nodes == []  # no project path attributed


def test_bash_command_not_found_classed_as_environment(tmp_path):
    attr = attribute_command_failure(
        command="npx vitest run",
        output="bash: node: command not found",
        project_root=tmp_path,
    )
    assert attr is not None
    assert attr.failure_class == "environment_build_error"
    assert attr.code_addressable is False


def test_genuine_pytest_assertion_still_classed_as_code(tmp_path):
    """Guard: the shell adapter must NOT swallow a real pytest assertion failure
    (no shell-not-found signature) — it stays code-addressable."""
    output = (
        "FAILED tests/test_x.py::test_it - AssertionError\n"
        "tests/test_x.py:3: in test_it\n"
        "E   AssertionError\n"
    )
    attr = attribute_command_failure(
        command="python -m pytest -q", output=output, project_root=tmp_path
    )
    assert attr is not None
    assert attr.failure_class == "assertion_failure"


# ── D1: runtime-verification failure carries an attribution ───────────────────


def test_runtime_failure_gets_environment_attribution(tmp_path):
    runner = VerifyRunner(tmp_path, {"project": {"type": "generic"}})
    result = {
        "passed": False,
        "command": "python -m pytest -q tests/e2e/test_x.py",
        "output": "/bin/sh: 1: python: not found\n",
    }
    failure = runner._failure_from_runtime_result(result)
    assert failure is not None
    assert failure.details["failure_class"] == "environment_build_error"
    assert failure.details["code_addressable"] is False


def test_runtime_failure_unrecognized_command_unchanged(tmp_path):
    """Byte-identity: a command with no adapter (curl/cdp) attaches no failure
    class — details stay as before."""
    runner = VerifyRunner(tmp_path, {"project": {"type": "generic"}})
    result = {"passed": False, "command": "curl -s http://x", "output": "curl: (7) refused"}
    failure = runner._failure_from_runtime_result(result)
    assert failure is not None
    assert "failure_class" not in failure.details


# ── D3: environment_build_error is deterministically unrepairable ─────────────


def test_environment_failure_overrides_llm_repairable_verdict():
    """Even when the LLM meta-classifier would call it repairable, an
    environment/build failure is pulled to unrepairable before the LLM is asked."""
    stub_llm = lambda prompt: '{"verification_test_runtime": "repairable"}'  # noqa: E731
    clf = RepairabilityClassifier(llm=stub_llm, repo_path=".")
    env_item = {"check_name": "verification_test_runtime", "failure_class": "environment_build_error"}
    result = clf.classify([env_item])
    assert env_item in result.unrepairable
    assert env_item not in result.repairable


def test_environment_failure_via_details_is_unrepairable():
    """failure_class carried in a nested `details` dict is honored too."""
    clf = RepairabilityClassifier(llm=None, repo_path=".")
    env_item = {"check_name": "verification_test_runtime", "details": {"failure_class": "environment_build_error"}}
    result = clf.classify([env_item])
    assert env_item in result.unrepairable


def test_null_classifier_still_guards_environment_failures():
    env_item = {"check_name": "verification_test_runtime", "failure_class": "environment_build_error"}
    code_item = {"check_name": "test_command", "failure_class": "assertion_failure"}
    result = NullClassifier().classify([env_item, code_item])
    assert env_item in result.unrepairable
    assert code_item in result.repairable


def test_non_environment_failures_flow_normally():
    """The env guard must not disturb ordinary classification: a code failure with
    affected files + code_addressable still routes to repairable (B0)."""
    clf = RepairabilityClassifier(llm=None, repo_path=".")
    code_item = {
        "check_name": "test_command",
        "failure_class": "assertion_failure",
        "code_addressable": True,
        "affected_files": ["src/pkg/mod.py"],
    }
    result = clf.classify([code_item])
    assert code_item in result.repairable


# ── D4: the harness env-provision state artifact is fenced from repair ────────


def test_exec_env_state_artifact_is_gate_controlled():
    assert auto_scope_guard._is_oracle_artifact(".codd/verify/exec_env.json") is True
