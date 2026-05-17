"""Runtime smoke orchestrator for ``codd verify --runtime``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from codd.runtime_smoke.checks import (
    CheckResult,
    DbChecker,
    DevServerChecker,
    E2eChecker,
    SmokeConnectivityChecker,
    skipped_result,
)
from codd.runtime_smoke.config import RuntimeSmokeConfig, load_runtime_smoke_config
from codd.runtime_smoke.report import generate_markdown_section, write_markdown_report


@dataclass
class SmokeResult:
    checks: list[CheckResult]
    overall_passed: bool
    markdown_section: str
    report_path: Path | None = None

    @property
    def passed(self) -> bool:
        return self.overall_passed


def run_runtime_smoke(
    config: RuntimeSmokeConfig | str | Path,
    skip_checks: list[str] | tuple[str, ...] | None = None,
    base_url_override: str | None = None,
) -> SmokeResult:
    """Run runtime smoke checks and return structured results."""
    runtime_config = (
        config
        if isinstance(config, RuntimeSmokeConfig)
        else load_runtime_smoke_config(Path(config).resolve(), base_url_override=base_url_override)
    )
    skip_set = set(skip_checks or [])
    checks: list[CheckResult] = []

    if not runtime_config.enabled:
        checks.append(skipped_result("runtime", "Runtime smoke", "runtime_smoke.enabled is false"))
        return _finish(runtime_config, checks, write_report=False)

    _run_category(
        "db",
        checks,
        skip_set,
        lambda: [DbChecker(runtime_config.db_check, runtime_config.project_root).run()],
    )
    if _should_stop(runtime_config, checks):
        return _finish(runtime_config, checks)

    _run_category(
        "dev-server",
        checks,
        skip_set,
        lambda: [DevServerChecker(runtime_config.dev_server).run()],
    )
    if _should_stop(runtime_config, checks):
        return _finish(runtime_config, checks)

    _run_category(
        "connectivity",
        checks,
        skip_set,
        lambda: SmokeConnectivityChecker(runtime_config.smoke_connectivity, runtime_config.dev_server.url).run(),
    )
    if _should_stop(runtime_config, checks):
        return _finish(runtime_config, checks)

    _run_category(
        "e2e",
        checks,
        skip_set,
        lambda: [E2eChecker(runtime_config.e2e, runtime_config.project_root, runtime_config.dev_server.url).run()],
    )
    return _finish(runtime_config, checks)


def _run_category(category: str, checks: list[CheckResult], skip_set: set[str], runner) -> None:
    names = {
        "db": "DB up",
        "dev-server": "Dev server up",
        "connectivity": "Smoke connectivity",
        "e2e": "Real-browser E2E",
    }
    if category in skip_set:
        checks.append(skipped_result(category, names[category], f"--runtime-skip {category}"))
        return
    checks.extend(runner())


def _finish(runtime_config: RuntimeSmokeConfig, checks: list[CheckResult], *, write_report: bool = True) -> SmokeResult:
    overall_passed = all(result.passed or result.skipped for result in checks)
    markdown = generate_markdown_section(checks, overall_passed)
    result = SmokeResult(checks=checks, overall_passed=overall_passed, markdown_section=markdown)
    if write_report and runtime_config.report.log_to_file:
        result.report_path = write_markdown_report(result, _report_path(runtime_config))
    return result


def _should_stop(runtime_config: RuntimeSmokeConfig, checks: list[CheckResult]) -> bool:
    return bool(runtime_config.report.fail_fast and any(not result.passed and not result.skipped for result in checks))


def _report_path(runtime_config: RuntimeSmokeConfig) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_path = runtime_config.report.file_path.replace("{{timestamp}}", timestamp)
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return runtime_config.project_root / path
