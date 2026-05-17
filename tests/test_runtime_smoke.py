"""Tests for codd verify --runtime smoke checks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
from click.testing import CliRunner

from codd.cli import _CliVerificationResult, main
from codd.runtime_smoke.checks import (
    CheckResult,
    DbChecker,
    DevServerChecker,
    E2eChecker,
    SmokeConnectivityChecker,
)
from codd.runtime_smoke.config import (
    ConnectivityConfig,
    DbCheckConfig,
    DevServerConfig,
    E2eConfig,
    load_runtime_smoke_config,
)
from codd.runtime_smoke.report import generate_markdown_section
from codd.runtime_smoke.runner import SmokeResult, run_runtime_smoke


def _project(tmp_path: Path, runtime_smoke: str = "") -> Path:
    project = tmp_path / "app"
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True)
    (codd_dir / "codd.yaml").write_text(
        f"""
version: "0.1.0"
project:
  name: app
  language: python
scan:
  source_dirs:
    - src/
{runtime_smoke}
""".lstrip(),
        encoding="utf-8",
    )
    return project


def test_t01_config_defaults_disabled_runtime_smoke(tmp_path):
    project = _project(tmp_path)

    config = load_runtime_smoke_config(project)

    assert config.enabled is False
    assert config.db_check.expected_exit_code == 0
    assert config.dev_server.expected_status == 200
    assert config.dev_server.timeout == 10
    assert config.report.log_to_file is True


def test_t02_db_check_success_when_command_exits_0(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="db ok\n", stderr="")

    monkeypatch.setattr("codd.runtime_smoke.checks.subprocess.run", fake_run)

    result = DbChecker(DbCheckConfig(command="nc -z localhost 5432"), tmp_path).run()

    assert result.passed is True
    assert result.category == "db"
    assert "db ok" in result.output


def test_t03_db_check_fail_when_command_returns_nonzero(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="refused\n")

    monkeypatch.setattr("codd.runtime_smoke.checks.subprocess.run", fake_run)

    result = DbChecker(DbCheckConfig(command="nc -z localhost 5432"), tmp_path).run()

    assert result.passed is False
    assert "expected=0" in result.output
    assert "refused" in result.output


def test_t04_dev_server_check_200(monkeypatch):
    monkeypatch.setattr(
        "codd.runtime_smoke.checks.httpx.get",
        lambda *args, **kwargs: SimpleNamespace(status_code=200),
    )

    result = DevServerChecker(DevServerConfig(url="http://127.0.0.1:3000")).run()

    assert result.passed is True
    assert "HTTP 200" in result.output


def test_t05_dev_server_check_timeout(monkeypatch):
    def fake_get(*args, **kwargs):
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr("codd.runtime_smoke.checks.httpx.get", fake_get)

    result = DevServerChecker(DevServerConfig(url="http://127.0.0.1:3000", timeout=0.1)).run()

    assert result.passed is False
    assert "timeout" in result.output


def test_t06_connectivity_check_with_cookie_jar_chain(monkeypatch):
    calls: list[tuple[str, str]] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.cookies = httpx.Cookies()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def request(self, method, url, **kwargs):
            calls.append((method, url))
            self.cookies.set("session", "ok")
            return SimpleNamespace(status_code=200, elapsed=SimpleNamespace(total_seconds=lambda: 0.05))

    monkeypatch.setattr("codd.runtime_smoke.checks.httpx.Client", FakeClient)
    checks = [
        ConnectivityConfig(name="login", method="POST", url="/login", save_cookie_jar="admin"),
        ConnectivityConfig(name="admin", method="GET", url="/admin", cookie_jar="admin"),
    ]

    results = SmokeConnectivityChecker(checks, "http://example.test").run()

    assert [result.passed for result in results] == [True, True]
    assert calls == [("POST", "http://example.test/login"), ("GET", "http://example.test/admin")]


def test_t07_connectivity_check_max_time_exceeded_marks_fail(monkeypatch):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.cookies = httpx.Cookies()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def request(self, *args, **kwargs):
            return SimpleNamespace(status_code=200, elapsed=SimpleNamespace(total_seconds=lambda: 9.0))

    monkeypatch.setattr("codd.runtime_smoke.checks.httpx.Client", FakeClient)

    result = SmokeConnectivityChecker(
        [ConnectivityConfig(name="slow", url="/slow", timeout=1)],
        "http://example.test",
    ).run()[0]

    assert result.passed is False
    assert "elapsed 9.000s <= 1.000s" in result.output


def test_t08_e2e_check_subprocess_exit_0_pass(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="e2e ok", stderr="")

    monkeypatch.setattr("codd.runtime_smoke.checks.subprocess.run", fake_run)

    result = E2eChecker(E2eConfig(command="npx playwright test"), tmp_path, "http://127.0.0.1:3000").run()

    assert result.passed is True
    assert "e2e ok" in result.output


def test_t09_e2e_check_subprocess_exit_1_fail(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="failed")

    monkeypatch.setattr("codd.runtime_smoke.checks.subprocess.run", fake_run)

    result = E2eChecker(E2eConfig(command="npx playwright test"), tmp_path, "http://127.0.0.1:3000").run()

    assert result.passed is False
    assert "exit_code=1" in result.output
    assert "failed" in result.output


def test_t10_runtime_smoke_disabled_in_config_emits_warning_and_skips(tmp_path):
    project = _project(tmp_path, "runtime_smoke:\n  enabled: false\n")

    result = run_runtime_smoke(project)

    assert result.overall_passed is True
    assert result.checks[0].skipped is True
    assert "runtime_smoke.enabled is false" in result.markdown_section


def test_t11_runtime_smoke_fail_fast_short_circuits_subsequent_checks(tmp_path, monkeypatch):
    project = _project(
        tmp_path,
        """
