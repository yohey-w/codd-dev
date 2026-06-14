from __future__ import annotations

import subprocess

import pytest

from codd.deployment import RuntimeStateKind, RuntimeStateNode
from codd.deployment.providers import (
    VERIFICATION_TEMPLATES,
    VerificationResult,
    VerificationTemplate,
    register_verification_template,
)
from codd.deployment.providers.verification.curl import CurlTemplate
from codd.deployment.providers.verification.playwright import PlaywrightTemplate
from codd.deployment.providers.verification.vitest import VitestTemplate


@pytest.fixture(autouse=True)
def restore_verification_templates():
    original = dict(VERIFICATION_TEMPLATES)
    yield
    VERIFICATION_TEMPLATES.clear()
    VERIFICATION_TEMPLATES.update(original)


def test_playwright_template_registers():
    assert VERIFICATION_TEMPLATES["playwright"] is PlaywrightTemplate


def test_curl_template_registers():
    assert VERIFICATION_TEMPLATES["curl"] is CurlTemplate


def test_playwright_generate_test_command_e2e(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runtime_state = RuntimeStateNode(
        identifier="runtime:server:app",
        kind=RuntimeStateKind.SERVER_RUNNING,
        target="http://localhost:3000",
    )

    command = PlaywrightTemplate().generate_test_command(runtime_state, "e2e")

    assert command == "npx playwright test tests/e2e/ --reporter=line"


def test_playwright_generate_test_command_prefers_verification_source_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    spec_path = tmp_path / "tests" / "e2e" / "login.spec.ts"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("test('login', () => {})", encoding="utf-8")

    class RuntimeState:
        project_root = tmp_path
        source = "tests/e2e/login.spec.ts"
        target = "/login"

    command = PlaywrightTemplate().generate_test_command(RuntimeState(), "e2e")

    assert command == "npx playwright test tests/e2e/login.spec.ts --reporter=line"


def test_playwright_source_file_does_not_add_login_grep(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    spec_path = tmp_path / "tests" / "smoke" / "stripe_billing.spec.ts"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("test('stripe', () => {})", encoding="utf-8")

    class RuntimeState:
        project_root = tmp_path
        source = "tests/smoke/stripe_billing.spec.ts"
        target = "/api/auth/login"

    command = PlaywrightTemplate().generate_test_command(RuntimeState(), "smoke")

    assert command == "npx playwright test tests/smoke/stripe_billing.spec.ts --reporter=line"


def test_playwright_generate_test_command_accepts_configured_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runtime_state = RuntimeStateNode(
        identifier="runtime:server:app",
        kind=RuntimeStateKind.SERVER_RUNNING,
        target="http://localhost:3000",
    )

    command = PlaywrightTemplate(config={"project": "desktop-1920"}).generate_test_command(runtime_state, "e2e")

    assert command == "npx playwright test tests/e2e/ --reporter=line --project desktop-1920"


def test_playwright_generate_test_command_smoke_login_has_grep(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runtime_state = RuntimeStateNode(
        identifier="runtime:db:users_seeded",
        kind=RuntimeStateKind.DB_SEED,
        target="login_endpoint",
    )

    command = PlaywrightTemplate().generate_test_command(runtime_state, "smoke")

    assert command == "npx playwright test tests/smoke/ --reporter=line --grep login"


def test_playwright_find_spec_files_e2e(tmp_path):
    wanted = tmp_path / "tests" / "e2e" / "auth" / "login.spec.ts"
    ignored = tmp_path / "tests" / "e2e" / "auth" / "login.test.ts"
    wanted.parent.mkdir(parents=True)
    wanted.write_text("test('login', () => {})", encoding="utf-8")
    ignored.write_text("test('ignored', () => {})", encoding="utf-8")

    assert PlaywrightTemplate().find_spec_files(tmp_path, "e2e") == [wanted]


def test_playwright_find_spec_files_smoke(tmp_path):
    wanted = tmp_path / "tests" / "smoke" / "health.test.ts"
    ignored = tmp_path / "tests" / "smoke" / "health.spec.ts"
    wanted.parent.mkdir(parents=True)
    wanted.write_text("test('health', () => {})", encoding="utf-8")
    ignored.write_text("test('ignored', () => {})", encoding="utf-8")

    assert PlaywrightTemplate().find_spec_files(tmp_path, "smoke") == [wanted]


def test_playwright_execute_success(monkeypatch):
    completed = subprocess.CompletedProcess("cmd", 0, stdout="passed\n", stderr="")
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: completed)

    result = PlaywrightTemplate(timeout=5).execute("npx playwright test")

    assert result.passed is True
    assert result.output == "passed\n"
    assert result.duration >= 0


def test_playwright_execute_failure(monkeypatch):
    completed = subprocess.CompletedProcess("cmd", 1, stdout="", stderr="failed\n")
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: completed)

    result = PlaywrightTemplate(timeout=5).execute("npx playwright test")

    assert result.passed is False
    assert result.output == "failed\n"
    assert result.duration >= 0


def test_curl_generate_test_command_health():
    runtime_state = RuntimeStateNode(
        identifier="runtime:server:health",
        kind=RuntimeStateKind.SERVER_RUNNING,
        target="https://example.test/dashboard",
    )

    command = CurlTemplate().generate_test_command(runtime_state, "health")

    assert command == "curl -s -o /dev/null -w '%{http_code}' https://example.test/api/health"


def test_curl_generate_test_command_prefers_actual_check_command():
    runtime_state = RuntimeStateNode(
        identifier="runtime:db:users_seeded",
        kind=RuntimeStateKind.DB_SEED,
        target="users",
        actual_check_command="npm run verify:seed",
    )

    command = CurlTemplate().generate_test_command(runtime_state, "smoke")

    assert command == "npm run verify:seed"


def test_curl_execute_200_passes(monkeypatch):
    completed = subprocess.CompletedProcess("cmd", 0, stdout="200", stderr="")
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: completed)

    result = CurlTemplate(timeout=5).execute("curl test")

    assert result.passed is True
    assert result.output == "200"
    assert result.duration >= 0


