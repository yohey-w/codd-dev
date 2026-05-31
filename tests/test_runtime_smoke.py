"""Tests for codd verify --runtime smoke checks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
from click.testing import CliRunner

from codd.cli import _CliVerificationResult, main
from codd.runtime_smoke.checks import (
    ActionOutcomeChecker,
    CheckResult,
    CrudFlowChecker,
    DbChecker,
    DevServerChecker,
    E2eChecker,
    SmokeConnectivityChecker,
)
from codd.runtime_smoke.config import (
    ActionOutcomeTargetConfig,
    ActionSpecConfig,
    ConnectivityConfig,
    CrudFlowTargetConfig,
    DbCheckConfig,
    DevServerConfig,
    E2eConfig,
    OutcomeExpectationConfig,
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
            "--runtime-skip",
            "global-action",
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        {
            "path": project.resolve(),
            "skip_checks": ("e2e", "global-action"),
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


def test_t15_config_loads_runtime_crud_flow_targets(tmp_path):
    project = _project(
        tmp_path,
        """
runtime_smoke:
  enabled: true
runtime:
  crud_flow_targets:
    - name: add item
      create:
        method: POST
        url: /items
        expected_status: 201
        json:
          name: alpha
      reflect:
        url: /items
        expected_status: 200
        expect_text: alpha
      max_wait_seconds: 2
      poll_interval: 0.1
""",
    )

    config = load_runtime_smoke_config(project)

    assert len(config.crud_flow_targets) == 1
    target = config.crud_flow_targets[0]
    assert target.name == "add item"
    assert target.create is not None
    assert target.create.method == "POST"
    assert target.reflect is not None
    assert target.expect_text == "alpha"
    assert target.max_wait_seconds == 2


def test_t16_crud_flow_http_passes_after_reflection_delay(tmp_path, monkeypatch):
    calls: list[tuple[str, str]] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def request(self, method, url, **kwargs):
            calls.append((method, url))
            if method == "POST":
                return SimpleNamespace(status_code=201, text="")
            if len(calls) == 2:
                return SimpleNamespace(status_code=200, text="not yet")
            return SimpleNamespace(status_code=200, text="alpha is visible")

    monkeypatch.setattr("codd.runtime_smoke.checks.httpx.Client", FakeClient)
    monkeypatch.setattr("codd.runtime_smoke.checks.time.sleep", lambda *_args, **_kwargs: None)

    target = CrudFlowTargetConfig(
        name="add item",
        create=ConnectivityConfig(name="create", method="POST", url="/items", expected_status=201),
        reflect=ConnectivityConfig(name="reflect", url="/items", expected_status=200),
        expect_text="alpha",
        max_wait_seconds=1,
        poll_interval=0,
    )

    result = CrudFlowChecker([target], tmp_path, "http://example.test").run()[0]

    assert result.passed is True
    assert result.category == "crud-flow"
    assert calls == [
        ("POST", "http://example.test/items"),
        ("GET", "http://example.test/items"),
        ("GET", "http://example.test/items"),
    ]


def test_t17_crud_flow_create_failure_fails(tmp_path, monkeypatch):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def request(self, method, url, **kwargs):
            return SimpleNamespace(status_code=500, text="")

    monkeypatch.setattr("codd.runtime_smoke.checks.httpx.Client", FakeClient)
    target = CrudFlowTargetConfig(
        name="add item",
        create=ConnectivityConfig(name="create", method="POST", url="/items", expected_status=201),
        reflect=ConnectivityConfig(name="reflect", url="/items", expected_status=200),
        expect_text="alpha",
    )

    result = CrudFlowChecker([target], tmp_path, "http://example.test").run()[0]

    assert result.passed is False
    assert "HTTP 500" in result.output


def test_t18_missing_crud_flow_config_is_opt_in_no_check(tmp_path, monkeypatch):
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

    result = run_runtime_smoke(project)

    assert "crud-flow" not in [check.category for check in result.checks]
    assert result.overall_passed is False  # connectivity/e2e still require their existing config.


def test_t19_runtime_skip_crud_flow_records_skipped_check(tmp_path, monkeypatch):
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
runtime:
  crud_flow_targets:
    - name: add item
      command: "npm run test:crud"
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

    result = run_runtime_smoke(project, skip_checks=["connectivity", "crud-flow"])

    skipped = [check for check in result.checks if check.skipped]
    assert [check.category for check in skipped] == ["connectivity", "crud-flow"]
    assert result.overall_passed is True


def test_t20_config_loads_runtime_action_outcome_targets(tmp_path):
    project = _project(
        tmp_path,
        """
