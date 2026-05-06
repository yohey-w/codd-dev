"""C9 environment coverage check."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codd.dag import DAG, Node
from codd.dag.checks import DagCheck, register_dag_check
from codd.dag.coverage_axes import CoverageAxis, CoverageVariant


@dataclass
class EnvironmentCoverageResult:
    check_name: str = "environment_coverage"
    severity: str = "red"
    status: str = "pass"
    message: str = ""
    block_deploy: bool = True
    violations: list[dict[str, Any]] = field(default_factory=list)
    passed: bool = True


@register_dag_check("environment_coverage")
class EnvironmentCoverageCheck(DagCheck):
    check_name = "environment_coverage"
    severity = "red"
    block_deploy = True

    def run(
        self,
        dag: DAG | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
    ) -> EnvironmentCoverageResult:
        target_dag = dag if dag is not None else self.dag
        if target_dag is None:
            raise ValueError("dag is required for environment_coverage check")
        if project_root is not None:
            self.project_root = Path(project_root)
        if settings is not None:
            self.settings = settings

        axes = [axis for axis in getattr(target_dag, "coverage_axes", []) if isinstance(axis, CoverageAxis)]
        if not axes:
            return EnvironmentCoverageResult(
                severity="info",
                status="pass",
                message="C9 environment_coverage PASS",
                block_deploy=self.block_deploy,
            )

        violations: list[dict[str, Any]] = []
        for axis in axes:
            for variant in axis.variants:
                related_tests = self._find_tests_for_axis_variant(target_dag, axis, variant)
                if variant.criticality is None:
                    violations.append(self._criticality_unclear(axis, variant))
                if not related_tests:
                    violations.append(self._missing_test(axis, variant))
                for journey in self._related_journeys(target_dag, axis):
                    if not self._test_executes_journey_under_variant(journey, related_tests):
                        violations.append(self._journey_not_executed(axis, variant, journey))

        red_count = sum(1 for violation in violations if violation.get("severity") == "red")
        amber_count = sum(1 for violation in violations if violation.get("severity") == "amber")
        if red_count:
            severity = "red"
            status = "fail"
            message = f"C9 environment_coverage found {red_count} red and {amber_count} amber violation(s)"
        elif amber_count:
            severity = "amber"
            status = "warn"
            message = f"C9 environment_coverage found {amber_count} amber violation(s)"
        else:
            severity = "info"
            status = "pass"
            message = "C9 environment_coverage PASS"

        return EnvironmentCoverageResult(
            severity=severity,
            status=status,
            message=message,
            block_deploy=self.block_deploy,
            violations=violations,
            passed=red_count == 0,
        )

    def format_report(self, result: EnvironmentCoverageResult | list[dict[str, Any]]) -> str:
        violations = result.violations if isinstance(result, EnvironmentCoverageResult) else result
        return json.dumps({"environment_coverage_report": violations}, ensure_ascii=False, indent=2, default=str)

    def _find_tests_for_axis_variant(
        self,
        dag: DAG,
        axis: CoverageAxis,
        variant: CoverageVariant,
    ) -> list[Node]:
        return [
            node
            for node in sorted(dag.nodes.values(), key=lambda item: item.id)
            if node.kind in {"test_file", "verification_test"} and self._node_mentions_axis_variant(node, axis, variant)
        ]

    def _related_journeys(self, dag: DAG, axis: CoverageAxis) -> list[dict[str, Any]]:
        journeys: list[dict[str, Any]] = []
        for node in sorted(dag.nodes.values(), key=lambda item: item.id):
            if node.kind != "design_doc":
                continue
            for item in self._as_list(node.attributes.get("user_journeys")):
                if not isinstance(item, dict):
                    continue
                if axis.source == "lexicon" or axis.owner_section == node.id or self._journey_mentions_axis(item, axis):
                    journeys.append({"name": str(item.get("name") or ""), "design_doc": node.id, "payload": item})
        return journeys

    def _test_executes_journey_under_variant(self, journey: dict[str, Any], related_tests: list[Node]) -> bool:
        if not related_tests:
            return False
        journey_name = str(journey.get("name") or "").strip()
        if not journey_name:
            return True
        return any(self._node_mentions_journey(node, journey_name) for node in related_tests)

    def _node_mentions_axis_variant(self, node: Node, axis: CoverageAxis, variant: CoverageVariant) -> bool:
        if self._value_mentions_axis_variant(node.attributes, axis, variant):
            return True
        text = self._node_text(node)
        return bool(text and self._text_mentions_axis_variant(text, axis, variant))

    def _node_mentions_journey(self, node: Node, journey_name: str) -> bool:
        if self._value_mentions_text(node.attributes, journey_name):
            return True
        text = self._node_text(node)
        return bool(text and journey_name in text)

    def _journey_mentions_axis(self, journey: dict[str, Any], axis: CoverageAxis) -> bool:
        return self._value_mentions_text(journey, axis.axis_type)

    def _value_mentions_axis_variant(self, value: Any, axis: CoverageAxis, variant: CoverageVariant) -> bool:
        if isinstance(value, dict):
            if self._mapping_mentions_axis_variant(value, axis, variant):
                return True
            return any(self._value_mentions_axis_variant(item, axis, variant) for item in value.values())
        if isinstance(value, list | tuple | set):
            return any(self._value_mentions_axis_variant(item, axis, variant) for item in value)
        if isinstance(value, str):
            return self._text_mentions_axis_variant(value, axis, variant)
        return False

    def _mapping_mentions_axis_variant(
        self,
        value: dict[str, Any],
        axis: CoverageAxis,
        variant: CoverageVariant,
    ) -> bool:
        axis_value = value.get("axis_type") or value.get("axis")
        variant_value = value.get("variant_id") or value.get("variant") or value.get("id")
        if str(axis_value or "").strip() == axis.axis_type and str(variant_value or "").strip() == variant.id:
            return True

        if str(axis_value or "").strip() == axis.axis_type:
            variants = self._as_list(value.get("variants") or value.get("variant_ids"))
            if variant.id in {str(item).strip() for item in variants}:
                return True
        return False

    def _text_mentions_axis_variant(self, text: str, axis: CoverageAxis, variant: CoverageVariant) -> bool:
        return axis.axis_type in text and variant.id in text

    def _value_mentions_text(self, value: Any, text: str) -> bool:
        if not text:
            return False
        if isinstance(value, dict):
            return any(self._value_mentions_text(item, text) for item in value.values())
        if isinstance(value, list | tuple | set):
            return any(self._value_mentions_text(item, text) for item in value)
        return isinstance(value, str) and text in value

    def _node_text(self, node: Node) -> str:
        if self.project_root is None or not node.path:
            return ""
        path = self.project_root / node.path
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")

    def _missing_test(self, axis: CoverageAxis, variant: CoverageVariant) -> dict[str, Any]:
        return {
            "type": "missing_test_for_variant",
            "axis_type": axis.axis_type,
            "variant_id": variant.id,
            "rationale": axis.rationale,
            "criticality": variant.criticality,
            "severity": self._severity_for_variant(variant),
            "source": axis.source,
            "owner_section": axis.owner_section,
        }

    def _journey_not_executed(
        self,
        axis: CoverageAxis,
        variant: CoverageVariant,
        journey: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "type": "journey_not_executed_under_variant",
            "journey": journey.get("name"),
            "design_doc": journey.get("design_doc"),
            "axis_type": axis.axis_type,
            "variant_id": variant.id,
            "severity": self._severity_for_variant(variant),
            "criticality": variant.criticality,
        }

    def _criticality_unclear(self, axis: CoverageAxis, variant: CoverageVariant) -> dict[str, Any]:
        return {
            "type": "variant_criticality_unclear",
            "axis_type": axis.axis_type,
            "variant_id": variant.id,
            "severity": "amber",
            "source": axis.source,
            "owner_section": axis.owner_section,
        }

    def _severity_for_variant(self, variant: CoverageVariant) -> str:
        return "red" if variant.criticality in {"critical", "high"} else "amber"

    def _as_list(self, value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, tuple | set):
            return list(value)
        return [value]