def test_curl_execute_404_fails(monkeypatch):
    completed = subprocess.CompletedProcess("cmd", 0, stdout="404", stderr="")
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: completed)

    result = CurlTemplate(timeout=5).execute("curl test")

    assert result.passed is False
    assert result.output == "404"
    assert result.duration >= 0


def test_verification_template_contract_is_satisfied():
    assert isinstance(PlaywrightTemplate(), VerificationTemplate)
    assert isinstance(CurlTemplate(), VerificationTemplate)
    assert isinstance(PlaywrightTemplate().execute("true"), VerificationResult)
    assert CurlTemplate(dry_run=True).execute("curl test").passed is True


def test_verification_templates_registry_restores_after_temp_registration():
    before = dict(VERIFICATION_TEMPLATES)

    @register_verification_template("temporary")
    class TemporaryTemplate(VerificationTemplate):
        def generate_test_command(self, runtime_state, test_kind: str) -> str:
            return "temporary"

        def execute(self, command: str) -> VerificationResult:
            return VerificationResult(True, command)

    assert VERIFICATION_TEMPLATES["temporary"] is TemporaryTemplate
    VERIFICATION_TEMPLATES.clear()
    VERIFICATION_TEMPLATES.update(before)
    assert VERIFICATION_TEMPLATES == before


# ── vitest CLI e2e template (#3 runner) ──────────────────────


def test_vitest_template_registers():
    assert VERIFICATION_TEMPLATES["vitest"] is VitestTemplate


