"""Preflight checks for autonomous CoDD task execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Literal

import yaml

from codd.config import load_project_config


PreflightSeverity = Literal["critical", "high", "medium", "low"]
CheckStatus = Literal["PASS", "WARN", "FAIL"]

SEVERITY_ORDER: tuple[PreflightSeverity, ...] = ("critical", "high", "medium", "low")
SUPPORTED_PROJECT_TYPES = {"web", "cli", "mobile", "iot"}
DEFAULTS_DIR = Path(__file__).parent / "defaults"


@dataclass
class PreflightCheck:
    name: str
    status: CheckStatus
    severity: PreflightSeverity
    message: str
    details: list[str] = field(default_factory=list)


@dataclass
class PreflightResult:
    task_id: str
    checks: list[PreflightCheck]
    severity: PreflightSeverity
    ntfy_sent: bool = False
    halt_recommended: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "checks": [
                {
                    "name": check.name,
                    "status": check.status,
                    "severity": check.severity,
                    "message": check.message,
                    "details": list(check.details),
                }
                for check in self.checks
            ],
            "severity": self.severity,
            "ntfy_sent": self.ntfy_sent,
            "halt_recommended": self.halt_recommended,
        }


class PreflightAuditor:
    """Run structured safety checks before autonomous task execution."""

    GOAL_IDENTITY_FIELDS = ("task_id", "parent_cmd")
    GOAL_PURPOSE_FIELDS = ("north_star", "purpose", "description")
    CONTEXT_FIELDS = ("project", "target_path", "context_files")
    ROLLBACK_FIELDS = ("rollback_strategy", "danger_signals")

    def __init__(self, project_root: Path | str | None = None, codd_yaml: dict[str, Any] | None = None):
        self.project_root = Path(project_root or Path.cwd()).resolve()
        self.codd_yaml = codd_yaml if codd_yaml is not None else self._load_project_config()
        preflight_config = self.codd_yaml.get("preflight", {})
        self.preflight_config = preflight_config if isinstance(preflight_config, dict) else {}

    def check_goal_clarity(self, task_yaml: dict[str, Any]) -> PreflightCheck:
        missing_identity = [field for field in self.GOAL_IDENTITY_FIELDS if not task_yaml.get(field)]
        if missing_identity:
            return PreflightCheck(
                name="goal_clarity",
                status="FAIL",
                severity="critical",
                message=f"Goal identity fields missing: {', '.join(missing_identity)}",
                details=[f"Add field: {field}" for field in missing_identity],
            )

        if not any(task_yaml.get(field) for field in self.GOAL_PURPOSE_FIELDS):
            return PreflightCheck(
                name="goal_clarity",
                status="FAIL",
                severity="high",
                message="Goal purpose missing: add north_star, purpose, or description",
                details=["Add one of: north_star, purpose, description"],
            )

        criteria = task_yaml.get("acceptance_criteria")
        if criteria is None or criteria == [] or criteria == "":
            return PreflightCheck(
                name="goal_clarity",
                status="WARN",
                severity="high",
                message="acceptance_criteria missing or empty",
                details=["Add testable acceptance_criteria before broad autonomous work"],
            )

        return PreflightCheck(
            name="goal_clarity",
            status="PASS",
            severity="low",
            message="Goal clarity OK",
        )

    def check_context_completeness(
        self,
        task_yaml: dict[str, Any],
        project_root: Path | str | None = None,
    ) -> PreflightCheck:
        root = Path(project_root or self.project_root)
        issues: list[str] = []
        if not any(task_yaml.get(field) for field in self.CONTEXT_FIELDS):
            issues.append("project, target_path, or context_files field missing")

        for raw_path in _as_list(task_yaml.get("context_files")):
            path = _resolve_path(root, str(raw_path))
            if not path.exists():
                issues.append(f"context file not found: {raw_path}")

        if task_yaml.get("project") and not _project_lexicon_exists(root):
            issues.append("project_lexicon.yaml not found")

        if issues:
            return PreflightCheck(
                name="context_completeness",
                status="WARN",
                severity="high",
                message="Context completeness gaps found",
                details=issues,
            )
        return PreflightCheck(
            name="context_completeness",
            status="PASS",
            severity="low",
            message="Context completeness OK",
        )

    def check_judgment_materials(
        self,
        task_yaml: dict[str, Any],
        project_root: Path | str | None = None,
    ) -> PreflightCheck:
        del project_root
        if not task_yaml.get("bloom_level"):
            return PreflightCheck(
                name="judgment_materials",
                status="WARN",
                severity="medium",
                message="bloom_level missing for judgment tracking",
                details=["Add bloom_level so model-routing and review depth are explicit"],
            )
        return PreflightCheck(
            name="judgment_materials",
            status="PASS",
            severity="low",
            message="Judgment materials OK",
        )

    def check_rollback_criteria(self, task_yaml: dict[str, Any]) -> PreflightCheck:
        has_rollback = bool(task_yaml.get("rollback_strategy"))
        has_danger = bool(task_yaml.get("danger_signals"))
        critical_matches = self.matching_critical_operations(task_yaml)

        if not has_rollback:
            severity: PreflightSeverity = "critical" if critical_matches else "high"
            details = ["Add rollback_strategy"]
            if critical_matches:
                details.append(f"Critical operation(s): {', '.join(critical_matches)}")
            return PreflightCheck(
                name="rollback_criteria",
                status="FAIL",
                severity=severity,
                message="rollback_strategy missing",
                details=details,
            )

        if not has_danger:
            return PreflightCheck(
                name="rollback_criteria",
                status="WARN",
                severity="medium",
                message="danger_signals missing",
                details=["Add danger_signals such as build_fail, test_fail, production_5xx"],
            )

        return PreflightCheck(
            name="rollback_criteria",
            status="PASS",
            severity="low",
            message="Rollback criteria OK",
        )

    def classify_severity(self, checks: list[PreflightCheck]) -> PreflightSeverity:
        for severity in SEVERITY_ORDER:
            if any(check.severity == severity and check.status in {"FAIL", "WARN"} for check in checks):
                return severity
        return "low"

    def run(self, task_yaml_path: Path | str) -> PreflightResult:
        path = Path(task_yaml_path)
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise ValueError(f"{path} must contain a YAML mapping")

        checks = [
            self.check_goal_clarity(payload),
            self.check_context_completeness(payload, self.project_root),
            self.check_judgment_materials(payload, self.project_root),
            self.check_rollback_criteria(payload),
        ]
        severity = self.classify_severity(checks)
        return PreflightResult(
            task_id=str(payload.get("task_id") or path),
            checks=checks,
            severity=severity,
            halt_recommended=severity == "critical",
        )

    def matching_critical_operations(self, task_yaml: dict[str, Any]) -> list[str]:
        structured_operations = {
            _normalize_for_match(str(value))
            for value in [
                *_as_list(task_yaml.get("operation")),
                *_as_list(task_yaml.get("operations")),
                *_as_list(task_yaml.get("critical_operation")),
                *_as_list(task_yaml.get("critical_operations")),
            ]
            if value
        }
        prose = " ".join(
            str(value)
            for value in [
                task_yaml.get("command", ""),
                task_yaml.get("purpose", ""),
                task_yaml.get("north_star", ""),
                task_yaml.get("description", ""),
            ]
        ).lower()
        matches: list[str] = []
        for operation in self.critical_operations():
            normalized = _normalize_for_match(operation)
            human_phrase = operation.replace("_", " ").lower()
            if normalized and (normalized in structured_operations or human_phrase in prose):
                matches.append(operation)
        return matches

    def critical_operations(self) -> list[str]:
        project_type = self.detect_project_type()
        operations: list[str] = []
        defaults_path = DEFAULTS_DIR / f"{project_type}.yaml"
        if defaults_path.exists():
            defaults = yaml.safe_load(defaults_path.read_text(encoding="utf-8")) or {}
            operations.extend(str(item) for item in defaults.get("critical_operations", []) if item)
        operations.extend(
            str(item)
            for item in self.preflight_config.get("critical_operations", [])
            if item
        )
        return _unique(operations)

    def detect_project_type(self) -> str:
        configured = str(
            self.preflight_config.get("project_type")
            or _nested_get(self.codd_yaml, ("requirement_completeness", "project_type"))
            or _nested_get(self.codd_yaml, ("project", "type"))
            or ""
        ).lower()
        if configured in SUPPORTED_PROJECT_TYPES:
            return configured
        if _looks_like_mobile_project(self.project_root):
            return "mobile"
        if _looks_like_iot_project(self.project_root):
            return "iot"
        if _looks_like_web_project(self.project_root):
            return "web"
        if _looks_like_cli_project(self.project_root):
            return "cli"
        return "web"

    def _load_project_config(self) -> dict[str, Any]:
        try:
            return load_project_config(self.project_root)
        except (FileNotFoundError, ValueError):
            return {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _resolve_path(root: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    return path if path.is_absolute() else root / path


def _project_lexicon_exists(root: Path) -> bool:
    return (root / "project_lexicon.yaml").exists() or (root / "codd" / "project_lexicon.yaml").exists()


def _normalize_for_match(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique_values: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique_values.append(value)
    return unique_values


def _nested_get(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _looks_like_web_project(root: Path) -> bool:
    return any((root / path).exists() for path in ("package.json", "index.html", "app", "pages", "src/app"))


def _looks_like_cli_project(root: Path) -> bool:
    return any((root / path).exists() for path in ("pyproject.toml", "setup.py", "go.mod", "Cargo.toml"))


def _looks_like_mobile_project(root: Path) -> bool:
    if (root / "pubspec.yaml").exists():
        return True
    package_json = root / "package.json"
    if not package_json.exists():
        return False
    text = package_json.read_text(encoding="utf-8", errors="ignore").lower()
    return "react-native" in text or "\"expo\"" in text


def _looks_like_iot_project(root: Path) -> bool:
    marker_names = ("platformio.ini", "zephyr", "firmware", "arduino")
    return any((root / marker).exists() for marker in marker_names)