runtime_smoke:
  enabled: true
runtime:
  action_outcome_targets:
    - name: update item reflects
      action:
        id: item.update
        verb: update
        target: item
        trigger: cli command
        outcomes:
          - server_acceptance
          - visible_reflection
      command: "pytest tests/e2e/test_item_update.py"
""",
    )

    config = load_runtime_smoke_config(project)

    assert len(config.action_outcome_targets) == 1
    target = config.action_outcome_targets[0]
    assert target.name == "update item reflects"
    assert target.command == "pytest tests/e2e/test_item_update.py"
    assert target.actions[0].id == "item.update"
    assert [outcome.name for outcome in target.actions[0].outcomes] == ["server_acceptance", "visible_reflection"]


def test_t21_action_outcome_command_records_action_matrix(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="action ok", stderr="")

    monkeypatch.setattr("codd.runtime_smoke.checks.subprocess.run", fake_run)
    target = ActionOutcomeTargetConfig(
        name="publish emits event",
        command="pytest tests/e2e/test_publish.py",
        actions=[
            ActionSpecConfig(
                id="record.publish",
                verb="publish",
                target="record",
                trigger="CLI",
                outcomes=[OutcomeExpectationConfig("server_acceptance"), OutcomeExpectationConfig("emitted_event")],
            )
        ],
    )

    result = ActionOutcomeChecker([target], tmp_path, None).run()[0]
    markdown = generate_markdown_section([result], overall_passed=True)

    assert result.passed is True
    assert result.category == "action-outcome"
    assert result.details["actions"][0]["id"] == "record.publish"
    assert "Action Outcome Matrix" in markdown
    assert "emitted_event" in markdown


def test_t22_action_outcome_http_passes_after_observed_outcome(tmp_path, monkeypatch):
    calls: list[tuple[str, str]] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def request(self, method, url, **kwargs):
            calls.append((method, url))
            if method == "POST":
                return SimpleNamespace(status_code=202, text="")
            if len(calls) == 2:
                return SimpleNamespace(status_code=200, text="not yet")
            return SimpleNamespace(status_code=200, text="record.publish event")

    monkeypatch.setattr("codd.runtime_smoke.checks.httpx.Client", FakeClient)
    monkeypatch.setattr("codd.runtime_smoke.checks.time.sleep", lambda *_args, **_kwargs: None)
    target = ActionOutcomeTargetConfig(
        name="publish event",
        invoke=ConnectivityConfig(name="invoke", method="POST", url="/api/publish", expected_status=202),
        observe=ConnectivityConfig(name="observe", method="GET", url="/api/events", expected_status=200),
        expect_text="record.publish",
        forbid_text="error",
        max_wait_seconds=1,
        poll_interval=0,
        actions=[
            ActionSpecConfig(
                id="record.publish",
                verb="publish",
                target="record",
                outcomes=[OutcomeExpectationConfig("emitted_event")],
            )
        ],
    )

    result = ActionOutcomeChecker([target], tmp_path, "http://example.test").run()[0]

    assert result.passed is True
    assert calls == [
        ("POST", "http://example.test/api/publish"),
        ("GET", "http://example.test/api/events"),
        ("GET", "http://example.test/api/events"),
    ]


def test_t23_runtime_skip_action_outcome_records_skipped_check(tmp_path, monkeypatch):
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
runtime:
  action_outcome_targets:
    - name: update item reflects
      action:
        id: item.update
        verb: update
        outcomes: [visible_reflection]
      command: "pytest tests/e2e/test_item_update.py"
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

    result = run_runtime_smoke(project, skip_checks=["connectivity", "e2e", "action-outcome"])

    skipped = [check for check in result.checks if check.skipped]
    assert [check.category for check in skipped] == ["connectivity", "e2e", "action-outcome"]
    assert "| Action outcome | SKIPPED |" in result.markdown_section


def test_t43_config_loads_runtime_global_action_targets(tmp_path):
    project = _project(
        tmp_path,
        """
