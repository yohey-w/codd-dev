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
    expect_text: str | None = None
    forbid_text: str | None = None
    expect_headers: dict[str, str] = field(default_factory=dict)
    forbid_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class E2eConfig:
    command: str | None = None
    working_dir: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout: float | None = None


@dataclass(frozen=True)
class CrudFlowTargetConfig:
    name: str
    command: str | None = None
    working_dir: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout: float | None = None
    create: ConnectivityConfig | None = None
    reflect: ConnectivityConfig | None = None
    expect_text: str | None = None
    poll_interval: float = 0.5
    max_wait_seconds: float = 10.0


@dataclass(frozen=True)
class OutcomeExpectationConfig:
    name: str
    required: bool = True


@dataclass(frozen=True)
class ActionSpecConfig:
    id: str
    verb: str | None = None
    target: str | None = None
    trigger: str | None = None
    outcomes: list[OutcomeExpectationConfig] = field(default_factory=list)
    actor: str | None = None
    actors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ActionOutcomeTargetConfig:
    name: str
    actions: list[ActionSpecConfig]
    command: str | None = None
    working_dir: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout: float | None = None
    invoke: ConnectivityConfig | None = None
    observe: ConnectivityConfig | None = None
    expect_text: str | None = None
    forbid_text: str | None = None
    poll_interval: float = 0.5
    max_wait_seconds: float = 10.0


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
    crud_flow_targets: list[CrudFlowTargetConfig] = field(default_factory=list)
    action_outcome_targets: list[ActionOutcomeTargetConfig] = field(default_factory=list)
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
    runtime_raw = raw_project_config.get("runtime", {})
    if runtime_raw is None:
        runtime_raw = {}
    if not isinstance(runtime_raw, dict):
        raise ValueError("runtime must be a YAML mapping")

    dev_server = _dev_server_config(_mapping(raw.get("dev_server"), "runtime_smoke.dev_server"), base_url_override)
    return RuntimeSmokeConfig(
        project_root=root,
        enabled=_bool(raw.get("enabled", False), "runtime_smoke.enabled"),
        db_check=_db_check_config(_mapping(raw.get("db_check"), "runtime_smoke.db_check")),
        dev_server=dev_server,
        smoke_connectivity=_connectivity_configs(raw.get("smoke_connectivity")),
        e2e=_e2e_config(_mapping(raw.get("e2e"), "runtime_smoke.e2e")),
        crud_flow_targets=_crud_flow_targets(runtime_raw.get("crud_flow_targets", raw.get("crud_flow_targets"))),
        action_outcome_targets=_action_outcome_targets(runtime_raw.get("action_outcome_targets")),
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
        configs.append(_connectivity_config(item, f"runtime_smoke.smoke_connectivity[{index}]", f"connectivity {index}"))
    return configs


def _connectivity_config(raw: Any, field_name: str, default_name: str) -> ConnectivityConfig:
    if not isinstance(raw, dict):
        raise ValueError(f"{field_name} must be a mapping")
    url = _string(raw.get("url"), f"{field_name}.url")
    timeout_value = raw.get("timeout", raw.get("max_time_seconds", 5))
    return ConnectivityConfig(
        name=str(raw.get("name") or default_name),
        method=str(raw.get("method") or "GET").upper(),
        url=url,
        expected_status=_int(raw.get("expected_status", 200), f"{field_name}.expected_status"),
        timeout=_float(timeout_value, f"{field_name}.timeout"),
        cookie_jar=_optional_string(raw.get("cookie_jar"), f"{field_name}.cookie_jar"),
        save_cookie_jar=_optional_string(raw.get("save_cookie_jar"), f"{field_name}.save_cookie_jar"),
        headers=_string_mapping(raw.get("headers"), f"{field_name}.headers"),
        body=raw.get("body"),
        json=raw.get("json"),
        expect_text=_optional_string(raw.get("expect_text"), f"{field_name}.expect_text"),
        forbid_text=_optional_string(raw.get("forbid_text"), f"{field_name}.forbid_text"),
        expect_headers=_string_mapping(
            raw.get("expect_headers", raw.get("expect_header_contains")),
            f"{field_name}.expect_headers",
        ),
        forbid_headers=_string_mapping(
            raw.get("forbid_headers", raw.get("forbid_header_contains")),
            f"{field_name}.forbid_headers",
        ),
    )


def _e2e_config(raw: dict[str, Any]) -> E2eConfig:
    return E2eConfig(
        command=_optional_string(raw.get("command"), "runtime_smoke.e2e.command"),
        working_dir=_optional_string(raw.get("working_dir"), "runtime_smoke.e2e.working_dir"),
        env=_string_mapping(raw.get("env"), "runtime_smoke.e2e.env"),
        timeout=_optional_float(raw.get("timeout"), "runtime_smoke.e2e.timeout"),
    )


def _crud_flow_targets(raw: Any) -> list[CrudFlowTargetConfig]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValueError("runtime.crud_flow_targets must be a list")

    targets: list[CrudFlowTargetConfig] = []
    for index, item in enumerate(raw, start=1):
        field_name = f"runtime.crud_flow_targets[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{field_name} must be a mapping")
        command = _optional_string(item.get("command"), f"{field_name}.command")
        create_raw = item.get("create")
        reflect_raw = item.get("reflect")
        create = _connectivity_config(create_raw, f"{field_name}.create", f"CRUD create {index}") if create_raw else None
        reflect = _connectivity_config(reflect_raw, f"{field_name}.reflect", f"CRUD reflect {index}") if reflect_raw else None
        if not command and (create is None or reflect is None):
            raise ValueError(f"{field_name} requires either command or create+reflect")
        reflect_mapping = reflect_raw if isinstance(reflect_raw, dict) else {}
        targets.append(
            CrudFlowTargetConfig(
                name=str(item.get("name") or f"CRUD flow {index}"),
                command=command,
                working_dir=_optional_string(item.get("working_dir"), f"{field_name}.working_dir"),
                env=_string_mapping(item.get("env"), f"{field_name}.env"),
                timeout=_optional_float(item.get("timeout"), f"{field_name}.timeout"),
                create=create,
                reflect=reflect,
                expect_text=_optional_string(
                    item.get("expect_text", reflect_mapping.get("expect_text")),
                    f"{field_name}.expect_text",
                ),
                poll_interval=_float(item.get("poll_interval", 0.5), f"{field_name}.poll_interval"),
                max_wait_seconds=_float(
                    item.get("max_wait_seconds", item.get("timeout_seconds", 10)),
                    f"{field_name}.max_wait_seconds",
                ),
            )
        )
    return targets


