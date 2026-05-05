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