runtime_smoke:
  enabled: true
runtime:
  global_action_targets:
    - name: mobile sign-out remains available
      action:
        id: session.sign_out
        verb: read
        target: session_action
        trigger: authenticated user opens compact viewport
        outcomes:
          - breakpoint_available
          - session_absence_after_action
      command: "pytest tests/e2e/test_mobile_session_action.py"
""",
    )

    config = load_runtime_smoke_config(project)

    assert len(config.global_action_targets) == 1
    target = config.global_action_targets[0]
    assert target.name == "mobile sign-out remains available"
    assert target.command == "pytest tests/e2e/test_mobile_session_action.py"
    assert target.actions[0].id == "session.sign_out"
    assert [outcome.name for outcome in target.actions[0].outcomes] == [
        "breakpoint_available",
        "session_absence_after_action",
    ]


def test_t44_global_action_command_records_runtime_category(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "codd.runtime_smoke.checks.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="global action ok", stderr=""),
    )
    target = ActionOutcomeTargetConfig(
        name="compact sign-out",
        command="pytest tests/e2e/test_compact_signout.py",
        actions=[
            ActionSpecConfig(
                id="session.sign_out",
                verb="read",
                target="session_action",
                trigger="compact viewport",
                outcomes=[
                    OutcomeExpectationConfig("breakpoint_available"),
                    OutcomeExpectationConfig("session_absence_after_action"),
                ],
            )
        ],
    )

    result = ActionOutcomeChecker(
        [target],
        tmp_path,
        None,
        category="global-action",
        label="Global action",
        missing_config_message="runtime.global_action_targets requires command or invoke+observe",
    ).run()[0]
    markdown = generate_markdown_section([result], overall_passed=True)

    assert result.passed is True
    assert result.category == "global-action"
    assert "Global action: compact sign-out" in markdown
    assert "session_absence_after_action" in markdown


def test_t45_runtime_skip_global_action_records_skipped_check(tmp_path, monkeypatch):
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
runtime:
  global_action_targets:
    - name: compact sign-out
      action:
        id: session.sign_out
        outcomes: [breakpoint_available, session_absence_after_action]
      command: "pytest tests/e2e/test_compact_signout.py"
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

    result = run_runtime_smoke(project, skip_checks=["connectivity", "e2e", "global-action"])

    skipped = [check for check in result.checks if check.skipped]
    assert [check.category for check in skipped] == ["connectivity", "e2e", "global-action"]
    assert "| Global action | SKIPPED |" in result.markdown_section


def test_t46_doctor_warns_when_authenticated_responsive_ui_lacks_global_action_target(tmp_path):
    project = _project(tmp_path)
    app = project / "src" / "app"
    app.mkdir(parents=True)
    (app / "layout.tsx").write_text(
        "export default async function Layout({ children }) { "
        "const session = await getServerSession(); "
        "return <div><aside className=\"hidden lg:block\"><nav>Menu</nav></aside>{children}</div>; }\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "CoDD doctor: WARN" in result.output
    assert "runtime.global_action_targets" in result.output
    assert "desktop-visible session controls are not accepted as mobile coverage" in result.output


def test_t47_doctor_accepts_authenticated_responsive_ui_with_global_action_target(tmp_path):
    project = _project(
        tmp_path,
        """
runtime:
  global_action_targets:
    - name: compact sign-out
      action:
        id: session.sign_out
        outcomes: [breakpoint_available, session_absence_after_action]
      command: "pytest tests/e2e/test_compact_signout.py"
""",
    )
    app = project / "src" / "app"
    app.mkdir(parents=True)
    (app / "layout.tsx").write_text(
        "export default async function Layout({ children }) { "
        "const session = await getServerSession(); "
        "return <div><aside className=\"hidden lg:block\"><nav>Menu</nav></aside>{children}</div>; }\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "runtime.global_action_targets" not in result.output


def test_t48_doctor_warns_on_missing_presentation_obligation(tmp_path):
    project = _project(tmp_path)
    docs = project / "docs" / "design"
    docs.mkdir(parents=True)
    (docs / "presentation.md").write_text(
        """