def _action_outcome_targets(raw: Any) -> list[ActionOutcomeTargetConfig]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValueError("runtime.action_outcome_targets must be a list")

    targets: list[ActionOutcomeTargetConfig] = []
    for index, item in enumerate(raw, start=1):
        field_name = f"runtime.action_outcome_targets[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{field_name} must be a mapping")
        command = _optional_string(item.get("command"), f"{field_name}.command")
        invoke_raw = item.get("invoke")
        observe_raw = item.get("observe")
        invoke = _connectivity_config(invoke_raw, f"{field_name}.invoke", f"action invoke {index}") if invoke_raw else None
        observe = (
            _connectivity_config(observe_raw, f"{field_name}.observe", f"action observe {index}") if observe_raw else None
        )
        if not command and (invoke is None or observe is None):
            raise ValueError(f"{field_name} requires either command or invoke+observe")
        actions = _action_specs(
            item.get("actions", item.get("action")),
            field_name,
            item.get("outcomes", item.get("outcome")),
            default_actor=item.get("actor"),
            default_actors=item.get("actors"),
        )
        observe_mapping = observe_raw if isinstance(observe_raw, dict) else {}
        targets.append(
            ActionOutcomeTargetConfig(
                name=str(item.get("name") or f"Action outcome {index}"),
                actions=actions,
                command=command,
                working_dir=_optional_string(item.get("working_dir"), f"{field_name}.working_dir"),
                env=_string_mapping(item.get("env"), f"{field_name}.env"),
                timeout=_optional_float(item.get("timeout"), f"{field_name}.timeout"),
                invoke=invoke,
                observe=observe,
                expect_text=_optional_string(
                    item.get("expect_text", observe_mapping.get("expect_text")),
                    f"{field_name}.expect_text",
                ),
                forbid_text=_optional_string(
                    item.get("forbid_text", observe_mapping.get("forbid_text")),
                    f"{field_name}.forbid_text",
                ),
                poll_interval=_float(item.get("poll_interval", 0.5), f"{field_name}.poll_interval"),
                max_wait_seconds=_float(
                    item.get("max_wait_seconds", item.get("timeout_seconds", 10)),
                    f"{field_name}.max_wait_seconds",
                ),
            )
        )
    return targets


def _action_specs(
    raw: Any,
    field_name: str,
    default_outcomes: Any = None,
    *,
    default_actor: Any = None,
    default_actors: Any = None,
) -> list[ActionSpecConfig]:
    if isinstance(raw, dict):
        raw_actions = [raw]
    elif isinstance(raw, list):
        raw_actions = raw
    else:
        raise ValueError(f"{field_name} requires actions or action metadata")

    actions: list[ActionSpecConfig] = []
    for index, item in enumerate(raw_actions, start=1):
        action_field = f"{field_name}.actions[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{action_field} must be a mapping")
        action_id = _string(item.get("id", item.get("name")), f"{action_field}.id")
        outcomes = _outcome_expectations(item.get("outcomes", item.get("outcome", default_outcomes)), action_field)
        if not outcomes:
            raise ValueError(f"{action_field} requires outcome metadata")
        actions.append(
            ActionSpecConfig(
                id=action_id,
                verb=_optional_string(item.get("verb"), f"{action_field}.verb"),
                target=_optional_string(item.get("target"), f"{action_field}.target"),
                trigger=_optional_string(item.get("trigger"), f"{action_field}.trigger"),
                outcomes=outcomes,
                actor=_optional_string(item.get("actor", default_actor), f"{action_field}.actor"),
                actors=_string_list(item.get("actors", default_actors), f"{action_field}.actors"),
            )
        )
    return actions


def _outcome_expectations(raw: Any, field_name: str) -> list[OutcomeExpectationConfig]:
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        return [OutcomeExpectationConfig(name=raw)]
    if isinstance(raw, list):
        outcomes: list[OutcomeExpectationConfig] = []
        for index, item in enumerate(raw, start=1):
            outcomes.extend(_outcome_expectations(item, f"{field_name}.outcomes[{index}]"))
        return outcomes
    if isinstance(raw, dict):
        name = raw.get("name") or raw.get("id") or raw.get("type")
        if name:
            return [
                OutcomeExpectationConfig(
                    name=_string(name, f"{field_name}.outcome.name"),
                    required=_outcome_required(raw.get("required", True)),
                )
            ]
        return [
            OutcomeExpectationConfig(name=str(key), required=_outcome_required(value))
            for key, value in raw.items()
            if _outcome_required(value)
        ]
    raise ValueError(f"{field_name}.outcomes must be a string, mapping, or list")


def _outcome_required(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return True
    text = str(value).strip().lower()
    return text not in {"false", "no", "optional", "skip", "skipped"}


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


def _string_list(value: Any, field_name: str) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a string or list")
    return [str(item) for item in value if item not in (None, "")]


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
