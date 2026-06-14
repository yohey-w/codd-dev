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