runtime_smoke:
  enabled: true
  db_check:
    command: "check-db"
  dev_server:
    url: "http://127.0.0.1:3000"
  report:
    fail_fast: true
""",
    )

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="down")

    def fail_http(*args, **kwargs):
        raise AssertionError("dev server should not run after fail_fast db failure")

    monkeypatch.setattr("codd.runtime_smoke.checks.subprocess.run", fake_run)
    monkeypatch.setattr("codd.runtime_smoke.checks.httpx.get", fail_http)

    result = run_runtime_smoke(project)

    assert result.overall_passed is False
    assert [check.category for check in result.checks] == ["db"]


def test_t12_report_markdown_contains_all_4_sections():
    checks = [
        CheckResult(True, "DB up", "ok", 0.01, category="db"),
        CheckResult(True, "Dev server up", "ok", 0.02, category="dev-server"),
        CheckResult(True, "Smoke connectivity: login", "ok", 0.03, category="connectivity"),
        CheckResult(True, "Real-browser E2E", "ok", 0.04, category="e2e"),
    ]

    markdown = generate_markdown_section(checks, overall_passed=True)

    assert "## § Step 8 Runtime Smoke" in markdown
    assert "DB up" in markdown
    assert "Dev server up" in markdown
    assert "Smoke connectivity: login" in markdown
    assert "Real-browser E2E" in markdown


def test_t13_cli_runtime_flag_invokes_smoke_runner(tmp_path, monkeypatch):
    project = _project(tmp_path)
    calls: list[dict] = []

    monkeypatch.setattr(
        "codd.cli._run_verify_once",
        lambda **kwargs: _CliVerificationResult(passed=True, exit_code=0),
    )

    def fake_runtime(path, skip_checks=None, base_url_override=None):
        calls.append({"path": path, "skip_checks": skip_checks, "base_url_override": base_url_override})
        return SmokeResult(
            checks=[CheckResult(True, "DB up", "ok", 0.01, category="db")],
            overall_passed=True,
            markdown_section="## § Step 8 Runtime Smoke\n\nOverall: PASS\n",
        )

    monkeypatch.setattr("codd.runtime_smoke.runner.run_runtime_smoke", fake_runtime)

    result = CliRunner().invoke(
        main,
        [
            "verify",
            "--path",
            str(project),
            "--runtime",
            "--runtime-base-url",
            "http://127.0.0.1:3001",
            "--runtime-skip",
            "e2e",
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        {
            "path": project.resolve(),
            "skip_checks": ("e2e",),
            "base_url_override": "http://127.0.0.1:3001",
        }
    ]
    assert "Step 8 Runtime Smoke" in result.output


def test_t14_runtime_skip_records_skipped_check(tmp_path, monkeypatch):
    project = _project(
        tmp_path,
        """
runtime_smoke:
  enabled: true
  db_check:
    command: "check-db"
  dev_server:
    url: "http://127.0.0.1:3000"
  e2e:
    command: "npx playwright test"
  report:
    log_to_file: false
""",
    )

    monkeypatch.setattr(
        "codd.runtime_smoke.checks.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
    )
    monkeypatch.setattr(
        "codd.runtime_smoke.checks.httpx.get",
        lambda *args, **kwargs: SimpleNamespace(status_code=200),
    )

    result = run_runtime_smoke(project, skip_checks=["connectivity", "e2e"])

    skipped = [check for check in result.checks if check.skipped]
    assert [check.category for check in skipped] == ["connectivity", "e2e"]
    assert result.overall_passed is True
