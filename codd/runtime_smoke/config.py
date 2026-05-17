"""Configuration model for runtime smoke verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codd.config import load_project_config


@dataclass(frozen=True)
class DbCheckConfig:
    command: str | None = None
    expected_exit_code: int = 0
    timeout: float | None = None


@dataclass(frozen=True)
class DevServerConfig:
    url: str | None = None
    expected_status: int = 200
    timeout: float = 10.0


@dataclass(frozen=True)
class ConnectivityConfig:
    name: str
    method: str = "GET"
    url: str = ""
    expected_status: int = 200
    timeout: float = 5.0
    cookie_jar: str | None = None
    save_cookie_jar: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: Any | None = None
    json: Any | None = None


@dataclass(frozen=True)
class E2eConfig:
    command: str | None = None
    working_dir: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout: float | None = None


@dataclass(frozen=True)
class ReportConfig:
    log_to_file: bool = True
    file_path: str = "reports/runtime_smoke_{{timestamp}}.md"
    fail_fast: bool = False


@dataclass(frozen=True)
class RuntimeSmokeConfig:
    project_root: Path
    enabled: bool = False
    db_check: DbCheckConfig = field(default_factory=DbCheckConfig)
    dev_server: DevServerConfig = field(default_factory=DevServerConfig)
    smoke_connectivity: list[ConnectivityConfig] = field(default_factory=list)
    e2e: E2eConfig = field(default_factory=E2eConfig)
    report: ReportConfig = field(default_factory=ReportConfig)


def load_runtime_smoke_config(project_root: Path | str, base_url_override: str | None = None) -> RuntimeSmokeConfig:
    """Load and validate the ``runtime_smoke`` section from ``codd.yaml``."""
    root = Path(project_root).resolve()
    raw_project_config = load_project_config(root)
    raw = raw_project_config.get("runtime_smoke", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("runtime_smoke must be a YAML mapping")

    dev_server = _dev_server_config(_mapping(raw.get("dev_server"), "runtime_smoke.dev_server"), base_url_override)
    return RuntimeSmokeConfig(
        project_root=root,
        enabled=_bool(raw.get("enabled", False), "runtime_smoke.enabled"),
        db_check=_db_check_config(_mapping(raw.get("db_check"), "runtime_smoke.db_check")),
        dev_server=dev_server,
        smoke_connectivity=_connectivity_configs(raw.get("smoke_connectivity")),
        e2e=_e2e_config(_mapping(raw.get("e2e"), "runtime_smoke.e2e")),
        report=_report_config(_mapping(raw.get("report"), "runtime_smoke.report")),
    )


def _db_check_config(raw: dict[str, Any]) -> DbCheckConfig:
    return DbCheckConfig(
        command=_optional_string(raw.get("command"), "runtime_smoke.db_check.command"),
        expected_exit_code=_int(raw.get("expected_exit_code", 0), "runtime_smoke.db_check.expected_exit_code"),
        timeout=_optional_float(raw.get("timeout"), "runtime_smoke.db_check.timeout"),
    )


def _dev_server_config(raw: dict[str, Any], base_url_override: str | None) -> DevServerConfig:
    url = base_url_override or _optional_string(raw.get("url"), "runtime_smoke.dev_server.url")
    timeout_value = raw.get("timeout", raw.get("timeout_seconds", 10))
    return DevServerConfig(
        url=url,
        expected_status=_int(raw.get("expected_status", 200), "runtime_smoke.dev_server.expected_status"),
        timeout=_float(timeout_value, "runtime_smoke.dev_server.timeout"),
    )


def _connectivity_configs(raw: Any) -> list[ConnectivityConfig]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValueError("runtime_smoke.smoke_connectivity must be a list")

    configs: list[ConnectivityConfig] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"runtime_smoke.smoke_connectivity[{index}] must be a mapping")
        url = _string(item.get("url"), f"runtime_smoke.smoke_connectivity[{index}].url")
        timeout_value = item.get("timeout", item.get("max_time_seconds", 5))
        configs.append(
            ConnectivityConfig(
                name=str(item.get("name") or f"connectivity {index}"),
                method=str(item.get("method") or "GET").upper(),
                url=url,
                expected_status=_int(
                    item.get("expected_status", 200),
                    f"runtime_smoke.smoke_connectivity[{index}].expected_status",
                ),
                timeout=_float(timeout_value, f"runtime_smoke.smoke_connectivity[{index}].timeout"),
                cookie_jar=_optional_string(item.get("cookie_jar"), f"runtime_smoke.smoke_connectivity[{index}].cookie_jar"),
                save_cookie_jar=_optional_string(
                    item.get("save_cookie_jar"),
                    f"runtime_smoke.smoke_connectivity[{index}].save_cookie_jar",
                ),
                headers=_string_mapping(item.get("headers"), f"runtime_smoke.smoke_connectivity[{index}].headers"),
                body=item.get("body"),
                json=item.get("json"),
            )
        )
    return configs


def _e2e_config(raw: dict[str, Any]) -> E2eConfig:
    return E2eConfig(
        command=_optional_string(raw.get("command"), "runtime_smoke.e2e.command"),
        working_dir=_optional_string(raw.get("working_dir"), "runtime_smoke.e2e.working_dir"),
        env=_string_mapping(raw.get("env"), "runtime_smoke.e2e.env"),
        timeout=_optional_float(raw.get("timeout"), "runtime_smoke.e2e.timeout"),
    )


def _report_config(raw: dict[str, Any]) -> ReportConfig:
    file_path = raw.get("file_path", raw.get("output_path", "reports/runtime_smoke_{{timestamp}}.md"))
    return ReportConfig(
        log_to_file=_bool(raw.get("log_to_file", True), "runtime_smoke.report.log_to_file"),
        file_path=_string(file_path, "runtime_smoke.report.file_path"),
        fail_fast=_bool(raw.get("fail_fast", False), "runtime_smoke.report.fail_fast"),
    )


def _mapping(value: Any, field_name: str) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a mapping")
    return value


def _string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _optional_string(value: Any, field_name: str) -> str | None:
    if value in (None, ""):
        return None
    return _string(value, field_name)


def _string_mapping(value: Any, field_name: str) -> dict[str, str]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a mapping")
    return {str(key): str(raw_value) for key, raw_value in value.items()}


def _bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be true or false")
    return value


def _int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _float(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    return float(value)


def _optional_float(value: Any, field_name: str) -> float | None:
    if value in (None, ""):
        return None
    return _float(value, field_name)