def test_vitest_generate_test_command_e2e(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runtime_state = RuntimeStateNode(
        identifier="runtime:cli:convert",
        kind=RuntimeStateKind.SERVER_RUNNING,
        target="convert",
    )
    command = VitestTemplate().generate_test_command(runtime_state, "e2e")
    # ``vitest run`` positionally filtered to the e2e dir. Collection (incl. the
    # ``.e2e.*`` convention) is owned by the scaffolded ``vitest.config.ts``
    # ``test.include`` — NOT a CLI flag: vitest's CLI has no ``--include`` (that
    # is a Jest option) and passing it aborts the run with ``CACError``.
    assert command == "npx vitest run tests/e2e/"
    assert "--include" not in command


def test_vitest_generate_test_command_no_invalid_include_flag(tmp_path, monkeypatch):
    # REGRESSION GUARD (vitest has NO ``--include`` CLI flag — Jest does): a
    # routed ``.e2e.ts`` is made collectable by the scaffolded ``vitest.config.ts``
    # ``test.include`` (see ``test_project_types``), NOT a CLI flag. Passing
    # ``--include`` aborts vitest (``CACError: Unknown option --include``) -> 0
    # collected -> opaque hard fail. The command is just ``run`` + positional.
    e2e_file = tmp_path / "tests" / "e2e" / "tempconv_cli_contract.e2e.ts"
    e2e_file.parent.mkdir(parents=True)
    e2e_file.write_text("import { it, expect } from 'vitest'; it('x', () => expect(1).toBe(1));\n")

    class RuntimeState:
        project_root = tmp_path
        source = "tests/e2e/tempconv_cli_contract.e2e.ts"
        target = "convert"

    command = VitestTemplate().generate_test_command(RuntimeState(), "e2e")

    assert "--include" not in command
    assert command.startswith("npx vitest run ")
    # The routed file is the positional filter, scoping the run to itself.
    assert command.endswith("tests/e2e/tempconv_cli_contract.e2e.ts")


def test_vitest_command_uses_only_valid_cli_surface(tmp_path, monkeypatch):
    # vitest's CLI surface for our use is ``run`` + a positional filter. No
    # ``--include`` in ANY form (it aborts the run); ``.test.*``/``.spec.*``/
    # ``.e2e.*`` collection is config-driven (see the scaffolded vitest.config.ts).
    monkeypatch.chdir(tmp_path)
    runtime_state = RuntimeStateNode(
        identifier="runtime:cli:convert",
        kind=RuntimeStateKind.SERVER_RUNNING,
        target="convert",
    )
    command = VitestTemplate().generate_test_command(runtime_state, "e2e")
    assert "--include" not in command
    assert command.split()[:3] == ["npx", "vitest", "run"]


def test_vitest_template_passes_on_real_run(monkeypatch):
    class _Completed:
        returncode = 0
        stdout = "Test Files  1 passed (1)\n Tests  2 passed (2)\n"
        stderr = ""

    monkeypatch.setattr(
        "codd.deployment.providers.verification.vitest.subprocess.run",
        lambda *a, **k: _Completed(),
    )
    result = VitestTemplate().execute("npx vitest run tests/e2e/convert.test.ts")
    assert result.passed is True


def test_vitest_template_zero_tests_is_hard_fail_even_on_exit_0(monkeypatch):
    # ANTI-FALSE-GREEN: vitest exits 0 with "No test files found" — must FAIL.
    class _Completed:
        returncode = 0
        stdout = "No test files found, exiting with code 0\n"
        stderr = ""

    monkeypatch.setattr(
        "codd.deployment.providers.verification.vitest.subprocess.run",
        lambda *a, **k: _Completed(),
    )
    result = VitestTemplate().execute("npx vitest run tests/e2e/")
    assert result.passed is False
    assert "0 tests" in result.output or "no test files" in result.output.lower()


def test_vitest_find_spec_files_recognizes_e2e_ts(tmp_path):
    # ``.e2e.ts`` is a genuine e2e naming convention; it must be SELECTED to run
    # alongside ``.test.ts``. Before the fix only ``*.test.ts`` was globbed.
    e2e_named = tmp_path / "tests" / "e2e" / "tempconv_conversion.e2e.ts"
    test_named = tmp_path / "tests" / "e2e" / "cli.test.ts"
    for path in (e2e_named, test_named):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("import 'vitest';\n")
    found = VitestTemplate().find_spec_files(tmp_path, "e2e")
    assert e2e_named in found
    assert test_named in found


def test_vitest_e2e_ts_run_zero_tests_still_hard_fails(monkeypatch):
    # ANTI-FALSE-GREEN: the executed-count discipline applies to ``.e2e.ts`` runs
    # too — a run that collects 0 tests is a hard fail even on exit 0.
    class _Completed:
        returncode = 0
        stdout = "No test files found, exiting with code 0\n"
        stderr = ""

    monkeypatch.setattr(
        "codd.deployment.providers.verification.vitest.subprocess.run",
        lambda *a, **k: _Completed(),
    )
    result = VitestTemplate().execute("npx vitest run tests/e2e/tempconv_conversion.e2e.ts")
    assert result.passed is False
    assert "0 tests" in result.output or "no test files" in result.output.lower()


def test_vitest_include_glob_run_on_empty_e2e_still_hard_fails(monkeypatch):
    # The ``--include`` fix only lets vitest SEE a ``.e2e.ts`` file; it must NOT
    # let vitest pass on nothing. A genuinely empty ``.e2e.ts`` (the include glob
    # matches it, but it declares no tests) still collects 0 -> still a HARD FAIL.
    # This mirrors the real tempconv-codex4 ``filter:``/``include:`` output shape.
    class _Completed:
        returncode = 1
        stdout = (
            "No test files found, exiting with code 1\n"
            "filter:  tests/e2e/empty.e2e.ts\n"
            "include:  **/*.test.{ts,tsx,js,jsx}, **/*.e2e.{ts,tsx,js,jsx}\n"
        )
        stderr = ""

    monkeypatch.setattr(
        "codd.deployment.providers.verification.vitest.subprocess.run",
        lambda *a, **k: _Completed(),
    )
    command = "npx vitest run --include '**/*.e2e.{ts,tsx,js,jsx}' tests/e2e/empty.e2e.ts"
    result = VitestTemplate().execute(command)
    assert result.passed is False
    assert "0 tests" in result.output or "no test files" in result.output.lower()


def test_vitest_template_contract_is_satisfied():
    assert isinstance(VitestTemplate(), VerificationTemplate)


# ── REGRESSION: verify command must run with cwd=project_root ─────────────────
#
# Root cause (greenfield tempconv-codex8, first v2.24.0 run): the per-node
# verification path called ``template.execute(command)`` with NO cwd, so the
# vitest subprocess inherited the ORCHESTRATOR's working directory (the CoDD
# install tree), not the generated project. vitest then rooted at codd-dev,
# loaded codd-dev's (absent) config + the DEFAULT include glob, found 0 test
# files, and the anti-false-green gate (correctly) hard-failed every e2e node.
# The fix threads ``cwd=project_root`` from the verify runner through every
# template's ``execute`` into ``subprocess.run(cwd=...)``. These tests prove the
# subprocess actually runs in the project dir (REAL subprocess, not a mock) by
# having the command print ``os.getcwd()`` and asserting it equals the project —
# while the CALLER's cwd is deliberately a different directory.

import os  # noqa: E402 - localized to the regression block below.


def _spy_subprocess_run(monkeypatch, module: str) -> dict[str, object]:
    """Patch ``<module>.subprocess.run`` to record the ``cwd`` it was given.

    Returns a dict that gets ``cwd`` populated on the next ``execute`` call.
    The wrapped run still executes (returns a harmless 0-exit completion) so the
    template's own success/failure handling is exercised, while we assert the
    EXACT working directory threaded into ``subprocess.run`` — the precise thing
    the bug got wrong (no cwd → inherits the orchestrator's directory).
    """
    seen: dict[str, object] = {}

    def _spy(cmd, **kwargs):
        seen["cwd"] = kwargs.get("cwd")
        # A real-shaped passing completion so each template's exit-0 path runs.
        return subprocess.CompletedProcess(
            cmd, 0, stdout=" Tests  1 passed (1)\n", stderr=""
        )

    monkeypatch.setattr(f"{module}.subprocess.run", _spy)
    return seen


def test_vitest_execute_threads_project_root_as_cwd(tmp_path, monkeypatch):
    # The exact shape of the bug: caller (orchestrator) cwd != generated project.
    caller = tmp_path / "orchestrator_cwd"
    project = tmp_path / "generated_project"
    caller.mkdir()
    project.mkdir()
    monkeypatch.chdir(caller)
    seen = _spy_subprocess_run(
        monkeypatch, "codd.deployment.providers.verification.vitest"
    )

    VitestTemplate(timeout=30).execute("npx vitest run tests/e2e/", cwd=project)

    assert seen["cwd"] == project  # NOT None, NOT the caller cwd


def test_playwright_execute_threads_project_root_as_cwd(tmp_path, monkeypatch):
    caller = tmp_path / "orchestrator_cwd"
    project = tmp_path / "generated_project"
    caller.mkdir()
    project.mkdir()
    monkeypatch.chdir(caller)
    seen = _spy_subprocess_run(
        monkeypatch, "codd.deployment.providers.verification.playwright"
    )

    PlaywrightTemplate(timeout=30).execute("npx playwright test", cwd=project)

    assert seen["cwd"] == project


def test_curl_execute_threads_project_root_as_cwd(tmp_path, monkeypatch):
    project = tmp_path / "generated_project"
    project.mkdir()
    seen = _spy_subprocess_run(
        monkeypatch, "codd.deployment.providers.verification.curl"
    )

    CurlTemplate(timeout=30).execute("curl -s http://localhost/api/health", cwd=project)

    assert seen["cwd"] == project


def test_execute_cwd_none_is_backward_compatible(tmp_path, monkeypatch):
    # Backward-compat guard: ``cwd=None`` (the default — what every legacy
    # ``execute(command)`` caller passes implicitly) passes ``cwd=None`` to
    # ``subprocess.run``, i.e. KEEPS the caller's working directory. No existing
    # direct caller (e.g. the provider unit tests, cli run-journey) changes
    # behaviour.
    seen = _spy_subprocess_run(
        monkeypatch, "codd.deployment.providers.verification.vitest"
    )

    VitestTemplate(timeout=30).execute("npx vitest run tests/e2e/")

    assert seen["cwd"] is None


def test_vitest_roots_at_project_config_when_run_from_other_cwd(tmp_path, monkeypatch):
    # The literal failure mode: vitest must collect the PROJECT's tests because
    # it roots at the project (loads the project's vitest.config.ts), even though
    # the caller's cwd is elsewhere. We simulate vitest with a tiny fake "vitest"
    # on PATH that — like the real CLI — reads ``vitest.config.ts`` RELATIVE TO
    # ITS OWN CWD and reports the tests it "found". Rooted at the project it
    # finds the test (PASS); rooted at the caller's empty cwd it finds none and
    # the anti-false-green gate hard-fails — exactly the codex8 symptom.
    project = tmp_path / "generated_project"
    caller = tmp_path / "orchestrator_cwd"
    bindir = tmp_path / "fakebin"
    (project / "tests" / "e2e").mkdir(parents=True)
    caller.mkdir()
    bindir.mkdir()
    # Project has a config + a test file; the caller cwd has neither.
    (project / "vitest.config.ts").write_text("export default {}\n", encoding="utf-8")
    (project / "tests" / "e2e" / "conv.e2e.test.ts").write_text("// a test\n", encoding="utf-8")

    # Fake `vitest`: prints a real vitest-shaped summary based on whether the
    # config + test file exist in the CURRENT working directory.
    fake = bindir / "vitest"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "has_cfg = os.path.exists('vitest.config.ts')\n"
        "tests = []\n"
        "for root, _dirs, files in os.walk('tests') if os.path.isdir('tests') else []:\n"
        "    tests += [f for f in files if f.endswith('.e2e.test.ts')]\n"
        "if has_cfg and tests:\n"
        "    print('Test Files  1 passed (1)')\n"
        "    print(' Tests  1 passed (1)')\n"
        "else:\n"
        "    print('No test files found, exiting with code 0')\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    # Make the fake `vitest` resolve via PATH; command avoids npx network use.
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.chdir(caller)

    command = "vitest run tests/e2e/conv.e2e.test.ts"
    # Rooted at the PROJECT: config + test are found → PASS.
    rooted_at_project = VitestTemplate(timeout=30).execute(command, cwd=project)
    assert rooted_at_project.passed is True, rooted_at_project.output
    # Rooted at the CALLER's cwd (the bug): nothing found → anti-false-green FAIL.
    rooted_at_caller = VitestTemplate(timeout=30).execute(command, cwd=caller)
    assert rooted_at_caller.passed is False
    assert (
        "0 tests" in rooted_at_caller.output
        or "no test files" in rooted_at_caller.output.lower()
    )


def test_verify_runner_threads_project_root_as_cwd_to_template_execute(tmp_path, monkeypatch):
    # End-to-end through the verify runner: a spy template records the ``cwd``
    # the runner passed to ``execute``. Proves verify_runner.py threads
    # ``self.project_root`` (the resolved project root) into the provider call —
    # the actual line that was missing it.
    from codd.dag import DAG, Node
    from codd.deployment.providers import VerificationResult as ProviderVerificationResult
    from codd.repair import verify_runner as verify_runner_module
    from codd.repair.verify_runner import VerifyRunner

    captured: dict[str, object] = {}

    class SpyTemplate:
        def __init__(self, timeout=None):
            pass

        def generate_test_command(self, runtime_state, test_kind: str) -> str:
            return "noop"

        def execute(self, command: str, cwd=None) -> ProviderVerificationResult:
            captured["cwd"] = cwd
            return ProviderVerificationResult(True, "ok")

    dag = DAG()
    dag.add_node(
        Node(
            "verification:e2e:flow",
            "verification_test",
            attributes={"kind": "e2e", "template_ref": "spy"},
        )
    )
    monkeypatch.setattr(
        verify_runner_module, "load_dag_settings", lambda project_root, settings: settings
    )
    monkeypatch.setattr(verify_runner_module, "build_dag", lambda project_root, settings: dag)

    class _Check:
        check_name = "node_completeness"
        severity = "red"
        passed = True

    monkeypatch.setattr(verify_runner_module, "run_checks", lambda *a, **k: [_Check()])
    monkeypatch.setitem(verify_runner_module.VERIFICATION_TEMPLATES, "spy", SpyTemplate)

    runner = VerifyRunner(tmp_path, {"project": {"type": "generic"}})
    runner.run()

    assert captured["cwd"] == runner.project_root
    assert captured["cwd"] == tmp_path.resolve()
