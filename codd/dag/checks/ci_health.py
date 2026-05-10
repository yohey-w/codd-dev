"""C8 CI health check with deterministic static validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from glob import glob
import json
from pathlib import Path
import re
from typing import Any, Mapping

import yaml

from codd.dag.checks import DagCheck, register_dag_check
from codd.dag.checks.opt_out import (
    OPT_OUT_STATUS,
    OptOutDeclaration,
    OptOutSignal,
)


_DEFAULT_PROVIDER = "github" + "_actions"
_OPT_OUT_PROVIDER = "none"


@dataclass
class CiConfig:
    provider: str = _DEFAULT_PROVIDER
    workflow_glob: str = ".github/workflows/*.yml"
    required_triggers: list[str] = field(default_factory=lambda: ["push", "pull_request"])
    runtime_check: bool = False
    staleness_days: int = 14
    default_branch: str = "main"
    trigger_key: str = "on"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "CiConfig":
        if not isinstance(value, Mapping):
            # Missing ``ci:`` section is no longer treated as a silent opt-out.
            # The default provider applies; if no workflow files exist this
            # surfaces as a normal red ``ci_workflow_missing`` finding rather
            # than a free PASS.
            return cls()

        config = cls()
        return cls(
            provider=_string_value(value.get("provider"), config.provider),
            workflow_glob=_string_value(value.get("workflow_glob"), config.workflow_glob),
            required_triggers=_string_list(value.get("required_triggers"), config.required_triggers),
            runtime_check=bool(value.get("runtime_check", config.runtime_check)),
            staleness_days=_int_value(value.get("staleness_days"), config.staleness_days),
            default_branch=_string_value(value.get("default_branch"), config.default_branch),
            trigger_key=_string_value(value.get("trigger_key"), config.trigger_key),
        )


@dataclass
class CiHealthFinding:
    violation_type: str
    severity: str
    block_deploy: bool
    message: str
    details: list[str] = field(default_factory=list)


@dataclass
class CiHealthResult:
    check_name: str = "ci_health"
    status: str = "pass"
    severity: str = "info"
    block_deploy: bool = False
    message: str = "C8 ci_health PASS"
    findings: list[CiHealthFinding] = field(default_factory=list)
    workflow_files: list[str] = field(default_factory=list)
    passed: bool = True


@register_dag_check("ci_health")
class CiHealthCheck(DagCheck):
    """C8 CI workflow presence and trigger validation.

    Runtime provider polling is intentionally deferred. The shipped path is
    static, deterministic, and driven by config values.
    """

    check_name = "ci_health"
    severity = "red"
    block_deploy = True

    def run(
        self,
        dag: Any | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
        codd_config: dict[str, Any] | None = None,
    ) -> CiHealthResult:
        del dag
        root = Path(project_root or self.project_root or ".").resolve()
        active_settings = codd_config or settings or self.settings
        config = CiConfig.from_mapping(_mapping_value(active_settings, "ci"))
        return self.check(root, config)

    def detect_opt_out(self, codd_config: dict[str, Any]) -> OptOutSignal | None:
        config = CiConfig.from_mapping(_mapping_value(codd_config, "ci"))
        if self._is_opt_out_provider(config):
            return OptOutSignal(
                check_name=self.check_name,
                source="ci.provider=none",
            )
        return None

    @staticmethod
    def _is_opt_out_provider(config: CiConfig) -> bool:
        return config.provider.strip().lower() == _OPT_OUT_PROVIDER

    def _make_opt_out_result(self, declaration: OptOutDeclaration | None) -> CiHealthResult:
        today = self.today
        if declaration is None:
            return CiHealthResult(
                status="fail",
                severity="red",
                block_deploy=True,
                message=(
                    "C8 ci_health: ci.provider=none requires an opt_outs declaration "
                    "in codd.yaml (check: ci_health, reason: ..., expires_at: "
                    "YYYY-MM-DD)."
                ),
                passed=False,
            )
        if declaration.is_expired(today):
            return CiHealthResult(
                status="fail",
                severity="red",
                block_deploy=True,
                message=(
                    f"C8 ci_health: opt-out expired on "
                    f"{declaration.expires_at.isoformat()} "
                    f"(reason: {declaration.reason}); renew the entry or remove it."
                ),
                passed=False,
            )
        return CiHealthResult(
            status=OPT_OUT_STATUS,
            severity=self.severity,
            block_deploy=False,
            message=(
                f"C8 ci_health opt-out active "
                f"(reason: {declaration.reason}, "
                f"expires: {declaration.expires_at.isoformat()})"
            ),
            passed=False,
        )

    def check(self, project_root: Path, config: CiConfig) -> CiHealthResult:
        project_root = Path(project_root).resolve()
        if self._is_opt_out_provider(config):
            declaration = (
                self.opt_out_policy.lookup(self.check_name) if self.opt_out_policy else None
            )
            return self._make_opt_out_result(declaration)
        workflow_files = self._locate_workflows(project_root, config.workflow_glob)
        if not workflow_files:
            finding = CiHealthFinding(
                violation_type="ci_workflow_missing",
                severity="red",
                block_deploy=True,
                message="No CI workflow files found matching glob.",
                details=[config.workflow_glob],
            )
            return CiHealthResult(
                status="fail",
                severity="red",
                block_deploy=True,
                message=finding.message,
                findings=[finding],
                passed=False,
            )

        findings = [
            *self._check_triggers(workflow_files, config.required_triggers, config.trigger_key),
            *self._check_verification_coverage(workflow_files, project_root),
        ]
        block_deploy = any(finding.block_deploy for finding in findings)
        severity = _max_severity(finding.severity for finding in findings)
        status = "fail" if block_deploy else ("warn" if findings else "pass")
        message = (
            "C8 ci_health PASS"
            if not findings
            else f"C8 ci_health found {len(findings)} static finding(s)"
        )
        return CiHealthResult(
            status=status,
            severity=severity,
            block_deploy=block_deploy,
            message=message,
            findings=findings,
            workflow_files=[path.relative_to(project_root).as_posix() for path in workflow_files],
            passed=not block_deploy,
        )

    def format_report(self, result: CiHealthResult) -> str:
        return json.dumps({"ci_health_report": asdict(result)}, ensure_ascii=False, indent=2)

    def _locate_workflows(self, project_root: Path, workflow_glob: str) -> list[Path]:
        pattern = workflow_glob.strip()
        if not pattern:
            return []
        if Path(pattern).is_absolute():
            paths = [Path(path) for path in glob(pattern)]
        else:
            paths = list(project_root.glob(pattern))
        return sorted(path for path in paths if path.is_file())

    def _check_triggers(
        self,
        workflow_files: list[Path],
        required_triggers: list[str],
        trigger_key: str,
    ) -> list[CiHealthFinding]:
        required = {trigger.strip() for trigger in required_triggers if trigger.strip()}
        if not required:
            return []

        actual: set[str] = set()
        for path in workflow_files:
            actual.update(self._workflow_triggers(path, trigger_key))

        missing = sorted(required - actual)
        if not missing:
            return []
        return [
            CiHealthFinding(
                violation_type="ci_trigger_incomplete",
                severity="amber",
                block_deploy=False,
                message="CI workflow does not include all required triggers.",
                details=[f"missing: {', '.join(missing)}"],
            )
        ]

    def _workflow_triggers(self, path: Path, trigger_key: str) -> set[str]:
        payload = _read_yaml_mapping(path)
        value = payload.get(trigger_key)
        if value is None and trigger_key == "on":
            value = payload.get(True)
        return _trigger_names(value)

    def _check_verification_coverage(
        self,
        workflow_files: list[Path],
        project_root: Path,
    ) -> list[CiHealthFinding]:
        verification_commands = self._deploy_verification_commands(project_root)
        if not verification_commands:
            return []

        workflow_commands = {
            _normalize_command(command)
            for path in workflow_files
            for command in self._workflow_commands(path)
            if _normalize_command(command)
        }
        missing = [
            command
            for command in verification_commands
            if not self._command_appears_in_workflow(command, workflow_commands)
        ]
        if not missing:
            return []
        return [
            CiHealthFinding(
                violation_type="ci_verification_not_in_workflow",
                severity="amber",
                block_deploy=False,
                message="Deployment verification command is not invoked by CI workflow.",
                details=missing,
            )
        ]

    def _deploy_verification_commands(self, project_root: Path) -> list[str]:
        commands: list[str] = []
        for path in self._deploy_yaml_candidates(project_root):
            if not path.is_file():
                continue
            payload = _read_yaml_mapping(path)
            commands.extend(_hook_commands(payload))
        return _dedupe(_normalize_command(command) for command in commands if _normalize_command(command))

    @staticmethod
    def _deploy_yaml_candidates(project_root: Path) -> list[Path]:
        return [
            project_root / "deploy.yaml",
            project_root / ".codd" / "deploy.yaml",
            project_root / "codd" / "deploy.yaml",
        ]

    def _workflow_commands(self, path: Path) -> list[str]:
        return _command_values(_read_yaml_mapping(path))

    @staticmethod
    def _command_appears_in_workflow(command: str, workflow_commands: set[str]) -> bool:
        return any(command == candidate or command in candidate for candidate in workflow_commands)


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return payload if isinstance(payload, dict) else {}


def _mapping_value(mapping: Any, key: str) -> Mapping[str, Any] | None:
    if isinstance(mapping, Mapping) and isinstance(mapping.get(key), Mapping):
        return mapping[key]
    return None


def _trigger_names(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {str(item) for item in value if item}
    if isinstance(value, dict):
        return {str(key) for key in value if key}
    return set()


def _hook_commands(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        commands: list[str] = []
        for item in value:
            commands.extend(_hook_commands(item))
        return commands
    if isinstance(value, dict):
        commands: list[str] = []
        for key in ("post_deploy", "post_deploy_steps", "post_deploy_hooks", "verification", "verifications"):
            commands.extend(_hook_commands(value.get(key)))
        for key in ("command", "run", "script", "test"):
            commands.extend(_hook_commands(value.get(key)))
        targets = value.get("targets")
        if isinstance(targets, dict):
            for target_config in targets.values():
                commands.extend(_hook_commands(target_config))
        return commands
    return []


def _command_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return []
    if isinstance(value, list):
        commands: list[str] = []
        for item in value:
            commands.extend(_command_values(item))
        return commands
    if isinstance(value, dict):
        commands: list[str] = []
        for key, item in value.items():
            if key in {"run", "script", "command", "commands"}:
                commands.extend(_coerce_command_list(item))
            else:
                commands.extend(_command_values(item))
        return commands
    return []


def _coerce_command_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        commands: list[str] = []
        for item in value:
            commands.extend(_coerce_command_list(item))
        return commands
    return []


def _normalize_command(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _dedupe(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _string_value(value: Any, default: str) -> str:
    return value if isinstance(value, str) and value.strip() else default


def _string_list(value: Any, default: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(default)
    result = [str(item) for item in value if str(item).strip()]
    return result or list(default)


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _max_severity(values: Any) -> str:
    order = {"info": 0, "amber": 1, "red": 2}
    selected = "info"
    for value in values:
        text = str(value)
        if order.get(text, 0) > order[selected]:
            selected = text
    return selected
