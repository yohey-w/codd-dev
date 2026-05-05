"""Python import based verification runner for repair attempts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from codd.config import find_codd_dir, load_project_config
from codd.dag import DAG, reset_dag_cache
from codd.dag.builder import build_dag, load_dag_settings
from codd.dag.runner import run_checks
from codd.deployment.providers import VERIFICATION_TEMPLATES
from codd.repair.schema import VerificationFailureReport


DEFAULT_CHECKS: tuple[str, ...] = (
    "node_completeness",
    "edge_validity",
    "depends_on_consistency",
    "task_completion",
    "transitive_closure",
    "deployment_completeness",
    "user_journey_coherence",
)


@dataclass
class VerificationFailure:
    check_name: str
    source: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationResult:
    passed: bool
    failures: list[VerificationFailure] = field(default_factory=list)
    check_results: list[Any] = field(default_factory=list)
    runtime_results: list[Any] = field(default_factory=list)
    failure: VerificationFailureReport | None = None


@dataclass
class _RuntimeVerificationState:
    identifier: str
    target: str
    project_root: Path
    actual_check_command: str | None = None
    journey: dict[str, Any] | None = None
    steps: list[Any] = field(default_factory=list)
    cdp_browser_config: dict[str, Any] | None = None


class VerifyRunner:
    """Run CoDD verification inside the current Python process."""

    def __init__(self, project_root: Path, codd_yaml: Mapping[str, Any] | None):
        self.project_root = Path(project_root).resolve()
        self.codd_yaml = dict(codd_yaml or {})

    def run(self) -> VerificationResult:
        """Reset DAG state, run C1-C7 checks, then run executable verification tests."""

        self.reset_dag_cache()
        if not self._has_codd_yaml():
            return self._error_result("codd_config", f"codd.yaml not found in {self.project_root}")

        try:
            settings = self._load_settings()
            dag_settings = load_dag_settings(self.project_root, settings)
            dag = build_dag(self.project_root, dag_settings)
            check_results = run_checks(dag, self.project_root, dag_settings, check_names=DEFAULT_CHECKS)
            failures = [
                failure
                for result in check_results
                for failure in [self._failure_from_check_result(result)]
                if failure is not None
            ]
            runtime_results = self._run_verification_tests(dag, settings)
            failures.extend(
                failure
                for result in runtime_results
                for failure in [self._failure_from_runtime_result(result)]
                if failure is not None
            )
        except Exception as exc:  # noqa: BLE001 - verification must fail gracefully for repair loop.
            return self._error_result("verification_error", str(exc))

        return VerificationResult(
            passed=not failures,
            failures=failures,
            check_results=check_results,
            runtime_results=runtime_results,
            failure=self._repair_failure_report(failures, dag),
        )

    def reset_dag_cache(self) -> None:
        """Clear DAG cache state before rebuilding."""

        reset_dag_cache(self.project_root)

    def _has_codd_yaml(self) -> bool:
        return bool(self.codd_yaml) or find_codd_dir(self.project_root) is not None

    def _load_settings(self) -> dict[str, Any]:
        if self.codd_yaml:
            return dict(self.codd_yaml)
        return load_project_config(self.project_root)

    def _run_verification_tests(self, dag: DAG, settings: dict[str, Any]) -> list[dict[str, Any]]:
        import codd.deployment.providers.verification  # noqa: F401

        results: list[dict[str, Any]] = []
        template_settings = _verification_template_settings(settings)
        for node in sorted(dag.nodes.values(), key=lambda item: item.id):
            if node.kind != "verification_test":
                continue
            template_ref = str(node.attributes.get("template_ref") or "").strip()
            if not template_ref:
                results.append(_runtime_result(node.id, "", False, "verification template ref is missing"))
                continue
            template_cls = VERIFICATION_TEMPLATES.get(template_ref)
            if template_cls is None:
                results.append(_runtime_result(node.id, template_ref, False, "verification template is not registered"))
                continue

            template_config = template_settings.get(template_ref, {})
            try:
                template = _new_template(template_cls, template_config)
                state = _runtime_state(node, self.project_root, template_config)
                test_kind = str(node.attributes.get("kind") or "")
                command = template.generate_test_command(state, test_kind)
                result = template.execute(command)
                results.append(
                    {
                        "check_name": "verification_test_runtime",
                        "node_id": node.id,
                        "template_ref": template_ref,
                        "command": command,
                        "passed": bool(getattr(result, "passed", False)),
                        "output": str(getattr(result, "output", "") or ""),
                        "duration": getattr(result, "duration", 0.0),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - one runtime test failure should not abort all checks.
                results.append(_runtime_result(node.id, template_ref, False, str(exc)))
        return results

    def _failure_from_check_result(self, result: Any) -> VerificationFailure | None:
        if _result_passed(result) or _result_severity(result) != "red":
            return None
        details = _plain_data(result)
        return VerificationFailure(
            check_name=str(details.get("check_name") or result.__class__.__name__),
            source="dag_check",
            message=_result_message(details),
            details=details,
        )

    def _failure_from_runtime_result(self, result: dict[str, Any]) -> VerificationFailure | None:
        if result.get("passed") is not False:
            return None
        return VerificationFailure(
            check_name="verification_test_runtime",
            source="verification_test_runtime",
            message=str(result.get("output") or "verification test failed"),
            details=dict(result),
        )

    def _error_result(self, check_name: str, message: str) -> VerificationResult:
        failure = VerificationFailure(check_name=check_name, source="verify_runner", message=message)
        return VerificationResult(
            passed=False,
            failures=[failure],
            failure=self._repair_failure_report([failure], None),
        )

    def _repair_failure_report(
        self,
        failures: list[VerificationFailure],
        dag: DAG | None,
    ) -> VerificationFailureReport | None:
        if not failures:
            return None
        return VerificationFailureReport(
            check_name=failures[0].check_name,
            failed_nodes=_failed_nodes(failures),
            error_messages=[failure.message for failure in failures],
            dag_snapshot=_dag_snapshot(dag),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )


def _new_template(template_cls: type[Any], template_config: dict[str, Any]) -> Any:
    if template_config:
        try:
            return template_cls(config=template_config)
        except TypeError:
            pass
    return template_cls()


def _runtime_state(node: Any, project_root: Path, template_config: dict[str, Any]) -> _RuntimeVerificationState:
    attributes = dict(getattr(node, "attributes", {}) or {})
    expected = attributes.get("expected_outcome") if isinstance(attributes.get("expected_outcome"), dict) else {}
    journey = expected.get("journey") if isinstance(expected.get("journey"), dict) else None
    steps = journey.get("steps") if isinstance(journey, dict) and isinstance(journey.get("steps"), list) else []
    target = attributes.get("target") or expected.get("target") or _journey_target(journey)
    return _RuntimeVerificationState(
        identifier=str(attributes.get("identifier") or getattr(node, "id", "")),
        target=str(target or ""),
        project_root=project_root,
        actual_check_command=_optional_string(attributes.get("actual_check_command") or expected.get("actual_check_command")),
        journey=journey,
        steps=steps,
        cdp_browser_config=template_config,
    )


def _journey_target(journey: dict[str, Any] | None) -> str:
    if not isinstance(journey, dict):
        return ""
    steps = journey.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict) or step.get("action") != "navigate":
                continue
            target = step.get("target") or step.get("url")
            if target:
                return str(target)
    return str(journey.get("target") or journey.get("url") or "")


def _verification_template_settings(settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    verification = settings.get("verification")
    templates = verification.get("templates") if isinstance(verification, dict) else None
    if not isinstance(templates, dict):
        return {}
    return {str(key): dict(value) for key, value in templates.items() if isinstance(value, Mapping)}


def _runtime_result(node_id: str, template_ref: str, passed: bool, output: str) -> dict[str, Any]:
    return {
        "check_name": "verification_test_runtime",
        "node_id": node_id,
        "template_ref": template_ref,
        "passed": passed,
        "output": output,
        "duration": 0.0,
    }


def _result_passed(result: Any) -> bool:
    return _result_value(result, "passed") is not False


def _result_severity(result: Any) -> str:
    return str(_result_value(result, "severity") or "red")


def _result_value(result: Any, key: str) -> Any:
    if isinstance(result, dict):
        return result.get(key)
    return getattr(result, key, None)


def _result_message(details: dict[str, Any]) -> str:
    message = details.get("message")
    if message:
        return str(message)
    for key in ("violations", "missing_impl_files", "orphan_edges", "dangling_refs", "incomplete_tasks"):
        value = details.get(key)
        if value:
            return f"{key}: {value}"
    return "verification check failed"


def _plain_data(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _plain_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain_data(item) for item in value]
    if isinstance(value, tuple):
        return [_plain_data(item) for item in value]
    if hasattr(value, "__dict__"):
        return {str(key): _plain_data(item) for key, item in vars(value).items()}
    return value


def _failed_nodes(failures: list[VerificationFailure]) -> list[str]:
    nodes: list[str] = []
    for failure in failures:
        _collect_node_refs(failure.details, nodes)
    return _dedupe(nodes)


def _collect_node_refs(value: Any, nodes: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"node", "node_id", "from_id", "to_id", "from_node", "to_node", "design_doc", "node_id"}:
                if isinstance(item, str) and item:
                    nodes.append(item)
            elif key in {"missing_impl_files", "dangling_refs", "unreachable_nodes", "failed_nodes"}:
                if isinstance(item, list):
                    nodes.extend(str(entry) for entry in item if isinstance(entry, str) and entry)
            else:
                _collect_node_refs(item, nodes)
        return
    if isinstance(value, list):
        for item in value:
            _collect_node_refs(item, nodes)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _dag_snapshot(dag: DAG | None) -> dict[str, Any]:
    if dag is None:
        return {"nodes": [], "edges": []}
    return {
        "node_count": len(dag.nodes),
        "edge_count": len(dag.edges),
        "nodes": sorted(dag.nodes)[:50],
        "edges": [
            {"from_id": edge.from_id, "to_id": edge.to_id, "kind": edge.kind}
            for edge in sorted(dag.edges, key=lambda item: (item.from_id, item.to_id, item.kind))[:50]
        ],
    }


def _optional_string(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


__all__ = [
    "DEFAULT_CHECKS",
    "VerificationFailure",
    "VerificationResult",
    "VerifyRunner",
]