---
display_fields:
  - field_id: record.published_at
    data_type: datetime
    lexicon_refs: ["i18n_unicode_cldr#time_zone_handling"]
    presentation_required: true
---
# Presentation
""".lstrip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "CoDD doctor: WARN" in result.output
    assert "W-PRES-001" in result.output
    assert "W-PRES-002" in result.output
    assert "displayed field `record.published_at`" in result.output


def test_t49_doctor_warns_on_missing_aggregation_policy(tmp_path):
    project = _project(tmp_path)
    docs = project / "docs" / "design"
    docs.mkdir(parents=True)
    (docs / "presentation.md").write_text(
        """
---
display_fields:
  - field_id: record.summary_value
    cardinality: "0..N"
    aggregation_required: true
---
# Presentation
""".lstrip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "CoDD doctor: WARN" in result.output
    assert "W-AGG-001" in result.output
    assert "collection/cardinality display field `record.summary_value`" in result.output


def test_t50_doctor_accepts_declared_presentation_and_aggregation_obligations(tmp_path):
    project = _project(tmp_path)
    docs = project / "docs" / "design"
    docs.mkdir(parents=True)
    (docs / "presentation.md").write_text(
        """
---
display_fields:
  - field_id: record.published_at
    data_type: datetime
    lexicon_refs: ["i18n_unicode_cldr#time_zone_handling"]
    presentation_required: true
  - field_id: record.summary_value
    cardinality: "0..N"
    aggregation_required: true
presentation_specs:
  - field_id: record.published_at
    format: "YYYY-MM-DD HH:mm"
    timezone: "Etc/UTC"
    locale: "en-US"
aggregation_policies:
  - field_id: record.summary_value
    cardinality_when_many:
      policy: average
    test_data_variants:
      required_cardinality: ["0", "1", "N"]
---
# Presentation
""".lstrip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "W-PRES" not in result.output
    assert "W-AGG" not in result.output


def test_t24_doctor_warns_on_post_without_reflection_e2e(tmp_path):
    project = _project(tmp_path)
    source_dir = project / "src"
    source_dir.mkdir()
    (source_dir / "routes.ts").write_text("export async function POST() { return Response.json({ok:true}) }\n")

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "CoDD doctor: WARN" in result.output
    assert "runtime.crud_flow_targets" in result.output


def test_t25_doctor_suppressed_by_crud_flow_target(tmp_path):
    project = _project(
        tmp_path,
        """
runtime:
  crud_flow_targets:
    - name: add item
      command: "npm run test:crud"
""",
    )
    source_dir = project / "src"
    source_dir.mkdir()
    (source_dir / "routes.ts").write_text("export async function POST() { return Response.json({ok:true}) }\n")

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "CoDD doctor: PASS" in result.output


def test_t26_doctor_warns_when_post_test_only_asserts_status(tmp_path):
    project = _project(tmp_path)
    source_dir = project / "src"
    tests_dir = project / "tests"
    source_dir.mkdir()
    tests_dir.mkdir()
    (source_dir / "routes.ts").write_text("export async function POST() { return Response.json({ok:true}, {status:201}) }\n")
    (tests_dir / "create.spec.ts").write_text(
        """
test("create returns 201", async ({ request }) => {
  const response = await request.post("/api/items", { data: { name: "x" } });
  expect(response.status()).toBe(201);
});
"""
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "CoDD doctor: WARN" in result.output
    assert "visible list/detail reflection" in result.output


def test_t27_doctor_accepts_post_with_visible_reflection_e2e(tmp_path):
    project = _project(tmp_path)
    source_dir = project / "src"
    tests_dir = project / "tests"
    source_dir.mkdir()
    tests_dir.mkdir()
    (source_dir / "routes.ts").write_text("export async function POST() { return Response.json({ok:true}, {status:201}) }\n")
    (tests_dir / "create.spec.ts").write_text(
        """
