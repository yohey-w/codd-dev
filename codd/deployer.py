"""Deployment orchestration for ``codd deploy``.

Phase 1 keeps deploy execution behind pluggable targets. Core code owns only
configuration validation, target dispatch, dry-run/apply flow, healthchecks,
rollback orchestration, and structured logging.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

import yaml

from codd.cli import CoddCLIError
from codd.deploy_targets import get_target

_coherence_bus: Any | None = None


@dataclass(frozen=True)
class DeployGateFailure:
    """One deploy gate failure, ready for CLI/log output."""

    gate: str
    message: str
    details: list[str] = field(default_factory=list)


@dataclass
class DeployGateResult:
    """Aggregated deploy gate result."""

    failures: list[DeployGateFailure] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reports: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.failures

    def add_failure(self, gate: str, message: str, details: list[str] | None = None) -> None:
        self.failures.append(DeployGateFailure(gate=gate, message=message, details=details or []))

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)

    def add_report(self, name: str, payload: Any) -> None:
        self.reports[name] = payload

    def format_failures(self) -> str:
        if self.passed:
            return "No deploy gate failures."

        lines: list[str] = []
        for failure in self.failures:
            lines.append(f"- {failure.gate}: {failure.message}")
            for detail in failure.details[:5]:
                lines.append(f"  - {detail}")
            if len(failure.details) > 5:
                lines.append(f"  - ... {len(failure.details) - 5} more")
        return "\n".join(lines)

    def as_log_payload(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failures": [
                {"gate": failure.gate, "message": failure.message, "details": failure.details}
                for failure in self.failures
            ],
            "warnings": self.warnings,
            **self.reports,
        }


def load_deploy_config(config_path: Path) -> dict[str, Any]:
    """Load and validate deploy.yaml."""
    config_path = Path(config_path).expanduser()
    if not config_path.exists():
        raise CoddCLIError(f"Deploy config not found: {config_path}")

    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise CoddCLIError("deploy.yaml must contain a YAML mapping")

    targets = payload.get("targets")
    if not isinstance(targets, dict) or not targets:
        raise CoddCLIError("deploy.yaml must define at least one target under 'targets'")

    default_target = payload.get("default_target")
    if default_target is not None and default_target not in targets:
        raise CoddCLIError(f"default_target {default_target!r} is not defined in targets")

    for target_name, target_config in targets.items():
        _validate_target_config(str(target_name), target_config)

    global_config = payload.get("global", {})
    if global_config is not None and not isinstance(global_config, dict):
        raise CoddCLIError("'global' must be a mapping when provided")

    return payload


def run_healthcheck(
    url: str,
    expected_status: int,
    timeout_seconds: int,
    retries: int,
) -> bool:
    """HTTP GET healthcheck with retries."""
    for attempt in range(max(1, retries)):
        try:
            request = Request(url, method="GET")
            with urlopen(request, timeout=timeout_seconds) as response:
                if response.status == expected_status:
                    return True
        except (OSError, URLError):
            pass

        if attempt < retries - 1:
            time.sleep(1)

    return False


def set_coherence_bus(bus: Any | None) -> None:
    """Set an optional EventBus used to publish deploy-gate DriftEvents."""

    global _coherence_bus
    _coherence_bus = bus


def run_deploy(
    project_root: Path,
    target_name: str | None = None,
    config: dict[str, Any] | None = None,
    *,
    config_path: Path | None = None,
    dry_run: bool = True,
    rollback_flag: bool = False,
    healthcheck_timeout: int = 60,
    emit_output: bool = False,
) -> int:
    """Main deploy entry point. Returns exit code."""
    project_root = Path(project_root).resolve()
    deploy_config = config if config is not None else load_deploy_config(config_path or project_root / "deploy.yaml")
    selected_target = _select_target_name(deploy_config, target_name)
    target_config = deploy_config["targets"][selected_target]
    target_cls = get_target(target_config["type"])
    target = target_cls(target_config)

    log_context: dict[str, Any] = {
        "target": selected_target,
        "target_type": target_config["type"],
        "dry_run": dry_run,
        "rollback": rollback_flag,
        "actions": [],
        "status": "started",
        "errors": [],
    }

    if not dry_run and not rollback_flag:
        gate_result = _run_deploy_gates(project_root)
        log_context["gates"] = gate_result.as_log_payload()
        if not gate_result.passed:
            message = gate_result.format_failures()
            log_context["status"] = "gate_failed"
            log_context["errors"].append(message)
            _write_deploy_log(project_root, deploy_config, selected_target, log_context)
            raise CoddCLIError(f"Deploy blocked: gate failed\n{message}")

    try:
        if rollback_flag:
            log_context["status"] = "rollback_succeeded" if target.rollback({}) else "rollback_failed"
            _write_deploy_log(project_root, deploy_config, selected_target, log_context)
            return 0 if log_context["status"] == "rollback_succeeded" else 1

        if dry_run:
            actions = target.dry_run()
            log_context["actions"] = actions
            log_context["status"] = "dry_run"
            if emit_output:
                _emit_dry_run_actions(selected_target, actions)
            _write_deploy_log(project_root, deploy_config, selected_target, log_context)
            return 0

        snapshot = target.snapshot()
        log_context["snapshot"] = snapshot
        if not target.deploy():
            log_context["status"] = "deploy_failed"
            _maybe_rollback(target, snapshot, deploy_config, log_context)
            _write_deploy_log(project_root, deploy_config, selected_target, log_context)
            return 1

        if not _run_target_healthcheck(target, target_config, healthcheck_timeout):
            log_context["status"] = "healthcheck_failed"
            _maybe_rollback(target, snapshot, deploy_config, log_context)
            _write_deploy_log(project_root, deploy_config, selected_target, log_context)
            return 1

        log_context["status"] = "deployed"
        _write_deploy_log(project_root, deploy_config, selected_target, log_context)
        return 0
    except Exception as exc:
        log_context["status"] = "failed"
        log_context["errors"].append(str(exc))
        _write_deploy_log(project_root, deploy_config, selected_target, log_context)
        raise


def _run_deploy_gates(project_root: Path) -> DeployGateResult:
    """Run validate, drift, coverage, DAG, C6, and C7 gates before apply deploy."""
    project_root = Path(project_root).resolve()
    settings = _load_gate_settings(project_root)
    codd_dir = _find_gate_codd_dir(project_root)
    result = DeployGateResult()

    _collect_validate_gate(project_root, codd_dir, settings, result)
    _collect_drift_gate(project_root, codd_dir, result)
    if codd_dir is None:
        return result
    _collect_coverage_gate(project_root, settings, result)
    _collect_dag_completeness_gate(project_root, settings, result)
    _collect_deployment_completeness_gate_result(project_root, settings, result)
    _collect_user_journey_coherence_gate_result(project_root, settings, result)
    return result


def _load_gate_settings(project_root: Path) -> dict[str, Any]:
    try:
        from codd.config import load_project_config

        return load_project_config(project_root)
    except (FileNotFoundError, ValueError):
        return {}


def _find_gate_codd_dir(project_root: Path) -> Path | None:
    from codd.config import find_codd_dir

    return find_codd_dir(project_root)


def _collect_validate_gate(
    project_root: Path,
    codd_dir: Path | None,
    settings: dict[str, Any],
    result: DeployGateResult,
) -> None:
    if codd_dir is None:
        result.add_failure("validate", "CoDD config dir not found")
        return

    try:
        from codd.coverage_metrics import check_edge_coverage_gate
        from codd.screen_flow_validator import (
            find_screen_flow_path,
            parse_screen_flow_routes,
            validate_screen_flow,
            validate_screen_flow_edges,
        )
        from codd.validator import run_validate, validate_design_tokens, validate_with_lexicon

        if run_validate(project_root, codd_dir) != 0:
            result.add_failure("validate", "frontmatter/dependency validation failed")

        lexicon_violations = validate_with_lexicon(project_root)
        if lexicon_violations:
            result.add_failure(
                "validate --lexicon",
                f"{len(lexicon_violations)} violation(s)",
                [_format_mapping_detail(violation) for violation in lexicon_violations],
            )

        design_token_violations = validate_design_tokens(project_root)
        if design_token_violations:
            result.add_failure(
                "validate --design-tokens",
                f"{len(design_token_violations)} violation(s)",
                [_format_object_detail(violation) for violation in design_token_violations],
            )

        screen_flow_drifts = validate_screen_flow(project_root, settings)
        if screen_flow_drifts:
            result.add_failure(
                "validate --screen-flow",
                f"{len(screen_flow_drifts)} route drift(s)",
                [_format_object_detail(drift) for drift in screen_flow_drifts],
            )

        screen_flow_path = find_screen_flow_path(project_root)
        screen_flow_nodes = parse_screen_flow_routes(screen_flow_path) if screen_flow_path else []
        edge_result = validate_screen_flow_edges(project_root, screen_flow_nodes, settings)
        edge_ok = check_edge_coverage_gate(edge_result, settings)
        if not edge_ok:
            result.add_failure(
                "validate --edges",
                f"edge coverage {edge_result.coverage_ratio:.0%} below threshold",
                _format_edge_result(edge_result),
            )
        if _screen_flow_strict_edges(settings) and edge_result.orphan_nodes:
            result.add_failure(
                "validate --edges",
                "orphan screen-flow nodes detected",
                [", ".join(edge_result.orphan_nodes)],
            )
    except Exception as exc:  # pragma: no cover - defensive gate behavior
        result.add_failure("validate", str(exc))


def _collect_drift_gate(
    project_root: Path,
    codd_dir: Path | None,
    result: DeployGateResult,
) -> None:
    if codd_dir is None:
        result.add_failure("drift", "CoDD config dir not found")
        return

    try:
        from codd.drift import run_drift

        drift_result = run_drift(project_root, codd_dir)
    except Exception as exc:  # pragma: no cover - defensive gate behavior
        result.add_failure("drift", str(exc))
        return

    if drift_result.exit_code != 0 or drift_result.drift:
        result.add_failure(
            "drift",
            f"{len(drift_result.drift)} drift(s)",
            [_format_object_detail(entry) for entry in drift_result.drift],
        )


def _collect_coverage_gate(
    project_root: Path,
    settings: dict[str, Any],
    result: DeployGateResult,
) -> None:
    try:
        from codd.coverage_metrics import run_coverage

        coverage_report = run_coverage(project_root, config=settings)
    except Exception as exc:  # pragma: no cover - defensive gate behavior
        result.add_failure("coverage", str(exc))
        return

    failed_metrics = [metric for metric in coverage_report.results if not metric.passed]
    if failed_metrics:
        result.add_failure(
            "coverage",
            f"{len(failed_metrics)} metric(s) failed",
            [_format_coverage_metric(metric) for metric in failed_metrics],
        )


def _collect_dag_completeness_gate(
    project_root: Path,
    settings: dict[str, Any],
    result: DeployGateResult,
) -> None:
    try:
        from codd.dag.runner import run_all_checks

        check_results = run_all_checks(
            project_root,
            settings=settings,
            check_names=[
                "node_completeness",
                "edge_validity",
                "depends_on_consistency",
                "task_completion",
                "transitive_closure",
            ],
        )
    except Exception as exc:  # pragma: no cover - defensive gate behavior
        result.add_failure("dag_completeness", str(exc))
        return

    failed_red = [
        check_result
        for check_result in check_results
        if _dag_result_severity(check_result) == "red" and _result_value(check_result, "passed") is False
    ]
    amber_findings = [
        check_result
        for check_result in check_results
        if _dag_result_severity(check_result) == "amber" and _dag_result_has_findings(check_result)
    ]

    for check_result in amber_findings:
        result.add_warning(f"dag_completeness: {_format_dag_check_result(check_result)}")

    if failed_red:
        details = [_format_dag_check_result(check_result) for check_result in failed_red]
        result.add_failure("dag_completeness", f"{len(failed_red)} DAG check(s) failed", details)
        _publish_dag_completeness_events(failed_red)


def _collect_deployment_completeness_gate(
    dag: Any | None,
    project_root: Path,
    codd_config: dict[str, Any],
) -> list[Any]:
    """Run the C6 deployment_completeness check and return violations.

    Projects without deployment_doc nodes or deploy-chain edges return an empty
    list for backward compatibility.
    """
    from codd.dag.checks.deployment_completeness import DeploymentCompletenessCheck

    project_root = Path(project_root).resolve()
    settings = codd_config
    target_dag = dag
    if target_dag is None:
        from codd.dag.builder import build_dag, load_dag_settings

        settings = load_dag_settings(project_root, codd_config)
        target_dag = build_dag(project_root, settings)

    check_result = DeploymentCompletenessCheck().run(
        target_dag,
        project_root=project_root,
        codd_config=settings,
    )
    return list(getattr(check_result, "violations", []) or [])


def _collect_deployment_completeness_gate_result(
    project_root: Path,
    settings: dict[str, Any],
    result: DeployGateResult,
) -> None:
    try:
        violations = _collect_deployment_completeness_gate(None, project_root, settings)
    except Exception as exc:  # pragma: no cover - defensive gate behavior
        result.add_failure("deployment_completeness", str(exc))
        return

    if not violations:
        return

    from codd.dag.checks.deployment_completeness import DeploymentCompletenessCheck

    report = DeploymentCompletenessCheck().format_report(violations)
    result.add_failure(
        "deployment_completeness",
        "C6 deployment_completeness: INCOMPLETE chain detected",
        [report],
    )
    result.add_report("incomplete_chain_report", _parse_incomplete_chain_report(report))
    _publish_deployment_completeness_events(violations)
    _ntfy_deploy_incomplete(violations, settings)


def _collect_user_journey_coherence_gate(
    dag: Any | None,
    project_root: Path,
    codd_config: dict[str, Any],
) -> Any:
    """Run the C7 user_journey_coherence check and return its result."""

    from codd.dag.checks.user_journey_coherence import UserJourneyCoherenceCheck

    project_root = Path(project_root).resolve()
    settings = codd_config
    target_dag = dag
    if target_dag is None:
        from codd.dag.builder import build_dag, load_dag_settings

        settings = load_dag_settings(project_root, codd_config)
        target_dag = build_dag(project_root, settings)

    return UserJourneyCoherenceCheck().run(
        target_dag,
        project_root=project_root,
        codd_config=settings,
    )


def _collect_user_journey_coherence_gate_result(
    project_root: Path,
    settings: dict[str, Any],
    result: DeployGateResult,
) -> None:
    try:
        check_result = _collect_user_journey_coherence_gate(None, project_root, settings)
    except Exception as exc:  # pragma: no cover - defensive gate behavior
        result.add_failure("user_journey_coherence", str(exc))
        return

    violations = list(_result_value(check_result, "violations") or [])
    if not violations:
        return

    from codd.dag.checks.user_journey_coherence import UserJourneyCoherenceCheck

    report = UserJourneyCoherenceCheck().format_report(check_result)
    result.add_report("user_journey_coherence_report", _parse_user_journey_coherence_report(report))

    red_violations = _red_user_journey_violations(violations)
    amber_violations = _amber_user_journey_violations(violations)
    if amber_violations:
        result.add_warning(f"user_journey_coherence: {len(amber_violations)} amber violation(s)")

    if not red_violations:
        return

    result.add_failure(
        "user_journey_coherence",
        f"C7 user_journey_coherence: {len(red_violations)} red violation(s)",
        [report],
    )
    _publish_user_journey_coherence_events(red_violations)
    _ntfy_user_journey_coherence_fail(red_violations, settings)


def _screen_flow_strict_edges(settings: dict[str, Any]) -> bool:
    screen_flow_config = settings.get("screen_flow", {})
    if not isinstance(screen_flow_config, dict):
        return True
    return bool(screen_flow_config.get("strict_edges", True))


def _result_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _format_mapping_detail(value: dict[str, Any]) -> str:
    node = value.get("node_id") or value.get("id") or value.get("name")
    message = value.get("message") or value.get("detail") or value.get("status") or value
    return f"{node}: {message}" if node else str(message)


def _format_object_detail(value: Any) -> str:
    if isinstance(value, dict):
        return _format_mapping_detail(value)
    for attrs in (("file", "line", "pattern"), ("kind", "url", "status"), ("source", "route", "detail")):
        parts = [str(getattr(value, attr)) for attr in attrs if getattr(value, attr, None)]
        if parts:
            return " ".join(parts)
    return str(value)


def _format_edge_result(edge_result: Any) -> list[str]:
    details = [f"covered nodes: {len(edge_result.covered_nodes)}", f"total edges: {edge_result.total_edges}"]
    if edge_result.unreachable_nodes:
        details.append("unreachable: " + ", ".join(edge_result.unreachable_nodes))
    if edge_result.orphan_nodes:
        details.append("orphan: " + ", ".join(edge_result.orphan_nodes))
    if edge_result.dead_end_nodes:
        details.append("dead-end: " + ", ".join(edge_result.dead_end_nodes))
    return details


def _format_coverage_metric(metric: Any) -> str:
    return (
        f"{metric.metric}: {metric.pct:.0f}% "
        f"(threshold: {metric.threshold:.0f}%, uncovered: {metric.uncovered})"
    )


def _dag_result_severity(check_result: Any) -> str:
    return str(_result_value(check_result, "severity") or "red")


def _dag_result_name(check_result: Any) -> str:
    return str(_result_value(check_result, "check_name") or check_result.__class__.__name__)


def _dag_result_has_findings(check_result: Any) -> bool:
    for key in (
        "violations",
        "missing_impl_files",
        "orphan_edges",
        "dangling_refs",
        "incomplete_tasks",
        "unreachable_nodes",
    ):
        if _result_value(check_result, key):
            return True
    return False


def _format_dag_check_result(check_result: Any) -> str:
    name = _dag_result_name(check_result)
    details: list[str] = []
    for key in (
        "missing_impl_files",
        "orphan_edges",
        "dangling_refs",
        "violations",
        "incomplete_tasks",
        "unreachable_nodes",
        "warnings",
    ):
        value = _result_value(check_result, key)
        if not value:
            continue
        if isinstance(value, list):
            rendered = ", ".join(str(item) for item in value[:3])
            if len(value) > 3:
                rendered += f", ... {len(value) - 3} more"
            details.append(f"{key}={rendered}")
        else:
            details.append(f"{key}={value}")
    return f"{name}: {'; '.join(details)}" if details else name


def _publish_dag_completeness_events(failed_results: list[Any]) -> None:
    if _coherence_bus is None:
        return

    try:
        from codd.coherence_engine import DriftEvent
    except Exception:  # pragma: no cover - optional coherence integration
        return

    for check_result in failed_results:
        _coherence_bus.publish(
            DriftEvent(
                source_artifact="design_doc",
                target_artifact="implementation",
                change_type="deleted",
                payload={
                    "check_name": _dag_result_name(check_result),
                    "result": _format_dag_check_result(check_result),
                },
                severity="red",
                fix_strategy="auto",
                kind="dag_completeness",
            )
        )


def _publish_deployment_completeness_events(violations: list[Any]) -> None:
    if _coherence_bus is None:
        return

    try:
        from codd.coherence_engine import DriftEvent
    except Exception:  # pragma: no cover - optional coherence integration
        return

    for violation in violations:
        _coherence_bus.publish(
            DriftEvent(
                source_artifact="design_doc",
                target_artifact="implementation",
                change_type="modified",
                payload={
                    "design_doc": _violation_attr(violation, "design_doc"),
                    "broken_at": _violation_attr(violation, "broken_at"),
                    "message": _format_deployment_violation_message(violation),
                    "remediation": _violation_attr(violation, "remediation"),
                    "expected_chain": _violation_attr(violation, "expected_chain") or [],
                },
                severity="red",
                fix_strategy="auto",
                kind="deployment_completeness",
            )
        )


def _publish_user_journey_coherence_events(violations: list[Any]) -> None:
    """Publish one DriftEvent per C7 user_journey_coherence violation."""

    if _coherence_bus is None:
        return

    try:
        from codd.coherence_engine import DriftEvent
    except Exception:  # pragma: no cover - optional coherence integration
        return

    for violation in violations:
        severity = _user_journey_violation_severity(violation)
        _coherence_bus.publish(
            DriftEvent(
                source_artifact="design_doc",
                target_artifact="implementation",
                change_type="modified",
                payload={
                    "type": _violation_attr(violation, "type"),
                    "design_doc": _violation_attr(violation, "design_doc"),
                    "user_journey": _violation_attr(violation, "user_journey"),
                    "violation": violation,
                },
                severity=severity if severity in {"red", "amber", "green"} else "red",
                fix_strategy="auto" if severity == "red" else "hitl",
                kind="user_journey_coherence",
            )
        )


def _ntfy_deploy_incomplete(violations: list[Any], settings: dict[str, Any]) -> bool:
    topic = _deployment_ntfy_topic(settings)
    if not topic:
        return False

    message = _format_deploy_incomplete_ntfy(violations[0])
    request = Request(
        _ntfy_url(topic),
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "Priority": "urgent",
            "Tags": "warning",
            "Title": "CoDD deploy incomplete",
        },
    )
    try:
        with urlopen(request, timeout=5):
            return True
    except (OSError, URLError):
        return False


def _ntfy_user_journey_coherence_fail(violations: list[Any], settings: dict[str, Any]) -> bool:
    red_violations = _red_user_journey_violations(violations)
    if not red_violations:
        return False

    topic = _deployment_ntfy_topic(settings)
    if not topic:
        return False

    request = Request(
        _ntfy_url(topic),
        data=_format_user_journey_coherence_ntfy(red_violations).encode("utf-8"),
        method="POST",
        headers={
            "Priority": "urgent",
            "Tags": "warning",
            "Title": "CoDD user journey coherence",
        },
    )
    try:
        with urlopen(request, timeout=5):
            return True
    except (OSError, URLError):
        return False


def _deployment_ntfy_topic(settings: dict[str, Any]) -> str:
    env_topic = os.environ.get("CODD_NTFY_TOPIC") or os.environ.get("NTFY_TOPIC")
    if env_topic:
        return env_topic

    candidates = [
        settings.get("ntfy_topic"),
        _nested_setting(settings, "deployment", "ntfy_topic"),
        _nested_setting(settings, "deploy", "ntfy_topic"),
        _nested_setting(settings, "preflight", "ntfy_topic"),
        _nested_setting(settings, "requirement_completeness", "ntfy_topic"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _nested_setting(settings: dict[str, Any], section: str, key: str) -> Any:
    value = settings.get(section)
    if isinstance(value, dict):
        return value.get(key)
    return None


def _ntfy_url(topic: str) -> str:
    from codd.ask_user_question_adapter import _ntfy_url as build_ntfy_url

    return build_ntfy_url(topic)


def _format_deploy_incomplete_ntfy(violation: Any) -> str:
    design_doc = _violation_attr(violation, "design_doc") or "deployment chain"
    broken_at = _violation_attr(violation, "broken_at") or "unknown"
    remediation = _violation_attr(violation, "remediation") or "Complete the deployment verification chain."
    return f"CRITICAL deploy INCOMPLETE: {design_doc} -> {broken_at}\n{remediation}"


def _format_user_journey_coherence_ntfy(violations: list[Any]) -> str:
    return f"C7 user_journey_coherence FAIL: {len(violations)} violations"


def _format_deployment_violation_message(violation: Any) -> str:
    formatter = getattr(violation, "format_as_message", None)
    if callable(formatter):
        return str(formatter())
    return _format_deploy_incomplete_ntfy(violation)


def _parse_incomplete_chain_report(report: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(report)
    except json.JSONDecodeError:
        return []
    value = payload.get("incomplete_chain_report")
    return value if isinstance(value, list) else []


def _parse_user_journey_coherence_report(report: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(report)
    except json.JSONDecodeError:
        return []
    value = payload.get("user_journey_coherence_report")
    return value if isinstance(value, list) else []


def _red_user_journey_violations(violations: list[Any]) -> list[Any]:
    return [violation for violation in violations if _user_journey_violation_severity(violation) == "red"]


def _amber_user_journey_violations(violations: list[Any]) -> list[Any]:
    return [violation for violation in violations if _user_journey_violation_severity(violation) == "amber"]


def _user_journey_violation_severity(violation: Any) -> str:
    return str(_violation_attr(violation, "severity") or "red")


def _violation_attr(violation: Any, key: str) -> Any:
    if isinstance(violation, dict):
        return violation.get(key)
    if hasattr(violation, key):
        return getattr(violation, key)
    try:
        return asdict(violation).get(key)
    except TypeError:
        return None


def _validate_target_config(target_name: str, target_config: Any) -> None:
    if not isinstance(target_config, dict):
        raise CoddCLIError(f"Deploy target {target_name!r} must be a mapping")
    target_type = target_config.get("type")
    if not isinstance(target_type, str) or not target_type.strip():
        raise CoddCLIError(f"Deploy target {target_name!r} must define a non-empty type")

    ssh_key = target_config.get("ssh_key")
    if isinstance(ssh_key, str) and ssh_key.lstrip().startswith("-----BEGIN"):
        raise CoddCLIError("ssh_key must be a path reference, not private key content")

    healthcheck = target_config.get("healthcheck")
    if healthcheck is not None and not isinstance(healthcheck, dict):
        raise CoddCLIError(f"Deploy target {target_name!r} healthcheck must be a mapping")


def _select_target_name(config: dict[str, Any], target_name: str | None) -> str:
    targets = config["targets"]
    selected = target_name or config.get("default_target")
    if selected:
        if selected not in targets:
            raise CoddCLIError(f"Deploy target {selected!r} is not defined")
        return selected
    if len(targets) == 1:
        return next(iter(targets))
    raise CoddCLIError("Target is required when deploy.yaml has multiple targets")


def _run_target_healthcheck(target: Any, target_config: dict[str, Any], timeout_seconds: int) -> bool:
    healthcheck = target_config.get("healthcheck") or {}
    url = healthcheck.get("url")
    if url:
        return run_healthcheck(
            url=str(url),
            expected_status=int(healthcheck.get("expected_status", 200)),
            timeout_seconds=int(timeout_seconds or healthcheck.get("timeout_seconds", 60)),
            retries=int(healthcheck.get("retries", 1)),
        )
    return bool(target.healthcheck())


def _maybe_rollback(
    target: Any,
    snapshot: dict[str, Any],
    deploy_config: dict[str, Any],
    log_context: dict[str, Any],
) -> None:
    global_config = deploy_config.get("global") or {}
    if not bool(global_config.get("rollback_on_healthcheck_fail", True)):
        log_context["rollback_attempted"] = False
        return
    log_context["rollback_attempted"] = True
    log_context["rollback_succeeded"] = bool(target.rollback(snapshot))


def _emit_dry_run_actions(target_name: str, actions: list[str]) -> None:
    print("Proposed actions:")
    print(f"Target: {target_name}")
    for action in actions:
        print(f"- {action}")


def _write_deploy_log(
    project_root: Path,
    deploy_config: dict[str, Any],
    target_name: str,
    context: dict[str, Any],
) -> Path:
    global_config = deploy_config.get("global") or {}
    log_dir = Path(global_config.get("log_dir", "docs/reports/deploy_logs/")).expanduser()
    if not log_dir.is_absolute():
        log_dir = project_root / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc)
    safe_target = re.sub(r"[^A-Za-z0-9_.-]+", "-", target_name).strip("-") or "target"
    log_path = log_dir / f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}_{safe_target}.yaml"
    payload = {
        "timestamp": timestamp.isoformat(),
        **context,
    }
    log_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return log_path