test("created item appears in list", async ({ page, request }) => {
  await request.post("/api/items", { data: { name: "codd-runtime-smoke" } });
  await page.goto("/items");
  await expect(page.getByText("codd-runtime-smoke")).toBeVisible();
});
"""
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "CoDD doctor: PASS" in result.output


def test_t28_doctor_crud_flow_only_does_not_cover_operation_flow_update_delete(tmp_path):
    project = _project(
        tmp_path,
        """
operation_flow:
  operations:
    - id: item_update
      verb: edit
      target: item
    - id: item_delete
      verb: remove
      target: item
runtime:
  crud_flow_targets:
    - name: add item
      command: "npm run test:create"
""",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "CoDD doctor: WARN" in result.output
    assert "runtime.crud_flow_targets" in result.output
    assert "operation_flow update/delete/command action coverage" in result.output


def test_t29_doctor_action_outcome_target_covers_operation_flow_action(tmp_path):
    project = _project(
        tmp_path,
        """
operation_flow:
  operations:
    - id: item_update
      verb: edit
      target: item
runtime:
  action_outcome_targets:
    - name: item update
      action:
        id: item.update
        verb: update
        target: item
        outcomes: [visible_reflection]
      command: "npm run test:update"
""",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "CoDD doctor: PASS" in result.output


def test_t30_config_loads_connectivity_body_and_header_assertions(tmp_path):
    project = _project(
        tmp_path,
        """
runtime_smoke:
  enabled: true
  smoke_connectivity:
    - name: read page
      url: /records
      expect_text: Records
      forbid_text: fixture
      expect_headers:
        X-Frame-Options: DENY
      forbid_headers:
        Location: localhost
""",
    )

    config = load_runtime_smoke_config(project)
    check = config.smoke_connectivity[0]

    assert check.expect_text == "Records"
    assert check.forbid_text == "fixture"
    assert check.expect_headers == {"X-Frame-Options": "DENY"}
    assert check.forbid_headers == {"Location": "localhost"}


def test_t31_connectivity_forbid_text_marks_page_fail(monkeypatch):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.cookies = httpx.Cookies()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def request(self, *args, **kwargs):
            return SimpleNamespace(
                status_code=200,
                text="normal page with fixture marker",
                headers={},
                elapsed=SimpleNamespace(total_seconds=lambda: 0.02),
            )

    monkeypatch.setattr("codd.runtime_smoke.checks.httpx.Client", FakeClient)

    result = SmokeConnectivityChecker(
        [ConnectivityConfig(name="read page", url="/records", forbid_text="fixture")],
        "http://example.test",
    ).run()[0]

    assert result.passed is False
    assert "forbid_text='fixture'" in result.output


def test_t32_connectivity_forbid_header_marks_redirect_fail(monkeypatch):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.cookies = httpx.Cookies()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def request(self, *args, **kwargs):
            return SimpleNamespace(
                status_code=302,
                text="",
                headers={"Location": "http://localhost:3000/login"},
                elapsed=SimpleNamespace(total_seconds=lambda: 0.02),
            )

    monkeypatch.setattr("codd.runtime_smoke.checks.httpx.Client", FakeClient)

    result = SmokeConnectivityChecker(
        [
            ConnectivityConfig(
                name="login redirect",
                url="/login",
                expected_status=302,
                forbid_headers={"Location": "localhost"},
            )
        ],
        "http://example.test",
    ).run()[0]

    assert result.passed is False
    assert "forbid_headers" in result.output
    assert "Location does not contain 'localhost': False" in result.output


def test_t33_doctor_warns_on_synthetic_mutation_without_persistence(tmp_path):
    project = _project(
        tmp_path,
        """
runtime:
  action_outcome_targets:
    - name: record create
      action:
        id: record.create
        verb: create
        target: record
        outcomes: [visible_reflection]
      command: "npm run test:create-record"
""",
    )
    source_dir = project / "src"
    source_dir.mkdir()
    (source_dir / "routes.ts").write_text(
        "export async function POST() { return Response.json({ id: crypto.randomUUID(), ok: true }) }\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "CoDD doctor: WARN" in result.output
    assert "synthetic success" in result.output


def test_t34_doctor_accepts_mutation_with_persistence_evidence(tmp_path):
    project = _project(
        tmp_path,
        """
runtime:
  action_outcome_targets:
    - name: record create
      action:
        id: record.create
        verb: create
        target: record
        outcomes: [visible_reflection]
      command: "npm run test:create-record"
""",
    )
    source_dir = project / "src"
    source_dir.mkdir()
    (source_dir / "routes.ts").write_text(
        "export async function POST() { const record = await db.record.create({ data: {} }); return Response.json(record) }\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "CoDD doctor: PASS" in result.output


def test_t35_doctor_warns_on_unconnected_mutating_button(tmp_path):
    project = _project(tmp_path)
    source_dir = project / "src"
    source_dir.mkdir()
    (source_dir / "RecordActions.tsx").write_text(
        "export function RecordActions() { return <button type=\"button\">Delete record</button>; }\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "CoDD doctor: WARN" in result.output
    assert "Interactive control `Delete record`" in result.output


def test_t36_doctor_accepts_connected_mutating_button(tmp_path):
    project = _project(tmp_path)
    source_dir = project / "src"
    source_dir.mkdir()
    (source_dir / "RecordActions.tsx").write_text(
        "export function RecordActions() { return <button type=\"button\" onClick={deleteRecord}>Delete record</button>; }\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "CoDD doctor: PASS" in result.output


def test_t37_doctor_warns_on_weak_action_outcome_metadata(tmp_path):
    project = _project(
        tmp_path,
        """
runtime:
  action_outcome_targets:
    - name: record update
      action:
        id: record.update
        verb: update
        target: record
        outcomes: [server_acceptance]
      command: "npm run test:update-record"
""",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "CoDD doctor: WARN" in result.output
    assert "only weak outcome metadata" in result.output


def test_t38_doctor_warns_when_terminal_action_lacks_control_state_outcome(tmp_path):
    project = _project(
        tmp_path,
        """
runtime:
  action_outcome_targets:
    - name: record complete
      action:
        id: record.complete
        verb: complete
        target: record
        outcomes: [visible_reflection, reload_persistence]
      command: "npm run test:complete-record"
""",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "CoDD doctor: WARN" in result.output
    assert "terminal/non-repeatable verb `complete`" in result.output


def test_t39_doctor_warns_when_update_action_id_names_terminal_outcome(tmp_path):
    project = _project(
        tmp_path,
        """
runtime:
  action_outcome_targets:
    - name: record completion
      action:
        id: record_complete
        verb: update
        target: record
        outcomes: [visible_reflection, reload_persistence]
      command: "npm run test:complete-record"
""",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "CoDD doctor: WARN" in result.output
    assert "terminal/non-repeatable verb `complete`" in result.output


def test_t40_doctor_accepts_terminal_action_with_disabled_state_outcome(tmp_path):
    project = _project(
        tmp_path,
        """
runtime:
  action_outcome_targets:
    - name: record complete
      action:
        id: record.complete
        verb: complete
        target: record
        outcomes: [visible_reflection, disabled_state]
      command: "npm run test:complete-record"
""",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "terminal/non-repeatable verb" not in result.output


def test_t41_doctor_warns_when_business_screen_lacks_escape_route(tmp_path):
    project = _project(tmp_path)
    screen = project / "src" / "app" / "notifications"
    screen.mkdir(parents=True)
    (screen / "page.tsx").write_text(
        'export default function Page() { return <main><h1>Messages</h1><p>Updates</p></main>; }\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "CoDD doctor: WARN" in result.output
    assert "escape route/navigation evidence" in result.output


def test_t42_doctor_accepts_business_screen_with_ancestor_navigation(tmp_path):
    project = _project(tmp_path)
    app = project / "src" / "app"
    screen = app / "notifications"
    screen.mkdir(parents=True)
    (app / "layout.tsx").write_text(
        'import Link from "next/link"; export default function Layout({ children }) { return <><nav><Link href="/dashboard">Dashboard</Link></nav>{children}</>; }\n',
        encoding="utf-8",
    )
    (screen / "page.tsx").write_text(
        'export default function Page() { return <main><h1>Messages</h1><p>Updates</p></main>; }\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "escape route/navigation evidence" not in result.output
